"""Focused read-only verification of /api/auth/me identity + token acceptance/rejection.

No live server, no real token, no credentials. Drives the REAL `me` handler and the REAL
`get_current_user` dependency through FastAPI's TestClient with a mocked DB session (the pattern
used by test_token_version.py), so these are code-level proofs of the deployed behaviour:

  1. a valid SUPER_ADMIN token → /api/auth/me returns that user's identity and role SUPER_ADMIN;
  2. an invalid (garbage) token → 401;
  3. an expired token → 401;
  4. logout semantics — a stateless JWT stays valid while the user's token_version is unchanged
     (which is all a normal /logout does: it does NOT bump the version), and is rejected only once
     the version is incremented (what /logout-all / a password change do).

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_auth_me_identity.py -v
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import me
from app.core.deps import get_current_user, get_db
from app.core.security import create_access_token, TOKEN_VERSION_CLAIM
from app.models.models import User, UserRole


def _sa(version: int = 0, active: bool = True) -> User:
    u = User(id=7, username="superadmin", role=UserRole.SUPER_ADMIN,
             email="sa@example.com", name="Demo SA", active=active)
    u.token_version = version
    return u


def _app(db_user: User) -> FastAPI:
    """Minimal app mounting the REAL /api/auth/me handler over the REAL get_current_user."""
    class _Result:
        def scalar_one_or_none(self):
            return db_user

    class _DB:
        async def execute(self, *_a, **_kw):
            return _Result()

    app = FastAPI()
    app.get("/api/auth/me")(me)

    async def _get_db():
        yield _DB()

    app.dependency_overrides[get_db] = _get_db
    return app


def _token(user_id: int = 7, version: int | None = 0, expires_delta=None) -> str:
    claims: dict = {"sub": str(user_id)}
    if version is not None:
        claims[TOKEN_VERSION_CLAIM] = version
    return create_access_token(claims, expires_delta=expires_delta)


def _get_me(app: FastAPI, token: str):
    return TestClient(app).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


# ── 1. Valid SUPER_ADMIN token → correct identity + role ─────────────────────────────────────

def test_me_returns_identity_and_superadmin_role():
    r = _get_me(_app(_sa()), _token())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 7
    assert body["username"] == "superadmin"
    assert body["name"] == "Demo SA"
    assert body["role"] == "SUPER_ADMIN"          # UserRole is a str-enum → serializes to its value
    assert body["active"] is True


# ── 2. Invalid token → 401 ───────────────────────────────────────────────────────────────────

def test_invalid_token_rejected():
    r = _get_me(_app(_sa()), "not.a.valid.jwt")
    assert r.status_code == 401


def test_missing_token_rejected():
    r = TestClient(_app(_sa())).get("/api/auth/me")
    assert r.status_code == 401


# ── 3. Expired token → 401 ───────────────────────────────────────────────────────────────────

def test_expired_token_rejected():
    expired = _token(expires_delta=timedelta(minutes=-5))   # exp already in the past
    r = _get_me(_app(_sa()), expired)
    assert r.status_code == 401


# ── 4. Logout semantics (token_version) ──────────────────────────────────────────────────────

def test_token_valid_while_version_unchanged_then_rejected_after_bump():
    """A normal /logout does NOT change token_version, so the same token keeps validating — proof
    that a stateless JWT survives a normal logout. Incrementing token_version (what /logout-all and
    a password change do) is what actually rejects it."""
    token = _token(version=0)
    # token_version unchanged (the state after a plain /logout) → still accepted
    assert _get_me(_app(_sa(version=0)), token).status_code == 200
    # token_version incremented (the state after /logout-all) → now rejected
    assert _get_me(_app(_sa(version=1)), token).status_code == 401
