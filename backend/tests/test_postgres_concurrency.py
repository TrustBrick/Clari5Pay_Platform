"""REAL PostgreSQL concurrency — the races the SQLite suites cannot exercise.

The engines protect the daily limits with row locks (``SELECT … FOR UPDATE [SKIP LOCKED]`` on
``account_master``) plus a re-read of today's usage while the lock is held. SQLite ignores FOR
UPDATE, so every other suite can only check the arithmetic, sequentially. This one runs two
requests AT THE SAME TIME, on two connections, through the real functions the routes place a
request with (_auto_allocate_deposit_account / _auto_allocate_withdrawal), against a real Postgres:

  * deposits — ₹50,000 of daily credit left; two simultaneous ₹40,000 deposits → exactly one is
    allocated, the other waits in ACCOUNT_REQUESTED, and the account ends the day within its limit.
  * withdrawals — the same for daily debit: exactly one gets a payout leg.
  * the startup migration runs on Postgres and moves a stored NO_ELIGIBLE_ACCOUNT row into
    ACCOUNT_REQUESTED.

It needs a Postgres server and is SKIPPED unless one is named:

    TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@localhost:5432/postgres \\
        python -m pytest tests/test_postgres_concurrency.py -v

The URL is only used to CREATE a throwaway database (``c5_race_<random>``); every test runs in
that database and it is DROPPED afterwards. No existing database is read or written.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountLedgerEntry, AccountMaster, AccountType, DepositAccountOverride, Transaction, TxStatus,
    TxType, User, UserRole, WithdrawalPayoutLeg,
)
from app.schemas.schemas import AccountOverrideRequest
from app.services import account_ledger as ledger
from app.services import deposit_allocation as alloc
from app.services import tg_notify as tgn
from app.services import withdrawal_allocation as walloc

PG_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set — no real Postgres")

# How long each request keeps its transaction (and so its row locks) open after the route returns,
# before committing. Long enough that the two requests genuinely overlap.
HOLD_SECONDS = 0.6


# ── A throwaway database ───────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def pg():
    admin_url = make_url(PG_URL)
    name = f"c5_race_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(admin_url.set(database=name), pool_size=5)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No reference sequences, cache or Telegram: the race is about the database only."""
    counter = {"n": 0}

    async def fake_next_ref(db, kind, code=None):
        counter["n"] += 1
        return f"R{counter['n']}-{uuid.uuid4().hex[:6]}"

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", noop)
    monkeypatch.setattr(tgn, "notify", noop)
    monkeypatch.setattr(tgn, "notify_account_request", noop)


def _sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(engine, *, credit_daily: float, debit_daily: float, used_credit: float,
                used_debit: float) -> None:
    today = alloc.ist_today()
    async with _sessions(engine)() as db:
        db.add_all([
            User(id=7, username="op7", name="BELLAGIO", role=UserRole.MERCHANT, hashed_password="x",
                 email="op7@test.local", merchant_role="DATA_OPERATOR", pay_in="DEP"),
            AccountMaster(
                reference_number="ACC0000001", account_name="only", account_number="5010000001",
                ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
                account_type=AccountType.CURRENT, status="ACTIVE",
                created_date=today, created_time="10:00:00",
                highest_credit=100000.0, highest_debit=100000.0, debit_alert_threshold=0.0,
                bank_daily_credit_limit=credit_daily, bank_daily_debit_limit=debit_daily),
        ])
        await db.flush()
        # Money in the account (yesterday, so it takes none of today's credit), and today's
        # usage so far on each side.
        db.add(Transaction(ref="FUND", type=TxType.DEPOSIT_REQUEST, amount=2_000_000.0,
                           status=TxStatus.DEPOSITED, merchant_id=7, merchant_name="BELLAGIO",
                           tx_date=today - timedelta(days=1), tx_time="10:00:00", member_id="F",
                           admin_ref="ACC0000001", account_assigned_on=today - timedelta(days=1),
                           created_at=datetime.utcnow()))
        if used_credit:
            db.add(Transaction(ref="USED", type=TxType.DEPOSIT_REQUEST, amount=used_credit,
                               status=TxStatus.DEPOSITED, merchant_id=7, merchant_name="BELLAGIO",
                               tx_date=today, tx_time="10:00:00", member_id="U",
                               admin_ref="ACC0000001", account_assigned_on=today,
                               created_at=datetime.utcnow()))
        if used_debit:
            start_utc, _ = alloc.ist_day_utc_bounds(today)
            db.add(AccountLedgerEntry(
                entry_ref="ADJ-RACE", entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.DEBIT,
                amount=used_debit, account_ref="ACC0000001", balance_before=0.0, balance_after=0.0,
                reason="Reconciliation Correction", created_at=start_utc + timedelta(hours=6),
                created_at_ist=alloc.ist_stamp()))
        await db.commit()


