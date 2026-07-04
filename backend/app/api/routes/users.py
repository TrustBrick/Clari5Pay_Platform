import re
import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.models import User, UserRole, Notification
from app.core.security import get_password_hash, verify_password
from app.core.passwords import assert_password_allowed, set_password
from app.core.deps import get_current_user, get_current_admin, get_current_super_admin
from app.core.uploads import validate_upload, IMAGE_TYPES
from app.core.config import settings
from app.schemas.schemas import (
    ChangePasswordRequest, ProfileUpdateRequest, ReasonRequest, AdminResetPasswordRequest,
)
from app.api.routes.system_logs import log_event, record_audit


def _ip(request: Request) -> str | None:
    return request.client.host if request and request.client else None

router = APIRouter(prefix="/api/users", tags=["users"])


def _u(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "email": u.email, "name": u.name,
        "phone": u.phone, "avatar": u.avatar, "role": u.role, "active": u.active, "created": str(u.created),
        "createdAt": (u.created_at.isoformat() + "Z") if u.created_at else None,
        "createdBy": u.created_by,
        "locked": bool(u.locked_until and u.locked_until > datetime.utcnow()),
        "lockedUntil": (u.locked_until.isoformat() + "Z") if u.locked_until else None,
        "failedAttempts": u.failed_attempts or 0,
        "payIn": u.pay_in, "payOut": u.pay_out, "settlement": u.settlement,
        "payInFee": u.pay_in_fee, "payOutFee": u.pay_out_fee, "settlementFee": u.settlement_fee,
        "balance": u.balance, "risk": u.risk, "profile": u.profile,
        "merchantRole": u.merchant_role,
        "merchantCode": u.merchant_code,
        "country": u.country, "fullName": u.full_name,
        "whatsappEnabled": bool(u.whatsapp_enabled),
    }


# Serial ID helpers. Codes are "<PREFIX><digits>" (bank-account style). Independent series per
# prefix — each continues after its own current max, so a code never collides or gets reused.
# Prefixes: Production merchants/users → "MID…"; demo merchant COMPANIES → "MER…"; demo USERS →
# the first 3 letters of their business name (e.g. Nexus Fintech → NEX00001). The business-derived
# prefix keeps demo user codes distinct from Production's MID codes with no numeric band needed.


async def _next_code(db: AsyncSession, prefix: str, width: int = 6) -> str:
    codes = (await db.execute(
        select(User.merchant_code).where(User.merchant_code.like(f"{prefix}%"))
    )).scalars().all()
    maxn, plen = 0, len(prefix)
    for c in codes:
        try:
            maxn = max(maxn, int(c[plen:]))
        except (TypeError, ValueError):
            continue
    return f"{prefix}{maxn + 1:0{width}d}"


def _business_prefix(name: str) -> str:
    """First 3 letters of a business name, uppercased — the demo user-ID prefix (e.g. 'Nexus
    Fintech' → NEX). Falls back to 'USR' when the name has fewer than 3 letters."""
    letters = re.sub(r"[^A-Za-z]", "", name or "")
    return letters[:3].upper() or "USR"


async def _next_merchant_code(db: AsyncSession) -> str:
    """Next serial Merchant ID (MID…) — Production merchants and all users continue this series."""
    return await _next_code(db, "MID")


async def _next_company_code(db: AsyncSession) -> str:
    """Demo only: next COMPANY Merchant ID (MER…) — companies get their own independent series."""
    return await _next_code(db, "MER")


async def _next_user_code(db: AsyncSession, business_name: str) -> str:
    """Demo only: next USER ID for a business — prefixed with the first 3 letters of the business
    name (e.g. Nexus Fintech → NEX00001), numbered per-prefix (5-digit)."""
    return await _next_code(db, _business_prefix(business_name), width=5)


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
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    # Super Admin cannot create merchants — only Admins.
    if admin.role == UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super Admin cannot create merchants")

    # Demo separates Companies (onboarding → MER code, no login) from Users (Create User → MID
    # code, real login). A sub-user is identified by carrying a merchant_role. Production is
    # unchanged: every merchant-role row is a login with a MID code.
    is_sub_user = bool(data.get("merchantRole"))
    if settings.is_demo and not is_sub_user:
        merchant_code = await _next_company_code(db)
        # A company collects no credentials — fill the NOT NULL login columns with a placeholder so
        # the company row exists but is not a usable login (username = its MER code, random secret).
        username = (data.get("username") or merchant_code).lower()
        raw_password = data.get("password") or secrets.token_urlsafe(24)
    elif settings.is_demo:
        merchant_code = await _next_user_code(db, data.get("name"))
        username, raw_password = data["username"], data["password"]
    else:
        merchant_code = await _next_merchant_code(db)
        username, raw_password = data["username"], data["password"]

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=username,
        hashed_password=get_password_hash(raw_password),
        email=data["email"],
        name=data["name"],
        phone=data.get("phone"),
        role=UserRole.MERCHANT,
        active=True,
        created_by=admin.id,
        merchant_code=merchant_code,
        pay_in=data.get("payIn"),
        pay_out=data.get("payOut"),
        settlement=data.get("settlement"),
        pay_in_fee=float(data.get("payInFee", 1.5)),
        pay_out_fee=float(data.get("payOutFee", 1.2)),
        settlement_fee=(float(data["settlementFee"]) if data.get("settlementFee") not in (None, "") else None),
        country=data.get("country"),
        full_name=data.get("fullName"),
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
    await record_audit(db, "MERCHANT_CREATED", actor=admin, entity_type="merchant", entity_id=user.id,
                       new=f"{user.name} ({user.username})", ip=_ip(request))
    await db.refresh(user)
    return _u(user)


