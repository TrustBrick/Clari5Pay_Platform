"""Tests for the account payment image — configured once per receiving account, shown per deposit.

The Admin uploads a bank / payment image against a managed account in Account Management. Every
deposit routed to that account then shows the merchant that account's image with its payment
instructions. The properties worth pinning down:

  1. **Configured once, reused everywhere.** One image per account; nothing is uploaded or copied
     per transaction, and a deposit's own row carries no image of it.
  2. **A merchant only ever receives the image of the account assigned to THEIR deposit.** The
     account is derived from the transaction server-side; there is no merchant route that names an
     account, and another merchant's transaction is refused outright.
  3. **The image never influences allocation.** The engine picks the same account with or without
     images, and an account with no image is still allocated.
  4. **Upload security holds.** Only genuine JPG/JPEG/PNG/WEBP data URLs within 5 MB whose bytes
     match their declared type are accepted; URLs and storage references are refused, so a caller
     can never point an account at an arbitrary stored object.
  5. **History is untouched.** Uploading, replacing or removing an image changes no transaction
     row, and historical Bank Transfer deposits still read as they always have (new Bank Transfer
     requests are refused — see tests/test_bank_transfer_retired.py).

NO PRODUCTION REFERENCE IDS ARE CONSUMED — ``_next_ref`` is patched to a counter.

Run from the backend directory:

    python -m pytest tests/test_account_payment_image.py -v
"""
from __future__ import annotations

import base64
import inspect
import json
from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import accounts as acr
from app.api.routes import transactions as txr
from app.core import deps, storage
from app.core.uploads import MAX_UPLOAD_BYTES
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountPaymentImage, AccountType, AuditLog, Transaction, TxStatus, TxType,
    User, UserRole,
)
from app.schemas.schemas import AccountPaymentImageUpload, DepositCreate
from app.services import account_payment_image as payimg
from app.services import deposit_allocation as alloc


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
    """No production reference number is drawn, no Redis connection is attempted, and object
    storage is in its default (database) mode unless a test opts into the fake bucket."""
    counter = {"n": 0}

    async def fake_next_ref(db, kind, code=None):
        counter["n"] += 1
        return str(counter["n"])

    async def fake_cache_delete(key):
        return None

    async def fake_cache_get(key):
        return None

    async def fake_cache_set(key, value, ttl=None):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", fake_cache_delete)
    monkeypatch.setattr(acr, "cache_delete", fake_cache_delete)
    monkeypatch.setattr(acr, "cache_get", fake_cache_get)
    monkeypatch.setattr(acr, "cache_set", fake_cache_set)
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "db", raising=False)
    return counter


