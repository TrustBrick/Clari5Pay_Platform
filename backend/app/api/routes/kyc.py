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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.system_logs import record_audit
from app.core.deps import get_current_kyc_user
from app.db.session import get_db
from app.models.models import KycVerificationHistory, NonMemberKyc, User
from app.services import kyc as kyc_service
from app.services.membership import lookup_member_name, normalize_member_id

router = APIRouter(prefix="/api/kyc", tags=["kyc"])

# ── Validation patterns (mirror the client-side rules so the API is safe on its own) ──
AADHAAR_RE = re.compile(r"^\d{12}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# Passport *File Number* (from the passport's back page) — NOT the passport number. The API docs
# define no strict format, so we only require a non-empty alphanumeric value; Melento validates it.
PASSPORT_RE = re.compile(r"^[A-Z0-9]+$")
OCR_ALLOWED_TYPES = {"jpg", "jpeg", "png", "pdf"}
OCR_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
# Image-upload verification (PAN / Passport / Aadhaar) accepts only PNG / JPG / JPEG.
IMAGE_DATA_URL_RE = re.compile(r"^data:image/(png|jpe?g);base64,", re.IGNORECASE)

# Verification-method labels stored on each history row (spec: "ID Number" / "Image Upload").
METHOD_ID = "ID Number"
METHOD_IMAGE = "Image Upload"
METHOD_DIGILOCKER = "DigiLocker"


class AadhaarRequest(BaseModel):
    aadhaarNumber: str


class PanRequest(BaseModel):
    panNumber: str


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
        "verificationMethod": row.verification_method,
        "documentType": row.document_type,
        "referenceId": row.reference_id,
        "transactionId": row.transaction_id,
        "status": row.verification_status,
        "createdBy": row.created_by,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


def _image_b64(data_url: str, field: str = "image") -> str:
    """Validate an uploaded image is PNG/JPG/JPEG and within the size limit, then return the raw
    base64 (data-URL prefix stripped) that Melento expects as ``source``."""
    if not data_url or not IMAGE_DATA_URL_RE.match(data_url):
        raise HTTPException(status_code=400, detail=f"Unsupported {field} — allowed types: PNG, JPG, JPEG.")
    b64 = data_url.split(",", 1)[-1]
    if (len(b64) * 3) // 4 > OCR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image too large — maximum size is 10 MB.")
    return b64


# ── Non-Member KYC (walk-ins with no Membership ID) ───────────────────────────
# A person who is not a registered member can still be verified: they are stored in the
# independent ``non_member_kyc`` table under their own NM… series and then flow through the
# very same verification routes. Nothing here reads or writes the Merchant membership records.
NON_MEMBER_RE = re.compile(r"^NM\d{6,}$")
SUBJECT_NOT_FOUND = "No Member or Non-Member found for this ID."
SUBJECT_MEMBER = "MEMBER"
SUBJECT_NON_MEMBER = "NON_MEMBER"
# Contact validation mirrors the Agent module's rules so the portal behaves consistently.
_MOBILE_RE = re.compile(r"^\d{10}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _find_duplicate_non_member(
    db: AsyncSession, user: User, *, mobile: str | None, email: str | None, exclude_id: int | None = None
) -> NonMemberKyc | None:
    """An existing Non-Member in this business pool reachable by the same mobile or email.

    This is the "do not create duplicate Non-Member records" guard: a returning walk-in should be
    found and re-used, not issued a second NM id. Name alone is deliberately not matched — it is
    far too weak an identifier to block on.
    """
    terms = [func.lower(field) == value.lower()
             for field, value in ((NonMemberKyc.mobile, mobile), (NonMemberKyc.email, email))
             if value]
    if not terms:
        return None
    stmt = select(NonMemberKyc).where(
        NonMemberKyc.merchant_business == user.name,
        or_(*terms),
    )
    if exclude_id is not None:
        stmt = stmt.where(NonMemberKyc.id != exclude_id)
    return (await db.execute(stmt.limit(1))).scalars().first()


async def _next_non_member_id(db: AsyncSession) -> str:
    """Next global serial Non-Member ID (NM000001…). Its own series — deliberately independent
    of the Merchant Membership numbering; never reused."""
    codes = (await db.execute(
        select(NonMemberKyc.non_member_id).where(NonMemberKyc.non_member_id.like("NM%"))
    )).scalars().all()
    maxn = 0
    for c in codes:
        try:
            maxn = max(maxn, int(c[2:]))
        except (TypeError, ValueError):
            continue
    return f"NM{maxn + 1:06d}"


async def _non_member_row(db: AsyncSession, user: User, nm_id: str) -> NonMemberKyc | None:
    """The Non-Member with this id inside the caller's business pool, or None."""
    if not nm_id or not NON_MEMBER_RE.match(nm_id):
        return None
    return (await db.execute(
        select(NonMemberKyc).where(
            NonMemberKyc.non_member_id == nm_id,
            NonMemberKyc.merchant_business == user.name,
        )
    )).scalar_one_or_none()


def _serialize_non_member(r: NonMemberKyc) -> dict:
    return {
        "id": r.id,
        "nonMemberId": r.non_member_id,
        "fullName": r.full_name,
        "mobile": r.mobile,
        "email": r.email,
        "aadhaarNumber": r.aadhaar_number,
        "panNumber": r.pan_number,
        "passportNumber": r.passport_number,
        "country": r.country,
        "state": r.state,
        "location": r.location,
        "createdBy": r.created_by,
        "createdAt": (r.created_at.isoformat() + "Z") if r.created_at else None,
        "updatedBy": r.updated_by,
        "updatedAt": (r.updated_at.isoformat() + "Z") if r.updated_at else None,
    }


def _result_aadhaar(data: dict) -> str | None:
    """The Aadhaar number out of a provider response, if it exposed one. DigiLocker and the OCR
    reader disagree on the key, and either may mask or omit it — hence the tolerant lookup."""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    for key in ("aadhaar_number", "aadhaar_no", "uid"):
        value = (result or {}).get(key)
        if value:
            return str(value).strip() or None
    return None


async def _capture_verified_doc(db: AsyncSession, user: User, subject_id: str, field: str, value: str | None) -> None:
    """Stamp a successfully verified document number onto the Non-Member's record.

    A no-op for Members (they have no row here) and when the number is unknown — e.g. an Image
    Upload / DigiLocker flow where the provider, not the operator, supplies the number. Every
    attempt is audited in ``kyc_verification_history`` regardless; this only maintains the
    at-a-glance summary on the person's own record.
    """
    if not value:
        return
    row = await _non_member_row(db, user, subject_id)
    if row is None or getattr(row, field) == value:
        return
    setattr(row, field, value)
    row.updated_by = _actor_name(user)
    row.updated_by_id = user.id
    row.updated_at = datetime.utcnow()
    db.add(row)


async def _require_member_name(db: AsyncSession, user: User, membership_id: str) -> tuple[str, str]:
    """Resolve (normalized id, name) for the KYC subject — a Member *or* a Non-Member — or 404.

    Every verification route funnels through this one function, which is why recognising a
    Non-Member id here is all it takes for Aadhaar / PAN / Passport / OCR to verify a walk-in:
    their own logic is untouched and simply sees an id and a name.

    Members win ties: an id is only looked up in ``non_member_kyc`` once it is confirmed absent
    from the membership records, so an existing Membership ID always resolves to Member data even
    in the unlikely event one is shaped like ``NM…``.
    """
    mid = normalize_member_id(membership_id)
    if not mid:
        raise HTTPException(status_code=404, detail=SUBJECT_NOT_FOUND)
    name = await lookup_member_name(db, user, mid)
    if name:
        return mid, name
    row = await _non_member_row(db, user, mid)
    if row is not None:
        return row.non_member_id, row.full_name
    raise HTTPException(status_code=404, detail=SUBJECT_NOT_FOUND)


class MembershipRequest(BaseModel):
    membershipId: str


class AadhaarStatusRequest(BaseModel):
    historyId: int


class PanVerifyRequest(BaseModel):
    membershipId: str
    pan: str | None = None          # ID Number method
    image: str | None = None        # Image Upload method (base64 data URL of the PAN card)


class PassportVerifyRequest(BaseModel):
    membershipId: str
    passportNumber: str | None = None   # ID Number (File Number) method
    dateOfBirth: str | None = None
    frontImage: str | None = None       # Image Upload method — front page (base64 data URL)
    backImage: str | None = None        # Image Upload method — back page (base64 data URL)


class AadhaarImageRequest(BaseModel):
    membershipId: str
    image: str                          # base64 data URL of the Aadhaar card


class OcrVerifyRequest(BaseModel):
    membershipId: str
    documentType: str
    fileName: str
    fileData: str          # base64 data URL of the uploaded document
    verification: bool = True


# OCR doc_type codes accepted by the General-Document API (dropdown → payload value).
OCR_DOC_TYPES = {"passport", "pan_card", "aadhaar_card", "driving_licence", "voter_card"}


class NonMemberCreate(BaseModel):
    fullName: str
    mobile: str | None = None
    email: str | None = None
    country: str | None = None
    state: str | None = None
    location: str | None = None


class NonMemberUpdate(BaseModel):
    fullName: str | None = None
    mobile: str | None = None
    email: str | None = None
    country: str | None = None
    state: str | None = None
    location: str | None = None


@router.get("/member/{membership_id}")
async def kyc_member_lookup(
    membership_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Auto-fill the name for a Membership ID *or* a Non-Member ID within the caller's business
    pool. A Membership ID resolves to Member data exactly as it always has; an NM… id resolves to
    the stored Non-Member. 404 when the ID matches neither — the operator is then offered the
    'create a Non-Member' path.

    ``subjectType`` is additive: existing callers keep reading ``membershipId``/``memberName``.
    """
    mid, name = await _require_member_name(db, user, membership_id)
    subject_type = SUBJECT_NON_MEMBER if NON_MEMBER_RE.match(mid) else SUBJECT_MEMBER
    row = await _non_member_row(db, user, mid) if subject_type == SUBJECT_NON_MEMBER else None
    return {
        "membershipId": mid,
        "memberName": name,
        "subjectType": subject_type,
        "nonMember": _serialize_non_member(row) if row is not None else None,
    }


@router.post("/non-members")
async def create_non_member(
    body: NonMemberCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Register a walk-in who has no Membership ID and hand back a generated NM… id.

    The id is what the person presents on a return visit, so we refuse to mint a second one for
    a mobile/email already on file and point the operator at the existing record instead.
    """
    full_name = (body.fullName or "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="Full Name is required.")
    mobile = (body.mobile or "").strip() or None
    email = (body.email or "").strip() or None
    if mobile and not _MOBILE_RE.match(mobile):
        raise HTTPException(status_code=400, detail="Invalid Mobile Number — expected 10 digits.")
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid Email Address.")

    existing = await _find_duplicate_non_member(db, user, mobile=mobile, email=email)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"A Non-Member with these contact details already exists "
                    f"({existing.non_member_id} — {existing.full_name}). Search that ID instead."),
        )

    row = NonMemberKyc(
        non_member_id=await _next_non_member_id(db),
        full_name=full_name,
        mobile=mobile,
        email=email,
        country=(body.country or "").strip() or None,
        state=(body.state or "").strip() or None,
        location=(body.location or "").strip() or None,
        merchant_business=user.name,
        created_by=_actor_name(user),
        created_by_id=user.id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await record_audit(
        db, "NON_MEMBER_CREATE", actor=user, entity_type="non_member_kyc", entity_id=row.non_member_id,
        new=f"{row.non_member_id} — {row.full_name}",
        ip=request.client.host if request.client else None,
        actor_username=user.username,
        actor_role=str(user.merchant_role).upper() if user.merchant_role else None,
        business=user.name,
    )
    return _serialize_non_member(row)


@router.get("/non-members")
async def list_non_members(
    q: str | None = None,               # search: NM id / name / mobile / email
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Non-Members registered by the caller's merchant business, newest first — lets an operator
    find a returning walk-in who no longer has their NM id to hand."""
    stmt = select(NonMemberKyc).where(NonMemberKyc.merchant_business == user.name)
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(NonMemberKyc.non_member_id).like(term),
            func.lower(NonMemberKyc.full_name).like(term),
            func.lower(func.coalesce(NonMemberKyc.mobile, "")).like(term),
            func.lower(func.coalesce(NonMemberKyc.email, "")).like(term),
        ))
    rows = (await db.execute(stmt.order_by(NonMemberKyc.id.desc()).limit(200))).scalars().all()
    return [_serialize_non_member(r) for r in rows]


@router.get("/non-members/{nm_id}")
async def get_non_member(
    nm_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    row = await _non_member_row(db, user, normalize_member_id(nm_id) or "")
    if row is None:
        raise HTTPException(status_code=404, detail="Non-Member not found.")
    return _serialize_non_member(row)


@router.patch("/non-members/{nm_id}")
async def update_non_member(
    nm_id: str,
    body: NonMemberUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Correct a Non-Member's details. The NM id itself is immutable, and verified document
    numbers are set by the verification flows only — never edited by hand."""
    row = await _non_member_row(db, user, normalize_member_id(nm_id) or "")
    if row is None:
        raise HTTPException(status_code=404, detail="Non-Member not found.")

    before = f"{row.full_name} / {row.mobile or '—'} / {row.email or '—'}"
    if body.fullName is not None:
        full_name = body.fullName.strip()
        if not full_name:
            raise HTTPException(status_code=400, detail="Full Name is required.")
        row.full_name = full_name
    if body.mobile is not None:
        mobile = body.mobile.strip() or None
        if mobile and not _MOBILE_RE.match(mobile):
            raise HTTPException(status_code=400, detail="Invalid Mobile Number — expected 10 digits.")
        row.mobile = mobile
    if body.email is not None:
        email = body.email.strip() or None
        if email and not _EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid Email Address.")
        row.email = email
    for field, value in (("country", body.country), ("state", body.state), ("location", body.location)):
        if value is not None:
            setattr(row, field, value.strip() or None)

    dup = await _find_duplicate_non_member(db, user, mobile=row.mobile, email=row.email, exclude_id=row.id)
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail=f"These contact details belong to another Non-Member ({dup.non_member_id}).",
        )

    row.updated_by = _actor_name(user)
    row.updated_by_id = user.id
    row.updated_at = datetime.utcnow()
    db.add(row)
    await record_audit(
        db, "NON_MEMBER_UPDATE", actor=user, entity_type="non_member_kyc", entity_id=row.non_member_id,
        old=before, new=f"{row.full_name} / {row.mobile or '—'} / {row.email or '—'}",
        ip=request.client.host if request.client else None,
        actor_username=user.username,
        actor_role=str(user.merchant_role).upper() if user.merchant_role else None,
        business=user.name,
    )
    return _serialize_non_member(row)


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
        verification_method=METHOD_DIGILOCKER,
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
        await _capture_verified_doc(db, user, row.membership_id or "", "aadhaar_number", _result_aadhaar(data))
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


@router.post("/aadhaar/verify-image")
async def aadhaar_verify_image(
    body: AadhaarImageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Verify Aadhaar from an uploaded card image via the General-Document (OCR) API — an
    alternative to the DigiLocker flow. Always sends verification=true and doc_type=aadhaar_card
    (fixed, not user-editable). Recorded as an AADHAAR verification (Image Upload method)."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)
    b64 = _image_b64(body.image, "Aadhaar card image")

    reference_id = _gen_reference("AADHAAR")
    request_payload = {"reference_id": reference_id, "source": b64, "verification": True, "doc_type": "aadhaar_card"}
    data, http_status = await kyc_service.melento_ocr_verify(reference_id, b64, "aadhaar_card", True)

    status_val = str(data.get("status") or "").lower()
    ok = status_val == "success"
    error_message = None if ok else (data.get("message") or data.get("error") or "Aadhaar verification failed.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="AADHAAR",
        verification_method=METHOD_IMAGE,
        document_type="aadhaar_card",
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

    await _capture_verified_doc(db, user, mid, "aadhaar_number", _result_aadhaar(data))
    return {"id": row.id, "status": row.verification_status, "verified": bool(data.get("verified")), "raw": data}


@router.post("/pan/verify-membership")
async def pan_verify_membership(
    body: PanVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Verify a PAN for a member (by PAN number OR uploaded card image) and record it."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)

    reference_id = _gen_reference("PAN")
    if body.image:
        # Image Upload → source_type "base64", source is the raw base64 PAN-card image.
        source = _image_b64(body.image, "PAN card image")
        method, source_type = METHOD_IMAGE, "base64"
    else:
        source = (body.pan or "").upper().strip()
        if not PAN_RE.match(source):
            raise HTTPException(status_code=400, detail="Invalid PAN Number — expected format ABCDE1234F.")
        method, source_type = METHOD_ID, "id"

    request_payload = {"reference_id": reference_id, "source_type": source_type, "source": source}
    data, http_status = await kyc_service.melento_pan_verify(reference_id, source, source_type)

    status_val = str(data.get("status") or "").lower()
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    valid_pan = bool((result or {}).get("valid_pan"))
    ok = status_val == "success"
    error_message = None if ok else (data.get("message") or data.get("error") or "PAN verification failed.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="PAN",
        verification_method=method,
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

    # Keep a Non-Member's own record current (no-op for Members). The Image Upload method has no
    # operator-entered number, so fall back to whatever the provider echoed back.
    await _capture_verified_doc(
        db, user, mid, "pan_number",
        source if source_type == "id" else (result or {}).get("pan") or (result or {}).get("pan_number"),
    )
    return {"id": row.id, "status": row.verification_status, "validPan": valid_pan, "result": result, "raw": data}


@router.post("/passport/verify-membership")
async def passport_verify_membership(
    body: PassportVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Verify a passport for a member (by File Number OR front+back card images) and record it."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)

    reference_id = _gen_reference("PASSPORT")
    dob = (body.dateOfBirth or "").strip() or None
    if body.frontImage or body.backImage:
        # Image Upload → both pages are mandatory; source is [front_b64, back_b64].
        if not (body.frontImage and body.backImage):
            raise HTTPException(status_code=400, detail="Both the Front and Back passport images are required.")
        front = _image_b64(body.frontImage, "passport front image")
        back = _image_b64(body.backImage, "passport back image")
        source: str | list[str] = [front, back]
        method, source_type, dob = METHOD_IMAGE, "base64", None
        request_payload = {"reference_id": reference_id, "source_type": source_type, "source": source}
    else:
        source = (body.passportNumber or "").upper().strip()
        if not PASSPORT_RE.match(source):
            raise HTTPException(status_code=400, detail="Passport File Number is required and must be alphanumeric.")
        method, source_type = METHOD_ID, "id"
        request_payload = {"reference_id": reference_id, "source_type": source_type, "source": source}
        if dob:
            request_payload["dob"] = dob

    data, http_status = await kyc_service.melento_passport_verify(reference_id, source, dob, source_type)

    status_val = str(data.get("status") or "").lower()
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    valid_passport = bool((result or {}).get("valid_passport"))
    ok = status_val == "success"
    error_message = None if ok else (data.get("message") or data.get("error") or "Passport verification failed.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="PASSPORT",
        verification_method=method,
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

    # Keep a Non-Member's own record current (no-op for Members). ``source`` is the File Number
    # for the ID method and a list of images for the upload method, hence the source_type guard.
    await _capture_verified_doc(
        db, user, mid, "passport_number",
        source if source_type == "id" else (result or {}).get("passport_number"),
    )
    return {"id": row.id, "status": row.verification_status, "validPassport": valid_passport, "result": result, "raw": data}


@router.post("/ocr/verify-membership")
async def ocr_verify_membership(
    body: OcrVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Run General-Document (OCR) verification for a member and record the request/response."""
    mid, member_name = await _require_member_name(db, user, body.membershipId)

    doc_type = (body.documentType or "").strip().lower()
    if doc_type not in OCR_DOC_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported document type.")
    ext = body.fileName.rsplit(".", 1)[-1].lower() if "." in body.fileName else ""
    if ext not in OCR_ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file type — allowed: JPG, JPEG, PNG, PDF.")
    # base64 payload is ~4/3 of the raw byte size; guard against oversized uploads.
    approx_bytes = (len(body.fileData) * 3) // 4
    if approx_bytes > OCR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large — maximum size is 10 MB.")

    reference_id = _gen_reference("OCR")
    # The provider takes the raw base64 (strip any data-URL prefix). We persist the complete
    # request exactly as sent (the platform already stores uploads as DB data-URLs), so no
    # information is discarded; API logs mask the source (see kyc_service._mask_payload).
    b64 = body.fileData.split(",", 1)[-1] if body.fileData.startswith("data:") else body.fileData
    request_payload = {"reference_id": reference_id, "source": b64,
                       "verification": body.verification, "doc_type": doc_type}
    data, http_status = await kyc_service.melento_ocr_verify(reference_id, b64, doc_type, body.verification)

    status_val = str(data.get("status") or "").lower()
    ok = status_val == "success"
    error_message = None if ok else (data.get("message") or data.get("error") or "OCR verification failed.")

    row = KycVerificationHistory(
        membership_id=mid,
        member_name=member_name,
        verification_type="OCR",
        verification_method=METHOD_IMAGE,
        document_type=doc_type,
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

    return {"id": row.id, "status": row.verification_status, "verified": bool(data.get("verified")), "raw": data}


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
