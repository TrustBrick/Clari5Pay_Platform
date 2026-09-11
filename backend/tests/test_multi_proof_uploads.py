"""Tests for multi-image payment slips / proofs on deposits and withdrawals.

A payment is not always one transfer. It is split across accounts, paid in instalments, or
evidenced by a statement page plus a UTR screenshot — and a record that can only hold three files
(or, on the Admin side, one) stops being able to show how the money actually moved. So the file
COUNT is unrestricted. Everything else about an upload is not, and these tests are mostly about
that distinction:

  1. **No count limit, anywhere.** Ten slips on a deposit, ten receipts on a withdrawal payout,
     and every one of them is stored and returned.
  2. **"Unlimited" is about the number of files, never their size or type.** Each file is still
     validated individually — a .txt, an HTML data URL and an oversized image are all rejected no
     matter how many good files travel with them.
  3. **Uploads ADD, they never replace.** A second upload keeps the first; that is the whole
     point of attaching more evidence, and silently dropping what was already reviewed would be
     the worst possible failure here.
  4. **Attaching a file is not approving one.** The append route changes no status, so uploading
     can never stand in for the Admin's manual verification.
  5. **Back-compat holds in both directions.** The legacy single columns keep the FIRST file, so
     an older client still renders something; and a withdrawal completed before the array existed
     keeps its original receipt when a new one is appended.
  6. **A deposit's `admin_proof` is the account-details image that was SENT, not a payment
     receipt** — it must never be folded into the receipt gallery.
  7. **Authorization is unchanged.** Another merchant cannot attach to someone else's request,
     and a closed request accepts nothing further from the merchant.

The deferred proof columns are exercised against a real database rather than a stub, deliberately:
appending has to READ what is already stored, and a deferred column that is read without being
loaded raises MissingGreenlet under async SQLAlchemy — the exact failure that once 500'd every
transaction write. A stub session would hide it.

NO PRODUCTION REFERENCE IDS ARE CONSUMED — `_next_ref` is patched to a counter.

Run from the backend directory:

    python -m pytest tests/test_multi_proof_uploads.py -v
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import Transaction, TxStatus, TxType, User, UserRole
from app.schemas.schemas import CompleteRequest, ProofsAppend, SlipRequest


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    """A real, empty database built from the project's own models."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def safe_refs_and_no_cache(monkeypatch):
    """No production reference number is drawn, and no Redis connection is attempted."""
    counter = {"n": 0}

    async def fake_next_ref(db, kind, code=None):
        counter["n"] += 1
        return str(counter["n"])

    async def fake_cache_delete(key):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", fake_cache_delete)
    return counter


# ── Uploads ────────────────────────────────────────────────────────────────────────────────────

def _png(tag: str) -> str:
    """A distinct, genuinely decodable PNG data URL per tag, so files never collide by accident."""
    return "data:image/png;base64," + base64.b64encode(f"png-bytes-{tag}".encode()).decode()


def _pdf(tag: str) -> str:
    return "data:application/pdf;base64," + base64.b64encode(f"%PDF-{tag}".encode()).decode()


TEN_SLIPS = [_png(f"slip{i}") for i in range(10)]


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

async def _merchant(db: AsyncSession, uid: int = 7, name: str = "BELLAGIO") -> User:
    user = User(id=uid, username=f"op{uid}", name=name, role=UserRole.MERCHANT,
                hashed_password="x", email=f"op{uid}@test.local", merchant_role="DATA_OPERATOR")
    db.add(user)
    await db.flush()
    return user


def _admin(uid: int = 1) -> User:
    return User(id=uid, username="admin1", name="Admin", role=UserRole.ADMIN)


async def _tx(db: AsyncSession, *, ttype: TxType, status: TxStatus, merchant_id: int = 7,
              ref: str = "T1", **cols) -> Transaction:
    tx = Transaction(
        ref=ref, type=ttype, amount=5000.0, status=status, merchant_id=merchant_id,
        merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00", member_id="MBR1",
        deposit_type="BANK" if ttype.value.startswith("DEPOSIT") else None,
        created_at=datetime.utcnow(), **cols,
    )
    db.add(tx)
    await db.flush()
    return tx


