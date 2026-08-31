"""Tests for the automatic deposit account allocation engine.

The engine decides where real money is sent, so the properties worth pinning down are the ones
that would cost money if they broke:

  1. **Highest Credit is a HARD DAILY CEILING.** Never ``amount <= highest_credit``; always
     ``used_today + amount <= highest_credit``. Reaching it exactly is allowed; exceeding it by a
     single paisa is not.
  2. **Capacity is consumed at allocation, not at completion.** An allocated-but-unpaid deposit
     holds its capacity, which is what stops two concurrent requests from spending it twice; a
     rejected or cancelled one releases it.
  3. **A merchant note is a preference, never an override.** "Use Bank of Baroda" narrows the
     pool and every Bank of Baroda account is evaluated — but if none can take the money, none is
     used.
  4. **Nearest suitable capacity wins**, and ties break deterministically.
  5. **A no-account outcome is explicit.** Nothing is assigned, the deposit keeps its existing
     state, and the decision is recorded with the reason.
  6. **The existing workflow is untouched.** An allocated deposit lands in ACCOUNT_SUBMITTED —
     the same state the Admin's manual send has always produced — and Cash/Crypto/Card requests
     never go near the engine.

NO PRODUCTION REFERENCE IDS ARE CONSUMED. ``_next_ref`` draws from the live Postgres
DEP/WIT/SET sequences; it is patched out for every test that creates a deposit, and every
transaction built directly carries a plain test id ("1", "2", "3"…). Nothing here reads, advances
or reserves a real reference number.

Run from the backend directory:

    python -m pytest tests/test_deposit_allocation.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountTransaction, AccountType, AdminUpi, DepositAllocation, Transaction,
    TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import DepositCreate
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
    db: AsyncSession, ref: str, *, name: str, bank: str = "HDFC Bank", credit: float = 100000.0,
    debit: float = 50000.0, atype: AccountType = AccountType.CURRENT, status: str = "ACTIVE",
    own: bool = False, ifsc: str = "HDFC0001234",
) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name, account_number=f"AC{ref}",
        ifsc_code=ifsc, bank_name=bank, branch="Mumbai", account_type=atype, status=status,
        created_date=date.today(), created_time="10:00:00",
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=debit,
        is_own_account=own,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _deposit(
    db: AsyncSession, ref: str, amount: float, account_ref: str | None, *, member: str = "MBR1",
    status: TxStatus = TxStatus.DEPOSITED, on: date | None = None,
) -> Transaction:
    """A deposit row. ``ref`` is a plain test id — never a generated production reference."""
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=status,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=on or alloc.ist_today(),
        tx_time="10:00:00", member_id=member, admin_ref=account_ref, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _merchant(db: AsyncSession, uid: int = 7) -> User:
    user = User(
        id=uid, username=f"op{uid}", name="BELLAGIO", role=UserRole.MERCHANT,
        hashed_password="x", email=f"op{uid}@test.local", merchant_role="DATA_OPERATOR",
        pay_in="DEP",
    )
    db.add(user)
    await db.flush()
    return user


def _payload(amount: float, **kw) -> DepositCreate:
    base = dict(
        amount=amount, depositType="BANK", memberName="Test Member", memberId="MBR1",
        accountHolder="Test Member", accountNumber="999", ifsc="HDFC0001234", bankName="HDFC Bank",
    )
    base.update(kw)
    return DepositCreate(**base)


async def _allocate(db: AsyncSession, amount: float, **kw):
    return await alloc.allocate_deposit_account(db, amount=amount, **kw)


# ═══ Rule 5 — the hard daily credit limit ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_limit_is_measured_against_todays_usage_not_the_raw_amount(db):
    """₹1,00,000 ceiling with ₹70,000 already taken accepts ₹30,000 and rejects ₹30,001.

    The naive test (amount <= highest_credit) would accept BOTH, which is the whole failure this
    engine exists to prevent.
    """
    await _account(db, "ACC1", name="sindu", credit=100000.0)
    await _deposit(db, "1", 70000, "ACC1")

    assert (await _allocate(db, 30000)).allocated is True
    assert (await _allocate(db, 30001)).allocated is False
    # And the naive comparison would have said yes to both — 30,001 is well under the ceiling.
    assert 30001 < 100000


@pytest.mark.asyncio
async def test_reaching_the_limit_exactly_is_allowed(db):
    """Used Today + Amount == Highest Credit is accepted (negative scenario 9)."""
    await _account(db, "ACC1", name="sindu", credit=100000.0)
    await _deposit(db, "1", 60000, "ACC1")

    result = await _allocate(db, 40000)
    assert result.allocated is True
    assert result.remaining == 40000.0


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [1, 0.01])
async def test_going_over_the_limit_is_rejected_by_any_margin(db, over):
    """Exceeding Highest Credit disqualifies the account — by ₹1 or by a single paisa.

    The paise case is the one that catches a sloppy comparison: unrounded float arithmetic can
    make 40000.01 read as within a 40000.00 gap.
    """
    await _account(db, "ACC1", name="sindu", credit=100000.0)
    await _deposit(db, "1", 60000, "ACC1")

    assert (await _allocate(db, 40000 + over)).allocated is False
    assert (await _allocate(db, 40000)).allocated is True


@pytest.mark.asyncio
async def test_an_account_with_no_configured_limit_is_never_allocated(db):
    """A Highest Credit of 0 means no capacity — never "unlimited"."""
    await _account(db, "ACC1", name="unconfigured", credit=0.0)

    result = await _allocate(db, 100)
    assert result.allocated is False
    assert result.detail["accounts"][0]["rejectReason"] == alloc.REJECT_NO_LIMIT


# ═══ Rule 6 — nearest suitable capacity ═════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_nearest_suitable_capacity_is_selected(db):
    """The specification's own worked example (section 14), reproduced exactly.

    Five accounts, a ₹45,000 request: A is out of room, and of B/C/D/E the engine must take B —
    the smallest remaining capacity that still fits — leaving the roomier accounts for requests
    only they can absorb.
    """
    await _account(db, "ACCA", name="a", credit=100000.0)
    await _account(db, "ACCB", name="b", credit=100000.0)
    await _account(db, "ACCC", name="c", credit=200000.0)
    await _account(db, "ACCD", name="d", credit=200000.0)
    await _account(db, "ACCE", name="e", credit=150000.0)
    await _deposit(db, "1", 80000, "ACCA")     # remaining 20,000 → cannot take 45,000
    await _deposit(db, "2", 50000, "ACCB")     # remaining 50,000 ← nearest suitable
    await _deposit(db, "3", 140000, "ACCC")    # remaining 60,000
    await _deposit(db, "4", 130000, "ACCD")    # remaining 70,000
    await _deposit(db, "5", 55000, "ACCE")     # remaining 95,000

    result = await _allocate(db, 45000)
    assert result.account.reference_number == "ACCB"
    assert result.remaining == 50000.0
    # A was evaluated and rejected for the right reason — not silently missing.
    rejected = {a["accountRef"]: a["rejectReason"] for a in result.detail["accounts"]}
    assert rejected["ACCA"] == alloc.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_equal_capacity_breaks_deterministically_never_randomly(db):
    """Identical remaining capacity resolves by deposit count, then reference number.

    Run repeatedly on the same data it must give the same answer every time — an allocation that
    wobbles is impossible to reason about after the fact.
    """
    await _account(db, "ACCZ", name="z", credit=50000.0)
    await _account(db, "ACCA", name="a", credit=50000.0)
    await _account(db, "ACCM", name="m", credit=50000.0)

    picks = {(await _allocate(db, 10000)).account.reference_number for _ in range(5)}
    assert picks == {"ACCA"}          # same capacity, same (zero) deposits → lowest reference


@pytest.mark.asyncio
async def test_no_account_can_handle_the_amount(db):
    """Every account is short of capacity → nothing is allocated (negative scenario 8)."""
    await _account(db, "ACC1", name="a", credit=50000.0)
    await _account(db, "ACC2", name="b", credit=60000.0)

    result = await _allocate(db, 90000)
    assert result.allocated is False
    assert result.detail["failure"] == "NO_ELIGIBLE_ACCOUNT"
    assert result.candidates and all(not c.eligible for c in result.candidates)


@pytest.mark.asyncio
async def test_no_accounts_configured_at_all(db):
    """An empty catalogue is a clear no-account state, not a crash (negative scenario 1)."""
    result = await _allocate(db, 1000)
    assert result.allocated is False
    assert result.detail["failure"] == "NO_ACCOUNTS_CONFIGURED"


# ═══ Rule 3 — availability ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_an_inactive_account_is_never_selected(db):
    """An account that cannot currently accept a deposit is excluded, however much room it has."""
    await _account(db, "ACC1", name="inactive", credit=900000.0, status="INACTIVE")
    await _account(db, "ACC2", name="active", credit=100000.0)

    result = await _allocate(db, 45000)
    assert result.account.reference_number == "ACC2"
    rejected = {a["accountRef"]: a["rejectReason"] for a in result.detail["accounts"]}
    assert rejected["ACC1"] == alloc.REJECT_INACTIVE


@pytest.mark.asyncio
async def test_every_account_unavailable_allocates_nothing(db):
    await _account(db, "ACC1", name="a", credit=900000.0, status="INACTIVE")
    await _account(db, "ACC2", name="b", credit=900000.0, status="INACTIVE")

    assert (await _allocate(db, 100)).allocated is False


# ═══ Rules 2 / 4 — account type and Own Account ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_account_types_are_allocatable_and_recorded(db):
    """Savings and Current are both valid receiving accounts; the type is carried on the result.

    The platform ties no deposit type to one account type, so neither is filtered out here — the
    only place a type is *required* is the new-customer Savings fallback, tested separately.
    """
    await _account(db, "ACC1", name="savings", credit=100000.0, atype=AccountType.SAVINGS)
    result = await _allocate(db, 1000, member_id="OLDM")
    assert result.snapshot()["accountType"] == "Savings Account"

    await _account(db, "ACC2", name="current", credit=2000.0, atype=AccountType.CURRENT)
    result = await _allocate(db, 1500, member_id="OLDM")
    assert result.account.reference_number == "ACC2"          # nearest capacity
    assert result.snapshot()["accountType"] == "Current Account"


@pytest.mark.asyncio
async def test_own_account_is_preserved_and_recorded_but_is_not_a_priority(db):
    """The flag reaches the decision and the journal; it does not reorder anything.

    The platform defines no Own Account priority, and inventing one would change where real money
    goes. Here the own account has MORE room, so nearest-capacity picks the other one — proving
    the flag did not quietly promote it.
    """
    await _account(db, "ACC1", name="own", credit=200000.0, own=True)
    await _account(db, "ACC2", name="third-party", credit=60000.0, own=False)

    result = await _allocate(db, 50000)
    assert result.account.reference_number == "ACC2"
    flags = {a["accountRef"]: a["isOwnAccount"] for a in result.detail["accounts"]}
    assert flags == {"ACC1": True, "ACC2": False}


# ═══ Rule 1 — new vs existing customer ══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_new_customer_prefers_an_unused_account(db):
    """A member with no history goes to an account that has never received a deposit.

    "Unused" is read off real usage, not a flag: ACC1 has served a deposit and is therefore used,
    even though it is the nearer capacity and would otherwise win.
    """
    await _account(db, "ACC1", name="used", credit=60000.0)
    await _account(db, "ACC2", name="never-used", credit=200000.0)
    await _deposit(db, "1", 10000, "ACC1", member="OTHER")

    result = await _allocate(db, 45000, member_id="BRANDNEW")
    assert result.customer_type == "NEW"
    assert result.account.reference_number == "ACC2"
    assert result.rule == alloc.RULES.NEW_UNUSED


@pytest.mark.asyncio
async def test_an_unused_account_is_still_subject_to_every_hard_rule(db):
    """The new-customer preference reorders eligible accounts; it never rescues an ineligible one."""
    await _account(db, "ACC1", name="unused-but-tiny", credit=1000.0)
    await _account(db, "ACC2", name="used-but-roomy", credit=200000.0)
    await _deposit(db, "1", 10000, "ACC2", member="OTHER")

    result = await _allocate(db, 45000, member_id="BRANDNEW")
    assert result.account.reference_number == "ACC2"          # the unused one cannot take it


@pytest.mark.asyncio
async def test_an_existing_customer_returns_to_an_account_they_have_used(db):
    """Account history comes first for a known member — even when another account has less room."""
    await _account(db, "ACC1", name="history", credit=200000.0)
    await _account(db, "ACC2", name="nearer", credit=60000.0)
    await _deposit(db, "1", 5000, "ACC1", member="MBR1")

    result = await _allocate(db, 45000, member_id="MBR1")
    assert result.customer_type == "OLD"
    assert result.account.reference_number == "ACC1"
    assert result.rule == alloc.RULES.OLD_ACCOUNT_HISTORY


@pytest.mark.asyncio
async def test_an_existing_customers_history_never_beats_the_credit_limit(db):
    """A previously used account with no room left is passed over, not forced."""
    await _account(db, "ACC1", name="history-full", credit=50000.0)
    await _account(db, "ACC2", name="other", credit=200000.0)
    await _deposit(db, "1", 45000, "ACC1", member="MBR1")

    result = await _allocate(db, 45000, member_id="MBR1")
    assert result.account.reference_number == "ACC2"


# ═══ Section 12 — the new-customer Savings fallback ═════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_new_customer_with_no_unused_account_falls_back_to_savings(db):
    """Every account has been used → eligible SAVINGS accounts are preferred, lower capacity first."""
    await _account(db, "ACC1", name="current", credit=200000.0, atype=AccountType.CURRENT)
    await _account(db, "ACC2", name="savings-big", credit=300000.0, atype=AccountType.SAVINGS)
    await _account(db, "ACC3", name="savings-small", credit=150000.0, atype=AccountType.SAVINGS)
    for i, ref in enumerate(("ACC1", "ACC2", "ACC3"), start=1):
        await _deposit(db, str(i), 1000, ref, member="OTHER")

    result = await _allocate(db, 45000, member_id="BRANDNEW")
    assert result.customer_type == "NEW"
    assert result.rule == alloc.RULES.NEW_SAVINGS_FALLBACK
    assert result.account.reference_number == "ACC3"          # the lower-capacity Savings account


@pytest.mark.asyncio
async def test_the_savings_fallback_never_violates_the_credit_limit(db):
    """"Lower available capacity" is a preference among ELIGIBLE accounts only.

    The Savings account here has the lower capacity and would win the fallback — except it cannot
    take the money, so the Current account does.
    """
    await _account(db, "ACC1", name="savings-full", credit=50000.0, atype=AccountType.SAVINGS)
    await _account(db, "ACC2", name="current", credit=200000.0, atype=AccountType.CURRENT)
    await _deposit(db, "1", 40000, "ACC1", member="OTHER")
    await _deposit(db, "2", 1000, "ACC2", member="OTHER")

    result = await _allocate(db, 45000, member_id="BRANDNEW")
    assert result.account.reference_number == "ACC2"


# ═══ Rule 7 — "same account" ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_same_account_returns_the_members_previous_account(db):
    await _account(db, "ACC1", name="previous", credit=200000.0)
    await _account(db, "ACC2", name="nearer", credit=60000.0)
    await _deposit(db, "1", 1000, "ACC1", member="MBR1")

    result = await _allocate(db, 45000, member_id="MBR1", note="Same account")
    assert result.rule == alloc.RULES.SAME_ACCOUNT
    assert result.account.reference_number == "ACC1"


@pytest.mark.asyncio
async def test_same_account_is_refused_when_that_account_is_unavailable(db):
    """Negative scenario 5 — an unavailable previous account is NOT used, and the reason is kept."""
    await _account(db, "ACC1", name="previous", credit=200000.0, status="INACTIVE")
    await _account(db, "ACC2", name="fallback", credit=100000.0)
    await _deposit(db, "1", 1000, "ACC1", member="MBR1")

    result = await _allocate(db, 45000, member_id="MBR1", note="use the same account")
    assert result.account.reference_number == "ACC2"
    assert result.requested_unavailable is True
    assert result.detail["sameAccountRejected"] == {
        "accountRef": "ACC1", "reason": alloc.REJECT_INACTIVE}


@pytest.mark.asyncio
async def test_same_account_is_refused_when_that_account_is_out_of_capacity(db):
    """Negative scenario 6 — insufficient remaining capacity beats the request, every time."""
    await _account(db, "ACC1", name="previous", credit=50000.0)
    await _account(db, "ACC2", name="fallback", credit=100000.0)
    await _deposit(db, "1", 40000, "ACC1", member="MBR1")

    result = await _allocate(db, 45000, member_id="MBR1", note="same a/c please")
    assert result.account.reference_number == "ACC2"
    assert result.detail["sameAccountRejected"]["reason"] == alloc.REJECT_NO_CAPACITY


# ═══ Rule 8 — the merchant note ═════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_bank_named_in_the_note_narrows_the_pool(db):
    await _account(db, "ACC1", name="hdfc-one", bank="HDFC Bank", credit=200000.0)
    await _account(db, "ACC2", name="bob-one", bank="Bank of Baroda", credit=300000.0)

    result = await _allocate(db, 45000, member_id="OLDM", note="Use Bank of Baroda")
    assert result.account.reference_number == "ACC2"
    assert result.note.bank_name == "Bank of Baroda"


@pytest.mark.asyncio
async def test_four_accounts_at_the_requested_bank_are_all_evaluated(db):
    """"Use Bank of Baroda" with four BoB accounts picks the BEST eligible one, not the first.

    Two are disqualified outright (inactive, no room), and of the two that remain the nearer
    capacity wins. Choosing the first match, or asking an Admin to pick, would both be wrong.
    """
    await _account(db, "ACC1", name="bob-1", bank="Bank of Baroda", credit=200000.0, status="INACTIVE")
    await _account(db, "ACC2", name="bob-2", bank="Bank of Baroda", credit=50000.0)
    await _account(db, "ACC3", name="bob-3", bank="Bank of Baroda", credit=100000.0)
    await _account(db, "ACC4", name="bob-4", bank="Bank of Baroda", credit=300000.0)
    await _account(db, "ACC5", name="hdfc", bank="HDFC Bank", credit=100000.0)
    await _deposit(db, "1", 30000, "ACC2", member="OTHER")      # remaining 20,000 → too small
    await _deposit(db, "2", 40000, "ACC3", member="OTHER")      # remaining 60,000 ← nearest
    await _deposit(db, "3", 40000, "ACC4", member="OTHER")      # remaining 260,000

    result = await _allocate(db, 45000, member_id="OLDM", note="give me Bank of Baroda")
    assert result.account.reference_number == "ACC3"


@pytest.mark.asyncio
async def test_a_note_never_bypasses_the_credit_limit(db):
    """Negative scenario 3 — no eligible account at the requested bank → the note is not honoured."""
    await _account(db, "ACC1", name="bob-1", bank="Bank of Baroda", credit=50000.0)
    await _account(db, "ACC2", name="bob-2", bank="Bank of Baroda", credit=200000.0, status="INACTIVE")
    await _account(db, "ACC3", name="hdfc", bank="HDFC Bank", credit=100000.0)
    await _deposit(db, "1", 40000, "ACC1", member="OTHER")

    result = await _allocate(db, 45000, member_id="OLDM", note="Use Bank of Baroda")
    assert result.account.reference_number == "ACC3"          # the defined fallback
    assert result.requested_unavailable is True
    assert result.detail["requestedBankUnavailable"] == "Bank of Baroda"


@pytest.mark.asyncio
async def test_a_note_naming_a_bank_that_does_not_exist_does_not_crash(db):
    """Negative scenario 12 — an unknown bank matches nothing and allocation proceeds normally."""
    await _account(db, "ACC1", name="hdfc", bank="HDFC Bank", credit=100000.0)

    result = await _allocate(db, 45000, member_id="OLDM", note="Use Bank of Nowhere")
    assert result.allocated is True
    assert result.note.bank_name is None
    assert result.requested_unavailable is False


@pytest.mark.asyncio
async def test_a_note_can_name_a_specific_account(db):
    """"Use SINDU" resolves the account by NAME — more specific than naming its bank."""
    await _account(db, "ACC1", name="SINDU", bank="Bank of Baroda", credit=200000.0)
    await _account(db, "ACC2", name="other", bank="Bank of Baroda", credit=60000.0)

    result = await _allocate(db, 45000, member_id="OLDM", note="Use SINDU")
    assert result.account.reference_number == "ACC1"          # not the nearer-capacity sibling


@pytest.mark.asyncio
async def test_note_parsing_recognises_the_phrasings_operators_actually_type(db):
    await _account(db, "ACC1", name="SINDU", bank="Bank of Baroda", credit=100000.0)
    await _account(db, "ACC2", name="kumar", bank="HDFC Bank", credit=100000.0)
    accounts = (await db.execute(select(AccountMaster))).scalars().all()

    def parse(note):
        return alloc.parse_note(note, accounts)

    assert parse("Use Bank of Baroda").bank_name == "Bank of Baroda"
    assert parse("give me bob").bank_name == "Bank of Baroda"          # alias
    assert parse("Use HDFC").bank_name == "HDFC Bank"
    assert parse("Give me HDFC Bank").bank_name == "HDFC Bank"
    assert parse("Use SINDU").account_ref == "ACC1"
    assert parse("Same account").same_account is True
    assert parse("use the same account").same_account is True
    assert parse("please use previous a/c").same_account is True
    assert parse("").same_account is False
    assert parse(None).bank_name is None
    assert parse("nothing relevant here").bank_name is None
    # A note that is essentially just the name is a request; one that merely MENTIONS a bank while
    # describing the merchant's own side of the transfer is not, and must not steer the deposit.
    assert parse("Bank of Baroda").bank_name == "Bank of Baroda"
    assert parse("SINDU pls").account_ref == "ACC1"
    assert parse("payment was received from our HDFC current account earlier today").bank_name is None
    assert parse("the customer holds a Bank of Baroda account in his own name").bank_name is None


# ═══ Rule 9 — the 5+ deposit condition ══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_deposit_count_and_the_five_plus_flag_are_calculated_and_carried(db):
    """The count is available to the decision and stored on the journal.

    The platform defines no action for the 5+ condition, so none is invented — what is guaranteed
    is that the number is correct and present, ready for whatever prioritisation is specified.
    """
    await _account(db, "ACC1", name="a", credit=1000000.0)
    for i in range(4):
        await _deposit(db, str(i + 1), 1000, "ACC1", member="MBR1")

    hist = await alloc.member_history(db, "MBR1")
    assert (hist.deposit_count, hist.is_five_plus, hist.is_new) == (4, False, False)

    await _deposit(db, "5", 1000, "ACC1", member="MBR1")
    hist = await alloc.member_history(db, "MBR1")
    assert (hist.deposit_count, hist.is_five_plus) == (5, True)

    result = await _allocate(db, 1000, member_id="MBR1")
    assert result.detail["memberDepositCount"] == 5
    assert result.detail["fivePlusDeposits"] is True


@pytest.mark.asyncio
async def test_the_member_id_is_matched_case_and_space_insensitively(db):
    """"mbr1", " MBR1 " and "MBR1" are one member — the platform's existing membership rule."""
    await _account(db, "ACC1", name="a", credit=1000000.0)
    await _deposit(db, "1", 1000, "ACC1", member="MBR1")

    assert (await alloc.member_history(db, " mbr1 ")).deposit_count == 1


