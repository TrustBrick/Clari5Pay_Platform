"""14-day archive engine — synthetic-data coverage of every scenario in the brief.

S3 is a faked in-memory bucket (same pattern as test_storage_migration); the archive volume is a
pytest ``tmp_path``. No AWS, no network, no real infrastructure. Everything the brief's Phase 8
lists is exercised: ages 1/13/14/15/30d, pending vs completed, multi-attachment, missing
attachment, multiple merchants, duplicate retries, interrupted jobs, checksum mismatch, volume
unavailable, and the copy-and-verify / no-delete guarantees.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import archive_storage, storage
from app.core.config import settings as cfg
from app.db.session import Base
from app.models.models import ArchiveBatch, ArchivedFile, Transaction, TxStatus, TxType, User, UserRole
from app.services import archive


# ── Fake S3 (in-memory) ──────────────────────────────────────────────────────────────────────
class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404 NoSuchKey")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.put_calls += 1
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404 NoSuchKey")
        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://fake.s3/{Params['Key']}?sig=x"


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _img(tag: str) -> str:
    return "data:image/png;base64," + base64.b64encode(f"img-{tag}".encode()).decode()


# ── Fixtures ──────────────────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(storage.settings, "S3_BUCKET", "test-bucket", raising=False)
    monkeypatch.setattr(storage.settings, "S3_PREFIX", "uploads", raising=False)
    storage._client.cache_clear()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    yield fake


@pytest.fixture
def archive_on(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "ARCHIVE_VOLUME_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(cfg, "ARCHIVE_AFTER_DAYS", 14, raising=False)
    monkeypatch.setattr(cfg, "ARCHIVE_DRY_RUN", False, raising=False)
    monkeypatch.setattr(cfg, "ARCHIVE_PREFIX", "uploads", raising=False)
    return tmp_path


NOW = datetime(2026, 11, 11, 12, 0, 0)


# ── Builders ────────────────────────────────────────────────────────────────────────────────
async def _merchant(db, uid=10, name="BELLAGIO"):
    u = User(id=uid, username=f"op{uid}", name=name, role=UserRole.MERCHANT,
             hashed_password="x", email=f"op{uid}@t.local", merchant_role="DATA_OPERATOR")
    db.add(u)
    await db.flush()
    return u


async def _txn(db, *, tid, merchant_id, status, age_days, proofs=None, bank=None,
               ttype=TxType.DEPOSIT):
    """A synthetic transaction `age_days` old (created + completed), with optional S3-backed images.

    Image values are produced through the real `storage.store_value` so the object actually lands
    in the fake bucket and the stored `storage://` reference matches it — exactly what the worker
    will read.
    """
    completed = NOW - timedelta(days=age_days)
    stored_proofs = []
    for p in (proofs or []):
        ref, _ = storage.store_value(p, field="merchant_proofs")
        stored_proofs.append(ref)
    bank_ref = None
    if bank is not None:
        bank_ref, _ = storage.store_value(bank, field="admin_bank_image")
    import json as _json
    tx = Transaction(
        id=tid, ref=f"DEP{tid:06d}", type=ttype, amount=1000.0, status=status,
        merchant_id=merchant_id, merchant_name="BELLAGIO", tx_date=date(2026, 1, 1),
        tx_time="10:00:00", created_at=completed, admin_action_at=completed,
        merchant_proofs=_json.dumps(stored_proofs) if stored_proofs else None,
        merchant_proof=stored_proofs[0] if stored_proofs else None,
        admin_bank_image=bank_ref,
    )
    db.add(tx)
    await db.flush()
    return tx


# ── 1. Recent records stay; eligible historical records are copied ────────────────────────────
@pytest.mark.asyncio
async def test_age_boundary_14_days(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=1, proofs=[_img("d1")])
    await _txn(db, tid=2, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=13, proofs=[_img("d13")])
    await _txn(db, tid=3, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=14, proofs=[_img("d14")])
    await _txn(db, tid=4, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=15, proofs=[_img("d15")])
    await _txn(db, tid=5, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("d30")])

    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)

    # Only the strictly-older-than-14-day records (15, 30) are archived; 1/13/14 stay hot.
    assert res["archivedFiles"] == 2
    assert res["eligibleTransactions"] == 2
    archived_refs = {r.source_ref for r in (await db.execute(
        select(ArchivedFile).where(ArchivedFile.archive_status == "VERIFIED"))).scalars()}
    assert archived_refs == {"DEP000004", "DEP000005"}


# ── 3. Pending records are never archived, however old ────────────────────────────────────────
@pytest.mark.asyncio
async def test_pending_never_archived(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.PENDING, age_days=30, proofs=[_img("p")])
    await _txn(db, tid=2, merchant_id=m.id, status=TxStatus.SUPERVISOR_REVIEW, age_days=40, proofs=[_img("s")])
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert res["archivedFiles"] == 0
    assert res["eligibleTransactions"] == 0


# ── multi-attachment + multiple merchants ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_multi_attachment_and_multi_merchant(db, s3, archive_on):
    a = await _merchant(db, uid=10, name="BELLAGIO")
    b = await _merchant(db, uid=11, name="MGM")
    await _txn(db, tid=1, merchant_id=a.id, status=TxStatus.COMPLETED, age_days=30,
               proofs=[_img("a1"), _img("a2"), _img("a3")], bank=_img("abank"))
    await _txn(db, tid=2, merchant_id=b.id, status=TxStatus.COMPLETED, age_days=20, proofs=[_img("b1")])
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    # 3 proofs + 1 bank image (merchant A) + 1 proof (merchant B) = 5 files across 2 txns.
    assert res["archivedFiles"] == 5
    assert res["eligibleTransactions"] == 2
    # Every object is on the volume.
    for r in (await db.execute(select(ArchivedFile))).scalars():
        assert archive_storage.exists(r.object_key)


# ── txn with no attachments contributes nothing ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transaction_without_attachments_is_skipped(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[])
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert res["eligibleTransactions"] == 0 and res["archivedFiles"] == 0


# ── missing source object → FAILED, source never deleted ──────────────────────────────────────
@pytest.mark.asyncio
async def test_missing_source_object_fails_cleanly(db, s3, archive_on):
    m = await _merchant(db)
    tx = await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("x")])
    # Delete the object from the bucket AFTER the row references it (simulates a dangling ref).
    s3.objects.clear()
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert res["failedFiles"] == 1 and res["archivedFiles"] == 0
    row = (await db.execute(select(ArchivedFile))).scalar_one()
    assert row.archive_status == "FAILED" and row.error


# ── 5. Retries are idempotent (duplicate archive retries) ─────────────────────────────────────
@pytest.mark.asyncio
async def test_rerun_is_idempotent(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("i")])
    first = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert first["archivedFiles"] == 1
    second = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert second["archivedFiles"] == 0 and second["skippedFiles"] == 1
    # Exactly one manifest row — no duplication.
    assert len((await db.execute(select(ArchivedFile))).scalars().all()) == 1


# ── 4. Interrupted job: a FAILED row is retried and succeeds on the next run ──────────────────
@pytest.mark.asyncio
async def test_failed_row_recovers_on_rerun(db, s3, archive_on, monkeypatch):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("r")])
    # First run: force the volume write to fail → the file is marked FAILED, source untouched.
    real_write = archive_storage.write
    def boom(*a, **k):
        raise archive_storage.ArchiveStorageError("simulated volume outage")
    monkeypatch.setattr(archive_storage, "write", boom)
    r1 = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert r1["failedFiles"] == 1 and r1["status"] == "FAILED"
    # Second run: restore ONLY the volume write (leave s3/volume config intact) → the same file
    # recovers to VERIFIED.
    monkeypatch.setattr(archive_storage, "write", real_write)
    r2 = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert r2["archivedFiles"] == 1 and r2["failedFiles"] == 0
    row = (await db.execute(select(ArchivedFile))).scalar_one()
    assert row.archive_status == "VERIFIED" and row.retry_count == 1


# ── checksum mismatch → FAILED ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_checksum_mismatch_fails(db, s3, archive_on, monkeypatch):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("c")])
    monkeypatch.setattr(archive_storage, "verify", lambda *a, **k: False)
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert res["failedFiles"] == 1 and res["archivedFiles"] == 0


# ── volume unavailable → batch refused up front ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_volume_unavailable_refuses_batch(db, s3, archive_on, monkeypatch, tmp_path):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("v")])
    monkeypatch.setattr(cfg, "ARCHIVE_VOLUME_PATH", str(tmp_path / "does-not-exist"), raising=False)
    res = await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert res["status"] == "FAILED" and res["archivedFiles"] == 0
    assert "volume" in (res["error"] or "").lower()


# ── 2. Dry run reports but copies nothing ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db, s3, archive_on, tmp_path):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("dry1"), _img("dry2")])
    res = await archive.run_archive(db, days=14, dry_run=True, now=NOW)
    assert res["dryRun"] is True
    assert res["eligibleFiles"] == 2
    assert res["bytesTotal"] > 0                      # sized via HEAD
    assert res["archivedFiles"] == 0                  # nothing copied
    # Volume is empty and no row is VERIFIED.
    assert not any(p.is_file() for p in tmp_path.rglob("*"))
    assert all(r.archive_status != "VERIFIED" for r in (await db.execute(select(ArchivedFile))).scalars())


# ── copy-and-verify never deletes the S3 source ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_source_is_retained_after_archive(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("keep")])
    before = dict(s3.objects)
    await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    assert s3.objects == before, "the S3 original must still be present after archiving"


# ── preview_archive: the read-only probe behind the scheduled dry-run ─────────────────────────
@pytest.mark.asyncio
async def test_preview_counts_without_writing(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30,
               proofs=[_img("p1"), _img("p2")])
    await _txn(db, tid=2, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=1, proofs=[_img("recent")])
    res = await archive.preview_archive(db, days=14, now=NOW)
    assert res["mode"] == "probe" and res["wrote"] is False
    assert res["eligibleTransactions"] == 1 and res["eligibleFiles"] == 2
    assert res["estimatedBytes"] > 0 and res["sizeUnavailable"] == 0
    # Nothing was persisted: no batch, no manifest rows, nothing on the volume.
    assert (await db.execute(select(ArchiveBatch))).scalars().all() == []
    assert (await db.execute(select(ArchivedFile))).scalars().all() == []


@pytest.mark.asyncio
async def test_preview_runs_with_feature_disabled(db, s3, monkeypatch, tmp_path):
    # ARCHIVE_ENABLED stays FALSE — the probe is pure read and must still work.
    monkeypatch.setattr(cfg, "ARCHIVE_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "ARCHIVE_VOLUME_PATH", str(tmp_path), raising=False)
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("x")])
    res = await archive.preview_archive(db, days=14, now=NOW)
    assert res["eligibleFiles"] == 1 and res["wrote"] is False
    assert (await db.execute(select(ArchivedFile))).scalars().all() == []


# ── verify_archive audits the volume ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_verify_archive_reports_integrity(db, s3, archive_on):
    m = await _merchant(db)
    await _txn(db, tid=1, merchant_id=m.id, status=TxStatus.COMPLETED, age_days=30, proofs=[_img("vok")])
    await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    audit = await archive.verify_archive(db)
    assert audit["verified"] == 1 and audit["present"] == 1 and audit["missing"] == 0
