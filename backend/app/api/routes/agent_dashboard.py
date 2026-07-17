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
from app.models.models import AgentAccount, AgentAssignmentHistory, AgentMaster, Transaction, TxStatus, User
# Operator-level access: Supervisors/Managers get the full payload, operators a counts-only one
# (see agent_dashboard). AGENT_MERCHANT_ROLES is the Supervisor/Manager set that gates the money.
from app.core.deps import get_current_agent_operator, AGENT_MERCHANT_ROLES
from app.api.routes.agent_transactions import TERMINAL_STATUSES

router = APIRouter(prefix="/api/agent-dashboard", tags=["agent-dashboard"])

ACCOUNT_TYPES = ["BANK", "UPI", "QR", "CRYPTO"]
TX_TYPES = ["DEPOSIT", "WITHDRAWAL", "SETTLEMENT"]
# Completed = same definition the merchant balance uses (deposits finish COMPLETED/DEPOSITED;
# withdrawals/settlements finish COMPLETED). Only completed txns count toward agent financials.
COMPLETED_STATUSES = {TxStatus.COMPLETED, TxStatus.DEPOSITED}
DEAD_STATUSES = {TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED}


def _m(x: float) -> float:
    return round(x or 0.0, 2)


def _bucketed(counter: Counter, keys: list[str]) -> dict:
    """A {key: count} dict guaranteeing every key in `keys` is present (0 if unseen)."""
    return {k: int(counter.get(k, 0)) for k in keys}


def _ranked(counter: Counter) -> list[dict]:
    return [{"label": k, "count": int(v)} for k, v in counter.most_common()]


@router.get("")
async def agent_dashboard(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_agent_operator),
):
    """Agent dashboard. Supervisors and Managers get the full payload, unchanged.

    An operator (Data Operator et al) is not authorised to see agent money, so the financial
    figures are omitted from the RESPONSE — not merely hidden by the UI, which would still ship
    every amount to their browser. They receive the same structure minus `financial`,
    `agentFinancials` and `financeCharts`, plus the request counts they are allowed to see.
    """
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

    # ── Current assignments + per-agent financials (cumulative, from COMPLETED assigned txns) ──
    # Commission = the agent's Fees % applied to every completed assigned transaction; Available
    # Balance = Deposit − Withdrawal − Commission (opening balance is not modelled → treated as 0).
    agent_by_id = {a.id: a for a in agents}
    by_tx_type: Counter = Counter()
    by_channel: Counter = Counter()           # account type of the assigned account
    per_agent: Counter = Counter()
    fin: dict = {a.id: {"deposit": 0.0, "withdrawal": 0.0, "commission": 0.0, "pending": 0, "completed": 0} for a in agents}
    comm_day: dict = {}; comm_week: dict = {}; comm_month: dict = {}
    for t in assigned:
        base = t.type.value.split("_")[0]
        by_tx_type[base] += 1
        ch = acct_type_by_id.get(t.assigned_agent_account_id)
        if ch:
            by_channel[ch] += 1
        aid = t.assigned_agent_id
        if aid is None:
            continue
        per_agent[aid] += 1
        f = fin.setdefault(aid, {"deposit": 0.0, "withdrawal": 0.0, "commission": 0.0, "pending": 0, "completed": 0})
        if t.status in COMPLETED_STATUSES:
            f["completed"] += 1
            agent = agent_by_id.get(aid)
            # Each leg charges its own fee: deposit → Pay-In, withdrawal → Pay-Out,
            # settlement → Settlement. (The single fees_pct this replaced is retired.)
            _rate = 0.0
            if agent:
                _rate = {"DEPOSIT": agent.pay_in_fee, "WITHDRAWAL": agent.pay_out_fee,
                         "SETTLEMENT": agent.settlement_fee}.get(base) or 0.0
            comm = round((t.amount or 0) * (_rate / 100.0), 2) if agent else 0.0
            f["commission"] += comm
            if base == "DEPOSIT":
                f["deposit"] += (t.amount or 0)
            elif base == "WITHDRAWAL":
                f["withdrawal"] += (t.amount or 0)
            if t.tx_date and comm:
                dk = t.tx_date.isoformat(); comm_day[dk] = _m(comm_day.get(dk, 0.0) + comm)
                wk = t.tx_date.strftime("%G-W%V"); comm_week[wk] = _m(comm_week.get(wk, 0.0) + comm)
                mo = t.tx_date.strftime("%Y-%m"); comm_month[mo] = _m(comm_month.get(mo, 0.0) + comm)
        elif t.status not in DEAD_STATUSES:
            f["pending"] += 1

    # Per-agent financial rows (every agent; zeros if nothing assigned/completed).
    agent_financials = []
    for a in agents:
        f = fin.get(a.id) or {"deposit": 0.0, "withdrawal": 0.0, "commission": 0.0, "pending": 0, "completed": 0}
        agent_financials.append({
            "agentId": a.agent_id, "name": a.full_name, "category": a.category,
            "deposit": _m(f["deposit"]), "withdrawal": _m(f["withdrawal"]), "commission": _m(f["commission"]),
            "availableBalance": _m(f["deposit"] - f["withdrawal"] - f["commission"]),
            "pending": f["pending"], "completed": f["completed"],
        })
    agent_financials.sort(key=lambda r: r["deposit"], reverse=True)
    total_deposit = _m(sum(r["deposit"] for r in agent_financials))
    total_withdrawal = _m(sum(r["withdrawal"] for r in agent_financials))
    total_commission = _m(sum(r["commission"] for r in agent_financials))
    available = _m(total_deposit - total_withdrawal - total_commission)

    def _bars(key, n=None):
        items = [r for r in sorted(agent_financials, key=lambda r: r[key], reverse=True) if r[key] > 0]
        return [{"label": f'{r["agentId"]} · {r["name"]}', "value": r[key]} for r in (items[:n] if n else items)]

    def _trend(d, recent):
        return [{"label": k, "value": v} for k, v in sorted(d.items())[-recent:]]

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

    # Supervisor / Manager only — an operator never receives an amount.
    is_manager = str(user.merchant_role or "").upper() in AGENT_MERCHANT_ROLES
    payload = {
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
    if not is_manager:
        # Counts only: what the operator may see in place of the money.
        payload["counts"] = {
            "deposits": int(by_tx_type.get("DEPOSIT", 0)),
            "withdrawals": int(by_tx_type.get("WITHDRAWAL", 0)),
            "settlements": int(by_tx_type.get("SETTLEMENT", 0)),
        }
        return payload
    payload["financial"] = {
        "openingBalance": 0,
        "totalDeposit": total_deposit,
        "totalWithdrawal": total_withdrawal,
        "totalCommission": total_commission,
        "netBalance": available,          # Opening(0) + Deposit − Withdrawal − Commission
        "availableBalance": available,
    }
    payload["agentFinancials"] = agent_financials
    payload["financeCharts"] = {
        "topDeposit": _bars("deposit", 10),
        "topCommission": _bars("commission", 10),
        "depositByAgent": _bars("deposit"),
        "withdrawalByAgent": _bars("withdrawal"),
        "commissionTrend": {
            "daily": _trend(comm_day, 14),
            "weekly": _trend(comm_week, 8),
            "monthly": _trend(comm_month, 6),
        },
    }
    return payload
