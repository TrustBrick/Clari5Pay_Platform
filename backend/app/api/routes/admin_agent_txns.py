"""Admin Portal → Agent Management (read-only monitoring).

A MONITORING VIEW over the isolated Agent Transaction subsystem, for Admins and Super Admins.
Every endpoint here is a GET and every one of them is gated by ``get_current_admin`` — there is no
create, approve, reject, upload, complete, edit, cancel or status change anywhere in this module,
so the Admin Portal cannot perform an Agent Module action even by calling the API directly.

The Agent Module's own write endpoints stay closed to Admins for the same reason from the other
side: they all depend on ``get_current_agent_operator``, which admits MERCHANT users only, so an
Admin token is refused with 403 by every one of them. That is the existing authorisation
architecture — nothing is widened, weakened or bypassed here.

Nothing is recomputed either: the figures come from the SAME aggregation the Merchant Agent
Dashboard uses (``_overview_payload`` / ``_agent_performance`` in agent_txns.py, called without a
business filter so they span every merchant), and the transaction feed reuses ``_row``. There is
therefore one statistics implementation, not a parallel Admin one that could drift.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import AgentMaster, AgentTransaction, AgentTransactionAudit, User
from app.core.deps import get_current_admin
# The one implementation of the agent ledger's shape and maths — imported, never re-derived.
from app.api.routes.agent_txns import (
    _agent_performance, _ist_parts, _ist_day_bounds, _overview_payload, _row, _txn_rate,
    COMPLETED_STATUSES, FINAL_STATUSES, REJECTED_STATUSES,
)

router = APIRouter(prefix="/api/admin/agent-txns", tags=["admin-agent-management"])


def _admin_row(t: AgentTransaction) -> dict:
    """The standard agent transaction row plus the owning merchant, which only the Admin view needs
    (a merchant operator only ever sees their own business, so `_row` does not carry it)."""
    return {**_row(t), "merchantBusiness": t.merchant_business}


@router.get("/overview")
async def admin_overview(business: str | None = None, db: AsyncSession = Depends(get_db),
                         _: User = Depends(get_current_admin)):
    """Platform-wide Agent Module overview — the Merchant Agent Dashboard's own figures, computed
    across every merchant business (or one, when `business` is given).

    Returns the merchant dashboard's `cards` / `byAgent` / `trend` / `recent` payload, the same
    `performance` block that dashboard's financial summary is built from, and an `agents`
    inventory (total / active / inactive) so the Admin sees agent counts alongside the money.
    """
    scope = (business or "").strip() or None
    payload = await _overview_payload(db, scope)
    perf = await _agent_performance(db, scope)

    _agent_q = select(AgentMaster)
    if scope is not None:
        _agent_q = _agent_q.where(AgentMaster.merchant_business == scope)
    agents = (await db.execute(_agent_q)).scalars().all()
    active = sum(1 for a in agents if str(a.status).upper() == "ACTIVE")

    payload["performance"] = perf
    payload["agents"] = {
        "total": len(agents), "active": active, "inactive": len(agents) - active,
        # How the agent estate is split across the three categories the module supports.
        "byCategory": {
            k: sum(1 for a in agents if str(a.category or "").upper() == k)
            for k in ("CASH", "BANK_TRANSFER", "CRYPTO")
        },
        # Merchants that actually operate agents — the count behind the business filter.
        "merchants": len({a.merchant_business for a in agents if a.merchant_business}),
    }
    payload["byMerchant"] = await _by_merchant(db, scope)
    # The shared payload's `recent` rows carry no merchant (a merchant operator only ever sees
    # their own), so re-attach it here — the Admin's recent-activity table names the business.
    _recent_ids = [r["id"] for r in payload.get("recent", [])]
    if _recent_ids:
        _by_id = {t.id: t for t in (await db.execute(select(AgentTransaction).where(
            AgentTransaction.id.in_(_recent_ids)))).scalars().all()}
        payload["recent"] = [{**r, "merchantBusiness": (_by_id[r["id"]].merchant_business
                                                        if r["id"] in _by_id else None)}
                             for r in payload["recent"]]
    payload["scope"] = scope
    return payload


@router.get("/businesses")
async def admin_businesses(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """Merchant businesses that have Agent Module activity — drives the Admin's business filter."""
    from_agents = (await db.execute(select(AgentMaster.merchant_business).distinct())).scalars().all()
    from_txns = (await db.execute(select(AgentTransaction.merchant_business).distinct())).scalars().all()
    return sorted({b for b in [*from_agents, *from_txns] if b})


@router.get("/agents")
async def admin_agents(business: str | None = None, db: AsyncSession = Depends(get_db),
                       _: User = Depends(get_current_admin)):
    """Every agent with its lifetime performance — the same per-agent rows the Merchant dashboard
    computes, plus the owning merchant so the Admin can tell whose agent it is."""
    scope = (business or "").strip() or None
    perf = await _agent_performance(db, scope)
    _agent_q = select(AgentMaster)
    if scope is not None:
        _agent_q = _agent_q.where(AgentMaster.merchant_business == scope)
    masters = {a.id: a for a in (await db.execute(_agent_q)).scalars().all()}
    rows = []
    for r in perf["agents"]:
        a = masters.get(r["agentMasterId"])
        rows.append({
            **r,
            "merchantBusiness": a.merchant_business if a else None,
            "state": a.state if a else None,
            "location": a.location if a else None,
            "payInFee": (a.pay_in_fee or 0.0) if a else 0.0,
            "payOutFee": (a.pay_out_fee or 0.0) if a else 0.0,
            "settlementFee": (a.settlement_fee or 0.0) if a else 0.0,
        })
    return {"overall": perf["overall"], "agents": rows, "rankings": perf["rankings"]}


