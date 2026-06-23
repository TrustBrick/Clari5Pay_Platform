import json
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import Transaction, TxType, TxStatus, User, UserRole, Notification, MerchantBankAccount, AccountTransaction
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import (
    DepositCreate, WithdrawalCreate, SettlementCreate,
    AccountSubmitRequest, SlipRequest, CompleteRequest, RejectRequest, ReasonRequest,
)
from app.api.routes.system_logs import log_event, record_audit


def _require_amount(amount: float) -> None:
    if amount is None or amount < 1:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")


async def _save_bank_account(db: AsyncSession, merchant: User, holder, number, ifsc, branch, bank, member_id=None) -> None:
    """Persist a merchant bank account for future reuse, scoped to a Member ID (deduped per member+account)."""
    if not (holder and number):
        return
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.account_number == number,
        )
    )).scalar_one_or_none()
    if existing:
        return
    db.add(MerchantBankAccount(
        merchant_id=merchant.id, member_id=member_id, account_holder=holder, account_number=number,
        ifsc=ifsc or "", branch=branch or "", bank_name=bank,
    ))


async def _save_member_upi(db: AsyncSession, merchant: User, member_id, upi) -> None:
    """Persist a member's UPI so it auto-fills on their next deposit/withdrawal (deduped).
    The first UPI saved for a member becomes that member's default."""
    if not upi:
        return
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id == upi,
        )
    )).scalar_one_or_none()
    if existing:
        return
    has_upi = (await db.execute(
        select(MerchantBankAccount.id).where(
            MerchantBankAccount.merchant_id == merchant.id,
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id.is_not(None),
        ).limit(1)
    )).scalar_one_or_none()
    db.add(MerchantBankAccount(
        merchant_id=merchant.id, member_id=member_id, upi_id=upi, is_default=(has_upi is None),
    ))


# Shown when a request's member name doesn't match the name already on file for that ID.
MEMBER_NAME_MISMATCH_MSG = "This Membership ID is already associated with another member name."

# Proof/slip upload limits (mirrored on the frontend).
MAX_PROOFS = 3
PROOF_LIMIT_MSG = "You can upload a maximum of 3 proof/slip files per request."
PROOF_TYPE_MSG = "Unsupported file type. Allowed: JPG, JPEG, PNG, PDF."
_ALLOWED_PROOF_PREFIXES = (
    "data:image/jpeg", "data:image/jpg", "data:image/png", "data:application/pdf",
)


def _clean_proofs(proofs: list[str] | None, single: str | None = None) -> list[str]:
    """Validate uploaded proofs: at most 3 files, each a JPG/JPEG/PNG/PDF. Returns the list."""
    items = [p for p in (proofs or []) if p]
    if not items and single:
        items = [single]
    if len(items) > MAX_PROOFS:
        raise HTTPException(status_code=400, detail=PROOF_LIMIT_MSG)
    for p in items:
        head = p[:64].lower()
        if head.startswith("data:") and not head.startswith(_ALLOWED_PROOF_PREFIXES):
            raise HTTPException(status_code=400, detail=PROOF_TYPE_MSG)
    return items