async def _deposit_awaiting_slip(db: AsyncSession, **cols) -> Transaction:
    return await _tx(db, ttype=TxType.DEPOSIT_REQUEST, status=TxStatus.ACCOUNT_SUBMITTED, **cols)


async def _withdrawal_awaiting_payout(db: AsyncSession, **cols) -> Transaction:
    return await _tx(db, ttype=TxType.WITHDRAWAL_REQUEST, status=TxStatus.SLIP_SUBMITTED, **cols)


async def _stored(db: AsyncSession, tx: Transaction, column: str):
    """Read a deferred proof column straight from the row, bypassing any in-request caching."""
    await db.refresh(tx, attribute_names=[column])
    return getattr(tx, column)


# ═══ 1. No count limit ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_deposit_slip_accepts_far_more_than_the_old_limit_of_three(db):
    """Ten slips on one deposit — all ten are stored and returned, none is dropped."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)

    out = await txr.submit_slip("1", SlipRequest(merchantProofs=TEN_SLIPS, merchantRef="UTR1"),
                                None, db, merchant)

    assert len(out["merchantProofs"]) == 10
    assert out["merchantProofs"] == TEN_SLIPS
    assert json.loads(await _stored(db, tx, "merchant_proofs")) == TEN_SLIPS


@pytest.mark.asyncio
async def test_a_withdrawal_payout_accepts_a_receipt_per_transfer(db):
    """A payout split across ten transfers records ten receipts, not one."""
    await _merchant(db)
    tx = await _withdrawal_awaiting_payout(db)
    receipts = [_png(f"rcpt{i}") for i in range(10)]

    out = await txr.mark_done("1", None, CompleteRequest(adminProofs=receipts, adminUtr="UTR9"),
                              db, _admin())

    assert out["status"] == TxStatus.COMPLETED
    assert len(out["adminProofs"]) == 10
    assert json.loads(await _stored(db, tx, "admin_proofs")) == receipts


@pytest.mark.asyncio
async def test_the_count_limit_is_gone_from_validation_itself(db):
    """`_clean_proofs` — the one gate every upload path shares — accepts a large set outright."""
    assert len(txr._clean_proofs([_png(str(i)) for i in range(50)])) == 50


# ═══ 2. Per-file validation is untouched ════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [
    "data:text/plain;base64," + base64.b64encode(b"not an image").decode(),
    "data:text/html;base64," + base64.b64encode(b"<script>alert(1)</script>").decode(),
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode(),
])
def test_an_unsupported_file_is_rejected_however_many_good_files_accompany_it(bad):
    """One bad file fails the whole upload — a large set is not a way past the type whitelist."""
    with pytest.raises(HTTPException) as e:
        txr._clean_proofs([_png("ok1"), bad, _png("ok2")])
    assert e.value.status_code == 400


def test_an_oversized_file_is_still_rejected():
    """"No count limit" is not "no size limit": each file is measured on its own."""
    huge = "data:image/png;base64," + ("A" * (8 * 1024 * 1024))
    with pytest.raises(HTTPException) as e:
        txr._clean_proofs([_png("ok"), huge])
    assert e.value.status_code == 400
    assert "too large" in e.value.detail.lower()


def test_pdfs_remain_acceptable_proofs():
    assert len(txr._clean_proofs([_png("a"), _pdf("b")])) == 2


# ═══ 3. Uploads add, they never replace ═════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_appending_keeps_every_file_already_uploaded(db):
    """The second upload adds to the first instead of overwriting it."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)
    first = [_png("a"), _png("b")]
    await txr.submit_slip("1", SlipRequest(merchantProofs=first, merchantRef="UTR1"),
                          None, db, merchant)

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("c")]), None, db, merchant)

    assert out["merchantProofs"] == first + [_png("c")]


@pytest.mark.asyncio
async def test_an_admin_receipt_added_later_keeps_the_one_already_recorded(db):
    """A second receipt on a paid-out withdrawal never erases the first."""
    await _merchant(db)
    tx = await _withdrawal_awaiting_payout(db)
    await txr.mark_done("1", None, CompleteRequest(adminProof=_png("first"), adminUtr="U1"),
                        db, _admin())

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("second")]), None, db, _admin())

    assert out["adminProofs"] == [_png("first"), _png("second")]


