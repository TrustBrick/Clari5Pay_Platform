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
from typing import Optional, Sequence

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    AccountLedgerEntry, AccountMaster, AccountTransaction, Transaction, TxStatus, TxType,
    WithdrawalPayoutLeg,
)

# Entry types.
WITHDRAWAL_PAYOUT = "WITHDRAWAL_PAYOUT"
MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"

# Directions.
CREDIT = "CREDIT"
DEBIT = "DEBIT"

# Payout payment methods (Feature 2). BANK debits a managed account; MANUAL is an offline payment
# that deliberately has no payout account.
PAYMENT_METHODS = ("BANK", "MANUAL")

# Payout-leg lifecycle (see models.WithdrawalPayoutLeg). ALLOCATED holds capacity, PAID has moved
# money, RELEASED has given the capacity back. Defined here because both the ledger and the
# allocation engine need them and the ledger is the lower layer.
LEG_ALLOCATED = "ALLOCATED"
LEG_PAID = "PAID"
LEG_RELEASED = "RELEASED"

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
    #
    # A withdrawal SPLIT across several accounts cannot be expressed by the single
    # `payout_account_ref` column — each account paid only its own share — so those rows are
    # excluded here and counted through their legs below instead. A single-account payout writes
    # BOTH the column (which every existing screen reads) and one leg, so it would otherwise be
    # counted twice; excluding every legged row and adding the legs back is what keeps exactly one
    # of the two in the total, whichever shape the payout took.
    legged = select(WithdrawalPayoutLeg.transaction_ref).where(
        WithdrawalPayoutLeg.status == LEG_PAID).scalar_subquery()
    explicit = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
            Transaction.type.in_(_WITHDRAWAL_TYPES + _SETTLEMENT_TYPES),
            Transaction.status == TxStatus.COMPLETED,
            Transaction.payout_account_ref == ref,
            Transaction.ref.notin_(legged),
        )
    )).scalar_one()

    # This account's share of every withdrawal actually paid from it — the authoritative figure for
    # a split payout, and the same figure as `explicit` for a single-account one.
    legs = (await db.execute(
        select(func.coalesce(func.sum(WithdrawalPayoutLeg.amount), 0.0)).where(
            WithdrawalPayoutLeg.account_ref == ref,
            WithdrawalPayoutLeg.status == LEG_PAID,
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

    return round(
        float(deposits) - float(explicit) - float(legs) - float(legacy)
        + await adjustments_net(db, ref), 2)


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
    """The FIRST payout entry already posted for this withdrawal, if any.

    The idempotency lookup: a withdrawal with any payout entry has already been paid, whether it
    was settled from one account or split across several. Callers use it as a boolean guard, so
    returning the first of a split's entries is the right answer — "this has been paid".
    """
    return (await db.execute(
        select(AccountLedgerEntry)
        .where(
            AccountLedgerEntry.entry_type == WITHDRAWAL_PAYOUT,
            AccountLedgerEntry.transaction_ref == transaction_ref,
        )
        .order_by(AccountLedgerEntry.id)
        .limit(1)
    )).scalar_one_or_none()


async def find_payout_entries(db: AsyncSession, transaction_ref: str) -> list[AccountLedgerEntry]:
    """EVERY payout entry posted for this withdrawal, in leg order — one per account it was paid
    from. A single-account payout returns one entry; a three-way split returns three."""
    return list((await db.execute(
        select(AccountLedgerEntry)
        .where(
            AccountLedgerEntry.entry_type == WITHDRAWAL_PAYOUT,
            AccountLedgerEntry.transaction_ref == transaction_ref,
        )
        .order_by(AccountLedgerEntry.id)
    )).scalars().all())


async def reserved_by_legs(db: AsyncSession, refs: Optional[Sequence[str]] = None) -> dict[str, float]:
    """{account reference -> rupees already promised out of it by ALLOCATED-but-unpaid legs}.

    Money that is spoken for but has not moved yet. It is deliberately NOT part of
    :func:`account_balance`, which reports the account's real accounting balance and must keep
    agreeing with the Account Management screen. It is what the ALLOCATION engine subtracts before
    deciding whether an account can carry another withdrawal, so two requests arriving seconds
    apart cannot each be allocated the same headroom.
    """
    stmt = (
        select(WithdrawalPayoutLeg.account_ref,
               func.coalesce(func.sum(WithdrawalPayoutLeg.amount), 0.0))
        .where(WithdrawalPayoutLeg.status == LEG_ALLOCATED)
        .group_by(WithdrawalPayoutLeg.account_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        stmt = stmt.where(WithdrawalPayoutLeg.account_ref.in_(refs))
    return {ref: round(float(total or 0.0), 2) for ref, total in (await db.execute(stmt)).all() if ref}


async def account_balances(db: AsyncSession, refs: Sequence[str]) -> dict[str, float]:
    """{account reference -> authoritative balance}, for MANY accounts in a fixed number of queries.

    Exactly the arithmetic :func:`account_balance` performs, evaluated for a whole set at once.
    The allocation engine measures every configured account on every withdrawal — fifteen or more
    of them — and calling the single-account function in a loop would issue five queries per
    account on the hot path of every request. The two must never disagree, so this is the same
    four terms in the same order, grouped instead of filtered.
    """
    refs = list(refs)
    if not refs:
        return {}

    deposits = dict((await db.execute(
        select(Transaction.admin_ref, func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(
            Transaction.type.in_(_DEPOSIT_TYPES),
            Transaction.status.in_(_COMPLETED_DEPOSIT),
            Transaction.admin_ref.in_(refs),
        )
        .group_by(Transaction.admin_ref)
    )).all())

    legged = select(WithdrawalPayoutLeg.transaction_ref).where(
        WithdrawalPayoutLeg.status == LEG_PAID).scalar_subquery()
    explicit = dict((await db.execute(
        select(Transaction.payout_account_ref, func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(
            Transaction.type.in_(_WITHDRAWAL_TYPES + _SETTLEMENT_TYPES),
            Transaction.status == TxStatus.COMPLETED,
            Transaction.payout_account_ref.in_(refs),
            Transaction.ref.notin_(legged),
        )
        .group_by(Transaction.payout_account_ref)
    )).all())

    legs = dict((await db.execute(
        select(WithdrawalPayoutLeg.account_ref,
               func.coalesce(func.sum(WithdrawalPayoutLeg.amount), 0.0))
        .where(WithdrawalPayoutLeg.account_ref.in_(refs), WithdrawalPayoutLeg.status == LEG_PAID)
        .group_by(WithdrawalPayoutLeg.account_ref)
    )).all())

    adjustments = dict((await db.execute(
        select(
            AccountLedgerEntry.account_ref,
            func.coalesce(func.sum(
                case((AccountLedgerEntry.direction == CREDIT, AccountLedgerEntry.amount),
                     else_=-AccountLedgerEntry.amount)
            ), 0.0),
        )
        .where(AccountLedgerEntry.entry_type == MANUAL_ADJUSTMENT,
               AccountLedgerEntry.account_ref.in_(refs))
        .group_by(AccountLedgerEntry.account_ref)
    )).all())

    # Legacy debits: a completed withdrawal/settlement that names no payout account is charged to
    # the member's most-recent receiving account, exactly as the single-account function does.
    member_map = await _member_accounts(db)
    wanted = set(refs)
    members_by_ref: dict[str, list[str]] = {}
    for member, acct in member_map.items():
        if acct in wanted:
            members_by_ref.setdefault(acct, []).append(member)
    legacy: dict[str, float] = {}
    if members_by_ref:
        all_members = [m for members in members_by_ref.values() for m in members]
        # ONE expression object, reused in the select list, the filter and the GROUP BY.
        #
        # Writing `func.upper(func.trim(func.coalesce(...)))` out three times builds three
        # separate expressions, and the empty-string default in each becomes its OWN bind
        # parameter ($5, $6, ...). Postgres compares GROUP BY against the select list
        # syntactically, sees `coalesce(member_id, $5)` and `coalesce(member_id, $6)`, and rejects
        # the query with "column transactions.member_id must appear in the GROUP BY clause".
        # SQLite accepts it, so the whole test suite passes and the failure only appears on a real
        # database — which is exactly what happened. Binding it once keeps the three renderings
        # identical.
        member_key = func.upper(func.trim(func.coalesce(Transaction.member_id, "")))
        rows = (await db.execute(
            select(member_key, func.coalesce(func.sum(Transaction.amount), 0.0))
            .where(
                Transaction.type.in_(_WITHDRAWAL_TYPES + _SETTLEMENT_TYPES),
                Transaction.status == TxStatus.COMPLETED,
                Transaction.payout_account_ref.is_(None),
                Transaction.payout_payment_method.is_distinct_from("MANUAL"),
                member_key.in_(all_members),
            )
            .group_by(member_key)
        )).all()
        by_member = {m: float(total or 0.0) for m, total in rows}
        for ref, members in members_by_ref.items():
            legacy[ref] = sum(by_member.get(m, 0.0) for m in members)

    return {
        ref: round(
            float(deposits.get(ref, 0.0)) - float(explicit.get(ref, 0.0))
            - float(legs.get(ref, 0.0)) - float(legacy.get(ref, 0.0))
            + float(adjustments.get(ref, 0.0)),
            2,
        )
        for ref in refs
    }


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
    leg_no: Optional[int] = None,
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
    # A payout entry ALWAYS carries a leg number, defaulting to 1 — the single-account case. That
    # default is what keeps the double-debit guarantee intact now that uniqueness spans the leg:
    # Postgres treats NULLs in a UNIQUE index as distinct, so leaving it NULL would have let a
    # replayed completion insert a second entry for the same withdrawal. With the default, the
    # second attempt collides on (WITHDRAWAL_PAYOUT, WIT000123, 1) and the database refuses it.
    # Manual adjustments have no transaction and stay NULL, so an account may hold any number.
    if leg_no is None and transaction_ref:
        leg_no = 1
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
        leg_no=leg_no,
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
        "legNo": e.leg_no,
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
