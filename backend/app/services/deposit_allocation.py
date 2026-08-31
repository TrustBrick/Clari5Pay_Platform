"""Automatic deposit account allocation.

THE single authority for "which managed bank account should receive this deposit?". The merchant
never picks one and the Admin never picks one per request — the Admin configures accounts and
their limits in Account Management, and this service decides. The frontend renders the result; it
never re-implements any of the rules below.

WHAT MAKES A LIMIT A LIMIT
──────────────────────────
``AccountMaster.highest_credit`` is a HARD DAILY CREDIT CEILING. The test is never
``amount <= highest_credit``; it is always

    credit_used_today(account) + amount  <=  highest_credit

so an account with a ₹1,00,000 ceiling that has already taken ₹70,000 today can accept ₹30,000
and cannot accept ₹30,001. Equality passes — reaching the ceiling exactly is allowed; exceeding it
by a single paisa is not.

WHAT "USED TODAY" COUNTS — AND WHY IT ISN'T ONLY COMPLETED DEPOSITS
──────────────────────────────────────────────────────────────────
A deposit consumes capacity from the moment an account is allocated to it, not from the moment the
money lands. If only completed deposits counted, two requests arriving seconds apart would both
read "₹0 used", both be allocated the same account, and together breach the ceiling — the exact
failure this service exists to prevent. So today's usage is every deposit routed to the account
today that is still LIVE, plus every completed one:

    type LIKE 'DEPOSIT%'  AND  admin_ref = <account>  AND  tx_date = <today, IST>
    AND  status NOT IN (REJECTED, SA_REJECTED, CANCELLED)

Rejecting or cancelling a request therefore RELEASES its capacity, automatically, with no separate
reservation record to keep in step. This is the platform's existing state machine doing the work:
"available", "temporarily assigned", "limit reached" and "unavailable" are all read off states and
data that already exist. No second balance store, no reservation table, no new status.

The day boundary is IST (``tx_date`` is already written IST-dated by the deposit flow), so usage
resets naturally at midnight IST. Nothing is reset, recalculated or written at the boundary —
tomorrow's query simply selects a different ``tx_date``. Historical rows are never touched.

CONCURRENCY
───────────
Selection walks the ranked candidates taking ``SELECT … FOR UPDATE SKIP LOCKED`` on the
``account_master`` row and RE-VERIFIES capacity while holding it. SKIP LOCKED is what makes this
deadlock-free: an account another request is mid-allocation on is passed over rather than waited
on, so two requests can never sit holding each other's next choice. If every candidate was
skipped, one final blocking pass takes a single lock, which cannot form a cycle because the caller
holds nothing at that point. The lock is held until the caller's transaction commits, and the
allocated deposit row is written inside it, so the next request's usage query sees it.

Order of the hard filters is deliberate: cheap disqualifications (status, capability,
configuration) run before the capacity arithmetic, and the capacity arithmetic runs before any
locking.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    AccountMaster, AccountTransaction, AccountType, AdminUpi, DepositAllocation, Transaction,
    TxStatus, TxType,
)

IST = timezone(timedelta(hours=5, minutes=30))

# Deposit rows in these states have been abandoned, so they release the capacity they held. Every
# other state — from ACCOUNT_SUBMITTED through DEPOSITED — still consumes it.
RELEASED_STATUSES = (TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED)

DEPOSIT_TYPES = (TxType.DEPOSIT, TxType.DEPOSIT_REQUEST)

# Deposit types paid into a managed bank account, which therefore need one allocated. CASH and
# CRYPTO carry their own member-supplied proof and skip the account hop entirely; CARD is paid
# through a payment-gateway link the Admin submits, not an account. All three are untouched here.
ALLOCATABLE_DEPOSIT_TYPES = ("UPI", "BANK", "IMPS", "NEFT", "RTGS")

# A UPI deposit is PREFERABLY paid to a linked UPI ID (the platform's existing per-account
# payment-method capability, AdminUpi.account_ref) — but that is a preference, never a
# requirement. Every managed account can receive money by bank transfer, so an account without a
# linked UPI is still a perfectly good destination for a UPI-typed request; the merchant is simply
# sent its bank details instead of a VPA.
#
# This was originally written as a HARD filter and it was wrong: with no admin_upis rows
# configured, every UPI deposit — the deposit form's DEFAULT type — was disqualified on every
# account and fell back to the Admin queue, which is precisely the manual assignment this engine
# exists to remove. Capability may narrow a choice between eligible accounts; it must never be the
# reason there is no eligible account.
UPI_DEPOSIT_TYPE = "UPI"


# ── Allocation rules (machine-readable; each maps to one human sentence) ────────────────────────
class RULES:
    SAME_ACCOUNT = "SAME_ACCOUNT_REQUEST"
    NOTE_BANK = "MERCHANT_NOTE_BANK_PREFERENCE"
    NEW_UNUSED = "NEW_CUSTOMER_UNUSED_ACCOUNT"
    NEW_SAVINGS_FALLBACK = "NEW_CUSTOMER_SAVINGS_FALLBACK"
    OLD_ACCOUNT_HISTORY = "EXISTING_CUSTOMER_ACCOUNT_HISTORY"
    NEAREST_CAPACITY = "NEAREST_SUITABLE_CAPACITY"
    UPI_CAPABLE = "UPI_DEPOSIT_UPI_ENABLED_ACCOUNT"


_RULE_TEXT = {
    RULES.SAME_ACCOUNT: "Same account requested by merchant note + eligible",
    RULES.NOTE_BANK: "Bank requested by merchant note + eligible + nearest remaining credit capacity",
    RULES.NEW_UNUSED: "New customer + unused account + nearest suitable credit capacity",
    RULES.NEW_SAVINGS_FALLBACK: (
        "New customer + no unused account + eligible Savings account + nearest suitable credit capacity"),
    RULES.OLD_ACCOUNT_HISTORY: "Existing customer + previously used account + nearest suitable credit capacity",
    RULES.NEAREST_CAPACITY: "Eligible + nearest remaining credit capacity",
    RULES.UPI_CAPABLE: "UPI deposit + account with a linked UPI + nearest suitable credit capacity",
}

# Why a candidate was rejected — recorded per account on the allocation journal, so a support
# question ("why not Bank of Baroda?") has a stored answer rather than a re-run against data that
# has since moved on.
REJECT_INACTIVE = "ACCOUNT_NOT_AVAILABLE"
REJECT_NO_LIMIT = "NO_CREDIT_LIMIT_CONFIGURED"
REJECT_NO_CAPACITY = "DAILY_CREDIT_LIMIT_REACHED"
REJECT_LOCKED = "CONCURRENTLY_ALLOCATED"

# The distinct ways an allocation can find nothing. Recorded on the journal and shown to the Admin,
# because each one has a different fix: activate an account, raise a limit, add an account, or wait
# for tomorrow. A single "no eligible account" sentence tells them none of that.
FAIL_NO_ACCOUNTS = "NO_ACCOUNTS_CONFIGURED"
FAIL_ALL_UNAVAILABLE = "ALL_ACCOUNTS_UNAVAILABLE"
FAIL_NO_LIMITS = "NO_CREDIT_LIMITS_CONFIGURED"
FAIL_LIMIT_REACHED = "ALL_ACCOUNTS_AT_DAILY_LIMIT"
FAIL_AMOUNT_TOO_LARGE = "AMOUNT_EXCEEDS_EVERY_REMAINING_CAPACITY"
FAIL_MIXED = "NO_ELIGIBLE_ACCOUNT"
FAIL_RACE = "CAPACITY_TAKEN_CONCURRENTLY"


def _failure(candidates: list["Candidate"], amount: float) -> tuple[str, str]:
    """(code, human sentence) for why nothing could be allocated.

    Distinguishes the cases an Admin resolves differently: an account that is switched off is
    activated, an unconfigured limit is set, a limit that is merely used up frees itself tomorrow,
    and an amount larger than every ceiling needs a bigger account. Derived from the per-account
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
    if all(r == REJECT_NO_LIMIT for r in reasons):
        return FAIL_NO_LIMITS, (
            f"No account has a Highest Credit configured, so none can accept a deposit. "
            f"Set a daily credit limit in Account Management.")

    # Only accounts that were otherwise usable say anything about capacity.
    capacity_blocked = [c for c in candidates if c.reject_reason == REJECT_NO_CAPACITY]
    if capacity_blocked:
        # Distinguish "used up today" from "this deposit is bigger than the account has ever held":
        # the first clears at midnight IST, the second needs a larger limit.
        biggest_ceiling = max(_money(c.account.highest_credit) for c in capacity_blocked)
        if _money(amount) > biggest_ceiling:
            return FAIL_AMOUNT_TOO_LARGE, (
                f"{money} is larger than every account's Highest Credit "
                f"(the largest is ₹{biggest_ceiling:,.2f}). Raise a limit or add an account.")
        left = max(c.remaining for c in capacity_blocked)
        return FAIL_LIMIT_REACHED, (
            f"Every eligible account has reached its daily credit limit for {money} — the most any "
            f"has left today is ₹{left:,.2f}. Capacity resets at midnight IST, or raise a limit.")

    return FAIL_MIXED, (
        f"No account can accept {money} — all {n} are unavailable or out of daily credit capacity.")


