"""Tests for retiring "Bank Transfer" as a deposit type for NEW requests — at the API, not just the UI.

Bank Transfer (deposit_type='BANK') is no longer offered on the deposit form, and the server now
refuses it too, so the rule holds against a request made outside the UI. What must be true:

  1. **A new Bank Transfer request is refused**, however the type is spelled, and a refused request
     leaves nothing behind — no transaction, no allocation journal row, no saved account.
  2. **Historical Bank Transfer deposits stay fully supported.** They open, read back exactly as
     stored, and one still waiting for an account can still be placed by the Admin's retry.
     Nothing about them is modified.
  3. **Every other deposit type is untouched** — UPI, IMPS, NEFT, RTGS, Cash, Crypto, Card and CDM
     each still create and land in the state they always have.
  4. **Only deposit CREATION checks it.** Withdrawals and the allocation engine are not involved.

NO PRODUCTION REFERENCE IDS ARE CONSUMED — ``_next_ref`` is patched to a counter.

Run from the backend directory:

    python -m pytest tests/test_bank_transfer_retired.py -v
"""
from __future__ import annotations

import inspect
from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountType, DepositAllocation, MerchantBankAccount, Transaction, TxStatus,
    TxType, User, UserRole,
)
from app.schemas.schemas import DepositCreate
from app.services import deposit_allocation as alloc

PROOF = "data:image/png;base64,iVBORw0KGgo="


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


async def _merchant(db: AsyncSession, uid: int = 7, role: str = "DATA_OPERATOR") -> User:
    user = User(
        id=uid, username=f"op{uid}", name="BELLAGIO", role=UserRole.MERCHANT, hashed_password="x",
        email=f"op{uid}@test.local", merchant_role=role, pay_in="DEP",
    )
    db.add(user)
    await db.flush()
    return user


def _admin() -> User:
    return User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN)


async def _account(db: AsyncSession, ref: str = "ACC1", credit: float = 500000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name="Account A", account_number=f"AC{ref}",
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE", created_date=date.today(),
        created_time="10:00:00", highest_credit=credit, highest_debit=50000.0,
        bank_daily_credit_limit=credit, bank_daily_debit_limit=50000.0,
        debit_alert_threshold=50000.0,
    )
    db.add(acc)
    await db.flush()
    return acc


def _bank_payload(deposit_type: str, **kw) -> DepositCreate:
    base = dict(
        amount=45000.0, depositType=deposit_type, memberName="Test Member", memberId="MBR1",
        accountHolder="Test Member", accountNumber="999", ifsc="HDFC0001234", bankName="HDFC Bank",
        accountType="CURRENT", saveBankAccount=True,
    )
    base.update(kw)
    return DepositCreate(**base)


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar()


async def _historical_bank(db: AsyncSession, *, ref: str, status: TxStatus, admin_ref: str | None,
                           merchant_id: int = 7) -> Transaction:
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=25000.0, status=status, merchant_id=merchant_id,
        merchant_name="BELLAGIO", tx_date=alloc.ist_today(), tx_time="10:00:00", deposit_type="BANK",
        member_id="MBR9", member_name="Old Member", admin_ref=admin_ref, utr="UTR123",
        admin_bank_details="Account Name: Account A\nBank: HDFC Bank" if admin_ref else None,
        created_at=datetime(2026, 8, 1, 4, 30),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _raw(db: AsyncSession, tx_id: int) -> dict:
    """Every column of one transaction, deferred image columns included."""
    table = Transaction.__table__
    return dict((await db.execute(select(table).where(table.c.id == tx_id))).mappings().one())


# ── 1. A new Bank Transfer request is rejected ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["BANK", "bank", " Bank ", "Bank Transfer", "BANK_TRANSFER", "bank-transfer"])
async def test_a_new_bank_transfer_request_is_rejected_and_leaves_nothing_behind(db, spelling):
    merchant = await _merchant(db)
    await _account(db)

    with pytest.raises(HTTPException) as e:
        await txr.create_deposit(_bank_payload(spelling), db, merchant)

    assert e.value.status_code == 400
    assert "Bank Transfer is no longer available" in e.value.detail
    # Refused before anything is written: no request, no allocation attempt, no saved account.
    assert await _count(db, Transaction) == 0
    assert await _count(db, DepositAllocation) == 0
    assert await _count(db, MerchantBankAccount) == 0


@pytest.mark.asyncio
# Every role that may raise a deposit at all. Supervisors and Managers are refused earlier (403) by
# the existing checker rule, before the deposit type is even looked at.
@pytest.mark.parametrize("role", ["DATA_OPERATOR", "DEO", "DEPOSIT_OPERATOR"])
async def test_no_deposit_creating_role_can_raise_a_bank_transfer(db, role):
    merchant = await _merchant(db, role=role)
    await _account(db)
    with pytest.raises(HTTPException) as e:
        await txr.create_deposit(_bank_payload("BANK"), db, merchant)
    assert e.value.status_code == 400


# ── 2. Historical Bank Transfer deposits remain fully supported ────────────────────────────────

