"""A member's SENDING ACCOUNT: what kind it is, and whether it has ever funded us.

THE ONE PLACE BOTH QUESTIONS ARE ANSWERED
─────────────────────────────────────────
Two facts travel with every deposit and withdrawal a merchant raises against a member's account:

  * **Account type** — SAVINGS or CURRENT. Business metadata the merchant supplies ONCE, the
    first time an account is seen, and which is remembered from then on.
  * **Profile** — NEW or OLD. NOT metadata. It is a *derived fact* about that account's history,
    and it is computed here, from the database, on every request.

Keeping them in one module is deliberate: both hang off the same question, "which saved account is
this?", and answering that question two different ways is how duplicate account rows and
contradictory Admin screens get created.

WHAT "OLD" MEANS, EXACTLY
─────────────────────────
An account is OLD when the platform has **actually received money from it at least once**:

    a DEPOSIT from this exact account, in a status the rest of the platform already treats as
    "the money arrived" — ``_COMPLETED_DEPOSIT`` = (COMPLETED, DEPOSITED)

and NEW when it has not. That is the whole rule. It is deliberately NOT:

  * the member's age, registration date, or Membership ID
  * the account row's own ``created_at``
  * how many transactions of any kind exist
  * withdrawal history — money going OUT tells you nothing about an account having funded us
  * a deposit that was merely *raised*: pending, rejected, cancelled and failed requests all leave
    the account NEW, because no money was received

The status tuple is IMPORTED from the ledger rather than re-listed here. A second local definition
of "successful deposit" would drift from the one the balances use, and then the Admin screen and
the money would disagree about the same account.

PER ACCOUNT, NOT PER MEMBER
───────────────────────────
One member can hold several accounts with different standings — ``satish@ybl`` that has never paid
us is NEW on the same day ``satish@okaxis`` is OLD. So every function here keys on the ACCOUNT, and
the member id is only ever a scoping filter.

IDENTITY
────────
Two spellings of the same account must resolve to one row, or the merchant is asked for the account
type again and a duplicate is saved. Normalisation reuses what the platform already does rather
than inventing a rival scheme:

  * bank accounts — :func:`app.services.withdrawal_allocation._norm_account_number`, the same
    canonical form the payout engine matches beneficiaries with (case, spaces and dashes removed)
  * UPI ids — trimmed and lower-cased, the platform's usual case-insensitive treatment

Scope is the whole BUSINESS (every merchant user sharing a name), matching how saved UPIs are
already looked up — not the single user who happened to type it, which would let two operators at
the same merchant each create their own copy of one account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import MerchantBankAccount, Transaction, TxType, User, UserRole
from app.services.account_ledger import _COMPLETED_DEPOSIT
from app.services.withdrawal_allocation import _norm_account_number

# ── Account type ────────────────────────────────────────────────────────────────────────────────
SAVINGS = "SAVINGS"
CURRENT = "CURRENT"
ACCOUNT_TYPES = (SAVINGS, CURRENT)

ACCOUNT_TYPE_LABELS = {SAVINGS: "Savings Account", CURRENT: "Current Account"}

# ── Profile ─────────────────────────────────────────────────────────────────────────────────────
PROFILE_NEW = "NEW"
PROFILE_OLD = "OLD"

# Deposits only. A withdrawal is money leaving; it can never make a sending account OLD.
_DEPOSIT_TYPES = (TxType.DEPOSIT, TxType.DEPOSIT_REQUEST)


def normalize_account_type(value: Optional[str]) -> Optional[str]:
    """SAVINGS / CURRENT from whatever the caller sent, or None when nothing was sent.

    Accepts the display forms the UI shows ("Savings Account") as well as the stored ones, so a
    payload echoed back from a screen is never rejected for cosmetic reasons. Anything else is
    None — the caller decides whether that is "not supplied" or an error, because those two cases
    need different answers and this function cannot tell them apart.
    """
    raw = (value or "").strip().upper().replace(" ACCOUNT", "").replace("-", "").replace("_", "")
    if raw in ("SAVING", "SAVINGS"):
        return SAVINGS
    if raw in ("CURRENT", "CURRENTACCOUNT"):
        return CURRENT
    return None


def normalize_upi(value: Optional[str]) -> Optional[str]:
    """The canonical form of a UPI id: trimmed and lower-cased.

    UPI handles are not case sensitive in practice, so ``Satish@YBL`` and ``satish@ybl`` are one
    account. Treating them as two is what would ask the merchant for the account type twice and
    leave two rows behind.
    """
    v = (value or "").strip().lower()
    return v or None


def normalize_account_number(value: Optional[str]) -> Optional[str]:
    """The payout engine's canonical account number, reused verbatim."""
    v = _norm_account_number(value)
    return v or None


@dataclass
class AccountIdentity:
    """Which saved account a request is talking about, in canonical form."""
    member_id: Optional[str] = None
    upi_id: Optional[str] = None            # normalised
    account_number: Optional[str] = None    # normalised

    @property
    def is_resolvable(self) -> bool:
        """Whether there is enough here to name one account at all."""
        return bool(self.member_id and (self.upi_id or self.account_number))


def identity_from(
    *, member_id: Optional[str], upi_id: Optional[str] = None,
    account_number: Optional[str] = None,
) -> AccountIdentity:
    return AccountIdentity(
        member_id=(member_id or "").strip() or None,
        upi_id=normalize_upi(upi_id),
        account_number=normalize_account_number(account_number),
    )