# Outcomes recorded on the allocation journal.
OUTCOME_ALLOCATED = "ALLOCATED"
OUTCOME_NO_ACCOUNT = "NO_ACCOUNT"


def ist_today(now: Optional[datetime] = None) -> date:
    """The current business date in IST — the boundary the daily credit limit resets on."""
    return (now or datetime.now(IST)).astimezone(IST).date()


def ist_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime("%d %b %Y, %I:%M %p") + " IST"


def _money(value: Optional[float]) -> float:
    """Round to paise. Every limit comparison goes through this, so ₹0.01 over is really over and
    float drift can never quietly widen a ceiling."""
    return round(float(value or 0.0), 2)


def _norm_member(member_id: Optional[str]) -> str:
    return (member_id or "").strip().upper()


def _type_value(account_type) -> str:
    return account_type.value if hasattr(account_type, "value") else str(account_type or "")


# ═══ 1. Daily credit usage ══════════════════════════════════════════════════════════════════════

async def credit_used_today(
    db: AsyncSession, refs: Optional[Sequence[str]] = None, *, on: Optional[date] = None,
) -> dict[str, float]:
    """{account reference → rupees credited/committed to it today}. Accounts with no activity are
    absent from the mapping (callers read them as 0.0).

    Computed straight from the deposit rows: there is no stored counter to drift out of step, and
    no nightly job whose failure would silently free up capacity.
    """
    day = on or ist_today()
    stmt = (
        select(Transaction.admin_ref, func.coalesce(func.sum(Transaction.amount), 0.0))
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            Transaction.admin_ref.isnot(None),
            Transaction.tx_date == day,
            Transaction.status.notin_(RELEASED_STATUSES),
        )
        .group_by(Transaction.admin_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        stmt = stmt.where(Transaction.admin_ref.in_(refs))
    return {ref: _money(total) for ref, total in (await db.execute(stmt)).all() if ref}


async def deposit_counts_today(
    db: AsyncSession, refs: Optional[Sequence[str]] = None, *, on: Optional[date] = None,
) -> dict[str, int]:
    """{account reference → how many deposits were routed to it today}. Shown in Account
    Management, and the first tie-break when two accounts offer the same remaining capacity."""
    day = on or ist_today()
    stmt = (
        select(Transaction.admin_ref, func.count(Transaction.id))
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            Transaction.admin_ref.isnot(None),
            Transaction.tx_date == day,
            Transaction.status.notin_(RELEASED_STATUSES),
        )
        .group_by(Transaction.admin_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        stmt = stmt.where(Transaction.admin_ref.in_(refs))
    return {ref: int(n or 0) for ref, n in (await db.execute(stmt)).all() if ref}


def remaining_credit(account: AccountMaster, used_today: float) -> float:
    """Rupees this account may still receive today. Never negative: an account already at or past
    its ceiling has zero capacity, not a debt."""
    return max(0.0, _money(_money(account.highest_credit) - _money(used_today)))


def remaining_debit(account: AccountMaster, used_today: float) -> float:
    """The debit-side counterpart of :func:`remaining_credit`, against ``highest_debit``.

    Provided so the same "remaining capacity, never the raw amount" arithmetic is available to the
    payout side when that work is specified. It is deliberately NOT wired into any workflow: this
    task covers the deposit allocation flow, and no withdrawal path calls it. Note that
    ``highest_debit`` is still auto-raised by a larger completed debit (the existing high-water
    behaviour in ``transactions._track_account_debit``, left untouched), so a caller enforcing it
    as a ceiling has to settle that question first.
    """
    return max(0.0, _money(_money(account.highest_debit) - _money(used_today)))


# ═══ 2. Merchant note interpretation ════════════════════════════════════════════════════════════

# "Same account" and its natural variants. The note is a free-text message to the agent, so this
# recognises the phrasings operators actually type rather than demanding one exact string.
_SAME_ACCOUNT_RE = re.compile(
    r"\b(same|previous|last|usual|regular|earlier)\s+(bank\s+)?(a/?c|acc|acct|account)\b"
    r"|\bsame\s+as\s+(last|before|previous|usual)\b"
    r"|\bsame\s+bank\b",
    re.IGNORECASE,
)

# Well-known short forms operators use for banks. Only needed where the abbreviation is NOT a
# substring of the stored bank name; anything already contained in the name ("HDFC" in "HDFC
# Bank") matches without an entry here. A missing entry degrades to "no bank matched", never to a
# crash.
_BANK_ALIASES = {
    "bob": "bank of baroda",
    "baroda": "bank of baroda",
    "sbi": "state bank of india",
    "pnb": "punjab national bank",
    "boi": "bank of india",
    "bom": "bank of maharashtra",
    "cbi": "central bank of india",
    "iob": "indian overseas bank",
    "ubi": "union bank of india",
    "cub": "city union bank",
    "kvb": "karur vysya bank",
    "rbl": "rbl bank",
    "idfc": "idfc first bank",
}


# Words that turn a mention of a bank into a REQUEST for it. Without one, a note that merely
# names a bank — "payment received from HDFC", "customer's Bank of Baroda account" — is describing
# the merchant's own side of the transfer, not asking for a receiving account, and reading it as a
# preference would quietly steer the deposit somewhere the merchant never asked for. A note that
# is essentially nothing BUT the bank name is treated as a request too (see _is_request_for).
_REQUEST_CUES = (
    "use", "using", "give", "send", "want", "need", "prefer", "please", "kindly", "provide",
    "share", "allot", "allocate", "assign", "route", "deposit to", "pay to", "credit to",
)


def _norm_text(value: Optional[str]) -> str:
    """Collapse to lowercase alphanumeric words — the shape both the note and the stored account
    names are compared in, so punctuation and spacing never decide a match."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def _name_keys(value: Optional[str]) -> list[str]:
    """The matchable forms of one bank/account name.

    "Bank of Baroda" yields "bank of baroda" and "baroda"; "HDFC Bank" yields "hdfc bank" and
    "hdfc". Dropping the generic words is what lets "Use HDFC" find "HDFC Bank" without a
    hard-coded bank list — the catalogue is the Admin's own accounts.
    """
    full = _norm_text(value)
    if not full:
        return []
    keys = {full}
    stripped = re.sub(r"\b(bank|ltd|limited|of|the|india|account)\b", " ", full)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(stripped) >= 3:
        keys.add(stripped)
    return list(keys)


@dataclass
class NoteRequest:
    """What the merchant's free-text note asked for. A PREFERENCE — never permission to skip a
    rule. Every account it points at still faces the full eligibility pipeline."""
    same_account: bool = False
    bank_name: Optional[str] = None          # the stored bank name the note matched
    account_ref: Optional[str] = None        # a specific account, when the note named one
    matched_text: Optional[str] = None       # what in the note produced the match

    @property
    def has_preference(self) -> bool:
        return bool(self.same_account or self.bank_name or self.account_ref)


def parse_note(note: Optional[str], accounts: Iterable[AccountMaster]) -> NoteRequest:
    """Read a merchant note against the real account catalogue.

    Matching is data-driven: the keys come from the Admin's own accounts — their bank names and
    their account names (e.g. "SINDU") — so a bank is recognised precisely when the platform has
    an account at it. A note naming a bank that does not exist matches nothing and is reported as
    such; it is not an error and it never raises.

    An account-name match wins over a bank match: naming "SINDU" is more specific than naming the
    bank SINDU happens to be at. Among equals the longest key wins, so "Bank of India" is not
    mistaken for "Bank of Baroda" on the shared word "bank".
    """
    req = NoteRequest()
    text = _norm_text(note)
    if not text:
        return req

    if _SAME_ACCOUNT_RE.search(note or ""):
        req.same_account = True

    padded = f" {text} "
    has_cue = any(f" {cue} " in padded for cue in _REQUEST_CUES)

    def _is_request_for(key: str) -> bool:
        """Whether naming ``key`` in this note is a REQUEST for it rather than a mention of it.

        Either the note carries a request cue ("use HDFC"), or the note is essentially nothing but
        the name itself ("Bank of Baroda", "SINDU pls") — at most two other words, which is not
        room for a sentence about something else.
        """
        if has_cue:
            return True
        leftover = [w for w in text.split() if w not in key.split()]
        return len(leftover) <= 2

    def _find(keys: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
        for key in sorted(keys, key=len, reverse=True):
            if len(key) >= 3 and f" {key} " in padded and _is_request_for(key):
                return keys[key], key
        return None, None

    accounts = list(accounts)

    # A specific account by name first ("Use SINDU").
    acct_keys: dict[str, str] = {}
    for acc in accounts:
        for key in _name_keys(acc.account_name):
            acct_keys.setdefault(key, acc.reference_number)
    ref, matched = _find(acct_keys)
    if ref:
        req.account_ref, req.matched_text = ref, matched
        acc = next((a for a in accounts if a.reference_number == ref), None)
        if acc:
            req.bank_name = acc.bank_name
        return req

    # Then the bank ("Use Bank of Baroda" — every Bank of Baroda account is then evaluated).
    bank_keys: dict[str, str] = {}
    for acc in accounts:
        for key in _name_keys(acc.bank_name):
            bank_keys.setdefault(key, acc.bank_name)
    for alias, canonical in _BANK_ALIASES.items():
        for acc in accounts:
            if canonical in _norm_text(acc.bank_name):
                bank_keys.setdefault(alias, acc.bank_name)
                break
    bank, matched = _find(bank_keys)
    if bank:
        req.bank_name, req.matched_text = bank, matched
    return req


# ═══ 3. Customer & account history ══════════════════════════════════════════════════════════════

@dataclass
class MemberHistory:
    """What the ledger already knows about this member. Everything here is derived from real
    deposit rows — there is no member master table on this platform, and no frontend flag is
    trusted (the request form's Profile OLD/NEW selector is the operator's opinion, not a fact)."""
    deposit_count: int = 0
    accounts_used: list[str] = field(default_factory=list)     # most recently used first
    per_account_deposits: dict[str, int] = field(default_factory=dict)

    @property
    def is_new(self) -> bool:
        """A member with no deposit on record is a NEW customer."""
        return self.deposit_count == 0

    @property
    def is_five_plus(self) -> bool:
        """The business's "number of deposits = 5+" condition.

        Computed and carried into the decision and the audit journal. The platform defines no
        action for it, so none is invented here — this is the value that rule needs, ready for
        whatever prioritisation is specified for it.
        """
        return self.deposit_count >= 5

    @property
    def last_account(self) -> Optional[str]:
        return self.accounts_used[0] if self.accounts_used else None


async def member_history(
    db: AsyncSession, member_id: Optional[str], *, exclude_tx_id: Optional[int] = None,
) -> MemberHistory:
    """Deposit history for one member: how many deposits, and which accounts served them.

    "Old vs new" is decided by deposits that were actually raised, so a member whose only request
    was cancelled or rejected still counts as known to the platform — they have transacted here
    before, which is what the distinction is about.

    ``exclude_tx_id`` leaves out the deposit currently BEING allocated. Allocation runs after the
    request row exists (it needs the reference), so without this a genuinely new customer's first
    deposit would find itself in its own history and be classified OLD — losing the unused-account
    preference on the one request that most needs it.
    """
    hist = MemberHistory()
    mid = _norm_member(member_id)
    if not mid:
        return hist

    stmt = (
        select(Transaction.admin_ref, Transaction.id)
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            func.upper(func.trim(func.coalesce(Transaction.member_id, ""))) == mid,
        )
        .order_by(Transaction.id.desc())
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)
    rows = (await db.execute(stmt)).all()
    hist.deposit_count = len(rows)
    for ref, _id in rows:
        if not ref:
            continue
        if ref not in hist.per_account_deposits:
            hist.accounts_used.append(ref)          # first sighting = most recent (ordered desc)
        hist.per_account_deposits[ref] = hist.per_account_deposits.get(ref, 0) + 1

    # The account_transaction links are the platform's own record of "which account served this
    # member" and are written by the manual send too, so a member served before this engine
    # existed still has a history. Appended after the deposit-derived list so recency order holds.
    links = (await db.execute(
        select(AccountTransaction.reference_number)
        .where(func.upper(func.trim(func.coalesce(AccountTransaction.member_id, ""))) == mid)
        .order_by(AccountTransaction.id.desc())
    )).scalars().all()
    for ref in links:
        if ref and ref not in hist.per_account_deposits:
            hist.accounts_used.append(ref)
            hist.per_account_deposits[ref] = 0
    return hist


async def unused_account_refs(db: AsyncSession, refs: Sequence[str]) -> set[str]:
    """Which of these accounts have NEVER received a deposit.

    Determined from actual usage — deposit rows routed to the account and the account_transaction
    links the manual send writes — never from a flag on the account. An account that has served a
    deposit is used, whatever any configuration says.
    """
    refs = list(refs)
    if not refs:
        return set()
    used = set((await db.execute(
        select(Transaction.admin_ref).where(
            Transaction.type.in_(DEPOSIT_TYPES),
            Transaction.admin_ref.in_(refs),
            Transaction.status.notin_(RELEASED_STATUSES),
        ).distinct()
    )).scalars().all())
    used |= set((await db.execute(
        select(AccountTransaction.reference_number)
        .where(AccountTransaction.reference_number.in_(refs)).distinct()
    )).scalars().all())
    return {ref for ref in refs if ref not in used}


async def upi_by_account(db: AsyncSession, refs: Sequence[str]) -> dict[str, str]:
    """{account reference → one ACTIVE linked UPI ID}. An account's UPI capability, read from the
    existing AdminUpi records rather than a new field. The lowest id wins so the same account
    always yields the same UPI — repeat deposits to one account show one VPA, not a rotation."""
    refs = list(refs)
    if not refs:
        return {}
    rows = (await db.execute(
        select(AdminUpi.account_ref, AdminUpi.upi_id)
        .where(AdminUpi.account_ref.in_(refs), func.upper(AdminUpi.status) == "ACTIVE")
        .order_by(AdminUpi.id)
    )).all()
    out: dict[str, str] = {}
    for ref, upi in rows:
        if ref and ref not in out:
            out[ref] = upi
    return out


# ═══ 4. Candidate evaluation ════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """One managed account measured against this specific deposit."""
    account: AccountMaster
    used_today: float
    remaining: float
    deposits_today: int
    member_deposits: int          # how many deposits this member has already made into it
    unused: bool                  # has never received a deposit (real usage, not a flag)
    upi_id: Optional[str]         # the account's active linked UPI, when it has one
    eligible: bool
    reject_reason: Optional[str] = None

    @property
    def ref(self) -> str:
        return self.account.reference_number

    def snapshot(self) -> dict:
        """The point-in-time record of this account's position, for the audit journal."""
        return {
            "accountRef": self.ref,
            "accountName": self.account.account_name,
            "bankName": self.account.bank_name,
            "accountType": _type_value(self.account.account_type),
            "isOwnAccount": bool(self.account.is_own_account),
            "status": self.account.status,
            "highestCredit": _money(self.account.highest_credit),
            "creditUsedToday": _money(self.used_today),
            "remainingCapacity": _money(self.remaining),
            "depositsToday": self.deposits_today,
            "memberDeposits": self.member_deposits,
            "unused": self.unused,
            "eligible": self.eligible,
            "rejectReason": self.reject_reason,
        }


def _evaluate(
    account: AccountMaster, amount: float, *, used_today: float, deposits_today: int,
    member_deposits: int, unused: bool, upi_id: Optional[str],
) -> Candidate:
    """Apply every HARD rule to one account. Cheapest disqualification first.

    The capacity test is the whole point of the exercise and is written as the rule reads:
    projected usage must not exceed the ceiling. Both sides are rounded to paise first, so an
    exact match is accepted and a one-paisa overshoot is not.
    """
    remaining = remaining_credit(account, used_today)
    cand = Candidate(
        account=account, used_today=_money(used_today), remaining=remaining,
        deposits_today=deposits_today, member_deposits=member_deposits, unused=unused,
        upi_id=upi_id, eligible=False,
    )

    # Rule 3 — availability. An inactive/disabled account can never be sent to a merchant.
    if (account.status or "").upper() != "ACTIVE":
        cand.reject_reason = REJECT_INACTIVE
        return cand

    # An account with no configured ceiling has no capacity to give. Treated as not eligible
    # rather than as unlimited — an unconfigured limit must never read as permission.
    if _money(account.highest_credit) <= 0:
        cand.reject_reason = REJECT_NO_LIMIT
        return cand

    # Rule 5 — the hard daily credit limit.
    if _money(_money(used_today) + _money(amount)) > _money(account.highest_credit):
        cand.reject_reason = REJECT_NO_CAPACITY
        return cand

    cand.eligible = True
    return cand


async def evaluate_accounts(
    db: AsyncSession, amount: float, *, deposit_type: Optional[str] = None,
    member_id: Optional[str] = None, history: Optional[MemberHistory] = None,
    on: Optional[date] = None, exclude_tx_id: Optional[int] = None,
) -> list[Candidate]:
    """Measure every managed account against this deposit and return them all — eligible or not.

    The rejected ones are kept deliberately: they are what the allocation journal records as the
    reason a particular account was not chosen.
    """
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    if not accounts:
        return []
    refs = [a.reference_number for a in accounts]
    used = await credit_used_today(db, refs, on=on)
    counts = await deposit_counts_today(db, refs, on=on)
    unused = await unused_account_refs(db, refs)
    # Loaded for every deposit type: a linked UPI is what gets SENT when the request is a UPI one,
    # and it is a ranking preference — never a filter (see UPI_DEPOSIT_TYPE above).
    upis = await upi_by_account(db, refs)
    if history is None:
        history = await member_history(db, member_id, exclude_tx_id=exclude_tx_id)
    return [
        _evaluate(
            a, amount,
            used_today=used.get(a.reference_number, 0.0),
            deposits_today=counts.get(a.reference_number, 0),
            member_deposits=history.per_account_deposits.get(a.reference_number, 0),
            unused=a.reference_number in unused,
            upi_id=upis.get(a.reference_number),
        )
        for a in accounts
    ]


# ═══ 5. Ranking ═════════════════════════════════════════════════════════════════════════════════

def rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Rule 6 — nearest suitable capacity first.

    Among accounts that can all take the deposit, the one with the SMALLEST remaining capacity
    wins: filling a ₹50,000 gap with a ₹45,000 request leaves the ₹95,000 account free for a
    request only it can take. Ties are broken deterministically — fewer deposits today first
    (spreading the day's traffic), then by reference number so the same inputs always produce the
    same answer. Nothing here is random.
    """
    return sorted(candidates, key=lambda c: (c.remaining, c.deposits_today, c.ref))


# ═══ 6. Allocation ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AllocationResult:
    """The engine's answer. ``account`` is None when nothing was eligible — a clear, explicit
    no-account state, never a fallback to some account that cannot take the money."""
    account: Optional[AccountMaster] = None
    upi_id: Optional[str] = None
    rule: Optional[str] = None
    reason: str = ""
    customer_type: str = "NEW"
    highest_credit: Optional[float] = None
    credit_used: Optional[float] = None
    remaining: Optional[float] = None
    note: NoteRequest = field(default_factory=NoteRequest)
    requested_unavailable: bool = False          # the note named a bank/account nothing eligible matched
    candidates: list[Candidate] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def allocated(self) -> bool:
        return self.account is not None

    def snapshot(self) -> Optional[dict]:
        """The immutable account card stored on the deposit and shown to the merchant. Deliberately
        limited to what the merchant needs in order to pay: no capacity, no ranking, no other
        candidate, no internal figure of any kind.

        When the deposit is being paid by UPI the account number, IFSC and branch are left out —
        the merchant is being asked to pay a UPI ID, and the platform's existing UPI send already
        withholds the bank details in that case ("a UPI send doesn't also expose bank details",
        routes/transactions.account_submit). The receiving account is still named, because that is
        what the payment credits and what the merchant is entitled to see.
        """
        if not self.account:
            return None
        a = self.account
        out = {
            "bankName": a.bank_name,
            "accountName": a.account_name,
            "accountType": _type_value(a.account_type),
            "referenceNumber": a.reference_number,
        }
        if self.upi_id:
            out["upiId"] = self.upi_id
        else:
            out["accountNumber"] = a.account_number
            out["ifsc"] = a.ifsc_code
            out["branch"] = a.branch
        return out


def _tiers(candidates: list[Candidate], history: MemberHistory, note: NoteRequest,
           *, deposit_type: Optional[str] = None) -> list[tuple[str, list[Candidate]]]:
    """The ordered preference pools, most preferred first. Every pool is already hard-filtered, so
    a rule can only ever change WHICH eligible account is picked — never whether an ineligible one
    becomes acceptable. Each pool is a subset of the eligible set, and the last pool is always the
    full eligible set, so a preference can narrow the choice but can never empty it.
    """
    tiers: list[tuple[str, list[Candidate]]] = []

    # A UPI request prefers an account that can actually be paid by UPI, when one is eligible.
    # A PREFERENCE, expressed as a pool — not a filter: if no eligible account has a linked UPI the
    # later pools still cover every eligible account and the merchant is sent bank details instead.
    if (deposit_type or "").upper() == UPI_DEPOSIT_TYPE:
        with_upi = [c for c in candidates if c.upi_id]
        if with_upi and len(with_upi) < len(candidates):
            tiers.append((RULES.UPI_CAPABLE, with_upi))

    if history.is_new:
        # Rule 1 — a new customer prefers an account that has never been used, determined from
        # real usage data rather than any flag.
        unused = [c for c in candidates if c.unused]
        if unused:
            tiers.append((RULES.NEW_UNUSED, unused))
        # The specified fallback: no unused account → eligible SAVINGS accounts, taking the lower
        # remaining capacity first (which is exactly what rank() already does). The hard credit
        # limit is untouched by this — the pool only ever contains accounts that passed it.
        savings = [c for c in candidates if _type_value(c.account.account_type) == AccountType.SAVINGS.value]
        if savings:
            tiers.append((RULES.NEW_SAVINGS_FALLBACK, savings))
    else:
        # Rule 1 — an existing customer's own account history comes first, most-recently-used
        # account preferred among equally ranked ones.
        order = {ref: i for i, ref in enumerate(history.accounts_used)}
        seen = [c for c in candidates if c.ref in order]
        if seen:
            seen = sorted(rank(seen), key=lambda c: (c.remaining, order.get(c.ref, 10**6)))
            tiers.append((RULES.OLD_ACCOUNT_HISTORY, seen))

    # Everything eligible, nearest capacity — the universal fallback that guarantees an eligible
    # account is never left unused just because no preference matched it.
    tiers.append((RULES.NOTE_BANK if note.has_preference else RULES.NEAREST_CAPACITY, candidates))
    return tiers


async def _lock(db: AsyncSession, ref: str, *, skip_locked: bool) -> Optional[AccountMaster]:
    """Row-lock one account. ``skip_locked`` returns None instead of waiting when another
    transaction holds it, which is what keeps concurrent allocations deadlock-free."""
    stmt = select(AccountMaster).where(AccountMaster.reference_number == ref)
    stmt = stmt.with_for_update(skip_locked=True) if skip_locked else stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _claim(
    db: AsyncSession, ordered: list[Candidate], amount: float, *, on: Optional[date] = None,
) -> tuple[Optional[Candidate], list[str]]:
    """Walk the ranked candidates and claim the first one that still has room UNDER ITS LOCK.

    Re-reading usage while holding the lock is the point: the ranking was computed from an
    unlocked read, and by the time we get here another request may have consumed the capacity we
    counted on. If it has, that account is dropped and selection continues down the list — which
    is exactly the "re-run selection against the remaining accounts" the concurrency rule calls
    for, without ever waiting on a lock we might deadlock against.
    """
    skipped: list[str] = []
    # Pass 1 never waits (SKIP LOCKED); pass 2 revisits ONLY what pass 1 skipped, this time
    # blocking. By then the caller holds no lock, so a single blocking wait cannot form a cycle.
    for pass_skip_locked in (True, False):
        for cand in ordered:
            if not pass_skip_locked and cand.ref not in skipped:
                continue
            locked = await _lock(db, cand.ref, skip_locked=pass_skip_locked)
            if locked is None:
                if pass_skip_locked:
                    skipped.append(cand.ref)
                continue
            if (locked.status or "").upper() != "ACTIVE":
                continue        # toggled inactive between the read and the lock
            used = (await credit_used_today(db, [cand.ref], on=on)).get(cand.ref, 0.0)
            if _money(_money(used) + _money(amount)) > _money(locked.highest_credit):
                continue        # someone else took the room; try the next account
            cand.account = locked
            cand.used_today = _money(used)
            cand.remaining = remaining_credit(locked, used)
            return cand, skipped
        if not skipped:
            break               # nothing was skipped, so the blocking pass has nothing to revisit
    return None, skipped


async def allocate_deposit_account(
    db: AsyncSession,
    *,
    amount: float,
    member_id: Optional[str] = None,
    deposit_type: Optional[str] = None,
    note: Optional[str] = None,
    on: Optional[date] = None,
    exclude_tx_id: Optional[int] = None,
) -> AllocationResult:
    """Select the best eligible account for one deposit, or report that there is none.

    The caller supplies only the deposit's own facts. Every rule, every figure and the final
    choice are decided here, under a row lock, from data in the database.

    Nothing is written by this function except the lock it holds: assigning the account to the
    deposit is the caller's job, inside the caller's transaction, so the allocation and the
    deposit row land together or not at all.
    """
    amount = _money(amount)
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    parsed = parse_note(note, accounts)
    history = await member_history(db, member_id, exclude_tx_id=exclude_tx_id)
    customer_type = "NEW" if history.is_new else "OLD"

    result = AllocationResult(customer_type=customer_type, note=parsed)
    result.detail = {
        "customerType": customer_type,
        "memberDepositCount": history.deposit_count,
        "fivePlusDeposits": history.is_five_plus,           # Rule 9 — computed and carried
        "depositType": deposit_type,
        "noteSameAccount": parsed.same_account,
        "noteBank": parsed.bank_name,
        "noteAccountRef": parsed.account_ref,
        "noteMatchedText": parsed.matched_text,
        "previousAccounts": history.accounts_used[:5],
    }

    if not accounts:
        result.detail["failure"], result.reason = _failure([], amount)
        return result

    candidates = await evaluate_accounts(
        db, amount, deposit_type=deposit_type, member_id=member_id, history=history, on=on,
        exclude_tx_id=exclude_tx_id)
    result.candidates = candidates
    eligible = [c for c in candidates if c.eligible]
    result.detail["accounts"] = [c.snapshot() for c in candidates]

    if not eligible:
        code, sentence = _failure(candidates, amount)
        result.detail["failure"] = code
        # A note that named a bank or an account is worth repeating back: "no eligible account" and
        # "the bank you asked for has no eligible account" send an Admin looking in different places.
        wanted = parsed.bank_name or parsed.account_ref
        if wanted:
            sentence = f"{sentence} (requested: {wanted} — unavailable)"
            result.requested_unavailable = True
        result.reason = sentence
        return result

    is_upi_request = (deposit_type or "").upper() == UPI_DEPOSIT_TYPE

    def _finish(cand: Candidate, rule: str, extra: str = "") -> AllocationResult:
        result.account = cand.account
        # The linked UPI is SENT only when the merchant said they are paying by UPI. Candidates
        # carry it whatever the deposit type (it is a ranking preference for UPI requests), so
        # without this gate a BANK/IMPS/NEFT/RTGS deposit into a UPI-linked account would be
        # handed a VPA and have its bank details withheld — the wrong instructions entirely.
        result.upi_id = cand.upi_id if is_upi_request else None
        result.rule = rule
        result.reason = _RULE_TEXT.get(rule, rule) + (f" ({extra})" if extra else "")
        result.highest_credit = _money(cand.account.highest_credit)
        result.credit_used = _money(cand.used_today)
        result.remaining = _money(cand.remaining)
        result.detail["selected"] = cand.snapshot()
        return result

    # ── Rule 7 — an explicit "same account" request ──
    # Checked against every mandatory rule like any other candidate. A previous account that is
    # unavailable or out of capacity is NOT used, and the reason is recorded.
    if parsed.same_account:
        previous = history.last_account
        target = next((c for c in eligible if c.ref == previous), None) if previous else None
        if target:
            claimed, _ = await _claim(db, [target], amount, on=on)
            if claimed:
                return _finish(claimed, RULES.SAME_ACCOUNT)
        # Record WHY the request could not be honoured. An account that failed a hard rule carries
        # its own reason; one that passed every rule and still could not be claimed lost the race
        # to a concurrent allocation.
        blocked = next((c for c in candidates if c.ref == previous), None) if previous else None
        if not previous:
            why = "NO_PREVIOUS_ACCOUNT"
        elif blocked is None:
            why = "ACCOUNT_NO_LONGER_EXISTS"
        else:
            why = blocked.reject_reason or REJECT_LOCKED
        result.requested_unavailable = True
        result.detail["sameAccountRejected"] = {"accountRef": previous, "reason": why}

    # ── Rule 8 — a bank/account named in the note ──
    # A preference, not an override: the pool is narrowed to the requested bank and EVERY account
    # in it is evaluated (four Bank of Baroda accounts means four evaluations, not the first one).
    # If none of them is eligible, the note is reported as unfulfilled and the normal rules run.
    pool = eligible
    if parsed.account_ref:
        preferred = [c for c in eligible if c.ref == parsed.account_ref]
    elif parsed.bank_name:
        preferred = [c for c in eligible if c.account.bank_name == parsed.bank_name]
    else:
        preferred = []
    if (parsed.account_ref or parsed.bank_name):
        if preferred:
            pool = preferred
        else:
            result.requested_unavailable = True
            result.detail["requestedBankUnavailable"] = parsed.bank_name or parsed.account_ref

    # ── Rules 1 / 12 / 6 — preference tiers, then nearest suitable capacity ──
    for rule, tier in _tiers(pool, history, parsed, deposit_type=deposit_type):
        ordered = tier if rule == RULES.OLD_ACCOUNT_HISTORY else rank(tier)
        claimed, skipped = await _claim(db, ordered, amount, on=on)
        if skipped:
            result.detail.setdefault("concurrentlyLocked", []).extend(skipped)
        if claimed:
            extra = ""
            if result.requested_unavailable and parsed.has_preference:
                extra = "requested account unavailable — fallback applied"
            elif pool is preferred and parsed.bank_name:
                extra = f"requested bank {parsed.bank_name}"
            return _finish(claimed, rule, extra)

    # Every eligible account lost its capacity to a concurrent request between the read and the
    # lock. No account is assigned — the deposit stays unallocated rather than breaching a limit.
    result.reason = (f"No account could be reserved for ₹{amount:,.2f} — the remaining capacity was "
                     f"taken by concurrent requests. Retry the allocation.")
    result.detail["failure"] = FAIL_RACE
    return result


# ═══ 7. Audit journal ═══════════════════════════════════════════════════════════════════════════

async def record_allocation(
    db: AsyncSession, result: AllocationResult, *, transaction: Transaction,
) -> DepositAllocation:
    """Write the append-only journal row for one allocation attempt — successful or not.

    Every figure the decision rested on is stored, because none of them can be recovered later:
    today's usage moves with the next deposit, and an Admin may re-configure the limit tomorrow.
    """
    acc = result.account
    row = DepositAllocation(
        transaction_ref=transaction.ref,
        transaction_id=transaction.id,
        merchant_id=transaction.merchant_id,
        merchant_name=transaction.merchant_name,
        member_id=transaction.member_id,
        member_name=transaction.member_name,
        requested_amount=_money(transaction.amount),
        deposit_type=transaction.deposit_type,
        merchant_note=transaction.notes,
        outcome=OUTCOME_ALLOCATED if result.allocated else OUTCOME_NO_ACCOUNT,
        account_ref=acc.reference_number if acc else None,
        account_id=acc.id if acc else None,
        account_name=acc.account_name if acc else None,
        bank_name=acc.bank_name if acc else None,
        account_type=_type_value(acc.account_type) if acc else None,
        is_own_account=bool(acc.is_own_account) if acc else None,
        highest_credit=result.highest_credit,
        credit_used_today=result.credit_used,
        remaining_capacity=result.remaining,
        rule=result.rule,
        reason=result.reason,
        detail=json.dumps(result.detail, default=str),
        customer_type=result.customer_type,
        candidates_considered=len(result.candidates),
        candidates_eligible=sum(1 for c in result.candidates if c.eligible),
        member_deposit_count=result.detail.get("memberDepositCount"),
        created_at=datetime.utcnow(),
        created_at_ist=ist_stamp(),
    )
    db.add(row)
    await db.flush()
    return row


def serialize(row: DepositAllocation) -> dict:
    """API shape for one journal row (camelCase, matching the rest of the API)."""
    return {
        "id": row.id,
        "transactionRef": row.transaction_ref,
        "merchant": row.merchant_name,
        "memberId": row.member_id,
        "memberName": row.member_name,
        "requestedAmount": _money(row.requested_amount),
        "depositType": row.deposit_type,
        "merchantNote": row.merchant_note,
        "outcome": row.outcome,
        "accountRef": row.account_ref,
        "accountName": row.account_name,
        "bankName": row.bank_name,
        "accountType": row.account_type,
        "isOwnAccount": row.is_own_account,
        "highestCredit": row.highest_credit,
        "creditUsedToday": row.credit_used_today,
        "remainingCapacity": row.remaining_capacity,
        "rule": row.rule,
        "reason": row.reason,
        "customerType": row.customer_type,
        "candidatesConsidered": row.candidates_considered,
        "candidatesEligible": row.candidates_eligible,
        "memberDepositCount": row.member_deposit_count,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
        "createdAtIst": row.created_at_ist,
    }
