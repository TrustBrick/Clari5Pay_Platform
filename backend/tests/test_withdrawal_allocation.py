"""Tests for the automatic withdrawal payout account allocation engine.

The engine decides which account real money LEAVES, so the properties worth pinning down are the
ones that would cost money if they broke:

  1. **Highest Debit is a HARD DAILY CEILING.** Never ``amount <= highest_debit``; always
     ``debit_used_today + amount <= highest_debit``. Reaching it exactly is allowed; exceeding it
     by a single paisa is not.
  2. **Available balance is a second, independent ceiling.** Headroom without money, or money
     without headroom, both disqualify an account.
  3. **Capacity is consumed at allocation, not at completion.** An allocated-but-unpaid leg holds
     its capacity, which is what stops two concurrent withdrawals from spending it twice; a
     rejected or cancelled withdrawal releases it.
  4. **A merchant note is a preference, never an override.** "Use Bank of Baroda" narrows the pool
     and EVERY Bank of Baroda account is evaluated — but if none can pay, none is used.
  5. **Nearest suitable capacity wins**, and ties break deterministically.
  6. **A split sums to EXACTLY the requested amount**, with the fewest accounts, and no leg ever
     exceeds its own account's usable capacity.
  7. **No partial completion.** Capacity short of the amount produces an exception, never a
     smaller payout.
  8. **A no-account outcome is an EXCEPTION, not a queue.** Nothing is assigned and the
     withdrawal goes to NO_ELIGIBLE_ACCOUNT with the reason journalled — never back to
     ACCOUNT_REQUESTED, which used to mean "waiting for an Admin to pick an account".
  9. **Completion is idempotent and atomic**, and never debits an account twice.

NO PRODUCTION REFERENCE IDS ARE CONSUMED. ``_next_ref`` draws from the live Postgres DEP/WIT/SET
sequences; it is patched out for every test that creates a withdrawal, and every transaction built
directly carries a plain test id ("1", "2", "3"…). Nothing here reads, advances or reserves a real
reference number.

Run from the backend directory:

    python -m pytest tests/test_withdrawal_allocation.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountLedgerEntry, AccountMaster, AccountType, AuditLog, MerchantBankAccount, Transaction,
    TxStatus, TxType, User, UserRole, WithdrawalAllocation, WithdrawalPayoutLeg,
)
from app.schemas.schemas import CompleteRequest, ReasonRequest, RemarkRequest, WithdrawalCreate
from app.services import account_ledger as ledger
from app.services import withdrawal_allocation as wa


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    """A real, empty database built from the project's own models.

    SQLite has no sequences, so the one raw ``nextval`` the ledger issues for its entry references
    is stubbed with a counter for the duration of the test — the same substitution the ledger's own
    suite makes, and for the same reason. Everything else (the tables, the UNIQUE constraints, the
    SQL the balances are computed with) is the production definition.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # autoflush=False MIRRORS PRODUCTION (db/session.py). Without it the tests run with
    # autoflush on, pending rows are visible to the next query, and a missing flush in the code
    # under test passes here while failing for real — which is exactly how a dropped account type
    # reached Demo.
    Session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    counter = {"n": 0}

    async def _fake_ref(_db, entry_type):
        counter["n"] += 1
        return f"{ledger._ENTRY_PREFIX.get(entry_type, 'LED')}{counter['n']:06d}"

    real = ledger._next_entry_ref
    ledger._next_entry_ref = _fake_ref
    async with Session() as session:
        yield session
    ledger._next_entry_ref = real
    await engine.dispose()


@pytest.fixture(autouse=True)
def safe_refs_and_no_cache(monkeypatch):
    """Two substitutions that keep the route tests honest and offline.

    ``_next_ref`` normally calls ``nextval`` on the production DEP/WIT/SET sequences. It is
    replaced with a counter yielding "1", "2", "3"… so no real reference number is ever generated
    or consumed by a test, and the ids in these tests are obviously test ids.

    ``cache_delete`` reaches Redis. The cache is fail-open, so leaving it live would still pass —
    it would just spend a connection timeout per call.
    """
    counter = {"n": 0}

    async def fake_next_ref(db, kind, code=None):
        counter["n"] += 1
        return str(counter["n"])

    async def fake_cache_delete(key):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", fake_cache_delete)
    return counter


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

async def _account(
    db: AsyncSession, ref: str, *, name: str | None = None, bank: str = "HDFC Bank",
    debit: float = 100000.0, credit: float = 1000000.0, modes: str | None = None,
    status: str = "ACTIVE", atype: AccountType = AccountType.CURRENT, number: str | None = None,
) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name or f"ACC-{ref}",
        account_number=number or f"9000{ref}", ifsc_code="HDFC0001234", bank_name=bank,
        branch="Mumbai", account_type=atype, status=status,
        created_date=date.today(), created_time="10:00:00",
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=0.0,
        payout_modes=modes,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _fund(db: AsyncSession, ref: str, account_ref: str, amount: float,
                *, member: str = "MBR1") -> Transaction:
    """A COMPLETED deposit — the only thing that puts money into a managed account."""
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=TxStatus.DEPOSITED,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=wa.ist_today(), tx_time="10:00:00",
        member_id=member, admin_ref=account_ref, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _withdrawal(
    db: AsyncSession, ref: str, amount: float, *, member: str = "MBR1",
    status: TxStatus = TxStatus.MANAGER_REVIEW, mode: str = "IMPS",
    number: str = "1234567890", notes: str | None = None, on: date | None = None,
) -> Transaction:
    """A withdrawal row. ``ref`` is a plain test id — never a generated production reference."""
    tx = Transaction(
        ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=on or wa.ist_today(),
        tx_time="10:00:00", member_id=member, member_name="Rita",
        account_holder="Rita", account_number=number, ifsc="HDFC0009999", bank_name="HDFC Bank",
        payout_mode=mode, notes=notes, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _merchant(db: AsyncSession, uid: int = 7) -> User:
    user = User(
        id=uid, username=f"op{uid}", name="BELLAGIO", role=UserRole.MERCHANT,
        hashed_password="x", email=f"op{uid}@test.local", merchant_role="DATA_OPERATOR",
        pay_out="WIT",
    )
    db.add(user)
    await db.flush()
    return user


async def _admin(db: AsyncSession, uid: int = 1) -> User:
    user = User(id=uid, username="admin1", name="Admin One", role=UserRole.ADMIN,
                hashed_password="x", email="admin1@test.local")
    db.add(user)
    await db.flush()
    return user


def _ben(number: str = "1234567890", *, mode: str = "IMPS", name: str = "Rita",
         ifsc: str = "HDFC0009999") -> wa.Beneficiary:
    return wa.read_beneficiary(mode=mode, account_number=number, ifsc=ifsc, name=name)


async def _allocate(db: AsyncSession, amount: float, *, mode: str = "IMPS", **kw):
    kw.setdefault("beneficiary", _ben())
    return await wa.allocate_withdrawal_accounts(db, amount=amount, mode=mode, **kw)


# ═══ Rule 10 — the hard daily debit limit ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_limit_is_measured_against_todays_usage_not_the_raw_amount(db):
    """₹1,00,000 ceiling with ₹70,000 already paid accepts ₹30,000 and rejects ₹30,001.

    The naive test (amount <= highest_debit) would accept BOTH, which is the whole failure this
    engine exists to prevent.
    """
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 70000)
    r = await _allocate(db, 70000)
    await wa.write_legs(db, r, transaction=tx)

    assert (await _allocate(db, 30000)).allocated is True
    assert (await _allocate(db, 30001)).allocated is False
    # And the naive comparison would have said yes to both — 30,001 is well under the ceiling.
    assert 30001 < 100000