class _FakeS3:
    """Just enough of an S3 client for store + presign."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://fake-bucket.example/{Params['Key']}?sig=abc"


@pytest.fixture
def s3(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(storage.settings, "S3_BUCKET", "test-bucket", raising=False)
    monkeypatch.setattr(storage.settings, "S3_PREFIX", "uploads", raising=False)
    storage._client.cache_clear()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    return fake


class _Req:
    """The route only reads ``request.client.host`` for the audit trail."""
    class client:  # noqa: N801 - mirrors starlette's attribute name
        host = "127.0.0.1"


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 "


def _png(tag: str) -> str:
    """A distinct PNG data URL whose bytes really start with the PNG signature."""
    return "data:image/png;base64," + base64.b64encode(PNG_MAGIC + f"img-{tag}".encode()).decode()


def _jpeg(tag: str) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(JPEG_MAGIC + f"img-{tag}".encode()).decode()


def _webp(tag: str) -> str:
    return "data:image/webp;base64," + base64.b64encode(WEBP_BYTES + f"img-{tag}".encode()).decode()


def _admin(uid: int = 1) -> User:
    return User(id=uid, username="admin1", name="Admin One", role=UserRole.ADMIN)


async def _merchant(db: AsyncSession, uid: int = 7, role: str = "DATA_OPERATOR", name: str = "BELLAGIO") -> User:
    user = User(
        id=uid, username=f"op{uid}", name=name, role=UserRole.MERCHANT, hashed_password="x",
        email=f"op{uid}@test.local", merchant_role=role, pay_in="DEP",
    )
    db.add(user)
    await db.flush()
    return user


async def _account(db: AsyncSession, ref: str, *, name: str, bank: str = "HDFC Bank",
                   credit: float = 100000.0, status: str = "ACTIVE",
                   atype: AccountType = AccountType.CURRENT) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name, account_number=f"AC{ref}", ifsc_code="HDFC0001234",
        bank_name=bank, branch="Mumbai", account_type=atype, status=status,
        created_date=date.today(), created_time="10:00:00", highest_credit=credit,
        highest_debit=50000.0, debit_alert_threshold=50000.0,
    )
    db.add(acc)
    await db.flush()
    return acc


def _payload(amount: float, **kw) -> DepositCreate:
    base = dict(
        amount=amount, depositType="IMPS", memberName="Test Member", memberId="MBR1",
        accountHolder="Test Member", accountNumber="999", ifsc="HDFC0001234", bankName="HDFC Bank",
        accountType="CURRENT",
    )
    base.update(kw)
    return DepositCreate(**base)


async def _upload(db, ref: str, image: str):
    return await acr.put_account_payment_image(ref, AccountPaymentImageUpload(image=image), _Req(), db, _admin())


async def _raw_transactions(db: AsyncSession) -> list[dict]:
    """Every transaction column, deferred image columns included, straight from the table."""
    rows = (await db.execute(select(Transaction.__table__).order_by(Transaction.__table__.c.id))).mappings().all()
    return [dict(r) for r in rows]


# ── A. Admin uploads an image to Account A ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_uploads_an_image_against_the_account(db):
    await _account(db, "ACC1", name="Account A")

    out = await _upload(db, "ACC1", _png("A"))

    assert out["hasImage"] is True and out["image"] == _png("A")
    assert out["contentType"] == "image/png" and out["uploadedBy"] == "Admin One"
    row = (await db.execute(select(AccountPaymentImage))).scalars().one()
    assert row.account_ref == "ACC1"
    audit = (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_PAYMENT_IMAGE_UPLOADED"))).scalars().one()
    assert audit.entity_id == "ACC1"
    # And Admin can read it back, and the account list reports it.
    assert (await acr.get_account_payment_image("ACC1", db, _admin()))["image"] == _png("A")
    listed = await acr.list_accounts(None, db, _admin())
    assert listed[0]["hasPaymentImage"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_png, _jpeg, _webp])
async def test_every_supported_image_type_is_accepted(db, make):
    await _account(db, "ACC1", name="Account A")
    assert (await _upload(db, "ACC1", make("x")))["hasImage"] is True


@pytest.mark.asyncio
async def test_an_unknown_account_is_404(db):
    with pytest.raises(HTTPException) as e:
        await _upload(db, "ACC404", _png("A"))
    assert e.value.status_code == 404


# ── B / C / D. Deposit auto-allocated to A shows A's image; nothing uploaded per transaction ─────

@pytest.mark.asyncio
async def test_an_auto_allocated_deposit_shows_its_accounts_image_with_no_per_transaction_upload(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await txr.create_deposit(_payload(50000), db, merchant)

    # B — the engine ran normally and allocated Account A.
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"
    # C — the merchant opening the deposit receives Account A's image with the account details.
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"] == {
        "accountRef": "ACC1", "hasImage": True, "image": _png("A"), "contentType": "image/png"}
    assert detail["allocationSnapshot"]["referenceNumber"] == "ACC1"
    # D — nothing was attached to the transaction: no per-transaction image column was written.
    raw = (await _raw_transactions(db))[0]
    assert raw["admin_proof"] is None and raw["admin_bank_image"] is None and raw["admin_proofs"] is None
    assert _png("A") not in json.dumps(raw, default=str)
    assert (await db.execute(select(func.count()).select_from(AccountPaymentImage))).scalar() == 1


@pytest.mark.asyncio
async def test_a_upi_deposit_shows_the_parent_accounts_image(db):
    """UPI is the form's DEFAULT type — exercise it, not just the bank rails."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await txr.create_deposit(_payload(1000, depositType="UPI", senderUpiId="m@ybl"), db, merchant)
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"]["image"] == _png("A")


