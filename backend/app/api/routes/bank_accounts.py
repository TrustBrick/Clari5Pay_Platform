from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.db.session import get_db
from app.models.models import MerchantBankAccount, User, UserRole
from app.core.deps import get_current_user
from app.schemas.schemas import BankAccountCreate

router = APIRouter(prefix="/api/merchant-bank-accounts", tags=["merchant-bank-accounts"])


def _b(a: MerchantBankAccount) -> dict:
    return {
        "id": a.id,
        "memberId": a.member_id,
        "accountHolder": a.account_holder,
        "accountNumber": a.account_number,
        "ifsc": a.ifsc,
        "branch": a.branch,
        "bankName": a.bank_name,
        "upiId": a.upi_id,
        "isDefault": a.is_default,
    }


@router.get("")
async def list_my_bank_accounts(
    memberId: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """A merchant's saved bank accounts, scoped to a Member ID.

    Accounts are shared across the same business name, but each Member ID sees only
    its own saved accounts. Without ``memberId`` nothing is returned (a member must
    be chosen first), so one member never sees another member's bank details.
    """
    if current_user.role != UserRole.MERCHANT or not memberId:
        return []
    ids = (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == current_user.name)
    )).scalars().all()
    rows = (await db.execute(
        select(MerchantBankAccount)
        .where(MerchantBankAccount.merchant_id.in_(ids), MerchantBankAccount.member_id == memberId)
        .order_by(MerchantBankAccount.id.desc())
    )).scalars().all() if ids else []
    return [_b(a) for a in rows]


@router.post("")
async def add_bank_account(
    data: BankAccountCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id == current_user.id,
            MerchantBankAccount.member_id == data.memberId,
            MerchantBankAccount.account_number == data.accountNumber,
        )
    )).scalar_one_or_none()
    if existing:
        return _b(existing)
    acc = MerchantBankAccount(
        merchant_id=current_user.id,
        member_id=data.memberId,
        account_holder=data.accountHolder,
        account_number=data.accountNumber,
        ifsc=data.ifsc,
        branch=data.branch,
        bank_name=data.bankName,
    )
    db.add(acc)
    await db.flush()
    await db.refresh(acc)
    return _b(acc)


async def _business_ids(db: AsyncSession, user: User) -> list[int]:
    return (await db.execute(
        select(User.id).where(User.role == UserRole.MERCHANT, User.name == user.name)
    )).scalars().all()


@router.post("/upi")
async def add_member_upi(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save a UPI ID for a Member ID (first one for that member becomes the default)."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    upi = (data.get("upiId") or "").strip()
    member_id = data.get("memberId")
    if "@" not in upi:
        raise HTTPException(status_code=400, detail="Enter a valid UPI ID (name@bank).")
    ids = await _business_ids(db, current_user)
    existing = (await db.execute(
        select(MerchantBankAccount).where(
            MerchantBankAccount.merchant_id.in_(ids),
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id == upi,
        )
    )).scalar_one_or_none() if ids else None
    if existing:
        return _b(existing)
    has_upi = (await db.execute(
        select(MerchantBankAccount.id).where(
            MerchantBankAccount.merchant_id.in_(ids),
            MerchantBankAccount.member_id == member_id,
            MerchantBankAccount.upi_id.is_not(None),
        ).limit(1)
    )).scalar_one_or_none() if ids else None
    row = MerchantBankAccount(merchant_id=current_user.id, member_id=member_id, upi_id=upi, is_default=(has_upi is None))
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _b(row)


@router.patch("/{acc_id}/default")
async def set_default_upi(
    acc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a saved UPI as the default for its Member ID (clears the flag on the others)."""
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant only")
    row = (await db.execute(select(MerchantBankAccount).where(MerchantBankAccount.id == acc_id))).scalar_one_or_none()
    if not row or not row.upi_id:
        raise HTTPException(status_code=404, detail="Saved UPI not found")
    ids = await _business_ids(db, current_user)
    if row.merchant_id not in ids:
        raise HTTPException(status_code=403, detail="Not your UPI")
    await db.execute(
        update(MerchantBankAccount)
        .where(
            MerchantBankAccount.merchant_id.in_(ids),
            MerchantBankAccount.member_id == row.member_id,
            MerchantBankAccount.upi_id.is_not(None),
        )
        .values(is_default=False)
    )
    row.is_default = True
    await db.flush()
    await db.refresh(row)
    return _b(row)
