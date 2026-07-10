"""Agent Management → Dashboard (Phase 5).

Read-only analytics for the Non-EPS agent module, scoped to the caller's merchant business and
restricted to Supervisor / Manager. Aggregates the agent inventory (AgentMaster), the account
inventory (AgentAccount), the current transaction assignments (Transaction.assigned_*) and the
assignment activity feed (AgentAssignmentHistory). Counts are small per business, so aggregation
is done in Python (keeps it DB-agnostic). Mounted only when ENVIRONMENT=demo (404 on Production).
"""
from collections import Counter
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import AgentAccount, AgentAssignmentHistory, AgentMaster, Transaction, User
from app.core.deps import get_current_agent_manager
from app.api.routes.agent_transactions import TERMINAL_STATUSES

router = APIRouter(prefix="/api/agent-dashboard", tags=["agent-dashboard"])

ACCOUNT_TYPES = ["BANK", "UPI", "QR", "CRYPTO"]
TX_TYPES = ["DEPOSIT", "WITHDRAWAL", "SETTLEMENT"]


def _bucketed(counter: Counter, keys: list[str]) -> dict:
    """A {key: count} dict guaranteeing every key in `keys` is present (0 if unseen)."""
    return {k: int(counter.get(k, 0)) for k in keys}


def _ranked(counter: Counter) -> list[dict]:
    return [{"label": k, "count": int(v)} for k, v in counter.most_common()]


@router.get("")
async def agent_dashboard(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_manager),
):
    business = user.name
    agents = (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business)
    )).scalars().all()
    accounts = (await db.execute(
        select(AgentAccount).where(AgentAccount.merchant_business == business)
    )).scalars().all()
    assigned = (await db.execute(
        select(Transaction).where(
            Transaction.merchant_name == business, Transaction.assigned_agent_id.is_not(None)
        )
    )).scalars().all()
    unassigned_count = (await db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.merchant_name == business,
            Transaction.assigned_agent_id.is_(None),
            Transaction.status.notin_(TERMINAL_STATUSES),
        )
    )).scalar() or 0
    history = (await db.execute(
        select(AgentAssignmentHistory)
        .where(AgentAssignmentHistory.merchant_business == business)
        .order_by(AgentAssignmentHistory.id.desc())
    )).scalars().all()

    # ── Agent inventory ──
    agent_active = sum(1 for a in agents if a.status == "ACTIVE")
    agents_by_country = Counter(a.country for a in agents if a.country)
    agents_by_category = Counter(a.category for a in agents if a.category)

    # ── Account inventory ──
    acct_active = sum(1 for a in accounts if a.status == "ACTIVE")
    accounts_by_type = Counter(a.account_type for a in accounts)
    acct_type_by_id = {a.id: a.account_type for a in accounts}

    # ── Current assignments (what agents are handling now) ──
    by_tx_type: Counter = Counter()
    by_channel: Counter = Counter()           # account type of the assigned account
    per_agent: Counter = Counter()
    for t in assigned:
        by_tx_type[t.type.value.split("_")[0]] += 1
        ch = acct_type_by_id.get(t.assigned_agent_account_id)
        if ch:
            by_channel[ch] += 1
        if t.assigned_agent_id:
            per_agent[t.assigned_agent_id] += 1

    agent_by_id = {a.id: a for a in agents}
    top_agents = [
        {
            "agentId": agent_by_id[aid].agent_id if aid in agent_by_id else str(aid),
            "name": agent_by_id[aid].full_name if aid in agent_by_id else "—",
            "count": int(cnt),
        }
        for aid, cnt in per_agent.most_common(5)
    ]

    # ── Assignment activity feed ──
    reassignments = sum(1 for h in history if h.action == "REASSIGN")
    recent = [
        {
            "id": h.id,
            "action": h.action,
            "txRef": h.transaction_ref,
            "txType": h.transaction_type,
            "agentId": h.agent_id,
            "agentName": h.agent_name,
            "accountRef": h.account_ref,
            "accountType": h.account_type,
            "assignedBy": h.assigned_by,
            "createdAt": h.created_at.isoformat() + "Z" if h.created_at else None,
        }
        for h in history[:15]
    ]

    return {
        "agents": {"total": len(agents), "active": agent_active, "inactive": len(agents) - agent_active},
        "accounts": {
            "total": len(accounts), "active": acct_active, "inactive": len(accounts) - acct_active,
            "byType": _bucketed(accounts_by_type, ACCOUNT_TYPES),
        },
        "agentsByCountry": _ranked(agents_by_country),
        "agentsByCategory": _bucketed(agents_by_category, ["CASH", "BANK_TRANSFER", "CRYPTO"]),
        "assignments": {
            "totalTransactions": len(assigned),
            "unassignedTransactions": int(unassigned_count),
            "reassignments": reassignments,
            "byTxType": _bucketed(by_tx_type, TX_TYPES),
            "byChannel": _bucketed(by_channel, ACCOUNT_TYPES),
        },
        "topAgents": top_agents,
        "recent": recent,
    }