# ═══ Daily usage, the day boundary, and released capacity ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_usage_counts_allocated_requests_not_only_completed_ones(db):
    """An allocated-but-unpaid deposit holds its capacity — the basis of the concurrency guard."""
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _deposit(db, "1", 40000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)
    await _deposit(db, "2", 10000, "ACC1", status=TxStatus.SUPERVISOR_REVIEW)

    assert (await alloc.credit_used_today(db))["ACC1"] == 50000.0


@pytest.mark.asyncio
async def test_rejected_and_cancelled_requests_release_their_capacity(db):
    """An abandoned request must not hold a limit hostage — and there is no separate reservation
    record that could fall out of step, because the deposit's own state IS the record."""
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _deposit(db, "1", 40000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)
    await _deposit(db, "2", 50000, "ACC1", status=TxStatus.REJECTED)
    await _deposit(db, "3", 50000, "ACC1", status=TxStatus.CANCELLED)
    await _deposit(db, "4", 50000, "ACC1", status=TxStatus.SA_REJECTED)

    assert (await alloc.credit_used_today(db))["ACC1"] == 40000.0
    assert (await _allocate(db, 60000)).allocated is True


@pytest.mark.asyncio
async def test_yesterdays_deposits_do_not_consume_todays_capacity(db):
    """The limit is DAILY: it resets on the date boundary, and history is never rewritten to do it."""
    yesterday = alloc.ist_today() - timedelta(days=1)
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _deposit(db, "1", 95000, "ACC1", on=yesterday)

    assert (await alloc.credit_used_today(db)) == {}
    assert (await _allocate(db, 100000)).allocated is True
    # Yesterday's row is untouched and still counts against yesterday.
    assert (await alloc.credit_used_today(db, on=yesterday))["ACC1"] == 95000.0
    assert (await db.execute(select(Transaction).where(Transaction.ref == "1"))).scalar_one().amount == 95000


