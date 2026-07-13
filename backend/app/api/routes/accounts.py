from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import AccountMaster, AccountTransaction, AdminUpi, Transaction, TxStatus, User, UserRole
from app.core.deps import get_current_admin
from app.core.cache import cache_get, cache_set
from app.schemas.schemas import AccountCreate, ReasonRequest
from app.api.routes.system_logs import log_event, record_audit
from app.api.routes.transactions import compute_balance, _COMPLETED_STATUSES, _kind, _completed, _member_label

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _monthly_average_balance(biz_txns: list[Transaction], pay_in_rate: float, pay_out_rate: float) -> float:
    """Monthly Average Balance: the average of the daily end-of-day settled balance across
    the current calendar month (reconstructed from completed transactions — always accurate,
    no nightly job to miss). Floored at 0."""
    completed = [t for t in biz_txns if t.status in _COMPLETED_STATUSES]
    if not completed:
        return 0.0
    today = date.today()
    day = today.replace(day=1)
    total, days = 0.0, 0
    while day <= today:
        dep = sum(t.amount for t in completed if t.type.value.startswith("DEPOSIT") and t.tx_date <= day)
        wd = sum(t.amount for t in completed if t.type.value.startswith("WITHDRAWAL") and t.tx_date <= day)
        st = sum(t.amount for t in completed if t.type.value.startswith("SETTLEMENT") and t.tx_date <= day)
        bal = dep - dep * pay_in_rate - st - wd - wd * pay_out_rate
        total += max(0.0, bal)
        days += 1
        day += timedelta(days=1)
    return round(total / days, 2) if days else 0.0


