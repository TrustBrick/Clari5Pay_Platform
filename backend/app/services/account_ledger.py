"""Accounting ledger for the Admin-managed bank accounts (``account_master``).

ONE place that answers two questions for a managed account:

  1. **What is its authoritative balance right now?** — ``account_balance()``. This is the SAME
     formula ``/api/accounts/balances`` has always used (completed deposits in, completed
     withdrawals/settlements out), plus the net of any manual adjustments, which have no
     transaction to be derived from. No second balance store is introduced: the balance stays
     derived, so the figure here and the figure on the Account Management screen cannot drift.

  2. **How is a movement recorded?** — ``post_entry()``. Every debit/credit writes ONE immutable
     ``AccountLedgerEntry`` carrying the account, amount, balance before/after, actor, reason and
     timestamp. Entries are write-once; a mistake is corrected with a compensating entry, never by
     editing history.

Concurrency and atomicity are the caller's transaction: every writer first takes
``lock_account()`` (``SELECT … FOR UPDATE`` on the ``account_master`` row), so two operators
adjusting or paying out from the same account serialise — the second reads the first's committed
result rather than a stale balance. Because the whole sequence (balance read → validate → ledger
insert → account/transaction update → audit) runs inside the request's single session, the
surrounding ``get_db`` commit makes it all-or-nothing.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AccountLedgerEntry, AccountMaster, AccountTransaction, Transaction, TxStatus, TxType

# Entry types.
WITHDRAWAL_PAYOUT = "WITHDRAWAL_PAYOUT"
MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"

# Directions.
CREDIT = "CREDIT"
DEBIT = "DEBIT"

# Payout payment methods (Feature 2). BANK debits a managed account; MANUAL is an offline payment
# that deliberately has no payout account.
PAYMENT_METHODS = ("BANK", "MANUAL")

# The reasons an authorised user may pick for a manual adjustment. A closed list keeps the ledger
# reportable — free text goes in Remarks.
ADJUSTMENT_REASONS = (
    "Bank Charges",
    "Interest Credit",
    "Offline Payment",
    "Reconciliation Correction",
    "Reversal / Chargeback",
    "Opening Balance",
    "Other",
)

# Transaction type/status groupings, mirroring routes/transactions.py so the balance basis here is
# byte-for-byte the one the rest of the platform uses.
_DEPOSIT_TYPES = (TxType.DEPOSIT, TxType.DEPOSIT_REQUEST)
_WITHDRAWAL_TYPES = (TxType.WITHDRAWAL, TxType.WITHDRAWAL_REQUEST)
_SETTLEMENT_TYPES = (TxType.SETTLEMENT, TxType.SETTLEMENT_REQUEST)
_COMPLETED_DEPOSIT = (TxStatus.COMPLETED, TxStatus.DEPOSITED)

IST = timezone(timedelta(hours=5, minutes=30))


def ist_stamp(now: Optional[datetime] = None) -> str:
    """The platform's standard human-facing IST timestamp string."""
    return (now or datetime.now(IST)).astimezone(IST).strftime("%d %b %Y, %I:%M %p") + " IST"


def _norm_member(m: Optional[str]) -> str:
    return (m or "").strip().upper()


async def lock_account(db: AsyncSession, ref: str) -> Optional[AccountMaster]:
    """Take a row lock on the managed account and return it (None when it does not exist).

    Every balance-affecting writer must call this FIRST. It is what stops two concurrent
    adjustments from both reading ₹1,00,000 and each writing their own "after" figure: the second
    transaction blocks here until the first commits, then computes from the real, current balance.
    """
    return (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref).with_for_update()
    )).scalar_one_or_none()


