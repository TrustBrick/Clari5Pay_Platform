from datetime import date
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import Transaction, TxType, TxStatus, User, UserRole, Notification
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import (
    DepositCreate, WithdrawalCreate, SettlementCreate,
    AccountSubmitRequest, SlipRequest, CompleteRequest,
)
from app.api.routes.system_logs import log_event

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _make_ref(prefix: str, tx_id: int) -> str:
    return f"{prefix}{str(tx_id).zfill(7)}"


async def compute_balance(db: AsyncSession, user: User) -> dict:
    """Available balance + counts, aggregated across all merchant users sharing a business name."""
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all()
    txns = (await db.execute(
        select(Transaction).where(Transaction.merchant_id.in_(ids))
    )).scalars().all() if ids else []

    def total(prefix: str, status: TxStatus | None = None) -> float:
        return sum(t.amount for t in txns
                   if t.type.value.startswith(prefix) and (status is None or t.status == status))

    pay_in_rate = (user.pay_in_fee or 0) / 100
    pay_out_rate = (user.pay_out_fee or 0) / 100
    total_deposit = total("DEPOSIT", TxStatus.COMPLETED)
    pay_in_fees = total_deposit * pay_in_rate
    total_settled = total("SETTLEMENT", TxStatus.COMPLETED)
    total_withdrawn = total("WITHDRAWAL", TxStatus.COMPLETED)
    pay_out_fees = total_withdrawn * pay_out_rate
    available = total_deposit - pay_in_fees - total_settled - total_withdrawn - pay_out_fees

    deposit_count = sum(1 for t in txns if t.type.value.startswith("DEPOSIT"))
    withdrawal_count = sum(1 for t in txns if t.type.value.startswith("WITHDRAWAL"))

    return {
        "available": available,
        "totalDeposit": total_deposit,
        "payInFees": pay_in_fees,
        "totalSettled": total_settled,
        "totalWithdrawn": total_withdrawn,
        "payOutFees": pay_out_fees,
        "depositCount": deposit_count,
        "withdrawalCount": withdrawal_count,
    }


async def notify_tx(db: AsyncSession, tx: Transaction, message: str, icon: str = "🔔") -> None:
    """Notify both the merchant and (if any) the admin who created them about a tx event."""
    recipients = {tx.merchant_id}
    merch = (await db.execute(select(User).where(User.id == tx.merchant_id))).scalar_one_or_none()
    if merch and merch.created_by:
        recipients.add(merch.created_by)
    for uid in recipients:
        db.add(Notification(user_id=uid, message=message, icon=icon))


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
        "accountHolder": t.account_holder,
        "accountNumber": t.account_number,
        "ifsc": t.ifsc,
        "merchantProof": t.merchant_proof,
        "merchantRef": t.merchant_ref,
        "adminProof": t.admin_proof,
        "adminRef": t.admin_ref,
        "adminBankDetails": t.admin_bank_details,
        "adminUpiId": t.admin_upi_id,
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


@router.get("/summary")
async def my_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Business-shared available balance + deposit/withdrawal counts for the current merchant."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    return await compute_balance(db, current_user)


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
    await notify_tx(db, tx, f"Deposit {tx.ref} requested by {tx.merchant_name}", "↓")
    await log_event(db, "DEPOSIT_REQUESTED", f"{tx.merchant_name} requested deposit {tx.ref} ({tx.amount})", actor=current_user)
    await db.refresh(tx)
    return _t(tx)


@router.post("/withdrawal")
async def create_withdrawal(
    data: WithdrawalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Block withdrawals that exceed the (business-shared) available balance.
    summary = await compute_balance(db, current_user)
    if data.amount > summary["available"] + 1e-6:
        raise HTTPException(
            status_code=400,
            detail="Total available balance insufficient — try again",
        )
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
    await notify_tx(db, tx, f"Withdrawal {tx.ref} requested by {tx.merchant_name}", "↑")
    await log_event(db, "WITHDRAWAL_REQUESTED", f"{tx.merchant_name} requested withdrawal {tx.ref} ({tx.amount})", actor=current_user)
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
    await notify_tx(db, tx, f"Settlement {tx.ref} requested by {tx.merchant_name}", "⇄")
    await log_event(db, "SETTLEMENT_REQUESTED", f"{tx.merchant_name} requested settlement {tx.ref} ({tx.amount})", actor=current_user)
    await db.refresh(tx)
    return _t(tx)


async def _get_tx(tx_id: str, db: AsyncSession) -> Transaction:
    numeric_id = int(tx_id.replace("TXN", "").lstrip("0") or "0")
    result = await db.execute(select(Transaction).where(Transaction.id == numeric_id))
    tx = result.scalar_one_or_none()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


async def _get_own_tx(tx_id: str, db: AsyncSession, user: User) -> Transaction:
    tx = await _get_tx(tx_id, db)
    if tx.merchant_id != user.id:
        raise HTTPException(status_code=403, detail="Not your transaction")
    return tx


@router.post("/{tx_id}/account-submit")
async def account_submit(
    tx_id: str,
    data: AccountSubmitRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin selects a managed account, the app sends its details/image, status → Account Submitted."""
    if not (data.adminBankDetails or data.adminUpiId or data.adminProof):
        raise HTTPException(
            status_code=400,
            detail="Select an account to send",
        )
    tx = await _get_tx(tx_id, db)
    tx.admin_ref = data.adminRef
    tx.admin_bank_details = data.adminBankDetails
    tx.admin_upi_id = data.adminUpiId
    if data.adminProof:
        tx.admin_proof = data.adminProof
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: account details sent to {tx.merchant_name}", "🏦")
    await log_event(db, "ACCOUNT_SUBMITTED", f"{tx.ref}: account details sent to {tx.merchant_name}", actor=actor)
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/slip")
async def submit_slip(
    tx_id: str,
    data: SlipRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant pays using the admin's details and submits proof (image and/or reference)."""
    if not (data.merchantProof or data.merchantRef):
        raise HTTPException(
            status_code=400,
            detail="Upload an image or enter a reference number",
        )
    tx = await _get_own_tx(tx_id, db, current_user)
    if data.merchantProof:
        tx.merchant_proof = data.merchantProof
    tx.merchant_ref = data.merchantRef
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: payment slip submitted by {tx.merchant_name}", "🧾")
    await log_event(db, "SLIP_SUBMITTED", f"{tx.ref}: slip submitted by {tx.merchant_name}", actor=current_user)
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/done")
async def mark_done(
    tx_id: str,
    data: CompleteRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Complete a request. For deposits this is 'Mark Deposited'; for withdrawals/settlements the
    admin attaches a payment receipt image (adminProof)."""
    tx = await _get_tx(tx_id, db)
    if data and data.adminProof:
        tx.admin_proof = data.adminProof
    tx.status = TxStatus.COMPLETED
    await db.flush()
    label = "deposited" if tx.type.value.startswith("DEPOSIT") else "completed"
    await notify_tx(db, tx, f"{tx.ref}: {label}", "✓")
    await log_event(db, "COMPLETED", f"{tx.ref} marked {label} by {actor.name}", actor=actor)
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/cancel")
async def cancel_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant cancels one of their own pending requests."""
    tx = await _get_own_tx(tx_id, db, current_user)
    tx.status = TxStatus.CANCELLED
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: cancelled by {tx.merchant_name}", "⊘")
    await log_event(db, "CANCELLED", f"{tx.ref} cancelled by {tx.merchant_name}", actor=current_user)
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
