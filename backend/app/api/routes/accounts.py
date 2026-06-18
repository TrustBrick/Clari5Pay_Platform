from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import AccountMaster, AccountTransaction, Transaction, User
from app.core.deps import get_current_admin
from app.schemas.schemas import AccountCreate, ReasonRequest
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


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