async def _member_accounts(db: AsyncSession) -> dict[str, str]:
    """member id → most-recent receiving account, the legacy attribution for a completed debit.

    A withdrawal completed before the payout-account step existed carries no account of its own,
    so it is attributed back to the account its member deposits into — exactly what
    ``/api/accounts/balances`` does. Rows that DO carry ``payout_account_ref`` never consult this.
    """
    out: dict[str, str] = {}
    links = (await db.execute(
        select(AccountTransaction.member_id, AccountTransaction.reference_number)
        .order_by(AccountTransaction.id.desc())
    )).all()
    for member_id, ref in links:
        key = _norm_member(member_id)
        if key and ref and key not in out:
            out[key] = ref
    return out


async def adjustments_net(db: AsyncSession, ref: str) -> float:
    """Net effect of every manual adjustment on this account (credits − debits)."""
    total = (await db.execute(
        select(func.coalesce(func.sum(
            case((AccountLedgerEntry.direction == CREDIT, AccountLedgerEntry.amount),
                 else_=-AccountLedgerEntry.amount)
        ), 0.0)).where(
            AccountLedgerEntry.account_ref == ref,
            AccountLedgerEntry.entry_type == MANUAL_ADJUSTMENT,
        )
    )).scalar_one()
    return float(total or 0.0)


async def account_balance(db: AsyncSession, ref: str) -> float:
    """The authoritative balance of one managed account.

        deposits received  −  withdrawals paid out  −  settlements  +  net manual adjustments

    The first three terms are the existing derived figure (``/api/accounts/balances``'s
    ``available``), so this never disagrees with the Account Management screen. A completed
    withdrawal/settlement attributes to its EXPLICIT ``payout_account_ref`` when it has one, and
    otherwise falls back to the member's most-recent receiving account — the historical rule,
    left in place so figures for rows completed before Feature 2 are unchanged.

    Excluded from the debit side: a withdrawal explicitly paid MANUAL/offline, which by definition
    did not come out of any managed bank account.
    """
    deposits = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.type.in_(_DEPOSIT_TYPES),
            Transaction.status.in_(_COMPLETED_DEPOSIT),
            Transaction.admin_ref == ref,
        )
    )).scalar_one()

    # Debits explicitly recorded against this account by the payout step.
    explicit = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.type.in_(_WITHDRAWAL_TYPES + _SETTLEMENT_TYPES),
            Transaction.status == TxStatus.COMPLETED,
            Transaction.payout_account_ref == ref,
        )
    )).scalar_one()

    # Legacy debits (no explicit payout account) attributed via the member map. A row paid
    # MANUAL/offline is skipped — no managed account was touched.
    member_map = await _member_accounts(db)
    members = [m for m, acct in member_map.items() if acct == ref]
    legacy = 0.0
    if members:
        legacy = (await db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
                Transaction.type.in_(_WITHDRAWAL_TYPES + _SETTLEMENT_TYPES),
                Transaction.status == TxStatus.COMPLETED,
                Transaction.payout_account_ref.is_(None),
                Transaction.payout_payment_method.is_distinct_from("MANUAL"),
                func.upper(func.trim(func.coalesce(Transaction.member_id, ""))).in_(members),
            )
        )).scalar_one()

    return round(float(deposits) - float(explicit) - float(legacy) + await adjustments_net(db, ref), 2)


_ENTRY_PREFIX = {MANUAL_ADJUSTMENT: "ADJ", WITHDRAWAL_PAYOUT: "LED"}


async def _next_entry_ref(db: AsyncSession, entry_type: str) -> str:
    """Next immutable ledger reference (ADJ000001 / LED000001).

    Uses a Postgres sequence like the transaction references do, so numbering is concurrency-safe
    and never reuses a value. Existing transaction reference IDs are untouched — this is its own
    sequence in its own namespace.
    """
    prefix = _ENTRY_PREFIX.get(entry_type, "LED")
    n = (await db.execute(text("SELECT nextval('account_ledger_ref_seq')"))).scalar_one()
    return f"{prefix}{str(n).zfill(6)}"


async def find_by_client_request(db: AsyncSession, client_request_id: Optional[str]) -> Optional[AccountLedgerEntry]:
    """The entry a previous, identical submit already created — the idempotency lookup."""
    if not client_request_id:
        return None
    return (await db.execute(
        select(AccountLedgerEntry).where(AccountLedgerEntry.client_request_id == client_request_id)
    )).scalar_one_or_none()


