"""Support Management — Admin/Super Admin CRUD for Support Team members.

Support members are ``SUPPORT_AGENT``-role users enriched with a Support ID, department,
shift, availability (Available/Busy) and a set of assigned merchants. They keep the existing
no-OTP support-portal login and appear in Active Users. This module adds:

  • GET    /api/support-management/agents            — list visible members (+ derived status)
  • GET    /api/support-management/agents/stream     — SSE live feed (cards + table)
  • POST   /api/support-management/agents            — create a member
  • PATCH  /api/support-management/agents/{id}        — edit details
  • PATCH  /api/support-management/agents/{id}/toggle — activate / deactivate
  • POST   /api/support-management/agents/{id}/reset-password
  • PUT    /api/support-management/agents/{id}/merchants — replace assigned merchants
  • GET    /api/support-management/agents/{id}/profile  — full profile (session details)
  • DELETE /api/support-management/agents/{id}        — soft-archive (Super Admin only)
  • GET    /api/support-management/assignable-merchants — merchants the caller may assign
  • PATCH  /api/support-management/me/availability    — member sets own Available/Busy

Scope: Super Admin → all members; Admin → only members they created (``created_by``).
"""
import asyncio
import json
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db, AsyncSessionLocal
from app.core.deps import get_current_admin, get_current_super_admin, get_current_support
from app.core.security import get_password_hash
from app.core.passwords import assert_password_allowed, set_password
from app.models.models import User, UserRole, Notification, SupportAssignment, SupportMessage
from app.schemas.schemas import (
    SupportMemberCreate, SupportMemberUpdate, AssignMerchantsRequest, AvailabilityRequest,
    ReasonRequest,
)
from app.services import presence
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/support-management", tags=["support-management"])

# Stream tuning — mirrors active_users.py so the two presence feeds behave identically.
STREAM_TICK_SECONDS = 1.0
STREAM_FORCE_REFRESH_SECONDS = 15.0

DEPARTMENTS = {"Technical Support", "Payments", "Merchant Support", "Finance", "Compliance"}
SHIFTS = {"Morning", "Afternoon", "Night"}


def _ip(request: Request) -> str | None:
    return request.client.host if request and request.client else None


