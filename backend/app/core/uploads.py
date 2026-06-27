"""Centralised validation for base64 data-URL uploads (images / PDFs).

Every upload in the platform is a data URL embedded in a JSON body. This enforces a per-file
size limit and an allowed MIME-type whitelist, with clear error messages, so oversized or
unsupported files are rejected consistently across all endpoints. The nginx/Caddy request-body
cap is the outer backstop; this layer produces the user-facing error and bounds a single file.
"""
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
