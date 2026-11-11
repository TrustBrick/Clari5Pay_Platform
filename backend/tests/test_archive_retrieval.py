"""Transparent, authorization-checked retrieval of archived transaction images.

Proves the acceptance requirement: an archived image opens for the authorized viewer exactly like
a hot one, through the existing backend, with the same permissions — and never for anyone else.
Also covers the detail view's archive-aware URL resolution and the S3 fallback.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.core import archive_storage, storage
from app.core.config import settings as cfg
from app.db.session import Base
from app.models.models import ArchivedFile, Transaction, TxStatus, TxType, User, UserRole
from app.services import archive


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        if Params["Key"] not in self.objects:
            raise RuntimeError("no key")
        return f"https://fake.s3/{Params['Key']}?sig=x"


class _Body:
    def __init__(self, d):
        self._d = d

    def read(self):
        return self._d


NOW = datetime(2026, 11, 11, 12, 0, 0)
PROOF_BYTES = b"archived-slip-bytes"
PROOF = "data:image/png;base64," + base64.b64encode(PROOF_BYTES).decode()


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
    return tmp_path


async def _user(db, uid, role, *, merchant_role=None, name="BELLAGIO"):
    u = User(id=uid, username=f"u{uid}", name=name, role=role, hashed_password="x",
             email=f"u{uid}@t.local", merchant_role=merchant_role)
    db.add(u)
    await db.flush()
    return u


async def _archived_txn(db, *, tid=1, merchant_id=10):
    """A completed, 30-day-old transaction with one S3 proof, archived to the volume."""
    import json
    ref, _ = storage.store_value(PROOF, field="merchant_proofs")
    completed = NOW - timedelta(days=30)
    tx = Transaction(id=tid, ref=f"DEP{tid:06d}", type=TxType.DEPOSIT, amount=1000.0,
                     status=TxStatus.COMPLETED, merchant_id=merchant_id, merchant_name="BELLAGIO",
                     tx_date=date(2026, 1, 1), tx_time="10:00:00", created_at=completed,
                     admin_action_at=completed, merchant_proofs=json.dumps([ref]), merchant_proof=ref)
    db.add(tx)
    await db.flush()
    await archive.run_archive(db, days=14, dry_run=False, now=NOW)
    row = (await db.execute(select(ArchivedFile).where(ArchivedFile.source_id == tid))).scalar_one()
    assert row.archive_status == "VERIFIED"
    return tx, row


# ── Owner / oversight / admin may read; a stranger may not ────────────────────────────────────
@pytest.mark.asyncio
async def test_owner_streams_archived_image_from_volume(db, s3, archive_on):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    resp = await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert resp.body == PROOF_BYTES
    assert resp.media_type == "image/png"


@pytest.mark.asyncio
async def test_admin_streams_archived_image(db, s3, archive_on):
    await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    admin = await _user(db, 1, UserRole.ADMIN, name="Admin")
    tx, row = await _archived_txn(db, merchant_id=10)
    resp = await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, admin)
    assert resp.body == PROOF_BYTES


@pytest.mark.asyncio
async def test_superadmin_streams_archived_image(db, s3, archive_on):
    await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    sa = await _user(db, 2, UserRole.SUPER_ADMIN, name="Super Admin")
    tx, row = await _archived_txn(db, merchant_id=10)
    resp = await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, sa)
    assert resp.body == PROOF_BYTES


# ── Strict OFF (DECISION 1): disabled feature → controlled 404 for everyone ────────────────────
@pytest.mark.asyncio
async def test_disabled_feature_returns_404(db, s3, archive_on, monkeypatch):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)   # archived while enabled
    # Now flip the feature OFF: the endpoint must reject with a controlled 404, even for the owner.
    monkeypatch.setattr(cfg, "ARCHIVE_ENABLED", False, raising=False)
    with pytest.raises(HTTPException) as exc:
        await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_disabled_feature_detail_still_resolves_via_s3(db, s3, archive_on, monkeypatch):
    """Ordinary (hot) image retrieval must keep working when archival is disabled: the detail
    resolver returns a presigned S3 URL, not a backend archive URL."""
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    monkeypatch.setattr(cfg, "ARCHIVE_ENABLED", False, raising=False)
    amap = await txr._archived_key_map(db, tx.id)
    assert amap == {}, "archived-key map must be empty when the feature is off"
    url = txr._resolve_archive_aware(storage.key_to_ref(row.object_key), amap, f"TXN{tx.id:03d}")
    assert url.startswith("https://") and "sig=" in url, "image must resolve via presigned S3"


@pytest.mark.asyncio
async def test_other_merchant_is_forbidden(db, s3, archive_on):
    await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR", name="BELLAGIO")
    stranger = await _user(db, 20, UserRole.MERCHANT, merchant_role="DATA_OPERATOR", name="MGM")
    tx, row = await _archived_txn(db, merchant_id=10)
    with pytest.raises(HTTPException) as exc:
        await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, stranger)
    assert exc.value.status_code == 403


# ── An archived_file id from another transaction is rejected ───────────────────────────────────
@pytest.mark.asyncio
async def test_cross_transaction_id_is_404(db, s3, archive_on):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx1, row1 = await _archived_txn(db, tid=1, merchant_id=owner.id)
    tx2, _ = await _archived_txn(db, tid=2, merchant_id=owner.id)
    # row1 belongs to tx1; asking for it under tx2 must 404 (the token is bound to its transaction).
    with pytest.raises(HTTPException) as exc:
        await txr.get_archived_attachment(f"TXN{tx2.id:03d}", row1.id, db, owner)
    assert exc.value.status_code == 404


# ── Volume unreadable → falls back to the retained S3 original ─────────────────────────────────
@pytest.mark.asyncio
async def test_volume_failure_falls_back_to_s3(db, s3, archive_on, monkeypatch):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    monkeypatch.setattr(archive_storage, "read",
                        lambda *a, **k: (_ for _ in ()).throw(archive_storage.ArchiveStorageError("gone")))
    resp = await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert resp.body == PROOF_BYTES, "the S3 original is the safety net when the volume fails"


# ── Retrieval works even when the ORIGINAL S3 object is gone (volume is the live copy) ────────
@pytest.mark.asyncio
async def test_retrieval_works_when_s3_original_is_gone(db, s3, archive_on):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    # Drop the S3 original entirely — the archived volume copy must still serve the image.
    s3.objects.clear()
    resp = await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert resp.body == PROOF_BYTES, "the volume copy must serve the image without S3"


# ── Both sources unavailable → a controlled 404, never a crash or a host path ─────────────────
@pytest.mark.asyncio
async def test_both_sources_unavailable_is_controlled_404(db, s3, archive_on, monkeypatch):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    monkeypatch.setattr(archive_storage, "read",
                        lambda *a, **k: (_ for _ in ()).throw(archive_storage.ArchiveStorageError("gone")))
    s3.objects.clear()   # volume read fails AND the S3 fallback is gone
    with pytest.raises(HTTPException) as exc:
        await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert exc.value.status_code == 404


# ── A non-VERIFIED manifest row is not served ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pending_row_is_404(db, s3, archive_on):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    row.archive_status = "PENDING"
    await db.flush()
    with pytest.raises(HTTPException) as exc:
        await txr.get_archived_attachment(f"TXN{tx.id:03d}", row.id, db, owner)
    assert exc.value.status_code == 404


# ── Detail resolver: archived → backend URL; hot → presigned S3 ───────────────────────────────
@pytest.mark.asyncio
async def test_resolver_routes_archived_vs_hot(db, s3, archive_on):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, row = await _archived_txn(db, merchant_id=owner.id)
    amap = await txr._archived_key_map(db, tx.id)
    assert row.object_key in amap

    archived_ref = storage.key_to_ref(row.object_key)
    url = txr._resolve_archive_aware(archived_ref, amap, f"TXN{tx.id:03d}")
    assert url == f"/api/transactions/TXN{tx.id:03d}/attachment/{row.id}"

    # A different (hot) object resolves to a presigned S3 URL, unchanged.
    hot_ref, _ = storage.store_value(
        "data:image/png;base64," + base64.b64encode(b"hot").decode(), field="admin_proof")
    hot_url = txr._resolve_archive_aware(hot_ref, amap, f"TXN{tx.id:03d}")
    assert hot_url.startswith("https://") and "sig=" in hot_url


@pytest.mark.asyncio
async def test_key_map_empty_when_feature_disabled(db, s3, archive_on, monkeypatch):
    owner = await _user(db, 10, UserRole.MERCHANT, merchant_role="DATA_OPERATOR")
    tx, _ = await _archived_txn(db, merchant_id=owner.id)
    monkeypatch.setattr(cfg, "ARCHIVE_ENABLED", False, raising=False)
    assert await txr._archived_key_map(db, tx.id) == {}
