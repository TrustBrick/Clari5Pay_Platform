"""Agent Management → isolated Agent Transaction subsystem (operator workflow).

A COMPLETELY SEPARATE ledger for third-party agent deposit/withdrawal requests. It reads and
writes ONLY the ``agent_transaction`` / ``agent_transaction_audit`` tables (plus the shared
``AgentMaster`` for agent details and the shared ``AuditLog`` for the unified Agent Audit Trail).

It NEVER touches the merchant payment system — no import of, read from, or write to the
merchant ``transactions``/``account_master``/settlement/treasury/risk code. Agent transactions
therefore never affect merchant balances, settlements, treasury, risk, reports or Transaction
History. Business-scoped (``merchant_business`` = the caller's business) and demo-gated in main.py.

Roles (Data Operator = DEO): Deposit → SUPERVISOR/MANAGER/DEO/DEPOSIT_OPERATOR; Withdrawal →
SUPERVISOR/MANAGER/DEO/WITHDRAWAL_OPERATOR; Manage → SUPERVISOR/MANAGER/DEO; Approve/Reject →
SUPERVISOR/MANAGER. Overview/list/audit → any of the five (base access).
"""
import secrets
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import AgentMaster, AgentTransaction, AgentTransactionAudit, User, UserRole
from app.core.deps import get_current_agent_operator, agent_role_in
from app.api.routes.system_logs import record_agent_audit

router = APIRouter(prefix="/api/agent-txns", tags=["agent-transactions-ledger"])

IST = timezone(timedelta(hours=5, minutes=30))

# Per-capability role sets (base module access is enforced by get_current_agent_operator).
DEPOSIT_ROLES = ("SUPERVISOR", "MANAGER", "DEO", "DEPOSIT_OPERATOR")
WITHDRAWAL_ROLES = ("SUPERVISOR", "MANAGER", "DEO", "WITHDRAWAL_OPERATOR")
MANAGE_ROLES = ("SUPERVISOR", "MANAGER", "DEO")
APPROVE_ROLES = ("SUPERVISOR", "MANAGER")

