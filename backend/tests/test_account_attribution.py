"""Tests for which managed account a completed withdrawal/settlement is charged to.

Deposits carry the receiving account on the row itself (``Transaction.admin_ref``). Debits do
not: where the payout step recorded the account explicitly that wins, but every row completed
before that step existed has to be inferred from the member's funding history. Two ways of
getting that inference wrong both showed up on production as wrong money on screen.

**Charging an account for money it never received.** An ``AccountTransaction`` link row is
written the moment an admin SENDS account details (on ``ACCOUNT_SUBMITTED``) — it records where
a deposit was *directed*, never that money arrived, and a deposit later CANCELLED leaves its row
behind. The member's raw deposit history has the same hole: an abandoned request still names an
``admin_ref``. One member with ten completed withdrawals and no completed deposit anywhere took
3.58L off an account that had never received a rupee from them, showing -1,67,542.

**Charging today's account for yesterday's withdrawal.** Attribution used the member's single
most-recent receiving account, so a member who funded one account and later moved to another had
their whole history charged to the later one — reading it down and the earlier one up. A debit
that predated the member's first deposit was charged to an account anyway.

The rule under test, in one sentence: **a debit belongs to the account the member was funding at
the moment it left**, and only a completed deposit counts as funding.

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


def _at(day: int) -> datetime:
    """A timestamp `day` days into the scenario, so ordering reads as dates in the tests."""
    return _CLOCK + timedelta(days=day)


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
                   day: int = 0) -> Transaction:
    t = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id=member, admin_ref=account_ref, created_at=_at(day),
    )
    db.add(t)
    await db.flush()
    return t


async def _withdrawal(db: AsyncSession, ref: str, amount: float, *, member: str = "WININ100",
                      status=TxStatus.COMPLETED, day: int = 99, payout_ref=None,
                      payout_method=None) -> Transaction:
    t = Transaction(
        ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="11:00:00",
        member_id=member, created_at=_at(day),
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


async def _charged_to(db: AsyncSession, tx: Transaction) -> str | None:
    """The account this debit is attributed to — the rule itself, in isolation."""
    txns = (await db.execute(select(Transaction))).scalars().all()
    funding = await acct._member_account_timeline(db, txns)
    return acct._debit_account(tx, funding)


async def _available(db: AsyncSession, ref: str) -> float:
    rows = await acct.account_balances(
        db, User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN,
                 hashed_password="x", email="admin1@example.com")
    )
    row = next(r for r in rows if r["referenceNumber"] == ref)
    return row["available"]


# ── 1. Only money that arrived counts as funding ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_deposit_does_not_charge_its_account(db):
    """The reported row: an account sent its details, the deposit was cancelled, the member
    withdrew anyway. The account received nothing, so it must be charged nothing."""
    await _merchant(db)
    await _account(db, "ACC0000031", "Jackpots World Tour and Travels")
    dep = await _deposit(db, "DEP000001", 100000, "ACC0000031", status=TxStatus.CANCELLED, day=1)
    await _link(db, "ACC0000031", dep)
    wd = await _withdrawal(db, "WIT000001", 41176, day=2)

    assert await _charged_to(db, wd) is None
    assert await _available(db, "ACC0000031") == 0.0


@pytest.mark.asyncio
async def test_available_never_goes_negative_on_an_unfunded_account(db):
    """The screenshot's arithmetic: 3,67,564 received, 5,35,106 charged, -1,67,542 shown. With
    the uncovered debits excluded the account reads what it actually holds."""
    await _merchant(db)
    await _account(db, "ACC0000031", "Jackpots World Tour and Travels")
    funded = await _deposit(db, "DEP000001", 367564, "ACC0000031", member="WININ38797", day=1)
    await _link(db, "ACC0000031", funded, member="WININ38797")
    await _withdrawal(db, "WIT000001", 176230, member="WININ38797", day=2)

    # A second member: link rows only, every deposit cancelled, big withdrawals.
    ghost = await _deposit(db, "DEP000002", 100000, "ACC0000031",
                           member="WININ39762", status=TxStatus.CANCELLED, day=1)
    await _link(db, "ACC0000031", ghost, member="WININ39762")
    await _withdrawal(db, "WIT000002", 358876, member="WININ39762", day=3)

    assert await _available(db, "ACC0000031") == 367564 - 176230


@pytest.mark.asyncio
async def test_an_uncompleted_deposit_is_not_funding(db):
    """A request still in flight names an admin_ref but has moved no money."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001",
                   status=TxStatus.ACCOUNT_SUBMITTED, day=1)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2)

    assert await _charged_to(db, wd) is None
    assert await _available(db, "ACC0000001") == 0.0


