"""The Automation Update switch, and what each of its two positions actually does.

This suite drives the REAL, database-backed switch — it carries the ``automation_switch`` marker,
so the conftest fixture that forces automation on for every other suite leaves it alone.

What it pins:

  * **OFF is the legacy platform, exactly.** A deposit waits in ACCOUNT_REQUESTED for an Admin. An
    approved withdrawal lands in ACCOUNT_REQUESTED for an Admin. No account is chosen, no capacity
    is consumed, no payout leg is written and no allocation journal row appears.
  * **OFF is NOT "the engine found nothing".** The single most important test here is
    ``test_off_parks_an_approved_withdrawal_where_an_admin_can_reach_it``. An earlier env-based
    kill switch made the engine return "not placed" when off, and the Manager-approval branch read
    that as "nothing was eligible" and routed the withdrawal to NO_ELIGIBLE_ACCOUNT — a state whose
    only exit is a retry endpoint that also refuses while the switch is off. Turning automation off
    stranded live withdrawals, and the release was rolled back. Off must park work where a human
    can still pick it up.
  * **ON is today's behaviour, untouched.**
  * **The switch is server-side.** It is read in the business layer on every request, so nothing a
    browser sends can change which path a transaction takes.

Run from the backend directory:

    python -m pytest tests/test_automation_mode.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import automation as auto_routes
from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountType, AppSetting, AuditLog, Transaction, TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import DepositCreate, RemarkRequest, WithdrawalCreate
from app.services import automation_mode
from app.services import withdrawal_allocation as wa

pytestmark = pytest.mark.automation_switch      # this suite owns the switch


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
def offline(monkeypatch):
    """Keep reference numbers and the cache out of these tests."""
    n = {"i": 0}

    async def fake_next_ref(db, kind, code=None):
        n["i"] += 1
        return str(n["i"])

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", noop)


class _Req:
    client = type("C", (), {"host": "127.0.0.1"})()


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

async def _admin(db: AsyncSession, uid: int = 1, role=UserRole.ADMIN) -> User:
    u = User(id=uid, username="admin%d" % uid, name="Admin %d" % uid, role=role,
             hashed_password="x", email="a%d@test.local" % uid)
    db.add(u)
    await db.flush()
    return u


async def _merchant(db: AsyncSession, uid: int = 7) -> User:
    u = User(id=uid, username="op%d" % uid, name="BELLAGIO", role=UserRole.MERCHANT,
             hashed_password="x", email="op%d@test.local" % uid,
             merchant_role="DATA_OPERATOR", pay_in="DEP")
    db.add(u)
    await db.flush()
    return u


async def _manager(db: AsyncSession, uid: int = 8) -> User:
    u = User(id=uid, username="mgr", name="BELLAGIO", role=UserRole.MERCHANT,
             hashed_password="x", email="mgr@test.local", merchant_role="MANAGER")
    db.add(u)
    await db.flush()
    return u


async def _account(db: AsyncSession, ref: str = "ACC1", *, credit: float = 1000000.0,
                   debit: float = 1000000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name="ACC-" + ref, account_number="9000" + ref,
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE",
        created_date=date.today(), created_time="10:00:00",
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=0.0,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _fund(db: AsyncSession, ref: str, account_ref: str, amount: float) -> None:
    db.add(Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=TxStatus.DEPOSITED,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=wa.ist_today(), tx_time="10:00:00",
        member_id="MBR1", admin_ref=account_ref, created_at=datetime.utcnow(),
    ))
    await db.flush()


def _dep(amount: float = 30000.0, **kw) -> DepositCreate:
    base = dict(amount=amount, depositType="IMPS", memberName="Test Member", memberId="MBR1",
                accountHolder="Test Member", accountNumber="999", ifsc="HDFC0001234",
                bankName="HDFC Bank", accountType="CURRENT")
    base.update(kw)
    return DepositCreate(**base)


def _wit(amount: float = 45000.0, **kw) -> WithdrawalCreate:
    base = dict(amount=amount, memberId="MBR1", memberName="Rita", payoutMode="IMPS",
                accountHolder="Rita", accountNumber="1234567890", ifsc="HDFC0009999",
                bankName="HDFC Bank", branch="Mumbai", accountType="SAVINGS")
    base.update(kw)
    return WithdrawalCreate(**base)


# An unsaved User: set_enabled only reads the actor's name, and persisting one here would collide
# with the admins the tests create for themselves.
_SYSTEM = User(id=None, username="tester", name="Tester", role=UserRole.ADMIN, hashed_password="x")


async def _turn(db: AsyncSession, on: bool, actor: User | None = None) -> None:
    await automation_mode.set_enabled(db, on, actor=actor or _SYSTEM)


# ═══ 1. The switch itself ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_fresh_platform_starts_in_standard_mode(db):
    """No row, no automation. The default is the behaviour that has been running for months."""
    assert await automation_mode.is_enabled(db) is False


@pytest.mark.parametrize("stored", ["", "1", "yes", "on", "enabled", "TRUE ", "False", "null"])
@pytest.mark.asyncio
async def test_only_the_exact_word_true_turns_automation_on(db, stored):
    """Fail-closed and deliberately literal. A truncated restore or a hand-edited row must not be
    able to start routing money automatically."""
    db.add(AppSetting(key=automation_mode.KEY, value=stored))
    await db.flush()

    assert await automation_mode.is_enabled(db) is (stored.strip().lower() == "true")


@pytest.mark.asyncio
async def test_the_switch_moves_both_ways_and_remembers_who_moved_it(db):
    admin = await _admin(db)

    await automation_mode.set_enabled(db, True, actor=admin, reason="UAT sign-off")
    on = await automation_mode.status(db)
    await automation_mode.set_enabled(db, False, actor=admin, reason="payouts misrouting")
    off = await automation_mode.status(db)

    assert on["enabled"] is True and off["enabled"] is False
    assert off["changedBy"] == "Admin 1" and off["reason"] == "payouts misrouting"
    assert off["changedAt"], "the screen has to be able to say WHEN"


@pytest.mark.asyncio
async def test_the_state_survives_a_new_connection(db):
    """Database-backed, not in-process: another worker, another container, or the same request an
    hour later all read the same answer. No restart is needed to change the mode."""
    await _turn(db, True)
    await db.commit()

    assert await automation_mode.is_enabled(db) is True
    assert (await db.execute(
        select(AppSetting.value).where(AppSetting.key == automation_mode.KEY))).scalar() == "true"


# ═══ 2. The endpoints ═════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_an_admin_can_read_and_move_the_switch(db):
    admin = await _admin(db)

    assert (await auto_routes.get_automation(db, admin))["enabled"] is False
    out = await auto_routes.set_automation(
        auto_routes.AutomationUpdate(enabled=True, reason="go live"), _Req(), db, admin)

    assert out["enabled"] is True
    assert (await auto_routes.get_automation(db, admin))["enabled"] is True


@pytest.mark.asyncio
async def test_a_super_admin_may_move_it_too(db):
    sa = await _admin(db, 2, role=UserRole.SUPER_ADMIN)
    out = await auto_routes.set_automation(
        auto_routes.AutomationUpdate(enabled=True), _Req(), db, sa)
    assert out["enabled"] is True


@pytest.mark.asyncio
async def test_moving_the_switch_is_audited_with_both_values(db):
    admin = await _admin(db)
    await auto_routes.set_automation(
        auto_routes.AutomationUpdate(enabled=True, reason="approved by ops"), _Req(), db, admin)
    await db.flush()

    row = (await db.execute(
        select(AuditLog).where(AuditLog.action_type == "AUTOMATION_MODE_CHANGED"))).scalars().first()
    assert row is not None
    assert row.old_value == "Standard Mode" and row.new_value == "Automation Enabled"
    assert row.reason == "approved by ops"


@pytest.mark.asyncio
async def test_the_switch_is_server_side_and_a_merchant_cannot_reach_it(db):
    """The browser does not decide this. `get_current_admin` is the dependency on both endpoints,
    so a merchant token is refused before any handler runs."""
    from app.core.deps import get_current_admin

    with pytest.raises(HTTPException) as e:
        await get_current_admin(await _merchant(db))
    assert e.value.status_code == 403


# ═══ 3. OFF is the legacy platform ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_off_leaves_a_new_deposit_for_an_admin_to_place(db):
    merchant = await _merchant(db)
    await _account(db)
    await _turn(db, False)

    out = await txr.create_deposit(_dep(), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED, "waiting for an Admin, as it always did"
    assert out["adminRef"] is None, "no account was chosen"


@pytest.mark.asyncio
async def test_off_consumes_no_capacity_and_writes_no_legs(db):
    merchant = await _merchant(db)
    await _account(db)
    await _fund(db, "D1", "ACC1", 500000.0)
    await _turn(db, False)

    out = await txr.create_withdrawal(_wit(), None, db, merchant)

    assert out["payoutLegs"] == [], "nothing was allocated, so nothing is being held"
    assert await wa.live_legs(db, out["ref"]) == []


@pytest.mark.asyncio
async def test_off_parks_an_approved_withdrawal_where_an_admin_can_reach_it(db):
    """THE test. Automation off must land a Manager-approved withdrawal in ACCOUNT_REQUESTED —
    "an Admin now chooses who pays" — and never in NO_ELIGIBLE_ACCOUNT.

    NO_ELIGIBLE_ACCOUNT is an exception state whose only exit is the retry endpoint, and that
    endpoint refuses while automation is off. A withdrawal sent there with the switch off cannot
    move at all. That is not a theoretical risk: it happened on production and the release was
    rolled back the next day.
    """
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db)
    await _fund(db, "D1", "ACC1", 500000.0)
    await _turn(db, False)
    created = await txr.create_withdrawal(_wit(), None, db, merchant)

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["status"] != TxStatus.NO_ELIGIBLE_ACCOUNT, "this is the state with no way out"


@pytest.mark.asyncio
async def test_off_makes_the_retry_endpoints_say_why_rather_than_shrug(db):
    """"Nothing was eligible" and "automation is switched off" are different facts. Reporting the
    first for the second sends an operator hunting through account limits for a switch."""
    merchant = await _merchant(db)
    await _account(db)
    await _turn(db, False)
    dep = await txr.create_deposit(_dep(), db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.retry_allocation(dep["id"], db, await _admin(db))

    assert e.value.status_code == 409
    assert "switched off" in e.value.detail.lower()


# ═══ 4. ON is today's platform ════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_allocates_a_new_deposit_immediately(db):
    merchant = await _merchant(db)
    await _account(db)
    await _turn(db, True)

    out = await txr.create_deposit(_dep(), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["adminRef"] == "ACC1"


@pytest.mark.asyncio
async def test_on_allocates_a_withdrawal_and_approval_makes_it_payable(db):
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db)
    await _fund(db, "D1", "ACC1", 500000.0)
    await _turn(db, True)
    created = await txr.create_withdrawal(_wit(), None, db, merchant)

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert [l["accountRef"] for l in out["payoutLegs"]] == ["ACC1"]


@pytest.mark.asyncio
async def test_flipping_the_switch_changes_the_very_next_request(db):
    """No restart, no redeploy. Two identical requests, one switch flip between them."""
    merchant = await _merchant(db)
    await _account(db)

    await _turn(db, False)
    first = await txr.create_deposit(_dep(memberId="M1"), db, merchant)
    await _turn(db, True)
    second = await txr.create_deposit(_dep(memberId="M2"), db, merchant)

    assert first["status"] == TxStatus.ACCOUNT_REQUESTED and first["adminRef"] is None
    assert second["status"] == TxStatus.ACCOUNT_SUBMITTED and second["adminRef"] == "ACC1"


# ═══ 5. Everything the switch must NOT touch ══════════════════════════════════════════════════


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_a_cdm_deposit_is_unaffected_either_way(db, enabled):
    """CDM has never been allocated — an Admin assigns its account by hand, in both modes. The
    switch governs automation; it is not a second CDM rule."""
    merchant = await _merchant(db)
    await _account(db)
    await _turn(db, enabled)

    out = await txr.create_deposit(_dep(depositType="CDM"), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_the_switch_does_not_change_which_types_need_an_account(db, enabled):
    """CASH and CRYPTO carry their own proof and never touch a managed account. The switch decides
    WHO picks an account, never WHETHER a type needs one."""
    from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES
    await _turn(db, enabled)

    assert "CASH" not in ALLOCATABLE_DEPOSIT_TYPES
    assert "CRYPTO" not in ALLOCATABLE_DEPOSIT_TYPES
    assert "IMPS" in ALLOCATABLE_DEPOSIT_TYPES


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_a_cash_withdrawal_needs_no_paying_account_either_way(db, enabled):
    merchant = await _merchant(db)
    await _account(db)
    await _fund(db, "D1", "ACC1", 500000.0)      # the merchant needs a balance to withdraw from
    await _turn(db, enabled)

    out = await txr.create_withdrawal(_wit(payoutMode="CASH"), None, db, merchant)

    assert out["status"] == TxStatus.MANAGER_REVIEW, "the review gate is untouched by the switch"
