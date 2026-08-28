"""Support conversation routing: auto-assignment, availability, and queue draining.

A customer (a merchant-role user) has at most one OPEN ``SupportConversation``. When they first
message, a conversation is opened and assigned to exactly one available agent (least-active or
round-robin). An agent is *available* when online, active, not manually Busy/On-Break, and below
the configured maximum active conversations; *busy* at the limit. If no agent is available the
conversation is queued (``support_id`` NULL) and drained to the oldest waiter the instant an agent
frees up. Notifications go only to the assigned agent (auto-mirrored to WhatsApp) and the customer —
never broadcast to every agent.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import User, UserRole, Notification, SupportConversation, SupportConfig
from app.services import presence

STRATEGIES = ("LEAST_ACTIVE", "ROUND_ROBIN")


async def get_config(db: AsyncSession) -> SupportConfig:
    """The singleton support config (id=1), created with defaults on first use."""
    cfg = (await db.execute(select(SupportConfig).where(SupportConfig.id == 1))).scalar_one_or_none()
    if not cfg:
        cfg = SupportConfig(id=1, max_active_conversations=10, strategy="LEAST_ACTIVE")
        db.add(cfg)
        await db.flush()
    return cfg


async def active_counts(db: AsyncSession, support_ids: list[int]) -> dict[int, int]:
    """OPEN conversation count per agent id."""
    if not support_ids:
        return {}
    rows = (await db.execute(
        select(SupportConversation.support_id, func.count())
        .where(SupportConversation.support_id.in_(support_ids), SupportConversation.status == "OPEN")
        .group_by(SupportConversation.support_id)
    )).all()
    return {sid: c for sid, c in rows}


async def _all_agents(db: AsyncSession) -> list[User]:
    return (await db.execute(
        select(User).where(
            User.role == UserRole.SUPPORT_AGENT,
            User.support_archived == False,  # noqa: E712
            User.active == True,             # noqa: E712
        )
    )).scalars().all()


def derive_status(agent: User, sess, count: int, cfg: SupportConfig, now: datetime) -> str:
    """offline | break | busy | available — the single source of truth for an agent's status."""
    if not presence.is_online(sess, now):
        return "offline"
    manual = str(agent.support_availability or "AVAILABLE").upper()
    if manual == "ON_BREAK":
        return "break"
    if manual == "BUSY":
        return "busy"
    if count >= cfg.max_active_conversations:
        return "busy"
    return "available"


async def _available_agents(db: AsyncSession, cfg: SupportConfig, now: datetime) -> list[tuple[User, int]]:
    """(agent, active_count) for agents that can take a new conversation right now."""
    agents = await _all_agents(db)
    if not agents:
        return []
    ids = [a.id for a in agents]
    sessions = await presence.latest_sessions(db, ids)
    counts = await active_counts(db, ids)
    out: list[tuple[User, int]] = []
    for a in agents:
        if derive_status(a, sessions.get(a.id), counts.get(a.id, 0), cfg, now) == "available":
            out.append((a, counts.get(a.id, 0)))
    return out


def _pick(candidates: list[tuple[User, int]], cfg: SupportConfig) -> User:
    if cfg.strategy == "ROUND_ROBIN":
        ordered = sorted(candidates, key=lambda t: t[0].id)
        last = cfg.last_assigned_support_id
        after = [t for t in ordered if last is None or t[0].id > last]
        return (after[0] if after else ordered[0])[0]
    # LEAST_ACTIVE (default): fewest active conversations, tie-break by lowest id (stable + fair).
    return min(candidates, key=lambda t: (t[1], t[0].id))[0]


async def _notify_assignment(db: AsyncSession, conv: SupportConversation, agent: User, customer: User) -> None:
    """Only the assigned agent + the customer are notified. The WhatsApp hook mirrors each
    Notification to that recipient's phone, so no other agent receives anything."""
    who = f"{customer.name} ({customer.merchant_code or customer.username})"
    db.add(Notification(user_id=agent.id, message=f"New support conversation assigned — {who}", icon="🎧"))
    db.add(Notification(
        user_id=customer.id,
        message=f"Your support request has been assigned to {agent.full_name or agent.name}.",
        icon="🎧",
    ))


async def assign_conversation(
    db: AsyncSession, conv: SupportConversation, *, actor_id: Optional[int] = None, now: Optional[datetime] = None
) -> Optional[User]:
    """Assign an OPEN, unassigned conversation to the best available agent. Returns the agent, or
    None when nobody is available (the conversation stays queued)."""
    now = now or datetime.utcnow()
    cfg = await get_config(db)
    candidates = await _available_agents(db, cfg, now)
    if not candidates:
        if conv.queued_at is None:
            conv.queued_at = now
        await db.flush()
        return None
    agent = _pick(candidates, cfg)
    conv.support_id = agent.id
    conv.status = "OPEN"
    conv.assigned_at = now
    conv.queued_at = None
    if actor_id is not None:
        conv.assigned_by = actor_id
    cfg.last_assigned_support_id = agent.id
    customer = (await db.execute(select(User).where(User.id == conv.customer_id))).scalar_one_or_none()
    if customer:
        await _notify_assignment(db, conv, agent, customer)
    await db.flush()
    return agent