@pytest.mark.asyncio
async def test_a_link_row_alone_attributes_nothing(db):
    """Link rows are not consulted: a confirmed one only ever describes a completed deposit,
    which is already in the funding timeline under its own date."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _link(db, "ACC0000001", None)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2)

    assert await _charged_to(db, wd) is None


@pytest.mark.asyncio
async def test_a_completed_deposit_does_attribute(db):
    """The rule must still do the job it exists for."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", day=1)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2)

    assert await _charged_to(db, wd) == "ACC0000001"
    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_legacy_completed_status_is_also_funding(db):
    """Deposits complete as DEPOSITED (admin final approval) or COMPLETED (legacy). Both count."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", status=TxStatus.COMPLETED, day=1)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2)

    assert await _charged_to(db, wd) == "ACC0000001"


# ── 2. A debit belongs to the moment it left ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debit_before_any_deposit_belongs_to_no_account(db):
    """WININ26927 on production: withdrew on 3 August, first funded anything on 15 August. No
    account had received a rupee from them, so none may be charged."""
    await _merchant(db)
    await _account(db, "ACC0000024", "KISHORE KUMMAR RAVULLAKOLLU")
    wd = await _withdrawal(db, "WIT000001", 10294, day=1)
    await _deposit(db, "DEP000001", 10000, "ACC0000024", day=14)

    assert await _charged_to(db, wd) is None
    assert await _available(db, "ACC0000024") == 10000.0


@pytest.mark.asyncio
async def test_debit_is_charged_to_the_account_funded_at_the_time(db):
    """WININ38797 on production: funded GURUBACHAN SINGH, withdrew 43,731 while that was the only
    account they had ever used, then moved to Jackpots. The 43,731 belongs to GURUBACHAN."""
    await _merchant(db)
    await _account(db, "ACC0000017", "GURUBACHAN SINGH")
    await _account(db, "ACC0000031", "Jackpots World Tour and Travels")
    await _deposit(db, "DEP000001", 85000, "ACC0000017", day=1)
    early = await _withdrawal(db, "WIT000001", 43731, day=2)
    await _deposit(db, "DEP000002", 138000, "ACC0000031", day=3)
    late = await _withdrawal(db, "WIT000002", 132499, day=4)

    assert await _charged_to(db, early) == "ACC0000017"
    assert await _charged_to(db, late) == "ACC0000031"
    assert await _available(db, "ACC0000017") == 85000 - 43731
    assert await _available(db, "ACC0000031") == 138000 - 132499


@pytest.mark.asyncio
async def test_the_later_account_does_not_absorb_the_earlier_history(db):
    """The mode-2 bug stated directly: without point-in-time both debits land on the newer
    account, reading it down and the older one up."""
    await _merchant(db)
    await _account(db, "ACC0000017", "Older")
    await _account(db, "ACC0000031", "Newer")
    await _deposit(db, "DEP000001", 100000, "ACC0000017", day=1)
    await _withdrawal(db, "WIT000001", 40000, day=2)
    await _deposit(db, "DEP000002", 100000, "ACC0000031", day=3)

    assert await _available(db, "ACC0000017") == 60000.0
    assert await _available(db, "ACC0000031") == 100000.0


@pytest.mark.asyncio
async def test_a_deposit_at_the_same_instant_counts_as_received(db):
    """Boundary: a deposit stamped at the debit's own moment has arrived, not missed it."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 100000, "ACC0000001", day=5)
    wd = await _withdrawal(db, "WIT000001", 40000, day=5)

    assert await _charged_to(db, wd) == "ACC0000001"


@pytest.mark.asyncio
async def test_returning_to_the_first_account_charges_it_again(db):
    """Funding is a history, not a high-water mark: moving back charges the account moved back to."""
    await _merchant(db)
    await _account(db, "ACC0000001", "First")
    await _account(db, "ACC0000002", "Second")
    await _deposit(db, "DEP000001", 100000, "ACC0000001", day=1)
    await _deposit(db, "DEP000002", 100000, "ACC0000002", day=2)
    middle = await _withdrawal(db, "WIT000001", 30000, day=3)
    await _deposit(db, "DEP000003", 100000, "ACC0000001", day=4)
    last = await _withdrawal(db, "WIT000002", 30000, day=5)

    assert await _charged_to(db, middle) == "ACC0000002"
    assert await _charged_to(db, last) == "ACC0000001"


# ── 3. Rules that must survive the change ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_member_ids_still_match_across_casing_and_spacing(db):
    """A deposit filed as ' winin100 ' and a withdrawal as 'WININ100' are one member."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", member=" winin100 ", day=1)
    await _withdrawal(db, "WIT000001", 50000, member="WININ100", day=2)

    assert await _available(db, "ACC0000001") == 150000.0


@pytest.mark.asyncio
async def test_an_explicit_payout_account_still_wins(db):
    """Where completion recorded the account the money actually left, nothing is inferred."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Funded")
    await _account(db, "ACC0000002", "Paid From")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", day=1)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2, payout_ref="ACC0000002")

    assert await _charged_to(db, wd) == "ACC0000002"
    assert await _available(db, "ACC0000001") == 200000.0
    assert await _available(db, "ACC0000002") == -50000.0


@pytest.mark.asyncio
async def test_a_manual_payout_belongs_to_no_account(db):
    """An offline payout leaves no account; that rule outranks the funding history."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Funded")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", day=1)
    wd = await _withdrawal(db, "WIT000001", 50000, day=2, payout_method="MANUAL")

    assert await _charged_to(db, wd) is None
    assert await _available(db, "ACC0000001") == 200000.0


@pytest.mark.asyncio
async def test_an_uncompleted_withdrawal_is_not_charged(db):
    """Only completed debits move an account."""
    await _merchant(db)
    await _account(db, "ACC0000001", "Aisha Trading")
    await _deposit(db, "DEP000001", 200000, "ACC0000001", day=1)
    await _withdrawal(db, "WIT000001", 50000, day=2, status=TxStatus.MANAGER_REVIEW)

    assert await _available(db, "ACC0000001") == 200000.0