# Instruction options offered on the Agent Deposit/Withdrawal Request forms. "High Priority" and
# "No Call" were retired in favour of "Telegram"; legacy rows may still carry the old values, which
# stay valid for display but are no longer offered as new choices.
INSTRUCTIONS = {"WHATSAPP_ONLY", "CALL_ONLY", "WHATSAPP_CALL", "TELEGRAM", "OTHER"}
MEMBERSHIP_TYPES = {"ONLINE", "OFFLINE"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _business(user: User) -> str:
    return user.name


def _require(user: User, roles: tuple[str, ...], what: str) -> None:
    if not agent_role_in(user, roles):
        raise HTTPException(status_code=403, detail=f"Your role cannot {what}.")


def _ist_parts(dt: datetime | None):
    """(iso_utc, ist_date, ist_time) for a stored (naive-UTC) timestamp."""
    if not dt:
        return None, None, None
    aware = dt.replace(tzinfo=timezone.utc)
    ist = aware.astimezone(IST)
    return aware.isoformat().replace("+00:00", "Z"), ist.strftime("%Y-%m-%d"), ist.strftime("%I:%M:%S %p")


async def _next_serial(db: AsyncSession, model_col, prefix: str) -> str:
    """Next global serial for `prefix` (e.g. AGD000001) — mirrors _next_agent_id (max()+1)."""
    vals = (await db.execute(select(model_col).where(model_col.like(f"{prefix}%")))).scalars().all()
    maxn = 0
    for v in vals:
        try:
            maxn = max(maxn, int(str(v)[len(prefix):]))
        except (TypeError, ValueError):
            continue
    return f"{prefix}{maxn + 1:06d}"


def _token() -> str:
    return "TKN-" + secrets.token_hex(8).upper()


async def _log(db: AsyncSession, t: AgentTransaction, action: str, actor: User, *,
               old_amount: float | None = None, new_amount: float | None = None,
               note: str | None = None, approver_name: str | None = None) -> None:
    """Write the isolated audit row + a parallel AuditLog (AGENT_TXN_*) for the unified trail."""
    db.add(AgentTransactionAudit(
        agent_transaction_id=t.id, reference_number=t.reference_number, action=action,
        old_amount=old_amount, new_amount=new_amount, note=note, approver_name=approver_name,
        merchant_business=t.merchant_business, actor_username=actor.username,
        actor_role=str(actor.merchant_role or "").upper() or None,
    ))
    await record_agent_audit(
        db, f"AGENT_TXN_{action}", actor,
        entity_type="agent_transaction", entity_id=t.reference_number,
        old=None if old_amount is None else f"{old_amount:.2f}",
        new=note or (None if new_amount is None else f"{new_amount:.2f}"),
    )


def _row(t: AgentTransaction) -> dict:
    c_iso, c_date, c_time = _ist_parts(t.created_at)
    u_iso, u_date, u_time = _ist_parts(t.updated_at)
    a_iso, a_date, a_time = _ist_parts(t.approved_at)
    return {
        "id": t.id,
        "referenceNumber": t.reference_number,
        "transactionCode": t.transaction_code,
        "type": t.txn_type,
        "agentMasterId": t.agent_master_id,
        "agentCode": t.agent_code, "agentName": t.agent_name,
        "agentCountry": t.agent_country, "agentState": t.agent_state,
        "agentLocation": t.agent_location, "agentCategory": t.agent_category,
        "membershipId": t.membership_id, "membershipName": t.membership_name,
        "membershipType": t.membership_type,
        "amount": round(t.amount, 2),
        "country": t.txn_country, "state": t.txn_state, "location": t.txn_location,
        "mobile": t.mobile,
        "tokenDetails": t.token_details, "noteNumber": t.note_number,
        "notes": t.notes, "instructions": t.instructions,
        "status": t.status,
        "sentForApproval": t.sent_for_approval,
        "approverName": t.approver_name,
        "approvedBy": t.approved_by, "approvedDate": a_date, "approvedTime": a_time, "approvedAt": a_iso,
        "linkedDepositId": t.linked_deposit_id,
        "createdBy": t.created_by, "createdAt": c_iso, "createdDate": c_date, "createdTime": c_time,
        "updatedBy": t.updated_by, "updatedAt": u_iso, "updatedDate": u_date, "updatedTime": u_time,
    }


async def _get_agent(db: AsyncSession, business: str, agent_master_id: int) -> AgentMaster:
    agent = (await db.execute(
        select(AgentMaster).where(AgentMaster.id == agent_master_id, AgentMaster.merchant_business == business)
    )).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found for this business.")
    if str(agent.status).upper() != "ACTIVE":
        raise HTTPException(status_code=400, detail="Selected agent is inactive.")
    return agent


async def _resolve_approver(db: AsyncSession, business: str, approver_user_id: int | None):
    if approver_user_id is None:
        return None, None
    u = (await db.execute(select(User).where(User.id == approver_user_id))).scalar_one_or_none()
    ok = u and u.role == UserRole.MERCHANT and u.name == business and str(u.merchant_role or "").upper() in ("SUPERVISOR", "MANAGER")
    if not ok:
        raise HTTPException(status_code=400, detail="Authorized approver must be a Supervisor or Manager of your business.")
    return u.id, u.username


# ── Request models ──────────────────────────────────────────────────────────────
class _Base(BaseModel):
    agentMasterId: int
    membershipId: str
    membershipName: str | None = None
    membershipType: str
    amount: float
    country: str | None = None
    state: str | None = None
    location: str | None = None
    mobile: str | None = None
    notes: str | None = None
    instructions: str | None = None
    sentForApproval: bool = False
    approverUserId: int | None = None


class AgentDepositCreate(_Base):
    pass


class AgentWithdrawalCreate(_Base):
    linkedDepositId: int | None = None


class AgentManage(BaseModel):
    amount: float
    notes: str | None = None
    sentForApproval: bool = False
    approverUserId: int | None = None


def _validate_common(body: _Base) -> None:
    if body.amount is None or body.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction Amount must be greater than zero.")
    if body.membershipType.upper() not in MEMBERSHIP_TYPES:
        raise HTTPException(status_code=400, detail="Membership Type must be Online or Offline.")
    if not body.membershipId.strip():
        raise HTTPException(status_code=400, detail="Membership ID is required.")
    if body.notes and len(body.notes) > 100:
        raise HTTPException(status_code=400, detail="Notes must be 100 characters or fewer.")
    if body.instructions and body.instructions.upper() not in INSTRUCTIONS:
        raise HTTPException(status_code=400, detail="Invalid instruction option.")


async def _create(db: AsyncSession, user: User, body: _Base, txn_type: str,
                  prefix: str, code_letter: str, linked_deposit_id: int | None) -> dict:
    business = _business(user)
    _validate_common(body)
    agent = await _get_agent(db, business, body.agentMasterId)
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId)

    ref = await _next_serial(db, AgentTransaction.reference_number, prefix)
    note_no = await _next_serial(db, AgentTransaction.note_number, "AGN")
    txn_code = f"{agent.transaction_code}-{code_letter}-{ref[len(prefix):]}"

    # Membership name: use the entered value, else auto-fill from a prior agent transaction.
    membership_name = (body.membershipName or "").strip()
    if not membership_name:
        prior = (await db.execute(
            select(AgentTransaction.membership_name).where(
                AgentTransaction.merchant_business == business,
                AgentTransaction.membership_id == body.membershipId.strip(),
                AgentTransaction.membership_name.is_not(None),
            ).order_by(AgentTransaction.id.desc())
        )).scalars().first()
        membership_name = prior or None

    t = AgentTransaction(
        reference_number=ref, transaction_code=txn_code, txn_type=txn_type,
        merchant_business=business,
        agent_master_id=agent.id, agent_code=agent.agent_id, agent_name=agent.full_name,
        agent_country=agent.country, agent_state=agent.state, agent_location=agent.location,
        agent_category=agent.category,
        membership_id=body.membershipId.strip(), membership_name=membership_name,
        membership_type=body.membershipType.upper(),
        amount=round(body.amount, 2),
        txn_country=body.country, txn_state=body.state, txn_location=body.location, mobile=body.mobile,
        token_details=_token(), note_number=note_no,
        notes=(body.notes or None), instructions=(body.instructions.upper() if body.instructions else None),
        status="PENDING", sent_for_approval=bool(body.sentForApproval),
        approver_user_id=approver_id, approver_name=approver_name,
        linked_deposit_id=linked_deposit_id,
        created_by=user.username, created_by_id=user.id,
    )
    db.add(t)
    await db.flush()   # assign t.id for the audit FK
    await _log(db, t, "CREATED", user, new_amount=t.amount, note=f"{txn_type.title()} request created")
    if t.sent_for_approval:
        await _log(db, t, "SENT_FOR_APPROVAL", user, approver_name=approver_name,
                   note=f"Sent to {approver_name or 'an approver'} for approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── Form data & membership lookup ────────────────────────────────────────────────
@router.get("/form-data")
async def form_data(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """Dropdown data for the create/manage forms — active agents + authorized approvers."""
    business = _business(user)
    agents = (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business, AgentMaster.status == "ACTIVE")
        .order_by(AgentMaster.id.desc())
    )).scalars().all()
    approvers = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.name == business)
    )).scalars().all()
    return {
        "agents": [{
            "id": a.id, "agentId": a.agent_id, "name": a.full_name, "country": a.country,
            "state": a.state, "location": a.location, "category": a.category,
            "transactionCode": a.transaction_code, "currency": a.currency,
        } for a in agents],
        "approvers": [{"id": u.id, "name": u.username, "role": str(u.merchant_role or "").upper()}
                      for u in approvers if str(u.merchant_role or "").upper() in ("SUPERVISOR", "MANAGER")],
        "instructions": sorted(INSTRUCTIONS),
        "membershipTypes": ["ONLINE", "OFFLINE"],
    }