@router.get("/balances")
async def account_balances(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Per admin bank account: how much each merchant has deposited into it, alongside that
    merchant's Available Balance (AB), Running Balance (RB) and Monthly Average Balance (MAB).
    Deposits are routed to an account via the reference the agent sends (Transaction.admin_ref)."""
    # Cached ~5s: global (identical for every admin) and very heavy — loads all accounts + merchants
    # + transactions and aggregates. Read-only; financial mutations never touch this cache.
    _hit = await cache_get("c:accounts:balances")
    if _hit is not None:
        return _hit
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().all()
    merchants = (await db.execute(select(User).where(User.role == UserRole.MERCHANT))).scalars().all()
    txns = (await db.execute(select(Transaction))).scalars().all()

    rep_by_name: dict[str, User] = {}        # one representative merchant user per business name
    for m in merchants:
        rep_by_name.setdefault(m.name, m)

    # AB / RB / MAB are business-level (a business shares one balance pool); compute once each.
    bal_by_name: dict[str, dict] = {}
    for name, user in rep_by_name.items():
        summ = await compute_balance(db, user)
        biz_txns = [t for t in txns if t.merchant_name == name]
        summ["mab"] = _monthly_average_balance(biz_txns, (user.pay_in_fee or 0) / 100, (user.pay_out_fee or 0) / 100)
        bal_by_name[name] = summ

    # Member → most recent receiving account (from account↔transaction links). Lets a member's
    # withdrawals/settlements be attributed back to the account they deposit into.
    links = (await db.execute(select(AccountTransaction).order_by(AccountTransaction.id.desc()))).scalars().all()
    member_acct: dict[str, str] = {}
    for l in links:
        if l.member_id and l.reference_number and l.member_id not in member_acct:
            member_acct[l.member_id] = l.reference_number

    # Linked UPIs grouped by their parent account.
    upis = (await db.execute(select(AdminUpi))).scalars().all()
    upis_by_acct: dict[str, list] = defaultdict(list)
    for u in upis:
        if u.account_ref:
            upis_by_acct[u.account_ref].append({"id": u.id, "label": u.label, "upiId": u.upi_id, "status": u.status})

    # Per-account money movements from completed transactions. Deposits route via admin_ref
    # (bank vs UPI distinguished by admin_upi_id); withdrawals/settlements via the member map.
    dep: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))   # account → merchant → amount
    acct_users: dict[str, set[int]] = defaultdict(set)   # account → distinct depositing users (operators)
    bank_dep: dict[str, float] = defaultdict(float)
    upi_dep: dict[str, float] = defaultdict(float)
    dep_high: dict[str, float] = {}   # account → highest single successful deposit ever received
    dep_low: dict[str, float] = {}    # account → lowest single successful deposit ever received
    acct_wd: dict[str, float] = defaultdict(float)
    acct_st: dict[str, float] = defaultdict(float)
    # Only completed transactions affect an account's balance. A deposit completes as COMPLETED
    # (legacy) or DEPOSITED (new admin final-approval); withdrawals/settlements complete as COMPLETED.
    for t in txns:
        ty = t.type.value
        if ty.startswith("DEPOSIT"):
            if t.status in _COMPLETED_STATUSES and t.admin_ref:
                dep[t.admin_ref][t.merchant_name] += t.amount
                acct_users[t.admin_ref].add(t.merchant_id)
                (upi_dep if t.admin_upi_id else bank_dep)[t.admin_ref] += t.amount
                # Track the largest/smallest individual deposit received into this account.
                if t.admin_ref not in dep_high or t.amount > dep_high[t.admin_ref]:
                    dep_high[t.admin_ref] = t.amount
                if t.admin_ref not in dep_low or t.amount < dep_low[t.admin_ref]:
                    dep_low[t.admin_ref] = t.amount
        elif ty.startswith("WITHDRAWAL"):
            if t.status == TxStatus.COMPLETED and t.member_id in member_acct:
                acct_wd[member_acct[t.member_id]] += t.amount
        elif ty.startswith("SETTLEMENT"):
            if t.status == TxStatus.COMPLETED and t.member_id in member_acct:
                acct_st[member_acct[t.member_id]] += t.amount

    out = []
    for a in accounts:
        ref = a.reference_number
        rows = []
        for name, deposited in dep.get(ref, {}).items():
            b = bal_by_name.get(name, {})
            rep = rep_by_name.get(name)
            rows.append({
                "merchantName": name,
                "merchantCode": rep.merchant_code if rep else None,
                "deposited": round(deposited, 2),
                "available": round(b.get("available", 0.0), 2),     # AB (merchant-level)
                "runningBalance": round(b.get("runningBalance", 0.0), 2),  # RB (merchant-level)
                "mab": b.get("mab", 0.0),                           # MAB (merchant-level)
            })
        rows.sort(key=lambda r: r["deposited"], reverse=True)
        bank_d = bank_dep.get(ref, 0.0)
        upi_d = upi_dep.get(ref, 0.0)
        total_d = bank_d + upi_d
        wd = acct_wd.get(ref, 0.0)
        st = acct_st.get(ref, 0.0)
        out.append({
            "referenceNumber": ref,
            "accountName": a.account_name,
            "accountHolder": a.account_name,      # AccountMaster has no separate holder field
            "accountNumber": a.account_number,
            "ifscCode": a.ifsc_code,
            "branch": a.branch,
            "bankName": a.bank_name,
            "status": a.status,
            # Account-level money received into THIS account — bank + all linked UPIs roll up.
            "bankDeposited": round(bank_d, 2),
            "upiDeposited": round(upi_d, 2),
            "totalDeposited": round(total_d, 2),
            "highestDeposit": round(dep_high.get(ref, 0.0), 2),
            "lowestDeposit": round(dep_low.get(ref, 0.0), 2),
            # Recorded high-water marks (stored on the account, auto-updated on completion):
            # highestCredit on deposit approval, highestDebit on a completed withdrawal/settlement.
            "highestCredit": round(a.highest_credit or 0.0, 2),
            "highestDebit": round(a.highest_debit or 0.0, 2),
            "withdrawals": round(wd, 2),
            "settlements": round(st, 2),
            "available": round(total_d - wd - st, 2),   # deposits − withdrawals − settlements
            "linkedUpis": upis_by_acct.get(ref, []),
            "userCount": len(acct_users.get(ref, set())),   # distinct depositing users (operators)
            "merchants": rows,
        })
    await cache_set("c:accounts:balances", out, 5)
    return out


@router.get("/{ref}/statement")
async def account_statement(
    ref: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Bank-statement ledger for a SINGLE account: every transaction routed to this account —
    deposits via ``Transaction.admin_ref``; withdrawals/settlements via the member→account map
    (the exact attribution used by /balances). Rows are shaped identically to the Reports
    payload so the frontend reuses the same Agent Ledger renderer (Opening/Running/Closing
    balance + PDF/Excel/CSV export). No balance logic is duplicated here — this only scopes
    transactions to the account; the running-balance math stays in the shared ledger view."""
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")

    txns = (await db.execute(select(Transaction))).scalars().all()

    # Member → most recent receiving account — same source as account_balances, so a member's
    # withdrawals/settlements attribute back to the account they deposit into.
    links = (await db.execute(
        select(AccountTransaction).order_by(AccountTransaction.id.desc())
    )).scalars().all()
    member_acct: dict[str, str] = {}
    for l in links:
        if l.member_id and l.reference_number and l.member_id not in member_acct:
            member_acct[l.member_id] = l.reference_number

    def _belongs(t: Transaction) -> bool:
        if _kind(t) == "deposit":
            return t.admin_ref == ref
        # withdrawals / settlements attribute via the member's receiving account
        return bool(t.member_id and member_acct.get(t.member_id) == ref)

    rows = [{
        "ref": t.ref, "memberId": t.member_id, "member": _member_label(t),
        "business": t.merchant_name,
        "type": _kind(t), "depositType": t.deposit_type, "amount": round(t.amount, 2),
        "status": t.status.value, "date": str(t.tx_date), "time": t.tx_time,
        "createdAt": (t.created_at.isoformat() + "Z") if t.created_at else None,
        "completed": _completed(t),
        "cancelReason": t.cancel_reason,
        "paymentMethod": t.deposit_type if _kind(t) == "deposit" else (t.payout_mode or None),
        "approvedBy": t.approved_by, "processedBy": t.processed_by,
        "agentCode": t.agent_code, "riskLevel": "HIGH" if t.high_risk else "LOW",
        "availableBalance": None,
    } for t in txns if _belongs(t)]
    rows.sort(key=lambda r: r["createdAt"] or "", reverse=True)
    return {"referenceNumber": ref, "accountName": acc.account_name, "transactions": rows}


# A player counts as "Active" if they have moved money through this account within the last
# 90 days (there is no separate player entity/status in the schema — it is derived from the
# transaction history, the same source everything else in this popup uses).
_ACTIVE_WINDOW_DAYS = 90


@router.get("/{ref}/users")
async def account_users(
    ref: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Drill-down for the Account Management popup: the Users (merchant operators) who have
    deposited into THIS account, and — nested under each — the Players (Membership / Player
    IDs like WININ25504) they transacted for.

    Attribution is identical to /balances and /statement so the figures reconcile with the
    account list: deposits route via ``Transaction.admin_ref``; withdrawals attribute back via
    the member→receiving-account map. Everything here is scoped to this single account, so a
    User's "deposited through this account" equals the sum of their Players' deposits shown.
    """
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == ref)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")

    txns = (await db.execute(select(Transaction))).scalars().all()

    # Member → most recent receiving account — same source as /balances, so a member's
    # withdrawals attribute back to the account they deposit into.
    links = (await db.execute(
        select(AccountTransaction).order_by(AccountTransaction.id.desc())
    )).scalars().all()
    member_acct: dict[str, str] = {}
    for l in links:
        if l.member_id and l.reference_number and l.member_id not in member_acct:
            member_acct[l.member_id] = l.reference_number

    def _pid(t: Transaction) -> str:
        return (t.member_id or "").strip().upper()

    # Per-member money attributed to THIS account (by member id, not creator) — withdrawals
    # complete as COMPLETED and attribute via the member map. Kept separate from the user→player
    # deposit hierarchy so a withdrawal made by a different operator still lands on its player.
    wd_by_member: dict[str, float] = defaultdict(float)
    last_activity: dict[str, datetime] = {}   # member id → most recent completed movement here

    def _mark_active(pid: str, ts: datetime | None):
        if pid and ts and (pid not in last_activity or ts > last_activity[pid]):
            last_activity[pid] = ts

    for t in txns:
        ty = t.type.value
        if ty.startswith("WITHDRAWAL") and t.status == TxStatus.COMPLETED and t.member_id:
            if member_acct.get(t.member_id) == ref:
                wd_by_member[_pid(t)] += t.amount
                _mark_active(_pid(t), t.created_at)

    # Deposit-derived hierarchy: creating User → Player. Deposits routed to this account define
    # which users/players belong here (the business relationship Account → User → Player).
    users: dict[int, dict] = {}
    for t in txns:
        if not t.type.value.startswith("DEPOSIT"):
            continue
        if t.status not in _COMPLETED_STATUSES or t.admin_ref != ref:
            continue
        uid = t.merchant_id
        u = users.get(uid)
        if u is None:
            u = users[uid] = {
                "merchant_id": uid,
                "userName": (t.creator_username or t.merchant_name),
                "userId": None,
                "deposited": 0.0,
                "players": {},   # player id → node
            }
        u["deposited"] += t.amount

        pid = _pid(t)
        players = u["players"]
        p = players.get(pid)
        if p is None:
            p = players[pid] = {
                "playerId": t.member_id or "—",
                "playerName": (t.member_name or "").strip() or "—",
                "deposits": 0.0,
                "createdAt": t.created_at,
            }
        p["deposits"] += t.amount
        if t.member_name and (p["playerName"] == "—"):
            p["playerName"] = t.member_name.strip()
        # Earliest deposit into this account = when this player started using it.
        if t.created_at and (p["createdAt"] is None or t.created_at < p["createdAt"]):
            p["createdAt"] = t.created_at
        _mark_active(pid, t.created_at)

    # Enrich each User with their canonical name / User ID from the users table.
    uids = list(users.keys())
    urows = (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all() if uids else []
    urow_by_id = {u.id: u for u in urows}
    for uid, u in users.items():
        rec = urow_by_id.get(uid)
        if rec:
            u["userName"] = rec.full_name or u["userName"] or rec.username
            u["userId"] = rec.merchant_code or rec.username

    cutoff = datetime.utcnow() - timedelta(days=_ACTIVE_WINDOW_DAYS)

    users_out = []
    total_players = 0
    for uid, u in users.items():
        players_out = []
        for pid, p in u["players"].items():
            la = last_activity.get(pid)
            players_out.append({
                "playerId": p["playerId"],
                "playerName": p["playerName"],
                "status": "Active" if (la and la >= cutoff) else "Inactive",
                "deposits": round(p["deposits"], 2),
                "withdrawals": round(wd_by_member.get(pid, 0.0), 2),
                "createdAt": (p["createdAt"].isoformat() + "Z") if p["createdAt"] else None,
            })
        players_out.sort(key=lambda r: r["deposits"], reverse=True)
        total_players += len(players_out)
        users_out.append({
            "merchantId": uid,
            "userName": u["userName"],
            "userId": u["userId"],
            "totalPlayers": len(players_out),
            "deposited": round(u["deposited"], 2),
            "players": players_out,
        })
    users_out.sort(key=lambda r: r["deposited"], reverse=True)

    return {
        "referenceNumber": ref,
        "accountHolder": acc.account_name,
        "accountNumber": acc.account_number,
        "totalUsers": len(users_out),
        "totalPlayers": total_players,
        "totalDeposited": round(sum(u["deposited"] for u in users_out), 2),
        "users": users_out,
    }


def _a(a: AccountMaster, merchant_name: str | None = None) -> dict:
    return {
        "id": a.id,
        "referenceNumber": a.reference_number,
        "accountName": a.account_name,
        "accountNumber": a.account_number,
        "ifscCode": a.ifsc_code,
        "bankName": a.bank_name,
        "branch": a.branch,
        "accountType": a.account_type.value if hasattr(a.account_type, "value") else a.account_type,
        "status": a.status,
        "createdDate": str(a.created_date),
        "createdTime": a.created_time,
        "lastMaintenanceDate": str(a.last_maintenance_date) if a.last_maintenance_date else None,
        "lastMaintenanceTime": a.last_maintenance_time,
        "highestCredit": round(a.highest_credit or 0.0, 2),
        "highestDebit": round(a.highest_debit or 0.0, 2),
        "merchantName": merchant_name or a.account_name,
    }


async def _merchant_name_map(db: AsyncSession) -> dict[str, str]:
    """Map account reference_number -> a merchant name, derived via account_transaction links."""
    links = (await db.execute(select(AccountTransaction))).scalars().all()
    if not links:
        return {}
    tx_refs = {l.transaction_reference_number for l in links if l.transaction_reference_number}
    tx_map: dict[str, str] = {}
    if tx_refs:
        txs = (await db.execute(select(Transaction).where(Transaction.ref.in_(tx_refs)))).scalars().all()
        tx_map = {t.ref: t.merchant_name for t in txs}
    out: dict[str, str] = {}
    for l in links:
        if l.reference_number in out:
            continue
        name = tx_map.get(l.transaction_reference_number or "")
        if name:
            out[l.reference_number] = name
    return out


@router.get("")
async def list_accounts(
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    _hit = await cache_get("c:accounts:list")
    if _hit is not None:
        out = _hit
    else:
        accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().all()
        name_map = await _merchant_name_map(db)
        out = [_a(a, name_map.get(a.reference_number)) for a in accounts]
        # Cached ~5s: the base account list (global) is the heavy part; the q-filter runs on the
        # cached result so any search term benefits. Read-only.
        await cache_set("c:accounts:list", out, 5)
    if q:
        ql = q.lower()
        out = [a for a in out if ql in (a["merchantName"] or "").lower()]
    return out


@router.get("/for-member/{member_id}")
async def last_account_for_member(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """The bank account most recently assigned to this Member ID (active only).

    Drives reuse: a repeat deposit for the same Member ID defaults to the same account.
    """
    link = (await db.execute(
        select(AccountTransaction)
        .where(AccountTransaction.member_id == member_id)
        .order_by(AccountTransaction.id.desc())
    )).scalars().first()
    if not link:
        return {"referenceNumber": None}
    acc = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == link.reference_number)
    )).scalar_one_or_none()
    if not acc or (acc.status or "").upper() != "ACTIVE":
        return {"referenceNumber": None}
    return {"referenceNumber": acc.reference_number}


@router.get("/{reference_number}")
async def get_account(
    reference_number: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    a = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == reference_number))
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Account not found")
    name_map = await _merchant_name_map(db)
    return _a(a, name_map.get(a.reference_number))


@router.post("")
async def create_account(
    data: AccountCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    ref = data.reference_number
    if not ref:
        # Generate a unique reference number like ACC0000007
        last = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().first()
        next_id = (last.id + 1) if last else 1
        ref = f"ACC{str(next_id).zfill(7)}"

    existing = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == ref))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Reference number already exists")

    now = datetime.now()
    acc = AccountMaster(
        reference_number=ref,
        account_name=data.account_name,
        account_number=data.account_number,
        ifsc_code=data.ifsc_code,
        bank_name=data.bank_name,
        branch=data.branch,
        account_type=data.account_type,
        status=data.status,
        created_date=date.today(),
        created_time=now.strftime("%H:%M:%S"),
        last_maintenance_date=date.today(),
        last_maintenance_time=now.strftime("%H:%M:%S"),
        highest_credit=max(0.0, data.highest_credit or 0.0),
        # The entered Highest Debit seeds both the auto-raising high-water mark and the FIXED
        # low-debit alert threshold. Thereafter highest_debit rises on larger debits; the
        # threshold stays put so "debit below the set amount" alerts remain stable.
        highest_debit=max(0.0, data.highest_debit or 0.0),
        debit_alert_threshold=max(0.0, data.highest_debit or 0.0),
    )
    db.add(acc)
    await db.flush()

    # Optionally link a UPI ID to this account on creation.
    if data.upiId and "@" in data.upiId:
        db.add(AdminUpi(
            label=data.account_name, upi_id=data.upiId.strip(), account_ref=ref,
            status="ACTIVE", created_time=now.strftime("%H:%M:%S"),
        ))
        await db.flush()

    # Optionally link the account to a merchant's most recent transaction.
    if data.merchant_id:
        tx = (
            await db.execute(
                select(Transaction)
                .where(Transaction.merchant_id == data.merchant_id)
                .order_by(Transaction.created_at.desc())
            )
        ).scalars().first()
        link = AccountTransaction(
            reference_number=ref,
            member_id=tx.member_id if tx else None,
            transaction_reference_number=tx.ref if tx else None,
            transaction_date=date.today(),
            transaction_time=now.strftime("%H:%M:%S"),
        )
        db.add(link)
        await db.flush()

    await db.refresh(acc)
    await log_event(db, "ACCOUNT_CREATED", f"Bank account {acc.reference_number} ({acc.bank_name}) created", actor=_)
    name_map = await _merchant_name_map(db)
    return _a(acc, name_map.get(acc.reference_number))


@router.patch("/{reference_number}/toggle")
async def toggle_account(
    reference_number: str,
    request: Request,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Flip an account's status between ACTIVE and INACTIVE (reason required)."""
    acc = (
        await db.execute(select(AccountMaster).where(AccountMaster.reference_number == reference_number))
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    reason = (data.reason if data else None) or ""
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required")
    was = acc.status
    acc.status = "INACTIVE" if (acc.status or "").upper() == "ACTIVE" else "ACTIVE"
    await db.flush()
    ip = request.client.host if request and request.client else None
    await log_event(db, "ACCOUNT_TOGGLED", f"Account {acc.reference_number} set {acc.status} by {actor.name} — reason: {reason}", actor=actor)
    await record_audit(db, "ACCOUNT_TOGGLED", actor=actor, entity_type="account", entity_id=acc.reference_number,
                       old=was, new=acc.status, reason=reason, ip=ip)
    await db.refresh(acc)
    name_map = await _merchant_name_map(db)
    return _a(acc, name_map.get(acc.reference_number))
