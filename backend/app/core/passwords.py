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


async def set_password(db: AsyncSession, user: User, new_password: str,
                       *, revoke_tokens: bool = True) -> None:
    """Record the outgoing password in history, set the new hash, and trim history to the limit.

    Call ``assert_password_allowed`` first to validate.

    ``revoke_tokens`` (default True) bumps ``user.token_version``, invalidating every access token
    previously issued to this user. This matters most on the RESET path: a password reset is what
    someone does after suspecting compromise, and without revocation an attacker holding a stolen
    token keeps access straight through it — at the exact moment the user believes they have shut
    the attacker out. See SECURITY_REVIEW.md SEC-002.

    Revocation lives here, rather than at each call site, so a future caller cannot forget it.
    Pass ``revoke_tokens=False`` only where the caller re-issues a token immediately (a user
    changing their own password should not be logged out by doing so).
    """
    # Keep the password being replaced so it counts toward the no-reuse window.
    if user.hashed_password:
        db.add(PasswordHistory(user_id=user.id, hashed_password=user.hashed_password))
    user.hashed_password = get_password_hash(new_password)
    if revoke_tokens:
        user.token_version = int(user.token_version or 0) + 1
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