@router.post("/admins")
async def create_admin(
    data: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required to create an admin")
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
    await log_event(db, "ADMIN_CREATED", f"Admin \"{user.name}\" created by {sa.name} ({sa.role.value}) — reason: {reason}", actor=sa)
    await record_audit(db, "ADMIN_CREATED", actor=sa, entity_type="admin", entity_id=user.id,
                       new=f"{user.name} ({user.username})", reason=reason, ip=_ip(request))
    await db.refresh(user)
    return _u(user)


@router.patch("/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    request: Request,
    data: ReasonRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    reason = (data.reason if data else None) or ""
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required")
    was_active = user.active
    user.active = not user.active
    await db.flush()
    state = "activated" if user.active else "deactivated"
    db.add(Notification(user_id=user.id, message=f"Your account was {state} — {reason}", icon="🔔"))
    if actor.id != user.id:
        db.add(Notification(user_id=actor.id, message=f"{user.name} {state}", icon="🔔"))
    await log_event(db, "USER_TOGGLED", f"{user.role.value} \"{user.name}\" {state} by {actor.name} — reason: {reason}", actor=actor)
    await record_audit(db, "USER_TOGGLED", actor=actor, entity_type=user.role.value.lower(), entity_id=user.id,
                       old=f"active={was_active}", new=f"active={user.active}", reason=reason, ip=_ip(request))
    await db.refresh(user)
    return _u(user)


@router.patch("/{user_id}/unlock")
async def unlock_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_admin),
):
    """Manually clear a locked account (failed attempts + lockout)."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.failed_attempts = 0
    user.locked_until = None
    await db.flush()
    db.add(Notification(user_id=user.id, message="Your account was unlocked by an administrator", icon="🔓"))
    await log_event(db, "ACCOUNT_UNLOCKED", f"{user.role.value} \"{user.name}\" unlocked by {actor.name}", actor=actor)
    await record_audit(db, "ACCOUNT_UNLOCKED", actor=actor, entity_type=user.role.value.lower(), entity_id=user.id,
                       ip=_ip(request))
    await db.refresh(user)
    return _u(user)


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    data: AdminResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sa: User = Depends(get_current_super_admin),
):
    """Super Admin directly resets another user's password (e.g. an Admin who can't receive OTPs)."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Cannot reset a Super Admin password here")
    await assert_password_allowed(db, user, data.new_password)
    await set_password(db, user, data.new_password)
    # Resetting also clears any lockout so the new credentials work immediately.
    user.failed_attempts = 0
    user.locked_until = None
    await db.flush()
    db.add(Notification(user_id=user.id, message="Your password was reset by the Super Admin", icon="🔑"))
    await log_event(db, "PASSWORD_RESET", f"{sa.name} reset {user.role.value} \"{user.name}\"'s password", actor=sa)
    await record_audit(db, "PASSWORD_RESET", actor=sa, entity_type=user.role.value.lower(), entity_id=user.id,
                       ip=_ip(request))
    return {"message": f"Password reset for {user.name}. They can now sign in with the new password."}


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    await assert_password_allowed(db, current_user, data.new_password)
    await set_password(db, current_user, data.new_password)
    await log_event(db, "PASSWORD_CHANGED", f"{current_user.name} changed their password", actor=current_user)
    await record_audit(db, "PASSWORD_CHANGED", actor=current_user, entity_type="user", entity_id=current_user.id)
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
        await assert_password_allowed(db, current_user, data.new_password)
        await set_password(db, current_user, data.new_password)
        await log_event(db, "PASSWORD_CHANGED", f"{current_user.name} changed their password", actor=current_user)
        await record_audit(db, "PASSWORD_CHANGED", actor=current_user, entity_type="user", entity_id=current_user.id)

    if data.email and data.email != current_user.email:
        existing = await db.execute(
            select(User).where(User.email == data.email, User.id != current_user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use")
        current_user.email = data.email

    if data.phone is not None:
        # Own contact number — where transaction/WhatsApp notifications are sent. Empty clears it;
        # otherwise store in tidy E.164-ish form (leading '+' + digits). 8–15 digits per E.164.
        raw = (data.phone or "").strip()
        if raw:
            digits = re.sub(r"\D", "", raw)
            if not (8 <= len(digits) <= 15):
                raise HTTPException(status_code=400, detail="Enter a valid phone number with country code, e.g. +919812345678")
            current_user.phone = "+" + digits
        else:
            current_user.phone = None

    if data.avatar is not None:
        # Empty string clears the picture; a data URL sets it (validated for type + size).
        current_user.avatar = validate_upload(data.avatar, allowed=IMAGE_TYPES, label="profile picture") or None

    if data.whatsappEnabled is not None:
        # Per-user "Receive WhatsApp Notifications" preference (in-app notifications are unaffected).
        current_user.whatsapp_enabled = bool(data.whatsappEnabled)

    await db.flush()
    await db.refresh(current_user)
    return _u(current_user)
