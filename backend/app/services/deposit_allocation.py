"""Automatic deposit account allocation.

THE single authority for "which managed bank account should receive this deposit?". The merchant
never picks one and the Admin never picks one per request — the Admin configures accounts and
their limits in Account Management, and this service decides. The frontend renders the result; it
never re-implements any of the rules below.

WHAT MAKES A LIMIT A LIMIT — TWO KINDS, NEVER CONFUSED
──────────────────────────────────────────────────────
A deposit must clear BOTH of an account's credit limits, and they answer different questions:

  • ``AccountMaster.highest_credit`` is the PER-TRANSACTION cap — the most ONE deposit may be.
    The test is ``amount <= highest_credit`` and nothing else: a ₹75,000 ceiling accepts ₹75,000
    and refuses ₹75,000.01, whatever capacity the day has left. Earlier deposits do NOT eat into
    it — three requests of ₹70,000, ₹20,000 and ₹30,000 are each individually fine.

  • ``AccountMaster.bank_daily_credit_limit`` is the CUMULATIVE daily ceiling — the most the
    account may RECEIVE in TOTAL across one IST calendar day. The test is

        credit_used_today(account) + amount  <=  bank_daily_credit_limit

    so a ₹2,00,000 daily limit that has taken ₹1,50,000 today can still accept ₹50,000 and cannot
    accept ₹50,001. Equality passes; a single paisa over does not.

An account with EITHER limit at 0 is "unconfigured" — it has no configured capacity and is never
chosen automatically. 0 is never read as "unlimited".

WHAT "USED TODAY" COUNTS — AND WHY IT ISN'T ONLY COMPLETED DEPOSITS
──────────────────────────────────────────────────────────────────
A deposit consumes daily bank capacity from the moment an account is allocated to it, not from the
moment the money lands. If only completed deposits counted, two requests arriving seconds apart
would both read "₹0 used", both be allocated the same account, and together breach the daily limit
— the exact failure this service exists to prevent. So today's usage is every deposit routed to the
account today that is still LIVE, plus every completed one, PLUS manual Account-Management CREDIT
adjustments dated today (they add real money to the account and must consume the same daily bank
capacity, never bypass it):

    type LIKE 'DEPOSIT%'  AND  admin_ref = <account>
    AND  COALESCE(account_assigned_on, tx_date) = <today, IST>
    AND  status NOT IN (REJECTED, SA_REJECTED, CANCELLED)
  + MANUAL_ADJUSTMENT CREDIT entries on <account> whose IST day is <today>

Rejecting or cancelling a request therefore RELEASES its capacity, automatically, with no separate
reservation record to keep in step. This is the platform's existing state machine doing the work:
"available", "temporarily assigned", "limit reached" and "unavailable" are all read off states and
data that already exist. No second balance store, no reservation table, no new status.

A deposit belongs to the IST day its receiving account was ASSIGNED (``account_assigned_on``,
written by the engine and by the Admin's manual send) — the day it starts consuming capacity —
not the day the request was raised, so a request raised yesterday and placed today counts against
TODAY. Rows assigned before that column existed fall back to ``tx_date``, as they always were.
Usage resets naturally at midnight IST: nothing is reset, recalculated or written at the boundary —
tomorrow's query simply selects a different day. Historical rows are never touched.

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
    AccountLedgerEntry, AccountMaster, AccountTransaction, AccountType, AdminUpi, DepositAllocation,
    Transaction, TxStatus, TxType,
)
from app.services.account_ledger import _COMPLETED_DEPOSIT

IST = timezone(timedelta(hours=5, minutes=30))

# Deposit rows in these states have been abandoned, so they release the capacity they held. Every
# other state — from ACCOUNT_SUBMITTED through DEPOSITED — still consumes it.
RELEASED_STATUSES = (TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED)

# The Own Account rule switches on AFTER this many successful deposits: 1-5 → the normal rules;
# the 6th onward → an eligible Own Account is preferred (see MemberHistory.prefers_own_account).
OWN_ACCOUNT_THRESHOLD = 5

# A SUCCESSFUL deposit — the money arrived. The platform's one definition of "received"
# (services/account_ledger, shared with the member Profile), not a second one.
SUCCESSFUL_STATUSES = _COMPLETED_DEPOSIT

DEPOSIT_TYPES = (TxType.DEPOSIT, TxType.DEPOSIT_REQUEST)

# Deposit types paid into a managed bank account, which therefore need one allocated. CASH and
# CRYPTO carry their own member-supplied proof and skip the account hop entirely; CARD is paid
# through a payment-gateway link the Admin submits, not an account. All three are untouched here.
ALLOCATABLE_DEPOSIT_TYPES = ("UPI", "BANK", "IMPS", "NEFT", "RTGS")

# ── Payin Mode — which deposit types an account may RECEIVE ─────────────────────────────────────
# The deposit-side counterpart of ``AccountMaster.payout_modes``, stored the same way: a
# comma-separated list of the platform's own deposit_type codes. These are the existing canonical
# values (the merchant form's DEPOSIT_TYPE_OPTIONS) — nothing is invented here. Cash and Crypto are
# absent because they are never paid into a managed account.
#
# NULL / empty means EVERY mode, for the reason payout_modes does: a capability column that
# started empty and was read as "receives nothing" would disqualify every account on a platform
# where no Admin has configured one, and send every deposit to the exception queue.
PAYIN_MODES = ("UPI", "IMPS", "NEFT", "RTGS", "CDM", "CREDIT_CARD", "DEBIT_CARD")
# The retired generic BANK type (historical rows only) is served by any of the specific rails.
_BANK_RAILS = ("IMPS", "NEFT", "RTGS")


def account_payin_modes(account: AccountMaster) -> Optional[set[str]]:
    """The account's configured payin modes, or None when unconfigured (= every mode)."""
    raw = (getattr(account, "payin_modes", None) or "").strip()
    if not raw:
        return None
    modes = {m.strip().upper() for m in raw.split(",") if m.strip()}
    return modes or None


def supports_payin_mode(account: AccountMaster, deposit_type: Optional[str]) -> bool:
    """Whether this account may receive a deposit of ``deposit_type``. An unconfigured account, or
    a deposit with no type, is never refused on this ground."""
    modes = account_payin_modes(account)
    mode = (deposit_type or "").strip().upper()
    if modes is None or not mode:
        return True
    if mode == "BANK":
        return bool(modes & set(_BANK_RAILS))
    return mode in modes

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
    EXACT_ACCOUNT = "EXACT_ACCOUNT_REQUEST"
    SAME_ACCOUNT = "SAME_ACCOUNT_REQUEST"
    NOTE_BANK = "MERCHANT_NOTE_BANK_PREFERENCE"
    NEW_UNUSED = "NEW_CUSTOMER_UNUSED_ACCOUNT"
    NEW_SAVINGS_FALLBACK = "NEW_CUSTOMER_SAVINGS_FALLBACK"
    NEW_FEWEST_SERVED = "NEW_CUSTOMER_FEWEST_CUSTOMERS_SERVED"
    OWN_ACCOUNT = "OWN_ACCOUNT_PREFERENCE"
    OLD_ACCOUNT_HISTORY = "EXISTING_CUSTOMER_ACCOUNT_HISTORY"
    NEAREST_CAPACITY = "NEAREST_SUITABLE_CAPACITY"
    UPI_CAPABLE = "UPI_DEPOSIT_UPI_ENABLED_ACCOUNT"


