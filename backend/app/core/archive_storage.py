"""Persistent-volume storage for the 14-day archive — the Docker-host counterpart of ``storage``.

Where :mod:`app.core.storage` moves an uploaded file's bytes to S3 and keeps a ``storage://<key>``
reference, this module copies an already-stored S3 object onto a persistent **Docker named volume**
mounted at ``settings.ARCHIVE_VOLUME_PATH`` (default ``/archive``). It deals only in bytes, keys
and sizes — it knows nothing of transactions or the database.

Design rules (see ARCHIVE_PHASE2_DESIGN.md):

* **Keys are reused verbatim** from S3, so an object is content-addressed on the volume exactly as
  it is in the bucket (``uploads/<field>/<sha2>/<sha16>.<ext>``). That is what makes a re-run find
  the object already present and skip it.
* **Every key is confined to the volume root.** A key that resolves outside the root is refused, so
  a crafted value can never read or write an arbitrary host path.
* **Writes are atomic** (temp file + ``os.replace``), so an interrupted copy never leaves a
  half-written object that a later existence check would wrongly accept.
* **No delete path.** V1 is copy-and-verify only; nothing here removes a file.
* **Host paths never leave this module.** Callers deal in volume-relative keys; the absolute path
  is internal.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from app.core.config import settings


class ArchiveStorageError(RuntimeError):
    """Raised when an archive-volume operation cannot be completed.

    Callers (the worker) must treat this as a hard failure for that file: mark it FAILED, never
    mark it VERIFIED, and — always — never delete the S3 source.
    """


def is_enabled() -> bool:
    """True when the archive feature is switched on and a volume path is configured."""
    return bool(settings.ARCHIVE_ENABLED and settings.ARCHIVE_VOLUME_PATH)


def _root() -> Path:
    if not settings.ARCHIVE_VOLUME_PATH:
        raise ArchiveStorageError("ARCHIVE_VOLUME_PATH is not configured.")
    return Path(settings.ARCHIVE_VOLUME_PATH)


def dest_key(object_key: str) -> str:
    """The volume-relative key for an S3 object key. Identity by default — keys are reused verbatim
    so the volume mirrors the bucket's content-addressed layout."""
    return object_key.lstrip("/")


def _safe_path(key: str) -> Path:
    """Resolve ``key`` under the volume root, refusing anything that escapes it.

    The guard is what lets the rest of the system treat a key as untrusted: ``..`` segments, an
    absolute key, or a symlink pointing outside the root all raise rather than touch a foreign path.
    """
    root = _root().resolve()
    candidate = (root / key.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ArchiveStorageError(f"Refusing a path outside the archive volume: {key!r}")
    return candidate


def volume_available() -> bool:
    """True when the configured volume root exists and is a writable directory."""
    try:
        root = _root()
        return root.is_dir() and os.access(root, os.W_OK)
    except Exception:
        return False


def free_bytes() -> int:
    """Free space (bytes) on the volume's filesystem, for the worker's pre-flight disk check."""
    try:
        usage = os.statvfs(_root())
        return usage.f_bavail * usage.f_frsize
    except (OSError, AttributeError):
        # statvfs is POSIX-only (absent on Windows dev hosts). Unknown → don't block; the worker's
        # per-file write still fails loudly if the disk is genuinely full.
        return -1


def exists(key: str) -> bool:
    try:
        return _safe_path(dest_key(key)).is_file()
    except ArchiveStorageError:
        return False


def size(key: str) -> int:
    path = _safe_path(dest_key(key))
    if not path.is_file():
        raise ArchiveStorageError(f"No archived object for key: {key!r}")
    return path.stat().st_size


def write(key: str, data: bytes) -> int:
    """Write ``data`` to the volume under ``key`` atomically, returning the byte count.

    Parent directories are created ``0700`` and the file ``0600`` (owner-only) — these are payment
    slips and bank images. A temp file in the same directory is renamed into place, so a reader
    never sees a partial object and an interrupted run leaves no corrupt file behind.
    """
    path = _safe_path(dest_key(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort on filesystems that don't honour POSIX modes (e.g. Windows dev)
    tmp = path.parent / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArchiveStorageError(f"Could not write archived object {key!r}: {exc}") from exc
    return len(data)


def read(key: str) -> bytes:
    path = _safe_path(dest_key(key))
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ArchiveStorageError(f"Could not read archived object {key!r}: {exc}") from exc


def verify(key: str, *, checksum: str | None, expected_size: int | None) -> bool:
    """Re-read the written object and confirm its size and SHA-256 match what was copied.

    This is the gate the worker must pass before marking a file VERIFIED — the archive's own
    integrity check, independent of whatever the copy appeared to do.
    """
    data = read(key)
    if expected_size is not None and len(data) != expected_size:
        return False
    if checksum is not None and hashlib.sha256(data).hexdigest() != checksum:
        return False
    return True