@router.get("/paged")
async def admin_list_paged(business: str | None = None, status: str | None = None,
                           txn_type: str | None = None, txn_method: str | None = None,
                           search: str | None = None, date: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           page: int = 1, page_size: int = 10,
                           db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    """Read-only, cross-merchant agent transaction feed — the Merchant module's `/paged` filters
    and ordering, widened to every business and with a `business` filter added. Filtering, counting
    and paging all run in Postgres; only one page crosses the wire."""
    stmt = select(AgentTransaction)
    if business and business.strip():
        stmt = stmt.where(AgentTransaction.merchant_business == business.strip())
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        if wanted:
            stmt = stmt.where(AgentTransaction.status.in_(wanted))
    if txn_type:
        stmt = stmt.where(AgentTransaction.txn_type == txn_type.strip().upper())
    if txn_method:
        stmt = stmt.where(AgentTransaction.txn_method == txn_method.strip().upper())
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(AgentTransaction.reference_number).like(like),
            func.lower(AgentTransaction.membership_id).like(like),
            func.lower(AgentTransaction.agent_code).like(like),
            func.lower(AgentTransaction.membership_name).like(like),
            func.lower(AgentTransaction.merchant_business).like(like),
            func.lower(AgentTransaction.created_by).like(like),
        ))
    if date:
        s, e = _ist_day_bounds(date)
        stmt = stmt.where(AgentTransaction.created_at >= s, AgentTransaction.created_at < e)
    if date_from:
        s, _e = _ist_day_bounds(date_from)
        stmt = stmt.where(AgentTransaction.created_at >= s)
    if date_to:
        _s, e = _ist_day_bounds(date_to)
        stmt = stmt.where(AgentTransaction.created_at < e)

    page_size = page_size if page_size in (10, 25, 50, 100) else 10
    page = page if page and page >= 1 else 1
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0)
    rows = (await db.execute(
        stmt.order_by(AgentTransaction.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # Per-leg commission enrichment — batched agent fetch (no N+1), the same maths as the
    # Merchant feed so a transaction reads identically in both portals.
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()} if rows else {}
    items = []
    for r in rows:
        rate = _txn_rate(r, agents.get(r.agent_master_id))
        amt = r.amount or 0.0
        commission = round(amt * rate, 2)
        d = _admin_row(r)
        d["commissionPct"] = round(rate * 100, 4)
        d["commissionAmount"] = commission
        d["netAmount"] = round(amt - commission, 2) if r.txn_type == "DEPOSIT" else round(amt + commission, 2)
        items.append(d)
    return {
        "items": items, "total": total, "page": page, "pageSize": page_size,
        "totalPages": (total + page_size - 1) // page_size if page_size else 0,
    }


async def _load(db: AsyncSession, txn_id: int) -> AgentTransaction:
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    return t


@router.get("/{txn_id}")
async def admin_txn(txn_id: int, db: AsyncSession = Depends(get_db),
                    _: User = Depends(get_current_admin)):
    """One agent transaction, any business — read-only."""
    return _admin_row(await _load(db, txn_id))


@router.get("/{txn_id}/audit")
async def admin_txn_audit(txn_id: int, db: AsyncSession = Depends(get_db),
                          _: User = Depends(get_current_admin)):
    """Who did what, and when — the transaction's existing audit trail (agent_transaction_audit),
    exposed unchanged. This is the module's own record of every action; no second audit system is
    introduced for the Admin view. Each row names the actor and the merchant role they acted in,
    which is what identifies the operator, the Manager who reviewed it and the Data Operator who
    completed the payment."""
    await _load(db, txn_id)
    rows = (await db.execute(select(AgentTransactionAudit).where(
        AgentTransactionAudit.agent_transaction_id == txn_id
    ).order_by(AgentTransactionAudit.id.desc()))).scalars().all()
    out = []
    for r in rows:
        iso, d, tm = _ist_parts(r.created_at)
        out.append({
            "id": r.id, "action": r.action, "oldAmount": r.old_amount, "newAmount": r.new_amount,
            "note": r.note, "approverName": r.approver_name, "actor": r.actor_username,
            "role": r.actor_role, "createdAt": iso, "createdDate": d, "createdTime": tm,
        })
    return out


async def _by_merchant(db: AsyncSession, scope: str | None) -> list[dict]:
    """Per-merchant Agent Module activity — the breakdown only the Admin has a use for, since a
    merchant operator never sees another business. Uses the module's own completed / rejected /
    in-flight definitions, so a merchant's row here adds up to what that merchant sees."""
    _q = select(AgentTransaction)
    if scope is not None:
        _q = _q.where(AgentTransaction.merchant_business == scope)
    txns = (await db.execute(_q)).scalars().all()
    out: dict[str, dict] = {}
    for t in txns:
        b = t.merchant_business or "—"
        r = out.setdefault(b, {"business": b, "total": 0, "deposits": 0, "withdrawals": 0,
                               "settlements": 0, "completed": 0, "rejected": 0, "pending": 0,
                               "completedAmount": 0.0})
        r["total"] += 1
        r[{"DEPOSIT": "deposits", "WITHDRAWAL": "withdrawals"}.get(t.txn_type, "settlements")] += 1
        if t.status in COMPLETED_STATUSES:
            r["completed"] += 1
            r["completedAmount"] += t.amount or 0.0
        elif t.status in REJECTED_STATUSES:
            r["rejected"] += 1
        if t.status not in FINAL_STATUSES:
            r["pending"] += 1
    for r in out.values():
        r["completedAmount"] = round(r["completedAmount"], 2)
    return sorted(out.values(), key=lambda r: -r["total"])
