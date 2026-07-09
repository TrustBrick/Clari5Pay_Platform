"""Merchant KYC Update module — Aadhaar / PAN / Passport / OCR / DigiLocker verification.

Access is restricted to MERCHANT users with a Supervisor or Manager role (enforced by
``get_current_kyc_user``). Every endpoint validates its input server-side, then delegates
to the ``app.services.kyc`` seam. Until the Melento.ai / DigiLocker credentials are supplied
via env, the seam raises ``KYCNotConfigured`` and we return a clear 503 — the UI handles it
gracefully. No existing schema, route, or data is touched by this module.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_kyc_user
from app.db.session import get_db
from app.models.models import KycVerificationHistory, User
from app.services import kyc as kyc_service
from app.services.membership import lookup_member_name, normalize_member_id

router = APIRouter(prefix="/api/kyc", tags=["kyc"])

# ── Validation patterns (mirror the client-side rules so the API is safe on its own) ──
AADHAAR_RE = re.compile(r"^\d{12}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PASSPORT_RE = re.compile(r"^[A-Z][0-9]{7}$")
OCR_ALLOWED_TYPES = {"jpg", "jpeg", "png", "pdf"}
OCR_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


class AadhaarRequest(BaseModel):
    aadhaarNumber: str


class PanRequest(BaseModel):
    panNumber: str


class PassportRequest(BaseModel):
    passportNumber: str
    dateOfBirth: str | None = None


class OcrRequest(BaseModel):
    documentType: str
    fileName: str
    fileData: str          # base64 data URL of the uploaded document


def _unavailable(exc: kyc_service.KYCNotConfigured) -> HTTPException:
    """Map a not-configured provider to a graceful, client-friendly 503."""
    return HTTPException(
        status_code=503,
        detail=f"{exc.provider} verification is not available yet — API credentials will be connected soon.",
    )


@router.post("/aadhaar/verify")
async def aadhaar_verify(body: AadhaarRequest, _: User = Depends(get_current_kyc_user)):
    number = body.aadhaarNumber.replace(" ", "").strip()
    if not AADHAAR_RE.match(number):
        raise HTTPException(status_code=400, detail="Invalid Aadhaar Number — must be exactly 12 digits.")
    try:
        return await kyc_service.verify_aadhaar(number)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/pan/verify")
async def pan_verify(body: PanRequest, _: User = Depends(get_current_kyc_user)):
    number = body.panNumber.upper().strip()
    if not PAN_RE.match(number):
        raise HTTPException(status_code=400, detail="Invalid PAN Number — expected format ABCDE1234F.")
    try:
        return await kyc_service.verify_pan(number)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/passport/verify")
async def passport_verify(body: PassportRequest, _: User = Depends(get_current_kyc_user)):
    number = body.passportNumber.upper().strip()
    if not PASSPORT_RE.match(number):
        raise HTTPException(status_code=400, detail="Invalid Passport Number — expected format A1234567.")
    try:
        return await kyc_service.verify_passport(number, body.dateOfBirth)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/ocr/extract")
async def ocr_extract(body: OcrRequest, _: User = Depends(get_current_kyc_user)):
    ext = body.fileName.rsplit(".", 1)[-1].lower() if "." in body.fileName else ""
    if ext not in OCR_ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file type — allowed: JPG, JPEG, PNG, PDF.")
    # base64 payload is ~4/3 of the raw byte size; guard against oversized uploads.
    approx_bytes = (len(body.fileData) * 3) // 4
    if approx_bytes > OCR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large — maximum size is 10 MB.")
    try:
        return await kyc_service.verify_ocr(body.documentType, body.fileName, body.fileData)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/digilocker/verify")
async def digilocker_verify(_: User = Depends(get_current_kyc_user)):
    """Verify Aadhaar via DigiLocker — the customer authenticates with DigiLocker and the
    verified Aadhaar document is retrieved (no manual Aadhaar entry). Returns the same
    Aadhaar result shape as /aadhaar/verify so the UI renders one unified details card."""
    try:
        return await kyc_service.verify_via_digilocker()
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Live Melento.ai integration — membership-based Aadhaar (DigiLocker) + PAN.
#  Every request is persisted to kyc_verification_history (new row per request);
#  the Aadhaar status poll updates its own originating row. Scoped to the caller's
#  merchant business pool (User.name), same as the transaction membership lookup.
# ══════════════════════════════════════════════════════════════════════════════

def _actor_name(user: User) -> str:
    return (user.full_name or user.name or user.username or "").strip() or user.username


def _gen_reference(prefix: str) -> str:
    """`<PREFIX>` + a numeric value (e.g. AADHAAR123456789 / PAN123456789).

    The provider rejects reused reference ids, so we combine the current epoch-ms with a
    random tail to make collisions effectively impossible while keeping it purely numeric.
    """
    return f"{prefix}{int(datetime.utcnow().timestamp() * 1000)}{random.randint(1000, 9999)}"


def _history_summary(row: KycVerificationHistory) -> dict:
    """List-shape row (omits the large request/response JSON blobs)."""
    return {
        "id": row.id,
        "membershipId": row.membership_id,
        "memberName": row.member_name,
        "verificationType": row.verification_type,
        "referenceId": row.reference_id,
        "transactionId": row.transaction_id,
        "status": row.verification_status,
        "createdBy": row.created_by,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


async def _require_member_name(db: AsyncSession, user: User, membership_id: str) -> tuple[str, str]:
    """Resolve (normalized membership id, member name) or raise 404 'Membership not found.'."""
    mid = normalize_member_id(membership_id)
    if not mid:
        raise HTTPException(status_code=404, detail="Membership not found.")
    name = await lookup_member_name(db, user, mid)
    if not name:
        raise HTTPException(status_code=404, detail="Membership not found.")
    return mid, name


class MembershipRequest(BaseModel):
    membershipId: str


class AadhaarStatusRequest(BaseModel):
    historyId: int


class PanVerifyRequest(BaseModel):
    membershipId: str
    pan: str


@router.get("/member/{membership_id}")
async def kyc_member_lookup(
    membership_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Auto-fill the Member Name for a Membership ID within the caller's business pool.
    404 'Membership not found.' when the ID has never been used."""
    mid, name = await _require_member_name(db, user, membership_id)
    return {"membershipId": mid, "memberName": name}


