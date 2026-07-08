"""Demo-only Telegram workflow notifications — single-recipient, next-step routing.

On the demo stack (ENVIRONMENT=demo) Telegram is the sole EXTERNAL notification channel:
WhatsApp/SMS mirroring is turned off (see ``app.services.whatsapp._deliver``) and each workflow
event sends ONE Telegram message to only the role responsible for the next step — never a
broadcast to everyone. In-app notifications (the bell) are left exactly as they are.

On production this module is inert (``settings.is_demo`` is False), so production notifications
are completely untouched. Every attempt (success, failure, or "no telegram linked") is written
to ``whatsapp_logs`` and any error is swallowed, so a notification can never break a workflow.

Routing targets:
  USER       → the requesting merchant user (tx.merchant_id)
  ADMIN      → every active Admin
  SUPERVISOR → the requesting business's Supervisors  (deposit review)
  MANAGER    → the requesting business's Managers      (withdrawal review)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import User, UserRole, Transaction, WhatsAppLog
from app.services.whatsapp import _send_telegram

log = logging.getLogger("clari5pay.telegram")

_IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_ist(dt) -> str:
    """Format a stored timestamp in IST as ``DD Mon YYYY, hh:mm AM/PM IST`` (e.g.
    ``08 Jul 2026, 12:51 PM IST``). Stored timestamps are naive UTC (``datetime.utcnow`` default),
    so a tz-naive value is treated as UTC before converting to IST. Falls back to "now" if the row
    has no usable timestamp."""
    if not isinstance(dt, datetime):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).strftime("%d %b %Y, %I:%M %p IST")


# Per-event "Action Required" line — the single next step owed by THIS recipient role (same wording
# used before the template simplification). Events that are purely informational to their recipient
# (successful completions, rejections, returns) have no next action and omit the block entirely.
_ACTIONS = {
    "deposit_request":        "Please upload the deposit account details.",       # → ADMIN
    "deposit_request_review": "Verify the payment and approve the deposit.",       # → SUPERVISOR
    "withdrawal_request":     "Verify the customer's bank account details.",       # → MANAGER
    "settlement_request":     "Review and approve the settlement.",                # → ADMIN
    "account_submitted":      "Complete the payment and upload the payment screenshot and UTR number.",  # → USER
    "slip_submitted":         "Verify the payment.",                               # → SUPERVISOR
    "supervisor_approved":    "Complete the final deposit approval.",              # → ADMIN
    "manager_verified":       "Complete the withdrawal payment.",                  # → ADMIN
}


# Single simplified Telegram template for EVERY workflow event: the four core fields (Reference ID,
# Requested By = Membership ID - Member Name, request creation Date & Time in IST) plus a per-role
# "Action Required" line naming the recipient's next step, closed by the standard footer.
# ``actor``/``reason`` are still accepted so the workflow callers (triggers/routing) stay unchanged.
def _build(tx: Transaction, event: str, actor: str | None = None, reason: str | None = None,
           requested_by: str | None = None) -> str:
    ref = tx.ref or "-"
    # "Requested By" = Membership ID - Member Name (e.g. "WININ20270 - B S NAGAPRASAD"). The member
    # name is resolved by notify() (full_name → username); fall back to creator/business only if
    # nothing else is available. Blank parts are dropped so the separator never dangles.
    mid = tx.member_id or None
    who = requested_by or tx.creator_username or tx.merchant_name or None
    requested = " - ".join(p for p in (mid, who) if p) or "-"
    # Always the request's stored creation time, converted to IST (never send-time / UTC).
    ts = _fmt_ist(getattr(tx, "created_at", None))
    lines = [
        "🔔 Clari5Pay Notification", "",
        "Reference ID:", ref, "",
        "Requested By:", requested, "",
        "Date & Time:", ts, "",
    ]
    action = _ACTIONS.get(event)
    if action:
        lines += ["Action Required:", action, ""]
    lines.append("Please login to Clari5Pay.")
    return "\n".join(lines)


async def _recipients(db: AsyncSession, tx: Transaction, target: str) -> list[User]:
    if target == "USER":
        u = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
        return [u] if u else []
    if target == "ADMIN":
        return list((await db.execute(
            select(User).where(User.role == UserRole.ADMIN, User.active == True)  # noqa: E712
        )).scalars().all())
    if target in ("SUPERVISOR", "MANAGER", "DEO"):
        merch = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
        if not merch:
            return []
        rows = (await db.execute(select(User).where(
            User.role == UserRole.MERCHANT, User.name == merch.name, User.active == True  # noqa: E712
        ))).scalars().all()
        return [u for u in rows if str(u.merchant_role or "").upper() == target]
    return []


async def notify(db: AsyncSession, tx: Transaction, target: str, event: str,
                 actor: str | None = None, reason: str | None = None) -> None:
    """Send ONE Telegram message to the role responsible for the next workflow step. ``event`` picks
    the message template (see ``_build``) and is also stored as the log's notification_type; ``actor``
    is the reviewer's name (supervisor/manager approvals) and ``reason`` the reject/return remark.
    Demo-only and best-effort: writes a ``whatsapp_logs`` row per recipient (SENT / FAILED /
    no-telegram-linked) and swallows all errors so the workflow is never affected. Uses the caller's
    session, so the log rows commit together with the transaction."""
    if not (settings.is_demo and settings.telegram_configured):
        return
    try:
        users = await _recipients(db, tx, target)
        # Resolve the person who raised the request (full_name preferred, then username) for the
        # "Requested By" line — falls back inside _build to creator username / business name.
        creator = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
        requested_by = (getattr(creator, "full_name", None) or getattr(creator, "username", None)) if creator else None
        body = _build(tx, event, actor=actor, reason=reason, requested_by=requested_by)
        seen: set[str] = set()
        for u in users:
            role_str = str(u.merchant_role or getattr(u.role, "value", u.role))
            chat = getattr(u, "telegram_chat_id", None)
            if not chat:
                # Per spec: record the delivery failure without affecting the workflow.
                db.add(WhatsAppLog(
                    user_id=u.id, username=u.username, role=role_str, phone=None,
                    notification_type=event, message=body, status="FAILED", provider="telegram",
                    delivery_status="failed", failure_reason="no telegram linked",
                ))
                continue
            if chat in seen:
                continue  # one person holding several target-role accounts → send once
            seen.add(chat)
            ok, mid, resp = False, None, None
            try:
                ok, mid, resp = await _send_telegram(chat, body)
            except Exception as e:
                resp = repr(e)
            db.add(WhatsAppLog(
                user_id=u.id, username=u.username, role=role_str, phone=chat,
                notification_type=event, message=body, status="SENT" if ok else "FAILED",
                provider="telegram", message_id=mid, delivery_status="sent" if ok else "failed",
                provider_response=resp, failure_reason=None if ok else resp,
            ))
    except Exception:
        log.exception("tg_notify failed for %s target=%s", getattr(tx, "ref", "?"), target)
