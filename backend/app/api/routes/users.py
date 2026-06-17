from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.models import User, UserRole, Notification
from app.core.security import get_password_hash, verify_password
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.schemas.schemas import ChangePasswordRequest, ProfileUpdateRequest
from app.api.routes.system_logs import log_event

router = APIRouter(prefix="/api/users", tags=["users"])


def _u(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "email": u.email, "name": u.name,
        "phone": u.phone, "role": u.role, "active": u.active, "created": str(u.created),
        "createdAt": (u.created_at.isoformat() + "Z") if u.created_at else None,
        "createdBy": u.created_by,
        "payIn": u.pay_in, "payOut": u.pay_out, "settlement": u.settlement,
        "payInFee": u.pay_in_fee, "payOutFee": u.pay_out_fee,
        "balance": u.balance, "risk": u.risk, "profile": u.profile,
        "merchantRole": u.merchant_role,
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
    admins = result.scalars().all()

    # Count merchants created by each admin
    counts = await db.execute(
        select(User.created_by, func.count(User.id))
        .where(User.role == UserRole.MERCHANT)
        .group_by(User.created_by)
    )
    count_map = {cb: c for cb, c in counts.all()}

    out = []
    for a in admins:
        d = _u(a)
        d["merchantCount"] = count_map.get(a.id, 0)
        out.append(d)
    return out


@router.get("/admins/{admin_id}/merchants")
async def get_admin_merchants(
    admin_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),
):
    result = await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.created_by == admin_id)
    )
    return [_u(u) for u in result.scalars().all()]


@router.post("/merchants")
async def create_merchant(
    data: dict,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    # Super Admin cannot create merchants — only Admins.
    if admin.role == UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super Admin cannot create merchants")

    existing = await db.execute(select(User).where(User.username == data["username"]))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=data["username"],
        hashed_password=get_password_hash(data["password"]),
        email=data["email"],
        name=data["name"],
        phone=data.get("phone"),
        role=UserRole.MERCHANT,
        active=True,
        created_by=admin.id,
        pay_in=data.get("payIn"),
        pay_out=data.get("payOut"),
        settlement=data.get("settlement"),
        pay_in_fee=float(data.get("payInFee", 1.5)),
        pay_out_fee=float(data.get("payOutFee", 1.2)),
        balance=0.0,
        risk=data.get("risk", "LOW"),
        profile=data.get("profile", "Maker"),
        merchant_role=data.get("merchantRole"),
    )
    db.add(user)
    await db.flush()
    db.add(Notification(user_id=user.id, message="Your merchant account was created", icon="🏪"))
    db.add(Notification(user_id=admin.id, message=f"Merchant \"{user.name}\" created", icon="🏪"))
    await log_event(db, "MERCHANT_CREATED", f"Merchant \"{user.name}\" (role {user.merchant_role or '—'}) created", actor=admin)
    await db.refresh(user)
    return _u(user)


@router.post("/admins")
async def create_admin(
    data: dict,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    existing = await db.execute(select(User).where(User.username == data["username"]))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=data["username"],
        hashed_password=get_password_hash(data["password"]),
        email=data["email"],
        name=data["name"],
        phone=data.get("phone"),
        role=UserRole.ADMIN,
        active=True,
    )
    db.add(user)
    await db.flush()
    db.add(Notification(user_id=user.id, message="Your admin account was created", icon="🛡"))
    db.add(Notification(user_id=sa.id, message=f"Admin \"{user.name}\" created", icon="🛡"))
    await log_event(db, "ADMIN_CREATED", f"Admin \"{user.name}\" created", actor=sa)
    await db.refresh(user)
    return _u(user)


@router.patch("/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.active = not user.active
    await db.flush()
    state = "activated" if user.active else "deactivated"
    db.add(Notification(user_id=user.id, message=f"Your account was {state}", icon="🔔"))
    if actor.id != user.id:
        db.add(Notification(user_id=actor.id, message=f"{user.name} {state}", icon="🔔"))
    await log_event(db, "USER_TOGGLED", f"{user.role.value} \"{user.name}\" {state}", actor=actor)
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


@router.patch("/me")
async def update_profile(
    data: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update own email and/or password — persisted immediately and used for future logins."""
    if data.new_password:
        # Verify current password when provided (required to change password).
        if data.current_password is not None and not verify_password(
            data.current_password, current_user.hashed_password
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        current_user.hashed_password = get_password_hash(data.new_password)

    if data.email and data.email != current_user.email:
        existing = await db.execute(
            select(User).where(User.email == data.email, User.id != current_user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use")
        current_user.email = data.email

    await db.flush()
    await db.refresh(current_user)
    return _u(current_user)
