"""Demo fixture + validation harness for the object-storage migration.

WHY THIS EXISTS
---------------
The demo database holds zero transactions, so every step of the storage migration would pass
vacuously there: the backfill would report "0 transactions carry an image", exit 0, and prove
nothing — including ``--clear-source``, the one irreversible step that was deliberately
sequenced onto demo so its behaviour would be observed rather than assumed.

This builds a corpus that exercises every storage path the migration has to handle, then
validates the read path against it. Run it on DEMO ONLY:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python seed_storage_migration_test.py
    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python seed_storage_migration_test.py --verify-only
    docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python seed_storage_migration_test.py --purge

IDEMPOTENT: every row is tagged ``notes = STORAGE_SEED`` and the tagged rows are deleted before
reseeding, so repeated runs converge on the same corpus instead of accumulating duplicates. Only
tagged rows are ever touched — the delete is scoped to that tag, so any real demo data survives.

PRODUCTION SAFETY: refuses to run unless ``ENVIRONMENT`` is demo/development, so pointing it at a
production database is an error rather than an accident. It also refuses while object storage is
enabled — the corpus must be written as legacy base64, which is the very thing the migration is
supposed to consume.

KNOWN LIMITATION — the corpus does NOT reproduce production's on-disk size. The fixtures are
synthetic PNGs padded with repeating bytes, which TOAST compresses almost perfectly: ~19 MB of
inline base64 occupies roughly 280 kB on disk, whereas production's real photographs are already
entropy-dense and barely compress (156 MB of base64 -> 162 MB of table). So this corpus fully
exercises the migration's LOGIC — decode, upload, verify, resolve, per-file counts, dedupe — and
the backfill still moves the full ~19 MB of decoded bytes. What it cannot demonstrate is the
space reclaimed by VACUUM, because there is little to reclaim. Judge reclamation on production
figures, not on demo.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import sys
import zlib
from datetime import datetime, timedelta

from sqlalchemy import select, delete, func

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.models import User, UserRole, Transaction, TxType, TxStatus

SEED_TAG = "STORAGE_SEED"
random.seed(20260719)          # deterministic corpus — reruns produce identical bytes


# ── image fixtures ────────────────────────────────────────────────────────────────────────

def _png(width_px: int, filler: bytes = b"") -> str:
    """Build a real, decodable PNG data URL of roughly a chosen size.

    Genuine PNG structure matters: the migration decodes and re-encodes these bytes and verifies
    a SHA-256 round-trip, so a fake payload would test the wrong thing. Size is grown with an
    ancillary chunk, which decoders ignore but which counts toward the byte budget exactly as a
    real photograph's data would.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))

    raw = zlib.compress(b"\x00" + b"\xff\x00\x00" * width_px)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", width_px.to_bytes(4, "big") + (1).to_bytes(4, "big")
                   + bytes([8, 2, 0, 0, 0]))
           + (chunk(b"teXt", b"pad\x00" + filler) if filler else b"")
           + chunk(b"IDAT", raw)
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _pdf(kb: int) -> str:
    """A minimal but structurally valid PDF — proofs accept PDFs as well as images."""
    body = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
            + b"% " + b"x" * (kb * 1024) + b"\n%%EOF\n")
    return "data:application/pdf;base64," + base64.b64encode(body).decode()


# Built once so every transaction referencing "the same" file really does share bytes — that is
# what lets the corpus prove content-addressed de-duplication rather than merely assume it.
SMALL_IMG = _png(4)                                  # ~200 B
MEDIUM_IMG = _png(64, b"m" * 40_000)                 # ~40 KB
LARGE_IMG = _png(256, b"L" * 4_600_000)              # ~4.6 MB — near the 5 MB cap
DUP_IMG = MEDIUM_IMG                                 # deliberately identical to MEDIUM_IMG
PDF_PROOF = _pdf(30)                                 # ~30 KB PDF

MEMBERS = [
    ("WINLK38728", "Sameera Perera"), ("WINLIN24324", "Akbar Davi"),
    ("WINCN36865", "Wang Wei"), ("MBR70011", "Ravi Teja"),
    ("MBR70012", "Priya Nair"), ("MBR70013", "Imran Sheikh"),
    ("NEX10001", "Arjun Mehta"), ("NEX10002", "Sara Ali"),
]
BANKS = [("HDFC Bank", "HDFC0001234"), ("ICICI Bank", "ICIC0004321"),
         ("State Bank of India", "SBIN0009876"), ("Axis Bank", "UTIB0005555")]
ADMINS = ["Admin", "Super Admin"]

