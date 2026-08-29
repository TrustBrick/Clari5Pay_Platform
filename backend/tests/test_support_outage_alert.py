"""Tests for the Support Availability → Telegram outage alert (services/support_alerts).

The alert exists to tell the Super Admin that merchants can no longer reach anybody. Its whole
value is in *when* it fires, so these pin down the state machine rather than the message text:

  1. **Availability drives it, nothing else** — the alert reads the existing two-pool summary
     (eligible Customer Support OR eligible Admin on duty); it never re-decides availability and
     nobody is hardcoded online or offline.
  2. **One alert per outage** — it fires on the AVAILABLE → UNAVAILABLE transition, never on the
     unavailable state, so polling, refreshes, several merchants and several workers cannot
     multiply it. Recovery re-arms it; the next outage alerts again.
  3. **Recovery is silent** — UNAVAILABLE → AVAILABLE is recorded, never notified.
  4. **Telegram can fail freely** — a dead bot must leave availability, the audit trail and the
     latch exactly as correct as a working one.
  5. **Credentials stay server-side** — nothing the merchant endpoint returns carries the bot
     token or a chat id.

Run from the backend directory:

    python -m pytest tests/test_support_outage_alert.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import Base
from app.models.models import (
    SupportAvailabilityEvent,
    SupportAvailabilityState,
    SupportConfig,
    SystemLog,
    User,
    UserRole,
    UserSession,
    WhatsAppLog,
)
from app.services import support_alerts, support_routing, whatsapp as wa

NOW = datetime(2026, 8, 28, 12, 0, 0)

TEST_CHAT_ID = "999000111"          # safe test destination — never a real Super Admin


@pytest_asyncio.fixture
async def factory(tmp_path):
    """An isolated database, handed to the service as its session factory.

    ``support_alerts`` deliberately opens its OWN short sessions (the latch must commit
    independently of whatever request triggered it), so the test injects the factory rather than a
    session — the same seam production uses, just pointed somewhere harmless.

    On disk with NullPool rather than the usual ``:memory:``, because ``:memory:`` hands every
    session the SAME connection: concurrent transactions would then share one, and the "twenty
    merchants poll at once" test would be measuring the pool instead of the latch.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alerts.db'}",
                                 poolclass=NullPool, connect_args={"timeout": 30})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with Session() as s:
        s.add(SupportConfig(id=1, max_active_conversations=2, strategy="LEAST_ACTIVE"))
        await s.commit()
    yield Session
    await engine.dispose()


@pytest.fixture(autouse=True)
def telegram(monkeypatch):
    """A configured bot pointed at a safe test chat, with every send captured instead of sent.

    Nothing in this file can reach the Telegram API or a real Super Admin: the transport itself is
    replaced, and the recipient is the override chat id.
    """
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str):
        sent.append((chat_id, text))
        return True, f"msg{len(sent)}", "200 ok"

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(settings, "SUPPORT_ALERT_TELEGRAM_CHAT_ID", TEST_CHAT_ID, raising=False)
    monkeypatch.setattr(wa, "_send_telegram", fake_send)
    return sent


# ── helpers ────────────────────────────────────────────────────────────────────────────────────

def summary(*, support_available=0, admin_available=0) -> dict:
    """A summary in the shape ``availability_summary`` returns. Availability is its OR, exactly as
    the merchant header renders it."""
    return {
        "available": (support_available + admin_available) > 0,
        "availableAgents": support_available + admin_available,
        "onlineAgents": support_available + admin_available,
        "totalAgents": 4,
        "support": {"available": support_available, "online": support_available, "total": 2},
        "admin": {"available": admin_available, "online": admin_available, "total": 2},
    }


UP = summary(support_available=1)
DOWN = summary()


async def observe(factory, s: dict, *, at: datetime = NOW):
    return await support_alerts.observe(s, now=at, session_factory=factory)


async def events(factory) -> list[SupportAvailabilityEvent]:
    async with factory() as db:
        return list((await db.execute(
            select(SupportAvailabilityEvent).order_by(SupportAvailabilityEvent.id)
        )).scalars().all())


