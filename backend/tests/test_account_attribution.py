"""Tests for which managed account a completed withdrawal/settlement is charged to.

Deposits carry the receiving account on the row itself (``Transaction.admin_ref``). Debits do
not, so they are attributed through a member -> account map. Two things feed that map, and both
used to be trusted blindly:

  * an ``AccountTransaction`` link row, which is written the moment an admin SENDS account
    details (on ``ACCOUNT_SUBMITTED``) — it records where a deposit was *directed*, never that
    money arrived. A deposit later CANCELLED leaves its link row behind.
  * the member's own deposit history, which has the same hole: an abandoned request still names
    an ``admin_ref``.

Charging an account for a deposit that never completed drove its Available Balance negative on
production — one member with ten completed withdrawals and no completed deposit at all took
3.58L off an account that had never received a rupee from them.

The property under test is one sentence: **only a completed deposit that actually credited an
account may attribute that member's debits to it.** Everything below is a corollary.

Run from the backend directory:

    python -m pytest tests/test_account_attribution.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import accounts as acct
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountTransaction, AccountType, Transaction, TxStatus, TxType, User, UserRole,
)


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
def no_cache(monkeypatch):
    """/balances is cached ~5s. Bypass it so each test sees its own data, not a neighbour's."""
    async def miss(_key):
        return None

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(acct, "cache_get", miss)
    monkeypatch.setattr(acct, "cache_set", noop)


_CLOCK = datetime(2026, 8, 1, 10, 0, 0)


async def _account(db: AsyncSession, ref: str, name: str) -> None:
    db.add(AccountMaster(
        reference_number=ref, account_name=name, account_number="9999" + ref[-4:],
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE",
        created_date=date.today(), created_time="10:00:00",
    ))
    await db.flush()


async def _merchant(db: AsyncSession) -> User:
    u = User(id=7, username="bellagio", name="BELLAGIO", role=UserRole.MERCHANT,
             hashed_password="x", email="bellagio@example.com",
             pay_in_fee=0.0, pay_out_fee=0.0)
    db.add(u)
    await db.flush()
    return u


async def _deposit(db: AsyncSession, ref: str, amount: float, account_ref: str | None,
                   *, member: str = "WININ100", status=TxStatus.DEPOSITED,
                   age: int = 0) -> Transaction:
    t = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id=member, admin_ref=account_ref, created_at=_CLOCK + timedelta(minutes=age),
    )
    db.add(t)
    await db.flush()
    return t


async def _withdrawal(db: AsyncSession, ref: str, amount: float, *, member: str = "WININ100",
                      status=TxStatus.COMPLETED, payout_ref=None,
                      payout_method=None) -> Transaction:
    t = Transaction(
        ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="11:00:00",
        member_id=member, created_at=_CLOCK + timedelta(hours=1),
        payout_account_ref=payout_ref, payout_payment_method=payout_method,
    )
    db.add(t)
    await db.flush()
    return t


async def _link(db: AsyncSession, account_ref: str, tx: Transaction | None,
                *, member: str = "WININ100") -> None:
    """The row transactions.py writes when an admin sends account details."""
    db.add(AccountTransaction(
        reference_number=account_ref, member_id=member,
        transaction_reference_number=tx.ref if tx else None,
        transaction_date=date.today(), transaction_time="10:00:00",
    ))
    await db.flush()


async def _map(db: AsyncSession) -> dict[str, str]:
    txns = (await db.execute(select(Transaction))).scalars().all()
    return await acct._member_account_map(db, txns)


async def _available(db: AsyncSession, ref: str) -> float:
    rows = await acct.account_balances(
        db, User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN,
                 hashed_password="x", email="admin1@example.com")
    )
    row = next(r for r in rows if r["referenceNumber"] == ref)
    return row["available"]


# ── 1. The production bug ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_deposit_does_not_charge_its_account(db):
    """The reported row: an account sent its details, the deposit was cancelled, the member
    withdrew anyway. The account received nothing, so it must be charged nothing."""
    await _merchant(db)
    await _account(db, "ACC0000031", "Jackpots World Tour and Travels")
    dep = await _deposit(db, "DEP000001", 100000, "ACC0000031", status=TxStatus.CANCELLED)
    await _link(db, "ACC0000031", dep)
    await _withdrawal(db, "WIT000001", 41176)

    assert await _map(db) == {}
    assert await _available(db, "ACC0000031") == 0.0


