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

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("app.kyc")

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


def _mask_one(src: str) -> str:
    return (src[:4] + "…") if len(src) <= 32 else f"<{len(src)} chars>"


def _mask_payload(payload: dict) -> dict:
    """Redact the sensitive source (Aadhaar/PAN/passport number or base64 document(s)) for logs —
    credentials (api_id/api_key) live in headers and are never logged. ``source`` may be a string
    (id / single base64) or a list of base64 images (passport front/back)."""
    out = dict(payload)
    src = out.get("source")
    if isinstance(src, str) and src:
        out["source"] = _mask_one(src)
    elif isinstance(src, list):
        out["source"] = [_mask_one(s) if isinstance(s, str) else s for s in src]
    return out


async def _melento_post(path_or_url: str, payload: dict) -> tuple[dict, int]:
    """POST to the Melento verify host. Returns (parsed_json_or_error, http_status).

    ``path_or_url`` may be a path (prefixed with ``MELENTO_VERIFY_BASE_URL``) or an absolute URL
    (used verbatim). On a non-JSON body we wrap the raw text; on a transport error we return
    status 0 with an error dict. Every request/response is logged with the sensitive source
    masked and credentials never emitted. A single retry is made on timeout. Callers store the
    returned dict verbatim as the response record.
    """
    url = path_or_url if path_or_url.startswith("http") else f"{settings.MELENTO_VERIFY_BASE_URL.rstrip('/')}{path_or_url}"
    ref = payload.get("reference_id")
    logger.info("KYC → POST %s ref=%s payload=%s", url, ref, _mask_payload(payload))
    last_exc: httpx.HTTPError | None = None
    for attempt in (1, 2):  # one retry, only on timeout
        try:
            async with httpx.AsyncClient(timeout=_MELENTO_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=_melento_headers())
            break
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("KYC ⏱ timeout on %s ref=%s (attempt %d/2)", url, ref, attempt)
            continue
        except httpx.HTTPError as exc:
            logger.error("KYC ✗ transport error on %s ref=%s: %s", url, ref, exc)
            return {"status": "error", "error": f"Could not reach the verification service: {exc}"}, 0
    else:
        return {"status": "error", "error": f"Verification service timed out: {last_exc}"}, 0
    try:
        data = resp.json()
        if not isinstance(data, dict):
            data = {"status": "error", "error": "Unexpected response format", "raw": data}
    except ValueError:
        data = {"status": "error", "error": (resp.text or "Empty response").strip()[:1000]}
    logger.info("KYC ← %s ref=%s http=%s status=%s", url, ref, resp.status_code, data.get("status"))
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


async def melento_pan_verify(reference_id: str, source: str, source_type: str = "id") -> tuple[dict, int]:
    """Verify a PAN. ``source_type`` 'id' → ``source`` is the PAN number; 'base64' → ``source`` is
    a base64 PAN-card image. Same endpoint/headers for both methods."""
    return await _melento_post(
        settings.PAN_VERIFICATION_URL,
        {"reference_id": reference_id, "source_type": source_type, "source": source},
    )


async def melento_passport_verify(
    reference_id: str, source: str | list[str], dob: str | None = None, source_type: str = "id"
) -> tuple[dict, int]:
    """Verify a passport. ``source_type`` 'id' → ``source`` is the Passport File Number (``dob``
    YYYY-MM-DD optional); 'base64' → ``source`` is ``[front_b64, back_b64]``. Same endpoint/headers."""
    payload: dict = {"reference_id": reference_id, "source_type": source_type, "source": source}
    if dob:
        payload["dob"] = dob
    return await _melento_post(settings.PASSPORT_VERIFICATION_URL, payload)


async def melento_ocr_verify(
    reference_id: str, source_b64: str, doc_type: str, verification: bool
) -> tuple[dict, int]:
    """General-document OCR verification: a base64 document + doc_type, verification toggle."""
    return await _melento_post(
        settings.OCR_VERIFICATION_URL,
        {"reference_id": reference_id, "source": source_b64, "verification": verification, "doc_type": doc_type},
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


# Passport and General-Document (OCR) verification are live, membership-driven flows — see
# ``melento_passport_verify`` / ``melento_ocr_verify`` above (mirroring the PAN integration).


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