async def latch(factory) -> str | None:
    async with factory() as db:
        return (await db.execute(
            select(SupportAvailabilityState.state).where(SupportAvailabilityState.id == 1)
        )).scalar_one_or_none()


# ── 1. Availability drives the alert — the existing rule, unchanged ─────────────────────────────
#
# Cases 1-5 of the brief: either pool alone keeps support up; only both being empty takes it down.
# The availability rule itself lives in support_routing (and is pinned by test_support_availability
# against real users, sessions and capacity) — what matters here is that the state machine consumes
# that answer verbatim and never forms its own.

@pytest.mark.asyncio
@pytest.mark.parametrize("support_available, admin_available, expected_state", [
    (0, 1, support_alerts.AVAILABLE),     # 1. an eligible Admin alone
    (1, 0, support_alerts.AVAILABLE),     # 2. an eligible Customer Support member alone
    (2, 0, support_alerts.AVAILABLE),     # 3. Admin unavailable, Customer Support available
    (0, 2, support_alerts.AVAILABLE),     # 4. Admin available, Customer Support unavailable
    (0, 0, support_alerts.UNAVAILABLE),   # 5. neither pool has anybody
])
async def test_the_latch_follows_the_availability_summary(factory, support_available,
                                                          admin_available, expected_state):
    await observe(factory, summary(support_available=support_available,
                                   admin_available=admin_available))
    assert await latch(factory) == expected_state


@pytest.mark.asyncio
async def test_it_reads_availability_from_the_real_rule_not_its_own(factory):
    """End to end against real rows: an online support member keeps the state AVAILABLE, and the
    same member going On Break takes it down — via availability_summary, not a second opinion."""
    async with factory() as db:
        db.add(User(id=1, username="sup1", name="Support 1", email="s1@x.com", hashed_password="x",
                    role=UserRole.SUPPORT_AGENT, active=True, support_availability="AVAILABLE"))
        await db.flush()
        db.add(UserSession(user_id=1, login_at=NOW - timedelta(hours=1),
                           last_activity_at=NOW - timedelta(seconds=5), active=True))
        await db.commit()

        live = await support_routing.availability_summary(db, now=NOW)
    assert live["available"] is True
    await observe(factory, live)
    assert await latch(factory) == support_alerts.AVAILABLE

    async with factory() as db:
        member = (await db.execute(select(User).where(User.id == 1))).scalar_one()
        member.support_availability = "ON_BREAK"
        await db.commit()
        live = await support_routing.availability_summary(db, now=NOW)
    assert live["available"] is False
    assert await observe(factory, live) == "OUTAGE"


@pytest.mark.asyncio
async def test_the_first_observation_is_not_a_transition(factory, telegram):
    """A stack that comes up already unavailable has not *gone* down — nobody is paged for it."""
    assert await observe(factory, DOWN) is None
    assert telegram == []
    assert await events(factory) == []
    assert await latch(factory) == support_alerts.UNAVAILABLE


# ── 2. One alert per outage ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_available_to_unavailable_sends_exactly_one_alert(factory, telegram):
    """Case 6: the last available person drops off → one alert, to the configured destination."""
    await observe(factory, UP)
    assert await observe(factory, DOWN) == "OUTAGE"

    assert len(telegram) == 1
    chat, body = telegram[0]
    assert chat == TEST_CHAT_ID
    assert "Support Team Unavailable" in body and "OFFLINE" in body
    assert "28 Aug 2026, 05:30 PM IST" in body      # NOW (UTC) rendered in IST


@pytest.mark.asyncio
async def test_staying_unavailable_never_alerts_again(factory, telegram):
    """Case 7 — the polling case. Every beat while support is down re-reads UNAVAILABLE; only the
    edge into it was an event."""
    await observe(factory, UP)
    await observe(factory, DOWN)
    for minute in range(1, 13):                     # ~4 minutes of 20s merchant polls
        assert await observe(factory, DOWN, at=NOW + timedelta(seconds=20 * minute)) is None
    assert len(telegram) == 1
    assert len(await events(factory)) == 1


