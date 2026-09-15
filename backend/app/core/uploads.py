"""Centralised validation for base64 data-URL uploads (images / PDFs).

Every upload in the platform is a data URL embedded in a JSON body. This enforces a per-file
size limit and an allowed MIME-type whitelist, with clear error messages, so oversized or
unsupported files are rejected consistently across all endpoints. The nginx/Caddy request-body
cap is the outer backstop; this layer produces the user-facing error and bounds a single file.
"""
import base64
import binascii
import re

from fastapi import HTTPException

# Maximum decoded size of a single uploaded file.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

IMAGE_TYPES = ("data:image/jpeg", "data:image/jpg", "data:image/png", "data:image/webp")
IMAGE_PDF_TYPES = IMAGE_TYPES + ("data:application/pdf",)

_ALLOWED_MSG = {
    IMAGE_TYPES: "Allowed: JPG, JPEG, PNG, WEBP.",
    IMAGE_PDF_TYPES: "Allowed: JPG, JPEG, PNG, WEBP, PDF.",
}


def _decoded_len(data_url: str) -> int:
    """Approximate decoded byte length of a data URL's base64 payload (no full decode)."""
    i = data_url.find(",")
    b64 = data_url[i + 1:] if i != -1 else data_url
    pad = b64[-2:].count("=")
    return max(0, (len(b64) * 3) // 4 - pad)


def validate_upload(value: str | None, *, allowed: tuple = IMAGE_TYPES, label: str = "file",
                    max_bytes: int = MAX_UPLOAD_BYTES) -> str | None:
    """Validate one optional data-URL upload and return it unchanged.

    Empty values and non-data-URL strings (e.g. an existing http URL) pass through untouched.
    A `data:` URL must match the `allowed` MIME whitelist and stay within `max_bytes` (decoded),
    otherwise a 400 with a clear message is raised.
    """
    if not value:
        return value
    head = value[:64].lower()
    if head.startswith("data:"):
        if not head.startswith(allowed):
            raise HTTPException(status_code=400,
                                detail=f"Unsupported {label} type. {_ALLOWED_MSG.get(allowed, '')}".strip())
        if _decoded_len(value) > max_bytes:
            raise HTTPException(status_code=400,
                                detail=f"{label[:1].upper() + label[1:]} is too large. "
                                       f"Maximum size is {max_bytes // (1024 * 1024)} MB.")
    return value


# ── Strict image content check ─────────────────────────────────────────────────────────────────
# `validate_upload` trusts the data URL's declared MIME type and lets any non-data-URL string pass
# through. That is right for the transaction flows it serves, and it is left exactly as it is. An
# image that is configured ONCE and then shown to every merchant routed to an account deserves a
# stricter gate, so this adds three checks on top of it — it never replaces or loosens it:
#
#   * the value MUST be a base64 data URL. A plain http(s) URL or a ``storage://`` reference would
#     otherwise be stored as-is, and a stored reference is later presigned — accepting one from a
#     request would let a caller point an account at any object in the bucket;
#   * the base64 must decode strictly;
#   * the decoded bytes must actually BE the declared image type (magic bytes), so a renamed HTML
#     or PDF file cannot travel under an image/png label.
_IMAGE_MAGIC = {
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/jpg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP",
}


def validate_image_content(value: str | None, *, label: str = "image",
                           max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[str, str, bytes]:
    """Validate one REQUIRED image upload by its declared type AND its actual bytes.

    Returns ``(data_url, content_type, raw_bytes)``. Raises a 400 on anything that is not a
    JPG/JPEG/PNG/WEBP data URL within ``max_bytes`` whose content matches its declared type.
    """
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Select an {label} to upload.")
    if not value[:5].lower() == "data:":
        raise HTTPException(status_code=400,
                            detail=f"Unsupported {label}. Upload a JPG, JPEG, PNG or WEBP file.")
    # MIME whitelist + decoded size — the platform's standard rule, applied first and unchanged.
    validate_upload(value, allowed=IMAGE_TYPES, label=label, max_bytes=max_bytes)
    m = re.match(r"^data:([\w.+-]+/[\w.+-]+);base64,(.*)$", value, re.DOTALL | re.IGNORECASE)
    if not m:
        raise HTTPException(status_code=400, detail=f"The {label} could not be read.")
    content_type = m.group(1).lower()
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail=f"The {label} could not be read.")
    if not raw:
        raise HTTPException(status_code=400, detail=f"The {label} is empty.")
    if len(raw) > max_bytes:
        raise HTTPException(status_code=400,
                            detail=f"{label[:1].upper() + label[1:]} is too large. "
                                   f"Maximum size is {max_bytes // (1024 * 1024)} MB.")
    check = _IMAGE_MAGIC.get(content_type)
    if check is None or not check(raw):
        raise HTTPException(status_code=400,
                            detail=f"The {label} content does not match its file type. "
                                   f"Upload a genuine JPG, JPEG, PNG or WEBP image.")
    return value, content_type, raw
