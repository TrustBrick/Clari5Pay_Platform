"""WhatsApp notification integration — mirrors every in-app notification to the recipient.

Provider-agnostic. Hooked into the ORM via a session ``after_commit`` listener, so NO
existing notification code changes: whenever a Notification row is committed for any user
(with ``whatsapp_enabled`` and a phone number), the same message is ALSO sent to that user's
WhatsApp — asynchronously, off the request path, retried, and never affecting the business
transaction. The recipient is whoever received the in-app notification (no hardcoded roles),
so future workflow/recipient changes are mirrored automatically. Per-role toggles act only as
an optional filter. Every attempt is written to ``whatsapp_logs``.

The feature is inert until a provider + token are configured (WHATSAPP_* env). Adding a new
provider only means extending ``_send`` — the notification logic never changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import User, UserRole, Notification, WhatsAppLog, AppSetting, Transaction

log = logging.getLogger("clari5pay.whatsapp")

# Loop captured at startup so the (possibly greenlet-driven) commit event can schedule work safely.
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_INSTALLED = False

# De-duplication: one transaction event fires a separate notification per recipient, and several
# recipients can share the same phone number (e.g. one person holding multiple accounts). Without
# this, that phone would receive identical messages several times. We remember each (phone, message)
# just sent and skip repeats within a short window. In-process + single-threaded asyncio, so the
# check-and-set below is atomic (no await between check and set).
_recent_sends: dict[tuple[str, str], float] = {}
_DEDUP_WINDOW = 120.0  # seconds

# A transaction reference token in a message: 2–4 uppercase letters + digits. Matches both the
# fixed prefixes (DEP/WIT/SET…) and merchant-specific codes (CLD/CLP/ABC…). Type is resolved from
# the linked transaction (below), not the prefix, so it stays correct whatever the prefix is.
_REF_RE = re.compile(r"\b[A-Z]{2,4}\d{4,}\b")

# ── Global per-role toggle (Admin → Settings → Notifications) ────────────────────
# Which roles receive WhatsApp, configurable at runtime without code changes. Stored as a
# JSON map in app_settings. Pure-mirror model: every role defaults ON — the toggles are an
# OPTIONAL filter an admin can use to switch a role off, not a hardcoded allow-list.
WA_ROLE_KEYS = ["ADMIN", "SUPERVISOR", "MANAGER", "MERCHANT",
                "DATA_OPERATOR", "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR"]
WA_DEFAULT_ROLES = {k: True for k in WA_ROLE_KEYS}
_WA_ROLES_KEY = "whatsapp_roles"


def _role_key(user: User) -> Optional[str]:
    """The per-role settings key for a user (None = never eligible, e.g. Super Admin/support)."""
    if user.role == UserRole.ADMIN:
        return "ADMIN"
    if user.role == UserRole.MERCHANT:
        mr = str(user.merchant_role or "").upper()
        if mr == "DEO":
            return "DATA_OPERATOR"
        if mr in ("SUPERVISOR", "MANAGER", "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR"):
            return mr
        return "MERCHANT"
    return None


async def get_role_settings(db: AsyncSession) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _WA_ROLES_KEY))).scalar_one_or_none()
    data = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
        except Exception:
            data = {}
    return {k: bool(data.get(k, WA_DEFAULT_ROLES.get(k, True))) for k in WA_ROLE_KEYS}


async def set_role_settings(db: AsyncSession, mapping: dict) -> dict:
    current = await get_role_settings(db)
    for k, v in (mapping or {}).items():
        if k in WA_ROLE_KEYS:
            current[k] = bool(v)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _WA_ROLES_KEY))).scalar_one_or_none()
    val = json.dumps(current)
    if row:
        row.value = val
    else:
        db.add(AppSetting(key=_WA_ROLES_KEY, value=val))
    await db.flush()
    return current


def _eligible(user: Optional[User], role_settings: dict) -> bool:
    """Pure mirror: any recipient with a phone and their personal preference on receives the
    WhatsApp copy of their in-app notification — regardless of role. The per-role toggles are an
    OPTIONAL filter (default ON): an admin can switch a specific role off, but nothing is
    hardcoded, so future workflow/recipient changes are mirrored automatically. Roles with no
    settings key (Super Admin / Support) are always mirrored."""
    if not user or not user.whatsapp_enabled or not (user.phone or "").strip():
        return False
    rk = _role_key(user)
    if rk is None:
        return True
    return role_settings.get(rk, True)


# ── Per-event toggle (which events trigger WhatsApp) ────────────────────────────
WA_EVENT_KEYS = ["DEPOSIT", "WITHDRAWAL", "SETTLEMENT", "MERCHANT_CREATED", "USER_CREATED",
                 "ACCOUNT_ASSIGNED", "PASSWORD_CHANGED", "LOGIN_ALERTS", "SECURITY_ALERTS", "OTHER"]
_WA_EVENTS_KEY = "whatsapp_events"


def _event_key(message: str) -> str:
    """Best-effort classification of a notification into an event category (the after_commit hook
    only sees the message text, so we match on the reference/keywords)."""
    m = (message or "").lower()
    t = _notif_type(message)
    if t:
        return t.upper()  # DEPOSIT / WITHDRAWAL / SETTLEMENT
    if "merchant" in m and "created" in m:
        return "MERCHANT_CREATED"
    if "created" in m and ("admin" in m or "user" in m or "account was created" in m):
        return "USER_CREATED"
    if "assigned" in m:
        return "ACCOUNT_ASSIGNED"
    if "password" in m:
        return "PASSWORD_CHANGED"
    if "locked" in m or "failed login" in m or "unlocked" in m:
        return "SECURITY_ALERTS"
    if "signed in" in m or "login" in m:
        return "LOGIN_ALERTS"
    return "OTHER"


async def get_event_settings(db: AsyncSession) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _WA_EVENTS_KEY))).scalar_one_or_none()
    data = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
        except Exception:
            data = {}
    return {k: bool(data.get(k, True)) for k in WA_EVENT_KEYS}   # default: every event on


async def set_event_settings(db: AsyncSession, mapping: dict) -> dict:
    current = await get_event_settings(db)
    for k, v in (mapping or {}).items():
        if k in WA_EVENT_KEYS:
            current[k] = bool(v)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _WA_EVENTS_KEY))).scalar_one_or_none()
    val = json.dumps(current)
    if row:
        row.value = val
    else:
        db.add(AppSetting(key=_WA_EVENTS_KEY, value=val))
    await db.flush()
    return current


def _notif_type(message: str) -> Optional[str]:
    m = _REF_RE.search(message or "")
    if not m:
        return None
    return {"DEP": "deposit", "WIT": "withdrawal", "SET": "settlement"}.get(m.group(0)[:3])


# Human-friendly status labels for the WhatsApp message (raw TxStatus enum → readable text).
_STATUS_LABEL = {
    "PENDING": "Pending Approval", "PENDING_APPROVAL": "Pending Approval",
    "SUPERVISOR_REVIEW": "Under Supervisor Review", "MANAGER_REVIEW": "Under Manager Review",
    "ACCOUNT_REQUESTED": "Requested", "ACCOUNT_SUBMITTED": "Account Details Sent",
    "SLIP_SUBMITTED": "Slip Submitted", "RESUBMITTED": "Resubmitted",
    "ADMIN_APPROVED": "Approved", "DEPOSITED": "Approved",
    "COMPLETED": "Completed", "SUCCESSFUL": "Completed",
    "REJECTED": "Rejected", "SA_REJECTED": "Rejected", "CANCELLED": "Cancelled",
}


def _money(value) -> Optional[str]:
    try:
        return f"₹{float(value):,.2f}"          # ₹25,000.00
    except (TypeError, ValueError):
        return None


def _tx_kind(tx_type) -> Optional[str]:
    v = str(getattr(tx_type, "value", tx_type) or "").upper()
    if "DEPOSIT" in v:
        return "Deposit"
    if "WITHDRAWAL" in v:
        return "Withdrawal"
    if "SETTLEMENT" in v:
        return "Settlement"
    return None


def _format(message: str, tx: Optional["Transaction"] = None) -> str:
    """Company-branded message that MIRRORS the in-app notification, enriched with the linked
    transaction's details (Business / Transaction / Reference / Amount / Status) when available.
    Falls back to just the in-app text + reference when there is no transaction (e.g. account /
    security notifications)."""
    company = settings.WHATSAPP_COMPANY_NAME or "Clari5Pay"
    when = datetime.now().strftime("%d-%b-%Y %I:%M %p")
    body = (message or "").strip()
    # Demo: hide the business name in WhatsApp — substitute the creating user's ID (e.g. MID000001)
    # in the message body and show it as "User ID" instead of "Business". Production is unchanged.
    hide_business = bool(settings.is_demo and tx is not None
                         and getattr(tx, "merchant_name", None) and getattr(tx, "agent_code", None))
    if hide_business:
        body = body.replace(tx.merchant_name, tx.agent_code)
    lines = [f"{company} Notification", "", body]
    if tx is not None:
        details = []
        if hide_business:
            details.append(f"User ID: {tx.agent_code}")
        elif getattr(tx, "merchant_name", None):
            details.append(f"Business: {tx.merchant_name}")
        kind = _tx_kind(tx.type)
        if kind:
            details.append(f"Transaction: {kind}")
        details.append(f"Reference: {tx.ref}")
        amt = _money(getattr(tx, "amount", None))
        if amt:
            details.append(f"Amount: {amt}")
        status = _STATUS_LABEL.get(str(getattr(tx.status, "value", tx.status) or "").upper())
        if status:
            details.append(f"Status: {status}")
        if details:
            lines += [""] + details
    else:
        ref = _REF_RE.search(message or "")
        if ref:
            lines += ["", f"Transaction: {ref.group(0)}"]
    lines += ["", f"Date: {when}", "", "Please log in to Clari5Pay for complete details."]
    return "\n".join(lines)


def _template_param(text: str) -> str:
    """Meta rejects template body parameters that contain newlines, tabs or >4 consecutive
    spaces. Flatten the enriched multi-line message into a single valid parameter so a simple
    one-variable ({{1}}) approved template works. A production template with discrete variables
    (Business / Amount / Status …) is preferable — see the deploy notes — but this keeps the
    single-parameter path Meta-valid rather than latently rejected."""
    s = re.sub(r"\s*[\r\n]+\s*", " · ", text or "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip(" ·")


def _extract_message_id(provider: str, body: dict) -> Optional[str]:
    try:
        if provider == "meta":
            return body["messages"][0]["id"]
        if provider == "twilio":
            return body.get("sid")
    except Exception:
        pass
    return None


async def _send(phone: str, body: str) -> tuple[bool, Optional[str], str]:
    """Dispatch one message via the configured provider. Returns (ok, message_id, provider_response)."""
    provider = settings.WHATSAPP_PROVIDER.lower()
    to = re.sub(r"[^\d]", "", phone or "")   # E.164 digits only
    if not to:
        return False, None, "no phone digits"
    if provider == "meta":
        url = settings.WHATSAPP_API_URL or f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
        if settings.WHATSAPP_TEMPLATE:
            # Business-initiated: approved template with the message as the single body parameter.
            payload = {
                "messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {
                    "name": settings.WHATSAPP_TEMPLATE,
                    "language": {"code": settings.WHATSAPP_LANG or "en"},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": _template_param(body)}]}],
                },
            }
        else:
            # Free text — only reaches users inside the 24h session window (dev/sandbox).
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, headers=headers, json=payload)
    elif provider == "twilio":
        sid = settings.WHATSAPP_ACCOUNT_SID
        url = settings.WHATSAPP_API_URL or f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = {"To": f"whatsapp:+{to}"}
        # Address either via a Messaging Service (production sender pool) or the from-number.
        if settings.WHATSAPP_MESSAGING_SERVICE_SID:
            data["MessagingServiceSid"] = settings.WHATSAPP_MESSAGING_SERVICE_SID
        else:
            data["From"] = f"whatsapp:{settings.WHATSAPP_PHONE_ID}"
        if settings.whatsapp_use_template:
            # Business-initiated (production) path — approved Content Template addressed by
            # ContentSid, with the flattened message as the single {{1}} variable. Reaches users
            # outside the 24h session window (no free text). Demo-gated so prod is untouched.
            data["ContentSid"] = settings.WHATSAPP_CONTENT_SID
            data["ContentVariables"] = json.dumps({"1": _template_param(body)})
        else:
            data["Body"] = body                        # free text (24h session / sandbox)
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, data=data, auth=(sid, settings.WHATSAPP_TOKEN))
    else:
        return False, None, f"unknown WhatsApp provider '{provider}'"
    ok = r.status_code < 300
    msg_id = None
    try:
        msg_id = _extract_message_id(provider, r.json()) if ok else None
    except Exception:
        msg_id = None
    return ok, msg_id, f"{r.status_code} {r.text[:500]}"


# Exceptions that mean the request never reached the provider (connection never established), so
# no message could have been created — safe to retry without risking a duplicate. A ReadTimeout /
# protocol error, by contrast, means the request WAS sent and the response was lost: the provider
# may already have accepted it, so we must NOT retry those.
_RETRYABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _status_code(resp: Optional[str]) -> Optional[int]:
    """Leading HTTP status from the '<code> <text>' provider-response string."""
    try:
        return int((resp or "").split(" ", 1)[0])
    except (ValueError, IndexError, AttributeError):
        return None


async def _deliver(user_id: int, message: str) -> None:
    """Look up the recipient, send (with retries) and log the attempt — all in its own session
    so it never touches the request's transaction. Any error is swallowed."""
    from app.db.session import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            role_settings = await get_role_settings(db)
            if not _eligible(user, role_settings):
                return  # role/user disabled or no phone — nothing to do
            # Find the linked transaction FIRST (any ref prefix — robust to merchant-specific
            # codes like CLD000010), so we classify the event by its real type and enrich the
            # message. A self-contained read, no notification/workflow changes.
            tx = None
            ref_m = _REF_RE.search(message or "")
            if ref_m:
                tx = (await db.execute(
                    select(Transaction).where(Transaction.ref == ref_m.group(0))
                )).scalar_one_or_none()
            kind = _tx_kind(tx.type) if tx is not None else None       # Deposit/Withdrawal/Settlement
            ntype = (kind.lower() if kind else None) or _notif_type(message)
            event_key = ntype.upper() if ntype in ("deposit", "withdrawal", "settlement") else _event_key(message)
            if not (await get_event_settings(db)).get(event_key, True):
                return  # this event type is switched off in Settings
            body = _format(message, tx)
            # Skip if this exact message was just sent to this phone (a shared number). Atomic
            # check-and-set (no await in between) so concurrent deliveries dedupe correctly.
            phone_key = re.sub(r"\D", "", user.phone or "")
            if phone_key:
                now = time.monotonic()
                for k, ts in list(_recent_sends.items()):
                    if now - ts > _DEDUP_WINDOW:
                        _recent_sends.pop(k, None)
                dkey = (phone_key, message or "")
                if dkey in _recent_sends:
                    return  # duplicate to the same phone from the same event — skip
                _recent_sends[dkey] = now
            ok, msg_id, resp, reason = False, None, None, None
            attempts = max(1, (settings.WHATSAPP_RETRIES or 0) + 1)
            used = 0
            for i in range(attempts):
                used = i + 1
                try:
                    ok, msg_id, resp = await _send(user.phone, body)
                    if ok:
                        reason = None
                        break
                    reason = resp
                    code = _status_code(resp)
                    # Only 429/5xx are transient (no message created). A 4xx is a deterministic
                    # rejection (bad number, not allow-listed, bad template) — retrying can't help
                    # and would only risk a duplicate, so stop.
                    if not (code == 429 or (code is not None and code >= 500)):
                        break
                except _RETRYABLE_EXC as e:                  # never reached provider → safe to retry
                    resp, reason = None, repr(e)
                except Exception as e:                       # request may have been delivered (e.g.
                    resp, reason = None, repr(e)             # read timeout) — do NOT retry (dedupe)
                    break
                if i < attempts - 1:
                    await asyncio.sleep(0.5 * (i + 1))       # simple backoff
            db.add(WhatsAppLog(
                user_id=user.id, username=user.username,
                role=str(user.merchant_role or user.role.value), phone=user.phone,
                notification_type=ntype, message=message,
                status="SENT" if ok else "FAILED", provider=settings.WHATSAPP_PROVIDER,
                message_id=msg_id, delivery_status="sent" if ok else "failed",
                retry_count=max(0, used - 1),
                provider_response=resp, failure_reason=None if ok else reason,
            ))
            await db.commit()
            if not ok:
                log.warning("WhatsApp delivery failed for user %s: %s", user_id, reason)
    except Exception:                                       # never let delivery break anything
        log.exception("WhatsApp delivery crashed for user %s", user_id)


