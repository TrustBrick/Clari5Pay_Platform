"""Payment proof / slip files — one place that decides how uploads are validated and kept.

A payment is not always one transfer. It is split across accounts, paid in instalments, or
evidenced by a statement page plus a UTR screenshot, and a record that can only hold a fixed
number of files stops being able to show how the money actually moved. So there is deliberately
NO cap on how many files a request may carry.

"No count limit" is NOT "no limit". Every individual file still goes through
``app.core.uploads.validate_upload`` for its MIME type and size, exactly as before — what is
lifted is the arbitrary cap on the count, nothing else.

Two rules hold everywhere files are attached:

  * **Uploads ADD, they never replace.** A second upload keeps the first. Silently dropping
    evidence that has already been reviewed would be the worst failure available here.
  * **A file set lives in a JSON array column, with the legacy single column keeping the FIRST
    file**, so historical rows and older clients still render something.

Both the merchant/admin transaction workflow and the isolated Agent module use this module, so
the two cannot drift apart on what an acceptable upload is.

Note for callers: the reverse proxy caps a single request body (12 MB — see Caddyfile), so a
large set necessarily arrives over several calls. That is a second, independent reason every
write must append rather than replace.
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.core import storage
from app.core.uploads import IMAGE_PDF_TYPES, validate_upload


def parse_proofs(raw: str | None) -> list[str]:
    """The stored JSON array as a list, tolerating a corrupt or absent value.

    A malformed value degrades to "no files" rather than raising: one unreadable column must
    never take down the transaction-detail response that happens to include it.
    """
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [p for p in items if p] if isinstance(items, list) else []


def resolve_proofs(raw: str | None) -> list[str] | None:
    """Resolve a stored proof array for output, entry by entry.

    Entries are resolved individually, so a mixed array — some files already migrated to object
    storage, others still inline — renders correctly during the backfill. An entry that cannot be
    signed is dropped rather than emitted as null, keeping the list usable by the frontend.
    """
    resolved = [storage.resolve_value(p) for p in parse_proofs(raw)]
    return [p for p in resolved if p] or None


def append_proofs(existing: list[str], new: list[str]) -> str | None:
    """Add newly stored files to the ones already on the record; returns the JSON to store.

    Additive by contract — `existing` survives intact, which is what keeps a second upload from
    wiping the evidence of the first. Exact duplicates are skipped: with object storage the key is
    content-addressed, so re-sending the same image yields the same reference and would otherwise
    appear twice in the gallery.
    """
    merged = list(existing)
    for p in new:
        if p and p not in merged:
            merged.append(p)
    return json.dumps(merged) if merged else None


def store(value: str | None, *, field: str) -> str | None:
    """Hand one validated upload to object storage, returning what the column should hold.

    With STORAGE_BACKEND="db" (the default) this returns the value untouched and the request
    behaves exactly as it always has. With "s3" the bytes are uploaded and a ``storage://<key>``
    reference comes back instead.

    A storage failure becomes a 503 rather than a silent fallback to writing base64: falling back
    would quietly reintroduce the row bloat that migration exists to remove, and the operator
    would have no signal that it happened.
    """
    try:
        stored, _ = storage.store_value(value, field=field)
        return stored
    except storage.StorageError as exc:
        raise HTTPException(status_code=503,
                            detail=f"Could not store the uploaded file: {exc}") from exc


def clean_proofs(proofs: list[str] | None, single: str | None = None,
                 field: str = "merchant_proofs",
                 allowed: tuple = IMAGE_PDF_TYPES, label: str = "proof/slip file") -> list[str]:
    """Validate uploaded proofs: each a JPG/JPEG/PNG/PDF within the per-file size limit.

    Every file is checked, however many there are. When object storage is enabled each accepted
    file is also uploaded and the returned list holds references rather than inline base64.

    `single` is the legacy one-file field; it is used only when the list is empty, so a client
    that sends both does not store the same image twice.
    """
    items = [p for p in (proofs or []) if p]
    if not items and single:
        items = [single]
    for p in items:
        validate_upload(p, allowed=allowed, label=label)
    return [store(p, field=field) for p in items]


def merge_into(existing_raw: str | None, legacy_single: str | None, new: list[str]) -> list[str]:
    """The full file set after an append, back-filling from the legacy single column.

    Returns the merged list; the caller writes the JSON (``append_proofs``) and decides what the
    legacy column should hold. Back-filling matters for any row written before the array column
    existed — without it the first append would make the original file vanish from the gallery.
    """
    existing = parse_proofs(existing_raw) or ([legacy_single] if legacy_single else [])
    merged = list(existing)
    for p in new:
        if p and p not in merged:
            merged.append(p)
    return merged
