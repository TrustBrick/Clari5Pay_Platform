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
from app.core.ratelimit import rate_limit

# Max wrong OTP guesses on a single code before it is invalidated (user must resend).
MAX_OTP_ATTEMPTS = 5
from app.schemas.schemas import (
    OtpVerifyRequest, OtpResendRequest, OtpConfigRequest,
    ForgotPasswordRequest, VerifyResetOtpRequest, ResetPasswordRequest,
)
from app.api.routes.system_logs import log_event, record_audit
from app.services import presence

router = APIRouter(prefix="/api/auth", tags=["auth"])

OTP_SETTING_KEY = "otp_enabled"

# Brute-force protection: lock an account after this many consecutive failures.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# Roles with no inactivity/session timeout: their access token is effectively non-expiring,
# so they stay signed in until they explicitly log out or their account is deactivated
# (token revocation). All other roles keep the standard ACCESS_TOKEN_EXPIRE_MINUTES lifetime.
NO_TIMEOUT_ROLES = (UserRole.ADMIN, UserRole.SUPER_ADMIN)


def _issue_session_token(user: User) -> str:
    """Access token for a fully-authenticated session, with a role-based lifetime.
    Admin / Super Admin get an effectively non-expiring token; everyone else keeps the
    configured timeout. JWT authenticity and permission checks are unchanged."""
    expires = timedelta(days=settings.ADMIN_TOKEN_EXPIRE_DAYS) if user.role in NO_TIMEOUT_ROLES else None
    return create_access_token({"sub": str(user.id)}, expires)


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
        "merchantCode": u.merchant_code,
    }


@router.post("/login", dependencies=[Depends(rate_limit(30, 60, "login"))])
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

    # OTP is mandatory for every successful login (Super Admin / Admin / Merchant) — there is
    # no toggle to disable it. Support agents use the separate support portal's direct-login
    # flow (that portal has no OTP screen), so they remain exempt by design.
    if user.role == UserRole.SUPPORT_AGENT:
        token = create_access_token({"sub": str(user.id)})
        await log_event(db, "LOGIN", f"{user.name} ({user.role.value}) signed in", actor=user)
        await record_audit(db, "LOGIN", actor=user, entity_type="user", entity_id=user.id, ip=ip)
        await presence.start_session(db, user, ip, request.headers.get("user-agent"))
        return {"access_token": token, "token_type": "bearer", "user": _user_to_out(user)}

    # Valid credentials → always generate + email an OTP and move to the verification step.
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


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Best-effort logout hook for the client (called on manual logout and on the
    inactivity timeout). Access tokens here are stateless JWTs, so they are not
    server-side revoked — the client clears its own token/cookies/storage. This
    endpoint records the event for the audit trail. Always returns 200 so a logout
    is never blocked by a transient backend issue (the client ignores failures).
    """
    await log_event(db, "LOGOUT", f"{current_user.name} ({current_user.role.value}) signed out", actor=current_user)
    await record_audit(db, "LOGOUT", actor=current_user, entity_type="user",
                       entity_id=current_user.id, ip=_client_ip(request))
    await presence.end_session(db, current_user)
    return {"status": "ok"}


@router.post("/verify-otp", dependencies=[Depends(rate_limit(20, 60, "verify-otp"))])
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
        # Count the wrong guess; invalidate the code once too many are made (forces a resend).
        if row:
            row.attempts = (row.attempts or 0) + 1
            if row.attempts >= MAX_OTP_ATTEMPTS:
                row.consumed = True
                await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                                   reason="too many incorrect attempts", ip=ip)
                await log_event(db, "OTP_LOCKED", f"OTP locked after {MAX_OTP_ATTEMPTS} wrong tries for {user.name}", actor=user)
                await db.commit()
                raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please resend a new code.")
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
    token = _issue_session_token(user)
    await record_audit(db, "OTP_VERIFIED", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "OTP_VERIFIED", f"OTP verified for {user.name}", actor=user)
    await record_audit(db, "LOGIN", actor=user, entity_type="user", entity_id=user.id, ip=ip)
    await log_event(db, "LOGIN", f"{user.name} ({user.role.value}) signed in", actor=user)
    await presence.start_session(db, user, ip, request.headers.get("user-agent"))
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_to_out(user),
    }


@router.post("/resend-otp", dependencies=[Depends(rate_limit(5, 300, "resend-otp"))])
async def resend_otp(
    data: OtpResendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Invalidate the previous OTP and send a fresh one."""
    user = await _user_from_otp_token(data.otpToken, db)
    return await _issue_otp(db, user, _client_ip(request))


# ─── Forgot Password (Email OTP) ───────────────────────────────────────────────
@router.post("/forgot-password", dependencies=[Depends(rate_limit(5, 300, "forgot-password"))])
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


@router.post("/verify-reset-otp", dependencies=[Depends(rate_limit(20, 60, "verify-reset-otp"))])
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
        if row:
            row.attempts = (row.attempts or 0) + 1
            if row.attempts >= MAX_OTP_ATTEMPTS:
                row.consumed = True
                await record_audit(db, "OTP_FAILED", actor=user, entity_type="user", entity_id=user.id,
                                   reason="too many incorrect reset attempts", ip=ip)
                await log_event(db, "OTP_LOCKED", f"Reset OTP locked after {MAX_OTP_ATTEMPTS} wrong tries for {user.name}", actor=user)
                await db.commit()
                raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please request a new code.")
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


@router.post("/reset-password", dependencies=[Depends(rate_limit(20, 60, "reset-password"))])
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
