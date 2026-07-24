"""Merchant KYC Update module — Aadhaar / PAN / Passport / OCR / DigiLocker verification.

Access is restricted to MERCHANT users with a Data Operator, Supervisor or Manager role
(enforced by ``get_current_kyc_user``). Within the module the roles differ: the Data Operator
RUNS verifications (``get_current_kyc_verifier``), while Supervisor and Manager are read-only —
they see the Verification History and each record's details, nothing more. Every endpoint
validates its input server-side, then delegates to the ``app.services.kyc`` seam. Until the
Melento.ai / DigiLocker credentials are supplied via env, the seam raises ``KYCNotConfigured``
and we return a clear 503 — the UI handles it gracefully. No existing schema, route, or data is
touched by this module.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import random
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_kyc_user, get_current_kyc_verifier
from app.db.session import get_db
from app.models.models import KycVerificationHistory, User
from app.services import kyc as kyc_service
from app.services.membership import lookup_member_name, normalize_member_id
from app.services.name_match import score_and_status

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
async def aadhaar_verify(body: AadhaarRequest, _: User = Depends(get_current_kyc_verifier)):
    number = body.aadhaarNumber.replace(" ", "").strip()
    if not AADHAAR_RE.match(number):
        raise HTTPException(status_code=400, detail="Invalid Aadhaar Number — must be exactly 12 digits.")
    try:
        return await kyc_service.verify_aadhaar(number)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/pan/verify")
async def pan_verify(body: PanRequest, _: User = Depends(get_current_kyc_verifier)):
    number = body.panNumber.upper().strip()
    if not PAN_RE.match(number):
        raise HTTPException(status_code=400, detail="Invalid PAN Number — expected format ABCDE1234F.")
    try:
        return await kyc_service.verify_pan(number)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/digilocker/verify")
async def digilocker_verify(_: User = Depends(get_current_kyc_verifier)):
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


async def _gen_reference(db: AsyncSession, prefix: str) -> str:
    """Sequential reference id per verification type: ``PAN000001``, ``AADHAAR000001``,
    ``PASSPORT000001``, … incrementing by one on each verification.

    Backed by a dedicated Postgres sequence per prefix (created on first use, idempotent). The
    sequence only ever advances — even across a data reset — so every reference id stays unique,
    which the provider requires (it rejects a reused reference id).
    """
    seq = f"kyc_{prefix.lower()}_ref_seq"
    await db.execute(text(f'CREATE SEQUENCE IF NOT EXISTS "{seq}" START WITH 1'))
    n = (await db.execute(text(f"SELECT nextval('{seq}')"))).scalar()
    return f"{prefix}{int(n):06d}"


# ── Verification-status labels ────────────────────────────────────────────────
# Codes the "Status" column renders (the UI maps each to its human label). They are DERIVED at
# read time from what is already stored — nothing new is persisted and no existing row is
# rewritten, so history stays exactly as the provider reported it.
STATUS_SUCCESS_MATCHED = "SUCCESS_MATCHED"          # "Success – Matched"
STATUS_SUCCESS_NOT_MATCHED = "SUCCESS_NOT_MATCHED"  # "Success – Not Matched"
STATUS_FAILED_NOT_EXIST = "FAILED_NOT_EXIST"        # "Failed – Doesn't Exist"

# Image-upload (OCR) verifications count as matched only at a PERFECT name match.
OCR_MATCH_THRESHOLD = 100


def _display_status(row: KycVerificationHistory) -> str | None:
    """The status shown in the Verification History "Status" column.

    Keyed off the verification METHOD (not the document type):
      * Image Upload / OCR — the name is read from the document, so the match matters:
          success + match score == 100 → Success – Matched
          success + score  < 100       → Success – Not Matched
      * ID Number / DigiLocker — a retrieved record is always Success – Not Matched (the match
          percentage is deliberately NOT applied to these).
      * Any FAILED verification (the id / document does not exist) → Failed – Doesn't Exist.
      * A DigiLocker Aadhaar still awaiting completion → Pending.
    """
    vstatus = str(row.verification_status or "")
    if vstatus == "PENDING":
        return "PENDING"                       # DigiLocker link generated, not yet completed
    if vstatus != "SUCCESS":
        return STATUS_FAILED_NOT_EXIST         # FAILED → the id / document does not exist

    if str(row.verification_method or "") == METHOD_IMAGE:
        return (STATUS_SUCCESS_MATCHED if (row.match_score or 0) >= OCR_MATCH_THRESHOLD
                else STATUS_SUCCESS_NOT_MATCHED)
    return STATUS_SUCCESS_NOT_MATCHED          # ID Number / DigiLocker


def _history_summary(row: KycVerificationHistory) -> dict:
    """List-shape row (omits the large request/response JSON blobs).

    The "Status" column shows the derived display status (see ``_display_status``): the name-match
    status for Aadhaar, and the PAN / Passport / OCR labels for the rest. The numeric match_score
    is intentionally NOT exposed — only the status is shown.
    """
    return {
        "id": row.id,
        "membershipId": row.membership_id,
        "memberName": row.member_name,
        "verificationType": row.verification_type,
        "verificationMethod": row.verification_method,
        "documentType": row.document_type,
        "referenceId": row.reference_id,
        "transactionId": row.transaction_id,
        "status": _display_status(row),
        "createdBy": row.created_by,
        "createdAt": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


# ── Name matching ─────────────────────────────────────────────────────────────
# Which response keys carry the document holder's name (vs. a relative/authority), searched across
# the provider's varied shapes (top-level, result, extracted_data, validated_data, …).
_NAME_KEYS = ("name", "full_name", "fullname", "name_on_card", "holder_name",
              "candidate_name", "person_name", "given_name")
_NAME_BLOCK = ("father", "mother", "guardian", "spouse", "care_of", "careof",
               "issuing", "authority", "bank", "relation")


def _iter_name_candidates(node, depth: int = 0):
    """(priority, name) pairs found anywhere in a verification response; lower priority is better."""
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            kl = str(key).lower()
            if isinstance(value, str) and value.strip() and not any(b in kl for b in _NAME_BLOCK):
                if kl in _NAME_KEYS:
                    yield (_NAME_KEYS.index(kl), value.strip())
            elif isinstance(value, (dict, list)):
                yield from _iter_name_candidates(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_name_candidates(value, depth + 1)


def _extract_kyc_name(response) -> str | None:
    """The document holder's official name from a KYC verification response, or None."""
    candidates = sorted(_iter_name_candidates(response), key=lambda c: c[0])
    return candidates[0][1] if candidates else None