@pytest.mark.asyncio
async def test_reaching_the_limit_exactly_is_allowed(db):
    """Used Today + Amount == Highest Debit is accepted."""
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 60000)
    await wa.write_legs(db, await _allocate(db, 60000), transaction=tx)

    result = await _allocate(db, 40000)
    assert result.allocated is True
    assert result.legs[0].candidate.remaining == 40000.0


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [1, 0.01])
async def test_going_over_the_limit_is_rejected_by_any_margin(db, over):
    """Exceeding Highest Debit disqualifies the account — by ₹1 or by a single paisa.

    The paise case is the one that catches a sloppy comparison: unrounded float arithmetic can
    make 40000.01 read as within a 40000.00 gap.
    """
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 60000)
    await wa.write_legs(db, await _allocate(db, 60000), transaction=tx)

    assert (await _allocate(db, 40000 + over)).allocated is False


@pytest.mark.asyncio
async def test_an_account_with_no_configured_debit_limit_is_not_eligible(db):
    """An unconfigured limit must never read as permission to pay any amount."""
    await _account(db, "ACC1", debit=0.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    result = await _allocate(db, 1000)
    assert result.allocated is False
    assert result.failure_code == wa.FAIL_NO_LIMITS


@pytest.mark.asyncio
async def test_the_daily_limit_resets_on_the_ist_day_boundary(db):
    """Yesterday's payouts do not consume today's capacity, and nothing is reset or recalculated
    to make that true — tomorrow's query simply selects a different leg date."""
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    yesterday = wa.ist_today() - timedelta(days=1)
    tx = await _withdrawal(db, "1", 100000, on=yesterday)
    r = await wa.allocate_withdrawal_accounts(
        db, amount=100000, mode="IMPS", beneficiary=_ben(), on=yesterday)
    await wa.write_legs(db, r, transaction=tx, on=yesterday)

    assert (await wa.debit_used_today(db, ["ACC1"], on=yesterday)) == {"ACC1": 100000.0}
    assert (await wa.debit_used_today(db, ["ACC1"])) == {}
    assert (await _allocate(db, 100000)).allocated is True


# ═══ Rule 9 — available balance is a second, independent ceiling ════════════════════════════════

@pytest.mark.asyncio
async def test_headroom_without_money_is_not_enough(db):
    """Negative scenario E — plenty of Highest Debit remaining, not enough balance."""
    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 40000.0)

    result = await _allocate(db, 50000)
    assert result.allocated is False
    assert result.failure_code == wa.FAIL_NO_BALANCE
    rejected = {c.ref: c.reject_reason for c in result.candidates}
    assert rejected["ACC1"] == wa.REJECT_NO_BALANCE


@pytest.mark.asyncio
async def test_money_without_headroom_is_not_enough(db):
    """Negative scenario D — plenty of balance, insufficient Highest Debit remaining."""
    await _account(db, "ACC1", debit=30000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    result = await _allocate(db, 50000)
    assert result.allocated is False
    rejected = {c.ref: c.reject_reason for c in result.candidates}
    assert rejected["ACC1"] == wa.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_the_balance_used_is_the_ledger_balance_not_total_deposits(db):
    """Rule 9's "do not use total historical deposits as available payout balance".

    ₹1,00,000 in, ₹80,000 already paid out → ₹20,000 available, so a ₹50,000 withdrawal fails even
    though this account has historically received ₹1,00,000.
    """
    acc = await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 100000.0)
    paid = await _withdrawal(db, "1", 80000, status=TxStatus.COMPLETED)
    paid.payout_account_ref = "ACC1"
    paid.payout_payment_method = "BANK"
    await db.flush()

    assert await ledger.account_balance(db, "ACC1") == 20000.0
    assert (await _allocate(db, 50000)).allocated is False
    assert (await _allocate(db, 20000)).allocated is True
    assert acc.reference_number == "ACC1"


@pytest.mark.asyncio
async def test_an_allocated_but_unpaid_leg_reserves_the_money_it_promised(db):
    """Rule 27 — capacity is consumed at allocation, so the second request cannot spend it again.

    The account's ACCOUNTING balance is deliberately unchanged (no money has moved yet); what the
    ALLOCATION engine sees is that balance minus what is already promised.
    """
    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 100000.0)
    tx = await _withdrawal(db, "1", 60000)
    await wa.write_legs(db, await _allocate(db, 60000), transaction=tx)

    assert await ledger.account_balance(db, "ACC1") == 100000.0      # nothing has moved
    assert await ledger.reserved_by_legs(db, ["ACC1"]) == {"ACC1": 60000.0}
    assert (await _allocate(db, 50000)).allocated is False           # only 40,000 is really free
    assert (await _allocate(db, 40000)).allocated is True


# ═══ Rule 5 / 7 — availability and transaction mode ═════════════════════════════════════════════

@pytest.mark.asyncio
async def test_an_inactive_account_is_never_selected(db):
    await _account(db, "ACC1", status="INACTIVE", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    result = await _allocate(db, 1000)
    assert result.allocated is False
    assert result.failure_code == wa.FAIL_ALL_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["UPI", "IMPS", "NEFT", "RTGS"])
async def test_only_an_account_supporting_the_requested_mode_is_used(db, mode):
    """Negative scenario G — an account that cannot process the mode is excluded, however much
    balance and headroom it has."""
    await _account(db, "RICH", debit=10000000.0, modes="NEFT")
    await _account(db, "ABLE", debit=100000.0, modes="UPI,IMPS,RTGS")
    await _fund(db, "D1", "RICH", 10000000.0)
    await _fund(db, "D2", "ABLE", 1000000.0)

    result = await _allocate(db, 5000, mode=mode)
    assert result.allocated is True
    assert result.legs[0].ref == ("RICH" if mode == "NEFT" else "ABLE")


@pytest.mark.asyncio
async def test_an_unconfigured_account_supports_every_mode(db):
    """The lesson the deposit engine learned the hard way: a payment-method capability must never
    be the reason there is NO eligible account. An account nobody has configured is fully capable
    until an Admin narrows it."""
    acc = await _account(db, "ACC1", modes=None, debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    assert wa.account_modes(acc) is None
    for mode in wa.TRANSACTION_MODES:
        assert (await _allocate(db, 1000, mode=mode)).allocated is True


@pytest.mark.asyncio
async def test_the_generic_bank_mode_is_satisfied_by_any_bank_scheme(db):
    """"BANK" names the rail, not a scheme — an IMPS-capable account can serve it. A UPI-only
    account cannot."""
    await _account(db, "UPIONLY", modes="UPI", debit=1000000.0)
    await _account(db, "IMPSCAP", modes="IMPS", debit=1000000.0)
    await _fund(db, "D1", "UPIONLY", 1000000.0)
    await _fund(db, "D2", "IMPSCAP", 1000000.0)

    result = await _allocate(db, 1000, mode="BANK")
    assert result.allocated is True
    assert result.legs[0].ref == "IMPSCAP"


@pytest.mark.asyncio
async def test_no_account_supports_the_mode_is_its_own_reported_failure(db):
    """An Admin fixes "enable RTGS somewhere" differently from "top up an account", so the two
    are reported separately."""
    await _account(db, "ACC1", modes="UPI", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    result = await _allocate(db, 1000, mode="RTGS")
    assert result.allocated is False
    assert result.failure_code == wa.FAIL_MODE_UNAVAILABLE
    assert "RTGS" in result.reason


# ═══ Rules 12 / 13 — one account preferred, nearest suitable capacity ═══════════════════════════

@pytest.mark.asyncio
async def test_the_nearest_suitable_capacity_wins(db):
    """The specification's own example: ₹45,000 against remaining capacities of 50k/60k/70k/95k
    selects the ₹50,000 account, leaving the bigger ones free for requests only they can take."""
    for ref, debit in (("A", 50000.0), ("B", 60000.0), ("C", 70000.0), ("D", 95000.0)):
        await _account(db, ref, debit=debit)
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 45000)
    assert result.allocated is True
    assert [l.ref for l in result.legs] == ["A"]
    assert result.rule == wa.RULES.NEAREST_CAPACITY


@pytest.mark.asyncio
async def test_one_account_is_preferred_over_splitting_unnecessarily(db):
    """Rule 12 — an account that can carry the whole amount is used alone, even when several
    smaller ones could have covered it together."""
    await _account(db, "SMALL1", debit=30000.0)
    await _account(db, "SMALL2", debit=30000.0)
    await _account(db, "WHOLE", debit=50000.0)
    for ref in ("SMALL1", "SMALL2", "WHOLE"):
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 45000)
    assert len(result.legs) == 1 and result.legs[0].ref == "WHOLE"


