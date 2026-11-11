"""14-day archive worker — CLI entry point.

Copies eligible S3 transaction attachments older than the retention window onto the persistent
Docker archive volume, verifies each by checksum + size, and records it in the ``archived_file``
manifest. COPY-AND-VERIFY ONLY: it never deletes an S3 original and never touches a transaction
row or any balance. See ARCHIVE_PHASE2_DESIGN.md.

Run inside the backend container (defaults to a DRY RUN — reports, writes nothing):

    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python archive_worker.py --dry-run
    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python archive_worker.py          # real copy
    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python archive_worker.py --verify  # integrity audit

Flags:
    --dry-run / --no-dry-run   override ARCHIVE_DRY_RUN (default: the configured value, which
                               itself defaults to true — a dry run)
    --days N                   override ARCHIVE_AFTER_DAYS (default 14)
    --verify                   re-check already-archived objects against the volume; writes nothing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _main() -> int:
    parser = argparse.ArgumentParser(description="14-day transaction-attachment archive worker.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--verify", action="store_true",
                        help="Audit archived objects against the volume; no copying.")
    args = parser.parse_args()

    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.services import archive

    if not settings.ARCHIVE_ENABLED:
        print("ARCHIVE_ENABLED is false — refusing to run. Enable it in the environment first.",
              file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as db:
        if args.verify:
            result = await archive.verify_archive(db)
        else:
            result = await archive.run_archive(db, days=args.days, dry_run=args.dry_run)
        await db.commit()

    print(json.dumps(result, indent=2))
    # A run that recorded any failed file exits non-zero so a scheduler/CI notices.
    return 1 if (not args.verify and result.get("failedFiles")) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