async def _agent_online(db: AsyncSession, agent_id: Optional[int], now: datetime) -> bool:
    """Is this agent a live, usable owner right now (active, not archived, session online)?"""
    if not agent_id:
        return False
    u = (await db.execute(select(User).where(User.id == agent_id))).scalar_one_or_none()
    if not u or not u.active or u.support_archived:
        return False
    sess = (await presence.latest_sessions(db, [agent_id])).get(agent_id)
    return presence.is_online(sess, now)


async def reclaim_offline(db: AsyncSession, *, now: Optional[datetime] = None) -> None:
    """Return to the queue any OPEN conversation whose owner is offline / deactivated / archived, so
    it can be reassigned to whoever is on shift now. This is what makes shift hand-over work: when a
    member logs out at the end of their shift, their live chats flow to the next available member."""
    now = now or datetime.utcnow()
    convs = (await db.execute(
        select(SupportConversation).where(
            SupportConversation.status == "OPEN", SupportConversation.support_id.isnot(None)
        )
    )).scalars().all()
    if not convs:
        return
    ids = list({c.support_id for c in convs})
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()}
    sessions = await presence.latest_sessions(db, ids)
    for c in convs:
        u = users.get(c.support_id)
        online = bool(u and u.active and not u.support_archived and presence.is_online(sessions.get(c.support_id), now))
        if not online:
            c.support_id = None
            if c.queued_at is None:
                c.queued_at = now
    await db.flush()


async def get_open_conversation(db: AsyncSession, customer_id: int) -> Optional[SupportConversation]:
    return (await db.execute(
        select(SupportConversation)
        .where(SupportConversation.customer_id == customer_id, SupportConversation.status == "OPEN")
        .order_by(SupportConversation.id.desc())
    )).scalars().first()


async def ensure_conversation(db: AsyncSession, customer_id: int, *, now: Optional[datetime] = None) -> SupportConversation:
    """Return the customer's OPEN conversation, opening + auto-assigning a new one if none exists."""
    now = now or datetime.utcnow()
    conv = await get_open_conversation(db, customer_id)
    if conv:
        # If the owning agent has gone offline (e.g. their shift ended), hand the thread to whoever
        # is on shift now so the customer always reaches an available member.
        if conv.support_id and not await _agent_online(db, conv.support_id, now):
            conv.support_id = None
            conv.queued_at = now
            await assign_conversation(db, conv, now=now)
        return conv
    conv = SupportConversation(customer_id=customer_id, status="OPEN", created_at=now, last_message_at=now)
    db.add(conv)
    await db.flush()
    await assign_conversation(db, conv, now=now)
    return conv


async def drain_queue(db: AsyncSession, *, now: Optional[datetime] = None) -> list[SupportConversation]:
    """Reclaim conversations from offline agents, then assign queued ones (oldest first) while
    capacity exists. Triggered on agent login, availability change, close and config change — so a
    new shift member automatically picks up both waiting customers and the prior shift's chats."""
    now = now or datetime.utcnow()
    await reclaim_offline(db, now=now)
    queued = (await db.execute(
        select(SupportConversation)
        .where(SupportConversation.status == "OPEN", SupportConversation.support_id.is_(None))
        .order_by(SupportConversation.created_at.asc(), SupportConversation.id.asc())
    )).scalars().all()
    assigned: list[SupportConversation] = []
    for conv in queued:
        agent = await assign_conversation(db, conv, now=now)
        if agent is None:
            break  # nobody available — stop draining
        assigned.append(conv)
    return assigned


async def close_conversation(
    db: AsyncSession, conv: SupportConversation, *, actor_id: Optional[int] = None, now: Optional[datetime] = None
) -> None:
    now = now or datetime.utcnow()
    conv.status = "CLOSED"
    conv.closed_at = now
    await db.flush()
    await drain_queue(db, now=now)  # a freed slot may admit a queued customer