async def apply_status_update(message_id: str, status: str, ts: Optional[str] = None) -> None:
    """Update a delivery log row from a provider webhook receipt (sent/delivered/read/failed)."""
    if not message_id:
        return
    from app.db.session import AsyncSessionLocal
    try:
        when = datetime.utcfromtimestamp(int(ts)) if ts else datetime.utcnow()
    except (TypeError, ValueError):
        when = datetime.utcnow()
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(WhatsAppLog).where(WhatsAppLog.message_id == message_id)
            )).scalar_one_or_none()
            if not row:
                return
            row.delivery_status = status
            if status == "delivered":
                row.delivered_at = row.delivered_at or when
            elif status == "read":
                row.read_at = row.read_at or when
                row.delivered_at = row.delivered_at or when
            elif status == "failed":
                row.status = "FAILED"
                row.failure_reason = row.failure_reason or "provider reported failed"
            await db.commit()
    except Exception:
        log.exception("WhatsApp status update failed for message %s", message_id)


async def send_test(user: User) -> dict:
    """Send a one-off test message to a user's own phone and log it (for the 'Send Test' button)."""
    if not settings.whatsapp_configured:
        return {"ok": False, "reason": "WhatsApp provider is not configured on the server."}
    if not (user.phone or "").strip():
        return {"ok": False, "reason": "No phone number is saved on this account."}
    body = _format(f"{settings.WHATSAPP_COMPANY_NAME} test notification — your WhatsApp integration is working.")
    ok, msg_id, resp, reason = False, None, None, None
    try:
        ok, msg_id, resp = await _send(user.phone, body)
        reason = None if ok else resp
    except Exception as e:
        reason = repr(e)
    from app.db.session import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            db.add(WhatsAppLog(
                user_id=user.id, username=user.username, role=str(user.merchant_role or user.role.value),
                phone=user.phone, notification_type="test", message="Test WhatsApp notification",
                status="SENT" if ok else "FAILED", provider=settings.WHATSAPP_PROVIDER,
                message_id=msg_id, delivery_status="sent" if ok else "failed",
                provider_response=resp, failure_reason=None if ok else reason,
            ))
            await db.commit()
    except Exception:
        log.exception("Failed to log test WhatsApp")
    return {"ok": ok, "messageId": msg_id, "reason": reason}


