"""Support Management — Admin/Super Admin console for the Support Team.

Support members are ``SUPPORT_AGENT``-role users enriched with a Support ID, department, shift and
availability. Customers are auto-assigned to a single member per conversation (see
services/support_routing); members are no longer permanently tied to merchants. This module covers:

  • GET    /api/support-management/agents             — list members (+ live status / active count)
  • GET    /api/support-management/agents/stream      — SSE live feed (cards + table + dashboard)
  • POST   /api/support-management/agents             — create a member
  • PATCH  /api/support-management/agents/{id}         — edit details
  • PATCH  /api/support-management/agents/{id}/toggle  — activate / deactivate
  • POST   /api/support-management/agents/{id}/reset-password
  • PATCH  /api/support-management/agents/{id}/availability — Admin force Available/Busy/On-Break
  • GET    /api/support-management/agents/{id}/profile — full profile (session details)
  • DELETE /api/support-management/agents/{id}         — soft-archive (Super Admin only)
  • GET    /api/support-management/config              — assignment config (max / strategy)
  • PUT    /api/support-management/config              — update config
  • GET    /api/support-management/conversations       — all conversations (active + queued)
  • POST   /api/support-management/conversations/{id}/reassign
  • POST   /api/support-management/conversations/{id}/close
  • PATCH  /api/support-management/me/availability     — member sets own Available/Busy/On-Break

Scope: Super Admin → all members; Admin → only members they created (``created_by``).
"""
import asyncio
import json
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db, AsyncSessionLocal
from app.core.deps import get_current_admin, get_current_super_admin, get_current_support
from app.core.security import get_password_hash
from app.core.passwords import assert_password_allowed, set_password
from app.models.models import User, UserRole, Notification, SupportMessage, SupportConversation
from app.schemas.schemas import (
    SupportMemberCreate, SupportMemberUpdate, AvailabilityRequest, ReasonRequest,
    SupportConfigUpdate, ReassignConversationRequest,
)
from app.services import presence, support_routing
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/support-management", tags=["support-management"])

STREAM_TICK_SECONDS = 1.0
STREAM_FORCE_REFRESH_SECONDS = 15.0

DEPARTMENTS = {"Technical Support", "Payments", "Merchant Support", "Finance", "Compliance"}
SHIFTS = {"Morning", "Afternoon", "Night"}
AVAILABILITY_VALUES = ("AVAILABLE", "BUSY", "ON_BREAK")


def _ip(request: Request) -> str | None:
    return request.client.host if request and request.client else None


