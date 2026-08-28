"""Tests for the Support Team availability the Merchant Portal header reports.

The indicator's whole value rests on one property: green means a merchant can actually reach
someone. So ``availability_summary`` must be a projection of the SAME rule the auto-assignment
uses (``derive_status``) — never "is a merchant logged in", never "does a Support Center page
exist", never "does a support account exist".

What these pin down:

  1. **Available means assignable** — only a member ``derive_status`` calls ``available`` counts.
     Offline, on-break, manually Busy and at-capacity members never make the pill green.
  2. **Absence is unavailable** — no members, no sessions, or only deactivated/archived ones all
     report unavailable rather than defaulting to green.
  3. **The counts are honest** — availableAgents / onlineAgents / totalAgents describe the team,
     and available is exactly ``availableAgents > 0``.

Run from the backend directory:

    python -m pytest tests/test_support_availability.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.models import SupportConfig, SupportConversation, User, UserRole, UserSession
from app.services import presence, support_routing

NOW = datetime(2026, 8, 28, 12, 0, 0)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        session.add(SupportConfig(id=1, max_active_conversations=2, strategy="LEAST_ACTIVE"))
        await session.flush()
        yield session
    await engine.dispose()


async def _agent(db: AsyncSession, uid: int, *, availability="AVAILABLE", online=True,
                 active=True, archived=False, last_seen_secs=5) -> User:
    u = User(
        id=uid, username=f"sup{uid}", name=f"Support {uid}", email=f"s{uid}@x.com",
        hashed_password="x", role=UserRole.SUPPORT_AGENT, active=active,
        support_archived=archived, support_availability=availability,
    )
    db.add(u)
    await db.flush()
    if online:
        db.add(UserSession(
            user_id=uid, login_at=NOW - timedelta(hours=1),
            last_activity_at=NOW - timedelta(seconds=last_seen_secs), active=True,
        ))
        await db.flush()
    return u


async def _open_conversations(db: AsyncSession, agent_id: int, n: int) -> None:
    for i in range(n):
        db.add(SupportConversation(
            customer_id=1000 + agent_id * 10 + i, support_id=agent_id, status="OPEN",
            created_at=NOW, last_message_at=NOW,
        ))
    await db.flush()


# ── 1. Available means assignable ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_online_available_member_makes_support_available(db):
    await _agent(db, 1)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is True
    assert out == {"available": True, "availableAgents": 1, "onlineAgents": 1, "totalAgents": 1}


@pytest.mark.asyncio
async def test_an_offline_member_is_not_available(db):
    """A member with no live session cannot be assigned a conversation, so support is not up."""
    await _agent(db, 1, online=False)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False
    assert out["availableAgents"] == 0 and out["onlineAgents"] == 0 and out["totalAgents"] == 1


@pytest.mark.asyncio
async def test_a_stale_heartbeat_reads_as_offline(db):
    """Presence, not a row in the table, is what counts — a session past the window is offline."""
    stale = int(presence.ONLINE_WINDOW.total_seconds()) + 30
    await _agent(db, 1, last_seen_secs=stale)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False and out["onlineAgents"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", ["BUSY", "ON_BREAK"])
async def test_manual_busy_or_on_break_is_not_available(db, manual):
    await _agent(db, 1, availability=manual)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False
    assert out["availableAgents"] == 0
    assert out["onlineAgents"] == 1        # still online, just not takeable


@pytest.mark.asyncio
async def test_a_member_at_the_conversation_limit_is_not_available(db):
    """Capacity is part of the same rule: a full member cannot take a new conversation."""
    await _agent(db, 1)
    await _open_conversations(db, 1, 2)     # config limit is 2
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False and out["onlineAgents"] == 1


@pytest.mark.asyncio
async def test_a_member_below_the_limit_is_available(db):
    await _agent(db, 1)
    await _open_conversations(db, 1, 1)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is True and out["availableAgents"] == 1


@pytest.mark.asyncio
async def test_one_available_member_among_several_is_enough(db):
    await _agent(db, 1, online=False)              # offline
    await _agent(db, 2, availability="ON_BREAK")   # on break
    await _agent(db, 3)                            # available
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is True
    assert out["availableAgents"] == 1 and out["onlineAgents"] == 2 and out["totalAgents"] == 3


# ── 2. Absence is unavailable ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_support_members_at_all(db):
    out = await support_routing.availability_summary(db, now=NOW)
    assert out == {"available": False, "availableAgents": 0, "onlineAgents": 0, "totalAgents": 0}


@pytest.mark.asyncio
async def test_a_deactivated_member_does_not_count(db):
    await _agent(db, 1, active=False)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False and out["totalAgents"] == 0


@pytest.mark.asyncio
async def test_an_archived_member_does_not_count(db):
    await _agent(db, 1, archived=True)
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False and out["totalAgents"] == 0


@pytest.mark.asyncio
async def test_a_logged_in_merchant_does_not_make_support_available(db):
    """The merchant's own session is irrelevant — only the support team's state matters."""
    merchant = User(id=50, username="m1", name="BELLAGIO", email="m@x.com", hashed_password="x",
                    role=UserRole.MERCHANT, active=True)
    db.add(merchant)
    db.add(UserSession(user_id=50, login_at=NOW, last_activity_at=NOW, active=True))
    await db.flush()
    out = await support_routing.availability_summary(db, now=NOW)
    assert out["available"] is False and out["totalAgents"] == 0


# ── 3. The counts are honest ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_available_is_exactly_available_agents_greater_than_zero(db):
    await _agent(db, 1, availability="BUSY")
    busy_only = await support_routing.availability_summary(db, now=NOW)
    assert busy_only["available"] is (busy_only["availableAgents"] > 0) is False

    await _agent(db, 2)
    now_up = await support_routing.availability_summary(db, now=NOW)
    assert now_up["available"] is (now_up["availableAgents"] > 0) is True


@pytest.mark.asyncio
async def test_summary_agrees_with_derive_status_member_by_member(db):
    """The summary is a projection of derive_status, not a second opinion about availability."""
    await _agent(db, 1)                              # available
    await _agent(db, 2, availability="BUSY")         # busy
    await _agent(db, 3, online=False)                # offline
    await _agent(db, 4, availability="ON_BREAK")     # break

    cfg = await support_routing.get_config(db)
    agents = await support_routing._all_agents(db)
    ids = [a.id for a in agents]
    sessions = await presence.latest_sessions(db, ids)
    counts = await support_routing.active_counts(db, ids)
    statuses = [support_routing.derive_status(a, sessions.get(a.id), counts.get(a.id, 0), cfg, NOW)
                for a in agents]

    out = await support_routing.availability_summary(db, now=NOW)
    assert out["availableAgents"] == statuses.count("available")
    assert out["onlineAgents"] == sum(1 for s in statuses if s != "offline")
    assert out["totalAgents"] == len(agents)
