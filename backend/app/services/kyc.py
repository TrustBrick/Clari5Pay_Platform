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

import httpx

from app.core.config import settings

# ── Melento.ai staging (UAT) live integration ─────────────────────────────────
# The Aadhaar (DigiLocker generate-link + poll) and PAN flows call the in-verify-utils
# host with api_key / api_id headers. These helpers each return (response_dict, http_status)
# and NEVER raise — network/timeout/parse failures are normalised into an error dict so the
# route can persist the exact outcome and surface a clean message to the UI.
_MELENTO_TIMEOUT = 30.0


def _melento_headers() -> dict:
    # The verify host is a Parse Server: the account's api_id is the Parse application id and
    # api_key is the Parse REST API key (verified against the staging endpoint).
    return {
        "x-parse-application-id": settings.MELENTO_API_ID,
        "x-parse-rest-api-key": settings.MELENTO_API_KEY,
    }


async def _melento_post(path: str, payload: dict) -> tuple[dict, int]:
    """POST to the Melento verify host. Returns (parsed_json_or_error, http_status).

    On a non-JSON body we wrap the raw text; on a transport error we return status 0 with an
    error dict. Callers store the returned dict verbatim as the response record.
    """
    url = f"{settings.MELENTO_VERIFY_BASE_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_MELENTO_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=_melento_headers())
    except httpx.HTTPError as exc:
        return {"status": "error", "error": f"Could not reach the verification service: {exc}"}, 0
    try:
        data = resp.json()
        if not isinstance(data, dict):
            data = {"status": "error", "error": "Unexpected response format", "raw": data}
    except ValueError:
        data = {"status": "error", "error": (resp.text or "Empty response").strip()[:1000]}
    return data, resp.status_code


async def melento_generate_aadhaar_url(reference_id: str) -> tuple[dict, int]:
    """Generate a DigiLocker Aadhaar verification link for a reference id."""
    return await _melento_post("/api/digilocker/generateUrl", {"reference_id": reference_id, "source": "AADHAAR"})


async def melento_get_aadhaar_details(reference_id: str, transaction_id: str | None) -> tuple[dict, int]:
    """Fetch the Aadhaar details once the customer has completed DigiLocker for this reference."""
    return await _melento_post(
        "/api/digilocker/getAadhaarDetails",
        {"reference_id": reference_id, "transaction_id": transaction_id},
    )


async def melento_pan_verify(reference_id: str, pan: str) -> tuple[dict, int]:
    """Verify a PAN number (source_type 'id')."""
    return await _melento_post(
        "/api/pan/panVerification",
        {"reference_id": reference_id, "source_type": "id", "source": pan},
    )


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


# ── DigiLocker (OAuth authorization → fetch the verified Aadhaar document) ──────
async def verify_via_digilocker() -> dict:
    """Verify a customer's Aadhaar through DigiLocker.

    The customer authenticates with DigiLocker (OAuth), then the verified Aadhaar document
    is retrieved — no manual Aadhaar number entry is needed. The returned shape matches the
    Aadhaar-number result so the UI can render both methods in one unified details card
    (plus a ``lastSynced`` timestamp DigiLocker provides).

    TODO(integration): once DIGILOCKER_CLIENT_ID / DIGILOCKER_CLIENT_SECRET are configured,
    run the DigiLocker OAuth flow against ``settings.DIGILOCKER_BASE_URL``, pull the Aadhaar
    document, and return it normalised to the Aadhaar result fields.
    """
    if not settings.digilocker_configured:
        raise KYCNotConfigured("DigiLocker")
    raise KYCNotConfigured("DigiLocker")
