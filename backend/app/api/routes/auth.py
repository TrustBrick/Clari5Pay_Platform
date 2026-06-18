import random
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.db.session import get_db
from app.core.config import settings
from app.models.models import User, LoginOtp, UserRole, AppSetting, Notification
from app.core.security import verify_password, create_access_token, decode_token
from app.core.passwords import assert_password_allowed, set_password
from app.core.deps import get_current_user
from app.core.email import send_otp_email, mask_email
from app.schemas.schemas import (
    OtpVerifyRequest, OtpResendRequest, OtpConfigRequest,
    ForgotPasswordRequest, VerifyResetOtpRequest, ResetPasswordRequest,
)
from app.api.routes.system_logs import log_event, record_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])

OTP_SETTING_KEY = "otp_enabled"

# Brute-force protection: lock an account after this many consecutive failures.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


async def _otp_enabled(db: AsyncSession) -> bool:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == OTP_SETTING_KEY))).scalar_one_or_none()
    return (row.value != "false") if row else True  # OTP is ON by default


async def _set_otp_enabled(db: AsyncSession, enabled: bool) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == OTP_SETTING_KEY))).scalar_one_or_none()
    val = "true" if enabled else "false"
    if row:
        row.value = val
    else:
        db.add(AppSetting(key=OTP_SETTING_KEY, value=val))
    await db.flush()


def _client_ip(request: Request):
    return request.client.host if request and request.client else None


async def _issue_otp(db: AsyncSession, user: User, ip=None, purpose: str = "login") -> dict:
    """Invalidate any pending OTPs of this purpose, generate+store a fresh 6-digit OTP, send it, and audit.

    ``purpose`` is "login" or "reset". The returned token's ``purpose`` claim is "otp"
    (login) or "reset" so the two flows can't cross-verify each other's codes.
    """
    await db.execute(
        update(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.purpose == purpose,
               LoginOtp.consumed == False)  # noqa: E712
        .values(consumed=True)
    )
    code = f"{random.randint(0, 999999):06d}"
    now = datetime.utcnow()
    db.add(LoginOtp(
        user_id=user.id, otp=code, purpose=purpose, created_at=now,
        expires_at=now + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
    ))
    await db.flush()
    label = "Password reset" if purpose == "reset" else "Login"
    await record_audit(db, "OTP_GENERATED", actor=user, entity_type="user", entity_id=user.id,
                       new=purpose, ip=ip)
    emailed = await send_otp_email(user.email, code, user.name, purpose=purpose)
    await record_audit(db, "OTP_SENT", actor=user, entity_type="user", entity_id=user.id,
                       new=("email" if emailed else "console (dev)"), ip=ip)
    await log_event(db, "OTP_SENT", f"{label} OTP sent to {mask_email(user.email)} for {user.name}", actor=user)
    token_purpose = "reset" if purpose == "reset" else "otp"
    otp_token = create_access_token({"sub": str(user.id), "purpose": token_purpose}, timedelta(minutes=settings.OTP_EXPIRE_MINUTES + 5))
    token_key = "resetToken" if purpose == "reset" else "otpToken"
    out = {"otpRequired": True, token_key: otp_token, "email": mask_email(user.email)}
    if not settings.email_configured:
        out["devOtp"] = code  # dev convenience only (never set when SMTP is configured)
    return out


async def _user_from_otp_token(token: str, db: AsyncSession, expected_purpose: str = "otp") -> User:
    payload = decode_token(token)
    msg = ("Reset session expired. Please start again."
           if expected_purpose == "reset" else "OTP session expired. Please sign in again.")
    if not payload or payload.get("purpose") != expected_purpose or not payload.get("sub"):
        raise HTTPException(status_code=401, detail=msg)
    user = (await db.execute(select(User).where(User.id == int(payload["sub"])))).scalar_one_or_none()
    if not user or not user.active:
        raise HTTPException(status_code=401, detail=msg)
    return user


