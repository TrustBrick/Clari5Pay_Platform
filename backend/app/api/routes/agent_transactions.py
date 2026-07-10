"""Agent Management → Transactions / Unassigned / Assignment History / Audit Trail (Phase 6).

Read-only listing endpoints for the Agent Management module, scoped to the caller's merchant
business and restricted to Supervisor / Manager. Purely additive over the existing transaction
data — nothing here creates or mutates a Deposit/Withdrawal/Settlement (assignment itself goes
through the existing demo-gated /assign-agent endpoint). Mounted only when ENVIRONMENT=demo.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    AgentAccount, AgentAssignmentHistory, AgentMaster, AuditLog, Transaction, TxStatus, User,
)
from app.core.deps import get_current_agent_manager

router = APIRouter(prefix="/api/agent-transactions", tags=["agent-transactions"])

# A transaction is only "unassigned & actionable" while still in-flight. Terminal transactions
# (done / rejected / cancelled) are excluded from the recovery worklist — assigning an agent to a
# finished request has no operational value and would clutter the screen with legacy records.
TERMINAL_STATUSES = [
    TxStatus.COMPLETED, TxStatus.DEPOSITED, TxStatus.SUCCESSFUL,
    TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED,
]


async def _agent_maps(db: AsyncSession, business: str):
    agents = {a.id: a for a in (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business))).scalars().all()}
    accounts = {a.id: a for a in (await db.execute(
        select(AgentAccount).where(AgentAccount.merchant_business == business))).scalars().all()}
    return agents, accounts


def _tx_row(t: Transaction, agent: AgentMaster | None, account: AgentAccount | None) -> dict:
    return {
        "id": f"TXN{str(t.id).zfill(3)}",
        "ref": t.ref,
        "type": t.type.value.split("_")[0],          # DEPOSIT / WITHDRAWAL / SETTLEMENT
        "typeFull": t.type.value,
        "memberId": t.member_id,
        "memberName": t.member_name,
        "amount": t.amount,
        "status": t.status.value,
        "assignedAgentId": t.assigned_agent_id,
        "agentCode": agent.agent_id if agent else None,
        "agentName": agent.full_name if agent else None,
        "accountId": t.assigned_agent_account_id,
        "accountRef": account.account_ref if account else None,
        "paymentMethod": account.account_type if account else None,
        "assignedBy": t.assigned_by,
        "assignedAt": t.assigned_at.isoformat() + "Z" if t.assigned_at else None,
        "createdBy": t.creator_username or t.merchant_name,
        "createdAt": t.created_at.isoformat() + "Z" if t.created_at else None,
        "txDate": t.tx_date.isoformat() if t.tx_date else None,
        "txTime": t.tx_time,
    }


@router.get("")
async def list_assigned(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    """Every transaction that HAS an assigned agent (business-scoped). Search / filter / paginate
    client-side, consistent with the other Agent Management tables."""
    business = user.name
    agents, accounts = await _agent_maps(db, business)
    txns = (await db.execute(
        select(Transaction).where(
            Transaction.merchant_name == business, Transaction.assigned_agent_id.is_not(None)
        ).order_by(Transaction.id.desc())
    )).scalars().all()
    return [_tx_row(t, agents.get(t.assigned_agent_id), accounts.get(t.assigned_agent_account_id)) for t in txns]


@router.get("/unassigned")
async def list_unassigned(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    """Unassigned + still-actionable transactions (business-scoped) — the recovery worklist.
    Terminal (done/rejected/cancelled) requests are excluded so legacy records don't clutter it."""
    business = user.name
    txns = (await db.execute(
        select(Transaction).where(
            Transaction.merchant_name == business,
            Transaction.assigned_agent_id.is_(None),
            Transaction.status.notin_(TERMINAL_STATUSES),
        ).order_by(Transaction.id.desc())
    )).scalars().all()
    return [_tx_row(t, None, None) for t in txns]


@router.get("/assignment-history")
async def assignment_history(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    """All agent assignments + reassignments (business-scoped), read-only. Resolves the previous
    agent/account (on a reassignment) to their code/ref for display."""
    business = user.name
    agents, accounts = await _agent_maps(db, business)
    rows = (await db.execute(
        select(AgentAssignmentHistory)
        .where(AgentAssignmentHistory.merchant_business == business)
        .order_by(AgentAssignmentHistory.id.desc())
    )).scalars().all()
    out = []
    for h in rows:
        pa = agents.get(h.prev_agent_master_id) if h.prev_agent_master_id else None
        pc = accounts.get(h.prev_agent_account_id) if h.prev_agent_account_id else None
        out.append({
            "id": h.id,
            "action": h.action,
            "txRef": h.transaction_ref,
            "txType": h.transaction_type,
            "paymentMethod": h.payment_method,
            "prevAgentId": pa.agent_id if pa else None,
            "prevAgentName": pa.full_name if pa else None,
            "newAgentId": h.agent_id,
            "newAgentName": h.agent_name,
            "prevAccountRef": pc.account_ref if pc else None,
            "newAccountRef": h.account_ref,
            "newAccountType": h.account_type,
            "assignedBy": h.assigned_by,
            "createdAt": h.created_at.isoformat() + "Z" if h.created_at else None,
            "note": "Initial assignment" if h.action == "ASSIGN" else "Reassigned",
        })
    return out


@router.get("/all-accounts")
async def all_accounts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    """Every agent account across the business (flat rows) — powers the Agent Accounts report."""
    business = user.name
    agents, _ = await _agent_maps(db, business)
    accts = (await db.execute(
        select(AgentAccount).where(AgentAccount.merchant_business == business).order_by(AgentAccount.id.desc())
    )).scalars().all()
    return [
        {
            "accountRef": a.account_ref,
            "agentCode": agents[a.agent_master_id].agent_id if a.agent_master_id in agents else None,
            "agentName": agents[a.agent_master_id].full_name if a.agent_master_id in agents else None,
            "accountType": a.account_type,
            "label": a.label,
            "currency": a.currency,
            "isDefault": a.is_default,
            "status": a.status,
            "detail": a.account_number or a.upi_id or a.wallet_address or a.qr_linked_ref or "",
            "createdBy": a.created_by,
            "createdAt": a.created_at.isoformat() + "Z" if a.created_at else None,
        }
        for a in accts
    ]


@router.get("/audit")
async def agent_audit(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    """Agent-related audit trail for the caller's business. New rows store the actor's login in
    ``username`` and the business in ``business``; pre-existing rows (before this change) stored the
    business in ``username`` with a NULL ``business`` — the OR clause keeps both visible."""
    business = user.name
    rows = (await db.execute(
        select(AuditLog)
        .where(
            AuditLog.action_type.like("AGENT%"),
            or_(
                AuditLog.business == business,
                and_(AuditLog.business.is_(None), AuditLog.username == business),
            ),
        )
        .order_by(AuditLog.id.desc()).limit(500)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "user": r.username,          # actual operator (login) on new rows
            "role": r.role,              # merchant role (Supervisor/Manager/DEO/…) on new rows
            "business": r.business,
            "action": r.action_type,
            "entityType": r.entity_type,
            "reference": r.entity_id,
            "note": r.new_value or r.reason,
            "createdAt": r.created_at.isoformat() + "Z" if r.created_at else None,
        }
        for r in rows
    ]