async def reassign_conversation(
    db: AsyncSession, conv: SupportConversation, new_support_id: int, *, actor_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> User:
    now = now or datetime.utcnow()
    agent = (await db.execute(
        select(User).where(User.id == new_support_id, User.role == UserRole.SUPPORT_AGENT,
                           User.support_archived == False)  # noqa: E712
    )).scalar_one_or_none()
    if not agent:
        raise ValueError("Support member not found")
    conv.support_id = agent.id
    conv.status = "OPEN"
    conv.assigned_at = now
    conv.queued_at = None
    conv.assigned_by = actor_id
    customer = (await db.execute(select(User).where(User.id == conv.customer_id))).scalar_one_or_none()
    if customer:
        await _notify_assignment(db, conv, agent, customer)
    await db.flush()
    return agent


async def record_agent_reply(db: AsyncSession, customer_id: int, *, now: Optional[datetime] = None) -> None:
    """Stamp first_response_at the first time an agent replies in an open conversation (response-time
    metric). No-op if already stamped."""
    now = now or datetime.utcnow()
    conv = await get_open_conversation(db, customer_id)
    if conv and conv.first_response_at is None:
        conv.first_response_at = now
        await db.flush()


# Admin-portal roles that count as reachable "Admin support" for the merchant header indicator.
# ADMIN only: Super Admin is a platform-owner role, not a support desk, and counting it would
# advertise cover that nobody is actually staffing.
SUPPORT_ADMIN_ROLES = (UserRole.ADMIN,)


async def _support_admins(db: AsyncSession) -> list[User]:
    """Admins who are ON SUPPORT DUTY — the second pool the availability indicator considers.

    Opt-in, deliberately. An admin keeps the Admin Portal open all day to approve withdrawals and
    manage accounts; having the portal open is NOT the same as being available to answer a
    merchant. So an admin counts here only once they have explicitly set a support-duty state
    (``support_availability`` non-NULL). NULL — the default for every admin — means "not on
    support duty" and is excluded outright.

    Once opted in they are judged exactly like a support member: ``derive_status`` applies live
    presence and their chosen AVAILABLE / BUSY / ON_BREAK, so going Busy or On Break takes them
    out of the count without opting out entirely.

    Kept SEPARATE from ``_all_agents`` on purpose: that function feeds conversation ROUTING, and
    adding admins to it would start auto-assigning customer chats to them.
    """
    return (await db.execute(
        select(User).where(
            User.role.in_(SUPPORT_ADMIN_ROLES),
            User.active == True,                       # noqa: E712
            User.support_availability.isnot(None),     # opted in to support duty
        )
    )).scalars().all()


def _tally(members: list[User], sessions: dict, counts: dict, cfg: SupportConfig, now: datetime) -> dict:
    """available / online / total for one pool, using the shared ``derive_status`` rule.

    Works unchanged for admins: they carry no ``support_availability`` (so they read as AVAILABLE
    rather than Busy/On-Break) and are never assigned conversations (so their load is 0). What
    actually decides an admin is therefore only whether their session is live — which is exactly
    what "is an admin reachable right now" should mean.
    """
    statuses = [derive_status(m, sessions.get(m.id), counts.get(m.id, 0), cfg, now) for m in members]
    return {
        "available": sum(1 for s in statuses if s == "available"),
        "online": sum(1 for s in statuses if s != "offline"),
        "total": len(members),
    }


async def availability_summary(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    """Is support reachable right now, from EITHER pool?

    The merchant header shows "Support Available" when at least one eligible person is available in
    the Customer Support team **or** among the Admins — and "Support Unavailable" only when both
    pools are empty of anyone reachable.

    Both pools are judged by the SAME ``derive_status`` the auto-assignment uses (live presence +
    manual Busy/On-Break + configured conversation capacity), so a member who could not be handed a
    conversation is never advertised as available, and a member who is merely *busy but under their
    limit* still counts — which is what the routing rules already say.

    Being signed in as a merchant, or the Support Center page existing, has no bearing on any of it.
    """
    now = now or datetime.utcnow()
    cfg = await get_config(db)
    agents = await _all_agents(db)
    admins = await _support_admins(db)

    everyone = agents + admins
    if not everyone:
        return {
            "available": False, "availableAgents": 0, "onlineAgents": 0, "totalAgents": 0,
            "support": {"available": 0, "online": 0, "total": 0},
            "admin": {"available": 0, "online": 0, "total": 0},
        }

    ids = [m.id for m in everyone]
    sessions = await presence.latest_sessions(db, ids)
    # Conversation load is a Customer-Support concept; admins are never assigned any, so the
    # lookup covers only the agent pool and admins fall through to 0.
    counts = await active_counts(db, [a.id for a in agents])

    support = _tally(agents, sessions, counts, cfg, now)
    admin = _tally(admins, sessions, counts, cfg, now)

    return {
        # The OR that the merchant header renders.
        "available": (support["available"] + admin["available"]) > 0,
        # Combined totals — the existing shape the indicator's tooltip reads.
        "availableAgents": support["available"] + admin["available"],
        "onlineAgents": support["online"] + admin["online"],
        "totalAgents": support["total"] + admin["total"],
        # Per-pool breakdown, so the indicator can say WHO is reachable.
        "support": support,
        "admin": admin,
    }