def _user_to_out(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "name": u.name,
        "phone": u.phone,
        "avatar": u.avatar,
        "role": u.role,
        "active": u.active,
        "locked": bool(u.locked_until and u.locked_until > datetime.utcnow()),
        "lockedUntil": (u.locked_until.isoformat() + "Z") if u.locked_until else None,
        "failedAttempts": u.failed_attempts or 0,
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
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Step 1: validate credentials, then issue a login OTP (no access token yet)."""
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()
    ip = _client_ip(request)

    # Distinct messages: unknown username vs wrong password (per spec).
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username and password are incorrect.",
        )

    # Account lockout: block while a lock is active.
    now = datetime.utcnow()
    if user.locked_until and user.locked_until > now:
        mins = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account locked due to multiple failed attempts. Try again in {mins} minute(s).",
        )

    if not verify_password(form_data.password, user.hashed_password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        await record_audit(db, "FAILED_LOGIN", actor=user, entity_type="user", entity_id=user.id,
                           new=f"attempt {user.failed_attempts}", ip=ip)
        await log_event(db, "FAILED_LOGIN", f"Failed login for {user.name} (attempt {user.failed_attempts})", actor=user)
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            await record_audit(db, "ACCOUNT_LOCKED", actor=user, entity_type="user", entity_id=user.id,
                               new=f"locked for {LOCKOUT_MINUTES}m", ip=ip)
            await log_event(db, "ACCOUNT_LOCKED", f"{user.name} locked after {MAX_FAILED_ATTEMPTS} failed attempts", actor=user)
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account locked due to {MAX_FAILED_ATTEMPTS} failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
            )
        await db.commit()
        remaining = MAX_FAILED_ATTEMPTS - user.failed_attempts
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Password is incorrect. {remaining} attempt(s) left before the account is locked.",
        )
    if not user.active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    # Successful credential check — clear any failed-attempt / lock state.
    if user.failed_attempts or user.locked_until:
        user.failed_attempts = 0
        user.locked_until = None
        await db.flush()

    # OTP applies to Super Admin / Admin / Merchant when the OTP toggle is ON.
    # Support agents (separate support portal) always use the direct-login flow.
    otp_on = await _otp_enabled(db)
    if user.role == UserRole.SUPPORT_AGENT or not otp_on:
        token = create_access_token({"sub": str(user.id)})
        await log_event(db, "LOGIN", f"{user.name} ({user.role.value}) signed in", actor=user)
        await record_audit(db, "LOGIN", actor=user, entity_type="user", entity_id=user.id, ip=ip)
        return {"access_token": token, "token_type": "bearer", "user": _user_to_out(user)}

    return await _issue_otp(db, user, ip)


@router.get("/otp-status")
async def otp_status(db: AsyncSession = Depends(get_db)):
    """Public: whether login OTP is currently enabled (drives the login-page toggle)."""
    return {"enabled": await _otp_enabled(db)}


@router.post("/otp-config")
async def otp_config(
    data: OtpConfigRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Toggle login OTP on/off (testing aid)."""
    await _set_otp_enabled(db, data.enabled)
    state = "enabled" if data.enabled else "disabled"
    await log_event(db, "OTP_CONFIG", f"Login OTP {state}")
    await record_audit(db, "OTP_CONFIG", new=state, ip=_client_ip(request))
    return {"enabled": data.enabled}


@router.post("/verify-otp")
async def verify_otp(
    data: OtpVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Step 2: validate the OTP, then create the session (access token)."""
    user = await _user_from_otp_token(data.otpToken, db)
    ip = _client_ip(request)
    code = (data.code or "").strip()

    row = (await db.execute(
        select(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.purpose == "login",
               LoginOtp.consumed == False)  # noqa: E712
        .order_by(LoginOtp.id.desc())
    )).scalars().first()

    if not row or row.otp != code:
        await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                           reason="incorrect code", ip=ip)
        await log_event(db, "OTP_FAILED", f"Incorrect OTP for {user.name}", actor=user)
        await db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP. Please try again.")
    if row.expires_at < datetime.utcnow():
        row.consumed = True
        await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                           reason="expired", ip=ip)
        await log_event(db, "OTP_FAILED", f"Expired OTP for {user.name}", actor=user)
        await db.commit()
        raise HTTPException(status_code=400, detail="OTP has expired. Please resend a new code.")

    # Success
    row.verified = True
    row.consumed = True
    token = create_access_token({"sub": str(user.id)})
    await record_audit(db, "OTP_VERIFIED", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "OTP_VERIFIED", f"OTP verified for {user.name}", actor=user)
    await record_audit(db, "LOGIN", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "LOGIN", f"{user.name} ({user.role.value}) signed in", actor=user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_to_out(user),
    }


@router.post("/resend-otp")
async def resend_otp(
    data: OtpResendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Invalidate the previous OTP and send a fresh one."""
    user = await _user_from_otp_token(data.otpToken, db)
    return await _issue_otp(db, user, _client_ip(request))


# ─── Forgot Password (Email OTP) ───────────────────────────────────────────────
@router.post("/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Step 1: look up the account by username and send a password-reset OTP to its registered email."""
    username = (data.username or "").strip()
    user = (await db.execute(
        select(User).where(User.username == username)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="No account found with that username.")
    if not user.active:
        raise HTTPException(status_code=403, detail="This account is deactivated. Contact an administrator.")
    # Returns the masked destination email so the user knows where the code was sent.
    return await _issue_otp(db, user, _client_ip(request), purpose="reset")


@router.post("/verify-reset-otp")
async def verify_reset_otp(
    data: VerifyResetOtpRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Step 2: verify the reset OTP, returning a short-lived token authorising a new password."""
    user = await _user_from_otp_token(data.resetToken, db, expected_purpose="reset")
    ip = _client_ip(request)
    code = (data.code or "").strip()
    row = (await db.execute(
        select(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.purpose == "reset",
               LoginOtp.consumed == False)  # noqa: E712
        .order_by(LoginOtp.id.desc())
    )).scalars().first()

    if not row or row.otp != code:
        await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                           reason="incorrect reset code", ip=ip)
        await db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP. Please try again.")
    if row.expires_at < datetime.utcnow():
        row.consumed = True
        await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                           reason="expired reset code", ip=ip)
        await db.commit()
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new code.")

    row.verified = True  # validated but not yet consumed (consumed when the password is set)
    await record_audit(db, "OTP_VERIFIED", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "OTP_VERIFIED", f"Reset OTP verified for {user.name}", actor=user)
    confirmed = create_access_token({"sub": str(user.id), "purpose": "reset_ok"}, timedelta(minutes=15))
    return {"confirmedToken": confirmed}


@router.post("/reset-password")
async def reset_password(
    data: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Step 3: set the new password (complexity + no-reuse enforced) and unlock the account."""
    user = await _user_from_otp_token(data.confirmedToken, db, expected_purpose="reset_ok")
    ip = _client_ip(request)
    row = (await db.execute(
        select(LoginOtp)
        .where(LoginOtp.user_id == user.id, LoginOtp.purpose == "reset",
               LoginOtp.verified == True, LoginOtp.consumed == False)  # noqa: E712
        .order_by(LoginOtp.id.desc())
    )).scalars().first()
    if not row or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Reset session expired. Please start again.")

    await assert_password_allowed(db, user, data.newPassword)
    await set_password(db, user, data.newPassword)
    row.consumed = True
    # A successful reset clears any lockout so the new password works immediately.
    user.failed_attempts = 0
    user.locked_until = None
    db.add(Notification(user_id=user.id, message="Your password was reset successfully", icon="🔑"))
    await record_audit(db, "PASSWORD_RESET", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "PASSWORD_RESET", f"{user.name} reset their password via email OTP", actor=user)
    return {"message": "Password updated successfully. You can now sign in with your new password."}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return _user_to_out(current_user)
