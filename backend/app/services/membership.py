"""Shared Membership lookup & validation (Change 10).

Memberships are derived from transaction history: the latest Member Name stored
against a Membership ID — within a merchant's business pool (all merchant users
sharing the same business name) — is the canonical name for that ID. This module
is the single source for that lookup plus the "one name per Membership ID" guard,
reused by the deposit / withdrawal / settlement create flows and the member-profile
auto-fill endpoint so the logic lives in exactly one place.

Capture rule (used by every transaction type):
  * Existing Membership ID  -> keep the on-record Member Name (authoritative; the
    form shows it read-only). A conflicting entered name is rejected (no duplicate /
    conflicting membership).
  * New Membership ID        -> take the manually entered Member Name. Storing it on
    the transaction "creates" the membership, so it auto-fills on every future
    transaction for the same merchant business.
Membership IDs are matched case-insensitively and trimmed.

BOTH LEDGERS COUNT. There is no member master table on this platform — a membership
exists precisely because some transaction records it — and the platform keeps two
separate ledgers: `transactions` (Merchant module) and `agent_transaction` (the
isolated Agent module). A membership recorded in either one is the same membership,
so a name is looked up across both and the most recently recorded wins. Without
this, a member onboarded in the Merchant module looked brand new inside the Agent
module (and vice versa), and the operator had to retype a name the platform already
knew — which is how one Membership ID ended up with two spellings.

Scope is the merchant BUSINESS either way: the merchant ledger scopes by the ids of
every merchant user sharing the business name, the agent ledger by that same name in
`merchant_business`. Reading the agent ledger here does not move money or figures
between the modules — only the member's identity is shared.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AgentTransaction, Transaction, User, UserRole

MEMBER_NAME_MISMATCH_MSG = "This Membership ID is already associated with another member."


def normalize_member_id(member_id: str | None) -> str | None:
    """Trim + uppercase a Membership ID (case- and space-insensitive). '' -> None."""
    if not member_id:
        return None
    mid = member_id.strip().upper()
    return mid or None


async def _business_member_ids(db: AsyncSession, user: User) -> list[int]:
    """All merchant-user ids sharing this user's business name (one shared member pool)."""
    return (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all()


async def _latest_merchant_name(db: AsyncSession, user: User, mid: str) -> tuple[str | None, datetime | None]:
    """Newest (name, when) for this Membership ID in the MERCHANT ledger."""
    ids = await _business_member_ids(db, user)
    if not ids:
        return None, None
    row = (await db.execute(
        select(Transaction.member_name, Transaction.created_at).where(
            Transaction.merchant_id.in_(ids),
            func.upper(func.trim(Transaction.member_id)) == mid,
            Transaction.member_name.is_not(None),
            Transaction.member_name != "",
        ).order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(1)
    )).first()
    return (row[0], row[1]) if row else (None, None)


async def _latest_agent_name(db: AsyncSession, user: User, mid: str) -> tuple[str | None, datetime | None]:
    """Newest (name, when) for this Membership ID in the AGENT ledger.

    The agent ledger stores its membership id as typed, so the comparison is upper/trimmed on both
    sides rather than relying on the stored form.
    """
    row = (await db.execute(
        select(AgentTransaction.membership_name, AgentTransaction.created_at).where(
            AgentTransaction.merchant_business == user.name,
            func.upper(func.trim(AgentTransaction.membership_id)) == mid,
            AgentTransaction.membership_name.is_not(None),
            AgentTransaction.membership_name != "",
        ).order_by(AgentTransaction.created_at.desc(), AgentTransaction.id.desc()).limit(1)
    )).first()
    return (row[0], row[1]) if row else (None, None)


async def lookup_member_name(db: AsyncSession, user: User, member_id: str | None) -> str | None:
    """Latest Member Name on record for this Membership ID anywhere in the business, or None if
    the Membership ID has never been used (i.e. a genuinely new member).

    Both ledgers are consulted and the most recently recorded name wins, so the Merchant module and
    the Agent module always answer this question identically. A row with no timestamp (older data)
    loses to one that has a timestamp, and falls back to whichever ledger produced a name at all.
    """
    mid = normalize_member_id(member_id)
    if not mid:
        return None
    m_name, m_at = await _latest_merchant_name(db, user, mid)
    a_name, a_at = await _latest_agent_name(db, user, mid)
    if m_name and a_name:
        return a_name if (a_at or datetime.min) > (m_at or datetime.min) else m_name
    return m_name or a_name


async def resolve_member_name(db: AsyncSession, user: User,
                              member_id: str | None, member_name: str | None) -> str | None:
    """Decide the Member Name to permanently store on a new transaction.

    Existing Membership ID -> the on-record name (authoritative); a different entered
    name raises 400. New Membership ID -> the trimmed entered name. Returns None when no
    Membership ID is supplied (e.g. an optional settlement member)."""
    mid = normalize_member_id(member_id)
    entered = (member_name or "").strip() or None
    if not mid:
        return entered
    existing = await lookup_member_name(db, user, mid)
    if existing:
        if entered and entered.casefold() != existing.strip().casefold():
            raise HTTPException(status_code=400, detail=MEMBER_NAME_MISMATCH_MSG)
        return existing      # existing membership keeps its authoritative name
    return entered           # new membership: the entered name (stored = auto-created)