@pytest.mark.asyncio
async def test_ranking_is_deterministic_on_a_tie(db):
    """Identical capacity must not produce a random winner: the same inputs always give the same
    answer, so an allocation can be explained and reproduced."""
    for ref in ("Z", "M", "A"):
        await _account(db, ref, debit=50000.0)
        await _fund(db, f"D{ref}", ref, 1000000.0)

    picks = {(await _allocate(db, 1000)).legs[0].ref for _ in range(5)}
    assert picks == {"A"}


# ═══ Rules 2 / 14 / 15 — merchant note, same account, beneficiary ═══════════════════════════════

@pytest.mark.asyncio
async def test_a_requested_bank_evaluates_every_account_at_that_bank(db):
    """The specification's example: four Bank of Baroda accounts, and the BEST eligible one is
    chosen — never simply the first that matches the name."""
    await _account(db, "BOB1", bank="Bank of Baroda", status="INACTIVE")   # unavailable
    await _account(db, "BOB2", bank="Bank of Baroda", debit=1000000.0)     # too little money
    await _account(db, "BOB3", bank="Bank of Baroda", debit=100000.0)      # eligible
    await _account(db, "BOB4", bank="Bank of Baroda", debit=200000.0)      # eligible, further away
    await _fund(db, "D2", "BOB2", 30000.0)
    await _fund(db, "D3", "BOB3", 500000.0)
    await _fund(db, "D4", "BOB4", 500000.0)
    tx = await _withdrawal(db, "1", 50000)
    await wa.write_legs(db, await _allocate(db, 50000, note="Use Bank of Baroda"), transaction=tx)

    # BOB3 now has 50,000 of headroom left and BOB4 has 200,000 — 45,000 fits both; nearest wins.
    result = await _allocate(db, 45000, note="Use Bank of Baroda")
    assert result.allocated is True
    assert result.legs[0].ref == "BOB3"
    rejected = {c.ref: c.reject_reason for c in result.candidates}
    assert rejected["BOB1"] == wa.REJECT_INACTIVE
    assert rejected["BOB2"] == wa.REJECT_NO_BALANCE


@pytest.mark.asyncio
async def test_a_requested_bank_that_cannot_pay_it_all_still_pays_what_it_can(db):
    """Negative scenarios B and C — a requested bank that cannot cover the amount is neither
    forced nor discarded: it pays every rupee it has and another bank completes the rest.

    Bank of Baroda has one unusable account and one holding ₹1,000 of capacity. ₹1,000 is not
    ₹50,000, so HDFC must be brought in — but bringing HDFC in for the WHOLE amount, as this used
    to, ignores capacity the merchant explicitly asked to use. The preference is honoured as far
    as it goes, and the shortfall is recorded with the figure that explains it.
    """
    await _account(db, "BOB1", bank="Bank of Baroda", status="INACTIVE")
    await _account(db, "BOB2", bank="Bank of Baroda", debit=1000.0)
    await _account(db, "HDFC1", bank="HDFC Bank", debit=1000000.0)
    await _fund(db, "D2", "BOB2", 1000000.0)
    await _fund(db, "D3", "HDFC1", 1000000.0)

    result = await _allocate(db, 50000, note="Use Bank of Baroda")
    assert result.allocated is True
    assert {l.ref: l.amount for l in result.legs} == {"BOB2": 1000.0, "HDFC1": 49000.0}
    assert result.legs[0].ref == "BOB2", "the requested bank is drawn on FIRST"
    assert result.total == 50000.0
    assert result.requested_unavailable is True
    assert result.detail["requestedBankUnavailable"] == "Bank of Baroda"
    assert result.detail["requestedBankPartial"]["usableCapacity"] == 1000.0
    assert result.detail["requestedBankPartial"]["shortfall"] == 49000.0


@pytest.mark.asyncio
async def test_a_note_naming_a_bank_with_no_eligible_account_says_which_bank(db):
    """"No eligible account" and "the bank you asked for has no eligible account" send an Admin
    looking in different places."""
    await _account(db, "BOB1", bank="Bank of Baroda", status="INACTIVE")
    result = await _allocate(db, 50000, note="Use Bank of Baroda")

    assert result.allocated is False
    assert "Bank of Baroda" in result.reason


@pytest.mark.asyncio
async def test_same_account_reuses_the_account_the_member_was_last_paid_from(db):
    """Rule 14 — and deliberately the last PAYING account, not the account the member's last
    deposit was received into, which would name a different account for the same words."""
    await _account(db, "PAYER", debit=1000000.0)
    await _account(db, "OTHER", debit=50000.0)
    await _fund(db, "D1", "PAYER", 1000000.0)
    await _fund(db, "D2", "OTHER", 1000000.0)
    previous = await _withdrawal(db, "1", 10000, status=TxStatus.COMPLETED, number="9999999999")
    previous.payout_account_ref = "PAYER"
    await db.flush()

    # OTHER has the nearer capacity and would win outright; the note redirects to PAYER.
    assert (await _allocate(db, 20000)).legs[0].ref == "OTHER"
    result = await _allocate(db, 20000, member_id="MBR1", note="Use the same account")
    assert result.legs[0].ref == "PAYER"
    assert result.rule == wa.RULES.SAME_ACCOUNT


@pytest.mark.asyncio
async def test_same_account_is_not_forced_when_it_is_no_longer_eligible(db):
    """Negative scenario H — the previous account is checked against every mandatory rule like any
    other candidate, and a failure falls back to normal allocation with the reason recorded."""
    await _account(db, "PAYER", debit=1000000.0, status="INACTIVE")
    await _account(db, "OTHER", debit=100000.0)
    await _fund(db, "D2", "OTHER", 1000000.0)
    previous = await _withdrawal(db, "1", 10000, status=TxStatus.COMPLETED, number="9999999999")
    previous.payout_account_ref = "PAYER"
    await db.flush()

    result = await _allocate(db, 20000, member_id="MBR1", note="same account")
    assert result.allocated is True
    assert result.legs[0].ref == "OTHER"
    assert result.detail["sameAccountRejected"]["accountRef"] == "PAYER"
    assert result.detail["sameAccountRejected"]["reason"] == wa.REJECT_INACTIVE


@pytest.mark.asyncio
async def test_an_account_that_already_paid_this_beneficiary_is_preferred(db):
    """Rule 15 — beneficiary-first, expressed against real payout history rather than a new
    registry the platform does not have."""
    await _account(db, "KNOWN", debit=200000.0)
    await _account(db, "FRESH", debit=100000.0)
    await _fund(db, "D1", "KNOWN", 1000000.0)
    await _fund(db, "D2", "FRESH", 1000000.0)
    previous = await _withdrawal(db, "1", 5000, status=TxStatus.COMPLETED, number="1234567890")
    previous.payout_account_ref = "KNOWN"
    await db.flush()

    # FRESH has the nearer capacity, so without the beneficiary rule it would win.
    assert (await wa.accounts_that_paid_beneficiary(db, _ben("1234567890"))) == {"KNOWN"}
    result = await _allocate(db, 20000)
    assert result.legs[0].ref == "KNOWN"
    assert result.rule == wa.RULES.BENEFICIARY_KNOWN