def _normalize_phone(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not (8 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Enter a valid phone number with country code, e.g. +919812345678")
    return "+" + digits


async def _next_support_code(db: AsyncSession) -> str:
    codes = (await db.execute(
        select(User.support_code).where(User.support_code.like("SUP%"))
    )).scalars().all()
    maxn = 0
    for c in codes:
        try:
            maxn = max(maxn, int(c[3:]))
        except (TypeError, ValueError):
            continue
    return f"SUP{maxn + 1:06d}"


# ─── Visibility / access ──────────────────────────────────────────────────────
def _visible_filter(caller_role, caller_id: int):
    base = (User.role == UserRole.SUPPORT_AGENT) & (User.support_archived == False)  # noqa: E712
    if caller_role == UserRole.SUPER_ADMIN:
        return base
    return base & (User.created_by == caller_id)


async def _get_member(db: AsyncSession, caller: User, member_id: int) -> User:
    m = (await db.execute(
        select(User).where(User.id == member_id, User.role == UserRole.SUPPORT_AGENT)
    )).scalar_one_or_none()
    if not m or m.support_archived:
        raise HTTPException(status_code=404, detail="Support member not found")
    if caller.role != UserRole.SUPER_ADMIN and m.created_by != caller.id:
        raise HTTPException(status_code=403, detail="You can only manage support members you created")
    return m


# ─── Serialization ────────────────────────────────────────────────────────────
def _member_out(u: User, sess, active: int, cfg, now: datetime) -> dict:
    ua = presence.parse_user_agent(sess.user_agent if sess else None)
    current = bool(sess and sess.active and sess.logout_at is None)
    duration = None
    if sess:
        end = now if current else (sess.logout_at or sess.last_activity_at)
        duration = max(0, int((end - sess.login_at).total_seconds()))
    return {
        "id": u.id,
        "supportCode": u.support_code,
        "fullName": u.full_name or u.name,
        "username": u.username,
        "email": u.email,
        "phone": u.phone,
        "avatar": u.avatar,
        "department": u.support_department,
        "shift": u.support_shift,
        "status": support_routing.derive_status(u, sess, active, cfg, now),
        "availability": str(u.support_availability or "AVAILABLE").upper(),
        "active": u.active,
        "activeConversations": active,
        "maxConversations": cfg.max_active_conversations,
        "loginTime": (sess.login_at.isoformat() + "Z") if sess else None,
        "lastActivity": (sess.last_activity_at.isoformat() + "Z") if sess else None,
        "lastSeen": (sess.last_activity_at.isoformat() + "Z") if sess else None,
        "logoutTime": (sess.logout_at.isoformat() + "Z") if sess and sess.logout_at else None,
        "currentSession": current,
        "sessionDuration": duration,
        "ip": sess.ip_address if sess else None,
        "device": ua["device"] if sess else None,
        "browser": ua["browser"] if sess else None,
        "os": ua["os"] if sess else None,
        "createdAt": (u.created_at.isoformat() + "Z") if u.created_at else None,
        "created": str(u.created),
        "createdBy": u.created_by,
    }


async def _avg_response_seconds(db: AsyncSession) -> int | None:
    # Average the (first_response - created) gap in SQL rather than loading every
    # answered conversation into Python. This runs once per SSE tick (every ~1s per
    # open dashboard); the old row-by-row version scanned the whole conversation
    # table each time and grew unbounded, a key driver of the 2026-07-11 connection
    # pool exhaustion. AVG over an empty set returns NULL → None.
    avg = (await db.execute(
        select(func.avg(func.extract(
            "epoch", SupportConversation.first_response_at - SupportConversation.created_at
        ))).where(
            SupportConversation.first_response_at.isnot(None),
            SupportConversation.created_at.isnot(None),
        )
    )).scalar()
    return int(avg) if avg is not None else None


async def _build_payload(db: AsyncSession, caller_role, caller_id: int) -> dict:
    members = (await db.execute(select(User).where(_visible_filter(caller_role, caller_id)))).scalars().all()
    now = datetime.utcnow()
    ids = [m.id for m in members]
    sessions = await presence.latest_sessions(db, ids)
    counts = await support_routing.active_counts(db, ids)
    cfg = await support_routing.get_config(db)
    rows = [_member_out(m, sessions.get(m.id), counts.get(m.id, 0), cfg, now) for m in members]
    rows.sort(key=lambda r: (r["fullName"] or "").lower())

    available = sum(1 for r in rows if r["status"] == "available")
    busy = sum(1 for r in rows if r["status"] == "busy")
    on_break = sum(1 for r in rows if r["status"] == "break")
    offline = sum(1 for r in rows if r["status"] == "offline")

    active_total = (await db.execute(
        select(func.count()).where(SupportConversation.status == "OPEN", SupportConversation.support_id.isnot(None))
    )).scalar() or 0
    waiting = (await db.execute(
        select(func.count()).where(SupportConversation.status == "OPEN", SupportConversation.support_id.is_(None))
    )).scalar() or 0

    summary = {
        "members": len(rows),
        "available": available,
        "busy": busy,
        "onBreak": on_break,
        "offline": offline,
        "activeConversations": active_total,
        "waitingCustomers": waiting,
        "queueLength": waiting,
        "avgResponseSeconds": await _avg_response_seconds(db),
        "maxActiveConversations": cfg.max_active_conversations,
        "strategy": cfg.strategy,
    }
    return {"summary": summary, "members": rows}


def _signature(payload: dict) -> str:
    s = payload["summary"]
    head = f"{s['available']}:{s['busy']}:{s['onBreak']}:{s['offline']}:{s['activeConversations']}:{s['waitingCustomers']}:{s['maxActiveConversations']}:{s['strategy']}"
    body = "|".join(
        f"{r['id']}:{r['status']}:{r['availability']}:{r['activeConversations']}:{r['lastActivity']}:{r['logoutTime']}:{r['active']}"
        for r in sorted(payload["members"], key=lambda r: r["id"])
    )
    return head + "#" + body


# ─── List / stream ────────────────────────────────────────────────────────────
@router.get("/agents")
async def list_agents(db: AsyncSession = Depends(get_db), caller: User = Depends(get_current_admin)):
    return await _build_payload(db, caller.role, caller.id)


@router.get("/agents/stream")
async def agents_stream(request: Request, caller: User = Depends(get_current_admin)):
    """SSE feed — pushes a fresh payload whenever member presence/availability/load changes."""
    caller_id, caller_role = caller.id, caller.role

    async def event_source():
        last_sig: str | None = None
        last_push = 0.0
        while True:
            if await request.is_disconnected():
                break
            tnow = time.monotonic()
            try:
                async with AsyncSessionLocal() as db:
                    payload = await _build_payload(db, caller_role, caller_id)
                sig = _signature(payload)
                if sig != last_sig or (tnow - last_push) >= STREAM_FORCE_REFRESH_SECONDS:
                    last_sig, last_push = sig, tnow
                    yield f"data: {json.dumps(payload)}\n\n"
            except Exception:
                pass
            await asyncio.sleep(STREAM_TICK_SECONDS)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


# ─── Create ───────────────────────────────────────────────────────────────────
@router.post("/agents")
async def create_agent(
    data: SupportMemberCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    username = (data.username or "").strip()
    if not username or not (data.password or "").strip() or not (data.email or "").strip() or not (data.fullName or "").strip():
        raise HTTPException(status_code=400, detail="Username, password, email and full name are required")
    if data.department and data.department not in DEPARTMENTS:
        raise HTTPException(status_code=400, detail="Invalid department")
    if data.shift and data.shift not in SHIFTS:
        raise HTTPException(status_code=400, detail="Invalid shift")

    if (await db.execute(select(User).where(User.username == username))).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")
    if (await db.execute(select(User).where(User.email == data.email))).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already in use")

    phone = _normalize_phone(data.phone)
    code = await _next_support_code(db)
    member = User(
        username=username,
        hashed_password=get_password_hash(data.password),
        email=data.email,
        name=data.fullName,
        full_name=data.fullName,
        phone=phone,
        role=UserRole.SUPPORT_AGENT,
        active=(str(data.status or "Active").lower() != "inactive"),
        created_by=caller.id,
        support_code=code,
        support_department=(data.department or None),
        support_shift=(data.shift or None),
        support_availability="AVAILABLE",
    )
    db.add(member)
    await db.flush()

    db.add(Notification(user_id=member.id, message="Your support account was created", icon="🎧"))
    db.add(Notification(user_id=caller.id, message=f"Support member \"{member.name}\" created", icon="🎧"))
    await log_event(db, "SUPPORT_CREATED", f"Support member \"{member.name}\" ({code}) created by {caller.name}", actor=caller)
    await record_audit(db, "SUPPORT_CREATED", actor=caller, entity_type="support", entity_id=member.id,
                       new=f"{member.name} ({member.username})", ip=_ip(request))
    await db.refresh(member)
    return await _one(db, member)


# ─── Edit ─────────────────────────────────────────────────────────────────────
@router.patch("/agents/{member_id}")
async def update_agent(
    member_id: int,
    data: SupportMemberUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    if data.department is not None and data.department not in DEPARTMENTS | {""}:
        raise HTTPException(status_code=400, detail="Invalid department")
    if data.shift is not None and data.shift not in SHIFTS | {""}:
        raise HTTPException(status_code=400, detail="Invalid shift")
    if data.email and data.email != m.email:
        if (await db.execute(select(User).where(User.email == data.email, User.id != m.id))).scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use")
        m.email = data.email
    if data.fullName:
        m.full_name = data.fullName
        m.name = data.fullName
    if data.phone is not None:
        m.phone = _normalize_phone(data.phone)
    if data.department is not None:
        m.support_department = data.department or None
    if data.shift is not None:
        m.support_shift = data.shift or None
    await db.flush()
    await log_event(db, "SUPPORT_UPDATED", f"Support member \"{m.name}\" updated by {caller.name}", actor=caller)
    await record_audit(db, "SUPPORT_UPDATED", actor=caller, entity_type="support", entity_id=m.id, ip=_ip(request))
    return await _one(db, m)


# ─── Activate / Deactivate ────────────────────────────────────────────────────
@router.patch("/agents/{member_id}/toggle")
async def toggle_agent(
    member_id: int,
    request: Request,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    reason = (data.reason if data else None) or ""
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required")
    was_active = m.active
    m.active = not m.active
    await db.flush()
    if not m.active:
        await _requeue_member_conversations(db, m.id)  # free their conversations for reassignment
    state = "activated" if m.active else "deactivated"
    db.add(Notification(user_id=m.id, message=f"Your account was {state} — {reason}", icon="🎧"))
    db.add(Notification(user_id=caller.id, message=f"Support member {m.name} {state}", icon="🎧"))
    await log_event(db, "SUPPORT_TOGGLED", f"Support member \"{m.name}\" {state} by {caller.name} — reason: {reason}", actor=caller)
    await record_audit(db, "SUPPORT_TOGGLED", actor=caller, entity_type="support", entity_id=m.id,
                       old=f"active={was_active}", new=f"active={m.active}", reason=reason, ip=_ip(request))
    return await _one(db, m)


# ─── Reset password ───────────────────────────────────────────────────────────
@router.post("/agents/{member_id}/reset-password")
async def reset_agent_password(
    member_id: int,
    data: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    new_password = (data.get("new_password") or data.get("newPassword") or "").strip()
    if not new_password:
        raise HTTPException(status_code=400, detail="A new password is required")
    await assert_password_allowed(db, m, new_password)
    await set_password(db, m, new_password)
    m.failed_attempts = 0
    m.locked_until = None
    await db.flush()
    db.add(Notification(user_id=m.id, message="Your password was reset by an administrator", icon="🔑"))
    await log_event(db, "SUPPORT_PASSWORD_RESET", f"{caller.name} reset support member \"{m.name}\"'s password", actor=caller)
    await record_audit(db, "SUPPORT_PASSWORD_RESET", actor=caller, entity_type="support", entity_id=m.id, ip=_ip(request))
    return {"message": f"Password reset for {m.name}."}


# ─── Admin: force availability ────────────────────────────────────────────────
@router.patch("/agents/{member_id}/availability")
async def force_availability(
    member_id: int,
    data: AvailabilityRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    value = str(data.availability or "").upper()
    if value not in AVAILABILITY_VALUES:
        raise HTTPException(status_code=400, detail="availability must be AVAILABLE, BUSY or ON_BREAK")
    m.support_availability = value
    m.support_availability_at = datetime.utcnow()
    await db.flush()
    if value == "AVAILABLE":
        await support_routing.drain_queue(db)  # a freed member may admit queued customers
    db.add(Notification(user_id=m.id, message=f"An administrator set your status to {value.title().replace('_', ' ')}", icon="🎧"))
    await log_event(db, "SUPPORT_AVAILABILITY_FORCED", f"{caller.name} set {m.name} to {value}", actor=caller)
    await record_audit(db, "SUPPORT_AVAILABILITY_FORCED", actor=caller, entity_type="support", entity_id=m.id, new=value, ip=_ip(request))
    return await _one(db, m)


# ─── Profile (drawer) ─────────────────────────────────────────────────────────
@router.get("/agents/{member_id}/profile")
async def agent_profile(
    member_id: int,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    out = await _one(db, m)
    out["activeConversations"] = (await support_routing.active_counts(db, [m.id])).get(m.id, 0)
    return out


# ─── Delete (soft archive; Super Admin only) ──────────────────────────────────
@router.delete("/agents/{member_id}")
async def archive_agent(
    member_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    m = (await db.execute(
        select(User).where(User.id == member_id, User.role == UserRole.SUPPORT_AGENT)
    )).scalar_one_or_none()
    if not m or m.support_archived:
        raise HTTPException(status_code=404, detail="Support member not found")
    m.support_archived = True
    m.active = False
    await _requeue_member_conversations(db, m.id)
    await presence.end_session(db, m)
    await db.flush()
    await log_event(db, "SUPPORT_ARCHIVED", f"Support member \"{m.name}\" deleted (archived) by {sa.name}", actor=sa)
    await record_audit(db, "SUPPORT_ARCHIVED", actor=sa, entity_type="support", entity_id=m.id,
                       new="archived", ip=_ip(request))
    return {"message": f"{m.name} removed."}


# ─── Config ───────────────────────────────────────────────────────────────────
@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db), caller: User = Depends(get_current_admin)):
    cfg = await support_routing.get_config(db)
    return {"maxActiveConversations": cfg.max_active_conversations, "strategy": cfg.strategy}


@router.put("/config")
async def update_config(
    data: SupportConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    cfg = await support_routing.get_config(db)
    if data.maxActiveConversations is not None:
        if data.maxActiveConversations < 1 or data.maxActiveConversations > 1000:
            raise HTTPException(status_code=400, detail="maxActiveConversations must be between 1 and 1000")
        cfg.max_active_conversations = data.maxActiveConversations
    if data.strategy is not None:
        if data.strategy not in support_routing.STRATEGIES:
            raise HTTPException(status_code=400, detail="strategy must be LEAST_ACTIVE or ROUND_ROBIN")
        cfg.strategy = data.strategy
    await db.flush()
    await support_routing.drain_queue(db)  # a higher limit can free capacity for queued customers
    await log_event(db, "SUPPORT_CONFIG_UPDATED",
                    f"{caller.name} set support config (max={cfg.max_active_conversations}, strategy={cfg.strategy})", actor=caller)
    await record_audit(db, "SUPPORT_CONFIG_UPDATED", actor=caller, entity_type="support",
                       new=f"max={cfg.max_active_conversations}, {cfg.strategy}", ip=_ip(request))
    return {"maxActiveConversations": cfg.max_active_conversations, "strategy": cfg.strategy}


# ─── Conversations (admin) ────────────────────────────────────────────────────
async def _conversation_out(db: AsyncSession, c: SupportConversation, users: dict[int, User]) -> dict:
    cust = users.get(c.customer_id)
    agent = users.get(c.support_id) if c.support_id else None
    last = (await db.execute(
        select(SupportMessage).where(SupportMessage.merchant_id == c.customer_id)
        .order_by(SupportMessage.created_at.desc())
    )).scalars().first()
    return {
        "id": c.id,
        "customerId": c.customer_id,
        "customerName": cust.name if cust else None,
        "customerCode": (cust.merchant_code or cust.username) if cust else None,
        "supportId": c.support_id,
        "supportName": (agent.full_name or agent.name) if agent else None,
        "status": "QUEUED" if (c.status == "OPEN" and c.support_id is None) else c.status,
        "queued": c.status == "OPEN" and c.support_id is None,
        "createdAt": c.created_at.isoformat() + "Z",
        "assignedAt": (c.assigned_at.isoformat() + "Z") if c.assigned_at else None,
        "lastMessage": last.content if last else None,
        "lastAt": (last.created_at.isoformat() + "Z") if last else None,
    }


@router.get("/conversations")
async def list_conversations(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    """All conversations (active + queued). Admin scope: only conversations owned by / queued for
    the caller's own support members plus unassigned queue; Super Admin sees everything."""
    q = select(SupportConversation)
    if status == "open":
        q = q.where(SupportConversation.status == "OPEN")
    elif status == "queued":
        q = q.where(SupportConversation.status == "OPEN", SupportConversation.support_id.is_(None))
    convs = (await db.execute(q.order_by(SupportConversation.created_at.desc()))).scalars().all()

    # Restrict an Admin to their own members' conversations (+ the shared queue).
    if caller.role != UserRole.SUPER_ADMIN:
        my_members = {mid for (mid,) in (await db.execute(
            select(User.id).where(User.role == UserRole.SUPPORT_AGENT, User.created_by == caller.id)
        )).all()}
        convs = [c for c in convs if c.support_id in my_members or c.support_id is None]

    uid = {c.customer_id for c in convs} | {c.support_id for c in convs if c.support_id}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uid or {0})))).scalars().all()}
    return [await _conversation_out(db, c, users) for c in convs]


@router.post("/conversations/{conv_id}/reassign")
async def reassign_conversation(
    conv_id: int,
    data: ReassignConversationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    conv = (await db.execute(select(SupportConversation).where(SupportConversation.id == conv_id))).scalar_one_or_none()
    if not conv or conv.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open conversation not found")
    target = await _get_member(db, caller, data.supportId)  # enforces Admin can only pick own members
    agent = await support_routing.reassign_conversation(db, conv, target.id, actor_id=caller.id)
    await log_event(db, "SUPPORT_CONVERSATION_REASSIGNED",
                    f"{caller.name} reassigned conversation #{conv.id} to {agent.name}", actor=caller)
    await record_audit(db, "SUPPORT_CONVERSATION_REASSIGNED", actor=caller, entity_type="support",
                       entity_id=conv.id, new=agent.name, ip=_ip(request))
    return {"ok": True, "supportId": agent.id, "supportName": agent.full_name or agent.name}


@router.post("/conversations/{conv_id}/close")
async def close_conversation(
    conv_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    conv = (await db.execute(select(SupportConversation).where(SupportConversation.id == conv_id))).scalar_one_or_none()
    if not conv or conv.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open conversation not found")
    if caller.role != UserRole.SUPER_ADMIN and conv.support_id is not None:
        await _get_member(db, caller, conv.support_id)  # scope check
    await support_routing.close_conversation(db, conv, actor_id=caller.id)
    await log_event(db, "SUPPORT_CONVERSATION_CLOSED", f"{caller.name} closed conversation #{conv.id}", actor=caller)
    await record_audit(db, "SUPPORT_CONVERSATION_CLOSED", actor=caller, entity_type="support", entity_id=conv.id, ip=_ip(request))
    return {"ok": True}


# ─── Member self: availability toggle ─────────────────────────────────────────
@router.patch("/me/availability")
async def set_availability(
    data: AvailabilityRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    member: User = Depends(get_current_support),
):
    value = str(data.availability or "").upper()
    if value not in AVAILABILITY_VALUES:
        raise HTTPException(status_code=400, detail="availability must be AVAILABLE, BUSY or ON_BREAK")
    member.support_availability = value
    member.support_availability_at = datetime.utcnow()
    await presence.touch(db, member)
    await db.flush()
    if value == "AVAILABLE":
        await support_routing.drain_queue(db)
    if member.created_by:
        pretty = {"AVAILABLE": "Available", "BUSY": "Busy", "ON_BREAK": "On Break"}[value]
        db.add(Notification(user_id=member.created_by, message=f"Support member {member.name} is now {pretty}", icon="🎧"))
    await log_event(db, "SUPPORT_AVAILABILITY_CHANGED", f"{member.name} set availability to {value}", actor=member)
    await record_audit(db, "SUPPORT_AVAILABILITY_CHANGED", actor=member, entity_type="support", entity_id=member.id,
                       new=value, ip=_ip(request))
    return {"availability": value}


# ─── helpers ──────────────────────────────────────────────────────────────────
async def _requeue_member_conversations(db: AsyncSession, member_id: int) -> None:
    """Unassign a member's OPEN conversations (they go back to the queue) and drain to others."""
    convs = (await db.execute(
        select(SupportConversation).where(
            SupportConversation.support_id == member_id, SupportConversation.status == "OPEN"
        )
    )).scalars().all()
    now = datetime.utcnow()
    for c in convs:
        c.support_id = None
        c.queued_at = now
    await db.flush()
    await support_routing.drain_queue(db, now=now)


async def _one(db: AsyncSession, m: User) -> dict:
    now = datetime.utcnow()
    sessions = await presence.latest_sessions(db, [m.id])
    counts = await support_routing.active_counts(db, [m.id])
    cfg = await support_routing.get_config(db)
    return _member_out(m, sessions.get(m.id), counts.get(m.id, 0), cfg, now)
