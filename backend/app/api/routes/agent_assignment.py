"""Agent Management → Phase 4: assign a Non-EPS agent + agent account to a transaction.

This is an ADDITIVE, demo-gated layer over the existing Deposit / Withdrawal / Settlement
workflows. It never creates transactions and never changes their business logic — it only writes
the assignment fields on an existing transaction row and appends to agent_assignment_history.
The existing create/approval endpoints are untouched.

Who may assign (mirrors who creates each type in the live system):
  • DEPOSIT     → DEO / DEPOSIT_OPERATOR
  • WITHDRAWAL  → DEO / WITHDRAWAL_OPERATOR
  • SETTLEMENT  → SUPERVISOR
Only Active agents and Active accounts can be assigned; the account must belong to the agent and
its type must match the chosen payment method. Everything is scoped to the caller's merchant
business. Mounted only when ENVIRONMENT=demo (404 on Production).

KNOWN LIMITATION — non-atomic create+assign (accepted, by design)
-----------------------------------------------------------------
Transaction creation and Agent Assignment are two separate operations. The frontend creates the
Deposit/Withdrawal/Settlement through the EXISTING create endpoint and then calls this endpoint to
assign. They are NOT wrapped in a single database transaction.

True atomicity is not achievable without modifying the established create endpoints: those
endpoints synchronously emit external notifications (Telegram via tg_notify, WhatsApp/SMS via the
whatsapp hook — real httpx sends) during creation, so even a shared DB transaction could not undo
those already-sent messages on rollback. Reordering the create endpoints to defer notifications
until after assignment would be exactly the kind of redesign of the Deposit/Withdrawal/Settlement
APIs we are deliberately avoiding.

Consequence: in the unlikely event that assignment fails AFTER the transaction is created (in
practice only a race — an agent/account deactivated in the sub-second window, since the picker
lists only Active agents/accounts and this endpoint re-validates), the request is created but left
unassigned. An administrator can assign an agent later. This preserves the existing Clari5Pay
architecture and avoids modifying established transaction endpoints — the chosen trade-off.
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    AgentAccount, AgentAssignmentHistory, AgentMaster, Transaction, User, UserRole,
)
from app.core.deps import get_current_user
from app.schemas.schemas import AgentAssignmentCreate
from app.api.routes.system_logs import record_agent_audit

router = APIRouter(prefix="/api/transactions", tags=["agent-assignment"])

ASSIGNER_ROLES = {
    "DEPOSIT": {"DEO", "DEPOSIT_OPERATOR"},
    "WITHDRAWAL": {"DEO", "WITHDRAWAL_OPERATOR"},
    "SETTLEMENT": {"SUPERVISOR"},
}
# Supervisor & Manager are the Agent Management oversight roles: they may assign/reassign an agent
# on ANY transaction type, both for reassignment and for the Unassigned-Transactions recovery
# screen (Phase 6). The per-type creator roles above can only assign their own type at creation.
OVERSIGHT_ASSIGNER_ROLES = {"SUPERVISOR", "MANAGER"}


def _business(user: User) -> str:
    return user.name


def _base_type(tx: Transaction) -> str:
    """DEPOSIT_REQUEST/DEPOSIT → DEPOSIT, etc."""
    return tx.type.value.split("_")[0]


async def _get_tx(db: AsyncSession, ref: str, business: str) -> Transaction:
    tx = (await db.execute(
        select(Transaction).where(Transaction.ref == ref, Transaction.merchant_name == business)
    )).scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


def _serialize_current(tx: Transaction, agent: AgentMaster | None, account: AgentAccount | None) -> dict:
    if not tx.assigned_agent_id:
        return {"transactionRef": tx.ref, "assigned": False}
    return {
        "transactionRef": tx.ref,
        "assigned": True,
        "assignedAgentId": tx.assigned_agent_id,
        "assignedAgentAccountId": tx.assigned_agent_account_id,
        "agentId": agent.agent_id if agent else None,
        "agentName": agent.full_name if agent else None,
        "accountRef": account.account_ref if account else None,
        "accountType": account.account_type if account else None,
        "accountLabel": (account.label if account else None),
        "accountDetail": _acct_detail(account) if account else None,
        "assignedBy": tx.assigned_by,
        "assignedAt": tx.assigned_at.isoformat() + "Z" if tx.assigned_at else None,
    }


def _acct_detail(a: AgentAccount) -> str:
    if a.account_type == "BANK":
        return f"{a.bank_name or ''} · {a.account_number or ''}".strip(" ·")
    if a.account_type == "UPI":
        return a.upi_id or ""
    if a.account_type == "CRYPTO":
        return f"{a.crypto_asset or ''} ({a.crypto_network or ''})".strip()
    return a.qr_linked_ref or "QR"


def _serialize_history(h: AgentAssignmentHistory) -> dict:
    return {
        "id": h.id,
        "action": h.action,
        "paymentMethod": h.payment_method,
        "agentId": h.agent_id,
        "agentName": h.agent_name,
        "accountRef": h.account_ref,
        "accountType": h.account_type,
        "prevAgentMasterId": h.prev_agent_master_id,
        "prevAgentAccountId": h.prev_agent_account_id,
        "assignedBy": h.assigned_by,
        "createdAt": h.created_at.isoformat() + "Z" if h.created_at else None,
    }


# ── Assign / Reassign ────────────────────────────────────────────────────────
@router.post("/{ref}/assign-agent")
async def assign_agent(
    ref: str,
    data: AgentAssignmentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Agent assignment requires a merchant operator role.")
    business = _business(current_user)
    tx = await _get_tx(db, ref, business)
    base = _base_type(tx)
    allowed = ASSIGNER_ROLES.get(base, set()) | OVERSIGHT_ASSIGNER_ROLES
    role = str(current_user.merchant_role or "").upper()
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Your role cannot assign an agent to a {base.lower()}.",
        )

    # Agent — must exist in this business and be Active.
    agent = (await db.execute(
        select(AgentMaster).where(AgentMaster.id == data.agentId, AgentMaster.merchant_business == business)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Only an active agent can be assigned.")

    # Account — must belong to the agent and be Active; type must match the payment method.
    account = (await db.execute(
        select(AgentAccount).where(
            AgentAccount.id == data.agentAccountId,
            AgentAccount.agent_master_id == agent.id,
            AgentAccount.merchant_business == business,
        )
    )).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found for this agent")
    if account.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Only an active account can be assigned.")
    if data.paymentMethod and account.account_type != data.paymentMethod.strip().upper():
        raise HTTPException(status_code=400, detail="Selected account does not match the chosen payment method.")

    prev_agent_id = tx.assigned_agent_id
    prev_account_id = tx.assigned_agent_account_id
    action = "REASSIGN" if prev_agent_id else "ASSIGN"
    if prev_agent_id == agent.id and prev_account_id == account.id:
        raise HTTPException(status_code=409, detail="This agent and account are already assigned.")

    now = datetime.utcnow()
    tx.assigned_agent_id = agent.id
    tx.assigned_agent_account_id = account.id
    tx.assigned_by = current_user.name
    tx.assigned_by_id = current_user.id
    tx.assigned_at = now

    db.add(AgentAssignmentHistory(
        transaction_ref=tx.ref, transaction_type=base, payment_method=account.account_type,
        action=action, agent_master_id=agent.id, agent_id=agent.agent_id, agent_name=agent.full_name,
        agent_account_id=account.id, account_ref=account.account_ref, account_type=account.account_type,
        prev_agent_master_id=prev_agent_id, prev_agent_account_id=prev_account_id,
        assigned_by=current_user.name, assigned_by_id=current_user.id, merchant_business=business,
        created_at=now,
    ))
    await record_agent_audit(
        db, f"AGENT_{action}", actor=current_user, entity_type="transaction", entity_id=tx.ref,
        old=(str(prev_agent_id) if prev_agent_id else None),
        new=f"{agent.agent_id} · {account.account_ref} ({account.account_type})",
        ip=request.client.host if request.client else None,
    )
    await db.flush()
    await db.refresh(tx)
    return _serialize_current(tx, agent, account)


# ── Current assignment + history ─────────────────────────────────────────────
@router.get("/{ref}/agent-assignment")
async def get_assignment(
    ref: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    business = _business(current_user)
    tx = await _get_tx(db, ref, business)
    agent = account = None
    if tx.assigned_agent_id:
        agent = (await db.execute(select(AgentMaster).where(AgentMaster.id == tx.assigned_agent_id))).scalar_one_or_none()
        account = (await db.execute(select(AgentAccount).where(AgentAccount.id == tx.assigned_agent_account_id))).scalar_one_or_none()
    history = (await db.execute(
        select(AgentAssignmentHistory)
        .where(AgentAssignmentHistory.transaction_ref == tx.ref, AgentAssignmentHistory.merchant_business == business)
        .order_by(AgentAssignmentHistory.id.desc())
    )).scalars().all()
    return {
        "current": _serialize_current(tx, agent, account),
        "history": [_serialize_history(h) for h in history],
    }


# ── Delete-guard helpers (used by the agents / agent_accounts routers) ────────
async def agent_has_assignment_history(db: AsyncSession, agent_master_id: int) -> bool:
    """True if this agent was ever assigned (or reassigned away) on any transaction."""
    hit = (await db.execute(
        select(AgentAssignmentHistory.id).where(or_(
            AgentAssignmentHistory.agent_master_id == agent_master_id,
            AgentAssignmentHistory.prev_agent_master_id == agent_master_id,
        )).limit(1)
    )).scalar()
    if hit is not None:
        return True
    # Also cover a current assignment with no history row (belt & braces).
    return (await db.execute(
        select(Transaction.id).where(Transaction.assigned_agent_id == agent_master_id).limit(1)
    )).scalar() is not None


async def account_has_assignment_history(db: AsyncSession, account_id: int) -> bool:
    """True if this agent account was ever assigned (or reassigned away) on any transaction."""
    hit = (await db.execute(
        select(AgentAssignmentHistory.id).where(or_(
            AgentAssignmentHistory.agent_account_id == account_id,
            AgentAssignmentHistory.prev_agent_account_id == account_id,
        )).limit(1)
    )).scalar()
    if hit is not None:
        return True
    return (await db.execute(
        select(Transaction.id).where(Transaction.assigned_agent_account_id == account_id).limit(1)
    )).scalar() is not None
