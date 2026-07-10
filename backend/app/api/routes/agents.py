"""Agent Management → Agents (Agent Master).

CRUD for Non-EPS Agents, restricted to MERCHANT users whose merchant_role is Supervisor or
Manager. Agents never log in — this stores agent information only. Agents are shared across a
merchant *business* (``merchant_business`` = the owning user's business name), so every
Supervisor/Manager of the same business manages the same pool.

The whole router is mounted only on the demo stack (see main.py) until the module is complete,
matching the demo-gated Merchant-portal menu; on Production every path here is a 404.
"""
import re
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import AgentMaster, User
from app.core.deps import get_current_agent_manager
from app.schemas.schemas import AgentCreate, AgentUpdate, AgentStatusUpdate
from app.api.routes.system_logs import record_agent_audit

router = APIRouter(prefix="/api/agents", tags=["agents"])

CATEGORIES = {"CASH", "BANK_TRANSFER", "CRYPTO"}
STATUSES = {"ACTIVE", "INACTIVE"}
_CODE_RE = re.compile(r"^[A-Za-z0-9]{3}$")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _business(user: User) -> str:
    """The agent pool this user belongs to (shared across the same business name)."""
    return user.name


async def _next_agent_id(db: AsyncSession) -> str:
    """Next global serial Agent ID (AGT000001…). Independent series; never reused."""
    codes = (await db.execute(
        select(AgentMaster.agent_id).where(AgentMaster.agent_id.like("AGT%"))
    )).scalars().all()
    maxn = 0
    for c in codes:
        try:
            maxn = max(maxn, int(c[3:]))
        except (TypeError, ValueError):
            continue
    return f"AGT{maxn + 1:06d}"


def _serialize(a: AgentMaster) -> dict:
    return {
        "id": a.id,
        "agentId": a.agent_id,
        "fullName": a.full_name,
        "country": a.country,
        "state": a.state,
        "location": a.location,
        "mobile": a.mobile,
        "email": a.email,
        "currency": a.currency,
        "dateOfCreation": a.date_of_creation.isoformat() if a.date_of_creation else None,
        "reference": a.reference,
        "feesPct": a.fees_pct,
        "transactionCode": a.transaction_code,
        "category": a.category,
        "notes": a.notes,
        "riskAnalysis": a.risk_analysis,
        "sentForApproval": a.sent_for_approval,
        "approvalStatus": a.approval_status,
        "status": a.status,
        "createdBy": a.created_by,
        "createdAt": a.created_at.isoformat() + "Z" if a.created_at else None,
        "updatedBy": a.updated_by,
        "updatedAt": a.updated_at.isoformat() + "Z" if a.updated_at else None,
    }


async def _duplicate(
    db: AsyncSession, business: str, *, field, value: str, exclude_id: int | None = None
) -> bool:
    """Case-insensitive duplicate check within the business pool (skips an optional self id)."""
    if value is None or str(value).strip() == "":
        return False
    q = select(AgentMaster.id).where(
        AgentMaster.merchant_business == business,
        func.lower(field) == str(value).strip().lower(),
    )
    if exclude_id is not None:
        q = q.where(AgentMaster.id != exclude_id)
    return (await db.execute(q.limit(1))).scalar() is not None


