"""Active Users (real-time presence) — session-metadata only, never tokens/secrets.

Two delivery paths, same data:
  • GET  /api/active-users          — one-shot snapshot (polled by the client as a fallback).
  • GET  /api/active-users/stream   — Server-Sent Events push: the server watches the
    user_sessions table and streams a fresh snapshot the instant presence changes (login,
    logout, heartbeat), so every open Active Users page updates in ~1s without a poll.
  • POST /api/active-users/heartbeat — keeps the caller's own session marked online.

Presence is computed from the newest session per user. Scope: Super Admin → everyone;
Admin → only users of merchants they created (plus themselves), never another admin's.
"""
import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db, AsyncSessionLocal
from app.core.deps import get_current_user, get_current_admin
from app.models.models import User, UserRole
from app.services import presence

router = APIRouter(prefix="/api/active-users", tags=["active-users"])

# Stream tuning. The loop re-checks presence every tick and pushes only when something changed;
# a forced refresh guarantees the derived fields (session duration, "x min ago") keep ticking and
# doubles as a keep-alive so proxies never see the connection go idle.
STREAM_TICK_SECONDS = 1.0
STREAM_FORCE_REFRESH_SECONDS = 15.0


@router.post("/heartbeat")
async def heartbeat(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Client heartbeat — keeps the caller marked online (bumps last_activity)."""
    await presence.touch(db, current_user)
    return {"ok": True}


def _display_name(u: User) -> str:
    return (u.full_name or "").strip() or (u.name or "").strip() or u.username


async def _build_payload(db: AsyncSession, caller_id: int, caller_role) -> dict:
    """Compute the full presence overview for a caller. Shared by the snapshot and the stream."""
    if caller_role == UserRole.SUPER_ADMIN:
        users = (await db.execute(
            select(User).where(User.support_archived == False)  # noqa: E712
        )).scalars().all()
    else:
        # Admin: their own merchants' users + their own support members + themselves.
        # Never another admin's users. Archived support members are excluded.
        users = (await db.execute(
            select(User).where(
                (User.id == caller_id) |
                ((User.role == UserRole.MERCHANT) & (User.created_by == caller_id)) |
                ((User.role == UserRole.SUPPORT_AGENT) & (User.created_by == caller_id)
                 & (User.support_archived == False))  # noqa: E712
            )
        )).scalars().all()

    now = datetime.utcnow()
    sessions = await presence.latest_sessions(db, [u.id for u in users])

    rows = []
    online_count = 0
    logged_in_count = 0
    for u in users:
        s = sessions.get(u.id)
        online = presence.is_online(s, now)
        if online:
            online_count += 1
        if s and s.active and s.logout_at is None:
            logged_in_count += 1
        ua = presence.parse_user_agent(s.user_agent if s else None)
        # Session duration (seconds): live for an active session, else login→logout span.
        duration = None
        if s:
            end = now if (s.active and s.logout_at is None) else (s.logout_at or s.last_activity_at)
            duration = max(0, int((end - s.login_at).total_seconds()))
        # Support members carry a manual availability (Available/Busy) shown while online.
        is_support = u.role == UserRole.SUPPORT_AGENT
        availability = str(u.support_availability or "AVAILABLE").upper() if is_support else None
        if online and is_support and availability == "BUSY":
            status = "busy"
        elif online and is_support and availability == "ON_BREAK":
            status = "break"
        else:
            status = "online" if online else "offline"
        rows.append({
            "id": u.id,
            "name": _display_name(u),
            "username": u.username,
            "merchant": u.name if u.role == UserRole.MERCHANT else None,
            "role": u.role.value if hasattr(u.role, "value") else u.role,
            "merchantRole": u.merchant_role,
            "supportCode": u.support_code if is_support else None,
            "availability": availability,
            "phone": u.phone,
            "email": u.email,
            "avatar": u.avatar,
            "country": u.country,
            "status": status,
            "loginTime": (s.login_at.isoformat() + "Z") if s else None,
            "lastActivity": (s.last_activity_at.isoformat() + "Z") if s else None,
            "lastSeen": (s.last_activity_at.isoformat() + "Z") if s else None,
            "logoutTime": (s.logout_at.isoformat() + "Z") if s and s.logout_at else None,
            "sessionDuration": duration,
            "ip": s.ip_address if s else None,
            "device": ua["device"] if s else None,
            "browser": ua["browser"] if s else None,
            "os": ua["os"] if s else None,
        })

    total = len(rows)
    summary = {
        "online": online_count,
        "offline": total - online_count,
        "totalLoggedIn": logged_in_count,
        "totalRegistered": total,
    }

    # Merchant-company status (MERCHANT-role users grouped by business name).
    biz: dict[str, dict] = {}
    for r in rows:
        if not r["merchant"]:
            continue
        b = biz.setdefault(r["merchant"], {"name": r["merchant"], "online": 0, "offline": 0, "total": 0})
        b["total"] += 1
        if r["status"] == "online":
            b["online"] += 1
        else:
            b["offline"] += 1
    merchants = sorted(biz.values(), key=lambda b: b["name"].lower())
    for b in merchants:
        b["status"] = "Online" if b["online"] > 0 else "Offline"

    return {"summary": summary, "merchants": merchants, "users": rows}


def _signature(payload: dict) -> str:
    """A cheap fingerprint of the *presence-relevant* state (who's online + their session marks).
    Excludes derived, always-moving fields like sessionDuration so we push on real changes only."""
    return "|".join(
        f"{r['id']}:{r['status']}:{r['lastActivity']}:{r['logoutTime']}"
        for r in sorted(payload["users"], key=lambda r: r["id"])
    )


@router.get("")
async def active_users(
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    """Real-time presence overview (one-shot). Super Admin sees all users; an Admin sees only the
    users belonging to merchants they created (plus themselves)."""
    return await _build_payload(db, caller.id, caller.role)


@router.get("/stream")
async def active_users_stream(
    request: Request,
    caller: User = Depends(get_current_admin),
):
    """SSE presence stream. Pushes a fresh snapshot whenever presence changes (≈1s latency) plus a
    periodic refresh so derived fields keep ticking. Uses its own short-lived DB sessions so it
    never holds a pooled connection across the idle waits."""
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
                    payload = await _build_payload(db, caller_id, caller_role)
                sig = _signature(payload)
                if sig != last_sig or (tnow - last_push) >= STREAM_FORCE_REFRESH_SECONDS:
                    last_sig, last_push = sig, tnow
                    yield f"data: {json.dumps(payload)}\n\n"
            except Exception:
                # A transient DB hiccup must not kill the stream — just skip this tick.
                pass
            await asyncio.sleep(STREAM_TICK_SECONDS)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # tell nginx not to buffer the stream
            "Content-Encoding": "identity",  # opt out of GZipMiddleware (it would buffer/break SSE)
        },
    )