# ── E. Account B's image is never shown for Account A (and vice versa) ─────────────────────────

@pytest.mark.asyncio
async def test_each_deposit_receives_only_its_own_accounts_image(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A", bank="HDFC Bank", credit=200000.0)
    await _account(db, "ACC2", name="Account B", bank="Bank of Baroda", credit=300000.0)
    await _upload(db, "ACC1", _png("A"))
    await _upload(db, "ACC2", _png("B"))

    to_b = await txr.create_deposit(_payload(40000, notes="Use Bank of Baroda"), db, merchant)
    to_a = await txr.create_deposit(_payload(40000, memberId="MBR2", notes="Use HDFC Bank"), db, merchant)
    assert (to_a["adminRef"], to_b["adminRef"]) == ("ACC1", "ACC2")

    detail_a = await txr.get_transaction_detail(to_a["id"], db, merchant)
    detail_b = await txr.get_transaction_detail(to_b["id"], db, merchant)
    assert detail_a["accountPaymentImage"]["image"] == _png("A")
    assert detail_b["accountPaymentImage"]["image"] == _png("B")
    # Not anywhere in the payload — not just not in the one field.
    assert _png("B") not in json.dumps(detail_a, default=str)
    assert _png("A") not in json.dumps(detail_b, default=str)


@pytest.mark.asyncio
async def test_another_merchant_cannot_open_the_deposit_or_its_image(db):
    owner = await _merchant(db, uid=7)
    stranger = await _merchant(db, uid=8, name="OTHER BIZ")
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))
    out = await txr.create_deposit(_payload(1000), db, owner)

    with pytest.raises(HTTPException) as e:
        await txr.get_transaction_detail(out["id"], db, stranger)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_the_account_image_routes_are_admin_only(db):
    """Merchants have no route to an account's image — every one depends on get_current_admin."""
    for fn in (acr.get_account_payment_image, acr.put_account_payment_image, acr.delete_account_payment_image):
        gates = [p.default.dependency for p in inspect.signature(fn).parameters.values()
                 if hasattr(p.default, "dependency")]
        assert deps.get_current_admin in gates, fn.__name__
    merchant = await _merchant(db)
    with pytest.raises(HTTPException) as e:
        await deps.get_current_admin(merchant)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_non_account_deposit_types_show_no_account_image(db):
    """Cash, Crypto, Card and CDM keep their screens — even when an admin_ref names an imaged account."""
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))
    for i, dtype in enumerate(("CASH", "CRYPTO", "CARD", "CDM"), start=1):
        tx = Transaction(ref=f"H{i}", type=TxType.DEPOSIT_REQUEST, amount=1000, status=TxStatus.ACCOUNT_SUBMITTED,
                         merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
                         deposit_type=dtype, admin_ref="ACC1", created_at=datetime.utcnow())
        db.add(tx)
        await db.flush()
        assert await payimg.for_transaction(db, tx) is None, dtype
    # A withdrawal is never shown one either.
    wd = Transaction(ref="W1", type=TxType.WITHDRAWAL_REQUEST, amount=1000, status=TxStatus.ACCOUNT_SUBMITTED,
                     merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
                     payout_mode="IMPS", admin_ref="ACC1", created_at=datetime.utcnow())
    db.add(wd)
    await db.flush()
    assert await payimg.for_transaction(db, wd) is None


