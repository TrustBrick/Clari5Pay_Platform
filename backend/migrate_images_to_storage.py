#!/usr/bin/env python
"""Backfill: move existing base64 proof/bank images out of `transactions` and into object storage.

Run from inside the backend container, e.g.:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python migrate_images_to_storage.py --dry-run
    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python migrate_images_to_storage.py --batch-size 10
    docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
        python migrate_images_to_storage.py --clear-source   # the irreversible step

Per file the sequence is strictly: upload -> read back and compare digests -> record metadata.
Only with --clear-source, and only after all of that has succeeded, is the inline base64 removed.
Until then every row keeps its original content and the change is fully reversible.

IDEMPOTENT / RESUMABLE. Keys are content-addressed (the SHA-256 of the bytes IS the key), so a
re-run recomputes the same key, finds the object already present and skips the transfer. A row
already holding a storage:// reference is skipped outright. Interrupting the script — Ctrl-C, a lost
connection, a killed container — is safe: each transaction is committed on its own, so work
already done is kept and the next run resumes from where it stopped.

SAFETY: take an RDS snapshot before running with --clear-source. That flag rewrites live
financial records and is the one step this tool cannot undo.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field as dc_field

from sqlalchemy import select, or_
from sqlalchemy.orm import undefer

from app.core import storage
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.models import Transaction, TransactionAttachment

# (column, is_json_array). merchant_proofs holds a JSON list of up to 3 files; the rest are scalar.
FIELDS: list[tuple[str, bool]] = [
    ("merchant_proof", False),
    ("merchant_proofs", True),
    ("admin_proof", False),
    ("admin_bank_image", False),
]


@dataclass
class Stats:
    scanned: int = 0
    uploaded: int = 0
    skipped_existing: int = 0      # object already in the bucket (a resumed / repeated run)
    already_ref: int = 0           # column already migrated
    cleared: int = 0
    bytes_moved: int = 0
    failures: list[str] = dc_field(default_factory=list)


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _attachment_exists(session, txn_id: int, field: str, key: str) -> bool:
    return (await session.execute(
        select(TransactionAttachment.id).where(
            TransactionAttachment.transaction_id == txn_id,
            TransactionAttachment.field == field,
            TransactionAttachment.object_key == key,
        )
    )).scalar_one_or_none() is not None


async def _migrate_one_value(session, txn_id: int, field: str, value: str,
                             *, dry_run: bool, stats: Stats) -> str | None:
    """Upload one data URL and return the ``storage://`` reference, or None to leave it unchanged."""
    if storage.is_ref(value):
        stats.already_ref += 1
        return None
    if not storage.is_data_url(value):
        return None                      # an http URL or similar — not ours to move

    decoded = storage.decode_data_url(value)
    key = storage.build_key(field=field, upload=decoded)
    stats.scanned += 1

    if dry_run:
        _log(f"  [dry-run] txn {txn_id} {field}: would upload {decoded.size:,} B -> {key}")
        return None

    existed = storage.object_exists(key)
    storage.put(key, decoded)            # a no-op when already present

    # Verify before trusting it. Reading the object back and comparing digests is what makes
    # --clear-source safe: we never discard the only copy on the strength of a write that merely
    # did not raise.
    if hashlib.sha256(storage.get_bytes(key)).hexdigest() != decoded.sha256:
        raise storage.StorageError(f"verification failed for {key} (digest mismatch)")

    if existed:
        stats.skipped_existing += 1
    else:
        stats.uploaded += 1
        stats.bytes_moved += decoded.size

    if not await _attachment_exists(session, txn_id, field, key):
        session.add(TransactionAttachment(
            transaction_id=txn_id,
            field=field,
            storage_backend=(settings.STORAGE_BACKEND or "").lower(),
            object_key=key,
            # Provider-native durable URI, for operators/tooling only. The reference written back
            # onto the transactions row stays provider-neutral (`storage://<key>`).
            object_url=f"s3://{settings.S3_BUCKET}/{key}",
            file_name=f"{decoded.sha256[:16]}.{decoded.extension}",
            mime_type=decoded.content_type,
            file_size=decoded.size,
            checksum=decoded.sha256,
        ))
    return storage.key_to_ref(key)