@pytest.mark.asyncio
async def test_a_legacy_receipt_stored_before_the_array_existed_is_carried_forward(db):
    """A withdrawal completed under the old single-column code keeps its receipt when one is added."""
    await _merchant(db)
    tx = await _tx(db, ttype=TxType.WITHDRAWAL_REQUEST, status=TxStatus.COMPLETED,
                   admin_proof=_png("legacy"))          # admin_proofs deliberately NULL

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("new")]), None, db, _admin())

    assert out["adminProofs"] == [_png("legacy"), _png("new")]


@pytest.mark.asyncio
async def test_re_uploading_the_same_file_does_not_list_it_twice(db):
    """Object-storage keys are content-addressed, so a duplicate would otherwise show up twice."""
    merchant = await _merchant(db)
    await _deposit_awaiting_slip(db)
    await txr.submit_slip("1", SlipRequest(merchantProofs=[_png("a")], merchantRef="U1"),
                          None, db, merchant)

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("a"), _png("b")]),
                                  None, db, merchant)

    assert out["merchantProofs"] == [_png("a"), _png("b")]


@pytest.mark.asyncio
async def test_a_recheck_still_clears_the_slip_for_re_upload(db):
    """The one place proofs are deliberately cleared keeps doing exactly that."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)
    await txr.submit_slip("1", SlipRequest(merchantProofs=TEN_SLIPS[:3], merchantRef="U1"),
                          None, db, merchant)

    out = await txr.recheck_payment("1", None, db, _admin())

    assert out["merchantProofs"] is None and out["merchantProof"] is None
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED


# ═══ 4. Attaching a file is not approving one ═══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_appending_slips_never_moves_the_request_forward(db):
    """Uploading more evidence leaves the review gate exactly where it was."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)
    await txr.submit_slip("1", SlipRequest(merchantProofs=[_png("a")], merchantRef="U1"),
                          None, db, merchant)
    before = tx.status
    assert before == TxStatus.SUPERVISOR_REVIEW

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("b")]), None, db, merchant)

    assert out["status"] == before
    assert out["approvedBy"] is None and out["processedBy"] is None
    assert out["adminActionAt"] is None


@pytest.mark.asyncio
async def test_an_admin_appending_receipts_does_not_complete_the_withdrawal(db):
    """Evidence lands on the record; completion still requires the Admin's own action."""
    await _merchant(db)
    tx = await _withdrawal_awaiting_payout(db)

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("r")]), None, db, _admin())

    assert out["status"] == TxStatus.SLIP_SUBMITTED
    assert len(out["adminProofs"]) == 1


@pytest.mark.asyncio
async def test_every_upload_is_recorded_in_the_audit_trail(db):
    """Attaching evidence is an auditable act, like every other change to a request."""
    merchant = await _merchant(db)
    await _deposit_awaiting_slip(db)

    await txr.append_proofs("1", ProofsAppend(proofs=[_png("a"), _png("b")]), None, db, merchant)

    from sqlalchemy import select
    from app.models.models import AuditLog
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.action_type == "PROOF_UPLOADED"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].entity_id == "T1"
    assert rows[0].new_value == "2", "the audit records how many files the request now holds"


# ═══ 5. Back-compat of the legacy single columns ════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_single_columns_keep_the_first_file_for_older_clients(db):
    """`merchantProof` / `adminProof` still render something on a client that knows only them."""
    merchant = await _merchant(db)
    dep = await _deposit_awaiting_slip(db)
    out = await txr.submit_slip("1", SlipRequest(merchantProofs=TEN_SLIPS, merchantRef="U1"),
                                None, db, merchant)
    assert out["merchantProof"] == TEN_SLIPS[0]

    wd = await _withdrawal_awaiting_payout(db, ref="T2")
    out = await txr.mark_done("2", None,
                              CompleteRequest(adminProofs=[_png("r1"), _png("r2")], adminUtr="U2"),
                              db, _admin())
    assert out["adminProof"] == _png("r1")


@pytest.mark.asyncio
async def test_sending_both_the_single_field_and_the_array_does_not_store_it_twice(db):
    """A client that fills the legacy field as well as the array gets one copy, not two."""
    merchant = await _merchant(db)
    await _deposit_awaiting_slip(db)

    out = await txr.submit_slip(
        "1", SlipRequest(merchantProof=_png("a"), merchantProofs=[_png("a"), _png("b")],
                         merchantRef="U1"), None, db, merchant)

    assert out["merchantProofs"] == [_png("a"), _png("b")]


