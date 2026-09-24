"""Account-limit rules the engines' own suites cannot reach on their own.

``test_limit_semantics.py`` pins the per-transaction vs cumulative-daily arithmetic inside the two
allocation engines. This suite pins the same rules everywhere ELSE money can be routed or moved,
plus the configuration that feeds them:

  • **The Admin's manual deposit assignment** obeys BOTH limits — Highest Credit per transaction,
    Bank Daily Credit Limit cumulatively — and the account's Payin Mode. (Before this, it measured
    today's usage against Highest Credit as though it were still a daily ceiling.)
  • **A manual Credit/Debit adjustment** may not push the account past its Bank Daily
    Credit/Debit Limit, and what it spends is what automation then sees (one counter, not two).
  • **Payin Mode**: unconfigured accepts every deposit type; a narrowed list refuses the rest, in
    the engine and on the manual path alike; values are validated against the platform's codes.
  • **Payin Summary** buckets: UPI / Bank Transfer / CDM / Credit Card / Debit Card / Other, and
    Total is their sum, for Today / This Month / All Time.

Run from the backend directory:

    python -m pytest tests/test_limit_final_rules.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import accounts as acct
from app.api.routes import transactions as txr
from app.models.models import (
    AccountMaster, AuditLog, Transaction, TxStatus, TxType, User,
)
from app.schemas.schemas import (
    AccountPayinModesUpdate, AccountSubmitRequest, AdjustmentCreate, DepositCreate,
)
from app.services import account_ledger as ledger
from app.services import deposit_allocation as alloc

from tests.test_deposit_allocation import (  # noqa: F401  (fixtures + builders)
    db, safe_refs_and_no_cache, _account, _deposit, _merchant,
)
from tests.test_withdrawal_allocation import _admin  # noqa: F401


class _Req:
    client = None


@pytest.fixture(autouse=True)
def offline_accounts_module(monkeypatch):
    """The accounts routes reach Redis for their cache and a Postgres sequence for ledger refs.
    Neither exists here: the cache is bypassed and ledger refs come from a counter."""
    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(acct, "cache_delete", _none)
    if hasattr(acct, "cache_get"):
        monkeypatch.setattr(acct, "cache_get", _none)
    if hasattr(acct, "cache_set"):
        monkeypatch.setattr(acct, "cache_set", _none)
    counter = {"n": 0}

    async def _fake_ref(_db, entry_type):
        counter["n"] += 1
        return f"{ledger._ENTRY_PREFIX.get(entry_type, 'LED')}{counter['n']:06d}"

    monkeypatch.setattr(ledger, "_next_entry_ref", _fake_ref)


def _dep(amount: float, **kw) -> DepositCreate:
    base = dict(amount=amount, depositType="IMPS", memberName="Member A", memberId="MBR1",
                accountHolder="Member A", accountNumber="999", ifsc="HDFC0001234",
                bankName="HDFC Bank", accountType="CURRENT")
    base.update(kw)
    return DepositCreate(**base)


async def _adjust(db, ref: str, kind: str, amount: float):
    return await acct.create_adjustment(
        ref, AdjustmentCreate(adjustmentType=kind, amount=amount, reason="Bank Charges"),
        _Req(), db, await _admin_once(db))


async def _admin_once(db):
    """The test database's Admin user, created on first use (the builder inserts a fixed id)."""
    existing = (await db.execute(select(User).where(User.id == 1))).scalar_one_or_none()
    return existing or await _admin(db)


async def _stuck(db, merchant, amount: float, **kw) -> dict:
    """A deposit the engine could not place, left for an Admin."""
    out = await txr.create_deposit(_dep(amount, **kw), db, merchant)
    assert out["adminRef"] is None, out
    return out


# ═══ The brief's worked examples through the real deposit route ═══════════════════════════════