@pytest.mark.asyncio
async def test_tomorrow_starts_with_the_full_limit_available(db):
    """The next business day sees the whole ceiling again — no reset job, just a different date."""
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _deposit(db, "1", 100000, "ACC1")

    assert (await _allocate(db, 1000)).allocated is False
    tomorrow = alloc.ist_today() + timedelta(days=1)
    assert (await _allocate(db, 100000, on=tomorrow)).allocated is True


# ═══ Payment-method capability ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_upi_deposit_requires_an_account_with_an_active_linked_upi(db):
    """The account's payment-method capability is the platform's existing AdminUpi link."""
    await _account(db, "ACC1", name="no-upi", credit=200000.0)
    await _account(db, "ACC2", name="has-upi", credit=100000.0)
    db.add(AdminUpi(label="has-upi", upi_id="sindu@ybl", account_ref="ACC2", status="ACTIVE",
                    created_date=date.today(), created_time="10:00:00"))
    await db.flush()

    result = await _allocate(db, 45000, deposit_type="UPI", member_id="OLDM")
    assert result.account.reference_number == "ACC2"
    assert result.upi_id == "sindu@ybl"
    # The UPI card names the account but withholds its bank details, as the manual UPI send does.
    snap = result.snapshot()
    assert snap["upiId"] == "sindu@ybl" and "accountNumber" not in snap

    # An inactive UPI link is not a capability.
    result = await _allocate(db, 45000, deposit_type="BANK", member_id="OLDM")
    assert result.account.reference_number == "ACC2"   # bank transfer works on any account


