from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import User, UserRole
from app.core.security import get_password_hash, verify_password
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import UserCreate, ChangePasswordRequest

router = APIRouter(prefix="/api/users", tags=["users"])


def _u(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "email": u.email, "name": u.name,
        "role": u.role, "active": u.active, "created": str(u.created),
        "payIn": u.pay_in, "payOut": u.pay_out, "settlement": u.settlement,
        "payInFee": u.pay_in_fee, "payOutFee": u.pay_out_fee,
        "balance": u.balance, "risk": u.risk, "profile": u.profile,
    }


@router.get("/merchants")
async def get_merchants(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.role == UserRole.MERCHANT))
    return [_u(u) for u in result.scalars().all()]


@router.get("/admins")
async def get_admins(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
    return [_u(u) for u in result.scalars().all()]


@router.post("/merchants")
async def create_merchant(
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    existing = await db.execute(select(User).where(User.username == data["username"]))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=data["username"],
        hashed_password=get_password_hash(data["password"]),
        email=data["email"],
        name=data["name"],
        role=UserRole.MERCHANT,
        active=True,
        pay_in=data.get("payIn"),
        pay_out=data.get("payOut"),
        settlement=data.get("settlement"),
        pay_in_fee=float(data.get("payInFee", 1.5)),
        pay_out_fee=float(data.get("payOutFee", 1.2)),
        balance=0.0,
        risk=data.get("risk", "LOW"),
        profile=data.get("profile", "Maker"),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return _u(user)


@router.post("/admins")
async def create_admin(
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    existing = await db.execute(select(User).where(User.username == data["username"]))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=data["username"],
        hashed_password=get_password_hash(data["password"]),
        email=data["email"],
        name=data["name"],
        role=UserRole.ADMIN,
        active=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return _u(user)


@router.patch("/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.active = not user.active
    await db.flush()
    await db.refresh(user)
    return _u(user)


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.hashed_password = get_password_hash(data.new_password)
    await db.flush()
    return {"message": "Password updated successfully"}