async def find_payout_entry(db: AsyncSession, transaction_ref: str) -> Optional[AccountLedgerEntry]:
    """The payout entry already posted for this withdrawal, if any (idempotency lookup)."""
    return (await db.execute(
        select(AccountLedgerEntry).where(
            AccountLedgerEntry.entry_type == WITHDRAWAL_PAYOUT,
            AccountLedgerEntry.transaction_ref == transaction_ref,
        )
    )).scalar_one_or_none()


async def post_entry(
    db: AsyncSession,
    *,
    entry_type: str,
    direction: str,
    amount: float,
    account: Optional[AccountMaster],
    balance_before: Optional[float],
    performed_by: Optional[str] = None,
    performed_by_id: Optional[int] = None,
    performed_by_role: Optional[str] = None,
    transaction_ref: Optional[str] = None,
    transaction_id: Optional[int] = None,
    payment_method: Optional[str] = None,
    reason: Optional[str] = None,
    reference: Optional[str] = None,
    remarks: Optional[str] = None,
    description: Optional[str] = None,
    merchant_business: Optional[str] = None,
    merchant_id: Optional[int] = None,
    member_id: Optional[str] = None,
    client_request_id: Optional[str] = None,
) -> AccountLedgerEntry:
    """Write ONE immutable ledger entry and return it.

    ``balance_before`` must be the value read from ``account_balance()`` under the row lock taken
    by ``lock_account()``; ``balance_after`` is derived here so the arithmetic lives in one place.
    Both are NULL when the entry has no account (a MANUAL/offline payout).

    The row is added to the caller's session and flushed, never committed: it lands together with
    the withdrawal/account update and the audit record, or not at all.
    """
    amount = round(float(amount), 2)
    after = None if balance_before is None else round(
        balance_before + amount if direction == CREDIT else balance_before - amount, 2
    )
    entry = AccountLedgerEntry(
        entry_ref=await _next_entry_ref(db, entry_type),
        entry_type=entry_type,
        direction=direction,
        amount=amount,
        account_ref=account.reference_number if account else None,
        account_id=account.id if account else None,
        balance_before=None if balance_before is None else round(balance_before, 2),
        balance_after=after,
        transaction_ref=transaction_ref,
        transaction_id=transaction_id,
        payment_method=payment_method,
        reason=reason,
        reference=reference,
        remarks=remarks,
        description=description,
        performed_by=performed_by,
        performed_by_id=performed_by_id,
        performed_by_role=performed_by_role,
        created_at=datetime.utcnow(),
        created_at_ist=ist_stamp(),
        merchant_business=merchant_business,
        merchant_id=merchant_id,
        member_id=member_id,
        client_request_id=client_request_id,
    )
    db.add(entry)
    await db.flush()
    return entry


def serialize(e: AccountLedgerEntry) -> dict:
    """API shape for one ledger entry (camelCase, matching the rest of the API)."""
    return {
        "id": e.id,
        "entryRef": e.entry_ref,
        "entryType": e.entry_type,
        "direction": e.direction,
        "amount": round(e.amount or 0.0, 2),
        "accountRef": e.account_ref,
        "balanceBefore": None if e.balance_before is None else round(e.balance_before, 2),
        "balanceAfter": None if e.balance_after is None else round(e.balance_after, 2),
        "transactionRef": e.transaction_ref,
        "paymentMethod": e.payment_method,
        "reason": e.reason,
        "reference": e.reference,
        "remarks": e.remarks,
        "description": e.description,
        "performedBy": e.performed_by,
        "performedByRole": e.performed_by_role,
        "createdAt": (e.created_at.isoformat() + "Z") if e.created_at else None,
        "createdAtIst": e.created_at_ist,
        "merchantBusiness": e.merchant_business,
        "memberId": e.member_id,
    }