@pytest.mark.asyncio
async def test_an_unplaced_deposit_has_no_account_and_no_image(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A", credit=1000.0)
    await _upload(db, "ACC1", _png("A"))
    out = await txr.create_deposit(_payload(5000), db, merchant)      # over the ceiling
    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"] is None


# ── F. Replacing works ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replacing_the_image_updates_it_for_every_deposit_on_the_account(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("old"))
    out = await txr.create_deposit(_payload(1000), db, merchant)

    replaced = await _upload(db, "ACC1", _jpeg("new"))

    assert replaced["image"] == _jpeg("new") and replaced["contentType"] == "image/jpeg"
    assert (await db.execute(select(func.count()).select_from(AccountPaymentImage))).scalar() == 1
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"]["image"] == _jpeg("new")
    assert (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_PAYMENT_IMAGE_REPLACED"))).scalars().one()


# ── G. Removing works ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_removing_the_image_leaves_the_account_eligible_and_the_deposit_explains_it(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await acr.delete_account_payment_image("ACC1", _Req(), db, _admin())

    assert out["hasImage"] is False and out["image"] is None
    assert (await db.execute(select(func.count()).select_from(AccountPaymentImage))).scalar() == 0
    assert (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_PAYMENT_IMAGE_REMOVED"))).scalars().one()
    # Removing twice is harmless and audits nothing further.
    await acr.delete_account_payment_image("ACC1", _Req(), db, _admin())
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action_type == "ACCOUNT_PAYMENT_IMAGE_REMOVED"))).scalar() == 1

    dep = await txr.create_deposit(_payload(1000), db, merchant)
    assert dep["adminRef"] == "ACC1"
    detail = await txr.get_transaction_detail(dep["id"], db, merchant)
    assert detail["accountPaymentImage"] == {"accountRef": "ACC1", "hasImage": False, "image": None, "contentType": None}


# ── H. The image never influences allocation ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_account_with_no_image_is_still_allocated(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="No image")
    out = await txr.create_deposit(_payload(1000), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"


@pytest.mark.asyncio
async def test_images_do_not_change_which_account_the_engine_chooses(db):
    """Nearest suitable capacity wins (ACC1). Giving ONLY the other account an image changes nothing."""
    await _account(db, "ACC1", name="nearer", credit=60000.0)
    await _account(db, "ACC2", name="further", credit=300000.0)

    before = await alloc.allocate_deposit_account(db, amount=45000, member_id="OLDM")
    await _upload(db, "ACC2", _png("B"))
    after_other = await alloc.allocate_deposit_account(db, amount=45000, member_id="OLDM")
    await _upload(db, "ACC1", _png("A"))
    await acr.delete_account_payment_image("ACC2", _Req(), db, _admin())
    after_swap = await alloc.allocate_deposit_account(db, amount=45000, member_id="OLDM")

    refs = {r.account.reference_number for r in (before, after_other, after_swap)}
    assert refs == {before.account.reference_number}
    assert before.reason == after_other.reason == after_swap.reason


def test_the_allocation_engine_does_not_read_the_image():
    source = inspect.getsource(alloc)
    assert "AccountPaymentImage" not in source and "payment_image" not in source


# ── Upload security ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad, why", [
    ("data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 x").decode(), "pdf not an image"),
    ("data:text/html;base64," + base64.b64encode(b"<script>x</script>").decode(), "html"),
    ("data:image/gif;base64," + base64.b64encode(b"GIF89a....").decode(), "gif not whitelisted"),
    ("data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(), "svg"),
    ("data:image/png;base64," + base64.b64encode(b"<html>not a png</html>").decode(), "png label, html bytes"),
    ("data:image/png;base64," + base64.b64encode(JPEG_MAGIC + b"jpeg").decode(), "png label, jpeg bytes"),
    ("data:image/jpeg;base64," + base64.b64encode(b"%PDF-1.4").decode(), "jpeg label, pdf bytes"),
    ("data:image/png;base64,@@not-base64@@", "malformed base64"),
    ("data:image/png;base64,", "empty"),
    ("https://evil.example/qr.png", "remote url"),
    ("storage://uploads/merchant_proofs/ab/0123456789abcdef.png", "someone else's stored object"),
    ("", "nothing"),
])
async def test_unsafe_uploads_are_rejected_and_nothing_is_stored(db, bad, why):
    await _account(db, "ACC1", name="Account A")
    with pytest.raises(HTTPException) as e:
        await _upload(db, "ACC1", bad)
    assert e.value.status_code == 400, why
    assert (await db.execute(select(func.count()).select_from(AccountPaymentImage))).scalar() == 0


