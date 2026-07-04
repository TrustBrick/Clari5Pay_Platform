"""Session presence tracking for the Active Users feature.

Presence is derived from the ``user_sessions`` table plus a lightweight heartbeat the
client polls (the app already uses polling — see usePoll). No WebSockets, no per-request
DB writes: login/logout create/close a session row, and a periodic heartbeat bumps
``last_activity_at``. A user is ONLINE when they have an active (not-logged-out) session
whose last_activity is within ONLINE_WINDOW. Only session metadata is stored — never tokens.

All helpers are defensive (try/except): presence tracking must NEVER break login/logout.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import User, UserSession

# A session counts as online while its heartbeat is at most this old. The client heartbeats
# every ~25s, so a 90s window tolerates a couple of missed beats before flipping to offline.
ONLINE_WINDOW = timedelta(seconds=90)


def is_online(sess: "UserSession | None", now: datetime | None = None) -> bool:
    if not sess or not sess.active or sess.logout_at is not None:
        return False
    now = now or datetime.utcnow()
    return (now - sess.last_activity_at) <= ONLINE_WINDOW


def parse_user_agent(ua: str | None) -> dict:
    """Best-effort device / browser / OS from a User-Agent string (display only)."""
    low = (ua or "").lower()
    if "edg" in low:
        browser = "Edge"
    elif "opr" in low or "opera" in low:
        browser = "Opera"
    elif "firefox" in low:
        browser = "Firefox"
    elif "chrome" in low or "chromium" in low:
        browser = "Chrome"
    elif "safari" in low:
        browser = "Safari"
    else:
        browser = "Unknown"
    if "windows" in low:
        os_name = "Windows"
    elif "android" in low:
        os_name = "Android"
    elif "iphone" in low or "ipad" in low or "ios" in low:
        os_name = "iOS"
    elif "mac os" in low or "macintosh" in low:
        os_name = "macOS"
    elif "linux" in low:
        os_name = "Linux"
    else:
        os_name = "Unknown"
    if "ipad" in low or "tablet" in low:
        device = "Tablet"
    elif "mobile" in low or "android" in low or "iphone" in low:
        device = "Mobile"
    else:
        device = "Desktop"
    return {"device": device, "browser": browser, "os": os_name}


async def start_session(db: AsyncSession, user: User, ip: str | None, user_agent: str | None) -> None:
    """On login: close any prior active sessions (single active session per user) and open a new one."""
    try:
        now = datetime.utcnow()
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.active == True)  # noqa: E712
            .values(active=False, logout_at=now)
        )
        db.add(UserSession(
            user_id=user.id, login_at=now, last_activity_at=now,
            active=True, ip_address=(ip or None), user_agent=(user_agent or None),
        ))
        await db.flush()
    except Exception:  # presence must never block authentication
        pass


async def end_session(db: AsyncSession, user: User) -> None:
    """On logout: close the user's active session(s)."""
    try:
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.active == True)  # noqa: E712
            .values(active=False, logout_at=datetime.utcnow())
        )
        await db.flush()
    except Exception:
        pass


async def touch(db: AsyncSession, user: User) -> None:
    """Heartbeat: bump last_activity on the user's current active session (keeps them online)."""
    try:
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.active == True)  # noqa: E712
            .values(last_activity_at=datetime.utcnow())
        )
        await db.flush()
    except Exception:
        pass


async def latest_sessions(db: AsyncSession, user_ids: list[int]) -> dict[int, UserSession]:
    """Return each user's most-recent session row, keyed by user_id (single query)."""
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(UserSession).where(UserSession.user_id.in_(user_ids)).order_by(UserSession.id.asc())
    )).scalars().all()
    latest: dict[int, UserSession] = {}
    for s in rows:            # ascending id → last write wins = newest session
        latest[s.user_id] = s
    return latest