_RULE_TEXT = {
    RULES.EXACT_ACCOUNT: "Exact account requested by merchant note + eligible",
    RULES.SAME_ACCOUNT: "Same account requested by merchant note + eligible",
    RULES.NEW_FEWEST_SERVED: (
        "New customer + eligible account that has served the fewest other customers"),
    RULES.OWN_ACCOUNT: (
        "Customer with more than 5 successful deposits + eligible Own Account"),
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
# The deposit is larger than this account's PER-TRANSACTION Highest Credit — a different failure
# from the cumulative daily one, and fixed differently (raise the single-transaction cap, or split
# the deposit), so it is recorded distinctly.
REJECT_OVER_PER_TXN = "AMOUNT_EXCEEDS_HIGHEST_CREDIT"
REJECT_NO_CAPACITY = "DAILY_CREDIT_LIMIT_REACHED"
REJECT_LOCKED = "CONCURRENTLY_ALLOCATED"
# The member is sending from a Savings account and this one is Current, or the reverse. A hard
# rule, not a preference: money must land in an account of the same kind it was sent from.
REJECT_ACCOUNT_TYPE = "ACCOUNT_TYPE_MISMATCH"
# An Admin has narrowed this account's Payin Mode and this deposit's type is not in it.
REJECT_PAYIN_MODE = "PAYIN_MODE_NOT_SUPPORTED"

# The distinct ways an allocation can find nothing. Recorded on the journal and shown to the Admin,
# because each one has a different fix: activate an account, raise a limit, add an account, or wait
# for tomorrow. A single "no eligible account" sentence tells them none of that.
FAIL_NO_ACCOUNTS = "NO_ACCOUNTS_CONFIGURED"
FAIL_ALL_UNAVAILABLE = "ALL_ACCOUNTS_UNAVAILABLE"
FAIL_NO_LIMITS = "NO_CREDIT_LIMITS_CONFIGURED"
FAIL_LIMIT_REACHED = "ALL_ACCOUNTS_AT_DAILY_LIMIT"
FAIL_AMOUNT_TOO_LARGE = "AMOUNT_EXCEEDS_EVERY_REMAINING_CAPACITY"
FAIL_NO_MATCHING_TYPE = "NO_ACCOUNT_OF_THE_REQUIRED_TYPE"
FAIL_NO_PAYIN_MODE = "NO_ACCOUNT_ACCEPTS_THIS_PAYIN_MODE"
FAIL_MIXED = "NO_ELIGIBLE_ACCOUNT"
FAIL_RACE = "CAPACITY_TAKEN_CONCURRENTLY"
# An EXPLICIT request in the note that cannot be met automatically. Each is an Admin decision, never
# a reason to place the deposit somewhere the merchant did not ask for.
FAIL_REQUESTED_ACCOUNT_NOT_ELIGIBLE = "REQUESTED_ACCOUNT_NOT_ELIGIBLE"
FAIL_REQUESTED_ACCOUNT_NOT_FOUND = "REQUESTED_ACCOUNT_NOT_FOUND"
FAIL_REQUESTED_ACCOUNT_AMBIGUOUS = "REQUESTED_ACCOUNT_AMBIGUOUS"
FAIL_SAME_ACCOUNT_NOT_ELIGIBLE = "SAME_ACCOUNT_NOT_ELIGIBLE"
FAIL_NO_PREVIOUS_ACCOUNT = "NO_PREVIOUS_SUCCESSFUL_ACCOUNT"
FAIL_REQUESTED_BANK_NOT_ELIGIBLE = "REQUESTED_BANK_NOT_ELIGIBLE"


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
            f"No account has both a per-transaction Highest Credit and a daily bank credit limit "
            f"configured, so none can accept a deposit. Set both in Account Management.")
    # Named explicitly, because the fix is completely different from every other refusal: the
    # money is coming from a Savings (or Current) account and the platform has no ACTIVE account
    # of that kind with room. Reporting this as "at the daily limit" would send an Admin to raise
    # a limit that was never the obstacle.
    type_blocked = [c for c in candidates if c.reject_reason == REJECT_ACCOUNT_TYPE]
    # The type the money is coming FROM, deduced from the accounts refused for being the other
    # kind. Once a type constraint is in play EVERY message below has to say so, or it describes
    # a pool the Admin cannot see and quietly misstates the platform's real capacity.
    required = None
    if type_blocked:
        wrong = _type_value(type_blocked[0].account.account_type)
        required = (AccountType.CURRENT.value if wrong == AccountType.SAVINGS.value
                    else AccountType.SAVINGS.value)
        of_type = f" of the required type ({required})"
    else:
        of_type = ""

    if any(r == REJECT_PAYIN_MODE for r in reasons) and all(
            r in (REJECT_PAYIN_MODE, REJECT_ACCOUNT_TYPE, REJECT_INACTIVE, REJECT_NO_LIMIT)
            for r in reasons):
        return FAIL_NO_PAYIN_MODE, (
            "No active, configured account has this deposit's payment mode enabled in its Payin "
            "Mode. Enable the mode on an account in Account Management, or assign one manually.")

    if type_blocked and all(
            r in (REJECT_ACCOUNT_TYPE, REJECT_INACTIVE, REJECT_NO_LIMIT) for r in reasons):
        return FAIL_NO_MATCHING_TYPE, (
            f"The member is sending from a {required}, and no eligible account of that type is "
            f"available. Add or activate a {required} in Account Management, or assign an account "
            f"manually.")

    # Only accounts that were otherwise usable say anything about capacity, and there are now two
    # distinct capacity failures with different fixes.
    pertxn_blocked = [c for c in candidates if c.reject_reason == REJECT_OVER_PER_TXN]
    capacity_blocked = [c for c in candidates if c.reject_reason == REJECT_NO_CAPACITY]

    if capacity_blocked:
        # A deposit that exceeds even the daily LIMIT (not just today's remainder) of every account
        # cannot be helped by waiting for midnight — say so, and point at the daily limit.
        biggest_daily = max(_money(c.account.bank_daily_credit_limit) for c in capacity_blocked)
        if _money(amount) > biggest_daily:
            return FAIL_AMOUNT_TOO_LARGE, (
                f"{money} is larger than the daily credit limit of every account{of_type} "
                f"(the largest is ₹{biggest_daily:,.2f}). Raise a daily credit limit or add an "
                f"account{' of that type' if required else ''}.")
        left = max(c.remaining for c in capacity_blocked)
        return FAIL_LIMIT_REACHED, (
            f"Every eligible account{of_type} has reached its daily credit limit for {money} — "
            f"the most any has left today is ₹{left:,.2f}. Capacity resets at midnight IST, or "
            f"raise a limit.")

    if pertxn_blocked:
        # The deposit is bigger than any single account's PER-TRANSACTION Highest Credit — waiting
        # for tomorrow never helps; the single-transaction cap has to rise (or the deposit split).
        biggest_pertxn = max(_money(c.account.highest_credit) for c in pertxn_blocked)
        return FAIL_AMOUNT_TOO_LARGE, (
            f"{money} is larger than the per-transaction Highest Credit of every account{of_type} "
            f"(the largest is ₹{biggest_pertxn:,.2f}). Raise a Highest Credit or add an account"
            f"{' of that type' if required else ''}.")

    return FAIL_MIXED, (
        f"No account can accept {money} — all {n} are unavailable or out of daily credit capacity.")


# Outcomes recorded on the allocation journal.
OUTCOME_ALLOCATED = "ALLOCATED"
OUTCOME_NO_ACCOUNT = "NO_ACCOUNT"


def ist_today(now: Optional[datetime] = None) -> date:
    """The current business date in IST — the boundary the daily credit limit resets on."""
    return (now or datetime.now(IST)).astimezone(IST).date()