@pytest.mark.asyncio
async def test_recovering_and_dropping_again_alerts_a_second_time(factory, telegram):
    """Case 9: recovery re-arms the latch, so the NEXT outage is a fresh event."""
    await observe(factory, UP)
    await observe(factory, DOWN)
    await observe(factory, UP, at=NOW + timedelta(minutes=15))
    assert await observe(factory, DOWN, at=NOW + timedelta(minutes=20)) == "OUTAGE"

    assert len(telegram) == 2
    assert [(e.previous_status, e.new_status) for e in await events(factory)] == [
        ("AVAILABLE", "UNAVAILABLE"),
        ("UNAVAILABLE", "AVAILABLE"),
        ("AVAILABLE", "UNAVAILABLE"),
    ]


@pytest.mark.asyncio
async def test_many_simultaneous_pollers_produce_one_alert(factory, telegram):
    """Case 10: twenty merchants polling the same instant — the transition is claimed once.

    This is the property that makes the feature safe to run behind multiple workers: the claim is a
    compare-and-set against a singleton row, so only the caller whose UPDATE matched sends.
    """
    await observe(factory, UP)
    results = await asyncio.gather(*[observe(factory, DOWN) for _ in range(20)])

    assert results.count("OUTAGE") == 1
    assert results.count(None) == 19
    assert len(telegram) == 1
    assert len(await events(factory)) == 1


@pytest.mark.asyncio
async def test_a_repeated_available_observation_is_not_an_event(factory, telegram):
    """The mirror of the polling case: a green pill polled all day writes nothing."""
    for i in range(10):
        await observe(factory, UP, at=NOW + timedelta(seconds=20 * i))
    assert await events(factory) == []
    assert telegram == []


# ── 3. Recovery is recorded, not announced ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coming_back_up_sends_nothing(factory, telegram):
    """Case 8: an outage alert already went out; the recovery is logged internally only."""
    await observe(factory, UP)
    await observe(factory, DOWN)
    telegram.clear()

    assert await observe(factory, UP, at=NOW + timedelta(minutes=5)) == "RECOVERY"
    assert telegram == []

    recovery = (await events(factory))[-1]
    assert (recovery.previous_status, recovery.new_status) == ("UNAVAILABLE", "AVAILABLE")
    assert recovery.telegram_status is None


# ── 4. Telegram is best-effort — it can never take anything else down ───────────────────────────

@pytest.mark.asyncio
async def test_a_failing_telegram_still_records_the_outage(factory, monkeypatch):
    """Case 11: the bot is dead. The state still moves, the outage is still audited, and the
    failure reason is kept — no exception escapes to the caller."""
    async def explode(chat_id, text):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(wa, "_send_telegram", explode)

    await observe(factory, UP)
    assert await observe(factory, DOWN) == "OUTAGE"           # no raise

    assert await latch(factory) == support_alerts.UNAVAILABLE  # availability tracking unaffected
    event = (await events(factory))[-1]
    assert event.telegram_status == "FAILED"
    assert "telegram unreachable" in event.telegram_failure_reason


@pytest.mark.asyncio
async def test_a_failed_send_does_not_re_alert_on_the_next_poll(factory, monkeypatch):
    """A failure must not turn into a retry loop: the transition was consumed, so the following
    beats stay quiet. 'Do not retry endlessly' means do not retry at all."""
    attempts: list[str] = []

    async def failing(chat_id, text):
        attempts.append(chat_id)
        return False, None, "502 bad gateway"

    monkeypatch.setattr(wa, "_send_telegram", failing)
    await observe(factory, UP)
    await observe(factory, DOWN)
    for i in range(1, 6):
        await observe(factory, DOWN, at=NOW + timedelta(seconds=20 * i))

    assert len(attempts) == 1
    assert (await events(factory))[-1].telegram_status == "FAILED"


