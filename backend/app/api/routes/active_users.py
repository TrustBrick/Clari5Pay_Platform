"""Active Users (real-time presence) — session-metadata only, never tokens/secrets.

Presence is polled by the client (see the existing usePoll architecture). A heartbeat keeps
the caller's session fresh; the list endpoint computes online/offline from the newest session
per user. Scope: Super Admin → everyone; Admin → only users of merchants they created.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.deps import get_current_user, get_current_admin
from app.models.models import User, UserRole
from app.services import presence

router = APIRouter(prefix="/api/active-users", tags=["active-users"])


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


@router.get("")
async def active_users(
    db: AsyncSession = Depends(get_db),
    caller: User = Depends(get_current_admin),
):
    """Real-time presence overview. Super Admin sees all users; an Admin sees only the users
    belonging to merchants they created (plus themselves)."""
    if caller.role == UserRole.SUPER_ADMIN:
        users = (await db.execute(select(User))).scalars().all()
    else:
        # Admin: their own merchants' users + themselves. Never another admin's users.
        users = (await db.execute(
            select(User).where(
                (User.id == caller.id) |
                ((User.role == UserRole.MERCHANT) & (User.created_by == caller.id))
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
        rows.append({
            "id": u.id,
            "name": _display_name(u),
            "username": u.username,
            "merchant": u.name if u.role == UserRole.MERCHANT else None,
            "role": u.role.value if hasattr(u.role, "value") else u.role,
            "merchantRole": u.merchant_role,
            "phone": u.phone,
            "email": u.email,
            "avatar": u.avatar,
            "country": u.country,
            "status": "online" if online else "offline",
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