@pytest.mark.asyncio
async def test_a_historical_bank_transfer_deposit_can_still_be_viewed_unchanged(db):
    merchant = await _merchant(db)
    await _account(db)
    tx = await _historical_bank(db, ref="DEP_OLD", status=TxStatus.DEPOSITED, admin_ref="ACC1")
    before = await _raw(db, tx.id)

    by_owner = await txr.get_transaction_detail(f"TXN{tx.id:03d}", db, merchant)
    by_admin = await txr.get_transaction_detail(f"TXN{tx.id:03d}", db, _admin())

    for payload in (by_owner, by_admin):
        assert payload["ref"] == "DEP_OLD"
        assert payload["depositType"] == "BANK"
        assert payload["status"] == TxStatus.DEPOSITED
        assert payload["adminRef"] == "ACC1"
    # BANK is an account-backed deposit, so the receiving account reaches the MERCHANT only as that
    # account's Account Details Image — never as text — on a historical row exactly as on a new one
    # (tests/test_account_details_image.py). The Admin keeps the internal record, and the stored
    # column is untouched either way, which is what the raw comparison below proves.
    assert by_owner["adminBankDetails"] is None
    assert by_admin["adminBankDetails"] == tx.admin_bank_details
    # Reading it changed nothing — not the type, not the status, not any other column.
    assert await _raw(db, tx.id) == before


@pytest.mark.asyncio
async def test_a_new_rejection_does_not_touch_existing_bank_transfer_rows(db):
    merchant = await _merchant(db)
    await _account(db)
    tx = await _historical_bank(db, ref="DEP_OLD", status=TxStatus.ACCOUNT_SUBMITTED, admin_ref="ACC1")
    before = await _raw(db, tx.id)

    with pytest.raises(HTTPException):
        await txr.create_deposit(_bank_payload("BANK"), db, merchant)

    assert await _count(db, Transaction) == 1
    assert await _raw(db, tx.id) == before


@pytest.mark.asyncio
async def test_an_unplaced_historical_bank_transfer_can_still_be_allocated_by_retry(db):
    """Raised before the retirement and still waiting for an account — it must be able to finish.
    Retry reads the STORED type, and the engine still serves BANK."""
    await _merchant(db)
    await _account(db)
    tx = await _historical_bank(db, ref="DEP_WAIT", status=TxStatus.NO_ELIGIBLE_ACCOUNT, admin_ref=None)

    out = await txr.retry_allocation(f"TXN{tx.id:03d}", db, _admin())

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["adminRef"] == "ACC1"
    assert out["depositType"] == "BANK"
    assert "BANK" in alloc.ALLOCATABLE_DEPOSIT_TYPES


# ── 3. Every other deposit type continues to work ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("dtype", ["IMPS", "NEFT", "RTGS"])
async def test_bank_rails_still_create_and_allocate(db, dtype):
    merchant = await _merchant(db)
    await _account(db)
    out = await txr.create_deposit(_bank_payload(dtype), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"
    assert out["depositType"] == dtype


@pytest.mark.asyncio
async def test_upi_still_creates_and_allocates(db):
    merchant = await _merchant(db)
    await _account(db)
    out = await txr.create_deposit(_bank_payload("UPI", senderUpiId="member@ybl"), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC1"
    assert out["depositType"] == "UPI"


@pytest.mark.asyncio
async def test_cash_still_creates_straight_into_review(db):
    merchant = await _merchant(db)
    out = await txr.create_deposit(DepositCreate(
        amount=1000.0, depositType="CASH", memberName="M", memberId="MBR3",
        depositDetails={"village": "V", "city": "C", "mobile": "9"}, proofs=[PROOF]), db, merchant)
    assert out["status"] == TxStatus.SLIP_SUBMITTED and out["depositType"] == "CASH"


@pytest.mark.asyncio
async def test_crypto_still_creates_straight_into_review(db):
    merchant = await _merchant(db)
    out = await txr.create_deposit(DepositCreate(
        amount=1000.0, depositType="CRYPTO", memberName="M", memberId="MBR4",
        depositDetails={"walletAddress": "TXYZ", "network": "TRC20", "txHash": "0xabc"},
        proofs=[PROOF]), db, merchant)
    assert out["status"] == TxStatus.SLIP_SUBMITTED and out["depositType"] == "CRYPTO"


@pytest.mark.asyncio
async def test_card_still_creates_and_waits_for_the_payment_link(db):
    merchant = await _merchant(db, role="DEO")
    await _account(db)
    out = await txr.create_deposit(_bank_payload("CARD"), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED and out["adminRef"] is None
    assert out["depositType"] == "CARD"


@pytest.mark.asyncio
async def test_cdm_still_creates_and_waits_for_the_admin(db):
    merchant = await _merchant(db)
    await _account(db)
    out = await txr.create_deposit(DepositCreate(
        amount=50000.0, depositType="CDM", memberName="M", memberId="MBR5"), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED and out["adminRef"] is None
    assert out["depositType"] == "CDM"


# ── 4. Only deposit creation checks it ─────────────────────────────────────────────────────────

def test_only_deposit_creation_applies_the_rule():
    assert "_is_retired_new_deposit_type" in inspect.getsource(txr.create_deposit)
    for fn in (txr.create_withdrawal, txr.retry_allocation, txr.account_submit,
               txr.get_transaction_detail, txr._auto_allocate_deposit_account):
        assert "_is_retired_new_deposit_type" not in inspect.getsource(fn), fn.__name__
    engine = inspect.getsource(alloc)
    assert "RETIRED_NEW_DEPOSIT_TYPES" not in engine and "_is_retired_new_deposit_type" not in engine