def _apply_name_match(row: KycVerificationHistory, response, member_name: str | None) -> None:
    """Compute + store the name-match score/status when the response carries an official name.

    The score/status compare the member's registered name (``member_name``) with the official KYC
    name. Leaves the row untouched (status falls back to the API state) when no name is returned.
    """
    kyc_name = _extract_kyc_name(response)
    if kyc_name and member_name:
        row.match_score, row.match_status = score_and_status(member_name, kyc_name)


def _image_b64(data_url: str, field: str = "image") -> str:
    """Validate an uploaded image is PNG/JPG/JPEG and within the size limit, then return the raw
    base64 (data-URL prefix stripped) that Melento expects as ``source``."""
    if not data_url or not IMAGE_DATA_URL_RE.match(data_url):
        raise HTTPException(status_code=400, detail=f"Unsupported {field} — allowed types: PNG, JPG, JPEG.")
    b64 = data_url.split(",", 1)[-1]
    if (len(b64) * 3) // 4 > OCR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image too large — maximum size is 10 MB.")
    return b64


# ── Aadhaar XML photograph ────────────────────────────────────────────────────
# An Aadhaar response carries an XML document that embeds the cardholder photo as base64 — `<Pht>`
# in the UIDAI offline-KYC XML, `<Photo>` in a DigiLocker certificate — and the XML may itself be
# base64-wrapped. The photo is derived at READ time and nothing derived is ever persisted, so an
# unparseable or photo-less response simply yields None and the popup omits the image.
_XML_HINT = re.compile(r"<\?xml|<OfflinePaperlessKyc|<Certificate|<PrintLetterBarcodeData|<UidData", re.IGNORECASE)
_PHOTO_TAGS = {"pht", "photo", "image"}
_MAX_XML_BYTES = 5 * 1024 * 1024


