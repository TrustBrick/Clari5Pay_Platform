"""KYC verification service layer — Melento.ai (Aadhaar / PAN / Passport / OCR) + DigiLocker.

This is the *integration seam*. The Merchant-portal KYC Update module (UI, client-side
and server-side validation, loading/error/success states, response models and routing) is
fully in place. The only thing intentionally left unimplemented is the outbound provider
HTTP call — until the Melento.ai and DigiLocker credentials are supplied (via env vars ONLY,
never hardcoded), each seam raises ``KYCNotConfigured`` and the API returns a graceful 503.

Wiring the real integration later means filling in ``_melento_verify`` and the DigiLocker
seams below; nothing in the routes, schemas, or frontend needs to change.
"""
from __future__ import annotations

from app.core.config import settings


class KYCNotConfigured(Exception):
    """Raised when a provider's credentials/integration are not yet available.

    Carries the human-readable provider name so the route can surface a clear message.
    """

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"{provider} is not configured yet")


# ── Melento.ai (Aadhaar / PAN / Passport / OCR) ────────────────────────────────
async def _melento_verify(endpoint: str, payload: dict) -> dict:
    """Single outbound seam for every Melento.ai verification call.

    TODO(integration): once MELENTO_API_ID / MELENTO_API_KEY are configured, POST
    ``payload`` to ``f"{settings.MELENTO_BASE_URL}/{endpoint}"`` with the provider's auth
    headers (e.g. x-api-id / x-api-key), then normalise and return the response dict.
    """
    if not settings.kyc_configured:
        raise KYCNotConfigured("Melento.ai KYC")
    # Credentials exist but the provider call is not wired yet.
    raise KYCNotConfigured("Melento.ai KYC")


async def verify_aadhaar(aadhaar_number: str) -> dict:
    return await _melento_verify("verify/aadhaar", {"aadhaar_number": aadhaar_number})


async def verify_pan(pan_number: str) -> dict:
    return await _melento_verify("verify/pan", {"pan_number": pan_number})


async def verify_passport(passport_number: str, date_of_birth: str | None = None) -> dict:
    return await _melento_verify(
        "verify/passport", {"passport_number": passport_number, "date_of_birth": date_of_birth}
    )


async def verify_ocr(document_type: str, file_name: str, file_data: str) -> dict:
    """OCR extraction from an uploaded identity document (base64 data URL)."""
    return await _melento_verify(
        "ocr/extract", {"document_type": document_type, "file_name": file_name, "file": file_data}
    )


# ── DigiLocker (OAuth authorization → fetch verified documents) ─────────────────
async def digilocker_connect(mobile: str | None, aadhaar: str | None) -> dict:
    """Begin a DigiLocker authorization session for the given customer identifier.

    TODO(integration): once DIGILOCKER_CLIENT_ID / DIGILOCKER_CLIENT_SECRET are configured,
    initiate the DigiLocker OAuth flow against ``settings.DIGILOCKER_BASE_URL`` and return
    the authorization URL / session handle.
    """
    if not settings.digilocker_configured:
        raise KYCNotConfigured("DigiLocker")
    raise KYCNotConfigured("DigiLocker")


async def digilocker_documents(session_id: str) -> dict:
    """List the verified documents available for an authorized DigiLocker session."""
    if not settings.digilocker_configured:
        raise KYCNotConfigured("DigiLocker")
    raise KYCNotConfigured("DigiLocker")
