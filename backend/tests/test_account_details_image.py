"""Tests for the Account Details Image — uploaded once per receiving account, the ONLY account
details a merchant ever receives for a deposit.

The Admin uploads one Account Details Image against a managed account in Account Management. Every
deposit routed to that account — automatically by the allocation engine, or by an Admin's manual
assignment — sends the merchant that account's image AND NOTHING ELSE about the account. The
properties worth pinning down:

  1. **Uploaded once, reused everywhere.** One image per account; nothing is uploaded or copied
     per transaction, and a deposit's own row carries no image of it.
  2. **The image IS the account details.** For a deposit that uses one, no merchant-side payload
     carries the bank name, account number, IFSC, branch, UPI ID or allocation snapshot — as text
     or in any other form. An Admin's own detail view still carries that internal record.
  3. **No image means no account details at all.** Nothing is rebuilt from the account record as a
     fallback: the merchant is told plainly that none is configured.
  4. **A merchant only ever receives the image of the account assigned to THEIR deposit.** The
     account is derived from the transaction server-side; there is no merchant route that names an
     account, and another merchant's transaction is refused outright.
  5. **The image never influences allocation.** The engine picks the same account with or without
     images, and an account with no image is still allocated.
  6. **Upload security holds.** Only genuine JPG/JPEG/PNG/WEBP data URLs within 5 MB whose bytes
     match their declared type are accepted; URLs and storage references are refused, so a caller
     can never point an account at an arbitrary stored object.
  7. **History is untouched.** Uploading, replacing or removing an image changes no transaction
     row, and a historical deposit's stored columns are exactly as they were recorded (new Bank
     Transfer requests are refused — see tests/test_bank_transfer_retired.py).

NO PRODUCTION REFERENCE IDS ARE CONSUMED — ``_next_ref`` is patched to a counter.

Run from the backend directory:

    python -m pytest tests/test_account_details_image.py -v
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
    AccountMaster, AccountDetailsImage, AccountType, AuditLog, Transaction, TxStatus, TxType,
    User, UserRole,
)
from app.schemas.schemas import AccountDetailsImageUpload, DepositCreate
from app.services import account_details_image as detimg
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
                   atype: AccountType = AccountType.CURRENT,
                   cdm_limit: float = 0.0) -> AccountMaster:
    # `cdm_limit` defaults to 0 — unconfigured — because that is what an account looks like until
    # an Admin grants it CDM capacity, and no non-CDM test should need to think about it.
    acc = AccountMaster(
        reference_number=ref, account_name=name, account_number=f"AC{ref}", ifsc_code="HDFC0001234",
        bank_name=bank, branch="Mumbai", account_type=atype, status=status,
        created_date=date.today(), created_time="10:00:00", highest_credit=credit,
        highest_debit=50000.0, debit_alert_threshold=50000.0, cdm_daily_limit=cdm_limit,
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
    return await acr.put_account_details_image(ref, AccountDetailsImageUpload(image=image), _Req(), db, _admin())


def _image(ref: str, image: str, content_type: str = "image/png",
           kind: str = detimg.ACCOUNT_KIND) -> dict:
    """What a deposit's detail payload carries when its account HAS the image of ITS OWN kind."""
    return {"accountRef": ref, "kind": kind, "hasImage": True, "image": image,
            "contentType": content_type, "message": None}


def _no_image(ref: str, kind: str = detimg.ACCOUNT_KIND) -> dict:
    """...and when it has none: the plain statement for that kind, never reconstructed account
    details and never the other image."""
    return {"accountRef": ref, "kind": kind, "hasImage": False, "image": None, "contentType": None,
            "message": detimg._spec(kind)[3]}


async def _upload_cdm(db, ref: str, image: str):
    return await acr.put_cdm_account_details_image(ref, AccountDetailsImageUpload(image=image),
                                                   _Req(), db, _admin())


def _no_account_text(payload: dict) -> bool:
    """No account-details text of any kind reached the merchant on this payload."""
    return (payload.get("adminBankDetails") is None
            and payload.get("adminUpiId") is None
            and payload.get("allocationSnapshot") is None)


