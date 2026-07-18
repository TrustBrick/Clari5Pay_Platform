"""Object storage for uploaded files — the durable home for proof/bank images.

Historically every upload was kept in the database as a base64 data URL. That put megabytes
into the row itself (see S3_IMAGE_MIGRATION.md): a single `transactions` row averaged ~2 MB,
the table reached 162 MB across 78 rows, and any query that touched those columns dragged the
whole corpus across the wire. This module moves the bytes to object storage and leaves the
database holding a short key.

Two backends, chosen by ``settings.STORAGE_BACKEND``:

* ``"db"``   — no object storage; callers keep the data URL exactly as before. This is the
               DEFAULT, so importing or deploying this module changes nothing until an
               operator explicitly opts in. Every function below is a no-op in this mode.
* ``"s3"``   — uploads to the configured bucket and returns a key.

Keys are CONTENT-ADDRESSED: the object name embeds a SHA-256 of the bytes, so storing the same
image twice produces the same key and overwrites itself rather than duplicating. That is what
makes the backfill idempotent and safely re-runnable — a re-run recomputes the same key, finds
the object already present, and skips the upload.

Nothing here presumes the caller's schema; it deals only in bytes, keys and URLs.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import settings

# Extension per accepted MIME type. Mirrors the whitelist in app.core.uploads — this module
# never widens what is accepted, it only decides how an already-validated file is named.
_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}

_DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.*)$", re.DOTALL | re.IGNORECASE)


class StorageError(RuntimeError):
    """Raised when object storage is enabled but an operation could not be completed.

    Callers must treat this as a hard failure: never fall back to writing base64 into the
    database, and never clear a source column, when this is raised.
    """


@dataclass(frozen=True)
class DecodedUpload:
    """The raw bytes of a data URL, plus what is needed to describe them in object storage."""
    data: bytes
    content_type: str
    extension: str
    size: int
    sha256: str


def is_enabled() -> bool:
    """True when uploads should go to object storage rather than stay as data URLs."""
    return (settings.STORAGE_BACKEND or "db").lower() == "s3"


def is_data_url(value: str | None) -> bool:
    return bool(value) and value[:5].lower() == "data:"


def decode_data_url(value: str) -> DecodedUpload:
    """Decode a base64 data URL into bytes + metadata.

    Raises StorageError on anything malformed. The MIME type must be one this platform already
    accepts; an unknown type is rejected rather than guessed, so a corrupt or hand-crafted value
    can never be written to storage under a misleading extension.
    """
    m = _DATA_URL_RE.match(value or "")
    if not m:
        raise StorageError("Not a base64 data URL.")
    content_type = m.group(1).lower()
    ext = _EXT.get(content_type)
    if not ext:
        raise StorageError(f"Unsupported content type for storage: {content_type}")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StorageError(f"Malformed base64 payload: {exc}") from exc
    if not raw:
        raise StorageError("Empty payload.")
    return DecodedUpload(
        data=raw,
        content_type=content_type,
        extension=ext,
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def build_key(*, field: str, upload: DecodedUpload) -> str:
    """Deterministic object key: ``<prefix>/<field>/<sha2>/<sha16>.<ext>``.

    Content-addressed by design — the same bytes always yield the same key. That is what makes
    re-running the backfill a no-op instead of a duplicate upload, and it means two transactions
    carrying the identical scan are stored once.

    Deliberately NOT scoped by transaction id: on the create flows the upload is validated before
    the row exists, so no id is available to key on. Uniqueness comes from the digest instead, and
    the ``field`` segment keeps objects browsable and lets a lifecycle or access policy target one
    class of file. The two-character shard keeps any single prefix from growing unboundedly.
    """
    prefix = (settings.S3_PREFIX or "").strip("/")
    parts = [p for p in (prefix, field, upload.sha256[:2]) if p]
    return "/".join(parts) + f"/{upload.sha256[:16]}.{upload.extension}"


@lru_cache(maxsize=1)
def _client():
    """Cached boto3 S3 client. Credentials come from the environment/instance role — this module
    never takes an access key as a parameter, so keys cannot leak in through a call site.

    The endpoint is pinned to the bucket's REGIONAL host and signing forced to SigV4. Without
    both, boto3 can sign for the configured region while addressing the global
    ``<bucket>.s3.amazonaws.com`` host; S3 validates the signature against the host it was sent
    to, the two disagree, and every presigned URL fails with ``SignatureDoesNotMatch``. The SDK's
    own calls survive that because they follow S3's redirect internally — a presigned URL handed
    to a browser cannot, so this breaks ONLY the read path, and only once a real bucket is in
    play. It is invisible to a fake-client test suite.
    """
    if not settings.S3_BUCKET:
        raise StorageError("STORAGE_BACKEND is 's3' but S3_BUCKET is not configured.")
    try:
        import boto3  # already a dependency (used for RDS IAM auth)
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - boto3 is pinned in requirements
        raise StorageError("boto3 is required for the 's3' storage backend.") from exc
    region = settings.S3_REGION or settings.AWS_REGION
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def object_exists(key: str) -> bool:
    """True when the key is already present — used to make re-uploads a no-op."""
    if not is_enabled():
        return False
    try:
        _client().head_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except Exception:
        # head_object raises 404/NoSuchKey for a missing object. Any other error (permissions,
        # network) also lands here and is reported as "absent", so the caller re-uploads; an
        # upload that genuinely cannot proceed then fails loudly in put_object instead.
        return False


def put(key: str, upload: DecodedUpload) -> str:
    """Upload bytes under ``key`` and return the key. Skips the transfer when already present.

    The object is written PRIVATE. Access is granted only through a short-lived presigned URL
    minted by :func:`presigned_url`, after the caller's own authorization checks have passed —
    these are payment slips and bank details, and must never be publicly readable.
    """
    if not is_enabled():
        raise StorageError("Object storage is not enabled.")
    if object_exists(key):
        return key
    try:
        _client().put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=upload.data,
            ContentType=upload.content_type,
            ServerSideEncryption="AES256",
        )
    except Exception as exc:
        raise StorageError(f"Upload failed for {key}: {exc}") from exc
    return key


def presigned_url(key: str, *, ttl: int | None = None) -> str:
    """Mint a short-lived GET URL for ``key``.

    NEVER persist the result: it expires. The database stores the key, and a fresh URL is minted
    per response — the same lesson the KYC module records about the provider's presigned
    ``xml_file`` links expiring after 48h and becoming unfetchable.

    This function performs NO authorization. Call it only after the endpoint has already
    confirmed the caller may see the owning record.
    """
    if not is_enabled():
        raise StorageError("Object storage is not enabled.")
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": key},
            ExpiresIn=int(ttl or settings.S3_URL_TTL),
        )
    except Exception as exc:
        raise StorageError(f"Could not sign {key}: {exc}") from exc


def get_bytes(key: str) -> bytes:
    """Fetch an object's bytes — used by the backfill's post-upload verification."""
    if not is_enabled():
        raise StorageError("Object storage is not enabled.")
    try:
        return _client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()
    except Exception as exc:
        raise StorageError(f"Could not read {key}: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────────────────
# Column-value helpers
#
# A migrated image column holds ``storage://<key>`` instead of a base64 data URL. The explicit
# scheme keeps the two formats unambiguous — a bare key could be mistaken for a filename, and
# `validate_upload` already lets plain http(s) URLs through untouched, so "not a data URL" is
# NOT sufficient to mean "an object reference".
#
# The scheme is deliberately PROVIDER-NEUTRAL. This module abstracts the provider, so the data
# should not contradict that: writing ``s3://`` into millions of rows would hard-code today's
# backend into the database itself, and migrating to MinIO / Azure Blob / GCS later would mean
# rewriting every stored value. ``storage://`` says "an object this application stores"; WHICH
# backend holds it is configuration (``STORAGE_BACKEND``), recorded per file on
# ``transaction_attachment.storage_backend``, and never baked into the reference.
#
# Storing the reference in the column (rather than only in the attachment table) is what keeps
# the read path free: ``_t()`` is synchronous and cannot issue a query, so the value it needs
# must already be on the row it was handed.
# ──────────────────────────────────────────────────────────────────────────────────────────

REF_PREFIX = "storage://"
# Tolerated on read only. No deployment ever ran with object storage enabled, so no row should
# carry this — it is accepted purely so that any value written during local experimentation
# still resolves instead of being served back as a broken literal. Never written.
_LEGACY_REF_PREFIXES = ("s3://",)


def is_ref(value: str | None) -> bool:
    """True when a column value is an object reference rather than inline content."""
    return bool(value) and value.startswith((REF_PREFIX, *_LEGACY_REF_PREFIXES))


def ref_to_key(value: str) -> str:
    """``storage://uploads/x.png`` -> ``uploads/x.png``."""
    for prefix in (REF_PREFIX, *_LEGACY_REF_PREFIXES):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def key_to_ref(key: str) -> str:
    """Always emits the canonical provider-neutral form."""
    return f"{REF_PREFIX}{key}"