async def _get_scoped(db: AsyncSession, agent_id: int, business: str) -> AgentMaster:
    row = (await db.execute(
        select(AgentMaster).where(AgentMaster.id == agent_id, AgentMaster.merchant_business == business)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


# ── List (search + filters) ─────────────────────────────────────────────────────
@router.get("")
async def list_agents(
    q: str | None = None,               # search: Agent ID / Name / Mobile / Email
    category: str | None = None,
    country: str | None = None,
    state: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    stmt = select(AgentMaster).where(AgentMaster.merchant_business == _business(user))
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(
            func.concat(
                AgentMaster.agent_id, " ", AgentMaster.full_name, " ",
                func.coalesce(AgentMaster.mobile, ""), " ", func.coalesce(AgentMaster.email, "")
            )
        ).like(term))
    if category and category.upper() in CATEGORIES:
        stmt = stmt.where(AgentMaster.category == category.upper())
    if country and country.strip():
        stmt = stmt.where(func.lower(AgentMaster.country) == country.strip().lower())
    if state and state.strip():
        stmt = stmt.where(func.lower(AgentMaster.state) == state.strip().lower())
    if status and status.upper() in STATUSES:
        stmt = stmt.where(AgentMaster.status == status.upper())
    rows = (await db.execute(stmt.order_by(AgentMaster.id.desc()))).scalars().all()
    return [_serialize(a) for a in rows]


@router.get("/{agent_id}")
async def get_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    return _serialize(await _get_scoped(db, agent_id, _business(user)))


# ── Create ───────────────────────────────────────────────────────────────────
@router.post("")
async def create_agent(
    data: AgentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = _business(user)
    full_name = (data.fullName or "").strip()
    code = (data.transactionCode or "").strip().upper()
    category = (data.category or "").strip().upper()
    mobile = (data.mobile or "").strip() or None
    email = (data.email or "").strip() or None

    # ── Validation (clear, field-specific messages) ──
    for label, val in (("Full Name", full_name), ("Country", data.country), ("State", data.state),
                       ("Location", data.location), ("Currency", data.currency)):
        if not str(val or "").strip():
            raise HTTPException(status_code=400, detail=f"{label} is required.")
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Category must be Cash, Bank Transfer or Crypto.")
    if not _CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="Transaction Code must be exactly 3 alphanumeric characters.")
    if data.feesPct is None or data.feesPct < 0:
        raise HTTPException(status_code=400, detail="Fees % cannot be negative.")
    if await _duplicate(db, business, field=AgentMaster.full_name, value=full_name):
        raise HTTPException(status_code=409, detail="An agent with this name already exists.")
    if mobile and await _duplicate(db, business, field=AgentMaster.mobile, value=mobile):
        raise HTTPException(status_code=409, detail="An agent with this mobile number already exists.")
    if email and await _duplicate(db, business, field=AgentMaster.email, value=email):
        raise HTTPException(status_code=409, detail="An agent with this email address already exists.")
    if await _duplicate(db, business, field=AgentMaster.transaction_code, value=code):
        raise HTTPException(status_code=409, detail="This Transaction Code is already in use.")

    doc = date.today()
    if data.dateOfCreation:
        try:
            doc = date.fromisoformat(data.dateOfCreation)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Date of Creation.")

    agent = AgentMaster(
        agent_id=await _next_agent_id(db),
        full_name=full_name,
        country=data.country.strip(),
        state=data.state.strip(),
        location=data.location.strip(),
        mobile=mobile,
        email=email,
        currency=data.currency.strip(),
        date_of_creation=doc,
        reference=(data.reference or "").strip() or None,
        fees_pct=float(data.feesPct),
        transaction_code=code,
        category=category,
        notes=(data.notes or "").strip() or None,
        risk_analysis=bool(data.riskAnalysis),
        sent_for_approval=bool(data.sendForApproval),
        approval_status="PENDING" if data.sendForApproval else "NOT_REQUIRED",
        status="ACTIVE",                       # new agents default to Active
        merchant_business=business,
        created_by=user.name,
        created_by_id=user.id,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    await record_agent_audit(
        db, "AGENT_CREATE", actor=user, entity_type="agent", entity_id=agent.agent_id,
        new=f"{agent.full_name} ({agent.transaction_code})", ip=request.client.host if request.client else None,
    )
    return _serialize(agent)


# ── Update ───────────────────────────────────────────────────────────────────
@router.put("/{agent_id}")
async def update_agent(
    agent_id: int,
    data: AgentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = _business(user)
    agent = await _get_scoped(db, agent_id, business)
    before = f"{agent.full_name} · {agent.status}"

    if data.fullName is not None:
        fn = data.fullName.strip()
        if not fn:
            raise HTTPException(status_code=400, detail="Full Name is required.")
        if await _duplicate(db, business, field=AgentMaster.full_name, value=fn, exclude_id=agent.id):
            raise HTTPException(status_code=409, detail="An agent with this name already exists.")
        agent.full_name = fn
    if data.mobile is not None:
        mob = data.mobile.strip() or None
        if mob and await _duplicate(db, business, field=AgentMaster.mobile, value=mob, exclude_id=agent.id):
            raise HTTPException(status_code=409, detail="An agent with this mobile number already exists.")
        agent.mobile = mob
    if data.email is not None:
        em = data.email.strip() or None
        if em and await _duplicate(db, business, field=AgentMaster.email, value=em, exclude_id=agent.id):
            raise HTTPException(status_code=409, detail="An agent with this email address already exists.")
        agent.email = em
    if data.country is not None:
        agent.country = data.country.strip() or agent.country
    if data.state is not None:
        agent.state = data.state.strip() or agent.state
    if data.location is not None:
        agent.location = data.location.strip() or agent.location
    if data.currency is not None:
        agent.currency = data.currency.strip() or agent.currency
    if data.reference is not None:
        agent.reference = data.reference.strip() or None
    if data.feesPct is not None:
        if data.feesPct < 0:
            raise HTTPException(status_code=400, detail="Fees % cannot be negative.")
        agent.fees_pct = float(data.feesPct)
    if data.category is not None:
        cat = data.category.strip().upper()
        if cat not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Category must be Cash, Bank Transfer or Crypto.")
        agent.category = cat
    if data.notes is not None:
        agent.notes = data.notes.strip() or None
    if data.riskAnalysis is not None:
        agent.risk_analysis = bool(data.riskAnalysis)
    if data.status is not None:
        st = data.status.strip().upper()
        if st not in STATUSES:
            raise HTTPException(status_code=400, detail="Status must be Active or Inactive.")
        agent.status = st

    agent.updated_by = user.name
    agent.updated_by_id = user.id
    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    await record_agent_audit(
        db, "AGENT_UPDATE", actor=user, entity_type="agent", entity_id=agent.agent_id,
        old=before, new=f"{agent.full_name} · {agent.status}",
        ip=request.client.host if request.client else None,
    )
    return _serialize(agent)


# ── Activate / Deactivate ────────────────────────────────────────────────────
@router.patch("/{agent_id}/status")
async def set_agent_status(
    agent_id: int,
    data: AgentStatusUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    st = (data.status or "").strip().upper()
    if st not in STATUSES:
        raise HTTPException(status_code=400, detail="Status must be Active or Inactive.")
    agent = await _get_scoped(db, agent_id, _business(user))
    old = agent.status
    agent.status = st
    agent.updated_by = user.name
    agent.updated_by_id = user.id
    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    await record_agent_audit(
        db, "AGENT_STATUS", actor=user, entity_type="agent", entity_id=agent.agent_id,
        old=old, new=st, ip=request.client.host if request.client else None,
    )
    return _serialize(agent)


async def _has_transaction_history(db: AsyncSession, agent: AgentMaster) -> bool:
    """True if this agent has ever been assigned to a Deposit / Withdrawal / Settlement (Phase 4
    agent-assignment history). Such agents can be deactivated but not deleted."""
    from app.api.routes.agent_assignment import agent_has_assignment_history
    return await agent_has_assignment_history(db, agent.id)


# ── Delete (only when the agent has no transaction history) ──────────────────
@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    agent = await _get_scoped(db, agent_id, _business(user))
    if await _has_transaction_history(db, agent):
        raise HTTPException(
            status_code=409,
            detail="This Agent has existing transaction history and cannot be deleted. "
                   "You may deactivate the Agent instead.",
        )
    label = f"{agent.full_name} ({agent.agent_id})"
    await record_agent_audit(
        db, "AGENT_DELETE", actor=user, entity_type="agent", entity_id=agent.agent_id,
        old=label, ip=request.client.host if request.client else None,
    )
    await db.delete(agent)
    await db.flush()
    return {"ok": True}