@pytest.mark.asyncio
async def test_the_existing_size_limit_is_enforced(db):
    await _account(db, "ACC1", name="Account A")
    big = "data:image/png;base64," + base64.b64encode(PNG_MAGIC + b"\0" * MAX_UPLOAD_BYTES).decode()
    with pytest.raises(HTTPException) as e:
        await _upload(db, "ACC1", big)
    assert e.value.status_code == 400 and "too large" in e.value.detail


@pytest.mark.asyncio
async def test_a_rejected_replacement_keeps_the_existing_image(db):
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("good"))
    with pytest.raises(HTTPException):
        await _upload(db, "ACC1", "data:image/png;base64," + base64.b64encode(b"<html>").decode())
    assert (await acr.get_account_payment_image("ACC1", db, _admin()))["image"] == _png("good")


@pytest.mark.asyncio
async def test_with_object_storage_the_image_is_a_private_object_served_by_presigned_url(db, s3):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    row = (await db.execute(select(AccountPaymentImage))).scalars().one()
    assert row.image.startswith("storage://uploads/account_payment_image/")
    assert "base64" not in row.image and len(s3.objects) == 1

    out = await txr.create_deposit(_payload(1000), db, merchant)
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"]["image"].startswith("https://fake-bucket.example/uploads/account_payment_image/")
    assert "storage://" not in json.dumps(detail, default=str)


# ── I. Historical transactions remain unchanged ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_image_changes_never_modify_any_transaction_row(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    historical = Transaction(
        ref="DEP_HIST", type=TxType.DEPOSIT_REQUEST, amount=25000, status=TxStatus.DEPOSITED,
        merchant_id=merchant.id, merchant_name="BELLAGIO", tx_date=date(2026, 8, 1), tx_time="10:00:00",
        deposit_type="BANK", admin_ref="ACC1", member_id="MBR1",
        admin_bank_details="Account Name: Account A\nBank: HDFC Bank", admin_proof=_png("sent-card"),
        created_at=datetime(2026, 8, 1, 4, 30),
    )
    db.add(historical)
    await db.flush()
    snapshot = await _raw_transactions(db)

    await _upload(db, "ACC1", _png("v1"))
    await _upload(db, "ACC1", _png("v2"))
    detail = await txr.get_transaction_detail(f"TXN{historical.id:03d}", db, merchant)
    await acr.delete_account_payment_image("ACC1", _Req(), db, _admin())

    assert await _raw_transactions(db) == snapshot
    # The historical Bank Transfer deposit still reads exactly as it was recorded.
    assert detail["depositType"] == "BANK"
    assert detail["adminProof"] == _png("sent-card")
    assert detail["adminBankDetails"] == historical.admin_bank_details


# ── J. Manual assignment: the Admin picks the account, the image follows the account ───────────
# The per-deposit image upload is gone. Whether the engine placed the deposit or an Admin sent the
# account by hand, the merchant receives the image configured on THAT account and nothing else.

async def _unplaced(db, merchant, member_id: str) -> dict:
    """A deposit the engine could not place (no accounts exist yet) — the manual-send case."""
    out = await txr.create_deposit(_payload(40000, memberId=member_id), db, merchant)
    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT and out["adminRef"] is None
    return out


async def _send(db, tx_id: str, ref: str, **kw):
    return await txr.account_submit(
        tx_id, txr.AccountSubmitRequest(adminRef=ref, adminBankDetails=f"Account {ref}", **kw), db, _admin())