@pytest.mark.asyncio
async def test_an_unconfigured_bot_still_tracks_the_outage(factory, monkeypatch, telegram):
    """Production runs with no bot token. The state machine and audit trail must work anyway."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "", raising=False)

    await observe(factory, UP)
    assert await observe(factory, DOWN) == "OUTAGE"
    assert telegram == []
    assert (await events(factory))[-1].telegram_status == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_no_linked_super_admin_is_recorded_not_raised(factory, monkeypatch, telegram):
    """No override and no Super Admin has linked Telegram — logged, and nothing else breaks."""
    monkeypatch.setattr(settings, "SUPPORT_ALERT_TELEGRAM_CHAT_ID", "", raising=False)

    await observe(factory, UP)
    assert await observe(factory, DOWN) == "OUTAGE"
    assert telegram == []
    assert (await events(factory))[-1].telegram_status == "NO_RECIPIENT"


@pytest.mark.asyncio
async def test_the_alert_goes_to_linked_super_admins_when_no_override_is_set(factory, monkeypatch,
                                                                            telegram):
    """The configured recipient is the Super Admin — and only the Super Admin. An Admin who has
    linked Telegram for workflow notifications is not paged about outages."""
    monkeypatch.setattr(settings, "SUPPORT_ALERT_TELEGRAM_CHAT_ID", "", raising=False)
    async with factory() as db:
        db.add(User(id=90, username="sa", name="Super", email="sa@x.com", hashed_password="x",
                    role=UserRole.SUPER_ADMIN, active=True, telegram_chat_id="555"))
        db.add(User(id=91, username="adm", name="Admin", email="a@x.com", hashed_password="x",
                    role=UserRole.ADMIN, active=True, telegram_chat_id="666"))
        await db.commit()

    await observe(factory, UP)
    await observe(factory, DOWN)
    assert [chat for chat, _ in telegram] == ["555"]


@pytest.mark.asyncio
async def test_every_attempt_is_written_to_the_existing_delivery_log(factory, telegram):
    """Reuses the existing notification-log architecture rather than inventing a second one."""
    await observe(factory, UP)
    await observe(factory, DOWN)

    async with factory() as db:
        logs = list((await db.execute(select(WhatsAppLog))).scalars().all())
    assert len(logs) == 1
    assert logs[0].provider == "telegram"
    assert logs[0].notification_type == support_alerts.NOTIFICATION_TYPE
    assert logs[0].status == "SENT"


# ── 5. The audit trail carries what the outage report needs ────────────────────────────────────

@pytest.mark.asyncio
async def test_the_outage_event_records_the_full_picture(factory):
    await observe(factory, UP)
    await observe(factory, DOWN)

    event = (await events(factory))[-1]
    assert event.previous_status == "AVAILABLE"
    assert event.new_status == "UNAVAILABLE"
    assert event.occurred_at == NOW                        # trigger time (naive UTC, as stored)
    assert event.occurred_at_ist == "28 Aug 2026, 05:30 PM IST"
    assert event.available_admins == 0
    assert event.available_support == 0
    assert event.telegram_status == "SENT"
    assert event.telegram_failure_reason is None


@pytest.mark.asyncio
async def test_the_counts_recorded_are_the_ones_observed(factory):
    """The recovery row keeps the counts that made it a recovery, so the log reads as a history."""
    await observe(factory, UP)
    await observe(factory, DOWN)
    await observe(factory, summary(support_available=2, admin_available=1),
                  at=NOW + timedelta(minutes=5))

    recovery = (await events(factory))[-1]
    assert (recovery.available_support, recovery.available_admins) == (2, 1)


@pytest.mark.asyncio
async def test_transitions_reach_the_system_log(factory):
    """Admins already read System Logs — transitions land there too, attributed to 'system'."""
    await observe(factory, UP)
    await observe(factory, DOWN)

    async with factory() as db:
        rows = list((await db.execute(select(SystemLog).where(
            SystemLog.action == "SUPPORT_AVAILABILITY_TRANSITION"))).scalars().all())
    assert len(rows) == 1
    assert rows[0].actor_name == "system"
    assert "AVAILABLE → UNAVAILABLE" in rows[0].detail


@pytest.mark.asyncio
async def test_the_latch_is_a_singleton(factory):
    """One row, forever — a second would mean two competing ideas of the current state."""
    for i, s in enumerate([UP, DOWN, UP, DOWN, DOWN]):
        await observe(factory, s, at=NOW + timedelta(minutes=i))
    async with factory() as db:
        assert (await db.execute(select(func.count()).select_from(SupportAvailabilityState))).scalar() == 1


# ── 6. Through the real endpoint ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def merchant_portal(factory, monkeypatch):
    """The real ``GET /api/support/availability`` route, over ASGI, on the test database.

    This is the merchant header's poll — the path everything above is reached through in
    production. Mounting the actual router (rather than calling the service) is what proves the
    endpoint is wired to the state machine and still returns the summary unchanged.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.routes import support as support_routes
    from app.core.deps import get_current_user
    from app.db.session import get_db

    monkeypatch.setattr(support_alerts, "AsyncSessionLocal", factory)

    app = FastAPI()
    app.include_router(support_routes.router)

    async def _db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: User(
        id=50, username="m1", name="BELLAGIO", email="m@x.com", hashed_password="x",
        role=UserRole.MERCHANT, active=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _set_support(factory, *, availability: str):
    async with factory() as db:
        member = (await db.execute(select(User).where(User.id == 1))).scalar_one_or_none()
        if member is None:
            db.add(User(id=1, username="sup1", name="Support 1", email="s1@x.com",
                        hashed_password="x", role=UserRole.SUPPORT_AGENT, active=True,
                        support_availability=availability))
            await db.flush()
            db.add(UserSession(user_id=1, login_at=datetime.utcnow() - timedelta(hours=1),
                               last_activity_at=datetime.utcnow(), active=True))
        else:
            member.support_availability = availability
            session = (await db.execute(
                select(UserSession).where(UserSession.user_id == 1))).scalars().first()
            session.last_activity_at = datetime.utcnow()   # keep presence live either way
        await db.commit()


@pytest.mark.asyncio
async def test_polling_the_merchant_endpoint_alerts_once_and_keeps_working(merchant_portal, factory,
                                                                          telegram):
    """The whole feature through its real entry point: the pill flips, ONE alert goes out, and
    every subsequent poll still answers correctly without sending anything more."""
    await _set_support(factory, availability="AVAILABLE")
    first = await merchant_portal.get("/api/support/availability")
    assert first.status_code == 200 and first.json()["available"] is True
    assert telegram == []

    await _set_support(factory, availability="ON_BREAK")     # the last member steps away
    outage = await merchant_portal.get("/api/support/availability")
    assert outage.json()["available"] is False
    assert len(telegram) == 1

    for _ in range(5):                                       # the merchant keeps polling
        again = await merchant_portal.get("/api/support/availability")
        assert again.status_code == 200 and again.json()["available"] is False
    assert len(telegram) == 1


@pytest.mark.asyncio
async def test_the_endpoint_still_answers_when_telegram_is_broken(merchant_portal, factory,
                                                                  monkeypatch):
    """Case 11 at the HTTP layer: Telegram is down, the availability API is not."""
    async def explode(chat_id, text):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(wa, "_send_telegram", explode)

    await _set_support(factory, availability="AVAILABLE")
    assert (await merchant_portal.get("/api/support/availability")).json()["available"] is True

    await _set_support(factory, availability="BUSY")
    down = await merchant_portal.get("/api/support/availability")
    assert down.status_code == 200
    assert down.json()["available"] is False
    assert (await events(factory))[-1].telegram_status == "FAILED"


@pytest.mark.asyncio
async def test_the_endpoint_response_never_carries_telegram_configuration(merchant_portal, factory):
    """Case 12 over the wire: the merchant sees counts, never the bot token or a chat id."""
    await _set_support(factory, availability="AVAILABLE")
    body = (await merchant_portal.get("/api/support/availability")).text.lower()
    for leaked in ("telegram", "chat", "token", "bot", TEST_CHAT_ID, "test-token"):
        assert leaked.lower() not in body


# ── 7. Credentials never leave the backend ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_availability_payload_carries_no_telegram_configuration(factory):
    """Case 12: what the merchant endpoint returns is counts only — no token, no chat id, no
    recipient. The alert configuration is read exclusively inside this service."""
    async with factory() as db:
        payload = await support_routing.availability_summary(db, now=NOW)

    blob = repr(payload).lower()
    assert settings.TELEGRAM_BOT_TOKEN not in repr(payload)
    assert TEST_CHAT_ID not in repr(payload)
    for leaked in ("telegram", "chat", "token", "bot"):
        assert leaked not in blob
    assert set(payload) == {"available", "availableAgents", "onlineAgents", "totalAgents",
                            "support", "admin"}