# A receiving account whose every identifying field differs from the SENDER details _payload
# carries, so a sweep of the payload can tell the two apart. The sender is HDFC Bank / HDFC0001234
# / A-C 999; nothing below is shared with it.
RECEIVER = dict(name="Receiving Alpha", bank="Bank of Baroda")
RECEIVER_FIELDS = ("Receiving Alpha", "Bank of Baroda", "BARB0TESTAC", "Andheri East")


async def _receiver(db: AsyncSession, ref: str = "ACC1", **kw) -> AccountMaster:
    """The account the deposit is paid INTO, identifiable in a payload by any of its own fields."""
    acc = await _account(db, ref, **{**RECEIVER, **kw})
    acc.account_number = f"AC{ref}"
    acc.ifsc_code = "BARB0TESTAC"
    acc.branch = "Andheri East"
    await db.flush()
    return acc


def _mentions_receiver(payload: dict) -> list[str]:
    """Which of the receiving account's own details leaked into a merchant payload (none may)."""
    blob = json.dumps(payload, default=str)
    return [f for f in RECEIVER_FIELDS if f in blob]


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
    row = (await db.execute(select(AccountDetailsImage))).scalars().one()
    assert row.account_ref == "ACC1"
    audit = (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_DETAILS_IMAGE_UPLOADED"))).scalars().one()
    assert audit.entity_id == "ACC1"
    # And Admin can read it back, and the account list reports it.
    assert (await acr.get_account_details_image("ACC1", db, _admin()))["image"] == _png("A")
    listed = await acr.list_accounts(None, db, _admin())
    assert listed[0]["hasAccountDetailsImage"] is True


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
    await _receiver(db, "ACC1")
    await _upload(db, "ACC1", _png("A"))

    out = await txr.create_deposit(_payload(50000), db, merchant)

    # B — the engine ran normally and allocated Account A.
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"
    # C — the merchant opening the deposit receives Account A's image AS the account details, and
    # no text version of them: no bank name, account number, IFSC, branch, UPI ID or snapshot.
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"] == _image("ACC1", _png("A"))
    assert _no_account_text(detail)
    assert _mentions_receiver(detail) == []
    # The Admin's own view keeps the internal record of what was assigned.
    admin_view = await txr.get_transaction_detail(out["id"], db, _admin())
    assert admin_view["allocationSnapshot"]["referenceNumber"] == "ACC1"
    # D — nothing was attached to the transaction: no per-transaction image column was written.
    raw = (await _raw_transactions(db))[0]
    assert raw["admin_proof"] is None and raw["admin_bank_image"] is None and raw["admin_proofs"] is None
    assert _png("A") not in json.dumps(raw, default=str)
    assert (await db.execute(select(func.count()).select_from(AccountDetailsImage))).scalar() == 1


@pytest.mark.asyncio
async def test_a_upi_deposit_shows_the_parent_accounts_image(db):
    """UPI is the form's DEFAULT type — exercise it, not just the bank rails."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await txr.create_deposit(_payload(1000, depositType="UPI", senderUpiId="m@ybl"), db, merchant)
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"]["image"] == _png("A")


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
    assert detail_a["accountDetailsImage"]["image"] == _png("A")
    assert detail_b["accountDetailsImage"]["image"] == _png("B")
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
    for fn in (acr.get_account_details_image, acr.put_account_details_image, acr.delete_account_details_image):
        gates = [p.default.dependency for p in inspect.signature(fn).parameters.values()
                 if hasattr(p.default, "dependency")]
        assert deps.get_current_admin in gates, fn.__name__
    merchant = await _merchant(db)
    with pytest.raises(HTTPException) as e:
        await deps.get_current_admin(merchant)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_non_account_deposit_types_show_no_account_image(db):
    """Cash, Crypto and Card keep their screens — even when an admin_ref names an imaged account.

    CDM is deliberately NOT in this list: it is paid into a managed receiving account like any
    allocatable type, so its merchant receives that account's image (see the CDM section below)."""
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))
    for i, dtype in enumerate(("CASH", "CRYPTO", "CARD"), start=1):
        tx = Transaction(ref=f"H{i}", type=TxType.DEPOSIT_REQUEST, amount=1000, status=TxStatus.ACCOUNT_SUBMITTED,
                         merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
                         deposit_type=dtype, admin_ref="ACC1", created_at=datetime.utcnow())
        db.add(tx)
        await db.flush()
        assert await detimg.for_transaction(db, tx) is None, dtype
    # A withdrawal is never shown one either.
    wd = Transaction(ref="W1", type=TxType.WITHDRAWAL_REQUEST, amount=1000, status=TxStatus.ACCOUNT_SUBMITTED,
                     merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
                     payout_mode="IMPS", admin_ref="ACC1", created_at=datetime.utcnow())
    db.add(wd)
    await db.flush()
    assert await detimg.for_transaction(db, wd) is None


