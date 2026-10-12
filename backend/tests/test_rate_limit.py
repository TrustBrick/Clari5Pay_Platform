"""Rate-limiting tests (SEC).

Exercises the shared limiter without a real Redis (a tiny in-memory fake stands in for the
fixed-window counter) and without any network. Covers:

  * per-user keying — two users do not share a bucket;
  * enforcement — the (limit+1)-th request in a window is rejected with HTTP 429 + Retry-After;
  * the authorization boundary — a missing token falls back to an IP key and NEVER turns the
    limiter into an auth gate (401 stays the job of the endpoint's own auth dependency);
  * FAIL-OPEN — if the Redis backend errors, requests are allowed (a payment platform must not
    lock everyone out when the limiter is down).

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python -m pytest tests/test_rate_limit.py -v
"""
from __future__ import annotations

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import ratelimit
from app.core.ratelimit import rate_limit_user, rate_limit
from app.core.security import create_access_token


class _FakeRedis:
    """Minimal async stand-in for the fixed-window counter: incr / expire / ttl."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return 30


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(ratelimit, "_client", _client)
    return fake


def _app_user_limited(limit: int):
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(rate_limit_user(limit, 60, "test-scope"))])
    async def x():
        return {"ok": True}

    return app


def _bearer(sub: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': sub})}"}


def test_allows_up_to_limit_then_429(fake_redis):
    client = TestClient(_app_user_limited(3))
    hdr = _bearer("42")
    assert [client.get("/x", headers=hdr).status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get("/x", headers=hdr)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_two_users_have_separate_buckets(fake_redis):
    client = TestClient(_app_user_limited(1))
    assert client.get("/x", headers=_bearer("1")).status_code == 200
    # User 1 is now over the limit…
    assert client.get("/x", headers=_bearer("1")).status_code == 429
    # …but user 2 is unaffected.
    assert client.get("/x", headers=_bearer("2")).status_code == 200


def test_missing_token_is_not_rejected_by_the_limiter(fake_redis):
    """No Authorization header → the limiter falls back to an IP key and still returns 200 (not
    401). Authorization is the endpoint's own dependency's job, never the limiter's."""
    client = TestClient(_app_user_limited(5))
    assert client.get("/x").status_code == 200


def test_fail_open_when_backend_errors(monkeypatch):
    """If Redis is unreachable, requests are ALLOWED rather than everyone being locked out."""
    async def _broken_client():
        raise RuntimeError("redis down")

    monkeypatch.setattr(ratelimit, "_client", _broken_client)
    client = TestClient(_app_user_limited(1))
    hdr = _bearer("99")
    # Well past the limit, all allowed because the backend fails open.
    assert all(client.get("/x", headers=hdr).status_code == 200 for _ in range(5))


def test_ip_based_rate_limit_also_enforced(fake_redis):
    """The IP-keyed variant (used for unauthenticated login/OTP) still works end to end."""
    app = FastAPI()

    @app.get("/y", dependencies=[Depends(rate_limit(2, 60, "ip-scope"))])
    async def y():
        return {"ok": True}

    client = TestClient(app)
    assert [client.get("/y").status_code for _ in range(2)] == [200, 200]
    assert client.get("/y").status_code == 429