async def _migrate_transaction(session, tx: Transaction, *, dry_run: bool,
                               clear_source: bool, stats: Stats) -> bool:
    """Migrate every image field on one transaction. Returns True if anything changed."""
    changed = False
    for field, is_array in FIELDS:
        current = getattr(tx, field, None)
        if not current:
            continue

        if is_array:
            try:
                items = json.loads(current)
            except (ValueError, TypeError):
                stats.failures.append(f"txn {tx.id} {field}: unparseable JSON")
                continue
            if not isinstance(items, list):
                continue
            new_items, touched = [], False
            for item in items:
                if not item:
                    continue
                ref = await _migrate_one_value(session, tx.id, field, item,
                                               dry_run=dry_run, stats=stats)
                new_items.append(ref or item)
                touched = touched or ref is not None
            if touched and clear_source:
                setattr(tx, field, json.dumps(new_items))
                changed = True
        else:
            ref = await _migrate_one_value(session, tx.id, field, current,
                                           dry_run=dry_run, stats=stats)
            if ref and clear_source:
                setattr(tx, field, ref)     # inline base64 replaced by the reference
                stats.cleared += 1
                changed = True
    return changed


async def run(*, dry_run: bool, clear_source: bool, batch_size: int, limit: int | None) -> Stats:
    stats = Stats()
    if not storage.is_enabled():
        _log("STORAGE_BACKEND is not 's3'. Enable it (and configure S3_BUCKET) before running.")
        sys.exit(2)

    async with AsyncSessionLocal() as session:
        # The image columns are deferred on the model, so they must be undeferred explicitly —
        # otherwise every value would read as unloaded and the run would silently do nothing.
        stmt = (
            select(Transaction)
            .options(*(undefer(getattr(Transaction, f)) for f, _ in FIELDS))
            .where(or_(*(getattr(Transaction, f).isnot(None) for f, _ in FIELDS)))
            .order_by(Transaction.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        _log(f"{len(rows)} transaction(s) carry at least one image.\n")

        pending = 0
        for tx in rows:
            try:
                if await _migrate_transaction(session, tx, dry_run=dry_run,
                                              clear_source=clear_source, stats=stats):
                    pending += 1
                # Commit per batch so an interruption keeps completed work. Each transaction is
                # independent, so a partial run is a valid state to resume from.
                if pending >= batch_size:
                    await session.commit()
                    pending = 0
            except storage.StorageError as exc:
                await session.rollback()
                stats.failures.append(f"txn {tx.id}: {exc}")
                _log(f"  !! txn {tx.id}: {exc}")
            except Exception as exc:                      # noqa: BLE001 - report and continue
                await session.rollback()
                stats.failures.append(f"txn {tx.id}: {type(exc).__name__}: {exc}")
                _log(f"  !! txn {tx.id}: {type(exc).__name__}: {exc}")
        if not dry_run:
            await session.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would move; upload nothing and write nothing")
    ap.add_argument("--clear-source", action="store_true",
                    help="IRREVERSIBLE: replace the inline base64 with the object reference "
                         "once upload and verification have succeeded")
    ap.add_argument("--batch-size", type=int, default=20, help="transactions per commit")
    ap.add_argument("--limit", type=int, default=None, help="process at most N transactions")
    args = ap.parse_args()

    if args.dry_run and args.clear_source:
        ap.error("--dry-run and --clear-source are mutually exclusive.")

    mode = ("DRY RUN" if args.dry_run
            else "UPLOAD + CLEAR SOURCE" if args.clear_source
            else "UPLOAD ONLY (source kept)")
    _log(f"=== image backfill — {mode} ===")
    _log(f"bucket={settings.S3_BUCKET or '(unset)'} region="
         f"{settings.S3_REGION or settings.AWS_REGION}\n")

    stats = asyncio.run(run(dry_run=args.dry_run, clear_source=args.clear_source,
                            batch_size=args.batch_size, limit=args.limit))

    _log("\n=== summary ===")
    _log(f"  files seen          : {stats.scanned}")
    _log(f"  uploaded            : {stats.uploaded} ({stats.bytes_moved:,} bytes)")
    _log(f"  already in bucket   : {stats.skipped_existing}")
    _log(f"  already referenced  : {stats.already_ref}")
    _log(f"  source cleared      : {stats.cleared}")
    _log(f"  failures            : {len(stats.failures)}")
    for f in stats.failures[:20]:
        _log(f"    - {f}")
    if not args.dry_run and not args.clear_source:
        _log("\nSource base64 was KEPT. Re-run with --clear-source to complete the migration.")
    sys.exit(1 if stats.failures else 0)


if __name__ == "__main__":
    main()