# ═══ 6. A deposit's admin_proof is not a payment receipt ════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_account_details_image_sent_on_a_deposit_never_joins_the_receipt_gallery(db):
    """On a deposit `admin_proof` holds the account card that was SENT — evidence of nothing paid.

    Folding it into the Admin's payment-proof set would misrepresent what it is, so an append on
    a deposit starts a fresh set and leaves the account image where it belongs.
    """
    merchant = await _merchant(db)
    account_card = _png("account-details")
    tx = await _deposit_awaiting_slip(db, admin_proof=account_card)

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("something-else")]),
                                  None, db, _admin())

    assert out["adminProofs"] == [_png("something-else")]
    assert account_card not in out["adminProofs"]
    # …and the account card itself is untouched, so the merchant's payment details still render.
    assert await _stored(db, tx, "admin_proof") == account_card


# ═══ 7. Authorization and workflow gates ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_another_merchant_cannot_attach_to_someone_elses_request(db):
    await _merchant(db, uid=7)
    other = await _merchant(db, uid=8, name="OTHER")
    await _deposit_awaiting_slip(db, merchant_id=7)

    with pytest.raises(HTTPException) as e:
        await txr.append_proofs("1", ProofsAppend(proofs=[_png("x")]), None, db, other)
    assert e.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [TxStatus.COMPLETED, TxStatus.REJECTED, TxStatus.CANCELLED,
                                    TxStatus.DEPOSITED, TxStatus.SA_REJECTED])
async def test_a_merchant_cannot_attach_to_a_closed_request(db, closed):
    """Once a request is finished its evidence is sealed; a correction goes through Recheck."""
    merchant = await _merchant(db)
    await _tx(db, ttype=TxType.DEPOSIT_REQUEST, status=closed)

    with pytest.raises(HTTPException) as e:
        await txr.append_proofs("1", ProofsAppend(proofs=[_png("x")]), None, db, merchant)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_an_admin_may_still_attach_a_receipt_after_completion(db):
    """The Admin records how a payout was made, and that can be evidenced after the fact."""
    await _merchant(db)
    await _tx(db, ttype=TxType.WITHDRAWAL_REQUEST, status=TxStatus.COMPLETED)

    out = await txr.append_proofs("1", ProofsAppend(proofs=[_png("late")]), None, db, _admin())

    assert out["adminProofs"] == [_png("late")]


@pytest.mark.asyncio
async def test_an_empty_upload_is_refused(db):
    merchant = await _merchant(db)
    await _deposit_awaiting_slip(db)

    with pytest.raises(HTTPException) as e:
        await txr.append_proofs("1", ProofsAppend(proofs=[]), None, db, merchant)
    assert e.value.status_code == 400


# ═══ Serialization ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_detail_payload_carries_both_full_sets(db):
    """Both galleries reach the client from one detail fetch, complete."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)
    await txr.submit_slip("1", SlipRequest(merchantProofs=TEN_SLIPS[:4], merchantRef="U1"),
                          None, db, merchant)
    await txr.append_proofs("1", ProofsAppend(proofs=[_png("admin1"), _png("admin2")]),
                            None, db, _admin())

    payload = await txr.get_transaction_detail("1", db, _admin())

    assert payload["merchantProofs"] == TEN_SLIPS[:4]
    assert payload["adminProofs"] == [_png("admin1"), _png("admin2")]


@pytest.mark.asyncio
async def test_list_payloads_still_omit_the_heavy_proof_arrays(db):
    """The arrays are detail-only — a list row must not start carrying them."""
    merchant = await _merchant(db)
    tx = await _deposit_awaiting_slip(db)
    await txr.submit_slip("1", SlipRequest(merchantProofs=TEN_SLIPS, merchantRef="U1"),
                          None, db, merchant)

    row = txr._t(tx, full=False)

    assert row["merchantProofs"] is None and row["adminProofs"] is None


def test_a_corrupt_stored_array_does_not_break_the_response():
    """A malformed value degrades to "no files", never to a 500 on the detail view."""
    assert txr._parse_proofs("{not json") == []
    assert txr._resolve_proofs("{not json") is None