def _iter_strings(node, depth: int = 0):
    """Every string in a nested response, so the XML is found whatever key it hides under."""
    if depth > 6:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_strings(value, depth + 1)


def _photo_from_xml(xml_text: str) -> str | None:
    """Base64 JPEG out of an Aadhaar XML document, as a data URL. None if absent or invalid."""
    if len(xml_text) > _MAX_XML_BYTES:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None                       # invalid XML → derive nothing
    for el in root.iter():
        tag = str(el.tag).rsplit("}", 1)[-1].lower()      # drop any namespace
        text = (el.text or "").strip()
        if tag in _PHOTO_TAGS and text:
            b64 = re.sub(r"\s+", "", text)
            try:
                base64.b64decode(b64, validate=True)      # only expose a decodable image
            except (binascii.Error, ValueError):
                continue
            return f"data:image/jpeg;base64,{b64}"
    return None


def _maybe_b64_xml(text: str) -> str | None:
    """Decode a base64-wrapped XML payload, if that's what this string is."""
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 64 or len(stripped) > _MAX_XML_BYTES or not re.fullmatch(r"[A-Za-z0-9+/=]+", stripped):
        return None
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return None
    return decoded if _XML_HINT.search(decoded) else None


def _xml_url(response) -> str | None:
    """The Aadhaar XML download link, if the response carries one instead of inline XML.

    Melento returns ``result.validated_data.result.xml_file`` as a PRESIGNED S3 URL to a .xml
    document — the XML is NOT inline. Restricted to https to keep this from being pointed at
    anything but the provider's file store.
    """
    for text in _iter_strings(response):
        s = text.strip()
        if s.startswith("https://") and ".xml" in s.split("?", 1)[0].lower():
            return s
    return None


async def _fetch_xml(url: str) -> str | None:
    """Download the Aadhaar XML. Returns None on any failure — an expired link (the presigned URL
    lives only 48h), a network error, or an over-sized body — so the caller simply gets no photo."""
    def _get() -> str | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "clari5pay"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if int(r.headers.get("Content-Length") or 0) > _MAX_XML_BYTES:
                    return None
                return r.read(_MAX_XML_BYTES).decode("utf-8", "ignore")
        except Exception:
            return None
    return await asyncio.to_thread(_get)


def _graphic_portrait(response) -> str | None:
    """The portrait crop an OCR (image-upload) Aadhaar response returns in ``graphic_fields``.

    The DigiLocker flow carries the photo in its XML; the image-upload flow instead returns it
    here. Supporting both means a photo is captured whichever route verified the member.
    """
    if not isinstance(response, dict):
        return None
    for holder in (response, response.get("result") if isinstance(response.get("result"), dict) else {}):
        graphic = (holder or {}).get("graphic_fields")
        if not isinstance(graphic, dict):
            continue
        for key in ("portrait", "photo", "face", "image"):
            raw = graphic.get(key)
            if isinstance(raw, str) and len(raw) > 100:
                b64 = re.sub(r"\s+", "", raw)
                if b64.startswith("data:image"):
                    return b64
                try:
                    base64.b64decode(b64, validate=True)
                except (binascii.Error, ValueError):
                    continue
                return f"data:image/jpeg;base64,{b64}"
    return None