def _normalize_phone(raw: str | None) -> str | None:
    """E.164-ish: leading '+' + 8–15 digits. Empty → None. Same rule as users.update_profile."""
    raw = (raw or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not (8 <= len(digits) <= 15):
        raise HTTPException(status_code=400, detail="Enter a valid phone number with country code, e.g. +919812345678")
    return "+" + digits


async def _next_support_code(db: AsyncSession) -> str:
    """Next serial Support ID (SUP000001…), continuing after the current max (collision-safe)."""
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


AVAILABILITY_VALUES = ("AVAILABLE", "BUSY", "ON_BREAK")


def _derive_status(u: User, sess, now: datetime) -> str:
    """online (green) / busy (yellow) / break (red) / offline (gray). Offline is derived from
    presence; Available/Busy/On-Break is the member's manual availability while their session is live."""
    if not presence.is_online(sess, now):
        return "offline"
    avail = str(u.support_availability or "AVAILABLE").upper()
    if avail == "BUSY":
        return "busy"
    if avail == "ON_BREAK":
        return "break"
    return "online"


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


async def _assignable_merchants(db: AsyncSession, caller: User) -> list[User]:
    q = select(User).where(User.role == UserRole.MERCHANT, User.active == True)  # noqa: E712
    if caller.role != UserRole.SUPER_ADMIN:
        q = q.where(User.created_by == caller.id)
    return (await db.execute(q)).scalars().all()


async def assigned_merchant_ids(db: AsyncSession, support_id: int) -> set[int]:
    """Merchant ids a support member may service. Imported by support.py to gate the chat."""
    rows = (await db.execute(
        select(SupportAssignment.merchant_id).where(SupportAssignment.support_id == support_id)
    )).scalars().all()
    return set(rows)


async def _assignments_map(db: AsyncSession, support_ids: list[int]) -> dict[int, list[dict]]:
    if not support_ids:
        return {}
    rows = (await db.execute(
        select(SupportAssignment.support_id, User.id, User.name)
        .join(User, User.id == SupportAssignment.merchant_id)
        .where(SupportAssignment.support_id.in_(support_ids))
    )).all()
    out: dict[int, list[dict]] = {}
    for sid, mid, mname in rows:
        out.setdefault(sid, []).append({"id": mid, "name": mname})
    return out


# ─── Serialization ────────────────────────────────────────────────────────────
def _member_out(u: User, sess, assigned: list[dict], now: datetime) -> dict:
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
        "status": _derive_status(u, sess, now),
        "availability": str(u.support_availability or "AVAILABLE").upper(),
        "active": u.active,
        "assignedMerchants": assigned,
        "assignedMerchantCount": len(assigned),
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


async def _build_payload(db: AsyncSession, caller_role, caller_id: int) -> dict:
    members = (await db.execute(select(User).where(_visible_filter(caller_role, caller_id)))).scalars().all()
    now = datetime.utcnow()
    ids = [m.id for m in members]
    sessions = await presence.latest_sessions(db, ids)
    amap = await _assignments_map(db, ids)
    rows = [_member_out(m, sessions.get(m.id), amap.get(m.id, []), now) for m in members]
    rows.sort(key=lambda r: (r["fullName"] or "").lower())
    online = sum(1 for r in rows if r["status"] == "online")
    busy = sum(1 for r in rows if r["status"] == "busy")
    on_break = sum(1 for r in rows if r["status"] == "break")
    # Distinct merchants assigned across all visible members (card metric).
    assigned_ids: set[int] = set()
    for lst in amap.values():
        for m in lst:
            assigned_ids.add(m["id"])
    summary = {
        "members": len(rows),
        "online": online,
        "busy": busy,
        "onBreak": on_break,
        "offline": len(rows) - online - busy - on_break,
        "assignedMerchants": len(assigned_ids),
        "openTickets": 0,  # future-ready: wired once a ticketing model exists
    }
    return {"summary": summary, "members": rows}


def _signature(payload: dict) -> str:
    """Fingerprint of presence-relevant state (id + status + availability + session marks)."""
    return "|".join(
        f"{r['id']}:{r['status']}:{r['availability']}:{r['lastActivity']}:{r['logoutTime']}:{r['active']}:{r['assignedMerchantCount']}"
        for r in sorted(payload["members"], key=lambda r: r["id"])
    )


# ─── List / stream ────────────────────────────────────────────────────────────
@router.get("/agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    return await _build_payload(db, caller.role, caller.id)


@router.get("/agents/stream")
async def agents_stream(
    request: Request,
    caller: User = Depends(get_current_admin),
):
    """SSE feed — pushes a fresh payload whenever a member's presence/availability/assignment
    changes, plus a periodic refresh so derived fields keep ticking. Uses its own short-lived
    DB sessions so it never holds a pooled connection across idle waits."""
    caller_id, caller_role = caller.id, caller.role  # extract primitives (caller detaches below)

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


@router.get("/assignable-merchants")
async def assignable_merchants(
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    ms = await _assignable_merchants(db, caller)
    return [{"id": m.id, "name": m.name, "merchantCode": m.merchant_code, "username": m.username} for m in ms]


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

    # Assignments (only merchants the caller may assign are honored).
    allowed = {m.id for m in await _assignable_merchants(db, caller)}
    chosen = [mid for mid in (data.merchantIds or []) if mid in allowed]
    for mid in chosen:
        db.add(SupportAssignment(support_id=member.id, merchant_id=mid, assigned_by=caller.id))

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


# ─── Assign merchants (replace set) ───────────────────────────────────────────
@router.put("/agents/{member_id}/merchants")
async def assign_merchants(
    member_id: int,
    data: AssignMerchantsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    m = await _get_member(db, caller, member_id)
    allowed = {mm.id for mm in await _assignable_merchants(db, caller)}
    chosen = [mid for mid in (data.merchantIds or []) if mid in allowed]
    await db.execute(delete(SupportAssignment).where(SupportAssignment.support_id == m.id))
    for mid in chosen:
        db.add(SupportAssignment(support_id=m.id, merchant_id=mid, assigned_by=caller.id))
    await db.flush()
    db.add(Notification(user_id=m.id, message=f"Your assigned merchants were updated ({len(chosen)})", icon="🎧"))
    await log_event(db, "SUPPORT_MERCHANTS_ASSIGNED", f"{caller.name} set {len(chosen)} merchant(s) for support member \"{m.name}\"", actor=caller)
    await record_audit(db, "SUPPORT_MERCHANTS_ASSIGNED", actor=caller, entity_type="support", entity_id=m.id,
                       new=f"{len(chosen)} merchants", ip=_ip(request))
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
    # Active conversations = assigned merchants that have at least one chat message.
    mids = await assigned_merchant_ids(db, m.id)
    active = 0
    if mids:
        rows = (await db.execute(
            select(SupportMessage.merchant_id).where(SupportMessage.merchant_id.in_(mids)).distinct()
        )).scalars().all()
        active = len(rows)
    out["activeConversations"] = active
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
    await db.execute(delete(SupportAssignment).where(SupportAssignment.support_id == m.id))
    await presence.end_session(db, m)
    await db.flush()
    await log_event(db, "SUPPORT_ARCHIVED", f"Support member \"{m.name}\" deleted (archived) by {sa.name}", actor=sa)
    await record_audit(db, "SUPPORT_ARCHIVED", actor=sa, entity_type="support", entity_id=m.id,
                       new="archived", ip=_ip(request))
    return {"message": f"{m.name} removed."}


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
    await presence.touch(db, member)  # a manual availability change also counts as activity
    await db.flush()
    if member.created_by:
        pretty = {"AVAILABLE": "Available", "BUSY": "Busy", "ON_BREAK": "On Break"}[value]
        db.add(Notification(user_id=member.created_by, message=f"Support member {member.name} is now {pretty}", icon="🎧"))
    await log_event(db, "SUPPORT_AVAILABILITY_CHANGED", f"{member.name} set availability to {value}", actor=member)
    await record_audit(db, "SUPPORT_AVAILABILITY_CHANGED", actor=member, entity_type="support", entity_id=member.id,
                       new=value, ip=_ip(request))
    return {"availability": value}


# ─── helper: serialize one member with its live session + assignments ─────────
async def _one(db: AsyncSession, m: User) -> dict:
    now = datetime.utcnow()
    sessions = await presence.latest_sessions(db, [m.id])
    amap = await _assignments_map(db, [m.id])
    return _member_out(m, sessions.get(m.id), amap.get(m.id, []), now)