@pytest.mark.asyncio
async def test_an_unplaced_deposit_has_no_account_and_no_image(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A", credit=1000.0)
    await _upload(db, "ACC1", _png("A"))
    out = await txr.create_deposit(_payload(5000), db, merchant)      # over the ceiling
    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"] is None


# ── F. Replacing works ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replacing_the_image_updates_it_for_every_deposit_on_the_account(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("old"))
    out = await txr.create_deposit(_payload(1000), db, merchant)

    replaced = await _upload(db, "ACC1", _jpeg("new"))

    assert replaced["image"] == _jpeg("new") and replaced["contentType"] == "image/jpeg"
    assert (await db.execute(select(func.count()).select_from(AccountDetailsImage))).scalar() == 1
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"]["image"] == _jpeg("new")
    assert (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_DETAILS_IMAGE_REPLACED"))).scalars().one()


# ── G. Removing works ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_removing_the_image_leaves_the_account_eligible_and_the_deposit_explains_it(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await acr.delete_account_details_image("ACC1", _Req(), db, _admin())

    assert out["hasImage"] is False and out["image"] is None
    assert (await db.execute(select(func.count()).select_from(AccountDetailsImage))).scalar() == 0
    assert (await db.execute(select(AuditLog).where(AuditLog.action_type == "ACCOUNT_DETAILS_IMAGE_REMOVED"))).scalars().one()
    # Removing twice is harmless and audits nothing further.
    await acr.delete_account_details_image("ACC1", _Req(), db, _admin())
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action_type == "ACCOUNT_DETAILS_IMAGE_REMOVED"))).scalar() == 1

    dep = await txr.create_deposit(_payload(1000), db, merchant)
    assert dep["adminRef"] == "ACC1"
    detail = await txr.get_transaction_detail(dep["id"], db, merchant)
    assert detail["accountDetailsImage"] == _no_image("ACC1")
    assert _no_account_text(detail)                 # no fallback text takes the image's place


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
    await acr.delete_account_details_image("ACC2", _Req(), db, _admin())
    after_swap = await alloc.allocate_deposit_account(db, amount=45000, member_id="OLDM")

    refs = {r.account.reference_number for r in (before, after_other, after_swap)}
    assert refs == {before.account.reference_number}
    assert before.reason == after_other.reason == after_swap.reason


def test_the_allocation_engine_does_not_read_the_image():
    source = inspect.getsource(alloc)
    for name in ("AccountDetailsImage", "account_details_image", "AccountPaymentImage",
                 "payment_image", "detimg"):
        assert name not in source, name


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
    assert (await db.execute(select(func.count()).select_from(AccountDetailsImage))).scalar() == 0


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
    assert (await acr.get_account_details_image("ACC1", db, _admin()))["image"] == _png("good")


