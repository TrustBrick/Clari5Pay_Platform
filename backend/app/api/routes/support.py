"""Customer Support real-time chat (WebSocket + REST fallback).

Each customer (a merchant-role user) owns one OPEN SupportConversation, assigned to exactly one
support agent (see services/support_routing). Messages for a conversation are delivered only to the
customer and their assigned agent — never broadcast to every agent — which prevents duplicate
replies and unnecessary traffic.
"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db, AsyncSessionLocal
from app.models.models import SupportMessage, SupportSender, User, UserRole, SupportConversation
from app.core.security import decode_token
from app.core.deps import get_current_user
from app.schemas.schemas import SupportMessageCreate
from app.api.routes.transactions import compute_balance
from app.services import support_routing

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
    """Sockets are indexed so a message reaches only its two parties: the customer and the single
    agent that owns the conversation."""
    def __init__(self) -> None:
        self.merchants: dict[int, set[WebSocket]] = {}   # customer_id -> sockets
        self.agents: dict[int, set[WebSocket]] = {}      # support_id  -> sockets

    async def connect_merchant(self, merchant_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.merchants.setdefault(merchant_id, set()).add(ws)

    async def connect_agent(self, agent_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.agents.setdefault(agent_id, set()).add(ws)

    def disconnect_merchant(self, merchant_id: int, ws: WebSocket) -> None:
        conns = self.merchants.get(merchant_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self.merchants.pop(merchant_id, None)

    def disconnect_agent(self, agent_id: int, ws: WebSocket) -> None:
        conns = self.agents.get(agent_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self.agents.pop(agent_id, None)

    async def _send(self, sockets: set[WebSocket], text: str, on_dead) -> None:
        for ws in list(sockets):
            try:
                await ws.send_text(text)
            except Exception:
                on_dead(ws)

    async def deliver(self, merchant_id: int, agent_id: int | None, payload: dict) -> None:
        """Send to the customer and, if assigned, only to their owning agent."""
        text = json.dumps(payload)
        await self._send(self.merchants.get(merchant_id, set()), text,
                         lambda ws: self.disconnect_merchant(merchant_id, ws))
        if agent_id is not None:
            await self._send(self.agents.get(agent_id, set()), text,
                             lambda ws: self.disconnect_agent(agent_id, ws))


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


async def _owns_open(db: AsyncSession, support_id: int, customer_id: int) -> SupportConversation | None:
    """The customer's OPEN conversation if it is currently owned by this agent, else None."""
    conv = await support_routing.get_open_conversation(db, customer_id)
    return conv if (conv and conv.support_id == support_id) else None


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
        await manager.connect_agent(user.id, websocket)
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
                async with AsyncSessionLocal() as db:
                    # An agent may only message a customer whose OPEN conversation they own.
                    if not await _owns_open(db, user.id, int(merchant_id)):
                        continue
                    msg = await _persist_message(db, int(merchant_id), SupportSender.SUPPORT, user.name, content)
                    await support_routing.record_agent_reply(db, int(merchant_id))
                    await db.commit()
                    payload_out = _m(msg)
                await manager.deliver(int(merchant_id), user.id, payload_out)
            else:
                async with AsyncSessionLocal() as db:
                    conv = await support_routing.ensure_conversation(db, user.id)
                    agent_id = conv.support_id
                    conv.last_message_at = datetime.utcnow()
                    msg = await _persist_message(db, user.id, SupportSender.MERCHANT, user.name, content)
                    await db.commit()
                    payload_out = _m(msg)
                await manager.deliver(user.id, agent_id, payload_out)
    except WebSocketDisconnect:
        pass
    finally:
        if is_agent:
            manager.disconnect_agent(user.id, websocket)
        else:
            manager.disconnect_merchant(user.id, websocket)


