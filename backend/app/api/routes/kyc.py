"""Merchant KYC Update module — Aadhaar / PAN / Passport / OCR / DigiLocker verification.

Access is restricted to MERCHANT users with a Supervisor or Manager role (enforced by
``get_current_kyc_user``). Every endpoint validates its input server-side, then delegates
to the ``app.services.kyc`` seam. Until the Melento.ai / DigiLocker credentials are supplied
via env, the seam raises ``KYCNotConfigured`` and we return a clear 503 — the UI handles it
gracefully. No existing schema, route, or data is touched by this module.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_kyc_user
from app.models.models import User
from app.services import kyc as kyc_service

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


class DigiLockerConnectRequest(BaseModel):
    mobile: str | None = None
    aadhaar: str | None = None


class DigiLockerDocumentsRequest(BaseModel):
    sessionId: str


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


@router.post("/digilocker/connect")
async def digilocker_connect(body: DigiLockerConnectRequest, _: User = Depends(get_current_kyc_user)):
    mobile = (body.mobile or "").strip()
    aadhaar = (body.aadhaar or "").replace(" ", "").strip()
    if not mobile and not aadhaar:
        raise HTTPException(status_code=400, detail="Enter the customer's mobile number or Aadhaar number to continue.")
    if mobile and not re.match(r"^\d{10}$", mobile):
        raise HTTPException(status_code=400, detail="Invalid mobile number — must be 10 digits.")
    if aadhaar and not AADHAAR_RE.match(aadhaar):
        raise HTTPException(status_code=400, detail="Invalid Aadhaar Number — must be exactly 12 digits.")
    try:
        return await kyc_service.digilocker_connect(mobile or None, aadhaar or None)
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)


@router.post("/digilocker/documents")
async def digilocker_documents(body: DigiLockerDocumentsRequest, _: User = Depends(get_current_kyc_user)):
    if not body.sessionId.strip():
        raise HTTPException(status_code=400, detail="Missing DigiLocker session.")
    try:
        return await kyc_service.digilocker_documents(body.sessionId.strip())
    except kyc_service.KYCNotConfigured as exc:
        raise _unavailable(exc)