def ist_day_utc_bounds(day: date) -> tuple[datetime, datetime]:
    """The [start, end) UTC datetimes spanning one IST calendar ``day``.

    Deposit rows carry an IST-dated ``tx_date`` column and filter on a plain equality, but manual
    ledger adjustments only carry a UTC ``created_at``. Converting that column per-row inside SQL is
    both DB-specific and un-indexable, so instead the IST day is turned into the UTC window it
    occupies once, here, and the query stays a simple indexed range. Naive (tz-stripped) datetimes,
    to match how ``created_at`` is stored (``datetime.utcnow()``).
    """
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, start_utc + timedelta(days=1)


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

def usage_date():
    """The IST date a deposit's daily credit usage belongs to: the day its receiving account was
    ASSIGNED (``account_assigned_on``), which is when it starts consuming the account's Bank Daily
    Credit Limit — automatically or by an Admin. ``tx_date`` (the day the request was raised) is
    only the fallback, for rows assigned before the assignment date was recorded; those were always
    counted that way, so no historical figure moves.

    Using ``tx_date`` alone was wrong for any request placed on a later day than it was raised — a
    retry, or a manual send from the queue the next morning. Its amount was charged to a day that
    had already ended, so today's total never saw it and the account could take more than its
    limit today.
    """
    return func.coalesce(Transaction.account_assigned_on, Transaction.tx_date)


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
            usage_date() == day,
            Transaction.status.notin_(RELEASED_STATUSES),
        )
        .group_by(Transaction.admin_ref)
    )
    if refs is not None:
        refs = list(refs)
        if not refs:
            return {}
        stmt = stmt.where(Transaction.admin_ref.in_(refs))
    out = {ref: _money(total) for ref, total in (await db.execute(stmt)).all() if ref}

    # Manual Account-Management CREDIT adjustments dated today also add money to the account, so
    # they consume the same daily bank credit capacity and are added here rather than kept in a
    # separate counter the engine could evaluate around. Filtered by the UTC window of the IST day
    # (manual entries carry only created_at), summed per account, and folded in.
    start_utc, end_utc = ist_day_utc_bounds(day)
    adj_stmt = (
        select(AccountLedgerEntry.account_ref,
               func.coalesce(func.sum(AccountLedgerEntry.amount), 0.0))
        .where(
            AccountLedgerEntry.entry_type == "MANUAL_ADJUSTMENT",
            AccountLedgerEntry.direction == "CREDIT",
            AccountLedgerEntry.account_ref.isnot(None),
            AccountLedgerEntry.created_at >= start_utc,
            AccountLedgerEntry.created_at < end_utc,
        )
        .group_by(AccountLedgerEntry.account_ref)
    )
    if refs is not None:
        adj_stmt = adj_stmt.where(AccountLedgerEntry.account_ref.in_(refs))
    for ref, total in (await db.execute(adj_stmt)).all():
        if ref:
            out[ref] = _money(out.get(ref, 0.0) + _money(total))
    return out


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
            usage_date() == day,
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
    """Rupees this account may still receive today, against its CUMULATIVE daily bank credit limit.
    Never negative: an account already at or past its ceiling has zero capacity, not a debt."""
    return max(0.0, _money(_money(account.bank_daily_credit_limit) - _money(used_today)))


def remaining_debit(account: AccountMaster, used_today: float) -> float:
    """The debit-side counterpart of :func:`remaining_credit`, against the CUMULATIVE daily bank
    debit limit. Kept here so the deposit module's callers (e.g. the balances endpoint) have one
    import site; the withdrawal engine has its own copy against the same field.
    """
    return max(0.0, _money(_money(account.bank_daily_debit_limit) - _money(used_today)))


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


# Request types, in the order the engine honours them (see allocate_deposit_account).
REQUEST_EXACT_ACCOUNT = "EXACT_ACCOUNT"
REQUEST_SAME_ACCOUNT = "SAME_ACCOUNT"
REQUEST_BANK = "BANK"

# How an exact account was identified.
MATCH_ACCOUNT_ID = "ACCOUNT_ID"            # "Use ACC000123" — the platform's own reference
MATCH_ACCOUNT_NUMBER = "ACCOUNT_NUMBER"    # "Use account 1234567890"
MATCH_ACCOUNT_ENDING = "ACCOUNT_ENDING"    # "Give account ending 7890"
MATCH_ACCOUNT_NAME = "ACCOUNT_NAME"        # "Use SINDU"
MATCH_UNSPECIFIED = "UNSPECIFIED"          # "Use this account" — a request with no identifier

# An exact-account request that names nothing resolvable is still a request: it is handed to the
# Admin rather than ignored, because ignoring it is exactly the silent substitution it forbids.
ACCOUNT_RESOLVED = "RESOLVED"
ACCOUNT_NOT_FOUND = "NOT_FOUND"
ACCOUNT_AMBIGUOUS = "AMBIGUOUS"

# "ACC000123" — the platform's canonical account reference (AccountMaster.reference_number).
_ACCOUNT_ID_RE = re.compile(r"\b(acc\d{3,})\b", re.IGNORECASE)
# "ending 7890", "ending with 7890", "ends in 7890", "last 4 digits 7890".
_ACCOUNT_ENDING_RE = re.compile(
    r"\b(?:ending|ends?)\s*(?:with|in)?\s*(?:no\.?|number|digits?)?\s*[:#-]?\s*(\d{3,})\b"
    r"|\blast\s*(?:\d+\s*)?digits?\s*(?:are|is|of)?\s*[:#-]?\s*(\d{3,})\b",
    re.IGNORECASE,
)
# "account 1234567890", "a/c no 1234567890", "acct: 1234567890" — a number the note explicitly
# presents AS an account number. A bare digit run is only treated as an account when it equals a
# real account number: notes carry UTRs, amounts and phone numbers too.
#
# The keyword must end at a word boundary, so "ACC0000123" (an account ID) is never read as the cue
# "acc" followed by the number "0000123"; likewise a digit run must stand alone, not sit inside an
# identifier.
_ACCOUNT_NUMBER_CUE_RE = re.compile(
    r"\b(?:a/?c|acc(?:ount)?|acct)\b\s*(?:no\.?|number|num|#)?\s*[:#-]?\s*(\d{6,})\b",
    re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r"(?<![A-Za-z0-9])\d{6,}(?![A-Za-z0-9])")
# "use this account" — explicitly an account request, but one that identifies nothing.
_THIS_ACCOUNT_RE = re.compile(r"\b(?:this|that)\s+(?:bank\s+)?(?:a/?c|acc|acct|account)\b",
                              re.IGNORECASE)


def _digits(value: Optional[str]) -> str:
    """An account number as bare digits (account_master rows can carry stray spaces)."""
    return re.sub(r"\D+", "", value or "")


def mask_account_number(value: Optional[str]) -> Optional[str]:
    """``XXXXXX7890`` — the last four digits only: the platform's convention for an account number
    shown outside the Admin Portal (a Telegram message, a log line)."""
    digits = _digits(value) or (value or "").strip()
    if not digits:
        return None
    return ("X" * max(len(digits) - 4, 0)) + digits[-4:]


