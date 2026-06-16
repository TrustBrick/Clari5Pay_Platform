"""Customer Support real-time chat (WebSocket + REST fallback)."""
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db, AsyncSessionLocal
from app.models.models import SupportMessage, SupportSender, User, UserRole
from app.core.security import decode_token
from app.core.deps import get_current_user
from app.schemas.schemas import SupportMessageCreate

router = APIRouter(prefix="/api/support", tags=["support"])


def _m(m: SupportMessage) -> dict:
    return {
        "id": m.id,
        "merchantId": m.merchant_id,
        "sender": m.sender.value if hasattr(m.sender, "value") else m.sender,
        "senderName": m.sender_name,
        "content": m.content,
        "read": m.read,
        "createdAt": m.created_at.isoformat(),
    }


# ─── Connection manager ───────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self) -> None:
        # merchant_id -> set of merchant websockets
        self.merchants: dict[int, set[WebSocket]] = {}
        # set of support-agent websockets (they see all conversations)
        self.agents: set[WebSocket] = set()

    async def connect_merchant(self, merchant_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.merchants.setdefault(merchant_id, set()).add(ws)

    async def connect_agent(self, ws: WebSocket) -> None:
        await ws.accept()
        self.agents.add(ws)

    def disconnect_merchant(self, merchant_id: int, ws: WebSocket) -> None:
        conns = self.merchants.get(merchant_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self.merchants.pop(merchant_id, None)

    def disconnect_agent(self, ws: WebSocket) -> None:
        self.agents.discard(ws)

    async def broadcast(self, merchant_id: int, payload: dict) -> None:
        text = json.dumps(payload)
        # to the merchant party
        for ws in list(self.merchants.get(merchant_id, set())):
            try:
                await ws.send_text(text)
            except Exception:
                self.disconnect_merchant(merchant_id, ws)
        # to all support agents
        for ws in list(self.agents):
            try:
                await ws.send_text(text)
            except Exception:
                self.disconnect_agent(ws)


manager = ConnectionManager()


async def _persist_message(
    db: AsyncSession, merchant_id: int, sender: SupportSender, sender_name: str, content: str
) -> SupportMessage:
    msg = SupportMessage(
        merchant_id=merchant_id,
        sender=sender,
        sender_name=sender_name,
        content=content,
        read=False,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    return msg


# ─── WebSocket endpoint ───────────────────────────────────────────────────────
@router.websocket("/ws")
async def support_ws(websocket: WebSocket, token: str):
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        await websocket.close(code=4401)
        return

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.id == int(payload["sub"])))
        ).scalar_one_or_none()

    if not user or not user.active or user.role not in (UserRole.MERCHANT, UserRole.SUPPORT_AGENT):
        await websocket.close(code=4403)
        return

    is_agent = user.role == UserRole.SUPPORT_AGENT
    if is_agent:
        await manager.connect_agent(websocket)
    else:
        await manager.connect_merchant(user.id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            content = (data.get("content") or "").strip()
            if not content:
                continue

            if is_agent:
                merchant_id = data.get("merchantId") or data.get("merchant_id")
                if not merchant_id:
                    continue
                sender = SupportSender.SUPPORT
            else:
                merchant_id = user.id
                sender = SupportSender.MERCHANT

            async with AsyncSessionLocal() as db:
                msg = await _persist_message(db, int(merchant_id), sender, user.name, content)
                await db.commit()
                payload_out = _m(msg)

            await manager.broadcast(int(merchant_id), payload_out)
    except WebSocketDisconnect:
        pass
    finally:
        if is_agent:
            manager.disconnect_agent(websocket)
        else:
            manager.disconnect_merchant(user.id, websocket)


# ─── REST endpoints ───────────────────────────────────────────────────────────
@router.get("/conversations")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Support agents: list every merchant conversation with last message + unread count."""
    if current_user.role != UserRole.SUPPORT_AGENT:
        raise HTTPException(status_code=403, detail="Support agent access required")

    merchants = (
        await db.execute(select(User).where(User.role == UserRole.MERCHANT))
    ).scalars().all()

    convos = []
    for m in merchants:
        msgs = (
            await db.execute(
                select(SupportMessage)
                .where(SupportMessage.merchant_id == m.id)
                .order_by(SupportMessage.created_at.desc())
            )
        ).scalars().all()
        if not msgs:
            last = None
            unread = 0
        else:
            last = msgs[0]
            unread = sum(1 for x in msgs if x.sender == SupportSender.MERCHANT and not x.read)
        convos.append({
            "merchantId": m.id,
            "merchantName": m.name,
            "email": m.email,
            "phone": m.phone,
            "username": m.username,
            "lastMessage": last.content if last else None,
            "lastAt": last.created_at.isoformat() if last else None,
            "unread": unread,
            "messageCount": len(msgs),
        })

    # Most recent / most active conversations first
    convos.sort(key=lambda c: (c["unread"], c["lastAt"] or ""), reverse=True)
    return convos


@router.get("/messages/{merchant_id}")
async def get_messages(
    merchant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Merchants can only read their own conversation.
    if current_user.role == UserRole.MERCHANT and current_user.id != merchant_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if current_user.role not in (UserRole.MERCHANT, UserRole.SUPPORT_AGENT):
        raise HTTPException(status_code=403, detail="Forbidden")

    msgs = (
        await db.execute(
            select(SupportMessage)
            .where(SupportMessage.merchant_id == merchant_id)
            .order_by(SupportMessage.created_at.asc())
        )
    ).scalars().all()

    # Mark the *other* party's messages as read.
    other = SupportSender.SUPPORT if current_user.role == UserRole.MERCHANT else SupportSender.MERCHANT
    for x in msgs:
        if x.sender == other and not x.read:
            x.read = True
    await db.flush()

    return [_m(x) for x in msgs]


@router.get("/my-messages")
async def my_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    msgs = (
        await db.execute(
            select(SupportMessage)
            .where(SupportMessage.merchant_id == current_user.id)
            .order_by(SupportMessage.created_at.asc())
        )
    ).scalars().all()
    for x in msgs:
        if x.sender == SupportSender.SUPPORT and not x.read:
            x.read = True
    await db.flush()
    return [_m(x) for x in msgs]


@router.post("/messages")
async def post_message(
    data: SupportMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """REST fallback for sending a message (also broadcast over WebSocket)."""
    if current_user.role == UserRole.MERCHANT:
        merchant_id = current_user.id
        sender = SupportSender.MERCHANT
    elif current_user.role == UserRole.SUPPORT_AGENT:
        if not data.merchant_id:
            raise HTTPException(status_code=400, detail="merchant_id required")
        merchant_id = data.merchant_id
        sender = SupportSender.SUPPORT
    else:
        raise HTTPException(status_code=403, detail="Forbidden")

    content = (data.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty message")

    msg = await _persist_message(db, merchant_id, sender, current_user.name, content)
    await db.flush()
    payload = _m(msg)
    await manager.broadcast(merchant_id, payload)
    return payload


@router.get("/merchant/{merchant_id}")
async def merchant_details(
    merchant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.SUPPORT_AGENT:
        raise HTTPException(status_code=403, detail="Support agent access required")
    m = (
        await db.execute(select(User).where(User.id == merchant_id, User.role == UserRole.MERCHANT))
    ).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")
    return {
        "id": m.id, "name": m.name, "username": m.username, "email": m.email,
        "phone": m.phone, "balance": m.balance, "risk": m.risk, "profile": m.profile,
        "payIn": m.pay_in, "payOut": m.pay_out, "active": m.active, "created": str(m.created),
    }
