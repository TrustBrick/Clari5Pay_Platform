"""The CDM daily limit — a hard, per-account, CDM-only ceiling.

What these tests are really defending is that it is a SEPARATE number from every other ceiling the
platform has. It is not Highest Credit, it is not Highest Debit, it is not the account balance, and
nothing CDM does to it touches them. The moment one is read for the other, an account either takes
cash it was never meant to take or refuses cash it was.

The second thing they defend is WHEN capacity is consumed: at assignment, not at completion. Two
Admins assigning the same account in the same instant must not both be told there is room.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base

from app.api.routes import accounts as acr
from app.api.routes import transactions as txr
from app.models.models import (AccountMaster, AccountType, AuditLog, Transaction, TxStatus,
                               User, UserRole)
from app.schemas.schemas import (AccountCdmLimitUpdate, AccountSubmitRequest, DepositCreate,
                                 SlipRequest)
from app.services import cdm_capacity

RECEIPT = "data:image/png;base64,cmVjZWlwdA=="


class _Req:
    client = None


@pytest_asyncio.fixture
async def db():
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

    async def fake_cache(*a, **kw):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", fake_cache)
    monkeypatch.setattr(acr, "cache_delete", fake_cache)
    return counter


def _admin(uid: int = 1, name: str = "Priya Nair") -> User:
    return User(id=uid, username="admin1", name=name, role=UserRole.ADMIN)


async def _merchant(db: AsyncSession, uid: int = 7) -> User:
    user = User(id=uid, username=f"op{uid}", name="BELLAGIO", role=UserRole.MERCHANT,
                hashed_password="x", email=f"op{uid}@test.local", merchant_role="DATA_OPERATOR",
                pay_in="DEP")
    db.add(user)
    await db.flush()
    return user


async def _account(db: AsyncSession, ref: str = "ACC1", *, credit: float = 900000.0,
                   cdm_limit: float = 100000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name="sindu", account_number=f"AC{ref}",
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE", created_date=date.today(),
        created_time="10:00:00", highest_credit=credit, highest_debit=50000.0,
        cdm_daily_limit=cdm_limit,
    )
    db.add(acc)
    await db.flush()
    return acc


def _cdm_payload(amount: float, member: str = "MBR1") -> DepositCreate:
    return DepositCreate(amount=amount, depositType="CDM", memberName="Test Member",
                         memberId=member)


def _bank_payload(amount: float, member: str = "MBR9") -> DepositCreate:
    return DepositCreate(amount=amount, depositType="IMPS", memberName="Test Member",
                         memberId=member, accountHolder="Test Member", accountNumber="999",
                         ifsc="HDFC0001234", bankName="HDFC Bank", accountType="CURRENT")


async def _assign(db, tx_id: str, ref: str = "ACC1"):
    """The Admin assigning the receiving account — the moment CDM capacity is consumed."""
    return await txr.account_submit(tx_id, AccountSubmitRequest(adminRef=ref), db, _admin())


async def _cdm_assigned(db, merchant, amount: float, member: str = "MBR1", ref: str = "ACC1"):
    out = await txr.create_deposit(_cdm_payload(amount, member), db, merchant)
    await _assign(db, out["id"], ref)
    return out["id"]


# ═══ Configuring the limit ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_an_admin_sets_and_edits_the_cdm_limit(db):
    await _account(db, "ACC1", cdm_limit=0.0)

    first = await acr.update_account_cdm_limit(
        "ACC1", AccountCdmLimitUpdate(cdm_daily_limit=100000.0), _Req(), db, _admin())
    assert first["cdmDailyLimit"] == 100000.0

    second = await acr.update_account_cdm_limit(
        "ACC1", AccountCdmLimitUpdate(cdm_daily_limit=250000.0, reason="festival week"),
        _Req(), db, _admin())
    assert second["cdmDailyLimit"] == 250000.0

    acc = (await db.execute(select(AccountMaster))).scalar_one()
    assert acc.cdm_daily_limit == 250000.0
    assert acc.highest_credit == 900000.0, "the credit ceiling is not touched"
    assert acc.highest_debit == 50000.0, "nor the debit ceiling"


@pytest.mark.asyncio
async def test_setting_the_cdm_limit_is_audited(db):
    await _account(db, "ACC1", cdm_limit=50000.0)

    await acr.update_account_cdm_limit(
        "ACC1", AccountCdmLimitUpdate(cdm_daily_limit=100000.0, reason="raised for Diwali"),
        _Req(), db, _admin())

    rows = (await db.execute(select(AuditLog))).scalars().all()
    row = next(r for r in rows if r.action_type == "ACCOUNT_CDM_LIMIT_UPDATED")
    assert row.old_value == "50000.00" and row.new_value == "100000.00"
    assert row.username == "Priya Nair" and "Diwali" in row.reason


@pytest.mark.asyncio
async def test_a_negative_cdm_limit_is_refused(db):
    await _account(db, "ACC1", cdm_limit=100000.0)

    with pytest.raises(HTTPException) as e:
        await acr.update_account_cdm_limit(
            "ACC1", AccountCdmLimitUpdate(cdm_daily_limit=-1.0), _Req(), db, _admin())

    assert e.value.status_code == 400
    assert (await db.execute(select(AccountMaster))).scalar_one().cdm_daily_limit == 100000.0


@pytest.mark.asyncio
async def test_the_cdm_limit_route_is_admin_only(db):
    import inspect
    from app.core.deps import get_current_admin
    sig = inspect.signature(acr.update_account_cdm_limit)
    assert sig.parameters["actor"].default.dependency is get_current_admin


# ═══ Usage and remaining capacity ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_cdm_deposit_consumes_cdm_capacity(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)

    await _cdm_assigned(db, merchant, 30000.0)

    used = await cdm_capacity.used_today(db, ["ACC1"])
    assert used["ACC1"] == 30000.0
    assert cdm_capacity.remaining(100000.0, used["ACC1"]) == 70000.0


@pytest.mark.asyncio
async def test_a_normal_deposit_consumes_no_cdm_capacity(db):
    """A UPI/IMPS/bank deposit into the same account is money, but it is not CDM."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)

    placed = await txr.create_deposit(_bank_payload(80000.0), db, merchant)
    assert placed["adminRef"] == "ACC1", "the engine used the account"

    assert (await cdm_capacity.used_today(db, ["ACC1"])).get("ACC1", 0.0) == 0.0
    caps = await cdm_capacity.for_accounts(db, [(await db.execute(select(AccountMaster))).scalar_one()])
    assert caps[0]["cdmRemainingToday"] == 100000.0, "the whole CDM limit is still there"


