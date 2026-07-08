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


def _kind(tx: Transaction) -> str:
    v = str(getattr(tx.type, "value", tx.type) or "").upper()
    if "DEPOSIT" in v:
        return "Deposit"
    if "WITHDRAWAL" in v:
        return "Withdrawal"
    if "SETTLEMENT" in v:
        return "Settlement"
    return "Transaction"


def _format(tx: Transaction, status_label: str, action: str | None, reason: str | None = None) -> str:
    """The fixed Clari5Pay Telegram layout: Membership ID / Reference ID / Transaction / Status,
    an optional Reason, an optional Action Required block, and the IST timestamp."""
    lines = [
        "📢 Clari5Pay Notification",
        "",
        f"Membership ID: {tx.member_id or '-'}",
        f"Reference ID: {tx.ref}",
        f"Transaction: {_kind(tx)}",
        f"Status: {status_label}",
    ]
    if reason:
        lines += ["", f"Reason: {reason}"]
    if action:
        lines += ["", "Action Required:", action]
    lines += ["", "Date & Time:", datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")]
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


async def notify(db: AsyncSession, tx: Transaction, target: str, status_label: str,
                 action: str | None = None, reason: str | None = None) -> None:
    """Send ONE Telegram message to the role responsible for the next workflow step. Demo-only and
    best-effort: writes a ``whatsapp_logs`` row per recipient (SENT / FAILED / no-telegram-linked)
    and swallows all errors so the workflow is never affected. Uses the caller's session, so the
    log rows commit together with the transaction."""
    if not (settings.is_demo and settings.telegram_configured):
        return
    try:
        users = await _recipients(db, tx, target)
        body = _format(tx, status_label, action, reason)
        event = f"{_kind(tx).lower()}:{target.lower()}"
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
