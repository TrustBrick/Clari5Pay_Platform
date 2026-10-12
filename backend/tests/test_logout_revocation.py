"""Per-session logout revocation (SEC) — jti denylist.

Normal `POST /api/auth/logout` now revokes ONLY the presented token (by its `jti`), ending the
current session server-side while leaving the user's other sessions valid. logout-all and
password change/reset keep revoking everything via token_version — the two mechanisms are
independent. Backward compatible: tokens with no `jti` (minted before this feature) cannot be
individually revoked and keep working until they expire.

Everything runs against in-memory SQLite with mocked/test data — no real credentials, no network,
no live financial operations. The HTTP app is seeded in a lifespan so the aiosqlite connection is
created on the TestClient's own event loop (a StaticPool shares that one in-memory DB).

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_logout_revocation.py -v
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.routes import auth as auth_routes
from app.api.routes import support as support_routes
from app.core import revocation
from app.core.config import settings
from app.core.deps import get_db
from app.core.security import (
    TOKEN_ID_CLAIM, TOKEN_VERSION_CLAIM, create_access_token, decode_token, token_jti,
)
from app.db.session import Base
from app.models.models import RevokedToken, User, UserRole


def _sa_user() -> User:
    u = User(id=1, username="superadmin", role=UserRole.SUPER_ADMIN,
             email="sa@example.com", name="Demo SA", active=True,
             hashed_password="x")  # non-null placeholder; never a real hash, never exposed by /me
    u.token_version = 0
    return u


def _mem_engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ── Unit tests: the revocation store ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revocation_store_roundtrip_and_idempotent():
    engine = _mem_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    exp = datetime.utcnow() + timedelta(hours=1)
    async with Session() as db:
        assert await revocation.is_jti_revoked(db, "abc") is False
        assert await revocation.revoke_token(db, jti="abc", user_id=1, expires_at=exp) is True
        assert await revocation.is_jti_revoked(db, "abc") is True
        # repeated revoke of the same jti is a safe no-op (repeated logout)
        assert await revocation.revoke_token(db, jti="abc", user_id=1, expires_at=exp) is False
        # a missing jti is never revocable and never recorded
        assert await revocation.revoke_token(db, jti=None, user_id=1, expires_at=exp) is False
        assert await revocation.is_jti_revoked(db, None) is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_purge_expired_removes_only_expired():
    engine = _mem_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with Session() as db:
        await revocation.revoke_token(db, jti="old", user_id=1, expires_at=now - timedelta(minutes=1))
        await revocation.revoke_token(db, jti="new", user_id=1, expires_at=now + timedelta(hours=1))
        removed = await revocation.purge_expired(db, now=now)
        assert removed == 1
        assert await revocation.is_jti_revoked(db, "old") is False   # expired → purged
        assert await revocation.is_jti_revoked(db, "new") is True    # still live
    await engine.dispose()


# ── Token shape ──────────────────────────────────────────────────────────────────────────────

def test_session_token_carries_a_jti():
    payload = decode_token(auth_routes._issue_session_token(_sa_user()))
    assert token_jti(payload), "every session token must carry a unique jti for revocation"


def test_two_tokens_get_distinct_jtis():
    a = token_jti(decode_token(auth_routes._issue_session_token(_sa_user())))
    b = token_jti(decode_token(auth_routes._issue_session_token(_sa_user())))
    assert a and b and a != b


# ── HTTP end to end: /logout revokes the current token ───────────────────────────────────────

def _http_client():
    engine = _mem_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(_sa_user())
            await db.commit()
        yield
        await engine.dispose()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(auth_routes.router)

    async def _get_db():
        async with Session() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
            finally:
                await s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _legacy_token(uid: int = 1, ver: int = 0) -> str:
    """A pre-feature token: valid signature + version, but NO jti claim."""
    claims = {"sub": str(uid), TOKEN_VERSION_CLAIM: ver,
              "exp": datetime.utcnow() + timedelta(hours=1)}
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def test_logout_revokes_current_token_only():
    with _http_client() as client:
        tok1 = auth_routes._issue_session_token(_sa_user())
        tok2 = auth_routes._issue_session_token(_sa_user())   # a second, independent session

        # both tokens work before logout
        assert client.get("/api/auth/me", headers=_auth(tok1)).status_code == 200
        assert client.get("/api/auth/me", headers=_auth(tok2)).status_code == 200

        # log out session 1
        assert client.post("/api/auth/logout", headers=_auth(tok1)).status_code == 200

        # session 1's token is now rejected; session 2 is untouched
        assert client.get("/api/auth/me", headers=_auth(tok1)).status_code == 401
        assert client.get("/api/auth/me", headers=_auth(tok2)).status_code == 200


def test_repeated_logout_is_safe():
    """Logging the same token out twice is safe: the first succeeds (200); the second is cleanly
    rejected with 401 (the token is already revoked, so it no longer authenticates anywhere) —
    never a 500 or a duplicate-row error. Store-level idempotency is covered by the unit test."""
    with _http_client() as client:
        tok = auth_routes._issue_session_token(_sa_user())
        assert client.post("/api/auth/logout", headers=_auth(tok)).status_code == 200
        # the now-revoked token is uniformly rejected — including by /logout itself
        assert client.post("/api/auth/logout", headers=_auth(tok)).status_code == 401
        assert client.get("/api/auth/me", headers=_auth(tok)).status_code == 401


def test_expired_token_rejected_on_protected_route():
    with _http_client() as client:
        expired = create_access_token({"sub": "1", TOKEN_VERSION_CLAIM: 0},
                                      expires_delta=timedelta(minutes=-5))
        assert client.get("/api/auth/me", headers=_auth(expired)).status_code == 401


def test_legacy_token_without_jti_still_works_and_logout_is_graceful():
    """A token minted before this feature (no jti) must keep working, and logging it out must not
    error — it simply records the audit event without an individually-revocable id."""
    with _http_client() as client:
        legacy = _legacy_token()
        assert token_jti(decode_token(legacy)) is None
        assert client.get("/api/auth/me", headers=_auth(legacy)).status_code == 200
        assert client.post("/api/auth/logout", headers=_auth(legacy)).status_code == 200
        # no jti → cannot be individually revoked → still valid (ends via expiry / logout-all)
        assert client.get("/api/auth/me", headers=_auth(legacy)).status_code == 200


# ── Independence from token_version (logout-all / password change) ───────────────────────────

def test_jti_revocation_and_token_version_are_independent():
    with _http_client() as client:
        tok = auth_routes._issue_session_token(_sa_user())
        # revoke by jti (single-session logout) rejects it...
        assert client.post("/api/auth/logout", headers=_auth(tok)).status_code == 200
        assert client.get("/api/auth/me", headers=_auth(tok)).status_code == 401
        # ...and a brand-new session for the same user (new jti, same version) is unaffected,
        # proving logout did NOT bump token_version (which would have logged out everyone).
        fresh = auth_routes._issue_session_token(_sa_user())
        assert client.get("/api/auth/me", headers=_auth(fresh)).status_code == 200


# ── WebSocket path performs the same revocation check ────────────────────────────────────────

def test_websocket_auth_checks_revocation():
    """Guards against regression: if the support WebSocket stops consulting the denylist, a
    logged-out token could still open a chat socket though every REST call rejects it."""
    src = inspect.getsource(support_routes.support_ws)
    assert "is_jti_revoked" in src, "support WebSocket must reject a revoked (logged-out) token"
    assert "token_version_matches" in src, "support WebSocket must keep the token_version check"