@pytest.mark.asyncio
async def test_the_beneficiary_preference_never_excuses_a_limit_breach(db):
    """Preference pools are subsets of the ELIGIBLE set, so a known beneficiary can change which
    account pays but never whether an ineligible one becomes acceptable."""
    await _account(db, "KNOWN", debit=1000.0)         # known, but nowhere near enough headroom
    await _account(db, "FRESH", debit=100000.0)
    await _fund(db, "D1", "KNOWN", 1000000.0)
    await _fund(db, "D2", "FRESH", 1000000.0)
    previous = await _withdrawal(db, "1", 500, status=TxStatus.COMPLETED, number="1234567890")
    previous.payout_account_ref = "KNOWN"
    await db.flush()

    result = await _allocate(db, 20000)
    assert result.legs[0].ref == "FRESH"


@pytest.mark.asyncio
async def test_incomplete_beneficiary_details_are_their_own_exception(db):
    """Negative scenario F — the problem is the request, not the accounts, so no account is
    evaluated and the reason says which fields are missing."""
    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    result = await wa.allocate_withdrawal_accounts(
        db, amount=1000, mode="IMPS",
        beneficiary=wa.read_beneficiary(mode="IMPS", account_number="", ifsc="", name="Rita"))
    assert result.allocated is False
    assert result.failure_code == wa.FAIL_BENEFICIARY
    assert "Account Number" in result.reason and "IFSC" in result.reason
    assert result.candidates == []          # the accounts were never the problem


@pytest.mark.asyncio
async def test_a_upi_withdrawal_needs_a_upi_id(db):
    result = wa.read_beneficiary(mode="UPI", payout_details={})
    assert result.valid is False
    assert wa.read_beneficiary(mode="UPI", payout_details={"upiId": "rita@hdfc"}).valid is True


# ═══ Rules 16 / 17 / 18 — multi-account payout ══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_withdrawal_no_single_account_can_carry_is_split(db):
    """The specification's own example: ₹1,50,000 across usable capacities of 70k / 50k / 40k."""
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _account(db, "C", debit=40000.0)
    await _fund(db, "DA", "A", 80000.0)
    await _fund(db, "DB", "B", 60000.0)
    await _fund(db, "DC", "C", 200000.0)

    result = await _allocate(db, 150000)
    assert result.allocated is True and result.split is True
    assert result.total == 150000.0
    assert sorted((l.ref, l.amount) for l in result.legs) == [
        ("A", 70000.0), ("B", 50000.0), ("C", 30000.0)]
    assert result.rule == wa.RULES.SPLIT


@pytest.mark.asyncio
async def test_every_leg_of_a_split_stays_within_its_own_two_ceilings(db):
    """A leg is capped at min(available balance, remaining debit capacity) — never at whichever
    of the two happens to be larger."""
    await _account(db, "RICH_TIGHT", debit=20000.0)      # lots of money, little headroom
    await _account(db, "POOR_LOOSE", debit=500000.0)     # lots of headroom, little money
    await _account(db, "BIG", debit=500000.0)
    await _fund(db, "D1", "RICH_TIGHT", 900000.0)
    await _fund(db, "D2", "POOR_LOOSE", 15000.0)
    await _fund(db, "D3", "BIG", 100000.0)

    result = await _allocate(db, 130000)
    assert result.total == 130000.0
    amounts = dict((l.ref, l.amount) for l in result.legs)
    assert amounts.get("RICH_TIGHT", 0) <= 20000.0       # capped by the daily limit
    assert amounts.get("POOR_LOOSE", 0) <= 15000.0       # capped by the balance
    assert amounts.get("BIG", 0) <= 100000.0


@pytest.mark.asyncio
async def test_a_split_uses_the_fewest_accounts_it_can(db):
    """Rule 17.4 — ₹1,00,000 is taken from the one ₹1,00,000 account plus nothing else, rather
    than dribbled across the four small ones."""
    await _account(db, "BIG", debit=99999.0)
    for ref in ("S1", "S2", "S3", "S4"):
        await _account(db, ref, debit=30000.0)
        await _fund(db, f"D{ref}", ref, 1000000.0)
    await _fund(db, "DBIG", "BIG", 1000000.0)

    result = await _allocate(db, 100000)
    assert result.total == 100000.0
    # BIG (99,999) plus one small account for the last rupee — not four small ones for 100,000,
    # and not the three-way split a naive nearest-capacity-first walk would have produced.
    assert len(result.legs) == 2
    assert dict((l.ref, l.amount) for l in result.legs)["BIG"] == 99999.0


@pytest.mark.asyncio
async def test_the_legs_always_sum_to_exactly_the_requested_amount(db):
    """Never ₹1,49,999 and never ₹1,50,001 — including on amounts that carry paise."""
    for ref, debit in (("A", 33333.33), ("B", 44444.44), ("C", 55555.55)):
        await _account(db, ref, debit=debit)
        await _fund(db, f"D{ref}", ref, 1000000.0)

    for amount in (100000.00, 99999.99, 133333.33, 0.01):
        result = await _allocate(db, amount)
        if result.allocated:
            assert result.total == round(amount, 2), (amount, result.total)


@pytest.mark.asyncio
async def test_capacity_short_of_the_amount_allocates_nothing_at_all(db):
    """Rule 18 / negative scenario J — ₹2,00,000 against ₹1,65,000 of capacity produces an
    exception, NOT a ₹1,65,000 payout."""
    await _account(db, "A", debit=100000.0)
    await _account(db, "B", debit=65000.0)
    await _fund(db, "DA", "A", 1000000.0)
    await _fund(db, "DB", "B", 1000000.0)

    result = await _allocate(db, 200000)
    assert result.allocated is False
    assert result.legs == []
    assert result.failure_code == wa.FAIL_CAPACITY
    assert result.detail["totalUsableCapacity"] == 165000.0
    assert (await db.execute(select(WithdrawalPayoutLeg))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_split_prefers_the_requested_bank_when_it_can_cover_the_amount(db):
    """A note shapes the split, but only where the requested bank can actually carry the whole
    amount — forcing it otherwise would fail a withdrawal the platform can pay."""
    await _account(db, "BOB1", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "BOB2", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "HDFC1", bank="HDFC Bank", debit=60000.0)
    for ref in ("BOB1", "BOB2", "HDFC1"):
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 100000, note="Use Bank of Baroda")
    assert result.total == 100000.0
    assert {l.ref for l in result.legs} == {"BOB1", "BOB2"}


@pytest.mark.asyncio
async def test_the_requested_bank_beats_one_account_somewhere_else(db):
    """Where Rule 12 and Rule 2 pull against each other, the REQUESTED BANK wins.

    "Prefer one account rather than splitting unnecessarily" decides between accounts the merchant
    is indifferent about. It is not a reason to move a payout to a bank they did not ask for: two
    Bank of Baroda accounts covering ₹1,00,000 between them is an allocation the requested bank
    CAN make, so HDFC — which could have carried it alone — must not replace it.

    This is the precedence reversed deliberately. The single-account rule is untouched wherever no
    bank was named; see ``test_a_single_account_is_preferred_when_no_bank_is_requested``.
    """
    await _account(db, "BOB1", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "BOB2", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "HDFC1", bank="HDFC Bank", debit=500000.0)
    for ref in ("BOB1", "BOB2", "HDFC1"):
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 100000, note="Use Bank of Baroda")
    assert {l.ref for l in result.legs} == {"BOB1", "BOB2"}
    assert result.total == 100000.0
    assert "HDFC1" not in {l.ref for l in result.legs}, "the requested bank was sufficient"
    assert result.requested_unavailable is False, "the preference was met, not missed"


