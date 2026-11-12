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
    parser.add_argument("--probe", action="store_true",
                        help="Read-only eligibility probe: counts + estimated bytes only. Writes "
                             "nothing and does NOT require ARCHIVE_ENABLED — this is the mode the "
                             "scheduled Demo dry-run uses.")
    parser.add_argument("--init-db", dest="init_db", action="store_true",
                        help="EXPLICIT one-time setup: create the archive tables (archive_batch, "
                             "archived_file) in the configured database, then exit. This is the ONLY "
                             "way the archive schema is created — the Demo app NEVER auto-creates it "
                             "at startup (the models use an isolated registry, not the app Base). "
                             "Operator action; requires explicit approval before running.")
    args = parser.parse_args()

    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.services import archive

    # EXPLICIT schema initialization — the separate, operator-invoked path. Never auto-run; creates
    # ONLY the two archive tables on the isolated ArchiveBase (no Demo table is touched).
    if args.init_db:
        from app.models.archive_models import init_archive_schema
        await init_archive_schema()
        print(json.dumps({"mode": "init-db", "created": ["archive_batch", "archived_file"]}, indent=2))
        return 0

    # --probe and --verify are strictly read-only, so they run regardless of the master switch.
    # A real/dry-run archive still requires explicit enablement.
    if not args.probe and not args.verify and not settings.ARCHIVE_ENABLED:
        print("ARCHIVE_ENABLED is false — refusing to run. Enable it in the environment first, "
              "or use --probe for a safe read-only eligibility check.", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as db:
        if args.probe:
            result = await archive.preview_archive(db, days=args.days)   # writes nothing
        elif args.verify:
            result = await archive.verify_archive(db)
        else:
            result = await archive.run_archive(db, days=args.days, dry_run=args.dry_run)
            await db.commit()

    print(json.dumps(result, indent=2))
    # A run that recorded any failed file exits non-zero so a scheduler/CI notices.
    return 1 if (not args.verify and not args.probe and result.get("failedFiles")) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
