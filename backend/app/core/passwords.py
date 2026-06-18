"""
Shared password-change helpers: complexity enforcement, reuse prevention
(last N passwords) and history bookkeeping. Used by every flow that sets a
password (self change-password, profile edit, forgot-password reset, and the
Super Admin reset).
"""
from fastapi import HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import User, PasswordHistory
from app.core.security import get_password_hash, verify_password, password_policy_error

# Number of previous passwords that may not be reused.
PASSWORD_HISTORY_LIMIT = 5


async def assert_password_allowed(db: AsyncSession, user: User, new_password: str) -> None:
    """Raise HTTPException(400) if the new password fails complexity or reuses a recent one."""
    err = password_policy_error(new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Compare against the current password and the recent history.
    recent = (await db.execute(
        select(PasswordHistory.hashed_password)
        .where(PasswordHistory.user_id == user.id)
        .order_by(PasswordHistory.id.desc())
        .limit(PASSWORD_HISTORY_LIMIT)
    )).scalars().all()
    for old_hash in [user.hashed_password, *recent]:
        if old_hash and verify_password(new_password, old_hash):
            raise HTTPException(
                status_code=400,
                detail=f"You cannot reuse any of your last {PASSWORD_HISTORY_LIMIT} passwords.",
            )


async def set_password(db: AsyncSession, user: User, new_password: str) -> None:
    """Record the outgoing password in history, set the new hash, and trim history to the limit.

    Call ``assert_password_allowed`` first to validate.
    """
    # Keep the password being replaced so it counts toward the no-reuse window.
    if user.hashed_password:
        db.add(PasswordHistory(user_id=user.id, hashed_password=user.hashed_password))
    user.hashed_password = get_password_hash(new_password)
    await db.flush()

    # Trim to the most recent PASSWORD_HISTORY_LIMIT entries.
    keep_ids = (await db.execute(
        select(PasswordHistory.id)
        .where(PasswordHistory.user_id == user.id)
        .order_by(PasswordHistory.id.desc())
        .limit(PASSWORD_HISTORY_LIMIT)
    )).scalars().all()
    if keep_ids:
        await db.execute(
            delete(PasswordHistory).where(
                PasswordHistory.user_id == user.id,
                PasswordHistory.id.notin_(keep_ids),
            )
        )