async def _aadhaar_photo(response) -> str | None:
    """The cardholder photograph for an Aadhaar response, as a JPEG data URL.

    Covers every shape the provider may use: the OCR route's ``graphic_fields.portrait``, inline
    XML, base64-wrapped XML, and — what DigiLocker actually returns — an ``xml_file`` link that
    must be downloaded. Any failure yields None rather than raising, so a photo-less or expired
    verification degrades quietly.
    """
    if not isinstance(response, (dict, list)):
        return None
    portrait = _graphic_portrait(response)          # 0. OCR / image-upload route
    if portrait:
        return portrait
    texts = list(_iter_strings(response))
    for text in texts:                              # 1. XML present as-is
        if _XML_HINT.search(text):
            photo = _photo_from_xml(text)
            if photo:
                return photo
    for text in texts:                              # 2. XML wrapped in base64
        decoded = _maybe_b64_xml(text)
        if decoded:
            photo = _photo_from_xml(decoded)
            if photo:
                return photo
    url = _xml_url(response)                        # 3. XML behind a (short-lived) link
    if url:
        xml = await _fetch_xml(url)
        if xml:
            return _photo_from_xml(xml)
    return None


# ── Membership ID: the primary reference for every KYC lookup ─────────────────
# An id the merchant transaction records already know resolves to its authoritative Member Name
# (app.services.membership — the same capture rule the deposit/withdrawal/settlement flows use).
# An id they do NOT know is still accepted: the operator types the name once, the verification
# persists id + name onto its own kyc_verification_history row, and every later lookup for that id
# auto-fills from there. No second "member" record is ever created, so there is nothing to
# duplicate — the history IS the register, keyed by membership_id.
NAME_REQUIRED_MSG = "Member Name is required for a Membership ID that is not yet on record."


async def _member_aadhaar_photo(db: AsyncSession, user: User, mid: str) -> str | None:
    """The Aadhaar photograph stored for a Membership ID by any successful verification.

    The photo belongs to the member, not to a single attempt, so once ANY Aadhaar verification for
    this id has captured one, every KYC record for that id can show it. Scoped to the caller's
    business pool, like every other lookup here.
    """
    return (await db.execute(
        select(KycVerificationHistory.aadhaar_photo).where(
            KycVerificationHistory.merchant_business == user.name,
            KycVerificationHistory.membership_id == mid,
            KycVerificationHistory.aadhaar_photo.is_not(None),
        ).order_by(KycVerificationHistory.id.desc()).limit(1)
    )).scalars().first()


async def _kyc_registered_name(db: AsyncSession, user: User, mid: str) -> str | None:
    """Latest Member Name a previous KYC verification captured for this Membership ID."""
    return (await db.execute(
        select(KycVerificationHistory.member_name).where(
            KycVerificationHistory.merchant_business == user.name,
            KycVerificationHistory.membership_id == mid,
            KycVerificationHistory.member_name.is_not(None),
            KycVerificationHistory.member_name != "",
        ).order_by(KycVerificationHistory.id.desc()).limit(1)
    )).scalars().first()


async def _on_record_name(db: AsyncSession, user: User, mid: str) -> str | None:
    """The name on record for a Membership ID — merchant membership pool first, then KYC's own
    register. None means the id has never been seen and the operator may name it by hand."""
    return await lookup_member_name(db, user, mid) or await _kyc_registered_name(db, user, mid)


async def _resolve_subject(db: AsyncSession, user: User, membership_id: str,
                           member_name: str | None = None) -> tuple[str, str]:
    """(normalized Membership ID, Member Name) for a verification.

    Existing id -> the on-record name wins (authoritative; an entered name is ignored rather than
    allowed to fork the membership). New id -> the entered name, which this verification then
    stores, making the id auto-fill from then on.
    """
    mid = normalize_member_id(membership_id)
    if not mid:
        raise HTTPException(status_code=400, detail="Membership ID is required.")
    existing = await _on_record_name(db, user, mid)
    if existing:
        return mid, existing
    entered = (member_name or "").strip()
    if not entered:
        raise HTTPException(status_code=400, detail=NAME_REQUIRED_MSG)
    return mid, entered