@router.get("/member/{membership_id}")
async def member_lookup(membership_id: str, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_agent_operator)):
    """Auto-fetch (isolated): membership name from prior agent transactions, and the agent from the
    latest agent DEPOSIT for this membership (for the Withdrawal form). Never reads merchant members."""
    business = _business(user)
    rows = (await db.execute(
        select(AgentTransaction).where(
            AgentTransaction.merchant_business == business,
            AgentTransaction.membership_id == membership_id.strip(),
        ).order_by(AgentTransaction.id.desc())
    )).scalars().all()
    membership_name = next((r.membership_name for r in rows if r.membership_name), None)
    dep = next((r for r in rows if r.txn_type == "DEPOSIT"), None)
    latest_deposit = None if dep is None else {
        "agentMasterId": dep.agent_master_id, "agentCode": dep.agent_code, "agentName": dep.agent_name,
        "country": dep.agent_country, "state": dep.agent_state, "location": dep.agent_location,
        "category": dep.agent_category, "depositId": dep.id, "reference": dep.reference_number,
    }
    return {"membershipId": membership_id.strip(), "membershipName": membership_name, "latestDeposit": latest_deposit}


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/deposit")
async def create_deposit(body: AgentDepositCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    _require(user, DEPOSIT_ROLES, "create Agent Deposit Requests")
    return await _create(db, user, body, "DEPOSIT", "AGD", "D", None)


@router.post("/withdrawal")
async def create_withdrawal(body: AgentWithdrawalCreate, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    _require(user, WITHDRAWAL_ROLES, "create Agent Withdrawal Requests")
    linked = None
    if body.linkedDepositId is not None:
        d = (await db.execute(select(AgentTransaction).where(
            AgentTransaction.id == body.linkedDepositId,
            AgentTransaction.merchant_business == _business(user),
            AgentTransaction.txn_type == "DEPOSIT",
        ))).scalar_one_or_none()
        linked = d.id if d else None
    return await _create(db, user, body, "WITHDRAWAL", "AGW", "W", linked)


# ── List / search (Manage Transaction worklist) ──────────────────────────────────
@router.get("")
async def list_txns(status: str | None = None, txn_type: str | None = None, search: str | None = None,
                    date: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """Business-scoped list of agent transactions with optional status/type/search/date filters.
    Manage Transaction calls this with status=PENDING. Search matches reference / membership / agent."""
    business = _business(user)
    stmt = select(AgentTransaction).where(AgentTransaction.merchant_business == business)
    if status:
        stmt = stmt.where(AgentTransaction.status == status.upper())
    if txn_type:
        stmt = stmt.where(AgentTransaction.txn_type == txn_type.upper())
    rows = (await db.execute(stmt.order_by(AgentTransaction.id.desc()))).scalars().all()

    q = (search or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r.reference_number or "").lower()
                or q in (r.membership_id or "").lower() or q in (r.agent_code or "").lower()
                or q in (r.membership_name or "").lower()]

    def _d(r):
        _, d, _t = _ist_parts(r.created_at)
        return d or ""
    if date:
        rows = [r for r in rows if _d(r) == date]
    if date_from:
        rows = [r for r in rows if _d(r) >= date_from]
    if date_to:
        rows = [r for r in rows if _d(r) <= date_to]
    return [_row(r) for r in rows]


@router.get("/{txn_id}/audit")
async def txn_audit(txn_id: int, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_agent_operator)):
    business = _business(user)
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    rows = (await db.execute(select(AgentTransactionAudit).where(
        AgentTransactionAudit.agent_transaction_id == txn_id).order_by(AgentTransactionAudit.id.desc()))).scalars().all()
    out = []
    for r in rows:
        iso, d, tm = _ist_parts(r.created_at)
        out.append({
            "id": r.id, "action": r.action, "oldAmount": r.old_amount, "newAmount": r.new_amount,
            "note": r.note, "approverName": r.approver_name, "actor": r.actor_username, "role": r.actor_role,
            "createdAt": iso, "createdDate": d, "createdTime": tm,
        })
    return out


# ── Manage (amount correction) + approval ────────────────────────────────────────
async def _load_pending(db: AsyncSession, business: str, txn_id: int) -> AgentTransaction:
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    if t.status != "PENDING":
        raise HTTPException(status_code=400, detail="Only pending transactions can be modified.")
    return t


@router.patch("/{txn_id}/manage")
async def manage_txn(txn_id: int, body: AgentManage, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_agent_operator)):
    _require(user, MANAGE_ROLES, "manage Agent Transactions")
    business = _business(user)
    t = await _load_pending(db, business, txn_id)
    if body.amount is None or body.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction Amount must be greater than zero.")
    if body.notes and len(body.notes) > 100:
        raise HTTPException(status_code=400, detail="Notes must be 100 characters or fewer.")
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId)

    old_amount = t.amount
    new_amount = round(body.amount, 2)
    t.updated_by = user.username
    t.updated_by_id = user.id
    t.updated_at = datetime.utcnow()
    if body.notes is not None:
        t.notes = body.notes or None
    if body.sentForApproval:
        t.sent_for_approval = True
        t.approver_user_id = approver_id
        t.approver_name = approver_name
    if new_amount != old_amount:
        t.amount = new_amount
        await _log(db, t, "AMOUNT_UPDATED", user, old_amount=old_amount, new_amount=new_amount,
                   note=(body.notes or None))
    if body.sentForApproval:
        await _log(db, t, "SENT_FOR_APPROVAL", user, approver_name=approver_name,
                   note=f"Sent to {approver_name or 'an approver'} for approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


async def _decide(db: AsyncSession, user: User, txn_id: int, approve: bool) -> dict:
    _require(user, APPROVE_ROLES, "approve or reject Agent Transactions")
    business = _business(user)
    t = await _load_pending(db, business, txn_id)
    t.status = "APPROVED" if approve else "REJECTED"
    t.approved_by = user.username
    t.approved_by_id = user.id
    t.approved_at = datetime.utcnow()
    t.updated_by = user.username
    t.updated_by_id = user.id
    t.updated_at = datetime.utcnow()
    await _log(db, t, "APPROVED" if approve else "REJECTED", user,
               note=f"{'Approved' if approve else 'Rejected'} by {user.username}")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/approve")
async def approve_txn(txn_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_agent_operator)):
    return await _decide(db, user, txn_id, True)


