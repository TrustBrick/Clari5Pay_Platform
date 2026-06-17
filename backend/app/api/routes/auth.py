from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.models.models import User
from app.core.security import verify_password, create_access_token
from app.core.deps import get_current_user
from app.schemas.schemas import Token, UserOut
from app.api.routes.system_logs import log_event

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_to_out(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "name": u.name,
        "phone": u.phone,
        "role": u.role,
        "active": u.active,
        "created": str(u.created),
        "createdAt": (u.created_at.isoformat() + "Z") if u.created_at else None,
        "createdBy": u.created_by,
        "payIn": u.pay_in,
        "payOut": u.pay_out,
        "settlement": u.settlement,
        "payInFee": u.pay_in_fee,
        "payOutFee": u.pay_out_fee,
        "balance": u.balance,
        "risk": u.risk,
        "profile": u.profile,
        "merchantRole": u.merchant_role,
    }


@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    if not user.active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token = create_access_token({"sub": str(user.id)})
    await log_event(db, "LOGIN", f"{user.name} ({user.role.value}) signed in", actor=user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_to_out(user),
    }


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return _user_to_out(current_user)