class MembershipRequest(BaseModel):
    membershipId: str
    memberName: str | None = None       # used only when the Membership ID is not yet on record


class AadhaarStatusRequest(BaseModel):
    historyId: int


class PanVerifyRequest(BaseModel):
    membershipId: str
    memberName: str | None = None   # used only when the Membership ID is not yet on record
    pan: str | None = None          # ID Number method
    image: str | None = None        # Image Upload method (base64 data URL of the PAN card)


class PassportVerifyRequest(BaseModel):
    membershipId: str
    memberName: str | None = None       # used only when the Membership ID is not yet on record
    passportNumber: str | None = None   # ID Number (File Number) method
    dateOfBirth: str | None = None
    frontImage: str | None = None       # Image Upload method — front page (base64 data URL)
    backImage: str | None = None        # Image Upload method — back page (base64 data URL)


class AadhaarImageRequest(BaseModel):
    membershipId: str
    memberName: str | None = None       # used only when the Membership ID is not yet on record
    image: str                          # base64 data URL of the Aadhaar card


class OcrVerifyRequest(BaseModel):
    membershipId: str
    memberName: str | None = None       # used only when the Membership ID is not yet on record
    documentType: str
    fileName: str
    fileData: str          # base64 data URL of the uploaded document
    verification: bool = True


# OCR doc_type codes accepted by the General-Document API (dropdown → payload value).
OCR_DOC_TYPES = {"passport", "pan_card", "aadhaar_card", "driving_licence", "voter_card"}


