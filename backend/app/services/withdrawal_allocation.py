"""Automatic withdrawal payout account allocation.

THE single authority for "which managed bank account should PAY this withdrawal?". The merchant
never picks one and the Admin never picks one per request — the Admin configures accounts, their
daily debit limits and their payout capabilities in Account Management, and this service decides.
The frontend renders the result; it never re-implements any of the rules below.

This is the debit-side counterpart of ``services/deposit_allocation``, and it is deliberately a
SEPARATE service. The two answer opposite questions against opposite limits — a deposit asks who
may RECEIVE money and is bounded by ``highest_credit``; a withdrawal asks who may SEND it and is
bounded by ``highest_debit`` AND by the money actually sitting in the account. Sharing one engine
would mean one set of rules pretending to be two. What IS shared is the pure note parsing
(``deposit_allocation.parse_note`` and friends), because "Use Bank of Baroda" means the same thing
whichever direction the money is going.

WHAT MAKES A LIMIT A LIMIT
──────────────────────────
``AccountMaster.highest_debit`` is a HARD DAILY DEBIT CEILING. The test is never
``amount <= highest_debit``; it is always

    debit_used_today(account) + amount  <=  highest_debit

so an account with a ₹1,00,000 ceiling that has already paid out ₹70,000 today can pay ₹30,000 and
cannot pay ₹30,001. Equality passes — reaching the ceiling exactly is allowed; exceeding it by a
single paisa is not.

``highest_debit`` used to be a high-water MARK that ``transactions._track_account_debit`` raised to
match any larger completed debit. A ceiling that moves up to accommodate whatever arrives is not a
ceiling, so that write is gone (the same correction ``highest_credit`` received). The configured
value now changes in exactly one place: an Admin editing it, which is audited.

TWO INDEPENDENT CEILINGS
────────────────────────
A withdrawal must clear BOTH, and they fail for different reasons and are fixed differently:

  • **Available balance** — the money really in the account, from the existing accounting ledger
    (``services/account_ledger.account_balance``). No second balance store is introduced.
  • **Remaining debit capacity** — ``highest_debit − debit_used_today``, a policy limit that
    resets at midnight IST.

An account's USABLE CAPACITY for one withdrawal is the smaller of the two. An account with plenty
of money and no headroom is excluded, and so is one with headroom and no money.

WHAT "USED TODAY" COUNTS — AND WHY IT ISN'T ONLY COMPLETED PAYOUTS
─────────────────────────────────────────────────────────────────
A withdrawal consumes an account's capacity from the moment the account is allocated to it, not
from the moment the payment is made. If only completed payouts counted, two requests arriving
seconds apart would both read "₹0 used", both be allocated the same account, and together breach
the ceiling — the exact failure this service exists to prevent. So today's usage is every payout
leg dated today that is still LIVE (``ALLOCATED``) or settled (``PAID``), plus any legacy debit
recorded straight onto a transaction before payout legs existed.

Rejecting or cancelling a withdrawal RELEASES its legs, and with them their capacity. There is no
separate reservation record to keep in step: the leg IS the reservation, and it becomes the debit.

CONCURRENCY
───────────
Planning runs unlocked; claiming takes ``SELECT … FOR UPDATE`` on every account in the plan, in
ascending reference order, and re-verifies balance and capacity while holding them. A consistent
global lock order is what makes this deadlock-free — two allocations wanting overlapping sets of
accounts always contend on the lowest shared reference first, so neither can be holding what the
other needs next. If re-verification fails (another request took the headroom between the plan and
the lock), one final pass locks EVERY configured account in the same order and re-plans against
figures that can no longer move.

NO PARTIAL PAYOUT
─────────────────
The legs of an allocation sum to EXACTLY the requested amount. If the eligible accounts together
cannot cover it, nothing is allocated and the withdrawal becomes an explicit exception — never a
smaller withdrawal, and never a partially-covered one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    AccountMaster, AccountType, MerchantBankAccount, Transaction, TxStatus, TxType,
    WithdrawalAllocation, WithdrawalPayoutLeg,
)
from app.services import account_ledger as ledger
# Pure, direction-agnostic note reading, reused rather than re-written. Nothing in the deposit
# engine is called that touches deposit data or deposit limits.
from app.services.deposit_allocation import NoteRequest, parse_note

IST = timezone(timedelta(hours=5, minutes=30))

WITHDRAWAL_TYPES = (TxType.WITHDRAWAL, TxType.WITHDRAWAL_REQUEST)

# A withdrawal in one of these states has been abandoned, so its legs are released and the
# capacity they held returns to the account. Every other state — from MANAGER_REVIEW through
# COMPLETED — still consumes it.
RELEASED_STATUSES = (TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED)

# The platform's existing transaction modes for a bank payout. These are the SAME four the deposit
# side already uses (services/deposit_allocation.ALLOCATABLE_DEPOSIT_TYPES); no new mode is
# invented here. "BANK" is the platform's generic bank-transfer mode and is treated as satisfied by
# any of IMPS/NEFT/RTGS capability — it names the rail, not a specific scheme.
TRANSACTION_MODES = ("UPI", "IMPS", "NEFT", "RTGS")
BANK_MODE = "BANK"
BANK_SCHEMES = ("IMPS", "NEFT", "RTGS")

# Payout modes that are NOT paid out of a managed bank account at all, and therefore never reach
# this engine: cash is handed over in person and crypto leaves a wallet. Both keep the manual
# workflow they have always had.
NON_BANK_PAYOUT_MODES = ("CASH", "CRYPTO")


# ── Allocation rules (machine-readable; each maps to one human sentence) ────────────────────────
class RULES:
    SAME_ACCOUNT = "SAME_ACCOUNT_REQUEST"
    NOTE_BANK = "MERCHANT_NOTE_BANK_PREFERENCE"
    BENEFICIARY_KNOWN = "BENEFICIARY_ALREADY_PAID_FROM_ACCOUNT"
    NEAREST_CAPACITY = "NEAREST_SUITABLE_DEBIT_CAPACITY"
    SPLIT = "MULTI_ACCOUNT_SPLIT"


_RULE_TEXT = {
    RULES.SAME_ACCOUNT: "Same account requested by merchant note + eligible",
    RULES.NOTE_BANK: "Bank requested by merchant note + eligible + nearest remaining debit capacity",
    RULES.BENEFICIARY_KNOWN: (
        "Beneficiary already paid from this account + eligible + nearest remaining debit capacity"),
    RULES.NEAREST_CAPACITY: "Eligible + nearest remaining debit capacity",
    RULES.SPLIT: "No single account could cover the amount — split across the fewest eligible accounts",
}

# Why a candidate was rejected — recorded per account on the allocation journal, so a support
# question ("why not Bank of Baroda?") has a stored answer rather than a re-run against data that
# has since moved on.
REJECT_INACTIVE = "ACCOUNT_NOT_AVAILABLE"
REJECT_NO_LIMIT = "NO_DEBIT_LIMIT_CONFIGURED"
REJECT_MODE = "TRANSACTION_MODE_NOT_SUPPORTED"
REJECT_NO_CAPACITY = "DAILY_DEBIT_LIMIT_REACHED"
REJECT_NO_BALANCE = "INSUFFICIENT_AVAILABLE_BALANCE"
REJECT_LOCKED = "CONCURRENTLY_ALLOCATED"

# The distinct ways an allocation can find nothing. Each one has a different fix — activate an
# account, raise a limit, enable a mode, move money in, or wait for tomorrow — so each is reported
# separately rather than as one "no eligible account" sentence.
FAIL_NO_ACCOUNTS = "NO_ACCOUNTS_CONFIGURED"
FAIL_ALL_UNAVAILABLE = "ALL_ACCOUNTS_UNAVAILABLE"
FAIL_NO_LIMITS = "NO_DEBIT_LIMITS_CONFIGURED"
FAIL_MODE_UNAVAILABLE = "NO_ACCOUNT_SUPPORTS_TRANSACTION_MODE"
FAIL_LIMIT_REACHED = "ALL_ACCOUNTS_AT_DAILY_DEBIT_LIMIT"
FAIL_NO_BALANCE = "INSUFFICIENT_PAYOUT_BALANCE"
FAIL_CAPACITY = "NO_SUFFICIENT_PAYOUT_CAPACITY"
FAIL_BENEFICIARY = "BENEFICIARY_DETAILS_INVALID"
FAIL_MIXED = "NO_ELIGIBLE_PAYOUT_ACCOUNT"
FAIL_RACE = "CAPACITY_TAKEN_CONCURRENTLY"

# Outcomes recorded on the allocation journal.
OUTCOME_ALLOCATED = "ALLOCATED"     # one account carries the whole withdrawal
OUTCOME_SPLIT = "SPLIT"             # several accounts share it
OUTCOME_NO_ACCOUNT = "NO_ACCOUNT"   # nothing eligible — an exception, not a queue

# Why a set of legs was released.
RELEASE_REALLOCATED = "REALLOCATED"
RELEASE_REJECTED = "WITHDRAWAL_REJECTED"
RELEASE_CANCELLED = "WITHDRAWAL_CANCELLED"
RELEASE_MANUAL = "PAID_MANUALLY"

# Locking one account per plan is the normal path; the fallback locks every configured account.
# One retry is enough: after the fallback the figures cannot move again inside this transaction.
_MAX_CLAIM_ATTEMPTS = 2


def ist_today(now: Optional[datetime] = None) -> date:
    """The current business date in IST — the boundary the daily debit limit resets on."""
    return (now or datetime.now(IST)).astimezone(IST).date()


def ist_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime("%d %b %Y, %I:%M %p") + " IST"


def _money(value: Optional[float]) -> float:
    """Round to paise. Every limit comparison goes through this, so ₹0.01 over is really over and
    float drift can never quietly widen a ceiling."""
    return round(float(value or 0.0), 2)


def _norm_member(member_id: Optional[str]) -> str:
    return (member_id or "").strip().upper()


def _norm_account_number(value: Optional[str]) -> str:
    """A beneficiary account number compared the way operators actually type it — spaces, dashes
    and casing removed — so the same destination matches across two requests that were keyed in
    slightly differently."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _type_value(account_type) -> str:
    return account_type.value if hasattr(account_type, "value") else str(account_type or "")