@pytest.mark.asyncio
async def test_a_single_account_is_preferred_when_no_bank_is_requested(db):
    """The control for the test above: with no note, ONE account still beats a split.

    Same three accounts, same amount, no preference — and the answer is the single HDFC account.
    The bank-preference rule reorders nothing that a merchant did not ask it to reorder.
    """
    await _account(db, "BOB1", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "BOB2", bank="Bank of Baroda", debit=60000.0)
    await _account(db, "HDFC1", bank="HDFC Bank", debit=500000.0)
    for ref in ("BOB1", "BOB2", "HDFC1"):
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 100000)
    assert [l.ref for l in result.legs] == ["HDFC1"]
    assert result.requested_unavailable is False


@pytest.mark.asyncio
async def test_a_split_falls_outside_the_requested_bank_rather_than_failing(db):
    """When the requested bank cannot cover the amount even collectively, the withdrawal is still
    paid — from wherever it can be — and the unfulfilled preference is recorded."""
    await _account(db, "BOB1", bank="Bank of Baroda", debit=20000.0)
    await _account(db, "HDFC1", bank="HDFC Bank", debit=60000.0)
    await _account(db, "ICICI1", bank="ICICI Bank", debit=60000.0)
    for ref in ("BOB1", "HDFC1", "ICICI1"):
        await _fund(db, f"D{ref}", ref, 1000000.0)

    result = await _allocate(db, 100000, note="Use Bank of Baroda")
    assert result.allocated is True
    assert result.total == 100000.0
    assert len(result.legs) >= 2


# ═══ Rule 11 / 27 — concurrency ═════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_two_withdrawals_cannot_both_spend_the_same_headroom(db):
    """The specification's concurrency example: a ₹1,00,000 account cannot take ₹60,000 AND
    ₹50,000. The first allocation's leg holds the capacity, so the second must look elsewhere —
    and finding nowhere, must fail rather than breach the ceiling."""
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 100000.0)

    first = await _withdrawal(db, "1", 60000)
    r1 = await _allocate(db, 60000)
    assert r1.allocated is True
    await wa.write_legs(db, r1, transaction=first)

    r2 = await _allocate(db, 50000)
    assert r2.allocated is False
    used = await wa.debit_used_today(db, ["ACC1"])
    assert used["ACC1"] == 60000.0
    assert used["ACC1"] + 50000 > 100000        # what would have happened without the reservation


@pytest.mark.asyncio
async def test_releasing_a_withdrawal_returns_the_capacity_it_held(db):
    """A rejected or cancelled withdrawal frees its accounts automatically — the leg IS the
    reservation, so there is no separate record to fall out of step."""
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 100000)
    await wa.write_legs(db, await _allocate(db, 100000), transaction=tx)
    assert (await _allocate(db, 1000)).allocated is False

    released = await wa.release_legs(db, tx.ref, reason=wa.RELEASE_REJECTED)
    assert released == 1
    assert (await wa.debit_used_today(db, ["ACC1"])) == {}
    assert (await _allocate(db, 100000)).allocated is True

    # History is kept, not deleted.
    legs = (await db.execute(select(WithdrawalPayoutLeg))).scalars().all()
    assert [l.status for l in legs] == [ledger.LEG_RELEASED]
    assert legs[0].released_reason == wa.RELEASE_REJECTED


@pytest.mark.asyncio
async def test_reallocating_replaces_the_previous_legs_rather_than_stacking_them(db):
    """A retried allocation must not leave the old legs holding capacity as well as the new ones."""
    await _account(db, "A", debit=100000.0)
    await _account(db, "B", debit=100000.0)
    await _fund(db, "DA", "A", 1000000.0)
    await _fund(db, "DB", "B", 1000000.0)
    tx = await _withdrawal(db, "1", 50000)

    await wa.write_legs(db, await _allocate(db, 50000), transaction=tx)
    await wa.write_legs(db, await _allocate(db, 50000, note="Use ACC-B"), transaction=tx)

    live = await wa.live_legs(db, tx.ref)
    assert len(live) == 1
    assert sum((await wa.debit_used_today(db, ["A", "B"])).values()) == 50000.0


# ═══ Rule 6 / 31 — account type and accessibility ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_account_type_is_carried_through_the_decision_not_invented_into_a_rule(db):
    """Rule 6 says to preserve an EXISTING account-type restriction. The withdrawal workflow has
    none — the platform's accounts are company-level, and no rule says a Savings account may not
    pay — so the type is recorded on every candidate and every leg, and nothing is filtered on it.
    Inventing a restriction would change which account real money leaves."""
    await _account(db, "SAV", atype=AccountType.SAVINGS, debit=50000.0)
    await _account(db, "CUR", atype=AccountType.CURRENT, debit=90000.0)
    await _fund(db, "D1", "SAV", 1000000.0)
    await _fund(db, "D2", "CUR", 1000000.0)

    result = await _allocate(db, 45000)
    assert result.legs[0].ref == "SAV"                  # chosen on capacity, not excluded on type
    types = {c.ref: c.snapshot()["accountType"] for c in result.candidates}
    assert types == {"SAV": "Savings Account", "CUR": "Current Account"}


# ═══ Rule 32 — the audit journal ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_successful_allocation_records_why_and_what_it_rejected(db):
    """Rule 32's worked example: the selected account, the rule, the figures it rested on, and the
    rejected candidates with their reasons — all stored, because none of them can be recovered
    later."""
    await _account(db, "BOB1", bank="Bank of Baroda", status="INACTIVE")
    await _account(db, "BOB2", bank="Bank of Baroda", debit=50000.0)
    await _account(db, "BOB3", bank="Bank of Baroda", debit=200000.0)
    await _fund(db, "D2", "BOB2", 1000000.0)
    await _fund(db, "D3", "BOB3", 1000000.0)
    tx = await _withdrawal(db, "1", 45000, notes="Use Bank of Baroda")

    result = await _allocate(db, 45000, note="Use Bank of Baroda")
    row = await wa.record_allocation(db, result, transaction=tx, triggered_by="System")

    assert row.outcome == wa.OUTCOME_ALLOCATED
    assert row.account_ref == "BOB2"
    assert row.highest_debit == 50000.0
    assert row.debit_used_today == 0.0
    assert row.remaining_capacity == 50000.0
    assert row.candidates_considered == 3
    assert row.candidates_eligible == 2
    assert "Bank of Baroda" in (row.reason or "")

    detail = wa.serialize(row)["detail"]
    rejected = {a["accountRef"]: a["rejectReason"] for a in detail["accounts"]}
    assert rejected["BOB1"] == wa.REJECT_INACTIVE
    assert rejected["BOB2"] is None


@pytest.mark.asyncio
async def test_a_failed_allocation_is_journalled_too(db):
    """The failure is the more valuable of the two records: "no eligible account for ₹45,000" is
    exactly the question support gets asked."""
    await _account(db, "ACC1", debit=1000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 45000)

    result = await _allocate(db, 45000)
    row = await wa.record_allocation(db, result, transaction=tx)

    assert row.outcome == wa.OUTCOME_NO_ACCOUNT
    assert row.account_ref is None
    assert row.failure_code == wa.FAIL_CAPACITY
    assert row.leg_count is None


@pytest.mark.asyncio
async def test_a_split_is_journalled_with_its_leg_count_and_total(db):
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _fund(db, "DA", "A", 1000000.0)
    await _fund(db, "DB", "B", 1000000.0)
    tx = await _withdrawal(db, "1", 100000)

    result = await _allocate(db, 100000)
    row = await wa.record_allocation(db, result, transaction=tx)

    assert row.outcome == wa.OUTCOME_SPLIT
    assert row.leg_count == 2
    assert row.allocated_amount == 100000.0
    assert row.account_ref is None          # a split names no single account — read the legs