@router.get("/member/{membership_id}")
async def kyc_member_lookup(
    membership_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Look up a Membership ID within the caller's business pool.

    Deliberately never 404s. A known id returns its authoritative Member Name so the page can
    auto-fill it; an unknown id returns ``exists: false`` and the operator names it by hand — the
    verification then persists id + name, and the next lookup for that id auto-fills. Prior KYC
    records for the id are NOT returned — the operator starts each verification fresh.
    """
    mid = normalize_member_id(membership_id)
    if not mid:
        raise HTTPException(status_code=400, detail="Membership ID is required.")
    name = await _on_record_name(db, user, mid)
    return {
        "membershipId": mid,
        "memberName": name,
        "exists": bool(name),
    }


@router.post("/aadhaar/generate-link")
async def aadhaar_generate_link(
    body: MembershipRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_verifier),
):
    """Generate a DigiLocker Aadhaar verification link for a member and record the attempt."""
    mid, member_name = await _resolve_subject(db, user, body.membershipId, body.memberName)
    reference_id = await _gen_reference(db, "AADHAAR")
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
        # Capture the cardholder photo NOW: the response's xml_file link is presigned and expires
        # after 48h, so this is the only moment it can be downloaded.
        row.aadhaar_photo = await _aadhaar_photo(data)
        # Name match: compare the member's registered name with the official Aadhaar name.
        _apply_name_match(row, data, row.member_name)
        db.add(row)
        return {"pending": False, "status": "SUCCESS", "details": data, "photo": row.aadhaar_photo,
                "referenceId": row.reference_id, "matchScore": row.match_score}

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
    return {"pending": False, "status": "FAILED", "error": row.error_message, "details": data,
            "referenceId": row.reference_id, "matchScore": None}


@router.post("/aadhaar/verify-image")
async def aadhaar_verify_image(
    body: AadhaarImageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_verifier),
):
    """Verify Aadhaar from an uploaded card image via the General-Document (OCR) API — an
    alternative to the DigiLocker flow. Always sends verification=true and doc_type=aadhaar_card
    (fixed, not user-editable). Recorded as an AADHAAR verification (Image Upload method)."""
    mid, member_name = await _resolve_subject(db, user, body.membershipId, body.memberName)
    b64 = _image_b64(body.image, "Aadhaar card image")

    reference_id = await _gen_reference(db, "AADHAAR")
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
    # Capture the photograph on a successful verification so it is permanently linked to this
    # Membership ID. This route returns it in graphic_fields.portrait (the DigiLocker route
    # carries it inside the XML instead) — _aadhaar_photo handles both shapes.
    if ok:
        row.aadhaar_photo = await _aadhaar_photo(data)
    db.add(row)
    await db.flush()
    await db.refresh(row)

    if not ok:
        # Persist the FAILED record before raising (get_db rolls back on exception). The detail is
        # a structured object so the browser can log the reference id even on a failed attempt
        # (see the debug console logging in KYCPage) — the human message is preserved under
        # `message`, which kycErrorMessage still surfaces unchanged.
        await db.commit()
        raise HTTPException(status_code=502, detail={
            "message": error_message, "referenceId": row.reference_id, "matchScore": None,
        })

    # Name match: compare the member's registered name with the official name extracted from the doc.
    _apply_name_match(row, data, member_name)
    return {"id": row.id, "status": row.verification_status, "verified": bool(data.get("verified")),
            "referenceId": row.reference_id, "matchScore": row.match_score, "raw": data}


@router.post("/pan/verify-membership")
async def pan_verify_membership(
    body: PanVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_verifier),
):
    """Verify a PAN for a member (by PAN number OR uploaded card image) and record it."""
    mid, member_name = await _resolve_subject(db, user, body.membershipId, body.memberName)

    reference_id = await _gen_reference(db, "PAN")
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
        # Persist the FAILED record before raising (get_db rolls back on exception). Structured
        # detail carries the reference id for the browser's debug console log; `message` is the
        # unchanged human error kycErrorMessage surfaces.
        await db.commit()
        raise HTTPException(status_code=502, detail={
            "message": error_message, "referenceId": row.reference_id, "matchScore": None,
        })

    # Name match: compare the member's registered name with the official PAN name.
    _apply_name_match(row, data, member_name)
    return {"id": row.id, "status": row.verification_status, "validPan": valid_pan,
            "referenceId": row.reference_id, "matchScore": row.match_score, "result": result, "raw": data}


@router.post("/passport/verify-membership")
async def passport_verify_membership(
    body: PassportVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_verifier),
):
    """Verify a passport for a member (by File Number OR front+back card images) and record it."""
    mid, member_name = await _resolve_subject(db, user, body.membershipId, body.memberName)

    reference_id = await _gen_reference(db, "PASSPORT")
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
        # Persist the FAILED record before raising (get_db rolls back on exception). Structured
        # detail carries the reference id for the browser's debug console log; `message` is the
        # unchanged human error kycErrorMessage surfaces.
        await db.commit()
        raise HTTPException(status_code=502, detail={
            "message": error_message, "referenceId": row.reference_id, "matchScore": None,
        })

    # Name match: compare the member's registered name with the official passport name.
    _apply_name_match(row, data, member_name)
    return {"id": row.id, "status": row.verification_status, "validPassport": valid_passport,
            "referenceId": row.reference_id, "matchScore": row.match_score, "result": result, "raw": data}


@router.post("/ocr/verify-membership")
async def ocr_verify_membership(
    body: OcrVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_verifier),
):
    """Run General-Document (OCR) verification for a member and record the request/response."""
    mid, member_name = await _resolve_subject(db, user, body.membershipId, body.memberName)

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

    reference_id = await _gen_reference(db, "OCR")
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
        # Persist the FAILED record before raising (get_db rolls back on exception). Structured
        # detail carries the reference id for the browser's debug console log; `message` is the
        # unchanged human error kycErrorMessage surfaces.
        await db.commit()
        raise HTTPException(status_code=502, detail={
            "message": error_message, "referenceId": row.reference_id, "matchScore": None,
        })

    # Name match: compare the member's registered name with the official name extracted from the doc.
    _apply_name_match(row, data, member_name)
    return {"id": row.id, "status": row.verification_status, "verified": bool(data.get("verified")),
            "referenceId": row.reference_id, "matchScore": row.match_score, "raw": data}


# ─── Server-side pagination (Verification History) ────────────────────────────
# Same envelope as the transaction / agent-txn paged feeds: {items, total, page, pageSize,
# totalPages}. Default 10 rows, sizes restricted to 10/25/50/100. The COUNT, the ORDER BY and
# the LIMIT/OFFSET all run in Postgres over the full history, so the browser only ever receives
# the rows it is about to draw.
_PAGE_SIZES = (10, 25, 50, 100)


@router.get("/stats")
async def kyc_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """Headline counter for the KYC dashboard's summary card.

    ``totalCompleted`` is the number of verifications that actually COMPLETED for the caller's
    business pool — i.e. the provider returned a result. Attempts still awaiting DigiLocker
    (PENDING) and attempts the provider rejected (FAILED) are not completed KYCs and are excluded.
    Same tenancy predicate as the history feed, so the card can never count another merchant's rows.
    """
    total_completed = int((await db.execute(
        select(func.count()).select_from(KycVerificationHistory).where(
            KycVerificationHistory.merchant_business == user.name,
            KycVerificationHistory.verification_status == "SUCCESS",
        )
    )).scalar() or 0)
    return {"totalCompleted": total_completed}


@router.get("/history")
async def kyc_history(
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_kyc_user),
):
    """One page of KYC verifications for the caller's merchant business pool, newest first.

    Sorting is unchanged (newest verification first, by descending id) and stays server-side.
    An out-of-range `page` yields an empty `items` with a truthful `total`, which lets the client
    step back onto the last real page.
    """
    page_size = page_size if page_size in _PAGE_SIZES else 10
    page = page if page >= 1 else 1

    # Same tenancy predicate as before — pagination never widens what the caller can see.
    base = select(KycVerificationHistory).where(
        KycVerificationHistory.merchant_business == user.name
    )
    total = int((await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar() or 0)
    rows = (await db.execute(
        base.order_by(KycVerificationHistory.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
    )).scalars().all()
    return {
        "items": [_history_summary(r) for r in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": (total + page_size - 1) // page_size if page_size else 0,
    }


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

    response = _parse(row.response_json)
    # The photograph is captured at verification time and read back from the STORED record — the
    # verification API is never called again just to show it. Resolution order:
    #   1. this record's own stored photo;
    #   2. a back-fill parse of its stored response (only works while the provider's xml_file link
    #      is alive — 48h — so this rescues a record verified just before capture existed);
    #   3. the photo stored for the SAME Membership ID by any other successful Aadhaar
    #      verification, since the photo belongs to the member, not to one attempt.
    photo = row.aadhaar_photo
    if not photo and row.verification_type == "AADHAAR" and row.verification_status == "SUCCESS":
        photo = await _aadhaar_photo(response)
        if photo:
            row.aadhaar_photo = photo
            db.add(row)
            await db.commit()
    if not photo and row.verification_type == "AADHAAR" and row.membership_id:
        photo = await _member_aadhaar_photo(db, user, row.membership_id)

    return {
        **_history_summary(row),
        "generatedLink": row.generated_link,
        "apiStatus": row.api_status,
        "errorMessage": row.error_message,
        "request": _parse(row.request_json),
        "response": response,
        # Aadhaar cardholder photo (data URL), or None when absent / the link had expired.
        "aadhaarPhoto": photo,
        "updatedAt": (row.updated_at.isoformat() + "Z") if row.updated_at else None,
    }