async def _assert_member_name(db: AsyncSession, user: User, member_id, member_name) -> None:
    """If this Membership ID was used before (across the merchant's business), the entered
    Member Name must match the name already on record. Raises 400 on mismatch."""
    if not member_id or not member_name:
        return
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all()
    if not ids:
        return
    existing = (await db.execute(
        select(Transaction.member_name)
        .where(
            Transaction.merchant_id.in_(ids),
            Transaction.member_id == member_id,
            Transaction.member_name.is_not(None),
            Transaction.member_name != "",
        )
        .order_by(Transaction.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if existing and existing.strip().casefold() != member_name.strip().casefold():
        raise HTTPException(status_code=400, detail=MEMBER_NAME_MISMATCH_MSG)

router = APIRouter(prefix="/api/transactions", tags=["transactions"])

# A generated UPI/QR payment code stays valid for this long before it must be regenerated.
QR_VALIDITY_MINUTES = 15
# Deposit types that are paid via UPI/QR (display UPI ID / QR only — never bank details).
UPI_QR_TYPES = {"UPI", "QR"}

# A request in any of these states is finished — it no longer reserves the running balance.
# Everything else (ACCOUNT_REQUESTED / ACCOUNT_SUBMITTED / SLIP_SUBMITTED / PENDING /
# ADMIN_APPROVED) is "in-flight" and counts toward RB.
_TERMINAL_STATUSES = {
    TxStatus.COMPLETED, TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED,
}
# Shown to the merchant when a withdrawal/settlement exceeds their available balance.
INSUFFICIENT_BALANCE_MSG = (
    "We cannot process this request. The requested amount exceeds your available balance."
)


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
    # Net of all settled/completed movements (before reserving in-flight requests).
    gross_available = total_deposit - pay_in_fees - total_settled - total_withdrawn - pay_out_fees

    # Running Balance (RB): in-flight (Pending) withdrawal + settlement amounts that are
    # reserved the moment they're requested, until they complete or are rejected/cancelled.
    # A pending withdrawal reserves the amount PLUS its pay-out fee (so the balance can't go
    # negative once the fee is charged on completion); settlements carry no fee.
    running_balance = 0.0
    for t in txns:
        if t.status in _TERMINAL_STATUSES:
            continue
        if t.type.value.startswith("WITHDRAWAL"):
            running_balance += t.amount * (1 + pay_out_rate)
        elif t.type.value.startswith("SETTLEMENT"):
            running_balance += t.amount

    # Available Balance (AB): reserves the running balance and never goes negative.
    available = max(0.0, gross_available - running_balance)
    # The most a NEW withdrawal can be: its amount + pay-out fee must fit inside AB.
    max_withdrawable = available / (1 + pay_out_rate) if pay_out_rate else available

    deposit_count = sum(1 for t in txns if t.type.value.startswith("DEPOSIT"))
    withdrawal_count = sum(1 for t in txns if t.type.value.startswith("WITHDRAWAL"))

    return {
        "available": available,              # AB — what can still be withdrawn/settled now
        "runningBalance": running_balance,   # RB — reserved by pending requests
        "grossAvailable": gross_available,   # before reserving pending
        "maxSettleable": available,          # settlements carry no extra fee → equals AB
        "maxWithdrawable": max_withdrawable, # AB net of the pay-out fee on a new withdrawal
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


def _t(t: Transaction, full: bool = True) -> dict:
    # In list mode (full=False) the heavy base64 image fields are omitted to keep
    # responses small/fast; they're fetched on demand via GET /transactions/{id}.
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
        "senderUpiId": t.sender_upi_id,
        "bank": t.bank_name,
        "accountHolder": t.account_holder,
        "accountNumber": t.account_number,
        "ifsc": t.ifsc,
        "merchantProof": t.merchant_proof if full else None,
        "merchantProofs": (json.loads(t.merchant_proofs) if t.merchant_proofs else None) if full else None,
        "merchantRef": t.merchant_ref,
        "adminProof": t.admin_proof if full else None,
        "adminRef": t.admin_ref,
        "adminBankDetails": t.admin_bank_details,
        "adminUpiId": t.admin_upi_id,
        "adminUtr": t.admin_utr,
        "payoutMode": t.payout_mode,
        "payoutDetails": json.loads(t.payout_details) if t.payout_details else None,
        "qrExpiresAt": (t.qr_expires_at.isoformat() + "Z") if t.qr_expires_at else None,
        "utr": t.utr,
        "notes": t.notes,
        "riskAnalysis": t.risk_analysis,
        "highRisk": t.high_risk,
        "rejectReason": t.reject_reason,
    }


@router.get("")
async def get_all_transactions(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(Transaction).order_by(Transaction.created_at.desc()))
    return [_t(t, full=False) for t in result.scalars().all()]


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
    return [_t(t, full=False) for t in result.scalars().all()]


@router.get("/{tx_id}/detail")
async def get_transaction_detail(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full transaction incl. heavy image fields — fetched only when a single tx is opened."""
    tx = await _get_tx(tx_id, db)
    if current_user.role == UserRole.MERCHANT and tx.merchant_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your transaction")
    return _t(tx, full=True)


@router.get("/summary")
async def my_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Business-shared available balance + deposit/withdrawal counts for the current merchant."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    return await compute_balance(db, current_user)


@router.get("/merchant-balances")
async def merchant_balances(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Available Balance (AB) + Running Balance (RB) per merchant business — for the admin
    Merchants page. Merchants sharing a business name share one balance pool."""
    merchants = (await db.execute(select(User).where(User.role == UserRole.MERCHANT))).scalars().all()
    rep: dict[str, User] = {}
    for m in merchants:
        rep.setdefault(m.name, m)
    out = []
    for name, user in rep.items():
        s = await compute_balance(db, user)
        out.append({"name": name, "available": round(s["available"], 2), "runningBalance": round(s["runningBalance"], 2)})
    return out


@router.get("/member-profile/{member_id}")
async def member_profile(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Latest known details for a Membership ID (member name + saved UPI + saved bank), scoped to
    the merchant's business — used to auto-fill the deposit form for repeat members."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    if not ids or not member_id:
        return {}
    name = (await db.execute(
        select(Transaction.member_name).where(
            Transaction.merchant_id.in_(ids), Transaction.member_id == member_id,
            Transaction.member_name.is_not(None), Transaction.member_name != "",
        ).order_by(Transaction.id.desc()).limit(1)
    )).scalar_one_or_none()
    upi_row = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id.is_not(None),
        ).order_by(MerchantBankAccount.is_default.desc(), MerchantBankAccount.id.desc()).limit(1)
    )).scalar_one_or_none()
    bank_row = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.account_number.is_not(None),
        ).order_by(MerchantBankAccount.is_default.desc(), MerchantBankAccount.id.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "memberName": name,
        "upiId": upi_row.upi_id if upi_row else None,
        "accountHolder": bank_row.account_holder if bank_row else None,
        "accountNumber": bank_row.account_number if bank_row else None,
        "ifsc": bank_row.ifsc if bank_row else None,
        "branch": bank_row.branch if bank_row else None,
        "bankName": bank_row.bank_name if bank_row else None,
    }


@router.post("/deposit")
async def create_deposit(
    data: DepositCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_amount(data.amount)
    if data.memberId:
        data.memberId = data.memberId.upper()
    await _assert_member_name(db, current_user, data.memberId, data.memberName)
    _proofs = _clean_proofs(data.proofs, data.proof)
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
        sender_upi_id=data.senderUpiId,
        merchant_proof=_proofs[0] if _proofs else None,
        merchant_proofs=json.dumps(_proofs) if _proofs else None,
        account_holder=data.accountHolder,
        account_number=data.accountNumber,
        ifsc=data.ifsc,
        bank_name=data.bankName,
        utr=data.utr,
        notes=data.notes,
        risk_analysis=data.riskAnalysis,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.pay_in or "DEP"
    tx.ref = _make_ref(prefix, tx.id)
    if data.saveBankAccount:
        await _save_bank_account(db, current_user, data.accountHolder, data.accountNumber, data.ifsc, data.branch, data.bankName, member_id=data.memberId)
    # Remember the merchant's sender UPI for this member (first one becomes the default).
    if data.senderUpiId:
        await _save_member_upi(db, current_user, data.memberId, data.senderUpiId.strip())
    await db.flush()
    await notify_tx(db, tx, f"Deposit {tx.ref} requested by {tx.merchant_name}", "↓")
    await log_event(db, "DEPOSIT_REQUESTED", f"{tx.merchant_name} requested deposit {tx.ref} ({tx.amount})", actor=current_user)
    await record_audit(db, "DEPOSIT_REQUESTED", actor=current_user, entity_type="deposit", entity_id=tx.ref, new=str(tx.amount))
    await db.refresh(tx)
    return _t(tx)


@router.post("/withdrawal")
async def create_withdrawal(
    data: WithdrawalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_amount(data.amount)
    # Block withdrawals whose amount + pay-out fee exceeds the Available Balance (which
    # already reserves in-flight requests), so the balance can never go negative.
    if data.memberId:
        data.memberId = data.memberId.upper()
    summary = await compute_balance(db, current_user)
    pay_out_rate = (current_user.pay_out_fee or 0) / 100
    total_required = data.amount * (1 + pay_out_rate)
    if total_required > summary["available"] + 1e-6:
        raise HTTPException(status_code=400, detail=INSUFFICIENT_BALANCE_MSG)
    _proofs = _clean_proofs(data.proofs, data.proof)
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
        merchant_proof=_proofs[0] if _proofs else None,
        merchant_proofs=json.dumps(_proofs) if _proofs else None,
        utr=data.utr,
        notes=data.notes,
        payout_mode=(data.payoutMode or "BANK").upper(),
        payout_details=json.dumps(data.payoutDetails) if data.payoutDetails else None,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.pay_out or "WIT"
    tx.ref = _make_ref(prefix, tx.id)
    # Remember this member's payout details so they auto-fill on the next withdrawal.
    _wd_mode = (data.payoutMode or "BANK").upper()
    if _wd_mode == "BANK" and data.accountNumber:
        await _save_bank_account(db, current_user, data.accountHolder, data.accountNumber, data.ifsc, data.branch, data.bankName, member_id=data.memberId)
    elif _wd_mode == "UPI":
        await _save_member_upi(db, current_user, data.memberId, (data.payoutDetails or {}).get("upiId"))
    await db.flush()
    await notify_tx(db, tx, f"Withdrawal {tx.ref} requested by {tx.merchant_name}", "↑")
    await log_event(db, "WITHDRAWAL_REQUESTED", f"{tx.merchant_name} requested withdrawal {tx.ref} ({tx.amount})", actor=current_user)
    await record_audit(db, "WITHDRAWAL_REQUESTED", actor=current_user, entity_type="withdrawal", entity_id=tx.ref, new=str(tx.amount))
    await db.refresh(tx)
    return _t(tx)


@router.post("/settlement")
async def create_settlement(
    data: SettlementCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_amount(data.amount)
    # A settlement cannot exceed the Available Balance either (same rule as withdrawals).
    if data.memberId:
        data.memberId = data.memberId.upper()
    summary = await compute_balance(db, current_user)
    if data.amount > summary["available"] + 1e-6:
        raise HTTPException(status_code=400, detail=INSUFFICIENT_BALANCE_MSG)
    _proofs = _clean_proofs(data.proofs, data.proof)
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
        merchant_proof=_proofs[0] if _proofs else None,
        merchant_proofs=json.dumps(_proofs) if _proofs else None,
    )
    db.add(tx)
    await db.flush()
    prefix = current_user.settlement or "SET"
    tx.ref = _make_ref(prefix, tx.id)
    await db.flush()
    await notify_tx(db, tx, f"Settlement {tx.ref} requested by {tx.merchant_name}", "⇄")
    await log_event(db, "SETTLEMENT_REQUESTED", f"{tx.merchant_name} requested settlement {tx.ref} ({tx.amount})", actor=current_user)
    await record_audit(db, "SETTLEMENT_REQUESTED", actor=current_user, entity_type="settlement", entity_id=tx.ref, new=str(tx.amount))
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
    # UPI/QR deposits: the QR (rendered from the UPI ID + amount) is valid for 15 minutes.
    is_upi_qr = tx.type.value.startswith("DEPOSIT") and (tx.deposit_type or "").upper() in UPI_QR_TYPES
    if is_upi_qr and data.adminUpiId:
        tx.qr_expires_at = datetime.utcnow() + timedelta(minutes=QR_VALIDITY_MINUTES)
        tx.admin_bank_details = None  # never expose bank details for UPI/QR payments
    # Remember which managed account was assigned to this Member ID so repeat deposits reuse it.
    elif data.adminRef and data.adminBankDetails and tx.member_id and data.adminRef.startswith("ACC"):
        db.add(AccountTransaction(
            reference_number=data.adminRef, member_id=tx.member_id,
            transaction_reference_number=tx.ref, transaction_date=date.today(),
            transaction_time=datetime.now().strftime("%H:%M:%S"),
        ))
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: account details sent to {tx.merchant_name}", "🏦")
    await log_event(db, "ACCOUNT_SUBMITTED", f"{tx.ref}: account details sent to {tx.merchant_name}", actor=actor)
    await record_audit(db, "ACCOUNT_SUBMITTED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, new="ACCOUNT_SUBMITTED")
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/slip")
async def submit_slip(
    tx_id: str,
    data: SlipRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant pays using the admin's details and submits proof (image(s) and/or reference)."""
    _proofs = _clean_proofs(data.merchantProofs, data.merchantProof)
    if not (_proofs or data.merchantRef):
        raise HTTPException(
            status_code=400,
            detail="Upload an image or enter a reference number",
        )
    tx = await _get_own_tx(tx_id, db, current_user)
    if _proofs:
        tx.merchant_proof = _proofs[0]
        tx.merchant_proofs = json.dumps(_proofs)
    tx.merchant_ref = data.merchantRef
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await notify_tx(db, tx, f"{tx.ref}: payment slip submitted by {tx.merchant_name}", "🧾")
    await log_event(db, "SLIP_SUBMITTED", f"{tx.ref}: slip submitted by {tx.merchant_name}", actor=current_user)
    await record_audit(db, "SLIP_SUBMITTED", actor=current_user, entity_type=tx.type.value, entity_id=tx.ref, new="SLIP_SUBMITTED")
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
    if data and data.adminUtr:
        tx.admin_utr = data.adminUtr
    tx.status = TxStatus.COMPLETED
    await db.flush()
    label = "deposited" if tx.type.value.startswith("DEPOSIT") else "completed"
    await notify_tx(db, tx, f"{tx.ref}: {label}", "✓")
    await log_event(db, "COMPLETED", f"{tx.ref} marked {label} by {actor.name}", actor=actor)
    await record_audit(db, "COMPLETED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, new="COMPLETED")
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/recheck")
async def recheck_payment(
    tx_id: str,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Agent rechecks a deposit slip: if the payment can't be confirmed, send it back to the
    merchant to re-upload the correct proof (status -> Account Submitted, old proof cleared)."""
    tx = await _get_tx(tx_id, db)
    if not tx.type.value.startswith("DEPOSIT"):
        raise HTTPException(status_code=400, detail="Recheck applies to deposits only.")
    reason = (data.reason if data else None) or "Payment could not be verified — please re-upload the correct proof."
    tx.merchant_proof = None
    tx.merchant_proofs = None
    tx.merchant_ref = None
    tx.status = TxStatus.ACCOUNT_SUBMITTED
    await db.flush()
    db.add(Notification(user_id=tx.merchant_id, message=f"{tx.ref}: re-upload payment proof — {reason}", icon="↻"))
    await log_event(db, "RECHECK", f"{tx.ref}: re-upload requested by {actor.name} — {reason}", actor=actor)
    await record_audit(db, "RECHECK", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, reason=reason)
    await db.refresh(tx)
    return _t(tx)


@router.post("/{tx_id}/flag-risk")
async def flag_high_risk(
    tx_id: str,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Payment still not received after re-upload: flag the member HIGH RISK and reject the request.
    The high-risk flag is shown to the merchant for that Member ID."""
    tx = await _get_tx(tx_id, db)
    reason = (data.reason if data else None) or "Payment not received in our bank."
    tx.high_risk = True
    tx.status = TxStatus.REJECTED
    tx.reject_reason = reason
    await db.flush()
    db.add(Notification(
        user_id=tx.merchant_id,
        message=f"⚠ Member {tx.member_id or tx.ref} flagged HIGH RISK — {reason}", icon="⚠",
    ))
    await log_event(db, "HIGH_RISK", f"{tx.ref} (member {tx.member_id}) flagged high risk by {actor.name} — {reason}", actor=actor)
    await record_audit(db, "HIGH_RISK", actor=actor, entity_type=tx.type.value, entity_id=tx.ref, new="HIGH_RISK", reason=reason)
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


@router.post("/{tx_id}/regenerate-qr")
async def regenerate_qr(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merchant re-arms an expired UPI/QR deposit code with a fresh 15-minute validity window."""
    tx = await _get_own_tx(tx_id, db, current_user)
    is_upi_qr = tx.type.value.startswith("DEPOSIT") and (tx.deposit_type or "").upper() in UPI_QR_TYPES
    if not (is_upi_qr and tx.admin_upi_id):
        raise HTTPException(status_code=400, detail="No UPI/QR code to regenerate for this request.")
    if tx.status != TxStatus.ACCOUNT_SUBMITTED:
        raise HTTPException(status_code=400, detail="This request is no longer awaiting payment.")
    tx.qr_expires_at = datetime.utcnow() + timedelta(minutes=QR_VALIDITY_MINUTES)
    await db.flush()
    await log_event(db, "QR_REGENERATED", f"{tx.ref}: QR code regenerated by {tx.merchant_name}", actor=current_user)
    await record_audit(db, "QR_REGENERATED", actor=current_user, entity_type=tx.type.value, entity_id=tx.ref)
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
    data: RejectRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Admin rejects a request with a required reason; merchant is notified."""
    if not data.reason or not data.reason.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required")
    tx = await _get_tx(tx_id, db)
    tx.status = TxStatus.REJECTED
    tx.reject_reason = data.reason.strip()
    await db.flush()
    db.add(Notification(user_id=tx.merchant_id, message=f"{tx.ref} rejected — {tx.reject_reason}", icon="✕"))
    await log_event(db, "REJECTED", f"{tx.ref} rejected by {actor.name} — reason: {tx.reject_reason}", actor=actor)
    await record_audit(db, "REJECTED", actor=actor, entity_type=tx.type.value, entity_id=tx.ref,
                       new="REJECTED", reason=tx.reject_reason)
    await db.refresh(tx)
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