@pytest.mark.asyncio
async def test_with_object_storage_the_image_is_a_private_object_served_by_presigned_url(db, s3):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    row = (await db.execute(select(AccountDetailsImage))).scalars().one()
    assert row.image.startswith("storage://uploads/account_payment_image/")
    assert "base64" not in row.image and len(s3.objects) == 1

    out = await txr.create_deposit(_payload(1000), db, merchant)
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"]["image"].startswith("https://fake-bucket.example/uploads/account_payment_image/")
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
    await acr.delete_account_details_image("ACC1", _Req(), db, _admin())

    assert await _raw_transactions(db) == snapshot
    # The historical Bank Transfer deposit still reads exactly as it was recorded, and its own
    # legacy per-deposit image still reaches the merchant — historical access is preserved.
    assert detail["depositType"] == "BANK"
    assert detail["adminProof"] == _png("sent-card")
    # BANK is an account-backed deposit, so the merchant side receives the account as an Account
    # Details Image and never as text — on a historical row exactly as on a new one. The stored
    # column is untouched (asserted above) and an Admin still reads it.
    assert detail["adminBankDetails"] is None
    admin_view = await txr.get_transaction_detail(f"TXN{historical.id:03d}", db, _admin())
    assert admin_view["adminBankDetails"] == historical.admin_bank_details


# ── J. Manual assignment: the Admin picks the account, the image follows the account ───────────
# The per-deposit image upload is gone. Whether the engine placed the deposit or an Admin sent the
# account by hand, the merchant receives the image configured on THAT account and nothing else.

async def _unplaced(db, merchant, member_id: str) -> dict:
    """A deposit the engine could not place (no accounts exist yet) — the manual-send case."""
    out = await txr.create_deposit(_payload(40000, memberId=member_id), db, merchant)
    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT and out["adminRef"] is None
    return out


async def _send(db, tx_id: str, ref: str, **kw):
    """The Admin's manual assignment: the ACCOUNT travels and nothing else. Typed account details
    are refused outright for these deposits (see the dedicated test below)."""
    return await txr.account_submit(tx_id, txr.AccountSubmitRequest(adminRef=ref, **kw), db, _admin())


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
    assert detail_a["accountDetailsImage"] == _image("ACC0000013", _png("HDFC-A"))
    assert _png("ICICI-B") not in json.dumps(detail_a, default=str)
    assert _no_account_text(detail_a)

    # Tests 6-8: assigned to B → merchant receives B, never A.
    await _send(db, second["id"], "ACC0000014")
    detail_b = await txr.get_transaction_detail(second["id"], db, merchant)
    assert detail_b["accountDetailsImage"]["accountRef"] == "ACC0000014"
    assert detail_b["accountDetailsImage"]["image"] == _png("ICICI-B")
    assert _png("HDFC-A") not in json.dumps(detail_b, default=str)
    assert _no_account_text(detail_b)
    # Neither merchant payload names the other account, by reference or by bank.
    assert "ACC0000014" not in json.dumps(detail_a, default=str)
    assert "ACC0000013" not in json.dumps(detail_b, default=str)

    # And nothing was copied onto either deposit row.
    for raw in await _raw_transactions(db):
        assert raw["admin_proof"] is None and raw["admin_bank_image"] is None


@pytest.mark.asyncio
async def test_manual_send_to_an_account_with_no_image_still_works_and_says_so(db):
    merchant = await _merchant(db)
    out = await _unplaced(db, merchant, "MBR1")
    await _receiver(db, "ACC1")

    sent = await _send(db, out["id"], "ACC1")

    assert sent["status"] == TxStatus.ACCOUNT_SUBMITTED and sent["adminRef"] == "ACC1"
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"] == _no_image("ACC1")
    # No image means NO account details: nothing is rebuilt from the account record to stand in.
    assert _no_account_text(detail)
    assert _mentions_receiver(detail) == []


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
    assert (await txr.get_transaction_detail(earlier["id"], db, merchant))["accountDetailsImage"]["image"] == _png("v1")

    await _upload(db, "ACC1", _png("v2"))
    later = await txr.create_deposit(_payload(1000, memberId="MBR2"), db, merchant)

    detail = await txr.get_transaction_detail(later["id"], db, merchant)
    assert later["adminRef"] == "ACC1" and detail["accountDetailsImage"]["image"] == _png("v2")
    assert _png("v1") not in json.dumps(detail, default=str)


