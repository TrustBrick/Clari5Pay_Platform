from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import AccountMaster, AccountTransaction, Transaction, TxStatus, User, UserRole
from app.core.deps import get_current_admin
from app.schemas.schemas import AccountCreate, ReasonRequest
from app.api.routes.system_logs import log_event, record_audit
from app.api.routes.transactions import compute_balance

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _monthly_average_balance(biz_txns: list[Transaction], pay_in_rate: float, pay_out_rate: float) -> float:
    """Monthly Average Balance: the average of the daily end-of-day settled balance across
    the current calendar month (reconstructed from completed transactions — always accurate,
    no nightly job to miss). Floored at 0."""
    completed = [t for t in biz_txns if t.status == TxStatus.COMPLETED]
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

    # Completed deposits routed to each account, summed per merchant business.
    dep: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in txns:
        if t.type.value.startswith("DEPOSIT") and t.status == TxStatus.COMPLETED and t.admin_ref:
            dep[t.admin_ref][t.merchant_name] += t.amount

    out = []
    for a in accounts:
        rows = []
        for name, deposited in dep.get(a.reference_number, {}).items():
            b = bal_by_name.get(name, {})
            rep = rep_by_name.get(name)
            rows.append({
                "merchantName": name,
                "merchantCode": rep.merchant_code if rep else None,
                "deposited": round(deposited, 2),
                "available": round(b.get("available", 0.0), 2),     # AB
                "runningBalance": round(b.get("runningBalance", 0.0), 2),  # RB
                "mab": b.get("mab", 0.0),                           # MAB
            })
        rows.sort(key=lambda r: r["deposited"], reverse=True)
        out.append({
            "referenceNumber": a.reference_number,
            "accountName": a.account_name,
            "accountNumber": a.account_number,
            "bankName": a.bank_name,
            "status": a.status,
            "totalDeposited": round(sum(r["deposited"] for r in rows), 2),
            "merchants": rows,
        })
    return out


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
    accounts = (await db.execute(select(AccountMaster).order_by(AccountMaster.id.desc()))).scalars().all()
    name_map = await _merchant_name_map(db)
    out = [_a(a, name_map.get(a.reference_number)) for a in accounts]
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
    )
    db.add(acc)
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