# The corpus. Each entry: (label, type, status, images, amount).
# `images` maps column -> fixture, and merchant_proofs takes a list, so the shape of the data
# is visible here rather than buried in generation logic.
CASES: list[tuple[str, TxType, TxStatus, dict, float]] = [
    # ── no images at all — must survive the migration completely untouched ──
    ("deposit, no images", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED, {}, 12500),
    ("withdrawal, no images", TxType.WITHDRAWAL_REQUEST, TxStatus.COMPLETED, {}, 38000),
    ("settlement, no images", TxType.SETTLEMENT_REQUEST, TxStatus.PENDING, {}, 75000),
    ("rejected, no images", TxType.DEPOSIT_REQUEST, TxStatus.REJECTED, {}, 5000),

    # ── exactly one image, one column at a time ──
    ("deposit, small merchant_proof", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"merchant_proof": SMALL_IMG}, 1500),
    ("deposit, medium merchant_proof", TxType.DEPOSIT_REQUEST, TxStatus.SLIP_SUBMITTED,
     {"merchant_proof": MEDIUM_IMG}, 22000),
    ("deposit, admin_proof only", TxType.DEPOSIT_REQUEST, TxStatus.DEPOSITED,
     {"admin_proof": MEDIUM_IMG}, 9500),
    ("deposit, admin_bank_image only", TxType.DEPOSIT_REQUEST, TxStatus.ACCOUNT_SUBMITTED,
     {"admin_bank_image": MEDIUM_IMG}, 55000),

    # ── the large case: admin_bank_image was 141 MB of the 162 MB in production ──
    ("deposit, LARGE admin_bank_image (~4.6MB)", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"admin_bank_image": LARGE_IMG}, 126000),
    ("withdrawal, LARGE merchant_proof (~4.6MB)", TxType.WITHDRAWAL_REQUEST, TxStatus.COMPLETED,
     {"merchant_proof": LARGE_IMG}, 88000),

    # ── merchant_proofs arrays (the JSON column, up to 3 files) ──
    ("deposit, 2 merchant_proofs", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"merchant_proofs": [SMALL_IMG, MEDIUM_IMG]}, 31000),
    ("deposit, 3 merchant_proofs (max)", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"merchant_proofs": [SMALL_IMG, MEDIUM_IMG, PDF_PROOF]}, 47000),
    ("withdrawal, 3 merchant_proofs incl. large", TxType.WITHDRAWAL_REQUEST, TxStatus.PENDING,
     {"merchant_proofs": [MEDIUM_IMG, LARGE_IMG, SMALL_IMG]}, 64000),

    # ── every image column populated at once ──
    ("deposit, ALL image columns", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"merchant_proof": MEDIUM_IMG, "merchant_proofs": [MEDIUM_IMG, SMALL_IMG],
      "admin_proof": SMALL_IMG, "admin_bank_image": MEDIUM_IMG}, 99000),
    ("settlement, proof + bank image", TxType.SETTLEMENT_REQUEST, TxStatus.COMPLETED,
     {"admin_proof": PDF_PROOF, "admin_bank_image": SMALL_IMG}, 150000),

    # ── duplicate bytes across rows: content-addressing must store these ONCE ──
    ("deposit, duplicate of medium image", TxType.DEPOSIT_REQUEST, TxStatus.COMPLETED,
     {"merchant_proof": DUP_IMG}, 18000),
    ("withdrawal, same duplicate bytes", TxType.WITHDRAWAL_REQUEST, TxStatus.COMPLETED,
     {"admin_proof": DUP_IMG}, 26000),

    # ── non-completed states still carrying images ──
    ("rejected, with proof", TxType.DEPOSIT_REQUEST, TxStatus.REJECTED,
     {"merchant_proof": SMALL_IMG}, 7300),
    ("cancelled, with proofs", TxType.WITHDRAWAL_REQUEST, TxStatus.CANCELLED,
     {"merchant_proofs": [SMALL_IMG, MEDIUM_IMG]}, 4100),
    ("pending settlement, PDF proof", TxType.SETTLEMENT_REQUEST, TxStatus.PENDING_APPROVAL,
     {"admin_proof": PDF_PROOF}, 210000),
]


def _guard() -> None:
    """Refuse to run anywhere the corpus would be wrong or dangerous."""
    env = (getattr(settings, "ENVIRONMENT", "") or "").lower()
    if env not in ("demo", "development", "dev", "local", "test", ""):
        sys.exit(f"REFUSING TO RUN: ENVIRONMENT={env!r}. This script is for demo/dev only.")
    backend = (getattr(settings, "STORAGE_BACKEND", "db") or "db").lower()
    if backend != "db":
        sys.exit(f"REFUSING TO RUN: STORAGE_BACKEND={backend!r}. The corpus must be written as "
                 "legacy base64 — that is what the migration is meant to consume. Seed first, "
                 "then enable object storage.")