@pytest.mark.asyncio
async def test_available_never_goes_negative_on_an_unfunded_account(db):
    """The screenshot's arithmetic: 3,67,564 received, 5,35,106 charged, -1,67,542 shown. With
    the uncovered debits excluded the account reads what it actually holds."""
    await _merchant(db)
    await _account(db, "ACC0000031", "Jackpots World Tour and Travels")
    funded = await _deposit(db, "DEP000001", 367564, "ACC0000031", member="WININ38797")
    await _link(db, "ACC0000031", funded, member="WININ38797")
    await _withdrawal(db, "WIT000001", 176230, member="WININ38797")

    # A second member: link rows only, every deposit cancelled, big withdrawals.
    ghost = await _deposit(db, "DEP000002", 100000, "ACC0000031",
                           member="WININ39762", status=TxStatus.CANCELLED)
    await _link(db, "ACC0000031", ghost, member="WININ39762")
    await _withdrawal(db, "WIT000002", 358876, member="WININ39762")

    assert await _available(db, "ACC0000031") == 367564 - 176230


# ── 2. A confirmed link still attributes ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_completed_deposit_link_attributes_the_debit(db):
    """The fix must not throw away the attribution it was built for."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001")
    await _link(db, "ACC0000001", dep)
    await _withdrawal(db, "WIT000001", 50000)

    assert await _map(db) == {"WININ100": "ACC0000001"}
    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_legacy_completed_status_also_attributes(db):
    """Deposits complete as DEPOSITED (admin final approval) or COMPLETED (legacy). Both count."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001", status=TxStatus.COMPLETED)
    await _link(db, "ACC0000001", dep)

    assert await _map(db) == {"WININ100": "ACC0000001"}


# ── 3. A link must belong to the account it names ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_link_pointing_at_a_deposit_into_another_account_is_ignored(db):
    """Creating an account links it to the merchant's most recent transaction, whatever that
    transaction was for. Such a row names an account the deposit never credited."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Funded")
    await _account(db, "ACC0000002", "Brand New")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001")
    await _link(db, "ACC0000002", dep)          # the bogus row
    await _withdrawal(db, "WIT000001", 50000)

    assert await _map(db) == {"WININ100": "ACC0000001"}
    assert await _available(db, "ACC0000002") == 0.0
    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_link_without_a_transaction_reference_is_ignored(db):
    """Seeded and hand-made rows can carry no transaction at all — nothing to confirm."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _link(db, "ACC0000001", None)
    await _withdrawal(db, "WIT000001", 50000)

    assert await _map(db) == {}


# ── 4. The deposit-history fallback carries the same rule ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_attributes_when_the_link_row_is_missing(db):
    """A debit is not dropped merely because no link row was ever written."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001")   # no _link()
    await _withdrawal(db, "WIT000001", 50000)

    assert await _map(db) == {"WININ100": "ACC0000001"}
    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_fallback_ignores_an_uncompleted_deposit(db):
    """The hole the link path had, reached the other way round."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", status=TxStatus.ACCOUNT_SUBMITTED)
    await _withdrawal(db, "WIT000001", 50000)

    assert await _map(db) == {}
    assert await _available(db, "ACC0000001") == 0.0


@pytest.mark.asyncio
async def test_fallback_takes_the_most_recent_funded_account(db):
    """Where a member has funded more than one account the newest completed deposit wins."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Older")
    await _account(db, "ACC0000002", "Newer")
    await _deposit(db, "DEP000001", 100000, "ACC0000001", age=0)
    await _deposit(db, "DEP000002", 100000, "ACC0000002", age=10)

    assert await _map(db) == {"WININ100": "ACC0000002"}


# ── 5. Rules that must survive the change ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_member_ids_still_match_across_casing_and_spacing(db):
    """A deposit filed as ' winin100 ' and a withdrawal as 'WININ100' are one member."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", member=" winin100 ")
    await _withdrawal(db, "WIT000001", 50000, member="WININ100")

    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_an_explicit_payout_account_still_wins(db):
    """Where completion recorded the account the money actually left, the map is not consulted."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Funded")
    await _account(db, "ACC0000002", "Paid From")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001")
    await _link(db, "ACC0000001", dep)
    await _withdrawal(db, "WIT000001", 50000, payout_ref="ACC0000002")

    assert await _available(db, "ACC0000001") == 200000.0
    assert await _available(db, "ACC0000002") == -50000.0


@pytest.mark.asyncio
async def test_a_manual_payout_belongs_to_no_account(db):
    """An offline payout leaves no account; that rule is untouched by the attribution fix."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Funded")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001")
    await _link(db, "ACC0000001", dep)
    await _withdrawal(db, "WIT000001", 50000, payout_method="MANUAL")

    assert await _available(db, "ACC0000001") == 200000.0


@pytest.mark.asyncio
async def test_an_uncompleted_withdrawal_is_not_charged(db):
    """Only completed debits move an account."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    dep = await _deposit(db, "DEP000001", 200000, "ACC0000001")
    await _link(db, "ACC0000001", dep)
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.MANAGER_REVIEW)

    assert await _available(db, "ACC0000001") == 200000.0
