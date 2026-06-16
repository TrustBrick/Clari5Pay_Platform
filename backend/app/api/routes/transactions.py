from datetime import date
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import Transaction, TxType, TxStatus, User, UserRole
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import DepositCreate, WithdrawalCreate, SettlementCreate, CheckRequest

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _make_ref(prefix: str, tx_id: int) -> str:
    return f"{prefix}{str(tx_id).zfill(7)}"


def _t(t: Transaction) -> dict:
    return {
        "id": f"TXN{str(t.id).zfill(3)}",
        "ref": t.ref,
        "type": t.type,
        "amount": t.amount,
        "status": t.status,
        "merchantId": t.merchant_id,
        "merchant": t.merchant_name,
        "date": str(t.tx_date),
        "time": t.tx_time,
        "depositType": t.deposit_type,
        "member": t.member_name,
        "memberId": t.member_id,
        "bank": t.bank_name,
        "merchantProof": t.merchant_proof,
        "adminProof": t.admin_proof,
        "adminRef": t.admin_ref,
    }


@router.get("")
async def get_all_transactions(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(Transaction).order_by(Transaction.created_at.desc()))
    return [_t(t) for t in result.scalars().all()]


@router.get("/mine")
async def get_my_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    result = await db.execute(
        select(Transaction)
        .where(Transaction.merchant_id == current_user.id)
        .order_by(Transaction.created_at.desc())
    )
    return [_t(t) for t in result.scalars().all()]


@router.post("/deposit")
async def create_deposit(
    data: DepositCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = Transaction(
        ref="TEMP",
        type=TxType.DEPOSIT_REQUEST,
        amount=data.amount,
        status=TxStatus.ACCOUNT_REQUESTED,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=date.today(),
        tx_time=datetime.now().strftime("%H:%M:%S"),
        deposit_type=data.depositType,
        member_name=data.memberName,
        member_id=data.memberId,
        segment=data.segment,
        merchant_proof=data.proof,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.pay_in or "DEP"
    tx.ref = _make_ref(prefix, tx.id)
    await db.flush()
    await db.refresh(tx)
    return _t(tx)


@router.post("/withdrawal")
async def create_withdrawal(
    data: WithdrawalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = Transaction(
        ref="TEMP",
        type=TxType.WITHDRAWAL_REQUEST,
        amount=data.amount,
        status=TxStatus.ACCOUNT_REQUESTED,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=date.today(),
        tx_time=datetime.now().strftime("%H:%M:%S"),
        member_id=data.memberId,
        bank_name=data.bankName,
        account_holder=data.accountHolder,
        account_number=data.accountNumber,
        ifsc=data.ifsc,
        merchant_proof=data.proof,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.pay_out or "WIT"
    tx.ref = _make_ref(prefix, tx.id)
    await db.flush()
    await db.refresh(tx)
    return _t(tx)


@router.post("/settlement")
async def create_settlement(
    data: SettlementCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = Transaction(
        ref="TEMP",
        type=TxType.SETTLEMENT_REQUEST,
        amount=data.amount,
        status=TxStatus.ACCOUNT_REQUESTED,
        merchant_id=current_user.id,
        merchant_name=current_user.name,
        tx_date=date.today(),
        tx_time=datetime.now().strftime("%H:%M:%S"),
        member_id=data.memberId,
        merchant_proof=data.proof,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.settlement or "SET"
    tx.ref = _make_ref(prefix, tx.id)
    await db.flush()
    await db.refresh(tx)
    return _t(tx)


async def _get_tx(tx_id: str, db: AsyncSession) -> Transaction:
    numeric_id = int(tx_id.replace("TXN", "").lstrip("0") or "0")
    result = await db.execute(select(Transaction).where(Transaction.id == numeric_id))
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


@router.post("/{tx_id}/check")
async def check_transaction(
    tx_id: str,
    data: CheckRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Admin reviews merchant proof, uploads its own document + reference number, marks submitted."""
    tx = await _get_tx(tx_id, db)
    tx.admin_ref = data.adminRef
    if data.adminProof:
        tx.admin_proof = data.adminProof
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    await db.flush()
    await db.refresh(tx)
    return _t(tx)


# ─── Legacy approval workflow (kept for backward compatibility) ────────────────
@router.post("/{tx_id}/approve")
async def approve_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.ADMIN_APPROVED
    await db.flush()
    return _t(tx)


@router.post("/{tx_id}/reject")
async def reject_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.REJECTED
    await db.flush()
    return _t(tx)


@router.post("/{tx_id}/complete")
async def complete_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.COMPLETED
    await db.flush()
    return _t(tx)


@router.post("/{tx_id}/sa-reject")
async def sa_reject_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.SA_REJECTED
    await db.flush()
    return _t(tx)