# ═══ 1. Transaction mode ════════════════════════════════════════════════════════════════════════

def normalize_mode(mode: Optional[str]) -> str:
    """The requested transaction mode, upper-cased, defaulting to the platform's generic BANK.

    An unrecognised value is returned as-is rather than silently rewritten: an account is then
    matched against it literally, which fails closed (no account claims to support a mode nobody
    has configured) instead of quietly falling back to something permissive.
    """
    value = (mode or "").strip().upper()
    return value or BANK_MODE


def account_modes(account: AccountMaster) -> Optional[set[str]]:
    """The modes this account can pay by, or None meaning "every mode".

    None is the UNCONFIGURED state and it means fully capable, deliberately. A capability column
    that started out empty and was read as "supports nothing" would disqualify every account on a
    platform where no Admin has configured one, and send every withdrawal to the exception queue —
    the same failure a hard UPI-link filter caused on the deposit side. Capability may narrow a
    choice between eligible accounts; it must never be the reason there is no eligible account.
    """
    raw = (account.payout_modes or "").strip()
    if not raw:
        return None
    modes = {m.strip().upper() for m in raw.split(",") if m.strip()}
    return modes or None


def supports_mode(account: AccountMaster, mode: str) -> bool:
    """Whether this account can process the requested transaction mode (Rule 7).

    ``BANK`` is the platform's generic bank-transfer mode — the merchant asked for a bank
    transfer without naming the scheme — so any account that can do IMPS, NEFT or RTGS satisfies
    it. Naming a scheme explicitly is matched exactly.
    """
    modes = account_modes(account)
    if modes is None:
        return True
    if mode == BANK_MODE:
        return bool(modes & set(BANK_SCHEMES)) or BANK_MODE in modes
    return mode in modes


# ═══ 2. Daily debit usage ═══════════════════════════════════════════════════════════════════════