def test_the_merchant_path_never_takes_an_account_reference():
    """Test 11: nothing a merchant can call accepts an account reference to resolve an image by.

    The detail route takes only the transaction id; the resolver takes only the transaction. The
    account-image routes that DO take a reference are Admin-only (tested above)."""
    detail_params = set(inspect.signature(txr.get_transaction_detail).parameters)
    assert detail_params == {"tx_id", "db", "current_user"}
    assert set(inspect.signature(detimg.for_transaction).parameters) == {"db", "tx"}


# ── K. The image IS the account details ────────────────────────────────────────────────────────
# The rule that gives this feature its name: whichever way the account was chosen, the merchant
# receives that account's image and NO text version of the account — and when there is no image,
# no account details at all rather than details rebuilt from the account record.

def test_the_not_configured_wording_is_the_platforms_own():
    """The merchant-facing sentence for an account with no image, in one place."""
    assert detimg.NOT_CONFIGURED_MSG == "Account details image is not configured for this account."


@pytest.mark.asyncio
async def test_auto_allocation_to_an_account_with_no_image_sends_nothing_and_says_so(db):
    """Allocation is untouched by the image: the deposit is placed, and the merchant is told
    plainly that no account details are configured rather than being sent any."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC1")

    out = await txr.create_deposit(_payload(1000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"
    detail = await txr.get_transaction_detail(out["id"], db, merchant)
    assert detail["accountDetailsImage"] == _no_image("ACC1")
    assert _no_account_text(detail)
    assert _mentions_receiver(detail) == []


@pytest.mark.asyncio
async def test_auto_allocation_writes_no_account_details_text_to_the_row(db):
    """Not merely hidden on the way out — the automatic engine never writes the text at all."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))

    out = await txr.create_deposit(_payload(1000), db, merchant)

    assert out["adminRef"] == "ACC1"
    raw = (await _raw_transactions(db))[0]
    assert raw["admin_bank_details"] is None
    # The snapshot is still recorded for the Admin's journal — it simply never reaches a merchant.
    assert json.loads(raw["allocation_snapshot"])["referenceNumber"] == "ACC1"


@pytest.mark.asyncio
async def test_the_same_account_gives_the_same_result_however_it_was_chosen(db):
    """Automatic allocation and a manual assignment of the SAME account are indistinguishable to
    the merchant: the same image, and the same absence of text."""
    merchant = await _merchant(db)
    other = await _merchant(db, uid=9, name="OTHER BIZ")
    # The manual leg has to be raised while nothing is eligible — that is what leaves a deposit for
    # an Admin to assign by hand in the first place.
    manual_tx = await _unplaced(db, other, "MBR9")

    await _receiver(db, "ACC1")
    await _upload(db, "ACC1", _png("A"))
    auto = await txr.create_deposit(_payload(1000, memberId="MBR1"), db, merchant)
    await _send(db, manual_tx["id"], "ACC1")

    auto_detail = await txr.get_transaction_detail(auto["id"], db, merchant)
    manual_detail = await txr.get_transaction_detail(manual_tx["id"], db, other)
    assert auto["adminRef"] == "ACC1"
    assert auto_detail["accountDetailsImage"] == manual_detail["accountDetailsImage"] == _image("ACC1", _png("A"))
    assert _no_account_text(auto_detail) and _no_account_text(manual_detail)
    assert _mentions_receiver(auto_detail) == _mentions_receiver(manual_detail) == []