# ═══ Serialisation ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_merchants_payout_card_masks_the_account_number(db):
    """The merchant is told WHICH account pays them, not given its full number — the platform has
    never exposed a payout account's number and this does not start."""
    await _account(db, "ACC1", debit=1000000.0, number="123456789012")
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 1000)
    result = await _allocate(db, 1000)
    legs = await wa.write_legs(db, result, transaction=tx)

    card = result.snapshot()[0]
    assert card["accountNumberMasked"] == "•••• 9012"
    assert "123456789012" not in str(card)
    assert wa.serialize_leg(legs[0])["accountNumber"] == "•••• 9012"
    assert wa.serialize_leg(legs[0], mask=False)["accountNumber"] == "123456789012"


# ═══ The workflow, end to end ═══════════════════════════════════════════════════════════════════
# These exercise the real endpoints, so they prove the thing the feature is actually for: a normal
# eligible withdrawal never waits for an Admin to choose an account.

def _payload(amount: float, **kw) -> WithdrawalCreate:
    base = dict(
        amount=amount, memberId="MBR1", memberName="Rita", payoutMode="IMPS",
        accountHolder="Rita", accountNumber="1234567890", ifsc="HDFC0009999",
        bankName="HDFC Bank", branch="Mumbai",
        # As above: these tests are about the PAYING account, not the member's account metadata.
        accountType="SAVINGS",
    )
    base.update(kw)
    return WithdrawalCreate(**base)


async def _manager(db: AsyncSession, uid: int = 8) -> User:
    user = User(id=uid, username="mgr1", name="BELLAGIO", role=UserRole.MERCHANT,
                hashed_password="x", email="mgr1@test.local", merchant_role="MANAGER")
    db.add(user)
    await db.flush()
    return user


async def _create(db: AsyncSession, merchant: User, amount: float, **kw) -> dict:
    return await txr.create_withdrawal(_payload(amount, **kw), None, db, merchant)


@pytest.mark.asyncio
async def test_a_created_withdrawal_is_allocated_a_paying_account_immediately(db):
    """The merchant sees WHICH account will pay them on the request they just raised — no Admin,
    and no waiting."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0, number="123456789012")
    await _fund(db, "D1", "ACC1", 500000.0)

    out = await _create(db, merchant, 45000)

    assert out["status"] == TxStatus.MANAGER_REVIEW      # the review gate is untouched
    assert out["payoutLegs"] == [{
        "legNo": 1, "accountRef": "ACC1", "accountName": "ACC-ACC1", "bankName": "HDFC Bank",
        "accountNumber": "•••• 9012", "ifsc": "HDFC0001234", "branch": "Mumbai",
        "accountType": "Current Account", "transactionMode": "IMPS", "amount": 45000.0,
        "status": "ALLOCATED", "ledgerEntryRef": None,
        "allocatedAt": out["payoutLegs"][0]["allocatedAt"],
        "allocatedAtIst": out["payoutLegs"][0]["allocatedAtIst"], "paidAt": None,
    }]
    assert out["payoutAllocatedTotal"] == 45000.0
    assert out["payoutAccountRef"] == "ACC1"


@pytest.mark.asyncio
async def test_manager_approval_no_longer_parks_a_withdrawal_in_account_requested(db):
    """THE state-machine change. A Manager-approved withdrawal used to land in ACCOUNT_REQUESTED,
    which for a withdrawal meant "an Admin must now choose which account pays this". It now lands
    in ACCOUNT_SUBMITTED with the account already attached — ready to be PAID, not chosen."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["status"] != TxStatus.ACCOUNT_REQUESTED
    assert [l["accountRef"] for l in out["payoutLegs"]] == ["ACC1"]


@pytest.mark.asyncio
async def test_a_withdrawal_with_no_eligible_account_becomes_an_exception_not_a_queue(db):
    """Negative scenario A. NO_ELIGIBLE_ACCOUNT is an explicit exception carrying a reason — not
    ACCOUNT_REQUESTED, which used to mean "waiting for an Admin to pick an account"."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db, "ACC1", debit=1000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    assert created["payoutLegs"] == []

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT
    journal = (await db.execute(select(WithdrawalAllocation))).scalars().all()
    assert journal[-1].outcome == wa.OUTCOME_NO_ACCOUNT
    assert journal[-1].failure_code == wa.FAIL_CAPACITY


@pytest.mark.asyncio
async def test_an_admin_retry_places_a_withdrawal_once_capacity_exists(db):
    """The ONE remaining Admin task: fix the configuration, press retry, and the ENGINE picks the
    account. The Admin still never chooses which one."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    acc = await _account(db, "ACC1", debit=1000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    acc.highest_debit = 100000.0                    # the Admin raises the limit
    await db.flush()
    out = await txr.retry_payout_allocation(created["id"], None, db, admin)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert [l["accountRef"] for l in out["payoutLegs"]] == ["ACC1"]


