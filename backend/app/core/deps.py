from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.security import decode_token, token_version_matches
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
    # Revocation check (SEC-002). The token asserts the token_version it was minted with;
    # incrementing that column on the user invalidates every token issued before. A token with
    # no `ver` claim reads as 0 and still matches a default row, so sessions that predate this
    # feature keep working. NOTE: the support WebSocket (support.py) authenticates on its own
    # code path and performs the same check — both must stay in step.
    if not token_version_matches(payload, user):
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


# Merchant roles permitted to OPEN the KYC Update module. Data Operator (DEO) performs the
# verifications; Supervisor and Manager have read-only access (history + details).
KYC_MERCHANT_ROLES = ("SUPERVISOR", "MANAGER", "DEO")
# Merchant roles permitted to PERFORM a verification (Aadhaar / PAN / Passport / OCR).
KYC_VERIFIER_ROLES = ("DEO",)


async def get_current_kyc_user(current_user: User = Depends(get_current_user)) -> User:
    """MERCHANT users whose merchant_role is Data Operator, Supervisor or Manager — the only
    roles allowed to access the KYC Update module. Every other role is rejected with 403.

    This is the READ gate: it guards the history, record detail and membership lookup. Running a
    verification additionally requires ``get_current_kyc_verifier`` below.

    Admins are also allowed through — read-only, system-wide oversight of every merchant's KYC
    history (the query scope widens for them in ``kyc.py`` via ``_kyc_scope``). They never pass
    ``get_current_kyc_verifier``, so they can view but never run a verification.
    """
    if current_user.role == UserRole.ADMIN:
        return current_user
    if (
        current_user.role == UserRole.MERCHANT
        and str(current_user.merchant_role or "").upper() in KYC_MERCHANT_ROLES
    ):
        return current_user
    raise HTTPException(status_code=403, detail="KYC access requires a Data Operator, Supervisor or Manager role")


async def get_current_kyc_admin(current_user: User = Depends(get_current_user)) -> User:
    """ADMIN users only — the gate on the uploaded OCR document (view / download).

    Strictly narrower than ``get_current_kyc_user``: the identity document a member handed over is
    exposed to the Admin Portal alone. Every Merchant Portal role — Data Operator, Supervisor,
    Manager, DEO and any other — is rejected here even though they may read the same record's
    history and details.
    """
    if current_user.role == UserRole.ADMIN:
        return current_user
    raise HTTPException(status_code=403, detail="Viewing the uploaded KYC document requires Admin access")


async def get_current_kyc_verifier(current_user: User = Depends(get_current_user)) -> User:
    """MERCHANT users whose merchant_role is Data Operator — the only role that may RUN a KYC
    verification. Supervisor and Manager keep read-only access via ``get_current_kyc_user``."""
    if (
        current_user.role == UserRole.MERCHANT
        and str(current_user.merchant_role or "").upper() in KYC_VERIFIER_ROLES
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Performing a KYC verification requires a Data Operator role")


# Merchant roles permitted to use the Agent Management module (Non-EPS agents).
AGENT_MERCHANT_ROLES = ("SUPERVISOR", "MANAGER")


async def get_current_agent_manager(current_user: User = Depends(get_current_user)) -> User:
    """MERCHANT users whose merchant_role is Supervisor or Manager — the only roles allowed to
    manage Non-EPS Agents (Agent Management module). Every other role is rejected with 403."""
    if (
        current_user.role == UserRole.MERCHANT
        and str(current_user.merchant_role or "").upper() in AGENT_MERCHANT_ROLES
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Agent Management requires a Supervisor or Manager role")
