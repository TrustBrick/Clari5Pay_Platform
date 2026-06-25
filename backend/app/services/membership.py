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
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, User, UserRole

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


async def lookup_member_name(db: AsyncSession, user: User, member_id: str | None) -> str | None:
    """Latest Member Name on record for this Membership ID within the merchant's business
    pool, or None if the Membership ID has never been used (i.e. a new member)."""
    mid = normalize_member_id(member_id)
    if not mid:
        return None
    ids = await _business_member_ids(db, user)
    if not ids:
        return None
    return (await db.execute(
        select(Transaction.member_name).where(
            Transaction.merchant_id.in_(ids),
            Transaction.member_id == mid,
            Transaction.member_name.is_not(None),
            Transaction.member_name != "",
        ).order_by(Transaction.id.desc()).limit(1)
    )).scalar_one_or_none()


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
