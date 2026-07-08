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


def _inr(amount) -> str:
    """Format an amount as ``INR 2,50,000.00`` using Indian digit grouping."""
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if n < 0 else ""
    n = abs(n)
    whole = int(n)
    dec = int(round((n - whole) * 100))
    if dec == 100:  # rounding spilled over
        whole, dec = whole + 1, 0
    s = str(whole)
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ",".join(groups) + "," + last3
    else:
        grouped = s
    return f"INR {sign}{grouped}.{dec:02d}"


def _row(label: str, value: str) -> str:
    """Aligned ``Label        : value`` line (colon aligned to a 13-char label column)."""
    return f"{label:<13} : {value}"


def _ts() -> str:
    return datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")


# Per-event message templates. Each returns the full Telegram text for one workflow step, shaped
# for the ONE recipient role that owns the next action. ``a`` = actor name (reviewer), ``r`` = reason.
def _build(tx: Transaction, event: str, actor: str | None = None, reason: str | None = None,
           requested_by: str | None = None) -> str:
    mid = tx.member_id or "-"
    ref = tx.ref
    amt = _inr(tx.amount)
    # "Requested By" is the person who raised the request (resolved by notify); fall back to the
    # creator username / business name if no personal name is available.
    who = requested_by or tx.creator_username or tx.merchant_name or "-"
    utr = tx.utr or "-"
    kind = _kind(tx)
    ts = _ts()

    if event == "deposit_request":            # → ADMIN (bank/UPI: upload account details next)
        return "\n".join([
            "🔔 New Deposit Request", "",
            _row("Membership ID", mid), _row("Reference ID", ref),
            _row("Amount", amt), _row("Requested By", who), "",
            "Action Required:", "Please upload the deposit account details.", "",
            f"Date & Time : {ts}",
        ])
    if event == "deposit_request_review":     # → SUPERVISOR (cash/crypto: proof already attached)
        return "\n".join([
            "📄 New Deposit — Verify Payment", "",
            _row("Membership ID", mid), _row("Reference ID", ref),
            _row("Amount", amt), _row("Requested By", who), "",
            "Action Required:", "Verify the payment and approve the deposit.", "",
            f"Date & Time : {ts}",
        ])
    if event == "withdrawal_request":         # → MANAGER
        return "\n".join([
            "💸 New Withdrawal Request", "",
            _row("Membership ID", mid), _row("Reference ID", ref), _row("Amount", amt), "",
            "Action Required:", "Verify the customer's bank account details.", "",
            f"Date & Time : {ts}",
        ])
    if event == "settlement_request":         # → ADMIN (our flow has no Manager step)
        return "\n".join([
            "💰 New Settlement Request", "",
            _row("Membership ID", mid), _row("Reference ID", ref), _row("Amount", amt), "",
            "Action Required:", "Review and approve the settlement.", "",
            f"Date & Time : {ts}",
        ])
    if event == "account_submitted":          # → USER
        return "\n".join([
            "🏦 Deposit Account Details Submitted", "",
            _row("Membership ID", mid), _row("Reference ID", ref), "",
            "Your deposit account details are ready.", "",
            "Please complete the payment and upload:", "",
            "• Payment Screenshot", "• UTR Number", "",
            f"Date & Time : {ts}",
        ])
    if event == "slip_submitted":             # → SUPERVISOR
        return "\n".join([
            "📄 Payment Slip Submitted", "",
            _row("Membership ID", mid), _row("Reference ID", ref), _row("Amount", amt), "",
            "The customer has uploaded:", "",
            "• Payment Screenshot", "• UTR Number", "",
            "Action Required:", "Verify the payment.", "",
            f"Date & Time : {ts}",
        ])
    if event == "supervisor_approved":        # → ADMIN
        return "\n".join([
            "✅ Deposit Approved by Supervisor", "",
            _row("Membership ID", mid), _row("Reference ID", ref), _row("Supervisor", actor or "-"), "",
            "Action Required:", "Complete the final deposit approval.", "",
            f"Date & Time : {ts}",
        ])
    if event == "manager_verified":           # → ADMIN
        return "\n".join([
            "✅ Withdrawal Verified by Manager", "",
            _row("Membership ID", mid), _row("Reference ID", ref), _row("Manager", actor or "-"), "",
            "Action Required:", "Complete the withdrawal payment.", "",
            f"Date & Time : {ts}",
        ])
    if event == "deposit_done":               # → USER
        return "\n".join([
            "✅ Deposit Successful", "",
            _row("Membership ID", mid), _row("Reference ID", ref), "",
            "Your deposit has been approved successfully.", "",
            f"Amount : {amt}", "",
            f"Date & Time : {ts}",
        ])
    if event == "withdrawal_done":            # → USER
        return "\n".join([
            "✅ Withdrawal Completed", "",
            _row("Membership ID", mid), _row("Reference ID", ref), "",
            f"Amount : {amt}", "",
            "UTR Number:", utr, "",
            f"Date & Time : {ts}",
        ])
    if event == "settlement_done":            # → USER
        return "\n".join([
            "✅ Settlement Completed", "",
            _row("Membership ID", mid), _row("Reference ID", ref), "",
            f"Amount : {amt}", "",
            "UTR Number:", utr, "",
            f"Date & Time : {ts}",
        ])
    if event == "returned":                   # → USER (returned for correction)
        return "\n".join([
            f"⚠ {kind} Requires Re-verification", "",
            _row("Membership ID", mid), _row("Reference ID", ref), "",
            "Reason:", reason or "-", "",
            f"Date & Time : {ts}",
        ])
    # default: rejection → USER
    return "\n".join([
        f"❌ {kind} Rejected", "",
        _row("Membership ID", mid), _row("Reference ID", ref), "",
        "Reason:", reason or "-", "",
        f"Date & Time : {ts}",
    ])


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