@dataclass
class NoteRequest:
    """What the merchant's free-text note asked for. It decides WHICH account is wanted, never
    whether an account may be used: every account it points at still faces the full eligibility
    pipeline, and an explicit request that cannot pass it goes to the Admin — not elsewhere."""
    same_account: bool = False
    bank_name: Optional[str] = None          # the stored bank name the note matched
    account_ref: Optional[str] = None        # a specific account, when the note named one
    matched_text: Optional[str] = None       # what in the note produced the match
    # ── Exact account request ── set whenever the note asks for ONE account, resolved or not.
    account_requested: bool = False
    account_match: Optional[str] = None      # MATCH_* — how it was identified
    account_status: Optional[str] = None     # ACCOUNT_RESOLVED / _NOT_FOUND / _AMBIGUOUS
    account_identifier: Optional[str] = None # the identifier as typed ("ending 7890", "ACC000123")
    account_candidates: list[str] = field(default_factory=list)   # the refs an ambiguous one fits

    @property
    def has_preference(self) -> bool:
        return bool(self.same_account or self.bank_name or self.account_ref)

    @property
    def request_type(self) -> Optional[str]:
        """The ONE request the engine acts on — the most specific the note contains."""
        if self.account_requested:
            return REQUEST_EXACT_ACCOUNT
        if self.same_account:
            return REQUEST_SAME_ACCOUNT
        if self.bank_name:
            return REQUEST_BANK
        return None


def _identify_exact_account(
    note: str, accounts: list[AccountMaster],
) -> Optional[tuple[str, str, str, list[str]]]:
    """(match kind, identifier text, status, matching refs) for an exact-account identifier in the
    note, or None when the note contains none.

    Identified only from the platform's own canonical data — the reference number and the stored
    account number — and never by guessing: an identifier that fits no account is NOT_FOUND, and
    one that fits several (or two identifiers pointing at different accounts) is AMBIGUOUS. Neither
    is resolved to "the likeliest" account.
    """
    by_ref = {(a.reference_number or "").strip().upper(): a.reference_number for a in accounts}
    by_number: dict[str, list[str]] = {}
    for a in accounts:
        d = _digits(a.account_number)
        if d:
            by_number.setdefault(d, []).append(a.reference_number)

    found: list[tuple[str, str, set[str]]] = []      # (kind, identifier, matching refs)
    for m in _ACCOUNT_ID_RE.finditer(note):
        ident = m.group(1).upper()
        found.append((MATCH_ACCOUNT_ID, ident, {by_ref[ident]} if ident in by_ref else set()))
    tails: set[str] = set()
    for m in _ACCOUNT_ENDING_RE.finditer(note):
        tail = m.group(1) or m.group(2)
        tails.add(tail)
        refs = {ref for d, rs in by_number.items() if d.endswith(tail) for ref in rs}
        found.append((MATCH_ACCOUNT_ENDING, f"ending {tail}", refs))
    cued = {m.group(1) for m in _ACCOUNT_NUMBER_CUE_RE.finditer(note)}
    for run in sorted({m.group(0) for m in _DIGIT_RUN_RE.finditer(note)}):
        if run in tails:
            continue            # the digits of an "ending …" phrase, already handled
        refs = set(by_number.get(run, []))
        # A digit run that is no account number is only a request when the note presented it as
        # one ("account 1234567890"); otherwise it is a UTR, an amount or a phone number.
        if refs or run in cued:
            found.append((MATCH_ACCOUNT_NUMBER, run, refs))

    if not found:
        this = _THIS_ACCOUNT_RE.search(note)
        if this:
            return MATCH_UNSPECIFIED, this.group(0), ACCOUNT_NOT_FOUND, []
        return None

    every: set[str] = set().union(*(refs for _, _, refs in found))
    kind, ident = found[0][0], ", ".join(i for _, i, _ in found)
    if any(not refs for _, _, refs in found):
        # Something the note names does not exist. Alone, that is NOT_FOUND; beside another
        # identifier that does resolve, the note contradicts itself — AMBIGUOUS, never a pick.
        return kind, ident, (ACCOUNT_AMBIGUOUS if every else ACCOUNT_NOT_FOUND), sorted(every)
    if len(every) == 1:
        return kind, ident, ACCOUNT_RESOLVED, sorted(every)
    return kind, ident, ACCOUNT_AMBIGUOUS, sorted(every)


def parse_note(note: Optional[str], accounts: Iterable[AccountMaster], *,
               identifiers: bool = True) -> NoteRequest:
    """Read a merchant note against the real account catalogue.

    Matching is data-driven: the keys come from the Admin's own accounts — their reference numbers,
    account numbers, account names (e.g. "SINDU") and bank names — so an account or bank is
    recognised precisely when the platform has it. Nothing here raises.

    Most specific first: an exact account (by reference, full number, trailing digits or name)
    wins over a bank, and among names the longest key wins, so "Bank of India" is not mistaken for
    "Bank of Baroda" on the shared word "bank".

    ``identifiers=False`` skips the reference / number / trailing-digit identification and keeps
    the original first-match name lookup. That is the withdrawal engine's reading: digits in a
    withdrawal note usually describe the BENEFICIARY, not one of the platform's paying accounts.
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
    by_ref = {a.reference_number: a for a in accounts}

    def _resolve(ref: str, matched: str, kind: str) -> None:
        req.account_ref, req.matched_text = ref, matched
        req.account_requested, req.account_match = True, kind
        req.account_status, req.account_identifier = ACCOUNT_RESOLVED, matched
        req.account_candidates = [ref]
        if ref in by_ref:
            req.bank_name = by_ref[ref].bank_name

    def _unresolved(kind: str, ident: str, status: str, refs: list[str]) -> None:
        req.account_requested, req.account_match = True, kind
        req.account_status, req.account_identifier = status, ident
        req.account_candidates, req.matched_text = refs, ident

    if identifiers:
        # 1. An exact account by its canonical identifiers ("Use ACC000123", "ending 7890").
        hit = _identify_exact_account(note or "", accounts)
        if hit is not None:
            kind, ident, status, refs = hit
            if status == ACCOUNT_RESOLVED:
                _resolve(refs[0], ident, kind)
            else:
                _unresolved(kind, ident, status, refs)
            return req

        # 2. An exact account by name ("Use SINDU"). Two accounts answering to the same name is a
        # note that does not say which — handed to the Admin, never settled by picking one.
        acct_keys: dict[str, set[str]] = {}
        for acc in accounts:
            for key in _name_keys(acc.account_name):
                acct_keys.setdefault(key, set()).add(acc.reference_number)
        for key in sorted(acct_keys, key=len, reverse=True):
            if len(key) >= 3 and f" {key} " in padded and _is_request_for(key):
                refs = sorted(acct_keys[key])
                if len(refs) == 1:
                    _resolve(refs[0], key, MATCH_ACCOUNT_NAME)
                else:
                    _unresolved(MATCH_ACCOUNT_NAME, key, ACCOUNT_AMBIGUOUS, refs)
                return req
    else:
        name_keys: dict[str, str] = {}
        for acc in accounts:
            for key in _name_keys(acc.account_name):
                name_keys.setdefault(key, acc.reference_number)
        ref, matched = _find(name_keys)
        if ref:
            req.account_ref, req.matched_text = ref, matched
            if ref in by_ref:
                req.bank_name = by_ref[ref].bank_name
            return req

    # 3. The bank ("Use Bank of Baroda" — every Bank of Baroda account is then evaluated).
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


def same_bank(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two stored bank names are the same bank, ignoring case, spacing and punctuation
    ("HDFC Bank" and "HDFC BANK" are one bank)."""
    return bool(a and b) and _norm_text(a) == _norm_text(b)


# ═══ 3. Customer & account history ══════════════════════════════════════════════════════════════