async def _race(engine, refs: list[str], place):
    """Place the two requests ``refs`` AT THE SAME TIME: each in its own session and connection,
    each running ``place(db, tx)`` — the real function the routes apply the engine with — and each
    holding its transaction (and so its row locks) open for HOLD_SECONDS before committing.

    The requests are inserted and committed FIRST, with their own references. Creating them inside
    the race would not test the engine at all: every create route inserts a placeholder
    ``ref='TEMP'`` into a UNIQUE column, so a second concurrent create already waits on that index
    for the first to commit, and the two never reach the engine together.
    """
    Session = _sessions(engine)
    start = asyncio.Event()

    async def one(ref):
        async with Session() as db:
            tx = (await db.execute(select(Transaction).where(Transaction.ref == ref))).scalar_one()
            await start.wait()
            placed = await place(db, tx)
            await asyncio.sleep(HOLD_SECONDS)       # keep the row locks while the other one runs
            await db.commit()
            return placed

    tasks = [asyncio.create_task(one(r)) for r in refs]
    await asyncio.sleep(0.2)                         # both sessions loaded and waiting
    start.set()
    return await asyncio.gather(*tasks)


async def _requests(engine, *rows: Transaction) -> None:
    async with _sessions(engine)() as db:
        db.add_all(rows)
        await db.commit()


def _deposit_request(ref: str, member: str) -> Transaction:
    return Transaction(ref=ref, type=TxType.DEPOSIT_REQUEST, amount=40000.0,
                       status=TxStatus.ACCOUNT_REQUESTED, merchant_id=7, merchant_name="BELLAGIO",
                       tx_date=alloc.ist_today(), tx_time="10:00:00", member_id=member,
                       deposit_type="IMPS", created_at=datetime.utcnow())


def _withdrawal_request(ref: str) -> Transaction:
    return Transaction(ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=40000.0,
                       status=TxStatus.MANAGER_REVIEW, merchant_id=7, merchant_name="BELLAGIO",
                       tx_date=alloc.ist_today(), tx_time="10:00:00", member_id="MBR1",
                       payout_mode="IMPS", account_number="1234567890", ifsc="HDFC0009999",
                       account_holder="Rita", bank_name="HDFC Bank", created_at=datetime.utcnow())


# ═══ 46. Deposit race ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_46_two_simultaneous_deposits_cannot_both_take_the_last_50000(pg, monkeypatch):
    async def _on(_db):
        return True
    monkeypatch.setattr(txr.automation_mode, "is_enabled", _on)
    # ₹2,00,000 daily credit limit, ₹1,50,000 already used → ₹50,000 left.
    await _seed(pg, credit_daily=200000.0, debit_daily=200000.0, used_credit=150000.0, used_debit=0)
    await _requests(pg, _deposit_request("DEP-A", "RACE1"), _deposit_request("DEP-B", "RACE2"))

    placed = await _race(pg, ["DEP-A", "DEP-B"],
                         lambda db, tx: txr._auto_allocate_deposit_account(db, tx))

    assert sorted(placed) == [False, True], placed
    async with _sessions(pg)() as db:
        rows = {t.ref: t for t in (await db.execute(
            select(Transaction).where(Transaction.ref.in_(["DEP-A", "DEP-B"])))).scalars()}
        used = (await alloc.credit_used_today(db, ["ACC0000001"]))["ACC0000001"]
    assert sorted(r.status.value for r in rows.values()) == ["ACCOUNT_REQUESTED", "ACCOUNT_SUBMITTED"]
    assert used == 190000.0, "₹1,50,000 + ONE ₹40,000 — never ₹2,30,000"
    assert used <= 200000.0


# ═══ 48. Admin Override & Send race — one request, two confirms ═════════════════════════════════