@pytest.mark.asyncio
async def test_a_rejected_cdm_request_releases_its_capacity(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    tx_id = await _cdm_assigned(db, merchant, 60000.0)
    assert (await cdm_capacity.used_today(db, ["ACC1"]))["ACC1"] == 60000.0

    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.REJECTED
    await db.flush()

    assert (await cdm_capacity.used_today(db, ["ACC1"])).get("ACC1", 0.0) == 0.0


@pytest.mark.asyncio
async def test_the_figures_an_admin_sees_while_choosing(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    await _cdm_assigned(db, merchant, 30000.0)

    acc = (await db.execute(select(AccountMaster))).scalar_one()
    caps = await cdm_capacity.for_accounts(db, [acc])

    assert caps == [{"referenceNumber": "ACC1", "cdmDailyLimit": 100000.0,
                     "cdmUsedToday": 30000.0, "cdmRemainingToday": 70000.0,
                     "cdmLimitConfigured": True}]


@pytest.mark.asyncio
async def test_remaining_never_goes_negative(db):
    """A limit lowered below what the day has already taken reads as 0, not as a debt."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    await _cdm_assigned(db, merchant, 90000.0)

    await acr.update_account_cdm_limit(
        "ACC1", AccountCdmLimitUpdate(cdm_daily_limit=50000.0), _Req(), db, _admin())

    acc = (await db.execute(select(AccountMaster))).scalar_one()
    assert (await cdm_capacity.for_accounts(db, [acc]))[0]["cdmRemainingToday"] == 0.0


# ═══ The hard limit ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_exactly_the_remaining_capacity_is_allowed(db):
    """Equality passes. Reaching the ceiling exactly is what a ceiling means."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    await _cdm_assigned(db, merchant, 70000.0, "MBR1")

    out = await txr.create_deposit(_cdm_payload(30000.0, "MBR2"), db, merchant)
    sent = await _assign(db, out["id"])

    assert sent["status"] == TxStatus.ACCOUNT_SUBMITTED and sent["adminRef"] == "ACC1"
    assert (await cdm_capacity.used_today(db, ["ACC1"]))["ACC1"] == 100000.0


@pytest.mark.asyncio
async def test_one_paisa_over_the_remaining_capacity_is_refused(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    await _cdm_assigned(db, merchant, 70000.0, "MBR1")

    out = await txr.create_deposit(_cdm_payload(30000.01, "MBR2"), db, merchant)
    with pytest.raises(HTTPException) as e:
        await _assign(db, out["id"])

    assert e.value.status_code == 400
    assert "Insufficient CDM capacity" in e.value.detail
    assert "30,000.00" in e.value.detail, "the refusal names what is actually left"
    tx = await txr._get_tx(out["id"], db)
    assert tx.admin_ref is None and tx.status == TxStatus.ACCOUNT_REQUESTED, "nothing was assigned"


@pytest.mark.asyncio
async def test_an_account_with_no_cdm_limit_configured_cannot_take_cdm(db):
    """0 is unconfigured, not unlimited — the same convention the other two ceilings use. The
    refusal says which, because "set a limit" and "wait until tomorrow" are different actions."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=900000.0, cdm_limit=0.0)

    out = await txr.create_deposit(_cdm_payload(1000.0), db, merchant)
    with pytest.raises(HTTPException) as e:
        await _assign(db, out["id"])

    assert "No CDM daily limit is configured" in e.value.detail
    assert (await txr._get_tx(out["id"], db)).admin_ref is None


@pytest.mark.asyncio
async def test_the_cdm_limit_is_not_highest_credit(db):
    """A wide-open credit ceiling grants no CDM capacity whatsoever."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=10_000_000.0, cdm_limit=50000.0)

    out = await txr.create_deposit(_cdm_payload(60000.0), db, merchant)
    with pytest.raises(HTTPException) as e:
        await _assign(db, out["id"])

    assert "Insufficient CDM capacity" in e.value.detail
    acc = (await db.execute(select(AccountMaster))).scalar_one()
    assert acc.highest_credit == 10_000_000.0, "and the credit ceiling is untouched by the refusal"


@pytest.mark.asyncio
async def test_the_cdm_limit_is_not_highest_debit(db):
    merchant = await _merchant(db)
    acc = await _account(db, "ACC1", cdm_limit=50000.0)
    acc.highest_debit = 10_000_000.0
    await db.flush()

    out = await txr.create_deposit(_cdm_payload(60000.0), db, merchant)
    with pytest.raises(HTTPException):
        await _assign(db, out["id"])

    assert (await db.execute(select(AccountMaster))).scalar_one().highest_debit == 10_000_000.0


@pytest.mark.asyncio
async def test_cdm_capacity_is_checked_per_account_not_across_them(db):
    """A full account does not make a fresh one unusable."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    await _account(db, "ACC2", cdm_limit=100000.0)
    await _cdm_assigned(db, merchant, 100000.0, "MBR1", "ACC1")

    out = await txr.create_deposit(_cdm_payload(100000.0, "MBR2"), db, merchant)
    sent = await _assign(db, out["id"], "ACC2")

    assert sent["adminRef"] == "ACC2"


@pytest.mark.asyncio
async def test_two_assignments_cannot_consume_the_same_remaining_capacity(db):
    """₹50,000 left; ₹40,000 and ₹30,000 both want it. One of them must be refused.

    The check locks the account row before reading usage, so the second assignment reads the
    first one's deposit rather than the figure the first one saw.
    """
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=50000.0)
    a = await txr.create_deposit(_cdm_payload(40000.0, "MBR1"), db, merchant)
    b = await txr.create_deposit(_cdm_payload(30000.0, "MBR2"), db, merchant)

    await _assign(db, a["id"])
    with pytest.raises(HTTPException) as e:
        await _assign(db, b["id"])

    assert "Insufficient CDM capacity" in e.value.detail
    assert (await cdm_capacity.used_today(db, ["ACC1"]))["ACC1"] == 40000.0, "only the first one"
    assert (await txr._get_tx(b["id"], db)).admin_ref is None


@pytest.mark.asyncio
async def test_the_check_takes_a_row_lock_before_it_reads_usage(db):
    """The ORDER is what makes the guarantee: lock, then read. Reading first and locking after
    would let two readers see the same figure, which is the whole failure being prevented."""
    import inspect
    src = inspect.getsource(cdm_capacity.check_and_reserve)
    assert "with_for_update()" in src
    assert src.index("with_for_update()") < src.index("used_today(db, [account_ref])")


@pytest.mark.asyncio
async def test_cdm_capacity_does_not_restrict_a_normal_deposit(db):
    """An exhausted CDM limit says nothing about UPI, IMPS or bank deposits into that account."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=900000.0, cdm_limit=10000.0)
    await _cdm_assigned(db, merchant, 10000.0, "MBR1")

    placed = await txr.create_deposit(_bank_payload(500000.0, "MBR2"), db, merchant)

    assert placed["adminRef"] == "ACC1", "the engine is untouched by the CDM ceiling"


@pytest.mark.asyncio
async def test_completing_a_cdm_deposit_keeps_its_capacity_consumed(db):
    """Capacity is held from assignment through completion — a completed deposit is still today's
    cash in the account, not freed room."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    tx_id = await _cdm_assigned(db, merchant, 60000.0)
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], atmId="13161SRY",
                                             txnNo="5864"), None, db, merchant)
    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await txr.mark_done(tx_id, None, None, db, _admin())

    assert (await cdm_capacity.used_today(db, ["ACC1"]))["ACC1"] == 60000.0


@pytest.mark.asyncio
async def test_yesterdays_cdm_deposits_do_not_consume_todays_capacity(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", cdm_limit=100000.0)
    tx_id = await _cdm_assigned(db, merchant, 100000.0)
    tx = await txr._get_tx(tx_id, db)
    tx.tx_date = date(2020, 1, 1)
    await db.flush()

    assert (await cdm_capacity.used_today(db, ["ACC1"])).get("ACC1", 0.0) == 0.0
    out = await txr.create_deposit(_cdm_payload(100000.0, "MBR2"), db, merchant)
    assert (await _assign(db, out["id"]))["adminRef"] == "ACC1"
