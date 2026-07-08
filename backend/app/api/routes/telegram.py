"""Telegram bot webhook — self-service registration + status.

Recipients opt in themselves: they send /start to the bot and tap "Share my phone number".
Telegram posts their contact here; we match that number to a Clari5Pay account and store the
chat id on it, so every in-app notification for that user is then mirrored to Telegram by role
(via the after_commit hook in app.services.whatsapp). No phone is retained beyond the account it
already belongs to, and only the person's OWN shared contact is accepted.

The route is always mounted but inert unless TELEGRAM_BOT_TOKEN is set (telegram_configured):
tg_send() no-ops and Telegram never calls an unconfigured webhook.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_admin
from app.db.session import get_db, AsyncSessionLocal
from app.models.models import User
from app.services import whatsapp as wa

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_WELCOME = (
    "👋 Welcome to Clari5Pay notifications.\n\n"
    "To start receiving your alerts here, tap the button below to share the phone number "
    "registered on your Clari5Pay account. We'll match it and confirm your role."
)
_NOT_FOUND = (
    "❌ That number isn't registered on any Clari5Pay account.\n\n"
    "Please ask your admin to add/verify your mobile number, then tap the button to try again."
)
_NOT_OWN = "⚠️ Please share YOUR OWN number using the button below (not a saved contact)."
_STOPPED = "🔕 You've been unsubscribed. Send /start anytime to register again."


@router.get("/webhook")
async def webhook_healthcheck():
    """Plain 200 so the path is verifiable in a browser; Telegram only ever POSTs here."""
    return {"ok": True}


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle a Telegram update: /start → ask for phone; shared contact → match + link + confirm."""
    # Optional shared-secret check (set via setWebhook secret_token).
    if settings.TELEGRAM_WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.TELEGRAM_WEBHOOK_SECRET:
            return Response(status_code=403)
    if not settings.telegram_configured:
        return {"ok": True}
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}                      # ignore non-message updates
    chat_id = str(chat_id)
    from_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    contact = msg.get("contact") or None

    try:
        if contact and contact.get("phone_number"):
            # Only accept the sender's own contact (Telegram sets contact.user_id for that case).
            if contact.get("user_id") and from_id and contact["user_id"] != from_id:
                await wa.tg_send(chat_id, _NOT_OWN, request_contact=True)
                return {"ok": True}
            async with AsyncSessionLocal() as db:
                user = await wa.link_telegram_by_phone(db, chat_id, contact["phone_number"])
                if user is not None:
                    await db.commit()
                    await wa.tg_send(
                        chat_id,
                        f"✅ You're registered, {user.full_name or user.username}!\n\n"
                        f"Role: {wa._role_label(user)}\n"
                        f"You'll now receive your Clari5Pay notifications here.\n\n"
                        f"Send /stop anytime to unsubscribe.",
                    )
                else:
                    await wa.tg_send(chat_id, _NOT_FOUND, request_contact=True)
        elif text.startswith("/stop"):
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(User).where(User.telegram_chat_id == chat_id)
                )).scalars().all()
                for u in rows:
                    u.telegram_chat_id = None
                await db.commit()
            await wa.tg_send(chat_id, _STOPPED)
        elif text.startswith("/start") or text.startswith("/help"):
            await wa.tg_send(chat_id, _WELCOME, request_contact=True)
        else:
            await wa.tg_send(chat_id, "Tap the button below to register your phone number.", request_contact=True)
    except Exception:
        pass                                      # never error back to Telegram (it would retry)
    return {"ok": True}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """Admin view: whether Telegram is configured and how many accounts are linked."""
    linked = (await db.execute(
        select(func.count()).select_from(User).where(User.telegram_chat_id.isnot(None))
    )).scalar_one()
    return {
        "configured": settings.telegram_configured,
        "webhookSecretSet": bool(settings.TELEGRAM_WEBHOOK_SECRET),
        "linkedUsers": int(linked or 0),
    }