@pytest.mark.asyncio
async def test_brief_test_1_three_deposits_each_within_highest_credit_all_pass(db):
    """Highest Credit ₹75,000 per txn, Bank Daily ₹2,00,000: 70k + 20k + 30k all allocate."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    for amount in (70000, 20000, 30000):
        out = await txr.create_deposit(_dep(amount), db, merchant)
        assert out["adminRef"] == "ACC1", amount
    assert (await alloc.credit_used_today(db, ["ACC1"]))["ACC1"] == 120000.0


@pytest.mark.asyncio
async def test_brief_tests_3_and_4_daily_limit_decides_after_per_txn_passes(db):
    """Today ₹1,50,000 used: ₹60,000 fails the daily limit, ₹50,000 fits it exactly."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "D1", 75000, "ACC1")
    await _deposit(db, "D2", 75000, "ACC1")
    over = await txr.create_deposit(_dep(60000), db, merchant)
    assert over["status"] == TxStatus.ACCOUNT_REQUESTED
    exact = await txr.create_deposit(_dep(50000), db, merchant)
    assert exact["adminRef"] == "ACC1"


# ═══ Manual deposit assignment obeys both limits ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_manual_assignment_refuses_a_deposit_over_the_per_transaction_highest_credit(db):
    merchant = await _merchant(db)
    admin = await _admin_once(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=500000.0)
    out = await _stuck(db, merchant, 75000.01)
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC1"), db, admin)
    assert e.value.status_code == 400
    assert "Highest Credit (per transaction)" in e.value.detail


@pytest.mark.asyncio
async def test_manual_assignment_is_not_limited_by_highest_credit_minus_todays_usage(db):
    """₹60,000 used today on a ₹75,000 per-txn / ₹2,00,000 daily account: a ₹70,000 manual send
    passes. The WRONG rule (Highest Credit − used = ₹15,000) would have refused it."""
    merchant = await _merchant(db)
    admin = await _admin_once(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "D1", 60000, "ACC1")
    # Force the request past the engine so the manual path is what is exercised.
    tx = Transaction(ref="M1", type=TxType.DEPOSIT_REQUEST, amount=70000, status=TxStatus.NO_ELIGIBLE_ACCOUNT,
                     merchant_id=merchant.id, merchant_name=merchant.name, tx_date=alloc.ist_today(),
                     tx_time="10:00:00", deposit_type="IMPS", member_id="MBR1",
                     created_at=datetime.utcnow())
    db.add(tx)
    await db.flush()
    out = await txr.account_submit(f"TXN{tx.id:03d}", AccountSubmitRequest(adminRef="ACC1"), db, admin)
    assert out["adminRef"] == "ACC1"


@pytest.mark.asyncio
async def test_manual_assignment_refuses_what_would_exceed_the_bank_daily_credit_limit(db):
    merchant = await _merchant(db)
    admin = await _admin_once(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "D1", 75000, "ACC1")
    await _deposit(db, "D2", 75000, "ACC1")
    out = await _stuck(db, merchant, 60000)
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC1"), db, admin)
    assert e.value.status_code == 400
    assert "Bank Daily Credit Limit" in e.value.detail and "50,000.00" in e.value.detail


@pytest.mark.asyncio
async def test_manual_assignment_refuses_an_account_with_no_bank_daily_credit_limit(db):
    merchant = await _merchant(db)
    admin = await _admin_once(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=0.0)
    out = await _stuck(db, merchant, 1000)
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC1"), db, admin)
    assert "no Bank Daily Credit Limit configured" in e.value.detail


# ═══ Manual Credit / Debit consumes the same daily capacity ═════════════════════════════════════