@pytest.mark.asyncio
async def test_completion_debits_the_allocated_account_and_writes_the_ledger(db):
    """Rules 22-25: the debit lands on the account the engine chose, through the EXISTING ledger,
    with the balance before and after recorded."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    out = await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    assert out["status"] == TxStatus.COMPLETED
    assert out["payoutAccountRef"] == "ACC1"
    assert out["payoutPaymentMethod"] == "BANK"
    entries = (await db.execute(select(AccountLedgerEntry))).scalars().all()
    assert len(entries) == 1
    assert (entries[0].account_ref, entries[0].amount) == ("ACC1", 45000.0)
    assert (entries[0].balance_before, entries[0].balance_after) == (500000.0, 455000.0)
    assert entries[0].leg_no == 1
    assert await ledger.account_balance(db, "ACC1") == 455000.0
    legs = (await db.execute(select(WithdrawalPayoutLeg))).scalars().all()
    assert [l.status for l in legs] == [ledger.LEG_PAID]
    assert legs[0].ledger_entry_ref == entries[0].entry_ref


@pytest.mark.asyncio
async def test_a_split_payout_debits_every_account_its_own_share(db):
    """Rule 24 — each leg is its own auditable ledger entry, all linked to the one withdrawal."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _account(db, "C", debit=40000.0)
    await _fund(db, "DA", "A", 80000.0)
    await _fund(db, "DB", "B", 60000.0)
    await _fund(db, "DC", "C", 200000.0)
    created = await _create(db, merchant, 150000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    out = await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    assert out["status"] == TxStatus.COMPLETED
    assert out["payoutAccountRef"] is None            # a split names no single account
    entries = (await db.execute(
        select(AccountLedgerEntry).order_by(AccountLedgerEntry.id))).scalars().all()
    assert {(e.account_ref, e.amount) for e in entries} == {
        ("A", 70000.0), ("B", 50000.0), ("C", 30000.0)}
    assert {e.transaction_ref for e in entries} == {out["ref"]}
    assert sorted(e.leg_no for e in entries) == [1, 2, 3]
    assert sum(e.amount for e in entries) == 150000.0
    # Every account's balance reflects exactly its own share.
    assert await ledger.account_balance(db, "A") == 10000.0
    assert await ledger.account_balance(db, "B") == 10000.0
    assert await ledger.account_balance(db, "C") == 170000.0


@pytest.mark.asyncio
async def test_a_duplicate_completion_never_debits_twice(db):
    """Negative scenario L / Rule 26 — idempotent, and enforced by the database rather than by a
    disabled button."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    first = await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)
    second = await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    assert first["status"] == second["status"] == TxStatus.COMPLETED
    entries = (await db.execute(select(AccountLedgerEntry))).scalars().all()
    assert len(entries) == 1
    assert await ledger.account_balance(db, "ACC1") == 455000.0


@pytest.mark.asyncio
async def test_a_duplicate_completion_of_a_split_never_debits_twice(db):
    """The same guarantee across several legs, where a naive per-transaction guard would let the
    second and third entries through."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _fund(db, "DA", "A", 100000.0)
    await _fund(db, "DB", "B", 100000.0)
    created = await _create(db, merchant, 100000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)
    await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    entries = (await db.execute(select(AccountLedgerEntry))).scalars().all()
    assert len(entries) == 2
    assert sum(e.amount for e in entries) == 100000.0


@pytest.mark.asyncio
async def test_manual_offline_payment_is_preserved_and_releases_the_allocation(db):
    """Rule 36 — the existing Manual / Offline capability still works, debits no managed account,
    and hands back the capacity the engine had allocated."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    out = await txr.mark_done(
        created["id"], None,
        CompleteRequest(paymentMethod="MANUAL", manualReference="CASHIER-77"), db, admin)

    assert out["status"] == TxStatus.COMPLETED
    assert out["payoutPaymentMethod"] == "MANUAL"
    assert out["payoutAccountRef"] is None
    entry = (await db.execute(select(AccountLedgerEntry))).scalar_one()
    assert entry.account_ref is None and entry.balance_before is None
    # The account was never debited, and its capacity is free again.
    assert await ledger.account_balance(db, "ACC1") == 500000.0
    assert await wa.debit_used_today(db, ["ACC1"]) == {}


@pytest.mark.asyncio
async def test_an_account_supplied_by_the_caller_is_validated_not_trusted(db):
    """Rule 33 — a payout account reference arriving from the browser faces the identical hard
    rules. One that cannot pay is refused; one that can is accepted and the override is audited."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "GOOD", debit=100000.0)
    await _account(db, "BROKE", debit=100000.0)
    await _fund(db, "D1", "GOOD", 500000.0)
    await _fund(db, "D2", "BROKE", 100.0)
    created = await _create(db, merchant, 45000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    with pytest.raises(HTTPException) as err:
        await txr.mark_done(
            created["id"], None,
            CompleteRequest(paymentMethod="BANK", payoutAccountRef="BROKE"), db, admin)
    assert err.value.status_code == 400
    assert (await db.execute(select(AccountLedgerEntry))).scalars().all() == []


@pytest.mark.asyncio
async def test_an_eligible_account_supplied_by_the_caller_is_accepted_and_audited(db):
    """The manual escape valve still exists — it just cannot break a rule to be used."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "AUTO", debit=50000.0)
    await _account(db, "CHOSEN", debit=200000.0)
    await _fund(db, "D1", "AUTO", 500000.0)
    await _fund(db, "D2", "CHOSEN", 500000.0)
    created = await _create(db, merchant, 45000)
    assert created["payoutLegs"][0]["accountRef"] == "AUTO"      # nearest capacity
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    out = await txr.mark_done(
        created["id"], None,
        CompleteRequest(paymentMethod="BANK", payoutAccountRef="CHOSEN"), db, admin)

    assert out["payoutAccountRef"] == "CHOSEN"
    assert await ledger.account_balance(db, "CHOSEN") == 455000.0
    assert await ledger.account_balance(db, "AUTO") == 500000.0     # never touched
    codes = [a.action_type for a in (await db.execute(select(AuditLog))).scalars().all()]
    assert "WITHDRAWAL_PAYOUT_OVERRIDDEN" in codes


@pytest.mark.asyncio
async def test_a_rejected_withdrawal_hands_its_capacity_back(db):
    """Negative scenario K's other half: capacity held by a request that never completes must not
    stay held."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 100000)
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 100000.0

    await txr.manager_reject(created["id"], RemarkRequest(remark="not today"), None, db, manager)

    assert await wa.debit_used_today(db, ["ACC1"]) == {}
    assert (await _create(db, merchant, 100000))["payoutLegs"][0]["accountRef"] == "ACC1"


@pytest.mark.asyncio
async def test_two_withdrawals_created_back_to_back_cannot_breach_the_limit(db):
    """Rule 27's example at the ROUTE level: ₹1,00,000 of balance and headroom, then requests of
    ₹60,000 and ₹50,000. The second must not get this account."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 100000.0)
    # The merchant's own spendable balance is a SEPARATE, pre-existing guard (a business cannot
    # withdraw more than it holds). Funding it well above the two requests keeps that guard out of
    # the way, so what this test measures is the account-side limit and nothing else.
    await _account(db, "PARKED", status="INACTIVE")
    await _fund(db, "D2", "PARKED", 5000000.0)

    first = await _create(db, merchant, 60000)
    second = await _create(db, merchant, 50000)

    assert [l["accountRef"] for l in first["payoutLegs"]] == ["ACC1"]
    assert second["payoutLegs"] == []
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 60000.0


@pytest.mark.asyncio
async def test_a_cash_withdrawal_never_goes_near_the_engine(db):
    """Cash is handed over in person, so it debits no managed account and keeps the manual
    workflow it has always had. Same for crypto."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)

    out = await _create(db, merchant, 1000, payoutMode="CASH",
                        payoutDetails={"village": "X", "city": "Y", "mobile": "9", "pinCode": "1"})

    assert out["payoutLegs"] == []
    assert out["status"] == TxStatus.MANAGER_REVIEW
    assert (await db.execute(select(WithdrawalPayoutLeg))).scalars().all() == []


@pytest.mark.asyncio
async def test_the_deposit_flow_is_untouched_by_any_of_this(db):
    """Rule 34 — deposits use Highest Credit, withdrawals use Highest Debit, and the two never
    mix. A withdrawal consuming an account's debit capacity leaves its credit capacity alone."""
    from app.services import deposit_allocation as dalloc

    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=1000000.0, debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    credit_before = await dalloc.credit_used_today(db, ["ACC1"])

    await _create(db, merchant, 100000)                 # exhausts the DEBIT limit entirely

    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 100000.0
    # The withdrawal consumed the account's DEBIT capacity and left its CREDIT capacity exactly
    # where it was, so the deposit engine still places a deposit into the very same account.
    assert await dalloc.credit_used_today(db, ["ACC1"]) == credit_before
    assert (await dalloc.allocate_deposit_account(db, amount=400000)).allocated is True


@pytest.mark.asyncio
async def test_highest_debit_is_no_longer_raised_by_a_larger_payout(db):
    """The correction that makes the ceiling a ceiling. Completing a debit ABOVE the configured
    limit reports a breach; it must not quietly re-configure the account, which would have
    licensed every larger withdrawal that followed."""
    admin = await _admin(db)
    acc = await _account(db, "ACC1", debit=50000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    tx = await _withdrawal(db, "W1", 90000, status=TxStatus.COMPLETED)
    tx.payout_account_ref = "ACC1"
    tx.payout_payment_method = "BANK"
    await db.flush()

    await txr._track_account_debit(db, tx, admin, None)

    assert acc.highest_debit == 50000.0              # unchanged — a limit that moves is not a limit
    codes = [a.action_type for a in (await db.execute(select(AuditLog))).scalars().all()]
    assert "ACCOUNT_DEBIT_LIMIT_EXCEEDED" in codes
    assert "ACCOUNT_HIGHEST_DEBIT" not in codes


@pytest.mark.asyncio
async def test_the_admin_journal_endpoint_explains_the_decision(db):
    """Rule 32 at the route level — and the internal figures stay OFF the merchant payload."""
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _account(db, "ACC2", debit=1000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    await _fund(db, "D2", "ACC2", 500000.0)
    created = await _create(db, merchant, 45000)

    out = await txr.get_payout_allocation(created["id"], db, admin)

    assert out["decision"]["outcome"] == wa.OUTCOME_ALLOCATED
    assert out["decision"]["accountRef"] == "ACC1"
    assert out["decision"]["remainingCapacity"] == 100000.0
    rejected = {a["accountRef"]: a["rejectReason"] for a in out["decision"]["detail"]["accounts"]}
    assert rejected["ACC2"] == wa.REJECT_NO_CAPACITY
    # The merchant's own payload carries the account, never the capacity figures behind it.
    assert "remainingCapacity" not in str(created["payoutLegs"])
    assert "debitUsedToday" not in str(created["payoutLegs"])


@pytest.mark.asyncio
async def test_an_agent_finalised_withdrawal_still_posts_its_payout_debit(db):
    """The agent-assigned path completes a withdrawal at the Manager's approval, skipping the
    Admin's Pay & Complete step. That is the ONLY place its debit can be posted, so it must be —
    otherwise the money leaves while the legs sit allocated and the ledger records nothing."""
    from app.models.models import AgentMaster

    merchant = await _merchant(db)
    manager = await _manager(db)
    agent = AgentMaster(
        id=3, agent_id="AG3", full_name="Test Agent", country="India", state="MH",
        location="Mumbai", currency="INR", transaction_code="TST", category="Test",
    )
    db.add(agent)
    await db.flush()
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 45000)
    tx = (await db.execute(select(Transaction).where(Transaction.ref == created["ref"]))).scalar_one()
    tx.assigned_agent_id = agent.id
    await db.flush()

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.COMPLETED
    entries = (await db.execute(select(AccountLedgerEntry))).scalars().all()
    assert [(e.account_ref, e.amount) for e in entries] == [("ACC1", 45000.0)]
    assert await ledger.account_balance(db, "ACC1") == 455000.0
    legs = (await db.execute(select(WithdrawalPayoutLeg))).scalars().all()
    assert [l.status for l in legs] == [ledger.LEG_PAID]


@pytest.mark.asyncio
async def test_a_cancelled_withdrawal_hands_its_capacity_back(db):
    """A merchant cancelling their own request frees the accounts it was holding."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 100000)
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 100000.0

    await txr.cancel_transaction(created["id"], ReasonRequest(reason="changed my mind"), db, merchant)

    assert await wa.debit_used_today(db, ["ACC1"]) == {}


@pytest.mark.asyncio
async def test_a_withdrawal_returned_for_correction_frees_its_accounts(db):
    """The amount or the beneficiary may change on the way back, so the allocation is released and
    a fresh decision is made when the corrected request returns."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    created = await _create(db, merchant, 100000)

    await txr.manager_resubmit(created["id"], RemarkRequest(remark="fix the IFSC"), None, db, manager)

    assert await wa.debit_used_today(db, ["ACC1"]) == {}
    legs = (await db.execute(select(WithdrawalPayoutLeg))).scalars().all()
    assert [l.status for l in legs] == [ledger.LEG_RELEASED]


@pytest.mark.asyncio
async def test_a_withdrawal_with_incomplete_beneficiary_details_is_an_exception(db):
    """Negative scenario F at the route level — the request is the problem, not the accounts, and
    the reason says so rather than blaming capacity."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)

    created = await _create(db, merchant, 1000, accountNumber="", ifsc="", accountHolder="",
                            payoutDetails={})

    assert created["payoutLegs"] == []
    journal = (await db.execute(select(WithdrawalAllocation))).scalars().all()
    assert journal[-1].failure_code == wa.FAIL_BENEFICIARY


@pytest.mark.asyncio
async def test_the_engine_never_allocates_an_account_with_no_configured_debit_limit(db):
    """An unconfigured limit is not a licence. The engine refuses to CHOOSE such an account —
    while an Admin naming it explicitly at completion still can, because they are recording a
    payment rather than asking the engine how much is safe."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "NOLIMIT", debit=0.0)
    await _fund(db, "D1", "NOLIMIT", 1000000.0)
    created = await _create(db, merchant, 45000)
    assert created["payoutLegs"] == []              # never auto-selected

    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)
    out = await txr.mark_done(
        created["id"], None,
        CompleteRequest(paymentMethod="BANK", payoutAccountRef="NOLIMIT"), db, admin)

    assert out["status"] == TxStatus.COMPLETED       # but an Admin may still record it
    assert await ledger.account_balance(db, "NOLIMIT") == 955000.0


@pytest.mark.asyncio
async def test_the_withdrawal_history_carries_the_payout_allocation(db):
    """Rule 29 — the detail view shows the requested amount, the mode, the paying account(s) and
    the allocated amount, on a completed withdrawal as well as a pending one."""
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _fund(db, "DA", "A", 100000.0)
    await _fund(db, "DB", "B", 100000.0)
    created = await _create(db, merchant, 100000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)
    await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    detail = await txr.get_transaction_detail(created["id"], db, merchant)

    assert detail["amount"] == 100000.0
    assert detail["payoutTransactionMode"] == "IMPS"
    assert detail["payoutAllocatedTotal"] == 100000.0
    assert sorted((l["accountRef"], l["amount"], l["status"]) for l in detail["payoutLegs"]) == [
        ("A", 70000.0, "PAID"), ("B", 30000.0, "PAID")]
    # The merchant sees WHICH account paid them, never its full number.
    assert all(l["accountNumber"].startswith("••••") for l in detail["payoutLegs"])


@pytest.mark.asyncio
async def test_a_split_payout_reconciles_across_every_account_view(db):
    """Balances, statement and users must all charge each account ITS OWN share of a split.

    Without this they disagree three ways: the balance is right, but the statement shows all three
    accounts paying the full withdrawal, and the whole payment attributes to the member's deposit
    account instead of the accounts that made it.
    """
    from app.api.routes import accounts as acct_routes

    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "A", debit=70000.0)
    await _account(db, "B", debit=50000.0)
    await _fund(db, "DA", "A", 100000.0)
    await _fund(db, "DB", "B", 100000.0)
    created = await _create(db, merchant, 100000)
    await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)
    await txr.mark_done(created["id"], None, CompleteRequest(), db, admin)

    legs = await acct_routes._payout_leg_map(db)
    assert sorted(legs[created["ref"]]) == [("A", 70000.0), ("B", 30000.0)]

    # Each statement shows this account's own share, and names the full withdrawal alongside it.
    for ref, share in (("A", 70000.0), ("B", 30000.0)):
        statement = await acct_routes.account_statement(ref, db, admin)
        row = next(r for r in statement["transactions"] if r["ref"] == created["ref"])
        assert row["amount"] == share
        assert row["requestedAmount"] == 100000.0

    # And the shares add back up to the withdrawal — nothing lost, nothing double-counted.
    assert sum(a for _ref, a in legs[created["ref"]]) == 100000.0
    assert await ledger.account_balance(db, "A") == 30000.0
    assert await ledger.account_balance(db, "B") == 70000.0