@pytest.mark.asyncio
async def test_a_upi_deposits_vpa_is_recorded_for_the_admin_and_withheld_from_the_merchant(db):
    """A UPI deposit still routes to the account's linked VPA — that is an allocation rule and it
    is untouched. What changes is that the VPA is not sent to the merchant as text either: the
    account's Account Details Image is the whole of what they receive."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC1")
    await _upload(db, "ACC1", _png("A"))
    tx = Transaction(
        ref="UPI1", type=TxType.DEPOSIT_REQUEST, amount=1000, status=TxStatus.ACCOUNT_SUBMITTED,
        merchant_id=merchant.id, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        deposit_type="UPI", admin_ref="ACC1", admin_upi_id="receiver@ybl",
        admin_bank_details="Account Name: Receiving Alpha\nBank: Bank of Baroda",
        created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()

    detail = await txr.get_transaction_detail(f"TXN{tx.id:03d}", db, merchant)
    assert _no_account_text(detail) and "receiver@ybl" not in json.dumps(detail, default=str)
    assert detail["accountDetailsImage"] == _image("ACC1", _png("A"))
    # The row itself is untouched — a payload rule, not a data deletion — and an Admin still reads it.
    assert tx.admin_upi_id == "receiver@ybl" and tx.admin_bank_details is not None
    admin_view = await txr.get_transaction_detail(f"TXN{tx.id:03d}", db, _admin())
    assert admin_view["adminUpiId"] == "receiver@ybl"


# ── L. The deposit-processing screen cannot supply account details at all ──────────────────────

@pytest.mark.asyncio
async def test_typed_account_details_are_refused_and_nothing_is_written(db):
    """Test 13's other half: the Admin's send cannot carry account-details TEXT either, so a
    client that still builds a bank-details summary cannot slip one past the image."""
    merchant = await _merchant(db)
    out = await _unplaced(db, merchant, "MBR1")
    await _account(db, "ACC1", name="Account A")
    await _upload(db, "ACC1", _png("A"))
    before = await _raw_transactions(db)

    with pytest.raises(HTTPException) as err:
        await txr.account_submit(out["id"], txr.AccountSubmitRequest(
            adminRef="ACC1", adminBankDetails="Account Name: Account A\nBank: HDFC Bank"), db, _admin())

    assert err.value.status_code == 400 and "no longer sent as text" in err.value.detail
    assert await _raw_transactions(db) == before      # status, admin_ref and the text all untouched


@pytest.mark.asyncio
async def test_an_account_backed_deposit_cannot_be_sent_without_an_account(db):
    """The image is resolved from the receiving account, so there must be one. Refused before
    anything is written rather than leaving a deposit that can show the merchant nothing."""
    merchant = await _merchant(db)
    out = await _unplaced(db, merchant, "MBR1")
    before = await _raw_transactions(db)

    with pytest.raises(HTTPException) as err:
        await txr.account_submit(out["id"], txr.AccountSubmitRequest(), db, _admin())

    assert err.value.status_code == 400
    assert await _raw_transactions(db) == before


@pytest.mark.asyncio
async def test_account_management_is_the_only_place_the_image_is_managed(db):
    """Upload, replace and remove live on the account — and nowhere on a transaction."""
    await _account(db, "ACC1", name="Account A")
    assert (await _upload(db, "ACC1", _png("A")))["hasImage"] is True
    assert (await _upload(db, "ACC1", _jpeg("B")))["image"] == _jpeg("B")
    assert (await acr.delete_account_details_image("ACC1", _Req(), db, _admin()))["hasImage"] is False
    # No transaction route offers an image field of its own.
    submit_fields = set(txr.AccountSubmitRequest.model_fields)
    assert not {f for f in submit_fields if "image" in f.lower()} - {"adminBankImage"}
    # ...and the one that remains exists only to REFUSE a stale client (tested above).
    assert "adminProof" in submit_fields

# ═══ CDM — the account an Admin assigns by hand reaches the merchant the same way ══════════════
# A CDM deposit is cash walked into a machine, so the allocation engine never picks its receiving
# account — an Admin assigns one. That is a difference in HOW the account is chosen, and the
# merchant's side does not depend on how: they are paying into a managed account either way, and
# what they receive is that account's Account Details Image and no text.


async def _cdm(db, merchant, member_id: str = "MBR9") -> dict:
    """A CDM request as the merchant raises it: no receiving account, nothing to show yet."""
    out = await txr.create_deposit(_payload(40000, depositType="CDM", memberId=member_id), db, merchant)
    assert out["adminRef"] is None
    return out


@pytest.mark.asyncio
async def test_a_cdm_deposit_shows_the_assigned_accounts_image(db):
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload_cdm(db, "ACC0000017", _png("CDM-SEVENTEEN"))
    out = await _cdm(db, merchant)

    await _send(db, out["id"], "ACC0000017")

    tx = await txr._get_tx(out["id"], db)
    assert await detimg.for_transaction(db, tx) == _image(
        "ACC0000017", _png("CDM-SEVENTEEN"), kind=detimg.CDM_KIND)


@pytest.mark.asyncio
async def test_a_cdm_deposit_never_shows_another_accounts_image(db):
    """The image is resolved from the assigned reference — not by bank, not the first one, not a
    default. Two imaged accounts exist and only the assigned one’s image is ever returned."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _receiver(db, "ACC0000018", bank="ICICI Bank", cdm_limit=100000.0)
    await _upload_cdm(db, "ACC0000017", _png("CDM-SEVENTEEN"))
    await _upload_cdm(db, "ACC0000018", _png("CDM-EIGHTEEN"))
    first = await _cdm(db, merchant, "MBR1")
    second = await _cdm(db, merchant, "MBR2")

    await _send(db, first["id"], "ACC0000017")
    await _send(db, second["id"], "ACC0000018")

    a = await detimg.for_transaction(db, await txr._get_tx(first["id"], db))
    b = await detimg.for_transaction(db, await txr._get_tx(second["id"], db))
    assert a == _image("ACC0000017", _png("CDM-SEVENTEEN"), kind=detimg.CDM_KIND)
    assert b == _image("ACC0000018", _png("CDM-EIGHTEEN"), kind=detimg.CDM_KIND)
    assert a["image"] != b["image"], "two accounts, two images"