@router.post("/{txn_id}/reject")
async def reject_txn(txn_id: int, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_agent_operator)):
    return await _decide(db, user, txn_id, False)


# ── Overview (isolated KPIs — summarizes ONLY agent_transactions) ────────────────
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """KPIs / summaries / trend for the Agent Overview — computed exclusively from the isolated
    agent_transaction table (never merchant transactions). Commission uses each agent's fees_pct."""
    business = _business(user)
    txns = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business).order_by(AgentTransaction.id.desc()))).scalars().all()
    fees = {a.id: (a.fees_pct or 0.0) for a in (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business))).scalars().all()}

    def _sum(pred):
        return round(sum(t.amount for t in txns if pred(t)), 2)

    dep = [t for t in txns if t.txn_type == "DEPOSIT"]
    wd = [t for t in txns if t.txn_type == "WITHDRAWAL"]
    approved = [t for t in txns if t.status == "APPROVED"]

    # ── Financial summary — mirrors the Merchant canonical formula (transactions.py) applied to
    # the ISOLATED agent ledger only. Each agent's commission = its fees_pct on the approved
    # leg's amount (same percentage for deposits and withdrawals). Computed on read (not stored),
    # exactly like the merchant modules, so figures always reflect the latest approvals.
    #   Gross Amount (Approved)  = Σ approved deposit amounts (before commission)
    #   Deposit Commission       = Σ (approved deposit amount × fees_pct)
    #   Withdrawal Commission    = Σ (approved withdrawal amount × fees_pct)
    #   Total Commission         = Deposit Commission + Withdrawal Commission
    #   Net (Approved)           = Gross − Deposit Commission − Withdrawals − Withdrawal Commission
    approved_dep = [t for t in approved if t.txn_type == "DEPOSIT"]
    approved_wd = [t for t in approved if t.txn_type == "WITHDRAWAL"]

    def _commission(rows):
        return sum(t.amount * fees.get(t.agent_master_id, 0.0) / 100 for t in rows)

    gross_amount = sum(t.amount for t in approved_dep)
    total_withdrawal_amount = sum(t.amount for t in approved_wd)
    deposit_commission = _commission(approved_dep)
    withdrawal_commission = _commission(approved_wd)
    total_commission = deposit_commission + withdrawal_commission
    net_amount = gross_amount - deposit_commission - total_withdrawal_amount - withdrawal_commission

    by_agent: dict = defaultdict(lambda: {"deposits": 0.0, "withdrawals": 0.0, "count": 0})
    for t in txns:
        k = (t.agent_code, t.agent_name)
        by_agent[k]["count"] += 1
        by_agent[k]["deposits" if t.txn_type == "DEPOSIT" else "withdrawals"] += t.amount

    trend: dict = defaultdict(lambda: {"deposits": 0.0, "withdrawals": 0.0})
    for t in txns:
        _, d, _t = _ist_parts(t.created_at)
        if d:
            trend[d]["deposits" if t.txn_type == "DEPOSIT" else "withdrawals"] += t.amount

    return {
        "cards": {
            "totalTransactions": len(txns),
            "depositCount": len(dep), "depositAmount": _sum(lambda t: t.txn_type == "DEPOSIT"),
            "withdrawalCount": len(wd), "withdrawalAmount": _sum(lambda t: t.txn_type == "WITHDRAWAL"),
            "pending": sum(1 for t in txns if t.status == "PENDING"),
            "approved": len(approved),
            "rejected": sum(1 for t in txns if t.status == "REJECTED"),
            "approvedDeposits": round(gross_amount, 2),
            "approvedWithdrawals": round(total_withdrawal_amount, 2),
            "grossAmount": round(gross_amount, 2),
            "depositCommission": round(deposit_commission, 2),
            "withdrawalCommission": round(withdrawal_commission, 2),
            "netAmount": round(net_amount, 2),
            "totalCommission": round(total_commission, 2),
        },
        "byAgent": [{"agentCode": k[0], "agentName": k[1], "deposits": round(v["deposits"], 2),
                     "withdrawals": round(v["withdrawals"], 2), "count": v["count"]}
                    for k, v in sorted(by_agent.items(), key=lambda kv: -(kv[1]["deposits"] + kv[1]["withdrawals"]))][:10],
        "trend": [{"date": d, "deposits": round(v["deposits"], 2), "withdrawals": round(v["withdrawals"], 2)}
                  for d, v in sorted(trend.items())][-14:],
        "recent": [_row(t) for t in txns[:10]],
    }