@dataclass
class MemberHistory:
    """What the ledger already knows about this member. Everything here is derived from real
    deposit rows — there is no member master table on this platform, and no frontend flag is
    trusted (the request form's Profile OLD/NEW selector is the operator's opinion, not a fact)."""
    deposit_count: int = 0
    successful_deposits: int = 0     # the member's COMPLETED/DEPOSITED deposits (any sending account)
    accounts_used: list[str] = field(default_factory=list)     # most recently used first
    per_account_deposits: dict[str, int] = field(default_factory=dict)
    # How many deposits the platform has actually RECEIVED from the exact account this request is
    # being sent from. None when the request names no sending account (cash, crypto), which is the
    # only case where the older member-wide count is still used.
    #
    # Supplied by the caller from services/member_account — the platform's one definition of
    # "received", shared with the Profile the merchant and the Admin see. Computing it a second
    # time here is how the engine and the screen would come to disagree about the same account.
    sender_account_deposits: Optional[int] = None

    @property
    def is_new(self) -> bool:
        """Whether this is a NEW customer for allocation purposes.

        The question is about the SENDING ACCOUNT, not the person: one member can hold an account
        that has never funded us beside one that has, and they are not the same customer as far as
        placing this deposit is concerned. So when the sending account is known the answer comes
        from ITS received deposits, and only a request with no sending account at all falls back to
        the member-wide count.

        Note the two counts mean different things and both are kept. ``deposit_count`` is every
        deposit ROW for the member in any status — it still drives the account-history preference
        below, where "which of our accounts has served this member before" is the useful question
        and a cancelled request is still evidence. ``sender_account_deposits`` counts only money
        that ARRIVED, from one account, which is what NEW/OLD means.
        """
        if self.sender_account_deposits is not None:
            return self.sender_account_deposits == 0
        return self.deposit_count == 0

    @property
    def successful_count(self) -> int:
        """The customer's SUCCESSFUL deposits — what the Own Account rule counts.

        The MEMBER's total, across EVERY account they have sent from: 3 received from one account
        and 3 from another is 6. It is deliberately NOT the sending-account count NEW/OLD uses —
        a member who starts paying from a new account is still the same long-standing customer,
        so a new sending account must never reset their Own Account standing to zero.

        Received means COMPLETED or DEPOSITED (SUCCESSFUL_STATUSES, the platform's one definition);
        pending, account-requested, rejected, cancelled and every other state count for nothing.
        """
        return self.successful_deposits

    @property
    def prefers_own_account(self) -> bool:
        """The Own Account rule: from the customer's 6th successful deposit onward (MORE than
        OWN_ACCOUNT_THRESHOLD), an eligible Own Account is preferred. Exactly 5 is still normal."""
        return self.successful_count > OWN_ACCOUNT_THRESHOLD

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
        select(Transaction.admin_ref, Transaction.id, Transaction.status)
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
    # Only money that ARRIVED — the platform's one definition of a successful deposit.
    hist.successful_deposits = sum(1 for _ref, _id, st in rows if st in SUCCESSFUL_STATUSES)
    for ref, _id, _st in rows:
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


async def last_successful_account(
    db: AsyncSession, member_id: Optional[str], *, exclude_tx_id: Optional[int] = None,
) -> Optional[str]:
    """The account this member's most recent SUCCESSFUL deposit was received into — what "same
    account" / "last account" in a note refers to.

    Successful means the money arrived: COMPLETED or DEPOSITED, the platform's one definition of a
    received deposit (services/account_ledger._COMPLETED_DEPOSIT, shared with the member Profile).
    A pending, rejected or cancelled request never names the "last account" — the member never
    actually paid into it. Most recent by when it completed, then by row id.
    """
    mid = _norm_member(member_id)
    if not mid:
        return None
    stmt = (
        select(Transaction.admin_ref)
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            Transaction.status.in_(SUCCESSFUL_STATUSES),
            Transaction.admin_ref.isnot(None),
            func.upper(func.trim(func.coalesce(Transaction.member_id, ""))) == mid,
        )
        .order_by(func.coalesce(Transaction.admin_action_at, Transaction.created_at).desc(),
                  Transaction.id.desc())
        .limit(1)
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def customers_served(
    db: AsyncSession, refs: Sequence[str], *, exclude_member: Optional[str] = None,
) -> dict[str, int]:
    """{account reference → how many DISTINCT other customers it has served}.

    The measure a NEW customer is spread by. A customer is a Member ID; an account has served them
    once any deposit of theirs has been routed to it and not abandoned (every state except
    rejected/cancelled — an allocated deposit is already that customer's account, which is what
    keeps simultaneous new customers from all landing on the same one). ``exclude_member`` leaves
    out the customer being placed, so the count is of OTHER customers. Accounts that have served
    nobody are absent (read as 0).
    """
    refs = list(refs)
    if not refs:
        return {}
    member = func.upper(func.trim(Transaction.member_id))
    stmt = (
        select(Transaction.admin_ref, func.count(func.distinct(member)))
        .where(
            Transaction.type.in_(DEPOSIT_TYPES),
            Transaction.admin_ref.in_(refs),
            Transaction.status.notin_(RELEASED_STATUSES),
            Transaction.member_id.isnot(None),
            func.trim(Transaction.member_id) != "",
        )
        .group_by(Transaction.admin_ref)
    )
    mid = _norm_member(exclude_member)
    if mid:
        stmt = stmt.where(member != mid)
    return {ref: int(n or 0) for ref, n in (await db.execute(stmt)).all() if ref}


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
    served: int = 0               # distinct OTHER customers this account has served (NEW spread)

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
            # highestCredit is the PER-TRANSACTION cap; bankDailyCreditLimit is the cumulative daily
            # ceiling that remainingCapacity is measured against.
            "highestCredit": _money(self.account.highest_credit),
            "bankDailyCreditLimit": _money(self.account.bank_daily_credit_limit),
            "creditUsedToday": _money(self.used_today),
            "remainingCapacity": _money(self.remaining),
            "depositsToday": self.deposits_today,
            "memberDeposits": self.member_deposits,
            "customersServed": self.served,
            "unused": self.unused,
            "eligible": self.eligible,
            "rejectReason": self.reject_reason,
        }


