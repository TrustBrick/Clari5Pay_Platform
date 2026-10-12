"""Per-session JWT revocation store (SEC — logout revocation).

A session token carries a unique ``jti``. Listing that ``jti`` in ``revoked_tokens`` revokes ONLY
that token, so a normal logout ends the current device's session and leaves the user's other
sessions untouched — unlike ``token_version``, which revokes every token at once (logout-all,
password change/reset).

All helpers are no-ops / safe when a token has no ``jti`` (pre-feature tokens), so existing
sessions keep working. No token value, secret, or sensitive claim is logged here.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import RevokedToken


async def is_jti_revoked(db: AsyncSession, jti: str | None) -> bool:
    """True when this exact token has been revoked. A missing ``jti`` (legacy token) is never
    considered revoked — it simply cannot be listed."""
    if not jti:
        return False
    row = (await db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti))).first()
    return row is not None


async def revoke_token(db: AsyncSession, *, jti: str | None, user_id: int | None,
                       expires_at: datetime) -> bool:
    """Revoke one token by ``jti``. Idempotent: a repeated logout of the same token is a no-op and
    returns False (already revoked / nothing to do); a first revocation returns True. Does not
    commit — the caller's request transaction does."""
    if not jti:
        return False
    exists = (await db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti))).first()
    if exists is not None:
        return False
    db.add(RevokedToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await db.flush()
    return True


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete revocation rows whose token has already expired (the token is invalid on its own by
    then, so the row is no longer needed). Returns the number removed. Safe to run repeatedly."""
    now = now or datetime.utcnow()
    res = await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    return res.rowcount or 0