@pytest.mark.asyncio
async def test_48_two_simultaneous_overrides_on_one_request_assign_once(pg, monkeypatch):
    """Two Admins (or a double-click) confirm Override & Send on the SAME request at the same
    instant. The transaction row lock serialises them: exactly one assignment is made and exactly
    one override journal row is written — the second confirm wakes on ACCOUNT_SUBMITTED and returns
    the placed state without assigning again or double-counting the deposit."""
    async def _on(_db):
        return True
    monkeypatch.setattr(txr.automation_mode, "is_enabled", _on)
    # ₹50,000 daily credit limit, ₹40,000 already used → ₹10,000 left; a ₹40,000 send needs override.
    await _seed(pg, credit_daily=50000.0, debit_daily=200000.0, used_credit=40000.0, used_debit=0)
    await _requests(pg, _deposit_request("DEP-X", "RACE"))

    admin = User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN,
                 hashed_password="x", email="admin1@test.local")

    def place(db, tx):
        return txr.override_account(
            f"TXN{str(tx.id).zfill(3)}",
            AccountOverrideRequest(adminRef="ACC0000001",
                                   reason="Customer specifically requested this account."),
            db, admin)

    results = await _race(pg, ["DEP-X", "DEP-X"], place)

    assert all(r.get("status") == "ACCOUNT_SUBMITTED" for r in results), results
    async with _sessions(pg)() as db:
        overrides = (await db.execute(select(DepositAccountOverride))).scalars().all()
        used = (await alloc.credit_used_today(db, ["ACC0000001"]))["ACC0000001"]
        placed = (await db.execute(
            select(Transaction).where(Transaction.ref == "DEP-X"))).scalar_one()
    assert len(overrides) == 1, "exactly one override row despite two concurrent confirms"
    assert placed.admin_ref == "ACC0000001" and placed.status == TxStatus.ACCOUNT_SUBMITTED
    assert used == 80000.0, "₹40,000 already + ONE ₹40,000 override — never ₹1,20,000"


# ═══ 47. Withdrawal race ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_47_two_simultaneous_withdrawals_cannot_both_take_the_last_50000(pg, monkeypatch):
    async def _on(_db):
        return True
    monkeypatch.setattr(txr.automation_mode, "is_enabled", _on)
    # ₹2,00,000 daily debit limit, ₹1,50,000 already debited (manual) → ₹50,000 left.
    await _seed(pg, credit_daily=5_000_000.0, debit_daily=200000.0, used_credit=0,
                used_debit=150000.0)
    await _requests(pg, _withdrawal_request("WIT-A"), _withdrawal_request("WIT-B"))

    placed = await _race(pg, ["WIT-A", "WIT-B"],
                         lambda db, tx: txr._auto_allocate_withdrawal(db, tx))

    assert sorted(placed) == [False, True], placed
    async with _sessions(pg)() as db:
        legs = (await db.execute(
            select(WithdrawalPayoutLeg).where(WithdrawalPayoutLeg.status == ledger.LEG_ALLOCATED)
        )).scalars().all()
        used = (await walloc.debit_used_today(db, ["ACC0000001"]))["ACC0000001"]
    assert len(legs) == 1, f"exactly one withdrawal holds the remaining capacity, got {len(legs)}"
    assert used == 190000.0 and used <= 200000.0


# ═══ The startup migration on Postgres ══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_migration_moves_stored_no_eligible_rows_into_account_requested(pg):
    import main  # noqa: F401 — registers every model, exactly as the app does before migrating
    from app.db.migrate import ensure_schema

    async with pg.begin() as conn:                 # the tables the other model modules add
        await conn.run_sync(Base.metadata.create_all)
    await _seed(pg, credit_daily=1.0, debit_daily=1.0, used_credit=0, used_debit=0)
    async with _sessions(pg)() as db:
        db.add(Transaction(ref="STUCK", type=TxType.WITHDRAWAL_REQUEST, amount=45000.0,
                           status=TxStatus.NO_ELIGIBLE_ACCOUNT, merchant_id=7,
                           merchant_name="BELLAGIO", tx_date=alloc.ist_today(), tx_time="10:00:00",
                           created_at=datetime.utcnow()))
        await db.commit()

    await ensure_schema(pg)
    await ensure_schema(pg)          # idempotent: a second startup changes nothing and does not fail

    async with _sessions(pg)() as db:
        row = (await db.execute(select(Transaction).where(Transaction.ref == "STUCK"))).scalar_one()
        cols = {r[0] for r in (await db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='transactions'"
        ))).all()}
    assert row.status == TxStatus.ACCOUNT_REQUESTED
    assert {"account_assigned_on", "account_request_alerted_at"} <= cols