async def purge(db) -> int:
    res = await db.execute(delete(Transaction).where(Transaction.notes == SEED_TAG))
    await db.commit()
    return res.rowcount or 0


async def seed(db) -> int:
    merchants = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT).order_by(User.id))).scalars().all()
    if not merchants:
        sys.exit("No MERCHANT users on this database — cannot attach transactions.")

    removed = await purge(db)
    if removed:
        print(f"  removed {removed} row(s) from a previous run (idempotent reseed)")

    base = datetime.utcnow() - timedelta(days=45)
    added = 0
    for i, (label, ttype, status, images, amount) in enumerate(CASES):
        m = merchants[i % len(merchants)]
        member_id, member_name = MEMBERS[i % len(MEMBERS)]
        bank, ifsc = BANKS[i % len(BANKS)]
        created = base + timedelta(days=i * 2, hours=(i * 7) % 24, minutes=(i * 13) % 60)
        completed = status in (TxStatus.COMPLETED, TxStatus.DEPOSITED)
        is_deposit = ttype == TxType.DEPOSIT_REQUEST

        added += 1
        db.add(Transaction(
            ref=f"STG{800000 + added}",
            type=ttype, amount=float(amount), status=status,
            merchant_id=m.id, merchant_name=m.name,
            tx_date=created.date(), tx_time=created.strftime("%H:%M:%S"),
            member_id=member_id, member_name=member_name,
            deposit_type=("UPI" if is_deposit else None),
            payout_mode=(None if is_deposit else "BANK"),
            bank_name=bank, ifsc=ifsc,
            account_holder=member_name,
            account_number=f"{9000000000 + i * 7717}",
            merchant_ref=f"MR{500000 + added}",
            admin_ref=(f"AR{700000 + added}" if completed else None),
            agent_code=m.merchant_code,
            approved_by=(ADMINS[i % len(ADMINS)] if completed else None),
            processed_by=(ADMINS[(i + 1) % len(ADMINS)] if completed else None),
            high_risk=(i % 9 == 0),
            reject_reason=("Payment not received in our bank."
                           if status == TxStatus.REJECTED else None),
            cancel_reason=("Duplicate request — cancelled by merchant."
                           if status == TxStatus.CANCELLED else None),
            # The images under test.
            merchant_proof=images.get("merchant_proof"),
            merchant_proofs=(json.dumps(images["merchant_proofs"])
                             if images.get("merchant_proofs") else None),
            admin_proof=images.get("admin_proof"),
            admin_bank_image=images.get("admin_bank_image"),
            notes=SEED_TAG,
            created_at=created,
        ))
        print(f"  [{added:2}] {label}")
    await db.commit()
    return added


# ── verification ──────────────────────────────────────────────────────────────────────────