def _evaluate(
    account: AccountMaster, amount: float, *, used_today: float, deposits_today: int,
    member_deposits: int, unused: bool, upi_id: Optional[str],
    sender_account_type: Optional[str] = None, deposit_type: Optional[str] = None,
) -> Candidate:
    """Apply every HARD rule to one account. Cheapest disqualification first.

    The two capacity tests are the whole point of the exercise and are written exactly as the rules
    read: one deposit must not exceed the PER-TRANSACTION Highest Credit, and projected daily usage
    must not exceed the CUMULATIVE daily bank credit limit. Every side is rounded to paise first, so
    an exact match is accepted and a one-paisa overshoot is not.
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

    # Payin Mode — an Admin who narrowed this account's receivable modes has excluded this type.
    # Unconfigured (NULL) accepts every mode, so this never disqualifies an account by default.
    if not supports_payin_mode(account, deposit_type):
        cand.reject_reason = REJECT_PAYIN_MODE
        return cand

    # Account type must match the account the money is coming FROM. Savings pays into Savings,
    # Current into Current — a hard rule, so it sits with the other disqualifications rather than
    # among the preference tiers, where a later pool could have overridden it.
    #
    # ``sender_account_type`` is None whenever the sending account's type is not known: a deposit
    # type that names no account at all (cash, crypto), or a request raised before the type was
    # recorded. Unknown is NOT treated as a mismatch — inventing a constraint from missing data
    # would send every such request to the Admin queue.
    if sender_account_type and _type_value(account.account_type) != sender_account_type:
        cand.reject_reason = REJECT_ACCOUNT_TYPE
        return cand

    # An account needs BOTH a per-transaction Highest Credit AND a daily bank credit limit to be
    # chosen. Either at 0 is "unconfigured" and has no capacity to give — never read as unlimited.
    if _money(account.highest_credit) <= 0 or _money(account.bank_daily_credit_limit) <= 0:
        cand.reject_reason = REJECT_NO_LIMIT
        return cand

    # PER-TRANSACTION limit — this single deposit may not exceed Highest Credit, regardless of how
    # much of the day's bank capacity remains.
    if _money(amount) > _money(account.highest_credit):
        cand.reject_reason = REJECT_OVER_PER_TXN
        return cand

    # CUMULATIVE daily limit — today's total plus this deposit may not exceed the daily bank limit.
    if _money(_money(used_today) + _money(amount)) > _money(account.bank_daily_credit_limit):
        cand.reject_reason = REJECT_NO_CAPACITY
        return cand

    cand.eligible = True
    return cand


async def evaluate_accounts(
    db: AsyncSession, amount: float, *, deposit_type: Optional[str] = None,
    member_id: Optional[str] = None, history: Optional[MemberHistory] = None,
    on: Optional[date] = None, exclude_tx_id: Optional[int] = None,
    sender_account_type: Optional[str] = None,
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
    served = await customers_served(db, refs, exclude_member=member_id)
    out = [
        _evaluate(
            a, amount,
            used_today=used.get(a.reference_number, 0.0),
            deposits_today=counts.get(a.reference_number, 0),
            member_deposits=history.per_account_deposits.get(a.reference_number, 0),
            unused=a.reference_number in unused,
            upi_id=upis.get(a.reference_number),
            sender_account_type=sender_account_type,
            deposit_type=deposit_type,
        )
        for a in accounts
    ]
    for cand in out:
        cand.served = served.get(cand.ref, 0)
    return out


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
    highest_credit: Optional[float] = None          # per-transaction cap
    bank_daily_credit_limit: Optional[float] = None # cumulative daily ceiling
    credit_used: Optional[float] = None
    remaining: Optional[float] = None               # remaining against the daily ceiling
    note: NoteRequest = field(default_factory=NoteRequest)
    requested_unavailable: bool = False          # the note named a bank/account nothing eligible matched
    candidates: list[Candidate] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def allocated(self) -> bool:
        return self.account is not None

    @property
    def failure_code(self) -> Optional[str]:
        """Why nothing was allocated (a FAIL_* code), or None on success."""
        return None if self.allocated else (self.detail or {}).get("failure")

    @property
    def request(self) -> dict:
        """What the note asked for and whether it was met (see allocate_deposit_account)."""
        return (self.detail or {}).get("request") or {}

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


def _customer_tiers(candidates: list[Candidate], history: MemberHistory,
                    *, deposit_type: Optional[str] = None,
                    is_new: bool = False) -> list[tuple[str, list[Candidate]]]:
    """The customer rules (NEW spread, or the existing-customer rules) as ORDERED pools over
    ``candidates``, most preferred first. Used on its own, and again — over the Own Account subset
    only — for the Own Account preference, so both rank by exactly the same rules."""
    is_upi = (deposit_type or "").upper() == UPI_DEPOSIT_TYPE

    # ── 4. A NEW customer is SPREAD across accounts ──
    # The eligible account that has served the FEWEST OTHER customers comes first. A distribution
    # rule: not "an account nobody has used", and not "the nearest remaining capacity" — an account
    # that has served 2 customers beats one that has served 10 whatever either has left today.
    # Ties are broken first by an account this member has ALREADY been given (placing them there
    # again adds no customer to any account, so it never works against the spread — and moving a
    # member between equally-loaded accounts is what left MM01 with a different account every
    # time), then by the ordinary ranking: a UPI request prefers an account with a linked UPI, then
    # nearest capacity, fewer deposits today, reference. Fully deterministic. The whole eligible
    # set is in the one ordered list, so if the first account's capacity is taken concurrently the
    # claim moves on to the next-least-served, not to some other rule.
    if is_new:
        theirs = set(history.accounts_used)
        ordered = sorted(candidates, key=lambda c: (
            c.served, 0 if c.ref in theirs else 1, 0 if (is_upi and c.upi_id) else 1,
            c.remaining, c.deposits_today, c.ref))
        return [(RULES.NEW_FEWEST_SERVED, ordered)]

    # ── 5 / 6. An EXISTING customer — the existing rules, unchanged ──
    tiers: list[tuple[str, list[Candidate]]] = []

    # A UPI request prefers an account that can actually be paid by UPI, when one is eligible.
    # A PREFERENCE, expressed as a pool — not a filter: if no eligible account has a linked UPI the
    # later pools still cover every eligible account and the merchant is sent bank details instead.
    if is_upi:
        with_upi = [c for c in candidates if c.upi_id]
        if with_upi and len(with_upi) < len(candidates):
            tiers.append((RULES.UPI_CAPABLE, rank(with_upi)))

    # An account already given to this Member ID comes first, while it can still take the amount:
    # they have those details saved. `candidates` is already the eligible set, so this never
    # overrides a limit. Ordered by remaining capacity, then by how recently it was used.
    order = {ref: i for i, ref in enumerate(history.accounts_used)}
    seen = [c for c in candidates if c.ref in order]
    if seen:
        seen = sorted(rank(seen), key=lambda c: (c.remaining, order.get(c.ref, 10**6)))
        tiers.append((RULES.OLD_ACCOUNT_HISTORY, seen))

    # Everything eligible, nearest capacity — the universal fallback that guarantees an eligible
    # account is never left unused just because no preference matched it.
    tiers.append((RULES.NEAREST_CAPACITY, rank(candidates)))
    return tiers


def _tiers(candidates: list[Candidate], history: MemberHistory, note: NoteRequest,
           *, deposit_type: Optional[str] = None, is_new: bool = False,
           own_preference: bool = False) -> list[tuple[str, list[Candidate], str]]:
    """The ordered preference pools for steps 4-7 of the priority, most preferred first, as
    ``(rule, ordered pool, detail)``. Every pool is already hard-filtered, so a rule can only ever
    change WHICH eligible account is picked — never whether an ineligible one becomes acceptable.
    The last pool always covers the whole eligible set, so a preference can narrow the choice but
    never empty it.

    Explicit requests in the note (exact account, same account, bank) are NOT handled here: they
    are decided before this runs and never fall through to it (see allocate_deposit_account). A
    bank request calls this with only that bank's eligible accounts, which is how the Own Account
    preference applies WITHIN the requested bank and never outside it.
    """
    base = [(rule, pool, "") for rule, pool in
            _customer_tiers(candidates, history, deposit_type=deposit_type, is_new=is_new)]
    if not own_preference:
        return base

    # ── 4. OWN ACCOUNT preference (the customer's 6th successful deposit onward) ──
    # The eligible Own Accounts come first, ranked among themselves by exactly the customer rules
    # above (the same NEW spread / UPI / account-history / nearest-capacity order) — never "the
    # first row". It is a PREFERENCE: `candidates` is already the eligible set, so an Own Account
    # that fails any limit is simply not here, and when no Own Account is eligible (or every one
    # loses its capacity to a concurrent request) the full eligible set follows unchanged, so a
    # non-own account that can take the money is used rather than the request going to the Admin.
    own = [c for c in candidates if c.account.is_own_account]
    if not own:
        return base
    own_tiers = [(RULES.OWN_ACCOUNT, pool, _RULE_TEXT.get(rule, rule)) for rule, pool in
                 _customer_tiers(own, history, deposit_type=deposit_type, is_new=is_new)]
    return own_tiers + base


def _why_not(cand: Candidate, amount: float, *, deposit_type: Optional[str],
             sender_account_type: Optional[str]) -> str:
    """One Admin-facing sentence for why this account cannot take this deposit. Internal — it
    names limits and capacity, so it only ever reaches the Admin (journal, Admin panel, audit)."""
    a = cand.account
    money = f"₹{_money(amount):,.2f}"
    r = cand.reject_reason
    if r == REJECT_INACTIVE:
        return f"it is {a.status or 'not active'}"
    if r == REJECT_PAYIN_MODE:
        return f"its Payin Mode does not include {deposit_type}"
    if r == REJECT_ACCOUNT_TYPE:
        return (f"it is a {_type_value(a.account_type)} and the member is sending from a "
                f"{sender_account_type}")
    if r == REJECT_NO_LIMIT:
        return "it has no Highest Credit and/or Bank Daily Credit Limit configured"
    if r == REJECT_OVER_PER_TXN:
        return (f"{money} is above its Highest Credit (per transaction) of "
                f"₹{_money(a.highest_credit):,.2f}")
    if r == REJECT_NO_CAPACITY:
        return (f"it has ₹{cand.remaining:,.2f} left of its ₹{_money(a.bank_daily_credit_limit):,.2f}"
                f" Bank Daily Credit Limit today (₹{cand.used_today:,.2f} used)")
    if r == REJECT_LOCKED:
        return "its remaining capacity was taken by a concurrent request"
    return r or "it is not eligible"


def _label(cand: Optional[Candidate], ref: Optional[str] = None) -> str:
    """How the Admin sees one account in a reason sentence."""
    if cand is None:
        return ref or "—"
    a = cand.account
    return f"{a.account_name} ({a.bank_name}, {a.reference_number})"


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
            # Both limits re-checked under the lock: the per-transaction cap cannot move, but the
            # daily figure can (another request may have consumed the headroom since the ranking).
            if _money(amount) > _money(locked.highest_credit):
                continue        # per-transaction cap (only reachable if the limit was edited down)
            used = (await credit_used_today(db, [cand.ref], on=on)).get(cand.ref, 0.0)
            if _money(_money(used) + _money(amount)) > _money(locked.bank_daily_credit_limit):
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
    # The member's SENDING account, as resolved by services/member_account:
    #   * its Savings/Current type — a hard filter on which managed accounts may be used;
    #   * how many deposits have been RECEIVED from it — which decides NEW vs OLD.
    # Both are None when the request names no sending account, and both rules then stand down
    # rather than guessing.
    sender_account_type: Optional[str] = None,
    sender_account_deposits: Optional[int] = None,
) -> AllocationResult:
    """Select the best eligible account for one deposit, or report that there is none.

    The caller supplies only the deposit's own facts. Every rule, every figure and the final
    choice are decided here, under a row lock, from data in the database.

    PRIORITY — the first that applies decides, and an EXPLICIT request (1-3) is either met exactly
    or refused; it never falls through to the rules below it:

      1. EXACT ACCOUNT the note names (reference, full number, unique trailing digits, name).
      2. SAME / LAST ACCOUNT — the account of the member's last SUCCESSFUL deposit.
      3. SPECIFIC BANK — the best eligible account AT that bank (by rules 4-6 within it).
      4. OWN ACCOUNT — from the MEMBER's 6th SUCCESSFUL deposit, counted across all their
         sending accounts (count > 5), an eligible Own Account first; when none is eligible,
         every other eligible account (a preference only).
      5. NEW customer — the eligible account that has served the fewest OTHER customers.
      6. EXISTING customer — the existing rules: UPI-capable (UPI deposits), an account already
         given to this member, then nearest remaining daily credit capacity.
      7. Nothing eligible, or an explicit request refused → no account (``failure_code`` says
         why); the caller puts the deposit in ACCOUNT_REQUESTED for an Admin.

    Every step chooses only among accounts that passed every HARD rule (availability, Payin Mode,
    account type, configured limits, Highest Credit per transaction, Bank Daily Credit Limit). A
    request can decide WHICH eligible account is used; it can never make an ineligible one usable.

    Nothing is written by this function except the lock it holds: assigning the account to the
    deposit is the caller's job, inside the caller's transaction, so the allocation and the
    deposit row land together or not at all.
    """
    amount = _money(amount)
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id))).scalars().all()
    parsed = parse_note(note, accounts)
    history = await member_history(db, member_id, exclude_tx_id=exclude_tx_id)
    # NEW/OLD is about the SENDING ACCOUNT when there is one. Attached before `is_new` is read.
    history.sender_account_deposits = sender_account_deposits
    customer_type = "NEW" if history.is_new else "OLD"

    result = AllocationResult(customer_type=customer_type, note=parsed)
    result.detail = {
        "customerType": customer_type,
        "memberDepositCount": history.deposit_count,
        # The figure NEW/OLD was actually decided on, and the account it belongs to, so the journal
        # explains the classification rather than leaving it to be re-derived later.
        "senderAccountDeposits": sender_account_deposits,
        "senderAccountType": sender_account_type,
        # The count the Own Account rule reads (successful deposits only), and whether it is on.
        "successfulDeposits": history.successful_count,
        "ownAccountPreference": history.prefers_own_account,
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
        exclude_tx_id=exclude_tx_id, sender_account_type=sender_account_type)
    result.candidates = candidates
    eligible = [c for c in candidates if c.eligible]
    result.detail["accounts"] = [c.snapshot() for c in candidates]

    is_upi_request = (deposit_type or "").upper() == UPI_DEPOSIT_TYPE
    by_ref = {c.ref: c for c in candidates}
    money = f"₹{amount:,.2f}"
    request = parsed.request_type
    why = lambda c: _why_not(c, amount, deposit_type=deposit_type,  # noqa: E731
                             sender_account_type=sender_account_type)

    # ── The Own Account preference (step 4) — recorded whether or not it is active, so the Admin
    # can see if it was involved and why an Own Account was or was not used. Internal only.
    own_preference = history.prefers_own_account
    own_all = [c for c in candidates if c.account.is_own_account]
    result.detail["ownAccount"] = {
        "successfulDeposits": history.successful_count,
        "threshold": OWN_ACCOUNT_THRESHOLD,
        "triggered": own_preference,
        "candidates": len(own_all),
        "eligible": sum(1 for c in own_all if c.eligible),
        "fallbackEligible": sum(1 for c in eligible if not c.account.is_own_account),
        "accounts": [{
            "accountRef": c.ref, "accountName": c.account.account_name,
            "bankName": c.account.bank_name, "eligible": c.eligible,
            "rejectReason": c.reject_reason, "why": None if c.eligible else why(c),
        } for c in own_all],
        "selectedIsOwn": None,
    }

    # What the note asked for, recorded whether or not it is met — the Admin panel, the audit trail
    # and the Telegram alert all read it from here.
    result.detail["request"] = {
        "type": request,
        "matchedText": parsed.matched_text,
        "match": parsed.account_match,
        "identifier": parsed.account_identifier,
        "accountStatus": parsed.account_status,
        "candidates": parsed.account_candidates,
        "accountRef": parsed.account_ref,
        "bankName": parsed.bank_name,
        "sameAccountRef": None,
        "fulfilled": None,
    }

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
        result.bank_daily_credit_limit = _money(cand.account.bank_daily_credit_limit)
        result.credit_used = _money(cand.used_today)
        result.remaining = _money(cand.remaining)
        result.detail["selected"] = cand.snapshot()
        result.detail["ownAccount"]["selectedIsOwn"] = bool(cand.account.is_own_account)
        if request:
            result.detail["request"]["fulfilled"] = True
        return result

    def _refuse(code: str, sentence: str) -> AllocationResult:
        """An EXPLICIT request that cannot be met automatically. Nothing is allocated — the deposit
        goes to the Admin with this reason — and nothing is substituted for what was asked."""
        result.requested_unavailable = True
        result.detail["failure"] = code
        result.detail["request"]["fulfilled"] = False
        result.reason = sentence
        return result

    async def _take(target: Candidate, rule: str, fail_code: str, asked: str) -> AllocationResult:
        """Honour a request for ONE specific account, or refuse it. Never another account."""
        result.detail["request"]["account"] = target.snapshot()
        if not target.eligible:
            return _refuse(fail_code, f"The note asks for {asked}, {_label(target)}, and it cannot "
                                      f"take {money}: {why(target)}.")
        claimed, _ = await _claim(db, [target], amount, on=on)
        if claimed:
            return _finish(claimed, rule)
        target.reject_reason = REJECT_LOCKED
        return _refuse(FAIL_RACE, f"The note asks for {asked}, {_label(target)}, and its remaining "
                                  f"capacity was taken by a concurrent request before {money} "
                                  f"could be placed.")

    # ── 1. EXACT ACCOUNT ── ("Use ACC000123", "account ending 7890", "Use SINDU")
    if request == REQUEST_EXACT_ACCOUNT:
        ident = parsed.account_identifier or parsed.matched_text or "an account"
        if parsed.account_status == ACCOUNT_AMBIGUOUS:
            refs = ", ".join(_label(by_ref.get(r), r) for r in parsed.account_candidates)
            return _refuse(FAIL_REQUESTED_ACCOUNT_AMBIGUOUS, (
                f"The note asks for one specific account (\"{ident}\") but does not identify which: "
                f"it fits {len(parsed.account_candidates)} accounts — {refs}. Confirm the account "
                f"with the merchant and assign it manually."))
        target = by_ref.get(parsed.account_ref) if parsed.account_status == ACCOUNT_RESOLVED else None
        if target is None:
            if parsed.account_match == MATCH_UNSPECIFIED:
                sentence = (f"The note asks for a specific account (\"{ident}\") but does not say "
                            f"which. Confirm the account with the merchant and assign it manually.")
            else:
                sentence = (f"The note asks for account \"{ident}\", which does not exist in "
                            f"Account Management. Confirm the account with the merchant and assign "
                            f"it manually.")
            return _refuse(FAIL_REQUESTED_ACCOUNT_NOT_FOUND, sentence)
        return await _take(target, RULES.EXACT_ACCOUNT, FAIL_REQUESTED_ACCOUNT_NOT_ELIGIBLE,
                           "a specific account")

    # ── 2. SAME / LAST ACCOUNT ── the account the member's last SUCCESSFUL deposit was received into
    if request == REQUEST_SAME_ACCOUNT:
        last = await last_successful_account(db, member_id, exclude_tx_id=exclude_tx_id)
        result.detail["request"]["sameAccountRef"] = last
        if not last:
            result.detail["sameAccountRejected"] = {"accountRef": None, "reason": "NO_PREVIOUS_ACCOUNT"}
            return _refuse(FAIL_NO_PREVIOUS_ACCOUNT, (
                "The note asks for the same / last account, but this member has no previous "
                "successful deposit, so there is no account to reuse. Assign an account manually."))
        target = by_ref.get(last)
        if target is None:
            result.detail["sameAccountRejected"] = {"accountRef": last, "reason": "ACCOUNT_NO_LONGER_EXISTS"}
            return _refuse(FAIL_SAME_ACCOUNT_NOT_ELIGIBLE, (
                f"The note asks for the same / last account, {last}, which no longer exists in "
                f"Account Management. Assign an account manually."))
        out = await _take(target, RULES.SAME_ACCOUNT, FAIL_SAME_ACCOUNT_NOT_ELIGIBLE,
                          "the same / last account")
        if not out.allocated:
            result.detail["sameAccountRejected"] = {
                "accountRef": last, "reason": target.reject_reason or REJECT_LOCKED}
        return out

    # ── 3. SPECIFIC BANK ── every account at that bank is evaluated; no other bank is considered.
    if request == REQUEST_BANK:
        bank = parsed.bank_name
        at_bank = [c for c in candidates if same_bank(c.account.bank_name, bank)]
        result.detail["request"]["bankAccounts"] = [c.snapshot() for c in at_bank]
        pool = [c for c in at_bank if c.eligible]
        if not pool:
            reasons = "; ".join(f"{_label(c)} — {why(c)}" for c in at_bank) or "it has no accounts"
            result.detail["requestedBankUnavailable"] = bank
            return _refuse(FAIL_REQUESTED_BANK_NOT_ELIGIBLE, (
                f"The note asks for {bank}, and none of its {len(at_bank)} account(s) can take "
                f"{money}: {reasons}. Another bank was NOT substituted."))
        for rule, ordered, sub in _tiers(pool, history, parsed, deposit_type=deposit_type,
                                         is_new=history.is_new, own_preference=own_preference):
            claimed, skipped = await _claim(db, ordered, amount, on=on)
            if skipped:
                result.detail.setdefault("concurrentlyLocked", []).extend(skipped)
            if claimed:
                why = _RULE_TEXT.get(rule, rule) + (f" · {sub}" if sub else "")
                return _finish(claimed, RULES.NOTE_BANK, f"requested bank {bank} · {why}")
        return _refuse(FAIL_RACE, (
            f"The note asks for {bank}, and the remaining capacity of its eligible account(s) was "
            f"taken by concurrent requests before {money} could be placed. Another bank was NOT "
            f"substituted."))

    # ── No explicit request: nothing eligible at all ──
    if not eligible:
        code, sentence = _failure(candidates, amount)
        result.detail["failure"] = code
        if own_preference and own_all:
            # Say that the Own Account preference was in play and why none of them could be used —
            # an Admin who configured Own Accounts for exactly this customer looks there first.
            sentence = (f"{sentence} Own Account preference applied ({history.successful_count} "
                        f"successful deposits): none of the {len(own_all)} Own Account(s) can take "
                        f"it — " + "; ".join(f"{_label(c)} — {why(c)}" for c in own_all) + ".")
        result.reason = sentence
        return result

    # ── 4. >5 successful deposits → an eligible Own Account first (then everything eligible);
    # ── 5 / 6. NEW customer → fewest other customers served; EXISTING → the existing rules ──
    for rule, ordered, sub in _tiers(eligible, history, parsed, deposit_type=deposit_type,
                                     is_new=history.is_new, own_preference=own_preference):
        claimed, skipped = await _claim(db, ordered, amount, on=on)
        if skipped:
            result.detail.setdefault("concurrentlyLocked", []).extend(skipped)
        if claimed:
            return _finish(claimed, rule, sub)

    # Every eligible account lost its capacity to a concurrent request between the read and the
    # lock. No account is assigned — the deposit stays unallocated rather than breaching a limit.
    result.reason = (f"No account could be reserved for {money} — the remaining capacity was "
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
    """API shape for one journal row (camelCase, matching the rest of the API).

    ADMIN-ONLY. Besides the decision it carries what the Admin needs to act on a request the engine
    could not place: the failure code, what the note asked for (type, identifier, requested
    account / bank / last account, whether it was met) and the requested account's own limit
    position at the moment of the decision.
    """
    try:
        detail = json.loads(row.detail) if row.detail else {}
    except (ValueError, TypeError):
        detail = {}
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
        "failure": detail.get("failure"),
        "request": detail.get("request"),
        # Whether the Own Account preference (>5 successful deposits) was in play, and each Own
        # Account's position — so the Admin can see why one was or was not used.
        "ownAccount": detail.get("ownAccount"),
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
        "createdAtIst": row.created_at_ist,
    }