@pytest.mark.asyncio
async def test_a_cdm_deposits_account_text_never_reaches_the_merchant(db):
    """The image IS the account details for a CDM deposit too: no bank name, number, IFSC or
    branch on the merchant's payload, and no snapshot standing in for them."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload_cdm(db, "ACC0000017", _png("CDM-SEVENTEEN"))
    out = await _cdm(db, merchant)

    sent = await _send(db, out["id"], "ACC0000017")

    assert _no_account_text(sent), sent
    assert _mentions_receiver(sent) == [], "the receiving account leaked into the merchant payload"
    row = await txr._get_tx(out["id"], db)
    assert row.admin_bank_details is None, "and nothing was written to the row either"


@pytest.mark.asyncio
async def test_a_cdm_account_with_no_cdm_image_says_so_and_substitutes_nothing(db):
    """No text fallback AND no image fallback. The account below has a perfectly good NORMAL
    Account Details Image; a CDM deposit is still told its CDM image is missing, because sending a
    CDM merchant the UPI instructions would walk them to the wrong thing."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload(db, "ACC0000017", _png("NORMAL-ONLY"))        # normal image, no CDM image
    out = await _cdm(db, merchant)

    sent = await _send(db, out["id"], "ACC0000017")

    tx = await txr._get_tx(out["id"], db)
    got = await detimg.for_transaction(db, tx)
    assert got == _no_image("ACC0000017", kind=detimg.CDM_KIND)
    assert got["message"] == "CDM account details image is not configured for this account."
    assert _png("NORMAL-ONLY") not in json.dumps(got), "the normal image is never the fallback"
    assert _no_account_text(sent) and _mentions_receiver(sent) == []


@pytest.mark.asyncio
async def test_typed_account_details_are_refused_on_a_cdm_deposit(db):
    """A client cannot route text to the merchant by calling the assignment route directly."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    out = await _cdm(db, merchant)

    with pytest.raises(HTTPException) as e:
        await _send(db, out["id"], "ACC0000017", adminBankDetails="Bank: X\nA/C: 1")

    assert e.value.status_code == 400
    row = await txr._get_tx(out["id"], db)
    assert row.admin_bank_details is None and row.admin_ref is None


@pytest.mark.asyncio
async def test_a_cdm_account_with_no_image_is_still_assignable(db):
    """Requirement 7 — the image has zero effect on which accounts may be used. An account with
    no image is assigned exactly as readily as one with an image."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)                      # no image
    out = await _cdm(db, merchant)

    sent = await _send(db, out["id"], "ACC0000017")

    assert sent["adminRef"] == "ACC0000017" and sent["status"] == TxStatus.ACCOUNT_SUBMITTED