def _schedule(user_id: int, message: str) -> None:
    if not settings.whatsapp_configured or _LOOP is None:
        return
    try:
        _LOOP.call_soon_threadsafe(lambda: _LOOP.create_task(_deliver(user_id, message)))
    except RuntimeError:
        pass  # loop not running (shutdown) — skip


def install_whatsapp_hook() -> None:
    """Register the ORM listeners once, at startup, inside the running event loop. No-op unless
    a provider is configured, so a plain deployment carries zero overhead."""
    global _LOOP, _INSTALLED
    if not settings.whatsapp_configured:
        return
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        _LOOP = None
    if _INSTALLED:
        return
    _INSTALLED = True

    @event.listens_for(Session, "before_flush")
    def _capture(session, flush_context, instances):
        new = [(o.user_id, o.message) for o in session.new if isinstance(o, Notification)]
        if new:
            session.info.setdefault("_wa", []).extend(new)

    @event.listens_for(Session, "after_commit")
    def _dispatch(session):
        for user_id, message in session.info.pop("_wa", []):
            _schedule(user_id, message)

    @event.listens_for(Session, "after_rollback")
    def _clear(session):
        session.info.pop("_wa", None)

    log.info("WhatsApp hook installed (provider=%s)", settings.WHATSAPP_PROVIDER)
