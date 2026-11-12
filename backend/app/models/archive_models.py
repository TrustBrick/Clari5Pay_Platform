"""Isolated SQLAlchemy registry for the 14-day archive metadata (ArchiveBatch, ArchivedFile).

WHY A SEPARATE Base — the single most important property of this module:

The Demo application creates its schema with ``Base.metadata.create_all`` / ``ensure_schema`` at
startup (the project uses no Alembic). If the archive models were attached to that same ``Base``,
importing them anywhere in the app would register them on the shared metadata and the archive tables
would be **created automatically on every normal boot** — which the Demo integration explicitly must
not do.

So the archive models live on their OWN declarative base, ``ArchiveBase``, with its own
``MetaData``. Importing this module (e.g. from the retrieval endpoint) only *registers* the mapped
classes on ``ArchiveBase`` — it creates nothing. The archive tables come into existence ONLY when an
operator explicitly calls :func:`init_archive_schema`, which is wired to the archive worker's
``--init-db`` flag and to nothing else. It is deliberately NOT part of ``Base.metadata.create_all``
or ``ensure_schema`` and is never invoked at application startup.

Queries against these tables work normally alongside the Demo models: the separation is only of
*metadata/DDL*, not of the database connection — ``AsyncSession`` runs ``select(ArchivedFile)`` and a
``select(Transaction)`` over the same connection regardless of which base each class is mapped on.

Design guarantees (unchanged from origin/main):
  * ``ArchivedFile.source_id`` is a plain integer, NOT a foreign key to ``transactions.id`` — the
    manifest can never constrain, lock or cascade into a live transaction row.
  * The only foreign key is ``ArchivedFile.batch_id -> archive_batch.id`` (both on this base).
  * ``UNIQUE (source_entity, source_id, field, object_key)`` makes the worker idempotent.
  * Copy-and-verify only: nothing here ever causes a ``transactions`` row, an S3 object or any
    balance to change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ArchiveBase(DeclarativeBase):
    """Dedicated metadata for archive tables — intentionally NOT the Demo ``Base``.

    Nothing on this base is created by the app's startup path. See the module docstring.
    """
    pass


class ArchiveBatch(ArchiveBase):
    """One row per run of the 14-day archive worker (see ARCHIVE_PHASE2_DESIGN.md).

    Purely additive bookkeeping. The archive is COPY-AND-VERIFY ONLY: this table, and the
    ``archived_file`` manifest it owns, never cause a ``transactions`` row, an S3 object or any
    balance to change. Created ONLY by the explicit :func:`init_archive_schema` (never at startup);
    a full rollback is a plain ``DROP TABLE``.
    """
    __tablename__ = "archive_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Human-facing serial (ARB000001…) from its own sequence — no existing reference is reused.
    batch_ref: Mapped[str] = mapped_column(String(24), unique=True, index=True, nullable=False)
    # A dry run reports sizes/counts and copies nothing — the default posture of the worker.
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cutoff_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)   # now_utc - cutoff_days
    # RUNNING → COMPLETED (no file failed) | FAILED (one or more did, or a batch-level error).
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", nullable=False)
    eligible_txn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eligible_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # destination bytes
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Batch-level failure note only — never file contents or any secret.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ArchivedFile(ArchiveBase):
    """The archive manifest/index — one row per (transaction, field, object) copied to the volume.

    Metadata ONLY. The bytes live on the persistent Docker volume; the authoritative S3 original is
    RETAINED (copy-and-verify, no deletion in V1). ``source_id`` is a plain integer, deliberately
    NOT a foreign key to ``transactions.id``, so the manifest can never constrain, lock or cascade
    into the live transaction row. ``id`` is the retrieval token used by
    ``GET /api/transactions/{tx_id}/attachment/{id}``.

    ``UNIQUE (source_entity, source_id, field, object_key)`` is what makes the worker idempotent:
    a re-run finds the existing row and skips. Keys are content-addressed (SHA-256), so the same
    bytes always map to the same object — retries never duplicate.
    """
    __tablename__ = "archived_file"
    __table_args__ = (
        UniqueConstraint("source_entity", "source_id", "field", "object_key",
                         name="uq_archived_file"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    batch_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("archive_batch.id"), index=True, nullable=True)

    # ── What was archived ──
    source_entity: Mapped[str] = mapped_column(String(32), default="transactions", nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)       # transactions.id (no FK)
    source_ref: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)  # transactions.ref
    merchant_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    # merchant_proof | merchant_proofs | admin_proof | admin_proofs | admin_bank_image
    field: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── The object ──
    object_key: Mapped[str] = mapped_column(String(512), index=True, nullable=False)  # portable S3 key
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)  # SHA-256
    source_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # bytes in S3
    dest_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)          # bytes on volume
    # Volume-RELATIVE key (e.g. uploads/admin_proof/ab/….png). Never an absolute host path, which
    # must never leave the backend.
    dest_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    storage_location: Mapped[str] = mapped_column(String(16), default="s3", nullable=False)

    # PENDING → COPYING → VERIFIED (checksum+size matched) | FAILED.
    archive_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True, nullable=False)

    # ── Provenance / lifecycle ──
    original_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    original_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)


async def init_archive_schema(engine=None) -> None:
    """EXPLICIT, operator-invoked creation of the archive tables (archive_batch, archived_file).

    This is the ONLY code path that creates the archive schema. It is deliberately SEPARATE from the
    Demo ``Base.metadata.create_all`` and ``ensure_schema`` startup path and is never called during
    normal application startup — importing this module registers the models on ``ArchiveBase`` but
    creates nothing. The archive worker exposes this via ``python archive_worker.py --init-db``.

    Creates ONLY the two archive tables (they are the sole classes on ``ArchiveBase``); it never
    touches any Demo table. Idempotent (``create_all`` is "create if not exists"). Requires an async
    engine; defaults to the app engine when none is passed.

    DO NOT call this against a live database without explicit operator approval.
    """
    if engine is None:
        from app.db.session import engine as _engine
        engine = _engine
    async with engine.begin() as conn:
        await conn.run_sync(ArchiveBase.metadata.create_all)