@pytest.mark.asyncio
async def test_manual_assignment_shows_exactly_the_assigned_accounts_image(db):
    merchant = await _merchant(db)
    first = await _unplaced(db, merchant, "MBR1")
    second = await _unplaced(db, merchant, "MBR2")
    await _account(db, "ACC0000013", name="Account A", bank="HDFC Bank")
    await _account(db, "ACC0000014", name="Account B", bank="ICICI Bank")
    await _upload(db, "ACC0000013", _png("HDFC-A"))
    await _upload(db, "ACC0000014", _png("ICICI-B"))

    # Tests 3-5: assigned to A → merchant receives A, never B.
    await _send(db, first["id"], "ACC0000013")
    detail_a = await txr.get_transaction_detail(first["id"], db, merchant)
    assert detail_a["accountPaymentImage"] == {
        "accountRef": "ACC0000013", "hasImage": True, "image": _png("HDFC-A"), "contentType": "image/png"}
    assert _png("ICICI-B") not in json.dumps(detail_a, default=str)

    # Tests 6-8: assigned to B → merchant receives B, never A.
    await _send(db, second["id"], "ACC0000014")
    detail_b = await txr.get_transaction_detail(second["id"], db, merchant)
    assert detail_b["accountPaymentImage"]["accountRef"] == "ACC0000014"
    assert detail_b["accountPaymentImage"]["image"] == _png("ICICI-B")
    assert _png("HDFC-A") not in json.dumps(detail_b, default=str)

    # And nothing was copied onto either deposit row.
    for raw in await _raw_transactions(db):
        assert raw["admin_proof"] is None and raw["admin_bank_image"] is None


@pytest.mark.asyncio
async def test_manual_send_to_an_account_with_no_image_still_works_and_says_so(db):
    merchant = await _merchant(db)
    out = await _unplaced(db, merchant, "MBR1")
    await _account(db, "ACC1", name="No image")

    sent = await _send(db, out["id"], "ACC1")

    assert sent["status"] == TxStatus.ACCOUNT_SUBMITTED and sent["adminRef"] == "ACC1"
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountPaymentImage"] == {"accountRef": "ACC1", "hasImage": False, "image": None, "contentType": None}
    assert detail["adminBankDetails"] == "Account ACC1"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["adminProof", "adminBankImage"])
async def test_a_per_deposit_image_is_refused_and_nothing_is_written(db, field):
    """Test 13, server side: the removed upload cannot be reached by calling the API directly."""
    merchant = await _merchant(db)
    out = await _unplaced(db, merchant, "MBR1")
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))
    before = await _raw_transactions(db)

    with pytest.raises(HTTPException) as e:
        await _send(db, out["id"], "ACC1", **{field: _png("smuggled")})

    assert e.value.status_code == 400 and "Account Management" in e.value.detail
    assert await _raw_transactions(db) == before           # status, admin_ref, image columns all untouched
    # Also refused with no account named at all (the old "image only" send).
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], txr.AccountSubmitRequest(**{field: _png("smuggled")}), db, _admin())
    assert e.value.status_code == 400
    assert await _raw_transactions(db) == before


@pytest.mark.asyncio
async def test_a_replaced_image_is_what_later_deposits_on_the_account_receive(db):
    """Test 10: replace once in Account Management — no re-upload per deposit."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("v1"))
    earlier = await txr.create_deposit(_payload(1000, memberId="MBR1"), db, merchant)
    assert (await txr.get_transaction_detail(earlier["id"], db, merchant))["accountPaymentImage"]["image"] == _png("v1")

    await _upload(db, "ACC1", _png("v2"))
    later = await txr.create_deposit(_payload(1000, memberId="MBR2"), db, merchant)

    detail = await txr.get_transaction_detail(later["id"], db, merchant)
    assert later["adminRef"] == "ACC1" and detail["accountPaymentImage"]["image"] == _png("v2")
    assert _png("v1") not in json.dumps(detail, default=str)


def test_the_merchant_path_never_takes_an_account_reference():
    """Test 11: nothing a merchant can call accepts an account reference to resolve an image by.

    The detail route takes only the transaction id; the resolver takes only the transaction. The
    account-image routes that DO take a reference are Admin-only (tested above)."""
    detail_params = set(inspect.signature(txr.get_transaction_detail).parameters)
    assert detail_params == {"tx_id", "db", "current_user"}
    assert set(inspect.signature(payimg.for_transaction).parameters) == {"db", "tx"}
