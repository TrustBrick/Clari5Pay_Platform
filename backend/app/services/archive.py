"""The 14-day archive engine — eligible S3 transaction attachments → persistent Docker volume.

COPY-AND-VERIFY ONLY. This engine never deletes an S3 original, never moves or edits a
``transactions`` row, and never touches any balance, report, reconciliation or allocation. It reads
the five image columns of terminal transactions older than the retention cutoff, copies each
``storage://`` object onto the archive volume, re-reads it, matches its SHA-256 and size, and only
then records it VERIFIED in the ``archived_file`` manifest. See ARCHIVE_PHASE2_DESIGN.md.

Consistency guarantees:

* A partial archive is never "success": a file is VERIFIED only after its bytes are re-read and
  checksum+size match; a failure marks that one row FAILED and the run continues.
* A failed copy never deletes its source — there is no delete path here at all.
* Re-running is idempotent and resumable: content-addressed keys + the manifest UNIQUE +
  atomic volume writes mean an already-VERIFIED file is skipped and a FAILED one is retried.
* UTC throughout (the timestamp columns are naive UTC).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core import archive_storage, storage
from app.models.models import ArchiveBatch, ArchivedFile, Transaction, TxStatus

log = logging.getLogger("clari5pay.archive")

# Terminal states whose attachments may be archived once old enough. Every other status is live
# workflow and is never archived regardless of age.
TERMINAL_STATUSES = {
    TxStatus.COMPLETED, TxStatus.DEPOSITED, TxStatus.REJECTED,
    TxStatus.SA_REJECTED, TxStatus.CANCELLED,
}

# The five image-bearing columns on `transactions`. The two *_proofs columns are JSON arrays of
# references; the other three are single references. All are deferred on the model.
SINGLE_FIELDS = ("merchant_proof", "admin_proof", "admin_bank_image")
ARRAY_FIELDS = ("merchant_proofs", "admin_proofs")
ARCHIVABLE_FIELDS = SINGLE_FIELDS + ARRAY_FIELDS


def _completion_ts(tx: Transaction) -> datetime:
    """The best available 'this transaction reached its terminal state' timestamp.

    Prefers the explicit admin/manager/supervisor action time, then a cancellation time, and falls
    back to creation time (a row can never have completed before it was created, so this is a safe
    lower bound that keeps very old rows eligible even when no action timestamp was recorded).
    """
    return (tx.admin_action_at or tx.manager_action_at or tx.supervisor_action_at
            or tx.cancelled_at or tx.created_at)


def _iter_refs(tx: Transaction):
    """Yield ``(field, object_key)`` for every DISTINCT ``storage://`` object on the transaction.

    A physical object is archived once per transaction even when several columns reference it — and
    they do: the legacy ``merchant_proof`` / ``admin_proof`` single columns hold a copy of the first
    entry of their ``*_proofs`` array (see _add_merchant_proofs / _add_admin_proofs). De-duplicating
    by content-addressed key keeps one manifest row and one volume copy per object; because the
    detail resolver maps a key to its archived id, every column that references the key still
    resolves. The first field in which the key appears is recorded.

    Base64 data URLs and plain http(s) URLs are ignored (not on the S3 workflow). The deferred
    columns must already be loaded on ``tx``.
    """
    seen: set[str] = set()

    def _emit(field: str, val):
        if storage.is_ref(val):
            key = storage.ref_to_key(val)
            if key not in seen:
                seen.add(key)
                return field, key
        return None

    for field in SINGLE_FIELDS:
        out = _emit(field, getattr(tx, field, None))
        if out:
            yield out
    for field in ARRAY_FIELDS:
        raw = getattr(tx, field, None)
        if not raw:
            continue
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = []
        for val in items if isinstance(items, list) else []:
            out = _emit(field, val)
            if out:
                yield out


async def _get_or_create_manifest(db: AsyncSession, tx: Transaction, field: str,
                                  object_key: str) -> ArchivedFile:
    existing = (await db.execute(
        select(ArchivedFile).where(
            ArchivedFile.source_entity == "transactions",
            ArchivedFile.source_id == tx.id,
            ArchivedFile.field == field,
            ArchivedFile.object_key == object_key,
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    row = ArchivedFile(
        source_entity="transactions", source_id=tx.id, source_ref=tx.ref,
        merchant_id=tx.merchant_id, field=field, object_key=object_key,
        dest_path=archive_storage.dest_key(object_key), storage_location="s3",
        archive_status="PENDING", original_created_at=tx.created_at,
        original_completed_at=_completion_ts(tx),
    )
    db.add(row)
    await db.flush()
    return row


async def _mint_batch_ref(db: AsyncSession, batch: ArchiveBatch) -> None:
    """Assign a human-facing ARB serial. Uses the row's own id (allocated at flush), so it is
    portable across Postgres and SQLite without a dedicated sequence."""
    await db.flush()
    batch.batch_ref = f"ARB{batch.id:06d}"


async def run_archive(db: AsyncSession, *, days: int | None = None, dry_run: bool | None = None,
                      now: datetime | None = None) -> dict:
    """Run one archive pass and return a summary.

    ``days`` / ``dry_run`` default to the configured ``ARCHIVE_AFTER_DAYS`` / ``ARCHIVE_DRY_RUN``.
    A dry run reports eligible counts and estimated bytes (via a HEAD, no download) and writes
    nothing to the volume. A real run copies, verifies and records each file.
    """
    from app.core.config import settings
    days = settings.ARCHIVE_AFTER_DAYS if days is None else days
    dry_run = settings.ARCHIVE_DRY_RUN if dry_run is None else dry_run
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)

    batch = ArchiveBatch(dry_run=dry_run, cutoff_days=days, cutoff_at=cutoff,
                         status="RUNNING", started_at=now, batch_ref="PENDING")
    db.add(batch)
    await _mint_batch_ref(db, batch)

    # A real run needs a writable volume; refuse up front rather than fail every file.
    if not dry_run and not archive_storage.volume_available():
        batch.status = "FAILED"
        batch.error = "Archive volume is not available or not writable."
        batch.finished_at = datetime.utcnow()
        await db.flush()
        return _summary(batch)

    # Candidate set: pre-filter on the indexed created_at (a row cannot complete before it is
    # created, so created_at < cutoff is a safe superset), load the deferred image columns, then
    # refine by terminal status + completion timestamp in Python.
    stmt = (select(Transaction)
            .where(Transaction.created_at < cutoff)
            .options(*(undefer(getattr(Transaction, f)) for f in ARCHIVABLE_FIELDS)))
    candidates = (await db.execute(stmt)).scalars().all()

    eligible_txns = 0
    eligible_files = 0
    archived = 0
    skipped = 0
    failed = 0
    bytes_total = 0

    for tx in candidates:
        if tx.status not in TERMINAL_STATUSES:
            continue
        if _completion_ts(tx) >= cutoff:
            continue
        refs = list(_iter_refs(tx))
        if not refs:
            continue
        eligible_txns += 1
        for field, object_key in refs:
            eligible_files += 1
            row = await _get_or_create_manifest(db, tx, field, object_key)

            # Already done and still present on the volume → nothing to do.
            if row.archive_status == "VERIFIED" and archive_storage.exists(object_key):
                skipped += 1
                continue

            if dry_run:
                try:
                    row.source_size = storage.object_size(object_key)
                    row.updated_at = datetime.utcnow()
                    bytes_total += row.source_size or 0
                except storage.StorageError as exc:
                    row.archive_status = "FAILED"
                    row.error = f"dry-run head failed: {exc}"[:500]
                    failed += 1
                continue

            try:
                row.archive_status = "COPYING"
                await db.flush()
                data = storage.get_bytes(object_key)          # raises if the source is missing
                checksum = hashlib.sha256(data).hexdigest()
                size = len(data)
                archive_storage.write(object_key, data)       # atomic
                if not archive_storage.verify(object_key, checksum=checksum, expected_size=size):
                    raise archive_storage.ArchiveStorageError("post-copy checksum/size mismatch")
                row.checksum = checksum
                row.source_size = size
                row.dest_size = size
                row.dest_path = archive_storage.dest_key(object_key)
                row.archive_status = "VERIFIED"
                row.verified_at = datetime.utcnow()
                row.error = None
                row.updated_at = datetime.utcnow()
                archived += 1
                bytes_total += size
            except (storage.StorageError, archive_storage.ArchiveStorageError, OSError) as exc:
                row.archive_status = "FAILED"
                row.error = str(exc)[:500]
                row.retry_count = (row.retry_count or 0) + 1
                row.updated_at = datetime.utcnow()
                failed += 1
                log.warning("archive: file %s/%s failed: %s", tx.ref, field, exc)

    batch.eligible_txn_count = eligible_txns
    batch.eligible_file_count = eligible_files
    batch.archived_file_count = archived
    batch.skipped_file_count = skipped
    batch.failed_file_count = failed
    batch.bytes_total = bytes_total
    batch.status = "FAILED" if failed else "COMPLETED"
    batch.finished_at = datetime.utcnow()
    await db.flush()
    return _summary(batch)


def _summary(batch: ArchiveBatch) -> dict:
    return {
        "batchRef": batch.batch_ref,
        "dryRun": batch.dry_run,
        "cutoffDays": batch.cutoff_days,
        "cutoffAt": batch.cutoff_at.isoformat() + "Z" if batch.cutoff_at else None,
        "status": batch.status,
        "eligibleTransactions": batch.eligible_txn_count,
        "eligibleFiles": batch.eligible_file_count,
        "archivedFiles": batch.archived_file_count,
        "skippedFiles": batch.skipped_file_count,
        "failedFiles": batch.failed_file_count,
        "bytesTotal": batch.bytes_total,
        "error": batch.error,
    }


async def verify_archive(db: AsyncSession) -> dict:
    """Re-check every VERIFIED manifest row against the volume (restore / integrity audit).

    Read-only: reports how many objects are present and match their recorded checksum/size, and
    which are missing or corrupt. Never writes or deletes.
    """
    rows = (await db.execute(
        select(ArchivedFile).where(ArchivedFile.archive_status == "VERIFIED")
    )).scalars().all()
    ok = missing = corrupt = 0
    for r in rows:
        if not archive_storage.exists(r.object_key):
            missing += 1
            continue
        try:
            good = archive_storage.verify(r.object_key, checksum=r.checksum,
                                          expected_size=r.dest_size)
        except archive_storage.ArchiveStorageError:
            good = False
        ok += 1 if good else 0
        corrupt += 0 if good else 1
    return {"verified": len(rows), "present": ok, "missing": missing, "corrupt": corrupt}