def store_value(value: str | None, *, field: str) -> tuple[str | None, DecodedUpload | None]:
    """Persist one upload and return ``(column_value, decoded)``.

    * storage disabled -> returns the value untouched, so behaviour is byte-for-byte what it
      was before this module existed;
    * already a reference or not a data URL (e.g. an http URL) -> returned untouched;
    * otherwise the bytes are uploaded and a provider-neutral ``storage://<key>`` is returned.

    Raises StorageError if the upload fails. Callers MUST let that propagate: falling back to
    writing base64 would silently reintroduce exactly the bloat this migration removes, and
    would do so invisibly.
    """
    if not value or not is_enabled() or is_ref(value) or not is_data_url(value):
        return value, None
    decoded = decode_data_url(value)
    key = build_key(field=field, upload=decoded)
    put(key, decoded)
    return key_to_ref(key), decoded


def resolve_value(value: str | None, *, ttl: int | None = None) -> str | None:
    """Turn a stored column value into something a browser can render.

    Legacy base64 data URLs are returned unchanged, so historical rows keep working exactly as
    they always have. A ``storage://`` reference is exchanged for a short-lived presigned URL —
    which an ``<img src>`` consumes identically to a data URL, so no frontend change is needed.

    A reference that cannot be signed (object deleted, storage misconfigured, credentials
    withdrawn) yields ``None`` rather than raising: one unreadable image must not take down the
    whole transaction-detail response. The record still reports the image's presence via its
    ``has*`` flag, so the gap is visible rather than silently indistinguishable from "no image".
    """
    if not is_ref(value):
        return value
    try:
        return presigned_url(ref_to_key(value), ttl=ttl)
    except StorageError:
        return None