@pytest.mark.asyncio
async def test_brief_test_7_manual_credit_consumes_daily_credit_and_blocks_automation(db):
    """Daily ₹2,00,000; automated ₹1,20,000 + manual ₹50,000 = ₹1,70,000 used, ₹30,000 left.
    A ₹40,000 automated deposit is refused; ₹30,000 still fits."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "D1", 60000, "ACC1")
    await _deposit(db, "D2", 60000, "ACC1")
    await _adjust(db, "ACC1", "CREDIT", 50000)
    assert (await alloc.credit_used_today(db, ["ACC1"]))["ACC1"] == 170000.0

    refused = await txr.create_deposit(_dep(40000), db, merchant)
    assert refused["status"] == TxStatus.ACCOUNT_REQUESTED
    fits = await txr.create_deposit(_dep(30000), db, merchant)
    assert fits["adminRef"] == "ACC1"


@pytest.mark.asyncio
async def test_a_manual_credit_cannot_itself_exceed_the_bank_daily_credit_limit(db):
    await _account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "D1", 75000, "ACC1")
    await _deposit(db, "D2", 75000, "ACC1")
    with pytest.raises(HTTPException) as e:
        await _adjust(db, "ACC1", "CREDIT", 50000.01)
    assert e.value.status_code == 400 and "Bank Daily Credit Limit" in e.value.detail
    ok = await _adjust(db, "ACC1", "CREDIT", 50000)                 # exactly the remainder
    assert ok["duplicate"] is False


@pytest.mark.asyncio
async def test_brief_test_8_manual_debit_consumes_daily_debit_capacity(db):
    """Daily debit ₹2,00,000: manual debits count, and one that would breach it is refused."""
    await _account(db, "ACC1", name="a", credit=500000.0, debit=75000.0, daily_debit=200000.0)
    await _deposit(db, "D1", 400000, "ACC1")                       # fund the account
    await _adjust(db, "ACC1", "DEBIT", 120000)
    await _adjust(db, "ACC1", "DEBIT", 50000)
    from app.services import withdrawal_allocation as wa
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 170000.0
    with pytest.raises(HTTPException) as e:
        await _adjust(db, "ACC1", "DEBIT", 40000)
    assert "Bank Daily Debit Limit" in e.value.detail and "30,000.00" in e.value.detail


@pytest.mark.asyncio
async def test_an_account_with_no_daily_limit_can_still_be_adjusted(db):
    """0 = unconfigured: there is no ceiling to measure against, so corrections still post."""
    await _account(db, "ACC1", name="a", credit=0.0, daily_credit=0.0)
    assert (await _adjust(db, "ACC1", "CREDIT", 999999))["duplicate"] is False


# ═══ Payin Mode ═════════════════════════════════════════════════════════════════════════════════

def test_payin_mode_unconfigured_accepts_every_type_and_bank_is_served_by_a_rail():
    open_acc = AccountMaster(payin_modes=None)
    for t in alloc.PAYIN_MODES + ("BANK", None, ""):
        assert alloc.supports_payin_mode(open_acc, t), t
    narrowed = AccountMaster(payin_modes="IMPS,CDM")
    assert alloc.supports_payin_mode(narrowed, "imps")
    assert alloc.supports_payin_mode(narrowed, "BANK")              # legacy generic bank
    assert not alloc.supports_payin_mode(narrowed, "UPI")
    assert not alloc.supports_payin_mode(narrowed, "NEFT")


@pytest.mark.asyncio
async def test_the_engine_skips_an_account_whose_payin_mode_excludes_the_type(db):
    merchant = await _merchant(db)
    near = await _account(db, "NEAR", name="near", credit=50000.0)     # nearest capacity
    await _account(db, "FAR", name="far", credit=500000.0)
    near.payin_modes = "IMPS"
    await db.flush()
    # Two different members, so the second is not steered by the first's account history.
    assert (await txr.create_deposit(_dep(1000, depositType="UPI", memberId="M1"),
                                     db, merchant))["adminRef"] == "FAR"
    assert (await txr.create_deposit(_dep(1000, depositType="IMPS", memberId="M2"),
                                     db, merchant))["adminRef"] == "NEAR"


@pytest.mark.asyncio
async def test_no_account_accepting_the_mode_is_reported_as_such(db):
    acc = await _account(db, "ACC1", name="a", credit=500000.0)
    acc.payin_modes = "IMPS"
    await db.flush()
    r = await alloc.allocate_deposit_account(db, amount=1000, member_id="M", deposit_type="UPI")
    assert not r.allocated and r.detail["failure"] == alloc.FAIL_NO_PAYIN_MODE


@pytest.mark.asyncio
async def test_manual_assignment_refuses_an_account_whose_payin_mode_excludes_the_type(db):
    merchant = await _merchant(db)
    admin = await _admin_once(db)
    acc = await _account(db, "ACC1", name="a", credit=500000.0)
    acc.payin_modes = "UPI"
    await db.flush()
    out = await _stuck(db, merchant, 1000)                           # IMPS
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC1"), db, admin)
    assert "Payin Mode" in e.value.detail


@pytest.mark.asyncio
async def test_payin_mode_route_validates_stores_and_audits(db):
    await _account(db, "ACC1", name="a")
    admin = await _admin_once(db)
    with pytest.raises(HTTPException) as e:
        await acct.update_account_payin_modes(
            "ACC1", AccountPayinModesUpdate(payinModes=["UPI", "CASH"]), _Req(), db, admin)
    assert e.value.status_code == 400

    out = await acct.update_account_payin_modes(
        "ACC1", AccountPayinModesUpdate(payinModes=["cdm", "UPI", "UPI"]), _Req(), db, admin)
    assert out["payinModes"] == ["CDM", "UPI"] and out["payinModesConfigured"] is True
    row = (await db.execute(select(AuditLog).where(
        AuditLog.action_type == "ACCOUNT_PAYIN_MODES_UPDATED"))).scalars().one()
    assert row.old_value == "All modes" and row.new_value == "CDM, UPI"

    cleared = await acct.update_account_payin_modes(
        "ACC1", AccountPayinModesUpdate(payinModes=[]), _Req(), db, admin)
    assert cleared["payinModesConfigured"] is False
    assert cleared["payinModes"] == sorted(alloc.PAYIN_MODES)


# ═══ Payin Summary ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_payin_summary_buckets_windows_and_total(db):
    await _merchant(db)
    admin = await _admin_once(db)
    await _account(db, "ACC1", name="a", credit=10_000_000.0)
    today = alloc.ist_today()
    month_start = today.replace(day=1)
    last_month = month_start - timedelta(days=1)

    async def dep(ref, amount, dtype, on, status=TxStatus.DEPOSITED):
        tx = await _deposit(db, ref, amount, "ACC1", on=on, status=status)
        tx.deposit_type = dtype
        await db.flush()

    await dep("T1", 50000, "UPI", today)
    await dep("T2", 20000, "IMPS", today)
    await dep("T3", 10000, "NEFT", today)
    await dep("T4", 20000, "CDM", today)
    await dep("T5", 7000, "CREDIT_CARD", today)
    await dep("T6", 3000, "DEBIT_CARD", today)
    await dep("T7", 5000, "CARD", today)                      # pre-split card → Other
    await dep("T8", 99999, "UPI", today, status=TxStatus.ACCOUNT_SUBMITTED)   # not received
    await dep("L1", 40000, "UPI", last_month)

    rows = await acct.account_balances(db, admin)
    pm = next(r for r in rows if r["referenceNumber"] == "ACC1")["payinByMode"]
    assert pm["today"] == {"UPI": 50000, "Bank Transfer": 30000, "CDM": 20000,
                           "Credit Card": 7000, "Debit Card": 3000, "Other": 5000}
    assert sum(pm["today"].values()) == 115000
    assert pm["allTime"]["UPI"] == 90000
    if last_month < month_start <= today:
        assert pm["month"]["UPI"] == 50000                    # last month excluded