# ─── REST endpoints ───────────────────────────────────────────────────────────
@router.get("/conversations")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Support agents: the conversations OWNED by them (OPEN), with last message + unread count."""
    if current_user.role != UserRole.SUPPORT_AGENT:
        raise HTTPException(status_code=403, detail="Support agent access required")

    convs = (await db.execute(
        select(SupportConversation).where(
            SupportConversation.support_id == current_user.id,
            SupportConversation.status == "OPEN",
        )
    )).scalars().all()
    if not convs:
        return []

    customer_ids = [c.customer_id for c in convs]
    merchants = {
        m.id: m for m in (await db.execute(select(User).where(User.id.in_(customer_ids)))).scalars().all()
    }

    out = []
    for c in convs:
        m = merchants.get(c.customer_id)
        if not m:
            continue
        msgs = (await db.execute(
            select(SupportMessage).where(SupportMessage.merchant_id == c.customer_id)
            .order_by(SupportMessage.created_at.desc())
        )).scalars().all()
        last = msgs[0] if msgs else None
        unread = sum(1 for x in msgs if x.sender == SupportSender.MERCHANT and not x.read)
        out.append({
            "conversationId": c.id,
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
    out.sort(key=lambda c: (c["unread"], c["lastAt"] or ""), reverse=True)
    return out


@router.get("/messages/{merchant_id}")
async def get_messages(
    merchant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.MERCHANT and current_user.id != merchant_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if current_user.role not in (UserRole.MERCHANT, UserRole.SUPPORT_AGENT):
        raise HTTPException(status_code=403, detail="Forbidden")
    # Agents may only read a conversation they currently own.
    if current_user.role == UserRole.SUPPORT_AGENT and not await _owns_open(db, current_user.id, merchant_id):
        raise HTTPException(status_code=403, detail="Conversation not assigned to you")

    msgs = (await db.execute(
        select(SupportMessage).where(SupportMessage.merchant_id == merchant_id)
        .order_by(SupportMessage.created_at.asc())
    )).scalars().all()

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
    msgs = (await db.execute(
        select(SupportMessage).where(SupportMessage.merchant_id == current_user.id)
        .order_by(SupportMessage.created_at.asc())
    )).scalars().all()
    for x in msgs:
        if x.sender == SupportSender.SUPPORT and not x.read:
            x.read = True
    await db.flush()
    return [_m(x) for x in msgs]


@router.get("/my-conversation")
async def my_conversation(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant: the status of their current support conversation (assigned agent, or queued)."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    conv = await support_routing.get_open_conversation(db, current_user.id)
    if not conv:
        return {"status": "NONE", "queued": False, "agentName": None}
    agent = None
    if conv.support_id:
        agent = (await db.execute(select(User).where(User.id == conv.support_id))).scalar_one_or_none()
    return {
        "status": conv.status,
        "queued": conv.support_id is None,
        "agentName": (agent.full_name or agent.name) if agent else None,
    }


@router.post("/messages")
async def post_message(
    data: SupportMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """REST fallback for sending a message (also delivered over WebSocket to the two parties)."""
    content = (data.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty message")

    if current_user.role == UserRole.MERCHANT:
        conv = await support_routing.ensure_conversation(db, current_user.id)
        conv.last_message_at = datetime.utcnow()
        merchant_id, agent_id, sender = current_user.id, conv.support_id, SupportSender.MERCHANT
    elif current_user.role == UserRole.SUPPORT_AGENT:
        if not data.merchant_id:
            raise HTTPException(status_code=400, detail="merchant_id required")
        if not await _owns_open(db, current_user.id, data.merchant_id):
            raise HTTPException(status_code=403, detail="Conversation not assigned to you")
        merchant_id, agent_id, sender = data.merchant_id, current_user.id, SupportSender.SUPPORT
    else:
        raise HTTPException(status_code=403, detail="Forbidden")

    msg = await _persist_message(db, merchant_id, sender, current_user.name, content)
    if sender == SupportSender.SUPPORT:
        await support_routing.record_agent_reply(db, merchant_id)
    await db.flush()
    payload = _m(msg)
    await manager.deliver(merchant_id, agent_id, payload)
    return payload


@router.get("/merchant/{merchant_id}")
async def merchant_details(
    merchant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.SUPPORT_AGENT:
        raise HTTPException(status_code=403, detail="Support agent access required")
    if not await _owns_open(db, current_user.id, merchant_id):
        raise HTTPException(status_code=403, detail="Conversation not assigned to you")
    m = (
        await db.execute(select(User).where(User.id == merchant_id, User.role == UserRole.MERCHANT))
    ).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")
    summary = await compute_balance(db, m)  # live, business-shared available balance
    return {
        "id": m.id, "name": m.name, "username": m.username, "email": m.email,
        "phone": m.phone, "balance": round(summary["available"], 2), "risk": m.risk, "profile": m.profile,
        "payIn": m.pay_in, "payOut": m.pay_out, "active": m.active, "created": str(m.created),
    }