def test_the_image_set_and_the_allocatable_set_are_separate():
    """CDM shows an image but is never allocated. Two questions, two constants — collapsing them
    would either hand CDM to the engine or take the image off the merchant's screen."""
    from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES
    assert "CDM" in detimg.IMAGE_DEPOSIT_TYPES
    assert "CDM" not in ALLOCATABLE_DEPOSIT_TYPES
    assert set(ALLOCATABLE_DEPOSIT_TYPES) < set(detimg.IMAGE_DEPOSIT_TYPES)


@pytest.mark.asyncio
async def test_one_account_carries_both_images_independently(db):
    """The same account, two images, two audiences. Neither upload disturbs the other row."""
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload(db, "ACC0000017", _png("NORMAL"))
    await _upload_cdm(db, "ACC0000017", _png("CDM"))

    # An IMPS deposit the engine places on the only account there is, and a CDM deposit an Admin
    # assigns to that same account — the two ways an account is chosen, one account, two images.
    normal = await txr.create_deposit(_payload(40000, memberId="MBR1"), db, merchant)
    assert normal["adminRef"] == "ACC0000017", "auto-allocated"
    cdm_dep = await _cdm(db, merchant, "MBR2")
    await _send(db, cdm_dep["id"], "ACC0000017")

    got_normal = await detimg.for_transaction(db, await txr._get_tx(normal["id"], db))
    got_cdm = await detimg.for_transaction(db, await txr._get_tx(cdm_dep["id"], db))
    assert got_normal == _image("ACC0000017", _png("NORMAL"))
    assert got_cdm == _image("ACC0000017", _png("CDM"), kind=detimg.CDM_KIND)
    assert got_normal["image"] != got_cdm["image"]


@pytest.mark.asyncio
async def test_replacing_one_image_leaves_the_other_exactly_as_it_was(db):
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload(db, "ACC0000017", _png("NORMAL"))
    await _upload_cdm(db, "ACC0000017", _png("CDM"))

    await _upload_cdm(db, "ACC0000017", _png("CDM-V2"))         # replace only the CDM one
    assert (await detimg.get(db, "ACC0000017")).image == _png("NORMAL")
    assert (await detimg.get(db, "ACC0000017", kind=detimg.CDM_KIND)).image == _png("CDM-V2")

    await _upload(db, "ACC0000017", _png("NORMAL-V2"))          # ...and only the normal one
    assert (await detimg.get(db, "ACC0000017")).image == _png("NORMAL-V2")
    assert (await detimg.get(db, "ACC0000017", kind=detimg.CDM_KIND)).image == _png("CDM-V2")


@pytest.mark.asyncio
async def test_removing_one_image_leaves_the_other_in_place(db):
    merchant = await _merchant(db)
    await _receiver(db, "ACC0000017", cdm_limit=100000.0)
    await _upload(db, "ACC0000017", _png("NORMAL"))
    await _upload_cdm(db, "ACC0000017", _png("CDM"))

    await acr.delete_cdm_account_details_image("ACC0000017", _Req(), db, _admin())

    assert await detimg.get(db, "ACC0000017", kind=detimg.CDM_KIND) is None
    assert (await detimg.get(db, "ACC0000017")).image == _png("NORMAL"), "untouched"
    # ...and the account is exactly as assignable as it was: an image is never a requirement.
    out = await _cdm(db, merchant)
    assert (await _send(db, out["id"], "ACC0000017"))["adminRef"] == "ACC0000017"


@pytest.mark.asyncio
async def test_the_cdm_image_routes_are_admin_only(db):
    """A merchant cannot name an account and receive its CDM image: there is no route to try."""
    import inspect
    from app.core.deps import get_current_admin
    for fn in (acr.get_cdm_account_details_image, acr.put_cdm_account_details_image,
               acr.delete_cdm_account_details_image):
        param = inspect.signature(fn).parameters
        dep = param.get("actor") or param.get("_")
        assert dep.default.dependency is get_current_admin, fn.__name__