async def verify(db) -> tuple[dict, list[str]]:
    """Count the corpus and drive the real read path over every seeded row."""
    from sqlalchemy.orm import undefer
    from app.api.routes.transactions import _t
    from app.core import storage

    failures: list[str] = []
    cols = ["merchant_proof", "merchant_proofs", "admin_proof", "admin_bank_image"]

    rows = (await db.execute(
        select(Transaction)
        .options(*(undefer(getattr(Transaction, c)) for c in cols))
        .where(Transaction.notes == SEED_TAG)
        .order_by(Transaction.id)
    )).scalars().all()

    stats = {
        "transactions": len(rows),
        "with_any_image": 0,
        "without_images": 0,
        "with_multi_proofs": 0,
        "total_images": 0,
        "total_image_bytes": 0,
        "by_type": {},
        "by_status": {},
        "resolved_ok": 0,
        "distinct_checksums": set(),
    }

    for tx in rows:
        stats["by_type"][tx.type.value] = stats["by_type"].get(tx.type.value, 0) + 1
        stats["by_status"][tx.status.value] = stats["by_status"].get(tx.status.value, 0) + 1

        inline: list[str] = []
        for c in cols:
            v = getattr(tx, c)
            if not v:
                continue
            if c == "merchant_proofs":
                try:
                    items = json.loads(v)
                except (ValueError, TypeError):
                    failures.append(f"txn {tx.id}: merchant_proofs is not valid JSON")
                    continue
                if len(items) > 1:
                    stats["with_multi_proofs"] += 1
                inline.extend(items)
            else:
                inline.append(v)

        if inline:
            stats["with_any_image"] += 1
        else:
            stats["without_images"] += 1
        stats["total_images"] += len(inline)

        for v in inline:
            stats["total_image_bytes"] += len(v)
            if not storage.is_data_url(v):
                failures.append(f"txn {tx.id}: expected legacy base64, found {v[:24]!r}")
                continue
            try:
                stats["distinct_checksums"].add(storage.decode_data_url(v).sha256)
            except storage.StorageError as exc:
                failures.append(f"txn {tx.id}: fixture does not decode — {exc}")

        # Drive the ACTUAL serializer — this is the read path a client hits.
        try:
            payload = _t(tx, full=True)
        except Exception as exc:                             # noqa: BLE001
            failures.append(f"txn {tx.id}: _t() raised {type(exc).__name__}: {exc}")
            continue

        # With STORAGE_BACKEND=db, resolve_value must hand back every legacy value untouched.
        for c, key in (("merchant_proof", "merchantProof"), ("admin_proof", "adminProof"),
                       ("admin_bank_image", "adminBankImage")):
            src, out = getattr(tx, c), payload.get(key)
            if src and out != src:
                failures.append(f"txn {tx.id}: {key} was altered on the read path")
            elif src:
                stats["resolved_ok"] += 1

        if tx.merchant_proofs:
            src_list = json.loads(tx.merchant_proofs)
            out_list = payload.get("merchantProofs") or []
            if out_list != src_list:
                failures.append(f"txn {tx.id}: merchantProofs altered "
                                f"({len(src_list)} in, {len(out_list)} out)")
            else:
                stats["resolved_ok"] += len(out_list)

        # List mode must never expose the heavy columns.
        lite = _t(tx, full=False)
        for key in ("merchantProof", "merchantProofs", "adminProof", "adminBankImage"):
            if lite.get(key) is not None:
                failures.append(f"txn {tx.id}: {key} leaked into a LIST payload")

    # Cross-check against SQL, so a serializer bug cannot quietly agree with itself.
    for col in cols:
        n = (await db.execute(
            select(func.count()).select_from(Transaction)
            .where(Transaction.notes == SEED_TAG, getattr(Transaction, col).isnot(None))
        )).scalar()
        stats[f"rows_with_{col}"] = n

    return stats, failures


def report(stats: dict, failures: list[str]) -> bool:
    dup_saving = stats["total_images"] - len(stats["distinct_checksums"])
    print("\n" + "=" * 68)
    print(" STORAGE MIGRATION — DEMO VALIDATION REPORT")
    print("=" * 68)
    print(f"  STORAGE_BACKEND            : {settings.STORAGE_BACKEND!r} (must be 'db')")
    print(f"  transactions seeded        : {stats['transactions']}")
    print(f"    with at least one image  : {stats['with_any_image']}")
    print(f"    with no images           : {stats['without_images']}")
    print(f"    with multiple proofs     : {stats['with_multi_proofs']}")
    print(f"  total image files          : {stats['total_images']}")
    print(f"    distinct by checksum     : {len(stats['distinct_checksums'])}"
          f"  (dedupe will save {dup_saving})")
    print(f"  inline base64 volume       : {stats['total_image_bytes']:,} bytes")
    print()
    print("  per column:")
    for c in ("merchant_proof", "merchant_proofs", "admin_proof", "admin_bank_image"):
        print(f"    {c:20}: {stats.get(f'rows_with_{c}', 0)} row(s)")
    print()
    print(f"  by type   : {stats['by_type']}")
    print(f"  by status : {stats['by_status']}")
    print()
    print(f"  read-path values served    : {stats['resolved_ok']}")
    print(f"  read failures              : {len(failures)}")
    print(f"  rendering failures         : {sum('leaked' in f or 'altered' in f for f in failures)}")
    for f in failures[:25]:
        print(f"    - {f}")
    ok = not failures and stats["transactions"] == len(CASES)
    print()
    print(f"  OVERALL: {'PASS' if ok else 'FAIL'}")
    print("=" * 68)
    return ok


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify-only", action="store_true", help="report on existing seed data only")
    ap.add_argument("--purge", action="store_true", help="delete the seeded rows and exit")
    args = ap.parse_args()

    _guard()
    async with AsyncSessionLocal() as db:
        if args.purge:
            print(f"purged {await purge(db)} seeded row(s).")
            return
        if not args.verify_only:
            print("seeding storage-migration test corpus…")
            print(f"  seeded {await seed(db)} transaction(s).")
        stats, failures = await verify(db)
    sys.exit(0 if report(stats, failures) else 1)


if __name__ == "__main__":
    asyncio.run(main())
