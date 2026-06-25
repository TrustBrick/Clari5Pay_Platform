from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.security import decode_token
from app.db.session import get_db
from app.models.models import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_token(token)
    if payload is None:
        raise credentials_exception
    user_id: int = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.active:
        raise credentials_exception
    return user


async def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


async def get_current_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super Admin access required")
    return current_user


async def get_current_merchant(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.MERCHANT:
        raise HTTPException(status_code=403, detail="Merchant access required")
    return current_user


async def get_current_support(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.SUPPORT_AGENT:
        raise HTTPException(status_code=403, detail="Support agent access required")
    return current_user


# Merchant access roles allowed read-only oversight of the whole transaction feed.
OVERSIGHT_MERCHANT_ROLES = ("SUPERVISOR", "MANAGER")


async def get_transactions_overseer(current_user: User = Depends(get_current_user)) -> User:
    """Read-only, system-wide transaction visibility.

    Granted to Admins/Super Admins, and to MERCHANT users whose merchant_role is an
    oversight role (Supervisor / Manager). Used only for *viewing* — it never grants
    the ability to complete (mark deposited / complete) a transaction.
    """
    if current_user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        return current_user
    if (
        current_user.role == UserRole.MERCHANT
        and str(current_user.merchant_role or "").upper() in OVERSIGHT_MERCHANT_ROLES
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Oversight access required")


def _is_merchant_role(user: User, role: str) -> bool:
    return user.role == UserRole.MERCHANT and str(user.merchant_role or "").upper() == role


async def get_current_supervisor(current_user: User = Depends(get_current_user)) -> User:
    """A MERCHANT user whose merchant_role is SUPERVISOR — the deposit review gate.
    Supervisors review (approve/reject/resubmit) but never complete a transaction."""
    if not _is_merchant_role(current_user, "SUPERVISOR"):
        raise HTTPException(status_code=403, detail="Supervisor access required")
    return current_user


async def get_current_manager(current_user: User = Depends(get_current_user)) -> User:
    """A MERCHANT user whose merchant_role is MANAGER — the withdrawal review gate.
    Managers review (approve/reject/resubmit) but never complete a transaction."""
    if not _is_merchant_role(current_user, "MANAGER"):
        raise HTTPException(status_code=403, detail="Manager access required")
    return current_user
