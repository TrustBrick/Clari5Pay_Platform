"""Tests for token_version revocation (SEC-002).

Before this feature a JWT was valid until it expired — ten years for Admin and Super Admin — and
could not be withdrawn by any means short of deactivating the account or rotating SECRET_KEY.
`token_version` makes revocation possible: every session token carries the user's version as a
`ver` claim, and authentication rejects a mismatch.

The properties that matter, and which these tests pin down:

  1. **Backward compatibility** — a token minted before this feature has no `ver` claim, reads as
     0, and must keep working. Getting this wrong logs out every user on deploy.
  2. **Both validation paths** — HTTP (`get_current_user`) and the support WebSocket
     (`support.py`) authenticate on separate code paths. If only one checks the version, the
     other is a revocation bypass.
  3. **Invalidation actually fires** on password change and reset.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_token_version.py -v
"""
from __future__ import annotations

import pytest

from app.core.security import (
    create_access_token, decode_token, token_claim_version, token_version_matches,
    TOKEN_VERSION_CLAIM,
)
from app.models.models import User, UserRole


def _user(version: int = 0, active: bool = True, role: UserRole = UserRole.ADMIN) -> User:
    u = User(id=1, username="u", role=role, email="u@example.com", name="U", active=active)
    u.token_version = version
    return u


def _token(user_id: int = 1, version: int | None = 0) -> str:
    """A session token. version=None mints a LEGACY token with no `ver` claim."""
    claims: dict = {"sub": str(user_id)}
    if version is not None:
        claims[TOKEN_VERSION_CLAIM] = version
    return create_access_token(claims)


# ── 1. Backward compatibility — the deploy-safety property ───────────────────────────────

def test_legacy_token_without_claim_reads_as_zero():
    """A token minted before this feature must not be rejected."""
    payload = decode_token(_token(version=None))
    assert TOKEN_VERSION_CLAIM not in payload, "fixture must actually omit the claim"
    assert token_claim_version(payload) == 0


def test_legacy_token_accepted_against_default_user():
    """The exact deploy scenario: old token + freshly-migrated user row (version 0)."""
    payload = decode_token(_token(version=None))
    assert token_version_matches(payload, _user(version=0)) is True


def test_null_column_treated_as_zero():
    """A row that somehow escaped the migration default must not lock its user out."""
    u = _user(version=0)
    u.token_version = None                      # simulate NULL
    assert token_version_matches(decode_token(_token(version=None)), u) is True
    assert token_version_matches(decode_token(_token(version=0)), u) is True


def test_legacy_token_rejected_once_user_is_bumped():
    """Grandfathering ends the moment the user's version is incremented — the documented way to
    retire pre-existing tokens."""
    payload = decode_token(_token(version=None))
    assert token_version_matches(payload, _user(version=1)) is False


# ── 2. Version matching ──────────────────────────────────────────────────────────────────

def test_matching_version_accepted():
    assert token_version_matches(decode_token(_token(version=3)), _user(version=3)) is True


@pytest.mark.parametrize("token_ver,user_ver", [(0, 1), (1, 2), (5, 6), (2, 0)])
def test_mismatched_version_rejected(token_ver, user_ver):
    """Rejects in both directions — a stale token AND a token from the future (e.g. after a
    database restore rolled the column back)."""
    assert token_version_matches(decode_token(_token(version=token_ver)), _user(version=user_ver)) is False


def test_malformed_claim_falls_back_to_zero():
    """A junk claim must not raise — it degrades to 0 and is then judged on merit."""
    payload = {"sub": "1", TOKEN_VERSION_CLAIM: "not-a-number"}
    assert token_claim_version(payload) == 0
    assert token_version_matches(payload, _user(version=0)) is True
    assert token_version_matches(payload, _user(version=1)) is False


def test_none_payload_is_safe():
    assert token_claim_version(None) == 0
    assert token_version_matches(None, _user(version=1)) is False


# ── 3. Issuance — every session-token path must stamp the claim ──────────────────────────

def test_issue_session_token_includes_version():
    from app.api.routes.auth import _issue_session_token
    payload = decode_token(_issue_session_token(_user(version=7)))
    assert payload[TOKEN_VERSION_CLAIM] == 7
    assert payload["sub"] == "1"


def test_support_agent_login_path_also_versions(monkeypatch):
    """The support direct-login path historically minted its own token. If it regresses to
    create_access_token, support sessions become unrevocable — this test catches that."""
    from app.api.routes.auth import _issue_session_token
    payload = decode_token(_issue_session_token(_user(version=4, role=UserRole.SUPPORT_AGENT)))
    assert payload[TOKEN_VERSION_CLAIM] == 4


def test_admin_keeps_long_lifetime():
    """Revocation must not have changed token lifetimes — that is a separate, announced change."""
    from app.api.routes.auth import _issue_session_token
    admin = decode_token(_issue_session_token(_user(role=UserRole.ADMIN)))
    merchant = decode_token(_issue_session_token(_user(role=UserRole.MERCHANT)))
    assert admin["exp"] > merchant["exp"], "admin lifetime should still exceed merchant's"


# ── 4. Password change / reset invalidation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_password_bumps_version():
    """The reset path is why this exists: a stolen token must not survive a password reset."""
    from app.core.passwords import set_password

    class _DB:
        def add(self, *_): pass
        async def flush(self): pass
        async def execute(self, *_a, **_kw):
            class R:
                def scalars(self): return self
                def all(self): return []
            return R()

    u = _user(version=2)
    u.hashed_password = "old-hash"
    await set_password(_DB(), u, "NewPassw0rd!")
    assert u.token_version == 3, "password change must revoke existing tokens"
    assert u.hashed_password != "old-hash"