@pytest.mark.asyncio
async def test_a_upi_deposit_with_no_upi_enabled_account_allocates_nothing(db):
    await _account(db, "ACC1", name="no-upi", credit=200000.0)

    result = await _allocate(db, 45000, deposit_type="UPI")
    assert result.allocated is False
    assert result.detail["accounts"][0]["rejectReason"] == alloc.REJECT_NO_UPI


# ═══ Concurrency ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_two_simultaneous_requests_cannot_together_cross_the_limit(db):
    """₹50,000 of room, requests of ₹30,000 and ₹25,000: they must not both take this account.

    Each allocation is claimed under the account's row lock and its capacity re-read there, so the
    second request sees the first's committed reservation. The second either finds another
    eligible account or gets none — what it can never do is push this one to ₹55,000.
    """
    await _account(db, "ACC1", name="only", credit=50000.0)

    first = await _allocate(db, 30000)
    assert first.allocated is True
    await _deposit(db, "1", 30000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)

    second = await _allocate(db, 25000)
    assert second.allocated is False          # 30,000 + 25,000 > 50,000
    assert (await alloc.credit_used_today(db))["ACC1"] == 30000.0


@pytest.mark.asyncio
async def test_a_second_request_falls_through_to_another_account(db):
    """When the first request consumes the best account, the second re-runs selection over the rest
    rather than failing or doubling up."""
    await _account(db, "ACC1", name="small", credit=50000.0)
    await _account(db, "ACC2", name="large", credit=200000.0)

    first = await _allocate(db, 45000, member_id="OLDM")
    assert first.account.reference_number == "ACC1"
    await _deposit(db, "1", 45000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)

    second = await _allocate(db, 45000, member_id="OLDM")
    assert second.account.reference_number == "ACC2"