@pytest.mark.asyncio
async def test_the_same_account_can_be_allocated_released_and_allocated_again(db):
    """A withdrawal legitimately cycles through one account more than once — allocated, returned
    for correction, re-allocated to the same account, returned again. Only the LIVE leg is unique
    per account; released legs are history and accumulate.
    """
    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 5000)

    for _ in range(3):
        await wa.write_legs(db, await _allocate(db, 5000), transaction=tx)
        assert [l.account_ref for l in await wa.live_legs(db, tx.ref)] == ["ACC1"]
        assert await wa.release_legs(db, tx.ref, reason=wa.RELEASE_REALLOCATED) == 1

    legs = (await db.execute(select(WithdrawalPayoutLeg))).scalars().all()
    assert len(legs) == 3
    assert {l.status for l in legs} == {ledger.LEG_RELEASED}
    assert await wa.debit_used_today(db, ["ACC1"]) == {}


@pytest.mark.asyncio
async def test_one_withdrawal_cannot_hold_two_live_legs_on_one_account(db):
    """The invariant the partial index enforces: a double-submit cannot double-book an account
    against the same withdrawal, whatever the calling code does."""
    from sqlalchemy.exc import IntegrityError

    await _account(db, "ACC1", debit=1000000.0)
    await _fund(db, "D1", "ACC1", 1000000.0)
    tx = await _withdrawal(db, "1", 5000)
    await wa.write_legs(db, await _allocate(db, 5000), transaction=tx)

    db.add(WithdrawalPayoutLeg(
        transaction_ref=tx.ref, leg_no=2, account_ref="ACC1", amount=5000.0,
        status=ledger.LEG_ALLOCATED, leg_date=wa.ist_today(), created_at=datetime.utcnow(),
    ))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