async def debit_used_today(
    db: AsyncSession, refs: Optional[Sequence[str]] = None, *, on: Optional[date] = None,
) -> dict[str, float]:
    """{account reference → rupees debited or committed from it today}. Accounts with no activity
    are absent from the mapping (callers read them as 0.0).

    Two sources, added together and never overlapping:

      • **Payout legs** dated today that are ALLOCATED or PAID. This is every withdrawal this
        engine has placed — capacity held from the moment of allocation, which is what stops two
        concurrent requests spending the same headroom.
      • **Legacy debits**: a completed withdrawal/settlement that names its payout account
        directly and has no legs. These predate payout legs (or are settlements, which are never
        split), and they really did leave the account today, so they count against the day's
        limit. A row that HAS legs is excluded here and counted above, so nothing is doubled.

    Computed straight from those rows: there is no stored counter to drift out of step, and no
    nightly job whose failure would silently free up capacity.
    """
    day = on or ist_today()

    leg_stmt = (
        select(WithdrawalPayoutLeg.account_ref,
               func.coalesce(func.sum(WithdrawalPayoutLeg.amount), 0.0))
        .where(
            WithdrawalPayoutLeg.leg_date == day,
            WithdrawalPayoutLeg.status.in_((ledger.LEG_ALLOCATED, ledger.LEG_PAID)),
        )
        .group_by(WithdrawalPayoutLeg.account_ref)
    )
    legged = select(WithdrawalPayoutLeg.transaction_ref).scalar_subquery()
    legacy_stmt = (
        select(Transaction.payout_account_ref, func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(
            Transaction.payout_account_ref.isnot(None),
            Transaction.status == TxStatus.COMPLETED,
            Transaction.tx_date == day,
            Transaction.ref.notin_(legged),
        )
        .group_by(Transaction.payout_account_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        leg_stmt = leg_stmt.where(WithdrawalPayoutLeg.account_ref.in_(refs))
        legacy_stmt = legacy_stmt.where(Transaction.payout_account_ref.in_(refs))

    out: dict[str, float] = {}
    for ref, total in (await db.execute(leg_stmt)).all():
        if ref:
            out[ref] = _money(total)
    for ref, total in (await db.execute(legacy_stmt)).all():
        if ref:
            out[ref] = _money(out.get(ref, 0.0) + _money(total))
    return out


async def withdrawal_counts_today(
    db: AsyncSession, refs: Optional[Sequence[str]] = None, *, on: Optional[date] = None,
) -> dict[str, int]:
    """{account reference → how many payout legs were placed on it today}. Shown in Account
    Management, and the tie-break when two accounts offer the same remaining capacity."""
    day = on or ist_today()
    stmt = (
        select(WithdrawalPayoutLeg.account_ref, func.count(WithdrawalPayoutLeg.id))
        .where(
            WithdrawalPayoutLeg.leg_date == day,
            WithdrawalPayoutLeg.status.in_((ledger.LEG_ALLOCATED, ledger.LEG_PAID)),
        )
        .group_by(WithdrawalPayoutLeg.account_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        stmt = stmt.where(WithdrawalPayoutLeg.account_ref.in_(refs))
    return {ref: int(n or 0) for ref, n in (await db.execute(stmt)).all() if ref}


def remaining_debit(account: AccountMaster, used_today: float) -> float:
    """Rupees this account may still pay out today. Never negative: an account already at or past
    its ceiling has zero capacity, not a licence to go further."""
    return max(0.0, _money(_money(account.highest_debit) - _money(used_today)))


# ═══ 3. Beneficiary ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Beneficiary:
    """The receiver this withdrawal pays, read off the request.

    Beneficiary Management on this platform is the merchant's own saved destinations
    (``MerchantBankAccount``, scoped per Member ID) — there is no per-payout-account beneficiary
    registry, and inventing one would be a second ownership model the project does not have. So
    the rule is expressed against what does exist: a beneficiary is VALID when the request carries
    the details the existing withdrawal form requires for its mode, and it is KNOWN TO AN ACCOUNT
    when that account has already paid this same destination before.
    """
    mode: str = BANK_MODE
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
    name: Optional[str] = None
    bank_name: Optional[str] = None
    upi_id: Optional[str] = None
    saved: bool = False                  # present in the merchant's saved destinations
    valid: bool = True
    invalid_reason: Optional[str] = None

    @property
    def key(self) -> str:
        """The comparison key for "is this the same destination?" — the normalised account number
        for a bank payout, the VPA for a UPI one."""
        if self.mode == "UPI":
            return (self.upi_id or "").strip().lower()
        return _norm_account_number(self.account_number)


def read_beneficiary(
    *, mode: str, account_number: Optional[str] = None, ifsc: Optional[str] = None,
    name: Optional[str] = None, bank_name: Optional[str] = None,
    payout_details: Optional[dict] = None,
) -> Beneficiary:
    """Read and validate the beneficiary off a withdrawal request (Rules 3 and 8).

    The requirement is the one the existing withdrawal form already enforces — a bank payout needs
    a holder, an account number and an IFSC; a UPI payout needs a VPA — so nothing new is demanded
    of an operator here. What changes is that the requirement is now checked SERVER-SIDE before an
    account is allocated, and a request that fails it produces an explicit exception rather than
    being quietly allocated to an account that could never pay it.
    """
    details = payout_details or {}
    ben = Beneficiary(
        mode=mode,
        account_number=(account_number or details.get("accountNumber") or "").strip() or None,
        ifsc=(ifsc or details.get("ifsc") or "").strip().upper() or None,
        name=(name or details.get("accountHolder") or "").strip() or None,
        bank_name=(bank_name or details.get("bank") or details.get("bankName") or "").strip() or None,
        upi_id=(details.get("upiId") or "").strip() or None,
    )
    if mode == "UPI":
        if not ben.upi_id:
            ben.valid, ben.invalid_reason = False, "The withdrawal has no UPI ID to pay."
    else:
        missing = [label for label, value in (
            ("Account Holder", ben.name), ("Account Number", ben.account_number), ("IFSC", ben.ifsc),
        ) if not value]
        if missing:
            ben.valid = False
            ben.invalid_reason = (
                f"The withdrawal's beneficiary details are incomplete — {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} missing.")
    return ben


async def beneficiary_is_saved(
    db: AsyncSession, merchant_id: Optional[int], member_id: Optional[str], ben: Beneficiary,
) -> bool:
    """Whether this destination is already in the merchant's saved beneficiaries for this member.

    Read through the platform's existing Beneficiary Management table; nothing is added to it here
    (the withdrawal create flow already saves a new destination on the merchant's behalf). Used as
    a ranking signal and recorded on the journal — never as a hard filter, because the existing
    workflow lets an operator type a fresh destination and that must keep working.
    """
    if not merchant_id or not ben.key:
        return False
    stmt = select(MerchantBankAccount.id).where(MerchantBankAccount.merchant_id == merchant_id)
    if member_id:
        stmt = stmt.where(MerchantBankAccount.member_id == member_id)
    if ben.mode == "UPI":
        stmt = stmt.where(func.lower(func.trim(MerchantBankAccount.upi_id)) == ben.key)
    else:
        rows = (await db.execute(
            stmt.where(MerchantBankAccount.account_number.isnot(None))
        )).scalars().all()
        if not rows:
            return False
        numbers = (await db.execute(
            select(MerchantBankAccount.account_number)
            .where(MerchantBankAccount.id.in_(rows))
        )).scalars().all()
        return any(_norm_account_number(n) == ben.key for n in numbers)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def accounts_that_paid_beneficiary(db: AsyncSession, ben: Beneficiary) -> set[str]:
    """Which managed accounts have already paid THIS destination (Rule 15).

    Derived from real payout history — the legs this engine wrote, plus the payout account
    recorded straight on older completed withdrawals — so it needs no new registry and is true of
    accounts that served the beneficiary before this feature existed. An account here is preferred
    over one that has never paid the destination, provided it passes every mandatory rule; the
    preference never excuses a balance or limit failure.
    """
    if not ben.key or ben.mode == "UPI":
        # A UPI payout's destination is a VPA held in the request's JSON payout_details, which is
        # not a queryable column. Rather than scan every withdrawal to synthesise a preference,
        # this returns nothing — the allocation simply falls through to the capacity rules, which
        # is the correct answer, not a degraded one.
        return set()

    matched_refs = (await db.execute(
        select(Transaction.ref, Transaction.account_number, Transaction.payout_account_ref)
        .where(
            Transaction.type.in_(WITHDRAWAL_TYPES),
            Transaction.status == TxStatus.COMPLETED,
            Transaction.account_number.isnot(None),
        )
    )).all()
    refs = {r for r, number, _acct in matched_refs if _norm_account_number(number) == ben.key}
    if not refs:
        return set()

    out = {acct for r, _number, acct in matched_refs if r in refs and acct}
    out |= set((await db.execute(
        select(WithdrawalPayoutLeg.account_ref)
        .where(WithdrawalPayoutLeg.transaction_ref.in_(refs),
               WithdrawalPayoutLeg.status == ledger.LEG_PAID)
        .distinct()
    )).scalars().all())
    return {r for r in out if r}


# ═══ 4. Member payout history ═══════════════════════════════════════════════════════════════════

async def last_payout_account(
    db: AsyncSession, member_id: Optional[str], *, exclude_tx_id: Optional[int] = None,
) -> Optional[str]:
    """The account this member's PREVIOUS withdrawal was paid from — what "same account" means on
    the debit side (Rule 14).

    Deliberately not the member's deposit history: "use the same account" on a withdrawal is a
    request to pay from the account that paid last time, and answering it with the account that
    RECEIVED their last deposit would name a different account for the same words.
    """
    mid = _norm_member(member_id)
    if not mid:
        return None
    stmt = (
        select(Transaction.ref, Transaction.payout_account_ref)
        .where(
            Transaction.type.in_(WITHDRAWAL_TYPES),
            func.upper(func.trim(func.coalesce(Transaction.member_id, ""))) == mid,
            Transaction.status.notin_(RELEASED_STATUSES),
        )
        .order_by(Transaction.id.desc())
        .limit(25)
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None
    refs = [r for r, _ in rows]
    # A split payout's accounts live on its legs; the largest leg is the account that carried most
    # of it and is the one "the same account" sensibly names.
    legs = (await db.execute(
        select(WithdrawalPayoutLeg.transaction_ref, WithdrawalPayoutLeg.account_ref,
               WithdrawalPayoutLeg.amount)
        .where(WithdrawalPayoutLeg.transaction_ref.in_(refs),
               WithdrawalPayoutLeg.status.in_((ledger.LEG_ALLOCATED, ledger.LEG_PAID)))
        .order_by(WithdrawalPayoutLeg.amount.desc())
    )).all()
    by_ref: dict[str, str] = {}
    for txn_ref, acct, _amount in legs:
        by_ref.setdefault(txn_ref, acct)
    for ref, explicit in rows:
        if by_ref.get(ref):
            return by_ref[ref]
        if explicit:
            return explicit
    return None


# ═══ 5. Candidate evaluation ════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """One managed account measured against this specific withdrawal."""
    account: AccountMaster
    used_today: float
    remaining: float                  # highest_debit − used today
    balance: float                    # spendable: accounting balance − capacity already promised
    reserved: float                   # promised by ALLOCATED-but-unpaid legs
    payouts_today: int
    mode_ok: bool
    beneficiary_known: bool           # this account has paid this destination before
    eligible: bool
    reject_reason: Optional[str] = None

    @property
    def ref(self) -> str:
        return self.account.reference_number

    @property
    def usable(self) -> float:
        """Rule 17's usable capacity: the most this account could contribute to this withdrawal —
        the smaller of the money it holds and the headroom it has left today.

        An account with NO configured daily limit is bounded only by its balance. It is never
        chosen automatically (see :func:`_evaluate`), but it can be paid from when an operator
        names it, and reporting its capacity as zero would make the audit record of that payout
        read as impossible.
        """
        if not self.eligible:
            return 0.0
        if _money(self.account.highest_debit) <= 0:
            return _money(self.balance)
        return _money(min(self.balance, self.remaining))

    def snapshot(self) -> dict:
        """The point-in-time record of this account's position, for the audit journal."""
        return {
            "accountRef": self.ref,
            "accountName": self.account.account_name,
            "bankName": self.account.bank_name,
            "accountType": _type_value(self.account.account_type),
            "isOwnAccount": bool(self.account.is_own_account),
            "status": self.account.status,
            "payoutModes": sorted(account_modes(self.account) or TRANSACTION_MODES),
            "highestDebit": _money(self.account.highest_debit),
            "debitUsedToday": _money(self.used_today),
            "remainingCapacity": _money(self.remaining),
            "availableBalance": _money(self.balance),
            "reservedByAllocations": _money(self.reserved),
            "usableCapacity": self.usable,
            "payoutsToday": self.payouts_today,
            "beneficiaryKnown": self.beneficiary_known,
            "eligible": self.eligible,
            "rejectReason": self.reject_reason,
        }


def _evaluate(
    account: AccountMaster, amount: float, *, mode: str, used_today: float, balance: float,
    reserved: float, payouts_today: int, beneficiary_known: bool, require_full: bool,
    operator_directed: bool = False,
) -> Candidate:
    """Apply every HARD rule to one account. Cheapest disqualification first.

    ``require_full`` distinguishes the two questions the engine asks. Choosing a SINGLE account
    demands it carry the whole withdrawal, so both ceilings are tested against the full amount.
    Assembling a SPLIT only needs an account able to contribute something, so the tests become
    "has any headroom" and "has any money" — each leg is then capped at that account's own usable
    capacity, so no individual account is ever taken past either ceiling.
    """
    remaining = remaining_debit(account, used_today)
    cand = Candidate(
        account=account, used_today=_money(used_today), remaining=remaining,
        balance=_money(balance), reserved=_money(reserved), payouts_today=payouts_today,
        mode_ok=True, beneficiary_known=beneficiary_known, eligible=False,
    )

    # Rule 5 — availability. An inactive/disabled account can never pay.
    if (account.status or "").upper() != "ACTIVE":
        cand.reject_reason = REJECT_INACTIVE
        return cand

    # Rule 7 — the account must support the requested transaction mode.
    if not supports_mode(account, mode):
        cand.mode_ok = False
        cand.reject_reason = REJECT_MODE
        return cand

    # An account with no configured ceiling has no capacity to give, so the engine will not CHOOSE
    # it: an unconfigured limit must never read as permission to pay any amount.
    #
    # An operator naming that account explicitly is a different act. They are not asking the engine
    # to decide how much is safe — they are recording a payment against an account for which no
    # daily policy exists, so there is no configured ceiling to breach. Refusing it would strand
    # real money on every account whose limit an Admin has not got round to setting. Every other
    # rule — active, supports the mode, holds the balance — still applies, and those are the ones
    # an explicit reference could otherwise be used to skip.
    unlimited = _money(account.highest_debit) <= 0
    if unlimited and not operator_directed:
        cand.reject_reason = REJECT_NO_LIMIT
        return cand

    # Rule 10 — the hard daily debit limit, always measured as used + amount vs the ceiling.
    if unlimited:
        pass
    elif require_full:
        if _money(_money(used_today) + _money(amount)) > _money(account.highest_debit):
            cand.reject_reason = REJECT_NO_CAPACITY
            return cand
    elif remaining <= 0:
        cand.reject_reason = REJECT_NO_CAPACITY
        return cand

    # Rule 9 — the money has to actually be there.
    if require_full:
        if _money(balance) < _money(amount):
            cand.reject_reason = REJECT_NO_BALANCE
            return cand
    elif _money(balance) <= 0:
        cand.reject_reason = REJECT_NO_BALANCE
        return cand

    cand.eligible = True
    return cand


async def evaluate_accounts(
    db: AsyncSession, amount: float, *, mode: str, beneficiary: Optional[Beneficiary] = None,
    on: Optional[date] = None, require_full: bool = True,
    accounts: Optional[Sequence[AccountMaster]] = None, operator_directed: bool = False,
) -> list[Candidate]:
    """Measure every managed account against this withdrawal and return them all — eligible or not.

    The rejected ones are kept deliberately: they are what the allocation journal records as the
    reason a particular account was not chosen.
    """
    if accounts is None:
        accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    accounts = list(accounts)
    if not accounts:
        return []
    refs = [a.reference_number for a in accounts]
    used = await debit_used_today(db, refs, on=on)
    counts = await withdrawal_counts_today(db, refs, on=on)
    # The accounting balance, then the part of it already promised to allocated-but-unpaid
    # withdrawals. Subtracting the second is what stops two requests being allocated the same
    # money; it is NOT folded into account_balance(), which must keep reporting the account's real
    # balance to Account Management.
    balances = await ledger.account_balances(db, refs)
    reserved = await ledger.reserved_by_legs(db, refs)
    known = await accounts_that_paid_beneficiary(db, beneficiary) if beneficiary else set()

    return [
        _evaluate(
            a, amount, mode=mode,
            used_today=used.get(a.reference_number, 0.0),
            balance=_money(balances.get(a.reference_number, 0.0)
                           - reserved.get(a.reference_number, 0.0)),
            reserved=reserved.get(a.reference_number, 0.0),
            payouts_today=counts.get(a.reference_number, 0),
            beneficiary_known=a.reference_number in known,
            require_full=require_full, operator_directed=operator_directed,
        )
        for a in accounts
    ]


# ═══ 6. Ranking ═════════════════════════════════════════════════════════════════════════════════

def rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Rule 13 — nearest suitable capacity first.

    Among accounts that can all carry the withdrawal, the one with the SMALLEST remaining debit
    capacity wins: filling a ₹50,000 gap with a ₹45,000 payout leaves the ₹95,000 account free for
    a request only it can take. Ties break deterministically — fewer payouts today first
    (spreading the day's traffic), then by reference number so the same inputs always produce the
    same answer. Nothing here is random.
    """
    return sorted(candidates, key=lambda c: (c.remaining, c.payouts_today, c.ref))


def _shortfall_text(amount: float, combined: Optional[float], contributors: int) -> str:
    """Why a withdrawal could not be covered even by combining accounts.

    Stated as a shortfall against the COMBINED usable capacity, because that is the number that
    has to change. A message naming the largest single account is actively misleading in an engine
    that splits across banks: it invites an Admin to raise one limit when the amount exceeds the
    total of all of them.
    """
    money = f"₹{amount:,.2f}"
    if combined is None:
        return (f"No sufficient payout capacity available for {money}, even combining accounts "
                f"across banks.")
    short = _money(_money(amount) - _money(combined))
    where = (f"across {contributors} eligible account{'s' if contributors != 1 else ''}"
             if contributors else "across the eligible accounts")
    return (f"{money} cannot be paid: automatic allocation combined every eligible account, "
            f"across all banks, and they can cover ₹{combined:,.2f} in total {where} — short by "
            f"₹{short:,.2f}. Fund an account, raise a daily Highest Debit, or add an account.")


def _failure(candidates: list[Candidate], amount: float, mode: str,
             combined: Optional[float] = None, contributors: int = 0) -> tuple[str, str]:
    """(code, human sentence) for why nothing could be allocated.

    Distinguishes the cases an Admin resolves differently: an account that is switched off is
    activated, an unconfigured limit is set, a mode is enabled, a limit that is merely used up
    frees itself tomorrow, and an empty account needs funding. Derived from the per-account
    rejection reasons already recorded, so it can never disagree with them.
    """
    if not candidates:
        return FAIL_NO_ACCOUNTS, "No accounts are configured in Account Management."
    reasons = [c.reject_reason for c in candidates]
    n = len(candidates)
    money = f"₹{amount:,.2f}"

    if all(r == REJECT_INACTIVE for r in reasons):
        return FAIL_ALL_UNAVAILABLE, (
            f"All {n} account(s) are unavailable — every one is set INACTIVE. "
            f"Activate an account in Account Management.")
    if all(r in (REJECT_INACTIVE, REJECT_MODE) for r in reasons) and REJECT_MODE in reasons:
        return FAIL_MODE_UNAVAILABLE, (
            f"No available account can pay by {mode}. Enable {mode} on an account in Account "
            f"Management, or use a different transaction mode.")
    if all(r in (REJECT_INACTIVE, REJECT_MODE, REJECT_NO_LIMIT) for r in reasons):
        return FAIL_NO_LIMITS, (
            "No account has a Highest Debit configured, so none can pay out. "
            "Set a daily debit limit in Account Management.")

    capacity_blocked = [c for c in candidates if c.reject_reason == REJECT_NO_CAPACITY]
    balance_blocked = [c for c in candidates if c.reject_reason == REJECT_NO_BALANCE]

    if capacity_blocked and not balance_blocked:
        biggest_ceiling = max(_money(c.account.highest_debit) for c in capacity_blocked)
        if _money(amount) > biggest_ceiling:
            # Being larger than every SINGLE ceiling is not by itself a reason to fail — the
            # engine splits across accounts, and across banks, precisely for this case. By the
            # time this runs the split has already been tried and could not cover the amount, so
            # the honest explanation is the COMBINED capacity, not the biggest single limit.
            # Reporting the ceiling here read as "no account is big enough", which sent Admins to
            # raise one limit when the shortfall was across all of them.
            return FAIL_CAPACITY, _shortfall_text(amount, combined, contributors)
        left = max(c.remaining for c in capacity_blocked)
        return FAIL_LIMIT_REACHED, (
            f"Every eligible account has reached its daily debit limit for {money} — the most any "
            f"has left today is ₹{left:,.2f}. Capacity resets at midnight IST, or raise a limit.")

    if balance_blocked and not capacity_blocked:
        richest = max(c.balance for c in balance_blocked)
        return FAIL_NO_BALANCE, (
            f"No account holds enough to pay {money} — the largest available balance is "
            f"₹{richest:,.2f}. Fund an account or split the withdrawal.")

    if capacity_blocked or balance_blocked:
        return FAIL_CAPACITY, _shortfall_text(amount, combined, contributors)

    return FAIL_MIXED, (
        f"No account can pay {money} — all {n} are unavailable, out of daily debit capacity or "
        f"short of balance.")


# ═══ 7. The allocation plan ═════════════════════════════════════════════════════════════════════

@dataclass
class Leg:
    """One account's share of the plan."""
    candidate: Candidate
    amount: float

    @property
    def ref(self) -> str:
        return self.candidate.ref


@dataclass
class AllocationResult:
    """The engine's answer. ``legs`` is empty when nothing was eligible — a clear, explicit
    no-account state, never a fallback to an account that cannot pay."""
    legs: list[Leg] = field(default_factory=list)
    rule: Optional[str] = None
    reason: str = ""
    mode: str = BANK_MODE
    note: NoteRequest = field(default_factory=NoteRequest)
    beneficiary: Optional[Beneficiary] = None
    requested_unavailable: bool = False       # the note named a bank/account nothing eligible matched
    failure_code: Optional[str] = None
    candidates: list[Candidate] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def allocated(self) -> bool:
        return bool(self.legs)

    @property
    def split(self) -> bool:
        return len(self.legs) > 1

    @property
    def outcome(self) -> str:
        if not self.legs:
            return OUTCOME_NO_ACCOUNT
        return OUTCOME_SPLIT if self.split else OUTCOME_ALLOCATED

    @property
    def total(self) -> float:
        return _money(sum(l.amount for l in self.legs))

    @property
    def primary(self) -> Optional[Candidate]:
        """The single paying account, or the largest leg of a split — what the existing
        ``payout_account_ref`` column and every screen reading it should show."""
        if not self.legs:
            return None
        return max(self.legs, key=lambda l: l.amount).candidate

    def snapshot(self) -> list[dict]:
        """The immutable payout card(s) stored on the withdrawal and shown to the merchant.

        Deliberately limited to what identifies the paying account: no capacity, no balance, no
        ranking and no other candidate. The account number is masked — the merchant is being told
        WHICH account pays them, not given its full details, and the platform's existing masking
        rule for a payout account is what applies.
        """
        out = []
        for leg in self.legs:
            a = leg.candidate.account
            number = a.account_number or ""
            out.append({
                "legNo": len(out) + 1,
                "accountRef": a.reference_number,
                "accountName": a.account_name,
                "bankName": a.bank_name,
                "accountNumberMasked": ("•••• " + number[-4:]) if len(number) >= 4 else number,
                "ifsc": a.ifsc_code,
                "branch": a.branch,
                "accountType": _type_value(a.account_type),
                "transactionMode": self.mode,
                "amount": _money(leg.amount),
            })
        return out


def _preference_pools(
    eligible: list[Candidate], note: NoteRequest, *, same_account_ref: Optional[str],
) -> list[tuple[str, list[Candidate]]]:
    """The ordered preference pools, most preferred first (Rules 2, 14, 15).

    Every pool is already hard-filtered, so a preference can only ever change WHICH eligible
    account is picked — never whether an ineligible one becomes acceptable. The last pool is
    always the full eligible set, so a preference can narrow the choice but can never empty it.
    """
    pools: list[tuple[str, list[Candidate]]] = []

    # Rule 14 — an explicit "same account" request, if that account is still eligible.
    if note.same_account and same_account_ref:
        same = [c for c in eligible if c.ref == same_account_ref]
        if same:
            pools.append((RULES.SAME_ACCOUNT, same))

    # Rule 15 — accounts that have already paid this beneficiary.
    known = [c for c in eligible if c.beneficiary_known]
    if known and len(known) < len(eligible):
        pools.append((RULES.BENEFICIARY_KNOWN, known))

    pools.append((RULES.NOTE_BANK if note.has_preference else RULES.NEAREST_CAPACITY, eligible))
    return pools


def _split(candidates: list[Candidate], amount: float,
           preferred: Optional[set[str]] = None, *,
           required: Optional[set[str]] = None) -> list[Leg]:
    """Rule 17 — cover ``amount`` with the FEWEST eligible accounts, and cover it exactly.

    Largest usable capacity first is what minimises the number of accounts: no other choice of the
    same size can cover more, so if k accounts suffice at all, the k largest suffice.

    Two kinds of preference reach this function, and they are NOT the same strength:

    ``preferred`` is a HINT — accounts that already know the beneficiary, or the account the
    member was last paid from. It is honoured by trying a hint-first ordering and keeping it only
    when it needs no more accounts than the pure-capacity answer, so a hint can shape the split
    but never make it wider.

    ``required`` is the merchant's OWN INSTRUCTION — the bank (or account) their note named,
    in the case where that bank cannot cover the whole amount by itself. Rule 4 of the
    bank-preference rule says its capacity is spent FIRST and other banks supply only the
    remainder, so this ordering is the answer rather than a tie-break: it stands even when
    draining the named bank costs an extra leg. Ignoring it is how "Use HDFC" ended up paid
    entirely by ICICI and BOB while an eligible HDFC account sat untouched.

    The final leg is trimmed to the exact remainder, so the legs sum to the requested amount to
    the paisa. No leg ever exceeds its own account's usable capacity.
    """
    amount = _money(amount)
    if amount <= 0:
        return []

    def _cover(ordered: list[Candidate]) -> list[Leg]:
        legs: list[Leg] = []
        left = amount
        for cand in ordered:
            if left <= 0:
                break
            take = _money(min(cand.usable, left))
            if take <= 0:
                continue
            legs.append(Leg(candidate=cand, amount=take))
            left = _money(left - take)
        return legs if left <= 0 else []

    if required:
        # The named bank's capacity first, then the rest largest-first so the REMAINDER is still
        # carried by the fewest other accounts. Ordering cannot change whether the amount is
        # coverable at all, so an empty result here still means Rule 18 and never a part-payment.
        return _cover(sorted(
            candidates,
            key=lambda c: (0 if c.ref in required else 1, -c.usable, c.payouts_today, c.ref)))

    by_capacity = sorted(candidates, key=lambda c: (-c.usable, c.payouts_today, c.ref))
    baseline = _cover(by_capacity)
    if not baseline:
        return []           # even everything together cannot cover it — Rule 18
    if not preferred:
        return baseline

    preference_first = sorted(
        candidates, key=lambda c: (0 if c.ref in preferred else 1, -c.usable, c.payouts_today, c.ref))
    shaped = _cover(preference_first)
    return shaped if shaped and len(shaped) <= len(baseline) else baseline


# ═══ 8. Claiming (concurrency) ══════════════════════════════════════════════════════════════════

async def _lock_accounts(db: AsyncSession, refs: Sequence[str]) -> dict[str, AccountMaster]:
    """Row-lock these accounts, ALWAYS in ascending reference order.

    The consistent order is the whole safety argument, and it only holds if every caller acquires
    the WHOLE set it may need in one pass: two allocations then contend on the lowest shared
    reference first and one waits, rather than each holding a row the other is reaching for.
    :func:`allocate_withdrawal_accounts` calls this exactly once, before planning, for that
    reason — do not add a second, narrower acquisition alongside it.

    Locks are held until the caller's transaction commits, so the legs written under them are
    visible to the next request's usage query before it can act.
    """
    out: dict[str, AccountMaster] = {}
    for ref in sorted(set(refs)):
        acc = (await db.execute(
            select(AccountMaster).where(AccountMaster.reference_number == ref).with_for_update()
        )).scalar_one_or_none()
        if acc is not None:
            out[ref] = acc
    return out


async def _verify_under_lock(
    db: AsyncSession, legs: list[Leg], *, mode: str, on: Optional[date],
) -> bool:
    """Re-check every leg against figures read while its account row is locked.

    The plan was built from an unlocked read; by now another request may have consumed the
    capacity it counted on. This is the check that decides whether the plan still holds — the
    ranking is advisory, this is authoritative.
    """
    if not legs:
        return False
    refs = [l.ref for l in legs]
    used = await debit_used_today(db, refs, on=on)
    balances = await ledger.account_balances(db, refs)
    reserved = await ledger.reserved_by_legs(db, refs)
    for leg in legs:
        acc = leg.candidate.account
        if (acc.status or "").upper() != "ACTIVE" or not supports_mode(acc, mode):
            return False
        used_today = _money(used.get(leg.ref, 0.0))
        balance = _money(_money(balances.get(leg.ref, 0.0)) - _money(reserved.get(leg.ref, 0.0)))
        if (_money(acc.highest_debit) > 0
                and _money(used_today + _money(leg.amount)) > _money(acc.highest_debit)):
            return False
        if balance < _money(leg.amount):
            return False
        # Refresh the snapshot the journal and the legs record, so what is stored is what was
        # actually true at the moment of the claim rather than at the moment of the plan.
        leg.candidate.used_today = used_today
        leg.candidate.remaining = remaining_debit(acc, used_today)
        leg.candidate.balance = balance
        leg.candidate.reserved = _money(reserved.get(leg.ref, 0.0))
    return True


# ═══ 9. Allocation ══════════════════════════════════════════════════════════════════════════════

async def allocate_withdrawal_accounts(
    db: AsyncSession,
    *,
    amount: float,
    mode: Optional[str] = None,
    member_id: Optional[str] = None,
    merchant_id: Optional[int] = None,
    note: Optional[str] = None,
    beneficiary: Optional[Beneficiary] = None,
    on: Optional[date] = None,
    exclude_tx_id: Optional[int] = None,
    force_account_ref: Optional[str] = None,
) -> AllocationResult:
    """Select the account — or the combination of accounts — that will pay one withdrawal.

    The caller supplies only the withdrawal's own facts. Every rule, every figure and the final
    choice are decided here, under row locks, from data in the database.

    Nothing is written by this function except the locks it holds: attaching the legs to the
    withdrawal is :func:`record_allocation`'s job, inside the caller's transaction, so the
    allocation and the withdrawal row land together or not at all.
    """
    amount = _money(amount)
    mode = normalize_mode(mode)
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    if force_account_ref:
        # An Admin directing the payout at ONE named account — the manual escape valve, kept for
        # the cases the engine cannot know about. It narrows the candidate set to that account and
        # nothing else; it does NOT relax a single rule. The account still has to be active, still
        # has to support the mode, still has to hold the money and still has to have the daily
        # headroom, and if it does not, the request is refused rather than forced. That is what
        # makes an account reference arriving from a browser safe to accept.
        accounts = [a for a in accounts if a.reference_number == force_account_ref]
    parsed = parse_note(note, accounts)
    same_ref = await last_payout_account(db, member_id, exclude_tx_id=exclude_tx_id) \
        if parsed.same_account else None

    result = AllocationResult(mode=mode, note=parsed, beneficiary=beneficiary)
    result.detail = {
        "transactionMode": mode,
        "requestedAmount": amount,
        "noteSameAccount": parsed.same_account,
        "noteBank": parsed.bank_name,
        "noteAccountRef": parsed.account_ref,
        "noteMatchedText": parsed.matched_text,
        "sameAccountCandidate": same_ref,
    }
    if beneficiary:
        result.detail["beneficiary"] = {
            "mode": beneficiary.mode,
            "accountNumber": beneficiary.account_number,
            "ifsc": beneficiary.ifsc,
            "name": beneficiary.name,
            "saved": beneficiary.saved,
            "valid": beneficiary.valid,
        }

    # Rule 8 / negative scenario F — an incomplete beneficiary is an exception in its own right.
    # No account is evaluated: the problem is the request, not the accounts, and reporting "no
    # eligible account" would send an Admin to fix the wrong thing.
    if beneficiary and not beneficiary.valid:
        result.failure_code = FAIL_BENEFICIARY
        result.reason = beneficiary.invalid_reason or "The withdrawal's beneficiary details are invalid."
        result.detail["failure"] = FAIL_BENEFICIARY
        return result

    if not accounts:
        result.failure_code, result.reason = _failure([], amount, mode)
        result.detail["failure"] = result.failure_code
        return result

    # ── Locking: ONE acquisition, in ONE deterministic order, before anything is planned ──
    #
    # Every candidate account is locked up front, ascending by reference. Two concurrent
    # allocations therefore request the same rows in the same sequence and one simply waits for
    # the other — a lock-order inversion is not possible, so this cannot deadlock.
    #
    # It is deliberately a single phase. Locking the chosen accounts first and widening to the
    # rest only on a retry looks cheaper, but it acquires locks in an order that depends on which
    # accounts the plan happened to pick: one request holding C and reaching for A while another
    # holds A and reaches for C is a deadlock, which Postgres resolves by killing one of them.
    # Paying the full cost once, in a fixed order, removes that class of failure outright.
    #
    # The cost is that an allocation holds the account rows for the rest of its transaction, so
    # concurrent withdrawals serialise through this section. That is the correct trade here: the
    # section is a few indexed reads, the account count is small, and withdrawal creation is not a
    # hot path — whereas the failure it prevents is a 500 on a request that was about to move
    # money. `force_account_ref` narrows the set to one account, so an operator-directed payout
    # locks exactly that row.
    locked_all = await _lock_accounts(db, [a.reference_number for a in accounts])
    accounts = [locked_all.get(a.reference_number, a) for a in accounts]

    for attempt in range(_MAX_CLAIM_ATTEMPTS):
        plan = await _plan(
            db, accounts, amount, mode=mode, beneficiary=beneficiary, note=parsed,
            same_account_ref=same_ref, on=on, result=result,
            operator_directed=bool(force_account_ref))
        if not plan:
            return result           # _plan filled in the failure code and reason

        legs, rule = plan
        # Re-verified under the locks already held. With every row locked the figures cannot move
        # underneath the plan, so this now confirms rather than races — and it is kept because it
        # is the check that decides, and a plan that fails it must never be paid.
        if await _verify_under_lock(db, legs, mode=mode, on=on):
            return _finish(result, legs, rule, amount)
        result.detail.setdefault("reclaimed", []).append(
            {"attempt": attempt + 1, "accounts": [l.ref for l in legs]})

    # Even with every account locked the plan did not hold — the capacity really is gone.
    result.failure_code = FAIL_RACE
    result.reason = (f"No account could be reserved for ₹{amount:,.2f} — the remaining capacity was "
                     f"taken by concurrent withdrawals. Retry the allocation.")
    result.detail["failure"] = FAIL_RACE
    return result


async def _contributors(
    db: AsyncSession, accounts: Sequence[AccountMaster], amount: float, *, mode: str,
    beneficiary: Optional[Beneficiary], on: Optional[date], result: AllocationResult,
    operator_directed: bool,
) -> list[Candidate]:
    """Every account that can pay SOME of ``amount`` — the pool a split is built from.

    Split out of :func:`_plan` because it is now needed at two points that must agree exactly: a
    bank preference is decided against this pool before the single-account rule runs, and the
    split itself is built from it afterwards. Evaluating twice, or evaluating the second time from
    a differently-filtered set, would let the two disagree about what the requested bank can pay.
    """
    partial = await evaluate_accounts(
        db, amount, mode=mode, beneficiary=beneficiary, on=on, require_full=False,
        accounts=accounts, operator_directed=operator_directed)
    eligible = [c for c in partial if c.eligible]
    result.detail["splitCandidates"] = [c.snapshot() for c in eligible]
    return eligible


async def _plan(
    db: AsyncSession, accounts: Sequence[AccountMaster], amount: float, *, mode: str,
    beneficiary: Optional[Beneficiary], note: NoteRequest, same_account_ref: Optional[str],
    on: Optional[date], result: AllocationResult, operator_directed: bool = False,
) -> Optional[tuple[list[Leg], str]]:
    """Decide who pays, without taking any lock. Returns (legs, rule) or None with the failure
    recorded on ``result``.

    Order of the rules is the specified one: filter, then prefer ONE account, then — only if no
    single account can carry the whole amount — assemble the smallest legal split.

    A merchant note naming a bank REORDERS that. The requested bank has priority whenever it can
    satisfy the withdrawal, and the ways it can satisfy it are themselves ranked:

      1. one of its accounts covers the whole amount   -> that account alone
      2. several of them together cover it             -> that bank alone, fewest accounts
      3. it cannot cover it even collectively          -> all of its capacity FIRST, then other
                                                          banks for the remainder only
      4. it has no eligible capacity at all            -> preference void, ordinary rules run

    So "prefer ONE account" decides between equals, and a named bank is not an equal: another
    bank's single account must never replace an allocation the requested bank could have made
    (cases 1-2), and must never crowd out capacity the requested bank does have (case 3). Only
    case 4 is an unmet preference in the older, all-or-nothing sense. With no note preference in
    play nothing below changes — the single-account rule is untouched.
    """
    # Which accounts could carry the whole withdrawal on their own? Rule 12 decides among these,
    # further down — this only measures them.
    full = await evaluate_accounts(
        db, amount, mode=mode, beneficiary=beneficiary, on=on, require_full=True, accounts=accounts,
        operator_directed=operator_directed)
    result.candidates = full
    result.detail["accounts"] = [c.snapshot() for c in full]
    eligible_full = [c for c in full if c.eligible]

    # Rules 2 / 8 — the merchant's note narrows the pool to the requested bank or account, and
    # EVERY account in it is evaluated (four Bank of Baroda accounts means four evaluations, not
    # the first one). If none of them is eligible the note is reported unfulfilled and the normal
    # rules run — a preference is never permission to skip a rule.
    def _requested(pool: list[Candidate]) -> list[Candidate]:
        if note.account_ref:
            return [c for c in pool if c.ref == note.account_ref]
        if note.bank_name:
            return [c for c in pool if c.account.bank_name == note.bank_name]
        return []

    has_preference = bool(note.account_ref or note.bank_name)
    preferred_full = _requested(eligible_full) if has_preference else []
    pool_full = preferred_full or eligible_full

    # Whether the requested bank can satisfy this withdrawal AT ALL decides everything below, and
    # answering it needs the split evaluation. So when a preference is in play that no single
    # preferred account can meet, that evaluation happens HERE rather than only after the
    # single-account attempt has already handed the whole amount to another bank.
    contributors: Optional[list[Candidate]] = None
    preferred_partial: list[Candidate] = []
    preferred_covers = False
    if has_preference and not preferred_full:
        contributors = await _contributors(
            db, accounts, amount, mode=mode, beneficiary=beneficiary, on=on, result=result,
            operator_directed=operator_directed)
        preferred_partial = _requested(contributors)
        preferred_covers = bool(preferred_partial) and (
            _money(sum(c.usable for c in preferred_partial)) >= _money(amount))
        if not preferred_partial:
            # Case 4 — nothing at the requested bank can pay any part of this. The preference is
            # void, the miss is recorded, and the ordinary rules run from here unchanged.
            result.requested_unavailable = True
            result.detail["requestedBankUnavailable"] = note.bank_name or note.account_ref

    # Rule 14 — a "same account" request that cannot be honoured is recorded BEFORE selection runs,
    # with why. Journalling it only on the failure path would leave the commonest case unexplained:
    # the previous account is unavailable, the fallback quietly succeeds, and nothing anywhere says
    # the merchant's request was not met.
    if note.same_account:
        usable_same = bool(same_account_ref) and any(c.ref == same_account_ref for c in pool_full)
        if not usable_same:
            blocked = (next((c for c in full if c.ref == same_account_ref), None)
                       if same_account_ref else None)
            result.requested_unavailable = True
            result.detail["sameAccountRejected"] = {
                "accountRef": same_account_ref,
                "reason": ("NO_PREVIOUS_ACCOUNT" if not same_account_ref
                           else (blocked.reject_reason if blocked else "ACCOUNT_NO_LONGER_EXISTS")
                           or REJECT_LOCKED),
            }

    # ── Rule 12 — can ONE account carry the whole withdrawal? ──
    # Skipped only in cases 2 and 3: the requested bank has capacity to contribute, so the decision
    # belongs to the split below, where that capacity is placed ahead of every other bank's. Taking
    # a single non-preferred account here is exactly what "other banks must not replace a
    # sufficient preferred-bank allocation" forbids.
    if pool_full and not (has_preference and preferred_partial):
        for rule, tier in _preference_pools(pool_full, note, same_account_ref=same_account_ref):
            ordered = rank(tier)
            if ordered:
                return [Leg(candidate=ordered[0], amount=amount)], rule

    # ── Rule 16 — no single account can carry it. Can a combination? ──
    if contributors is None:
        contributors = await _contributors(
            db, accounts, amount, mode=mode, beneficiary=beneficiary, on=on, result=result,
            operator_directed=operator_directed)
        if has_preference:
            preferred_partial = _requested(contributors)
            preferred_covers = bool(preferred_partial) and (
                _money(sum(c.usable for c in preferred_partial)) >= _money(amount))

    pool_partial = contributors
    required_refs: Optional[set[str]] = None
    if preferred_covers:
        # Case 2 — the requested bank covers the whole amount across its own accounts, so no other
        # bank is admitted at all. Fewest accounts still applies, but only within that bank.
        pool_partial = preferred_partial
    elif preferred_partial:
        # Case 3 — it cannot cover the amount. Every rupee it CAN pay is placed first and other
        # banks supply only what is left, so a preference the platform can partly honour is partly
        # honoured rather than discarded. The merchant asked for a bank and did not get all of it,
        # which is what ``requested_unavailable`` has always meant on this path.
        usable_pref = _money(sum(c.usable for c in preferred_partial))
        required_refs = {c.ref for c in preferred_partial}
        result.requested_unavailable = True
        result.detail["requestedBankUnavailable"] = note.bank_name or note.account_ref
        result.detail["requestedBankPartial"] = {
            "bank": note.bank_name or note.account_ref,
            "usableCapacity": usable_pref,
            "shortfall": _money(_money(amount) - usable_pref),
        }

    # The soft hints — accounts that already know the beneficiary, and the member's last paying
    # account. These shape the split only where they cost no extra account (see :func:`_split`).
    # The requested bank travels separately, as ``required_refs``, because it is not a hint.
    preferred_refs = {c.ref for c in contributors if c.beneficiary_known}
    if same_account_ref:
        preferred_refs.add(same_account_ref)

    legs = _split(pool_partial, amount, preferred_refs, required=required_refs)
    if legs:
        return legs, RULES.SPLIT

    # ── Rule 18 — nothing, and nothing combined either. An exception, never a part-payment. ──
    combined = _money(sum(c.usable for c in contributors))
    result.failure_code, result.reason = _failure(
        full, amount, mode, combined=combined, contributors=len(contributors))
    result.detail["failure"] = result.failure_code
    result.detail["totalUsableCapacity"] = combined
    result.detail["shortfall"] = _money(_money(amount) - combined)
    wanted = note.bank_name or note.account_ref
    if wanted:
        result.reason = f"{result.reason} (requested: {wanted} — unavailable)"
        result.requested_unavailable = True
    return None


def _finish(result: AllocationResult, legs: list[Leg], rule: str, amount: float) -> AllocationResult:
    """Attach a verified plan to the result, asserting the one invariant that must never bend."""
    total = _money(sum(l.amount for l in legs))
    if total != _money(amount):
        # Unreachable by construction (_split trims the final leg to the exact remainder), and
        # checked anyway: a payout allocation that does not equal the withdrawal is worse than no
        # allocation, so it fails loudly here rather than paying the wrong amount.
        raise AssertionError(
            f"payout allocation {total} does not equal the requested amount {amount}")
    result.legs = legs
    result.rule = rule
    # The sentence the Admin's payout screen prints under the allocation table, so it has to
    # describe what actually happened to the merchant's request. A bank that paid what it could
    # and was topped up by others is NOT the same event as a bank that could not be used at all,
    # and reporting both as "unavailable — fallback applied" told an Admin the named bank was
    # skipped while its reference sat in the first row of the table.
    partial = result.detail.get("requestedBankPartial")
    extra = ""
    if partial:
        extra = (f"requested bank {partial['bank']} covered ₹{partial['usableCapacity']:,.2f} of "
                 f"₹{_money(amount):,.2f} — other banks completed the rest")
    elif result.requested_unavailable and result.note.has_preference:
        extra = "requested account unavailable — fallback applied"
    elif result.note.bank_name:
        extra = f"requested bank {result.note.bank_name}"
    result.reason = _RULE_TEXT.get(rule, rule) + (f" ({extra})" if extra else "")
    result.detail["selected"] = [
        {"accountRef": l.ref, "amount": _money(l.amount), **l.candidate.snapshot()} for l in legs]
    result.detail["allocatedTotal"] = total
    return result


# ═══ 10. Persisting the decision ════════════════════════════════════════════════════════════════

async def engine_has_seen(db: AsyncSession, transaction_ref: str) -> bool:
    """Whether the allocation engine has ever run for this withdrawal.

    This is the line between a withdrawal RAISED UNDER automatic allocation and one that genuinely
    predates it, and it has to be exact: it decides whether completing without any payout
    accounting is a legacy allowance or a bug about to lose the record of real money.

    :func:`record_allocation` writes its journal row on BOTH branches — a placement and a failure
    to place — before either is acted on. So any withdrawal the engine touched has a row here even
    when it ended in NO_ELIGIBLE_ACCOUNT with no legs to show for it, which is precisely the case
    that must not be quietly completed. A withdrawal with no journal row and no leg was never seen
    by the engine and can only be one raised before the feature existed.

    Legs are checked too, so a row whose journal was written by an older code path is still
    recognised. Either is proof; neither is required to be present alone.
    """
    seen = (await db.execute(
        select(WithdrawalAllocation.id).where(
            WithdrawalAllocation.transaction_ref == transaction_ref).limit(1)
    )).scalar_one_or_none()
    if seen is not None:
        return True
    leg = (await db.execute(
        select(WithdrawalPayoutLeg.id).where(
            WithdrawalPayoutLeg.transaction_ref == transaction_ref).limit(1)
    )).scalar_one_or_none()
    return leg is not None


async def release_legs(
    db: AsyncSession, transaction_ref: str, *, reason: str,
) -> int:
    """Release every live leg of a withdrawal, returning the capacity they held.

    History is kept: the leg is marked RELEASED with its reason, never deleted, so the record of
    where a withdrawal was going to be paid from survives its rejection or re-allocation.
    """
    legs = (await db.execute(
        select(WithdrawalPayoutLeg).where(
            WithdrawalPayoutLeg.transaction_ref == transaction_ref,
            WithdrawalPayoutLeg.status == ledger.LEG_ALLOCATED,
        )
    )).scalars().all()
    for leg in legs:
        leg.status = ledger.LEG_RELEASED
        leg.released_reason = reason[:64]
    if legs:
        await db.flush()
    return len(legs)


async def live_legs(db: AsyncSession, transaction_ref: str) -> list[WithdrawalPayoutLeg]:
    """The legs currently standing for this withdrawal — allocated, or already paid."""
    return list((await db.execute(
        select(WithdrawalPayoutLeg)
        .where(
            WithdrawalPayoutLeg.transaction_ref == transaction_ref,
            WithdrawalPayoutLeg.status.in_((ledger.LEG_ALLOCATED, ledger.LEG_PAID)),
        )
        .order_by(WithdrawalPayoutLeg.leg_no)
    )).scalars().all())


async def write_legs(
    db: AsyncSession, result: AllocationResult, *, transaction: Transaction,
    allocated_by: Optional[str] = None, on: Optional[date] = None,
) -> list[WithdrawalPayoutLeg]:
    """Write one ALLOCATED leg per account in the plan. From here the capacity is held.

    Any legs already standing for this withdrawal are released first, so a re-allocation replaces
    the previous decision rather than stacking on top of it — the database's own uniqueness on
    (withdrawal, account, status) refuses the alternative.
    """
    if not result.legs:
        return []
    await release_legs(db, transaction.ref, reason=RELEASE_REALLOCATED)
    day = on or ist_today()
    stamp = ist_stamp()
    rows: list[WithdrawalPayoutLeg] = []
    for i, leg in enumerate(result.legs, start=1):
        acc = leg.candidate.account
        row = WithdrawalPayoutLeg(
            transaction_ref=transaction.ref,
            transaction_id=transaction.id,
            merchant_id=transaction.merchant_id,
            merchant_business=transaction.merchant_name,
            member_id=transaction.member_id,
            leg_no=i,
            account_ref=acc.reference_number,
            account_id=acc.id,
            account_name=acc.account_name,
            bank_name=acc.bank_name,
            account_number=acc.account_number,
            ifsc=acc.ifsc_code,
            branch=acc.branch,
            account_type=_type_value(acc.account_type),
            transaction_mode=result.mode,
            amount=_money(leg.amount),
            highest_debit=_money(acc.highest_debit),
            debit_used_today=_money(leg.candidate.used_today),
            remaining_capacity=_money(leg.candidate.remaining),
            available_balance=_money(leg.candidate.balance),
            status=ledger.LEG_ALLOCATED,
            allocated_by=allocated_by,
            leg_date=day,
            created_at=datetime.utcnow(),
            created_at_ist=stamp,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def record_allocation(
    db: AsyncSession, result: AllocationResult, *, transaction: Transaction,
    triggered_by: Optional[str] = None,
) -> WithdrawalAllocation:
    """Write the append-only journal row for one allocation attempt — successful or not.

    Every figure the decision rested on is stored, because none of them can be recovered later:
    today's usage moves with the next withdrawal, balances move with every payment, and an Admin
    may re-configure the limit tomorrow.
    """
    primary = result.primary
    acc = primary.account if primary else None
    ben = result.beneficiary
    row = WithdrawalAllocation(
        transaction_ref=transaction.ref,
        transaction_id=transaction.id,
        merchant_id=transaction.merchant_id,
        merchant_name=transaction.merchant_name,
        member_id=transaction.member_id,
        member_name=transaction.member_name,
        requested_amount=_money(transaction.amount),
        transaction_mode=result.mode,
        merchant_note=transaction.notes,
        beneficiary_account=(ben.account_number if ben else None),
        beneficiary_ifsc=(ben.ifsc if ben else None),
        beneficiary_name=(ben.name if ben else None),
        outcome=result.outcome,
        leg_count=len(result.legs) or None,
        allocated_amount=result.total if result.legs else None,
        account_ref=acc.reference_number if (acc and not result.split) else None,
        account_name=acc.account_name if (acc and not result.split) else None,
        bank_name=acc.bank_name if (acc and not result.split) else None,
        account_type=_type_value(acc.account_type) if (acc and not result.split) else None,
        highest_debit=_money(acc.highest_debit) if acc else None,
        debit_used_today=_money(primary.used_today) if primary else None,
        remaining_capacity=_money(primary.remaining) if primary else None,
        available_balance=_money(primary.balance) if primary else None,
        rule=result.rule,
        reason=result.reason,
        detail=json.dumps(result.detail, default=str),
        failure_code=result.failure_code,
        candidates_considered=len(result.candidates),
        candidates_eligible=sum(1 for c in result.candidates if c.eligible),
        triggered_by=triggered_by,
        created_at=datetime.utcnow(),
        created_at_ist=ist_stamp(),
    )
    db.add(row)
    await db.flush()
    return row


# ═══ 11. Serialisation ══════════════════════════════════════════════════════════════════════════

def serialize_leg(leg: WithdrawalPayoutLeg, *, mask: bool = True,
                  capacity: bool = False) -> dict:
    """API shape for one payout leg (camelCase, matching the rest of the API).

    The account number is masked by default. The merchant is entitled to know WHICH account is
    paying them — that is the point of showing the allocation — but not to its full number, which
    the platform has never exposed on a payout. An Admin view passes ``mask=False``.

    ``capacity`` adds the account's daily debit position AS IT WAS at allocation — the limit, what
    had been used, what remained, the balance. It is OFF by default and must stay that way: those
    are internal operating figures, and a merchant learning how much headroom the platform's
    accounts have is a disclosure, not a feature. Only the admin-only allocation endpoint turns it
    on, which is the same boundary ``_t()`` already keeps.
    """
    number = leg.account_number or ""
    extra = {}
    if capacity:
        extra = {
            "highestDebit": _money(leg.highest_debit or 0.0),
            "debitUsedToday": _money(leg.debit_used_today or 0.0),
            # What the account had left AFTER this leg was allocated — the figure an Admin needs
            # to see how much room the payout consumed.
            "remainingCapacity": _money(_money(leg.remaining_capacity or 0.0)
                                        - _money(leg.amount or 0.0)),
            "remainingBefore": _money(leg.remaining_capacity or 0.0),
            "availableBalance": _money(leg.available_balance or 0.0),
        }
    return {**extra,
        "legNo": leg.leg_no,
        "accountRef": leg.account_ref,
        "accountName": leg.account_name,
        "bankName": leg.bank_name,
        "accountNumber": (("•••• " + number[-4:]) if (mask and len(number) >= 4) else number) or None,
        "ifsc": leg.ifsc,
        "branch": leg.branch,
        "accountType": leg.account_type,
        "transactionMode": leg.transaction_mode,
        "amount": _money(leg.amount),
        "status": leg.status,
        "ledgerEntryRef": leg.ledger_entry_ref,
        "allocatedAt": (leg.created_at.isoformat() + "Z") if leg.created_at else None,
        "allocatedAtIst": leg.created_at_ist,
        "paidAt": (leg.paid_at.isoformat() + "Z") if leg.paid_at else None,
    }


def serialize(row: WithdrawalAllocation) -> dict:
    """API shape for one journal row (camelCase, matching the rest of the API)."""
    return {
        "id": row.id,
        "transactionRef": row.transaction_ref,
        "merchant": row.merchant_name,
        "memberId": row.member_id,
        "memberName": row.member_name,
        "requestedAmount": _money(row.requested_amount),
        "transactionMode": row.transaction_mode,
        "merchantNote": row.merchant_note,
        "beneficiaryAccount": row.beneficiary_account,
        "beneficiaryIfsc": row.beneficiary_ifsc,
        "beneficiaryName": row.beneficiary_name,
        "outcome": row.outcome,
        "legCount": row.leg_count,
        "allocatedAmount": row.allocated_amount,
        "accountRef": row.account_ref,
        "accountName": row.account_name,
        "bankName": row.bank_name,
        "accountType": row.account_type,
        "highestDebit": row.highest_debit,
        "debitUsedToday": row.debit_used_today,
        "remainingCapacity": row.remaining_capacity,
        "availableBalance": row.available_balance,
        "rule": row.rule,
        "reason": row.reason,
        "failureCode": row.failure_code,
        "candidatesConsidered": row.candidates_considered,
        "candidatesEligible": row.candidates_eligible,
        "detail": json.loads(row.detail) if row.detail else None,
        "triggeredBy": row.triggered_by,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
        "createdAtIst": row.created_at_ist,
    }


# ═══ 11. Daily debit limit readiness ════════════════════════════════════════════════════════════
#
# `highest_debit` is the one value that decides whether the engine can place a withdrawal at all.
# An account at 0 is skipped (:func:`_evaluate`), so an environment whose accounts are all at 0
# allocates nothing and EVERY withdrawal falls to the Admin exception queue — the manual step this
# feature exists to remove, reappearing silently.
#
# Silently is the part this section addresses. It cannot be fixed by guessing a limit: the daily
# ceiling is a business policy and inferring one from past transactions produces a number nobody
# chose (see the migration note). What it can do is make the gap impossible to miss — name every
# account that needs a decision, say why, and let the platform report before a single withdrawal
# is raised whether it is able to pay one.

# Why an account cannot be relied on to pay.
READY_OK = "CONFIGURED"            # an Admin explicitly set this account's daily limit
READY_MISSING = "NOT_CONFIGURED"   # no limit at all — the engine will never choose this account
READY_UNCONFIRMED = "UNCONFIRMED"  # a limit is present but no Admin ever confirmed it
READY_SUSPICIOUS = "SUSPICIOUS"    # the limit looks inherited from the old high-water mark

# Ordered worst-first, so a report reads top-down in the order an Admin should act.
_READY_ORDER = (READY_MISSING, READY_SUSPICIOUS, READY_UNCONFIRMED, READY_OK)

_READY_TEXT = {
    READY_MISSING: ("No daily Highest Debit is configured, so this account is never chosen "
                    "automatically and can only be paid from when an Admin names it explicitly."),
    READY_SUSPICIOUS: ("The daily limit equals the largest single debit this account has ever "
                       "made, which is what the value meant before it became a daily limit. It is "
                       "almost certainly an inherited figure rather than a chosen daily policy."),
    READY_UNCONFIRMED: ("A daily limit is set but no Admin has confirmed it since Highest Debit "
                        "became a hard daily ceiling."),
    READY_OK: "An Admin has explicitly set this account's daily Highest Debit.",
}


def classify_debit_limit(account: AccountMaster) -> str:
    """How much trust this account's daily Highest Debit deserves.

    The distinction that matters is not "is there a number" but "did a person choose it". A value
    inherited from the auto-raising era is a record of one past payout, not a policy, and it is
    reported as such rather than being quietly relied upon.
    """
    limit = _money(account.highest_debit)
    if limit <= 0:
        return READY_MISSING
    if account.highest_debit_configured_at is not None:
        return READY_OK
    observed = _money(getattr(account, "observed_max_debit", 0.0) or 0.0)
    # Equal to the largest debit ever seen — the signature of the old high-water mark. Or BELOW it,
    # which is worse: a daily ceiling under a single payout the account has already made means it
    # would now refuse a withdrawal it has demonstrably handled.
    if observed > 0 and limit <= observed:
        return READY_SUSPICIOUS
    return READY_UNCONFIRMED


async def debit_limit_readiness(db: AsyncSession) -> dict:
    """Audit every payout account's daily Highest Debit and report what still needs a decision.

    Read-only: it changes nothing, guesses nothing and is safe to call at any time. ``canAllocate``
    is the question an operator actually needs answered before deploying — is there at least one
    ACTIVE account this engine is able to choose?
    """
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    rows = []
    for a in accounts:
        state = classify_debit_limit(a)
        active = (a.status or "").upper() == "ACTIVE"
        rows.append({
            "accountRef": a.reference_number,
            "accountName": a.account_name,
            "bankName": a.bank_name,
            "status": a.status,
            "active": active,
            "highestDebit": _money(a.highest_debit),
            "observedMaxDebit": _money(getattr(a, "observed_max_debit", 0.0) or 0.0),
            "configuredAt": (a.highest_debit_configured_at.isoformat() + "Z")
                            if a.highest_debit_configured_at else None,
            "configuredBy": a.highest_debit_configured_by,
            "state": state,
            "message": _READY_TEXT[state],
            # Only an ACTIVE account can pay, so only an ACTIVE one needs a limit before go-live.
            "needsConfiguration": active and state != READY_OK,
        })

    rows.sort(key=lambda r: (not r["needsConfiguration"], _READY_ORDER.index(r["state"]),
                             r["accountRef"]))
    active_rows = [r for r in rows if r["active"]]
    allocatable = [r for r in active_rows if r["highestDebit"] > 0]
    needing = [r for r in rows if r["needsConfiguration"]]
    counts = {state: sum(1 for r in rows if r["state"] == state) for state in _READY_ORDER}

    return {
        "accounts": rows,
        "total": len(rows),
        "activeTotal": len(active_rows),
        # The go-live question: can the engine place a withdrawal on ANY account right now?
        "canAllocate": bool(allocatable),
        "allocatableAccounts": len(allocatable),
        "needsConfiguration": len(needing),
        "needsConfigurationRefs": [r["accountRef"] for r in needing],
        "counts": counts,
        "summary": (
            f"{len(allocatable)} of {len(active_rows)} active accounts can be allocated a "
            f"withdrawal; {len(needing)} still need a daily Highest Debit decision."
            if active_rows else "No active payout accounts are configured."),
    }