@router.post("/aadhaar/generate-link")
async def aadhaar_generate_link(
    body: MembershipRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Generate a DigiLocker Aadhaar verification link for a member and record the attempt."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)
    reference_id = _gen_reference("AADHAAR")
    request_payload = {"reference_id": reference_id, "source": "AADHAAR"}
    data, http_status = await kyc_service.melento_generate_aadhaar_url(reference_id)

    status_val = str(data.get("status") or "").lower()
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    link = (result or {}).get("link")
    transaction_id = data.get("transaction_id")
    ok = status_val == "success" and bool(link)
    error_message = None if ok else (data.get("message") or data.get("error") or "Failed to generate the verification link.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="AADHAAR",
        reference_id=reference_id,
        transaction_id=transaction_id,
        verification_status="PENDING" if ok else "FAILED",
        request_json=json.dumps(request_payload),
        response_json=json.dumps(data),
        error_message=error_message,
        generated_link=link,
        api_status=str(data.get("status") or http_status),
        created_by=_actor_name(user),
        merchant_business=user.name,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)

    if not ok:
        # Persist the FAILED record before surfacing the error — get_db() rolls back on a raised
        # exception, so commit first so the audit row is never lost.
        await db.commit()
        raise HTTPException(status_code=502, detail=error_message)

    return {
        "id": row.id,
        "referenceId": reference_id,
        "transactionId": transaction_id,
        "link": link,
        "status": row.verification_status,
        "message": data.get("message"),
    }


@router.post("/aadhaar/status")
async def aadhaar_status(
    body: AadhaarStatusRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Poll DigiLocker for the Aadhaar details and update the originating history row."""
    row = (await db.execute(
        select(KycVerificationHistory).where(
            KycVerificationHistory.id == body.historyId,
            KycVerificationHistory.merchant_business == user.name,
            KycVerificationHistory.verification_type == "AADHAAR",
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Verification record not found.")

    data, _ = await kyc_service.melento_get_aadhaar_details(row.reference_id, row.transaction_id)
    status_val = str(data.get("status") or "").lower()
    err_text = str(data.get("error") or data.get("message") or "")
    row.updated_at = datetime.utcnow()

    if status_val == "success":
        row.verification_status = "SUCCESS"
        row.api_status = "success"
        row.error_message = None
        row.response_json = json.dumps(data)
        db.add(row)
        return {"pending": False, "status": "SUCCESS", "details": data}

    # The provider signals "not yet done" as status=failed + error="Validation Pending" (or
    # similar). Treat any pending/processing marker as still-in-progress, not a hard failure,
    # so the row stays PENDING and the UI shows "Verification Under Process".
    if "pending" in err_text.lower() or "process" in err_text.lower() or status_val in ("pending", "processing"):
        return {"pending": True, "status": "PENDING", "message": err_text or None}

    # A genuine failure.
    row.verification_status = "FAILED"
    row.api_status = status_val or "failed"
    row.error_message = err_text or "Aadhaar verification failed."
    row.response_json = json.dumps(data)
    db.add(row)
    return {"pending": False, "status": "FAILED", "error": row.error_message, "details": data}


@router.post("/pan/verify-membership")
async def pan_verify_membership(
    body: PanVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Verify a PAN for a member and record the request/response."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)
    pan = (body.pan or "").upper().strip()
    if not PAN_RE.match(pan):
        raise HTTPException(status_code=400, detail="Invalid PAN Number — expected format ABCDE1234F.")

    reference_id = _gen_reference("PAN")
    request_payload = {"reference_id": reference_id, "source_type": "id", "source": pan}
    data, http_status = await kyc_service.melento_pan_verify(reference_id, pan)

    status_val = str(data.get("status") or "").lower()
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    valid_pan = bool((result or {}).get("valid_pan"))
    ok = status_val == "success"
    error_message = None if ok else (data.get("message") or data.get("error") or "PAN verification failed.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="PAN",
        reference_id=data.get("reference_id") or reference_id,
        transaction_id=data.get("transaction_id"),
        verification_status="SUCCESS" if ok else "FAILED",
        request_json=json.dumps(request_payload),
        response_json=json.dumps(data),
        error_message=error_message,
        api_status=str(data.get("status") or http_status),
        created_by=_actor_name(user),
        merchant_business=user.name,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)

    if not ok:
        # Persist the FAILED record before raising (get_db rolls back on exception).
        await db.commit()
        raise HTTPException(status_code=502, detail=error_message)

    return {"id": row.id, "status": row.verification_status, "validPan": valid_pan, "result": result, "raw": data}


@router.get("/history")
async def kyc_history(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """All KYC verifications for the caller's merchant business pool, newest first."""
    rows = (await db.execute(
        select(KycVerificationHistory)
        .where(KycVerificationHistory.merchant_business == user.name)
        .order_by(KycVerificationHistory.id.desc())
    )).scalars().all()
    return [_history_summary(r) for r in rows]


@router.get("/history/{history_id}")
async def kyc_history_detail(
    history_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """One verification record incl. the full request/response JSON (for the View Details popup)."""
    row = (await db.execute(
        select(KycVerificationHistory).where(
            KycVerificationHistory.id == history_id,
            KycVerificationHistory.merchant_business == user.name,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Verification record not found.")

    def _parse(blob: str | None):
        if not blob:
            return None
        try:
            return json.loads(blob)
        except (ValueError, TypeError):
            return None

    return {
        **_history_summary(row),
        "generatedLink": row.generated_link,
        "apiStatus": row.api_status,
        "errorMessage": row.error_message,
        "request": _parse(row.request_json),
        "response": _parse(row.response_json),
        "updatedAt": (row.updated_at.isoformat() + "Z") if row.updated_at else None,
    }
