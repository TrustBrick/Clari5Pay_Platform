"""Regression tests for SEC-001 — POST /api/auth/otp-config must be Super Admin only.

This endpoint switches OFF the second authentication factor for every user on the platform.
It shipped unguarded on 2026-06-18 (f87b3239) as a "testing aid" and was reachable
unauthenticated from the public internet until 2026-07-19 — confirmed live, and the production
setting was found switched OFF. See SECURITY_REVIEW.md SEC-001.

These tests exist so that regression is caught mechanically rather than by a future audit.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_auth_otp_config.py -v
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.core.deps import get_current_user, get_db
from app.models.models import User, UserRole


# ── fixtures ──────────────────────────────────────────────────────────────────────────────

def _user(role: UserRole, username: str) -> User:
    u = User(id=1, username=username, role=role, email=f"{username}@example.com", name=username)
    return u


class _FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Minimal stand-in: records what the handler would have written, without a database.

    Only the surface `otp_config` actually touches is implemented — enough to prove the
    authorization decision and that the audit calls still fire.
    """

    def __init__(self):
        self.added = []
        self.committed = False

    async def execute(self, *_a, **_kw):
        return _FakeResult(None)          # no existing AppSetting row

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def flush(self):
        pass

    async def refresh(self, *_a, **_kw):
        pass


@pytest.fixture
def app_and_db():
    """A minimal app exposing only the auth router, with the DB dependency stubbed."""
    app = FastAPI()
    app.include_router(auth_routes.router)
    db = _FakeDB()

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return app, db


def _as(app: FastAPI, user: User | None):
    """Override the identity the request authenticates as. None = unauthenticated."""
    if user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        async def _cu():
            return user
        app.dependency_overrides[get_current_user] = _cu


# ── the three cases the fix must satisfy ─────────────────────────────────────────────────

def test_unauthenticated_is_rejected(app_and_db):
    """No credentials → must NOT reach the handler.

    This is the exact request that succeeded in production before the fix.
    """
    app, db = app_and_db
    _as(app, None)
    r = TestClient(app).post("/api/auth/otp-config", json={"enabled": False})
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"
    assert db.added == [], "handler must not have run"
    assert db.committed is False


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MERCHANT, UserRole.SUPPORT_AGENT])
def test_non_super_admin_is_forbidden(app_and_db, role):
    """Authenticated but insufficient — including ADMIN, which is otherwise highly privileged."""
    app, db = app_and_db
    _as(app, _user(role, f"user_{role.value.lower()}"))
    r = TestClient(app).post("/api/auth/otp-config", json={"enabled": False})
    assert r.status_code == 403, f"{role.value} expected 403, got {r.status_code}"
    assert db.added == [], "handler must not have run"


def test_super_admin_succeeds_and_preserves_behaviour(app_and_db, monkeypatch):
    """Super Admin still works exactly as before — the fix restricts access, not function."""
    app, db = app_and_db
    sa = _user(UserRole.SUPER_ADMIN, "superadmin")
    _as(app, sa)

    audited: dict = {}
    logged: dict = {}

    async def _fake_record_audit(_db, action_type, **kw):
        audited.update(action_type=action_type, **kw)

    async def _fake_log_event(_db, event_type, message, *a, **kw):
        logged.update(event_type=event_type, message=message)

    monkeypatch.setattr(auth_routes, "record_audit", _fake_record_audit)
    monkeypatch.setattr(auth_routes, "log_event", _fake_log_event)

    r = TestClient(app).post("/api/auth/otp-config", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False}, "response contract must be unchanged"

    # Audit logging must still fire — it is the only forensic trail on this control.
    assert audited.get("action_type") == "OTP_CONFIG"
    assert audited.get("new") == "disabled"
    assert logged.get("event_type") == "OTP_CONFIG"

    # And it must now name the actor. Every pre-fix row says "system" because the endpoint
    # had no caller identity to record.
    assert audited.get("actor") is sa, "audit row must attribute the change to the caller"


def test_enabling_is_also_restricted(app_and_db):
    """Both directions are privileged — re-enabling matters as much as disabling."""
    app, db = app_and_db
    _as(app, _user(UserRole.ADMIN, "admin1"))
    r = TestClient(app).post("/api/auth/otp-config", json={"enabled": True})
    assert r.status_code == 403


def test_otp_status_remains_public(app_and_db):
    """GET /otp-status is intentionally public — the login page needs it before authenticating.

    Guards against an over-correction that breaks login by locking this down too.
    """
    app, _ = app_and_db
    _as(app, None)
    r = TestClient(app).get("/api/auth/otp-status")
    assert r.status_code == 200
    assert "enabled" in r.json()
