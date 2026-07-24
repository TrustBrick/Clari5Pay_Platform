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
from app.models.models import AgentAccount, AgentMaster, User
from app.core.deps import get_current_agent_manager
from app.schemas.schemas import AgentCreate, AgentUpdate, AgentStatusUpdate
from app.api.routes.system_logs import record_agent_audit
# The category → allowed account types rule lives with the accounts router that enforces it on
# create; re-used here so a category CHANGE cannot strand accounts the new category disallows.
from app.api.routes.agent_accounts import ALLOWED_ACCOUNT_TYPES, _CATEGORY_LABEL

router = APIRouter(prefix="/api/agents", tags=["agents"])

CATEGORIES = {"CASH", "BANK_TRANSFER", "CRYPTO"}
STATUSES = {"ACTIVE", "INACTIVE"}
_CODE_RE = re.compile(r"^[A-Za-z]{3}$")          # exactly 3 alphabetic characters (no digits)
# Per-leg reference-code prefixes (Deposit / Withdrawal / Settlement): up to 3 alphanumeric chars.
# Shorter than the agent's own Transaction Code on purpose — the requirement caps these at 3 rather
# than fixing them at 3.
_REF_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,3}$")
# (field label, AgentCreate/AgentUpdate attribute, AgentMaster column) for the three prefixes.
REF_CODE_FIELDS = (
    ("Deposit Code", "depositCode", "deposit_code"),
    ("Withdrawal Code", "withdrawalCode", "withdrawal_code"),
    ("Settlement Code", "settlementCode", "settlement_code"),
)
_MOBILE_RE = re.compile(r"^\d{10}$")             # exactly 10 digits, numbers only
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
        "mobile": a.mobile, "mobileCode": a.mobile_code,
        "email": a.email,
        "currency": a.currency,
        "dateOfCreation": a.date_of_creation.isoformat() if a.date_of_creation else None,
        "reference": a.reference,
        "payInFee": a.pay_in_fee or 0.0,
        "payOutFee": a.pay_out_fee or 0.0,
        "settlementFee": a.settlement_fee or 0.0,
        "transactionCode": a.transaction_code,
        "depositCode": a.deposit_code,
        "withdrawalCode": a.withdrawal_code,
        "settlementCode": a.settlement_code,
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
    mobile = (data.mobile or "").strip()
    email = (data.email or "").strip()

    # ── Validation (clear, field-specific messages) ──
    # Mandatory: Name, Country, State, Location, Currency, Mobile, Email, Category, Transaction Code.
    for label, val in (("Full Name", full_name), ("Country", data.country), ("State", data.state),
                       ("Location", data.location), ("Currency", data.currency)):
        if not str(val or "").strip():
            raise HTTPException(status_code=400, detail=f"{label} is required.")
    if not _MOBILE_RE.match(mobile):
        raise HTTPException(status_code=400, detail="Mobile Number must be exactly 10 digits (numbers only).")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Category must be Cash, Bank Transfer or Crypto.")
    if not _CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="Transaction Code must be exactly 3 alphabetic characters.")
    # The three per-leg reference codes are mandatory and stored uppercased. They are deliberately
    # NOT checked for duplicates across agents: reference numbers are issued as max(existing for
    # that prefix) + 1, so two agents sharing a code simply share one series and can never produce
    # a duplicate reference. Requiring uniqueness would also make agents that predate the
    # configuration unsavable, since all of them were seeded with AGD/AGW/AGS.
    ref_codes: dict[str, str] = {}
    for _label, _attr, _col in REF_CODE_FIELDS:
        _val = (getattr(data, _attr, None) or "").strip().upper()
        if not _val:
            raise HTTPException(status_code=400, detail=f"{_label} is required.")
        if not _REF_CODE_RE.match(_val):
            raise HTTPException(
                status_code=400,
                detail=f"{_label} must be 1 to 3 letters or numbers.")
        ref_codes[_col] = _val
    for _label, _val in (("Pay-In Fee", data.payInFee), ("Pay-Out Fee", data.payOutFee),
                         ("Settlement Fee", data.settlementFee)):
        if _val is None or _val < 0:
            raise HTTPException(status_code=400, detail=f"{_label} cannot be negative.")
    if await _duplicate(db, business, field=AgentMaster.full_name, value=full_name):
        raise HTTPException(status_code=409, detail="An agent with this name already exists.")
    if await _duplicate(db, business, field=AgentMaster.mobile, value=mobile):
        raise HTTPException(status_code=409, detail="An agent with this mobile number already exists.")
    if await _duplicate(db, business, field=AgentMaster.email, value=email):
        raise HTTPException(status_code=409, detail="An agent with this email address already exists.")
    if await _duplicate(db, business, field=AgentMaster.transaction_code, value=code):
        raise HTTPException(status_code=409, detail="This Transaction Code is already in use.")

    doc = date.today()
    if data.dateOfCreation:
        try:
            doc = date.fromisoformat(data.dateOfCreation)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Date of Creation.")

    # ── Approval workflow (by creator role) ──
    # Supervisor-created agents start INACTIVE / PENDING and need a Manager to approve before they
    # can be assigned or do anything. Manager-created agents are APPROVED / ACTIVE immediately.
    is_supervisor = str(user.merchant_role or "").upper() == "SUPERVISOR"
    status = "INACTIVE" if is_supervisor else "ACTIVE"
    approval = "PENDING" if is_supervisor else "APPROVED"

    agent = AgentMaster(
        agent_id=await _next_agent_id(db),
        full_name=full_name,
        country=data.country.strip(),
        state=data.state.strip(),
        location=data.location.strip(),
        mobile=mobile,
        mobile_code=(data.mobileCode or None),
        email=email,
        currency=data.currency.strip(),
        date_of_creation=doc,
        reference=(data.reference or "").strip() or None,
        pay_in_fee=float(data.payInFee),
        pay_out_fee=float(data.payOutFee),
        settlement_fee=float(data.settlementFee),
        transaction_code=code,
        **ref_codes,
        category=category,
        notes=(data.notes or "").strip() or None,
        risk_analysis=bool(data.riskAnalysis),
        sent_for_approval=is_supervisor,
        approval_status=approval,
        status=status,
        merchant_business=business,
        created_by=user.username,
        created_by_id=user.id,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    await record_agent_audit(
        db, "AGENT_CREATE", actor=user, entity_type="agent", entity_id=agent.agent_id,
        new=f"{agent.full_name} ({agent.transaction_code}) · {approval}",
        ip=request.client.host if request.client else None,
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
        mob = data.mobile.strip()
        if not _MOBILE_RE.match(mob):
            raise HTTPException(status_code=400, detail="Mobile Number must be exactly 10 digits (numbers only).")
        if await _duplicate(db, business, field=AgentMaster.mobile, value=mob, exclude_id=agent.id):
            raise HTTPException(status_code=409, detail="An agent with this mobile number already exists.")
        agent.mobile = mob
    if data.mobileCode is not None:
        agent.mobile_code = data.mobileCode.strip() or None
    if data.email is not None:
        em = data.email.strip()
        if not _EMAIL_RE.match(em):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        if await _duplicate(db, business, field=AgentMaster.email, value=em, exclude_id=agent.id):
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
    for _label, _val, _attr in (("Pay-In Fee", data.payInFee, "pay_in_fee"),
                                ("Pay-Out Fee", data.payOutFee, "pay_out_fee"),
                                ("Settlement Fee", data.settlementFee, "settlement_fee")):
        if _val is not None:
            if _val < 0:
                raise HTTPException(status_code=400, detail=f"{_label} cannot be negative.")
            setattr(agent, _attr, float(_val))
    # Per-leg reference codes. Same rule as create; only the ones actually sent are touched, so a
    # partial update never blanks the others. Existing references keep the code they were issued
    # under — only transactions created after the change use the new one.
    for _label, _attr, _col in REF_CODE_FIELDS:
        _val = getattr(data, _attr, None)
        if _val is None:
            continue
        _val = _val.strip().upper()
        if not _REF_CODE_RE.match(_val):
            raise HTTPException(status_code=400, detail=f"{_label} must be 1 to 3 letters or numbers.")
        setattr(agent, _col, _val)
    if data.category is not None:
        cat = data.category.strip().upper()
        if cat not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Category must be Cash, Bank Transfer or Crypto.")
        # The category decides which account types this agent may hold, so it cannot be moved to one
        # that its existing accounts contradict (e.g. a Bank Transfer agent with a bank account
        # cannot become Cash, which permits no accounts at all). The operator must remove the
        # offending accounts first — silently stranding them would leave payouts pointing nowhere.
        if cat != (agent.category or "").upper():
            allowed = ALLOWED_ACCOUNT_TYPES.get(cat)
            if allowed is not None:
                existing = set((await db.execute(
                    select(AgentAccount.account_type).where(AgentAccount.agent_master_id == agent.id)
                )).scalars().all())
                clashes = existing - allowed
                if clashes:
                    nice = ", ".join(sorted(clashes))
                    raise HTTPException(
                        status_code=400,
                        detail=f"This agent still has {nice} account(s), which a "
                               f"{_CATEGORY_LABEL.get(cat, cat)} agent cannot have. "
                               "Remove them before changing the category.",
                    )
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
    # A Supervisor-created agent awaiting approval can't be activated via the toggle — it must go
    # through the Manager approve/reject flow.
    if agent.approval_status == "PENDING":
        raise HTTPException(status_code=400, detail="This agent is pending Manager approval — use Approve / Reject.")
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


# ── Approval (Manager only) — approve/reject a Supervisor-created pending agent ──
async def _decide_approval(agent_id: int, request: Request, db: AsyncSession, user: User, *, approve: bool) -> dict:
    if str(user.merchant_role or "").upper() != "MANAGER":
        raise HTTPException(status_code=403, detail="Only a Manager can approve or reject agents.")
    agent = await _get_scoped(db, agent_id, _business(user))
    if agent.approval_status != "PENDING":
        raise HTTPException(status_code=400, detail="This agent is not pending approval.")
    agent.approval_status = "APPROVED" if approve else "REJECTED"
    agent.status = "ACTIVE" if approve else "INACTIVE"
    agent.updated_by = user.name
    agent.updated_by_id = user.id
    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    await record_agent_audit(
        db, "AGENT_APPROVE" if approve else "AGENT_REJECT", actor=user, entity_type="agent",
        entity_id=agent.agent_id, new=agent.approval_status,
        ip=request.client.host if request.client else None,
    )
    return _serialize(agent)


@router.patch("/{agent_id}/approve")
async def approve_agent(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_manager)):
    return await _decide_approval(agent_id, request, db, user, approve=True)


@router.patch("/{agent_id}/reject")
async def reject_agent(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_manager)):
    return await _decide_approval(agent_id, request, db, user, approve=False)


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
