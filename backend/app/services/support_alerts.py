"""Support-outage alerting: one Telegram message to the Super Admin the moment support goes dark.

The Merchant Portal header already reports whether support is reachable — the OR of the two pools
``support_routing.availability_summary`` evaluates (an eligible Customer Support member **or** an
eligible Admin). This module bolts a *state machine* onto that same answer so the Super Admin
learns about an outage without watching the pill.

What it is careful about
------------------------
**It never decides availability.** The answer is always the existing summary, derived live from
presence, manual Busy/On-Break and conversation capacity. Nothing here is hardcoded or cached as
an availability source; ``SupportAvailabilityState`` is a de-duplication latch, not a status.

**Exactly one alert per outage.** The alert fires on the AVAILABLE → UNAVAILABLE *transition*, not
on the unavailable *state*, so polling, page refreshes, many merchants and many workers cannot
multiply it. The transition is claimed by a compare-and-set UPDATE against a singleton row in its
own short transaction — ``SET state = :new WHERE id = 1 AND state = :previously_read`` — and only
the caller whose UPDATE actually matched a row owns the notification. A single UPDATE statement is
atomic, and concurrent writers serialise on the row and then re-evaluate the WHERE against the
committed value, so the loser matches nothing and stands down. Recovery (UNAVAILABLE → AVAILABLE)
is recorded but never notified, and re-arms the latch so the *next* outage alerts again.

**It can never break anything.** Every entry point is fully guarded and runs as a FastAPI
background task — after the response has been sent — so a slow or dead Telegram cannot add a
millisecond to the availability API, the Merchant Portal, the Support Center or login. A failed
send is logged (``whatsapp_logs`` + the event row) and dropped; there is no retry loop.

**Credentials stay server-side.** The bot token and the Super Admin chat id live in settings and
are read only here; no endpoint returns them and nothing reaches the frontend.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.models import (
    SupportAvailabilityEvent,
    SupportAvailabilityState,
    SystemLog,
    User,
    UserRole,
    WhatsAppLog,
)
from app.services import support_routing, whatsapp as wa
from app.services.tg_notify import _fmt_ist

log = logging.getLogger("clari5pay.support_alerts")

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"

NOTIFICATION_TYPE = "support_unavailable"


def _factory(session_factory=None):
    """The session factory to use — its own, never the caller's request session, so the latch
    commits independently of whatever transaction triggered the check. Injectable for tests."""
    return session_factory or AsyncSessionLocal


def build_message(now: datetime) -> str:
    """The outage alert. Plain text (the bot sends without parse_mode), IST timestamp."""
    return "\n".join([
        "🔴 Support Team Unavailable", "",
        "All eligible Admin and Customer Support members are currently unavailable.", "",
        "Merchant Portal support status:", "OFFLINE", "",
        "Time:", _fmt_ist(now), "",
        "Please check the Support Team availability.",
    ])


async def _recipients(db: AsyncSession) -> list[tuple[Optional[User], str]]:
    """(user, chat_id) pairs the outage alert goes to — Super Admin only.

    ``SUPPORT_ALERT_TELEGRAM_CHAT_ID`` overrides the lookup entirely when set, which is how a
    non-production stack is pointed at a safe test chat instead of a real Super Admin. Otherwise
    the recipients are the active Super Admin accounts that have linked Telegram through the
    existing self-service bot flow (``users.telegram_chat_id``) — no second bot, no new plumbing.
    """
    override = (settings.SUPPORT_ALERT_TELEGRAM_CHAT_ID or "").strip()
    if override:
        return [(None, c.strip()) for c in override.split(",") if c.strip()]
    rows = (await db.execute(select(User).where(
        User.role == UserRole.SUPER_ADMIN,
        User.active == True,                        # noqa: E712
        User.telegram_chat_id.isnot(None),
    ))).scalars().all()
    return [(u, u.telegram_chat_id) for u in rows]


async def _claim(new_state: str, summary: dict, now: datetime, factory) -> Optional[int]:
    """Atomically move the latch to ``new_state`` and record the transition.

    Returns the new event id when THIS caller won the transition (and therefore owns the
    notification), or None when there was nothing to claim — the latch already reads ``new_state``
    because nothing changed, or because another worker moved it a moment earlier.

    The first evaluation on a database that has never had the latch seeds it *without* treating it
    as a transition: a fresh deployment that starts out unavailable has not gone down, it has only
    just been observed for the first time, and paging the Super Admin for that would be noise.
    """
    # The latch is two-valued, so the state a transition INTO new_state must have come from is the
    # other one — which is what the compare-and-set below matches on, making `previous` the exact
    # value the winning UPDATE replaced rather than a guess about it.
    previous = AVAILABLE if new_state == UNAVAILABLE else UNAVAILABLE

    async with factory()() as db:
        # Write first, deliberately: a lone UPDATE is atomic, and concurrent claimers queue on the
        # row and then re-evaluate against the committed value instead of racing to upgrade a read
        # they have both already taken. Whoever matches a row owns the transition; everyone else
        # matches nothing and stands down — N pollers, N workers, ONE alert.
        moved = (await db.execute(
            update(SupportAvailabilityState)
            .where(SupportAvailabilityState.id == 1, SupportAvailabilityState.state == previous)
            .values(state=new_state, changed_at=now)
        )).rowcount

        if not moved:
            exists = (await db.execute(
                select(SupportAvailabilityState.id).where(SupportAvailabilityState.id == 1)
            )).scalar_one_or_none()
            if exists is None:
                db.add(SupportAvailabilityState(id=1, state=new_state, changed_at=now))
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()   # another worker seeded it in the same instant — fine
            return None

        outage = new_state == UNAVAILABLE
        support = summary.get("support") or {}
        admin = summary.get("admin") or {}
        event = SupportAvailabilityEvent(
            previous_status=previous,
            new_status=new_state,
            occurred_at=now,
            occurred_at_ist=_fmt_ist(now),
            available_admins=int(admin.get("available") or 0),
            available_support=int(support.get("available") or 0),
            # Recorded up front so the outage survives even if the send never completes; patched
            # with the real result by _notify().
            telegram_status="PENDING" if outage else None,
        )
        db.add(event)
        db.add(SystemLog(
            actor_name="system",
            action="SUPPORT_AVAILABILITY_TRANSITION",
            detail=(f"Support availability {previous} → {new_state} at {_fmt_ist(now)} "
                    f"(available: {event.available_support} Customer Support, "
                    f"{event.available_admins} Admin)"),
        ))
        await db.commit()
        return event.id


async def _finish(event_id: int, status: str, reason: Optional[str], factory) -> None:
    """Stamp the notification outcome onto the transition audit row."""
    async with factory()() as db:
        event = (await db.execute(
            select(SupportAvailabilityEvent).where(SupportAvailabilityEvent.id == event_id)
        )).scalar_one_or_none()
        if event:
            event.telegram_status = status
            event.telegram_failure_reason = reason
            await db.commit()


async def _notify(event_id: int, now: datetime, factory) -> None:
    """Send the outage alert once per recipient, log every attempt, record the outcome.

    Best-effort by construction: the recipient lookup and the delivery log each run in their own
    short session with the network calls in between (so no pooled connection is held across
    Telegram latency), and a failure is recorded rather than raised or retried.
    """
    if not settings.telegram_configured:
        await _finish(event_id, "NOT_CONFIGURED", "telegram bot token not configured", factory)
        return

    body = build_message(now)
    async with factory()() as db:
        targets = await _recipients(db)
    if not targets:
        await _finish(event_id, "NO_RECIPIENT", "no Super Admin has linked Telegram", factory)
        return

    results: list[tuple[Optional[User], str, bool, Optional[str], Optional[str]]] = []
    seen: set[str] = set()
    for user, chat in targets:
        if chat in seen:
            continue                                  # one chat, one message
        seen.add(chat)
        try:
            ok, mid, resp = await wa._send_telegram(chat, body)
        except Exception as exc:                      # network error, timeout, bad token …
            ok, mid, resp = False, None, repr(exc)
        results.append((user, chat, ok, mid, resp))

    async with factory()() as db:
        for user, chat, ok, mid, resp in results:
            db.add(WhatsAppLog(
                user_id=getattr(user, "id", None),
                username=getattr(user, "username", None),
                role="SUPER_ADMIN" if user is not None else "SUPPORT_ALERT",
                phone=chat,
                notification_type=NOTIFICATION_TYPE,
                message=body,
                status="SENT" if ok else "FAILED",
                provider="telegram",
                message_id=mid,
                delivery_status="sent" if ok else "failed",
                provider_response=resp,
                failure_reason=None if ok else resp,
            ))
        await db.commit()

    delivered = [r for r in results if r[2]]
    reasons = [str(r[4]) for r in results if not r[2] and r[4]]
    await _finish(
        event_id,
        "SENT" if delivered else "FAILED",
        None if delivered else ("; ".join(reasons)[:1000] or "send failed"),
        factory,
    )


async def observe(summary: dict, *, now: Optional[datetime] = None, session_factory=None) -> Optional[str]:
    """Feed a freshly computed availability summary to the outage state machine.

    Returns the transition it claimed — ``"OUTAGE"``, ``"RECOVERY"`` or None when there was none.
    Never raises: this runs behind user-facing endpoints and must not be able to affect them.
    """
    now = now or datetime.utcnow()

    def factory():
        return _factory(session_factory)

    try:
        new_state = AVAILABLE if summary.get("available") else UNAVAILABLE
        event_id = await _claim(new_state, summary, now, factory)
        if event_id is None:
            return None
        if new_state == AVAILABLE:
            return "RECOVERY"                     # recorded above; deliberately not notified
        await _notify(event_id, now, factory)
        return "OUTAGE"
    except Exception:
        log.exception("support availability alert check failed")
        return None


async def refresh(*, session_factory=None) -> Optional[str]:
    """Recompute availability from committed state, then run the state machine over it.

    Used by the callers that have just *changed* availability (a member going On Break, an admin
    stepping off support duty): running after their response has been sent means this reads the
    committed truth rather than their in-flight transaction.
    """
    try:
        async with _factory(session_factory)() as db:
            summary = await support_routing.availability_summary(db)
    except Exception:
        log.exception("support availability refresh failed")
        return None
    return await observe(summary, session_factory=session_factory)


def schedule(background: BackgroundTasks, summary: Optional[dict] = None) -> None:
    """Queue the outage check to run AFTER the response is sent.

    The single call-site pattern for the whole feature. Pass ``summary`` when the caller has
    already computed it on a read-only request (the availability endpoint); omit it after a write,
    so the check re-reads committed state.
    """
    if summary is None:
        background.add_task(refresh)
    else:
        background.add_task(observe, summary)
