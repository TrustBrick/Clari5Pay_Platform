"""Read-only metrics for the 14-day archive — consumed by archive_monitor.sh.

Runs INSIDE the backend container (it needs the app's DB session and the mounted volume). Emits a
single JSON line and never writes anything. Reports volume usage, filesystem free space, archived /
failed file counts, failed-batch count and the age of the last batch. Threshold evaluation and
alert routing live in the shell wrapper, so this stays a pure sensor.

    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python ops/archive_monitor.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime


async def main() -> None:
    from sqlalchemy import func, select
    from app.core import archive_storage
    from app.core.config import settings
    from app.db.session import AsyncSessionLocal
    from app.models.models import ArchiveBatch, ArchivedFile

    root = settings.ARCHIVE_VOLUME_PATH
    fs_total = fs_avail = None
    try:
        st = os.statvfs(root)
        fs_total = st.f_blocks * st.f_frsize
        fs_avail = st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        pass

    vol_bytes = vol_files = 0
    try:
        for dp, _dirs, files in os.walk(root):
            for fn in files:
                try:
                    vol_bytes += os.path.getsize(os.path.join(dp, fn))
                    vol_files += 1
                except OSError:
                    pass
    except OSError:
        pass

    async with AsyncSessionLocal() as db:
        failed_batches = (await db.execute(
            select(func.count()).select_from(ArchiveBatch).where(ArchiveBatch.status == "FAILED"))).scalar()
        last_batch = (await db.execute(
            select(ArchiveBatch).order_by(ArchiveBatch.id.desc()).limit(1))).scalar_one_or_none()
        verified = (await db.execute(
            select(func.count()).select_from(ArchivedFile).where(ArchivedFile.archive_status == "VERIFIED"))).scalar()
        failed_files = (await db.execute(
            select(func.count()).select_from(ArchivedFile).where(ArchivedFile.archive_status == "FAILED"))).scalar()

    last_at = last_batch.finished_at or last_batch.started_at if last_batch else None
    age_h = round((datetime.utcnow() - last_at).total_seconds() / 3600, 1) if last_at else None
    print(json.dumps({
        "volumePath": root,
        "volumeAvailable": archive_storage.volume_available(),
        "fsTotalBytes": fs_total,
        "fsAvailBytes": fs_avail,
        "fsUsedPct": round(100 * (1 - fs_avail / fs_total), 1) if (fs_total and fs_avail is not None) else None,
        "archiveBytes": vol_bytes,
        "archiveFiles": vol_files,
        "verifiedFiles": verified,
        "failedFiles": failed_files,
        "failedBatches": failed_batches,
        "lastBatchStatus": last_batch.status if last_batch else None,
        "lastBatchAgeHours": age_h,
    }))


if __name__ == "__main__":
    asyncio.run(main())