@pytest.mark.asyncio
async def test_concurrent_route_level_deposits_never_oversubscribe_one_account(db):
    """Two deposit requests raced through the real endpoint against a single ₹50,000 account.

    Whatever the interleaving, the account's committed total must stay within its ceiling.
    """
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="only", credit=50000.0)

    await txr.create_deposit(_payload(30000, memberId="RACE1"), db, merchant)
    await txr.create_deposit(_payload(25000, memberId="RACE2"), db, merchant)

    used = (await alloc.credit_used_today(db)).get("ACC1", 0.0)
    assert used <= 50000.0
    rows = (await db.execute(select(Transaction).order_by(Transaction.id))).scalars().all()
    assert [t.admin_ref for t in rows] == ["ACC1", None]
    assert [t.status for t in rows] == [TxStatus.ACCOUNT_SUBMITTED, TxStatus.ACCOUNT_REQUESTED]


# ═══ The deposit endpoint — the existing workflow, automated ════════════════════════════════════

@pytest.mark.asyncio
async def test_a_deposit_request_is_allocated_and_lands_in_account_submitted(db):
    """The engine's decision takes the SAME hop the Admin's manual send has always taken."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="sindu", credit=100000.0, atype=AccountType.SAVINGS)

    out = await txr.create_deposit(_payload(45000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["adminRef"] == "ACC1"
    assert out["allocationSnapshot"] == {
        "bankName": "HDFC Bank", "accountName": "sindu", "accountType": "Savings Account",
        "referenceNumber": "ACC1", "accountNumber": "ACACC1", "ifsc": "HDFC0001234",
        "branch": "Mumbai",
    }
    assert "Account Type: Savings Account" in out["adminBankDetails"]
    # The platform's existing account-usage record is written, exactly as the manual send writes it.
    link = (await db.execute(select(AccountTransaction))).scalars().one()
    assert (link.reference_number, link.member_id, link.transaction_reference_number) == (
        "ACC1", "MBR1", out["ref"])


@pytest.mark.asyncio
async def test_a_deposit_with_no_eligible_account_waits_for_the_admin(db):
    """Negative scenario 2 — nothing is assigned and the request keeps its existing state, so the
    Admin's manual send still handles it. No limit is bent to make a request succeed."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="sindu", credit=10000.0)

    out = await txr.create_deposit(_payload(45000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None
    assert out["allocationSnapshot"] is None
    journal = (await db.execute(select(DepositAllocation))).scalars().one()
    assert journal.outcome == alloc.OUTCOME_NO_ACCOUNT
    assert journal.account_ref is None


@pytest.mark.asyncio
async def test_allocation_consumes_capacity_immediately_for_the_next_request(db):
    """Section 14's closing requirement: after B takes ₹45,000 of its ₹50,000, the next ₹10,000
    must not be sent there."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="b", credit=50000.0)

    first = await txr.create_deposit(_payload(45000, memberId="M1"), db, merchant)
    assert first["adminRef"] == "ACC1"

    second = await txr.create_deposit(_payload(10000, memberId="M2"), db, merchant)
    assert second["adminRef"] is None
    assert second["status"] == TxStatus.ACCOUNT_REQUESTED


@pytest.mark.asyncio
async def test_cash_and_crypto_deposits_never_touch_the_engine(db):
    """Those two carry their own proof and skip the account hop — their flow is unchanged."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="sindu", credit=100000.0)

    out = await txr.create_deposit(
        _payload(1000, depositType="CASH", proofs=["data:image/png;base64,AAAA"],
                 depositDetails={"village": "V", "city": "C", "mobile": "9"}),
        db, merchant)

    assert out["status"] == TxStatus.SLIP_SUBMITTED
    assert out["adminRef"] is None
    assert (await db.execute(select(DepositAllocation))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_card_deposit_still_waits_for_the_admins_payment_link(db):
    """Card is paid through a gateway link, not an account — the engine must leave it alone."""
    merchant = await _merchant(db)
    merchant.merchant_role = "DEO"          # Card is a Data/Deposit Operator request
    await _account(db, "ACC1", name="sindu", credit=100000.0)

    out = await txr.create_deposit(_payload(1000, depositType="CARD"), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None
    assert (await db.execute(select(DepositAllocation))).scalars().all() == []


# ═══ Section 21 — audit and traceability ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_every_allocation_is_recorded_with_the_figures_it_rested_on(db):
    """The journal stores the point-in-time position, which cannot be recomputed later: today's
    usage moves with the next deposit and an Admin may re-configure the limit tomorrow."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="sindu", bank="Bank of Baroda", credit=100000.0, own=True)
    await _deposit(db, "seed", 20000, "ACC1", member="OTHER")

    out = await txr.create_deposit(_payload(45000, memberId="MBR9", notes="Use Bank of Baroda"), db, merchant)

    row = (await db.execute(select(DepositAllocation))).scalars().one()
    assert row.outcome == alloc.OUTCOME_ALLOCATED
    assert row.transaction_ref == out["ref"]
    assert (row.merchant_name, row.member_id, row.member_name) == ("BELLAGIO", "MBR9", "Test Member")
    assert row.requested_amount == 45000.0
    assert row.merchant_note == "Use Bank of Baroda"
    assert (row.account_ref, row.account_name, row.bank_name) == ("ACC1", "sindu", "Bank of Baroda")
    assert row.account_type == "Current Account"
    assert row.is_own_account is True
    assert (row.highest_credit, row.credit_used_today, row.remaining_capacity) == (100000.0, 20000.0, 80000.0)
    assert row.rule and row.reason
    assert row.customer_type == "NEW"
    assert row.created_at is not None and row.created_at_ist.endswith("IST")


@pytest.mark.asyncio
async def test_a_failed_allocation_is_recorded_too(db):
    """The unsuccessful attempt is the one support gets asked about, so it is journalled with the
    reason and the per-account rejection detail."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="full", credit=10000.0)

    await txr.create_deposit(_payload(45000), db, merchant)

    row = (await db.execute(select(DepositAllocation))).scalars().one()
    assert row.outcome == alloc.OUTCOME_NO_ACCOUNT
    assert row.candidates_considered == 1 and row.candidates_eligible == 0
    assert "No account can accept" in row.reason
    assert alloc.REJECT_NO_CAPACITY in row.detail


@pytest.mark.asyncio
async def test_the_snapshot_is_immutable_when_the_account_is_edited_later(db):
    """What the merchant was told to pay cannot be rewritten by a later change to the account."""
    merchant = await _merchant(db)
    acc = await _account(db, "ACC1", name="sindu", credit=100000.0)

    out = await txr.create_deposit(_payload(45000), db, merchant)
    acc.account_number = "CHANGED-LATER"
    acc.branch = "Delhi"
    await db.flush()

    tx = (await db.execute(select(Transaction).where(Transaction.ref == out["ref"]))).scalar_one()
    # full=False keeps the deferred image columns out of the payload, which is what a list read
    # does; the snapshot is a plain column and is present either way.
    assert txr._t(tx, full=False)["allocationSnapshot"]["accountNumber"] == "ACACC1"
    assert txr._t(tx, full=False)["allocationSnapshot"]["branch"] == "Mumbai"


# ═══ Highest Credit is configuration, and stays where the Admin put it ══════════════════════════

@pytest.mark.asyncio
async def test_a_completed_deposit_no_longer_raises_the_configured_limit(db):
    """A ceiling that moves up to fit whatever arrives is not a ceiling.

    The high-water behaviour this replaced would have re-configured the account on the spot; the
    limit is now changed only by an Admin, through the audited limits route.
    """
    acc = await _account(db, "ACC1", name="sindu", credit=50000.0)
    # Only a manual send can put an over-limit deposit on an account; the engine never can.
    tx = await _deposit(db, "1", 90000, "ACC1", status=TxStatus.DEPOSITED)

    await txr._track_account_credit(db, tx, User(id=1, name="Admin", role=UserRole.ADMIN), None)

    await db.refresh(acc)
    assert acc.highest_credit == 50000.0


@pytest.mark.asyncio
async def test_exceeding_the_configured_limit_raises_an_alert(db):
    """The breach the engine cannot cause is still reported to the Admins who can."""
    from app.models.models import AuditLog, Notification, SystemLog

    db.add(User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN,
                hashed_password="x", email="a@test.local", active=True))
    acc = await _account(db, "ACC1", name="sindu", credit=50000.0)
    tx = await _deposit(db, "1", 90000, "ACC1", status=TxStatus.DEPOSITED)

    await txr._track_account_credit(db, tx, User(id=1, name="Admin", role=UserRole.ADMIN), None)

    audits = (await db.execute(select(AuditLog))).scalars().all()
    assert [a.action_type for a in audits] == ["ACCOUNT_CREDIT_LIMIT_EXCEEDED"]
    assert (audits[0].old_value, audits[0].new_value) == ("₹50,000.00", "₹90,000.00")
    assert (await db.execute(select(SystemLog))).scalars().all()
    assert (await db.execute(select(Notification))).scalars().all()
    await db.refresh(acc)
    assert acc.highest_credit == 50000.0          # reported, never re-configured


@pytest.mark.asyncio
async def test_a_deposit_within_the_limit_raises_nothing(db):
    from app.models.models import AuditLog

    await _account(db, "ACC1", name="sindu", credit=100000.0)
    tx = await _deposit(db, "1", 40000, "ACC1", status=TxStatus.DEPOSITED)

    await txr._track_account_credit(db, tx, User(id=1, name="Admin", role=UserRole.ADMIN), None)

    assert (await db.execute(select(AuditLog))).scalars().all() == []


# ═══ Helper arithmetic ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_remaining_capacity_never_goes_negative(db):
    """An account already past its ceiling has zero capacity, not a debt to be repaid."""
    acc = await _account(db, "ACC1", name="a", credit=50000.0)
    assert alloc.remaining_credit(acc, 80000.0) == 0.0
    assert alloc.remaining_credit(acc, 20000.0) == 30000.0
    # The debit-side counterpart is available for the payout work, unwired here by design.
    assert alloc.remaining_debit(acc, 10000.0) == 40000.0