@dataclass
class AccountView:
    """Everything the merchant form and the Admin screen need about one sending account."""
    account: Optional[MerchantBankAccount]
    account_type: Optional[str]        # SAVINGS / CURRENT / None when never recorded
    profile: str                       # NEW / OLD — always decided here, never accepted
    successful_deposits: int
    exists: bool                       # a saved row was found
    needs_account_type: bool           # the merchant must be asked (new account, or legacy NULL)

    @property
    def account_type_label(self) -> Optional[str]:
        return ACCOUNT_TYPE_LABELS.get(self.account_type or "")

    def as_dict(self) -> dict:
        return {
            "accountId": self.account.id if self.account else None,
            "exists": self.exists,
            "accountType": self.account_type,
            "accountTypeLabel": self.account_type_label,
            "needsAccountType": self.needs_account_type,
            "profile": self.profile,
            "successfulDeposits": self.successful_deposits,
            "bankName": self.account.bank_name if self.account else None,
            "branch": self.account.branch if self.account else None,
            "accountHolder": self.account.account_holder if self.account else None,
            "accountNumber": self.account.account_number if self.account else None,
            "upiId": self.account.upi_id if self.account else None,
        }


async def business_ids(db: AsyncSession, user: User) -> list[int]:
    """Every merchant user sharing this business name — the scope a saved account lives in."""
    return list((await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all())


def _matches(ident: AccountIdentity, upi: Optional[str], number: Optional[str]) -> bool:
    """Whether a stored (upi, account number) pair is the account this identity names.

    Comparison happens in PYTHON, against values normalised by the same functions that built the
    identity. Doing it in SQL would need ``regexp_replace``, which PostgreSQL has and SQLite does
    not — the exact shape of divergence that once let a whole suite pass while the query was
    unusable on the real database. The candidate set is one member's saved accounts, so there is
    nothing to gain by pushing it down.
    """
    if ident.upi_id and normalize_upi(upi) == ident.upi_id:
        return True
    if ident.account_number and normalize_account_number(number) == ident.account_number:
        return True
    return False


async def find_account(
    db: AsyncSession, ids: Sequence[int], ident: AccountIdentity,
) -> Optional[MerchantBankAccount]:
    """The saved row this identity refers to, or None.

    Matched case- and format-insensitively so the same account typed differently resolves to one
    row. A UPI match and an account-number match are both accepted, because a saved row may carry
    either or both. The oldest match wins, so re-using an account never prefers a later duplicate.
    """
    if not ids or not ident.is_resolvable:
        return None
    rows = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids),
            MerchantBankAccount.member_id == ident.member_id,
        ).order_by(MerchantBankAccount.id.asc())
    )).scalars().all()
    for row in rows:
        if _matches(ident, row.upi_id, row.account_number):
            return row
    return None


async def successful_deposit_count(
    db: AsyncSession, ids: Sequence[int], ident: AccountIdentity,
    *, exclude_tx_id: Optional[int] = None,
) -> int:
    """How many deposits this exact account has actually funded.

    Counts transactions, not saved-account rows: an account is OLD because money arrived from it,
    and that record lives on the deposit. ``exclude_tx_id`` lets a request ask about its own
    history without counting itself, which is what the Admin detail view needs when it re-derives
    the standing of a deposit that has since completed.
    """
    if not ids or not ident.is_resolvable:
        return 0
    # Narrowed in SQL on the cheap, index-friendly predicates; the account identity itself is
    # compared in Python for the portability reason explained on :func:`_matches`. What comes back
    # is one member's RECEIVED deposits, which is a handful of rows.
    stmt = select(Transaction.id, Transaction.sender_upi_id, Transaction.account_number).where(
        Transaction.merchant_id.in_(ids),
        Transaction.member_id == ident.member_id,
        Transaction.type.in_(_DEPOSIT_TYPES),
        Transaction.status.in_(_COMPLETED_DEPOSIT),   # the platform's ONE definition of received
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)
    rows = (await db.execute(stmt)).all()
    return sum(1 for _id, upi, number in rows if _matches(ident, upi, number))


async def describe(
    db: AsyncSession, user: User, ident: AccountIdentity,
    *, exclude_tx_id: Optional[int] = None,
) -> AccountView:
    """The authoritative view of one sending account. Read-only.

    This is what both the merchant form and the request handlers ask, so the value shown while
    typing and the value stored on submit come from the same calculation.
    """
    if not ident.is_resolvable:
        return AccountView(None, None, PROFILE_NEW, 0, False, True)
    ids = await business_ids(db, user)
    acc = await find_account(db, ids, ident)
    n = await successful_deposit_count(db, ids, ident, exclude_tx_id=exclude_tx_id)
    stored = normalize_account_type(acc.account_type) if acc else None
    return AccountView(
        account=acc,
        account_type=stored,
        # NEW/OLD is decided here, from the count, every time. A caller cannot pass it in.
        profile=PROFILE_OLD if n > 0 else PROFILE_NEW,
        successful_deposits=n,
        exists=acc is not None,
        # Asked when the account is new, and asked ONCE more for a legacy row saved before this
        # field existed. Never guessed: an invented Savings/Current would read to an Admin exactly
        # like one a person chose.
        needs_account_type=stored is None,
    )


async def remember_account_type(
    db: AsyncSession, account: MerchantBankAccount, account_type: str,
) -> None:
    """Write the account type onto a saved account that has none.

    Deliberately one-way. An account that already carries a type is left alone, so re-using it can
    never rewrite what was recorded the first time, and a stale form cannot overwrite a correction
    made elsewhere. Changing a recorded type is a separate, explicit act — not a side effect of
    raising a payment.
    """
    if account is None:
        return
    if normalize_account_type(account.account_type) is not None:
        return
    normalised = normalize_account_type(account_type)
    if normalised is None:
        return
    account.account_type = normalised
    await db.flush()