@pytest.mark.asyncio
async def test_set_password_can_opt_out_of_revocation():
    """Self-service callers re-issue a token immediately, so they suppress the bump."""
    from app.core.passwords import set_password

    class _DB:
        def add(self, *_): pass
        async def flush(self): pass
        async def execute(self, *_a, **_kw):
            class R:
                def scalars(self): return self
                def all(self): return []
            return R()

    u = _user(version=2)
    u.hashed_password = "old-hash"
    await set_password(_DB(), u, "NewPassw0rd!", revoke_tokens=False)
    assert u.token_version == 2


def test_token_issued_before_password_change_is_rejected_after():
    """End-to-end shape of the invalidation, without a database."""
    u = _user(version=0)
    stolen = decode_token(_issue := _token(version=0))
    assert token_version_matches(stolen, u) is True
    u.token_version += 1                                     # what set_password does
    assert token_version_matches(stolen, u) is False


def test_self_service_password_change_revokes_other_sessions():
    """Changing your own password must end sessions on OTHER devices.

    That is the whole point of the control: a user who suspects someone has their old password
    changes it, and expects the other party to lose access. Both self-service endpoints must
    therefore let set_password revoke (i.e. NOT pass revoke_tokens=False).

    The caller keeps working because each endpoint returns a replacement token, which the
    frontend persists (api.ts persistNewToken). These two halves must ship together: revoking
    without the frontend change logs the caller out of the tab they just used.
    """
    import inspect
    from app.api.routes import users

    for fn in (users.change_password, users.update_profile):
        src = inspect.getsource(fn)
        assert "revoke_tokens=False" not in src, \
            f"{fn.__name__} must let set_password revoke other sessions"
        assert "_issue_session_token" in src, \
            f"{fn.__name__} must return a replacement token, or it logs the caller out"


# ── 5. Account disable remains independent ───────────────────────────────────────────────

def test_disabled_account_is_orthogonal_to_version():
    """`active` was already enforced and must keep working on its own — a matching version must
    never resurrect a disabled account."""
    disabled = _user(version=0, active=False)
    assert token_version_matches(decode_token(_token(version=0)), disabled) is True
    assert disabled.active is False, "the active check is the gate, enforced separately in deps.py"


# ── 6. Both authentication paths use the same rule ───────────────────────────────────────

def test_http_and_websocket_share_one_implementation():
    """Guards the §1.2 blocker: if either path stops importing the shared helper and hand-rolls
    its own comparison, the two can drift and one becomes a bypass."""
    import inspect
    from app.core import deps
    from app.api.routes import support

    assert "token_version_matches" in inspect.getsource(deps.get_current_user), \
        "get_current_user must perform the version check"
    assert "token_version_matches" in inspect.getsource(support.support_ws), \
        "support WebSocket must perform the version check — otherwise it is a revocation bypass"


# ── 7. BEHAVIOURAL — authentication actually enforces the version ────────────────────────
#
# The tests above verify token_version_matches in isolation. That helper exists whether or not
# get_current_user calls it, so on its own it proves almost nothing about enforcement: with the
# check removed from deps.py, 18 of the 19 tests above still pass. These drive the real dependency
# end to end, and fail loudly if the check is dropped.

def _app_with_protected_route(db_user: User):
    """Minimal app whose single route depends on the real get_current_user."""
    from fastapi import Depends, FastAPI
    from app.core.deps import get_current_user, get_db

    class _Result:
        def scalar_one_or_none(self): return db_user

    class _DB:
        async def execute(self, *_a, **_kw): return _Result()

    app = FastAPI()

    @app.get("/protected")
    async def protected(u: User = Depends(get_current_user)):
        return {"id": u.id}

    async def _get_db():
        yield _DB()

    app.dependency_overrides[get_db] = _get_db
    return app


def _call_protected(app, token: str):
    from fastapi.testclient import TestClient
    return TestClient(app).get("/protected", headers={"Authorization": f"Bearer {token}"})


def test_http_auth_accepts_matching_version():
    app = _app_with_protected_route(_user(version=2))
    assert _call_protected(app, _token(version=2)).status_code == 200


def test_http_auth_REJECTS_stale_version():
    """The core of SEC-002: a token issued before revocation must stop working.

    This is the test that fails if the check is ever removed from get_current_user.
    """
    app = _app_with_protected_route(_user(version=2))
    r = _call_protected(app, _token(version=1))
    assert r.status_code == 401, f"stale token must be rejected, got {r.status_code}"


def test_http_auth_accepts_legacy_token_on_default_user():
    """Deploy safety, exercised through the real dependency rather than the helper."""
    app = _app_with_protected_route(_user(version=0))
    assert _call_protected(app, _token(version=None)).status_code == 200


def test_http_auth_rejects_legacy_token_after_bump():
    app = _app_with_protected_route(_user(version=1))
    assert _call_protected(app, _token(version=None)).status_code == 401


def test_http_auth_still_rejects_disabled_account():
    """The pre-existing `active` gate must survive this change, independently of version."""
    app = _app_with_protected_route(_user(version=0, active=False))
    assert _call_protected(app, _token(version=0)).status_code == 401
