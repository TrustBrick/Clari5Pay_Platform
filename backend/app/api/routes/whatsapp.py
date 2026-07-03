"""WhatsApp notification admin settings, delivery logs, and the provider status webhook."""
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_admin
from app.db.session import get_db
from app.models.models import User, WhatsAppLog
from app.services import whatsapp as wa

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """WhatsApp console state: connection/provider status (from server env — credentials are never
    returned), plus the runtime per-role and per-event toggles."""
    return {
        "configured": settings.whatsapp_configured,
        "provider": settings.WHATSAPP_PROVIDER or None,
        "businessNumber": settings.WHATSAPP_BUSINESS_NUMBER or None,
        "businessAccountId": settings.WHATSAPP_BUSINESS_ACCOUNT_ID or None,
        "phoneIdSet": bool(settings.WHATSAPP_PHONE_ID),
        "templateSet": bool(settings.WHATSAPP_TEMPLATE or settings.WHATSAPP_CONTENT_SID),
        "usingTemplate": settings.whatsapp_use_template,   # demo-gated Twilio Content Template path
        "webhookConfigured": bool(settings.WHATSAPP_VERIFY_TOKEN),
        "roles": await wa.get_role_settings(db),
        "roleKeys": wa.WA_ROLE_KEYS,
        "events": await wa.get_event_settings(db),
        "eventKeys": wa.WA_EVENT_KEYS,
    }


@router.put("/settings")
async def put_settings(payload: dict, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """Update the per-role and/or per-event toggles.
    Body: {"roles": {...}, "events": {...}}."""
    payload = payload or {}
    roles = await wa.set_role_settings(db, payload.get("roles", {})) if "roles" in payload else await wa.get_role_settings(db)
    events = await wa.set_event_settings(db, payload.get("events", {})) if "events" in payload else await wa.get_event_settings(db)
    await db.commit()
    return {"roles": roles, "events": events}


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """Today's delivery statistics (from the delivery log)."""
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.execute(select(WhatsAppLog).where(WhatsAppLog.created_at >= start))).scalars().all()
    sent = sum(1 for r in rows if r.status == "SENT")
    failed = sum(1 for r in rows if r.status == "FAILED")
    delivered = sum(1 for r in rows if r.delivered_at or r.delivery_status in ("delivered", "read"))
    read = sum(1 for r in rows if r.read_at or r.delivery_status == "read")
    pending = sum(1 for r in rows if r.status == "SENT" and not (r.delivered_at or r.delivery_status in ("delivered", "read")))
    attempts = sent + failed
    return {
        "sentToday": sent, "delivered": delivered, "read": read, "failed": failed,
        "pending": pending, "total": len(rows),
        "successRate": round(sent / attempts * 100, 1) if attempts else 0.0,
    }


@router.post("/test")
async def test_message(current: User = Depends(get_current_admin)):
    """Send a test WhatsApp to the current admin's own registered number."""
    return await wa.send_test(current)


@router.get("/logs")
async def list_logs(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Recent WhatsApp delivery attempts, newest first."""
    rows = (await db.execute(
        select(WhatsAppLog).order_by(WhatsAppLog.created_at.desc()).limit(min(max(limit, 1), 500))
    )).scalars().all()
    return [{
        "id": r.id, "user": r.username, "role": r.role, "phone": r.phone,
        "type": r.notification_type, "message": r.message,
        "status": r.status, "deliveryStatus": r.delivery_status, "messageId": r.message_id,
        "retryCount": r.retry_count, "provider": r.provider,
        "failureReason": r.failure_reason,
        "sentAt": (r.created_at.isoformat() + "Z") if r.created_at else None,
        "deliveredAt": (r.delivered_at.isoformat() + "Z") if r.delivered_at else None,
        "readAt": (r.read_at.isoformat() + "Z") if r.read_at else None,
    } for r in rows]


# ── Provider webhook (Meta Cloud API) — delivery/read receipts ──────────────────
@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta webhook verification handshake."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN and settings.WHATSAPP_VERIFY_TOKEN:
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Meta status callbacks → update the matching delivery-log rows (never errors back to Meta)."""
    try:
        body = await request.json()
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                for st in (change.get("value", {}) or {}).get("statuses", []):
                    await wa.apply_status_update(st.get("id"), st.get("status"), st.get("timestamp"))
    except Exception:
        pass
    return {"ok": True}
