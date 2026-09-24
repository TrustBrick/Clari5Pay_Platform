"""The Own Account preference — an ACTIVE account-selection rule for deposits.

  • The MEMBER's SUCCESSFUL deposits are counted (COMPLETED / DEPOSITED — the platform's one
    definition of "received") across EVERY sending account they have used: 3 from account A +
    3 from account B = 6. A new sending account never resets the count. (NEW/OLD is a separate
    rule and is still decided per sending account.)
  • 1-5 → the normal rules. MORE than 5 (the 6th onward) → an eligible Own Account = Yes is
    preferred, ranked among the Own Accounts by the normal rules.
  • A PREFERENCE, never an override: an Own Account must pass every hard limit, and when none is
    eligible any other eligible account is used. Only when NOTHING is eligible → ACCOUNT_REQUESTED.
  • Explicit requests keep their higher priority: exact account → same / last account → bank
    (and within a requested bank, Own Accounts are preferred). Withdrawals are unaffected.

Letters A-J are the brief's worked examples; numbers are its test list.

    python -m pytest tests/test_own_account_preference.py -v
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import transactions as txr
from app.models.models import AccountMaster, AccountType, Transaction, TxStatus, TxType, User, UserRole
from app.schemas.schemas import DepositCreate
from app.services import deposit_allocation as alloc
from app.services import tg_notify as tgn
from app.services import withdrawal_allocation as walloc

from tests.test_deposit_allocation import db, safe_refs_and_no_cache  # noqa: F401  (fixtures)

TODAY = alloc.ist_today()
YESTERDAY = TODAY - timedelta(days=1)
UPI = "member.one@ybl"
UPI_B = "member.one.second@okaxis"
UPI_NEW = "member.one.brand.new@ibl"


# ── Fixtures / builders ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def telegram(monkeypatch):
    sent = {"alerts": [], "notify": []}

    async def _alert(db, tx, *, requested, reason):
        sent["alerts"].append({"ref": tx.ref, "requested": requested, "reason": reason})

    async def _notify(db, tx, target, event, actor=None, reason=None):
        sent["notify"].append((target, event))

    monkeypatch.setattr(tgn, "notify_account_request", _alert)
    monkeypatch.setattr(tgn, "notify", _notify)
    return sent


async def _acc(db, ref: str, *, own: bool = False, bank: str = "HDFC Bank",
               number: str | None = None, credit: float = 100000.0,
               daily_credit: float | None = None, status: str = "ACTIVE",
               debit: float = 100000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=f"acct-{ref.lower()}",
        account_number=number or f"77{ref[-6:]}", ifsc_code="HDFC0001234", bank_name=bank,
        branch="Mumbai", account_type=AccountType.CURRENT, status=status,
        created_date=date.today(), created_time="10:00:00", is_own_account=own,
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=0.0,
        bank_daily_credit_limit=credit if daily_credit is None else daily_credit,
        bank_daily_debit_limit=debit,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _row(db, ref: str, amount: float, account: str | None, *, member: str = "OTHER",
               status: TxStatus = TxStatus.DEPOSITED, on: date = YESTERDAY,
               assigned_on: date | None = None, **kw) -> Transaction:
    tx = Transaction(ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=status,
                     merchant_id=7, merchant_name="BELLAGIO", tx_date=on, tx_time="10:00:00",
                     member_id=member, admin_ref=account,
                     account_assigned_on=assigned_on if assigned_on else on,
                     created_at=datetime.utcnow(), **kw)
    db.add(tx)
    await db.flush()
    return tx


async def _use_today(db, account: str, amount: float) -> None:
    """Consume ``amount`` of ``account``'s daily credit today (another customer's deposit)."""
    await _row(db, f"use-{account}-{amount}", amount, account, member="SOMEONE", on=TODAY)


async def _allocate(db, amount: float, *, successful: int | None = 6, **kw):
    """The engine, for a member with ``successful`` received deposits in total.

    The deposits are really written — split across TWO sending accounts, because the Own Account
    count is the member's total, not one account's — and topped up rather than duplicated if a
    test calls this twice. ``successful=None`` writes nothing: the test seeded the rows itself.
    The sending account's own figure (what NEW/OLD reads) is passed as the same number, so the
    NEW/OLD classification in these tests is exactly what it was.
    """
    member = kw.setdefault("member_id", "MBR1")
    if successful:
        have = (await alloc.member_history(db, member)).successful_count
        for i in range(have, successful):
            await _row(db, f"seed-{member}-{i}", 1000.0, None, member=member,
                       sender_upi_id=UPI if i % 2 == 0 else UPI_B)
    return await alloc.allocate_deposit_account(
        db, amount=amount, sender_account_deposits=successful, **kw)


async def _merchant(db) -> User:
    u = User(id=7, username="op7", name="BELLAGIO", role=UserRole.MERCHANT, hashed_password="x",
             email="op7@test.local", merchant_role="DATA_OPERATOR", pay_in="DEP")
    db.add(u)
    await db.flush()
    return u


async def _admin(db) -> User:
    u = User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN, hashed_password="x",
             email="admin1@test.local")
    db.add(u)
    await db.flush()
    return u


async def _received(db, n: int, *, status: TxStatus = TxStatus.DEPOSITED, start: int = 0,
                    upi: str = UPI) -> None:
    """``n`` deposits of MBR1 in ``status``, sent from the sending account ``upi``."""
    for i in range(start, start + n):
        await _row(db, f"rcv{upi}{status.value}{i}", 1000.0, None, member="MBR1", status=status,
                   sender_upi_id=upi)


def _dep(amount: float, note: str | None = None, upi: str = UPI) -> DepositCreate:
    return DepositCreate(amount=amount, depositType="UPI", memberName="Member One", memberId="MBR1",
                         senderUpiId=upi, accountType="CURRENT", notes=note)


# ═══ The threshold (1, 2, 16, 18 / A, B) ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_A_1_exactly_5_successful_is_still_the_normal_rules(db):
    """5 is NOT more than 5. Own A (more room) and non-own B (nearer capacity) are both eligible;
    the normal existing-customer rule — nearest capacity — picks B."""
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)

    r = await _allocate(db, 5000, successful=5)
    assert r.detail["ownAccountPreference"] is False
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.NEAREST_CAPACITY


@pytest.mark.asyncio
async def test_B_2_9_the_6th_successful_deposit_prefers_the_own_account(db):
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)

    r = await _allocate(db, 5000, successful=6)
    assert r.detail["ownAccountPreference"] is True
    assert r.account.reference_number == "ACC0000001"
    assert r.rule == alloc.RULES.OWN_ACCOUNT
    assert r.detail["ownAccount"]["selectedIsOwn"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
async def test_18_one_to_five_successful_never_prefers_the_own_account(db, n):
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)
    r = await _allocate(db, 5000, successful=n)
    assert r.account.reference_number == "ACC0000002"
    assert r.rule != alloc.RULES.OWN_ACCOUNT


@pytest.mark.asyncio
async def test_17_a_new_customer_is_spread_not_sent_to_the_own_account(db):
    """0 successful deposits → NEW. Own A has served 10 customers, non-own B has served 2 →
    B, by the NEW-customer spread. The Own Account rule is not active yet."""
    await _acc(db, "ACC0000001", own=True)
    await _acc(db, "ACC0000002", own=False)
    for i in range(10):
        await _row(db, f"a{i}", 100.0, "ACC0000001", member=f"A{i}")
    for i in range(2):
        await _row(db, f"b{i}", 100.0, "ACC0000002", member=f"B{i}")

    r = await _allocate(db, 5000, successful=0, member_id="BRANDNEW")
    assert r.customer_type == "NEW"
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.NEW_FEWEST_SERVED


# ═══ What counts (3-8) ══════════════════════════════════════════════════════════════════════════

NOT_SUCCESSFUL = [TxStatus.PENDING, TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED,
                  TxStatus.SLIP_SUBMITTED, TxStatus.PENDING_APPROVAL, TxStatus.SUPERVISOR_REVIEW,
                  TxStatus.RESUBMITTED, TxStatus.ADMIN_APPROVED, TxStatus.NO_ELIGIBLE_ACCOUNT,
                  TxStatus.REJECTED, TxStatus.SA_REJECTED, TxStatus.CANCELLED]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NOT_SUCCESSFUL)
async def test_3_4_5_6_pending_rejected_cancelled_and_failed_do_not_count(db, status):
    """5 successful + a 6th that is pending / account-requested / rejected / cancelled / failed
    (SA_REJECTED, the platform's failed-at-final-review state) → still 5 → normal rules."""
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)
    for i in range(5):
        await _row(db, f"ok{i}", 1000.0, None, member="MBR1")
    await _row(db, "notyet", 1000.0, None, member="MBR1", status=status)

    hist = await alloc.member_history(db, "MBR1")
    assert hist.successful_count == 5 and hist.prefers_own_account is False
    r = await _allocate(db, 5000, successful=None)          # the member-wide count
    assert r.account.reference_number == "ACC0000002"


@pytest.mark.asyncio
@pytest.mark.parametrize("mix", [(6, 0), (0, 6), (3, 3)])
async def test_7_8_completed_and_deposited_both_count(db, mix):
    completed, deposited = mix
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)
    for i in range(completed):
        await _row(db, f"c{i}", 1000.0, None, member="MBR1", status=TxStatus.COMPLETED)
    for i in range(deposited):
        await _row(db, f"d{i}", 1000.0, None, member="MBR1", status=TxStatus.DEPOSITED)

    assert (await alloc.member_history(db, "MBR1")).successful_count == 6
    r = await _allocate(db, 5000, successful=None)
    assert r.account.reference_number == "ACC0000001"


@pytest.mark.asyncio
async def test_the_route_counts_the_members_received_deposits(db, telegram):
    """End to end through the route: 5 received + 3 pending → normal; the 6th received → Own
    Account. (All from one sending account here; the multi-account cases are below.)"""
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)
    await _received(db, 5)
    await _received(db, 3, status=TxStatus.SLIP_SUBMITTED)

    five = await txr.create_deposit(_dep(5000), db, merchant)
    assert five["adminRef"] == "ACC0000002", "5 successful — normal rules"

    await _received(db, 1, start=100)
    six = await txr.create_deposit(_dep(5000), db, merchant)
    assert six["adminRef"] == "ACC0000001", "6 successful — Own Account"


# ═══ The MEMBER's total across every sending account ═════════════════════════════════════════════
# Every case below goes through the real deposit route, so the sending-account figure is the one
# services/member_account derives — and the Own Account rule must NOT be reading it.

async def _two_accounts(db):
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, daily_credit=60000.0)      # nearer capacity


@pytest.mark.asyncio
async def test_member_5_total_across_two_sending_accounts_is_off(db, telegram):
    """3 from account A + 2 from account B = 5 → NOT more than 5 → the normal rules."""
    merchant = await _merchant(db)
    await _two_accounts(db)
    await _received(db, 3, upi=UPI)
    await _received(db, 2, upi=UPI_B)

    out = await txr.create_deposit(_dep(5000, upi=UPI), db, merchant)
    assert out["adminRef"] == "ACC0000002"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["ownAccount"]["successfulDeposits"] == 5
    assert decision["ownAccount"]["triggered"] is False


@pytest.mark.asyncio
async def test_member_6_total_across_two_sending_accounts_is_on(db, telegram):
    """3 from account A + 3 from account B = 6 → Own Account, although the account this deposit
    is sent from has only 3 of its own."""
    merchant = await _merchant(db)
    await _two_accounts(db)
    await _received(db, 3, upi=UPI)
    await _received(db, 3, upi=UPI_B)

    out = await txr.create_deposit(_dep(5000, upi=UPI), db, merchant)
    assert out["adminRef"] == "ACC0000001"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["ownAccount"]["successfulDeposits"] == 6
    assert decision["ownAccount"]["triggered"] is True
    assert decision["rule"] == alloc.RULES.OWN_ACCOUNT


@pytest.mark.asyncio
async def test_member_6_from_one_sending_account_is_on(db, telegram):
    merchant = await _merchant(db)
    await _two_accounts(db)
    await _received(db, 6, upi=UPI)

    out = await txr.create_deposit(_dep(5000, upi=UPI), db, merchant)
    assert out["adminRef"] == "ACC0000001"


@pytest.mark.asyncio
async def test_a_new_sending_account_does_not_reset_the_member_count(db, telegram):
    """6 received from two old accounts; this deposit comes from an account the platform has
    NEVER received from. NEW/OLD (a per-sending-account rule) says NEW — and the Own Account rule,
    which sits ABOVE it in the priority, still applies, because the member has 6."""
    merchant = await _merchant(db)
    await _two_accounts(db)
    await _received(db, 4, upi=UPI)
    await _received(db, 2, upi=UPI_B)

    out = await txr.create_deposit(_dep(5000, upi=UPI_NEW), db, merchant)
    assert out["adminRef"] == "ACC0000001"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["customerType"] == "NEW", "the sending account is new…"
    assert decision["ownAccount"]["successfulDeposits"] == 6, "…the member is not"
    assert decision["rule"] == alloc.RULES.OWN_ACCOUNT


@pytest.mark.asyncio
async def test_only_completed_and_deposited_count_across_sending_accounts(db, telegram):
    """3 DEPOSITED (A) + 2 COMPLETED (B) = 5 successful; everything else — on either account —
    counts for nothing, however many there are. → normal rules. One more COMPLETED → 6 → ON."""
    merchant = await _merchant(db)
    await _two_accounts(db)
    await _received(db, 3, upi=UPI, status=TxStatus.DEPOSITED)
    await _received(db, 2, upi=UPI_B, status=TxStatus.COMPLETED)
    for i, status in enumerate([TxStatus.PENDING, TxStatus.ACCOUNT_REQUESTED, TxStatus.REJECTED,
                                TxStatus.CANCELLED, TxStatus.SA_REJECTED, TxStatus.SLIP_SUBMITTED,
                                TxStatus.ACCOUNT_SUBMITTED, TxStatus.SUCCESSFUL]):
        await _received(db, 1, upi=UPI if i % 2 else UPI_B, status=status, start=50 + i)

    assert (await alloc.member_history(db, "MBR1")).successful_count == 5
    five = await txr.create_deposit(_dep(5000, upi=UPI), db, merchant)
    assert five["adminRef"] == "ACC0000002"

    await _received(db, 1, upi=UPI_B, status=TxStatus.COMPLETED, start=99)
    six = await txr.create_deposit(_dep(5000, upi=UPI), db, merchant)
    assert six["adminRef"] == "ACC0000001"


@pytest.mark.asyncio
@pytest.mark.parametrize("note,expected,rule", [
    ("Use account 1234567890", "ACC0000003", alloc.RULES.EXACT_ACCOUNT),   # exact — non-own
    ("same account", "ACC0000002", alloc.RULES.SAME_ACCOUNT),              # last — non-own
    ("Use SBI", "ACC0000004", alloc.RULES.NOTE_BANK),                      # bank — non-own, only SBI
])
async def test_explicit_requests_still_outrank_own_account_for_a_multi_account_member(
        db, telegram, note, expected, rule):
    """A member with 6 received deposits over two sending accounts: exact account, same / last
    account and a bank request all still come before the Own Account preference."""
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", own=True, daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False)
    await _acc(db, "ACC0000003", own=False, number="1234567890")
    await _acc(db, "ACC0000004", own=False, bank="State Bank of India")
    await _received(db, 3, upi=UPI)
    await _received(db, 2, upi=UPI_B)
    await _row(db, "last-ok", 1000.0, "ACC0000002", member="MBR1", sender_upi_id=UPI_B)  # 6th

    out = await txr.create_deposit(_dep(5000, note, upi=UPI), db, merchant)
    assert out["adminRef"] == expected
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["ownAccount"]["triggered"] is True
    assert decision["rule"] == rule


# ═══ A preference, never an override (10, 11 / C, D, E, §6) ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_C_10_an_exhausted_own_account_does_not_block_a_normal_one(db):
    await _acc(db, "ACC0000001", own=True, daily_credit=100000.0)
    await _acc(db, "ACC0000002", own=False)
    await _use_today(db, "ACC0000001", 100000.0)

    r = await _allocate(db, 5000, successful=6)
    assert r.account.reference_number == "ACC0000002"
    assert r.detail["ownAccount"]["eligible"] == 0
    assert r.detail["ownAccount"]["selectedIsOwn"] is False


@pytest.mark.asyncio
async def test_the_briefs_own_account_limit_example_is_refused_and_a_normal_account_used(db):
    """Own: Highest Credit ₹75,000, daily ₹2,00,000, ₹1,80,000 used. ₹30,000 passes per
    transaction but ₹2,10,000 > ₹2,00,000 — the Own Account is NOT assigned."""
    await _acc(db, "ACC0000001", own=True, credit=75000.0, daily_credit=200000.0)
    await _acc(db, "ACC0000002", own=False)
    await _use_today(db, "ACC0000001", 180000.0)

    r = await _allocate(db, 30000, successful=8)
    assert r.account.reference_number == "ACC0000002"
    own = r.detail["ownAccount"]["accounts"][0]
    assert own["rejectReason"] == alloc.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_an_own_account_never_bypasses_highest_credit(db):
    await _acc(db, "ACC0000001", own=True, credit=10000.0)
    await _acc(db, "ACC0000002", own=False)
    r = await _allocate(db, 20000, successful=9)
    assert r.account.reference_number == "ACC0000002"


@pytest.mark.asyncio
async def test_D_11_of_several_own_accounts_the_eligible_one_is_used(db):
    """Own A has ₹10,000 left, Own B ₹50,000; ₹20,000 → A is not eligible, B is. A normal account
    is also eligible and must NOT be reached while an eligible Own Account exists."""
    await _acc(db, "ACC0000001", own=True, daily_credit=100000.0)
    await _acc(db, "ACC0000002", own=True, daily_credit=100000.0)
    await _acc(db, "ACC0000003", own=False, daily_credit=25000.0)       # nearest — but not own
    await _use_today(db, "ACC0000001", 90000.0)
    await _use_today(db, "ACC0000002", 50000.0)

    r = await _allocate(db, 20000, successful=8)
    assert r.account.reference_number == "ACC0000002"


@pytest.mark.asyncio
async def test_11_several_eligible_own_accounts_are_ranked_by_the_existing_rules(db):
    """Not the first row: between two eligible Own Accounts the existing-customer ranking applies
    — here nearest remaining capacity, so the SECOND one (₹30,000 left) beats the first (₹90,000)."""
    await _acc(db, "ACC0000001", own=True, daily_credit=90000.0)
    await _acc(db, "ACC0000002", own=True, daily_credit=30000.0)
    await _acc(db, "ACC0000003", own=False, daily_credit=21000.0)
    r = await _allocate(db, 20000, successful=6)
    assert r.account.reference_number == "ACC0000002"


@pytest.mark.asyncio
async def test_11_the_customers_own_account_history_is_kept_within_the_own_accounts(db):
    """The existing "account already given to this member" rule applies INSIDE the Own Accounts:
    the member's previous Own Account wins over a nearer-capacity Own Account."""
    await _acc(db, "ACC0000001", own=True, daily_credit=30000.0)          # nearer
    await _acc(db, "ACC0000002", own=True, daily_credit=90000.0)          # theirs
    await _row(db, "theirs", 1000.0, "ACC0000002", member="MBR1")
    r = await _allocate(db, 20000, successful=7)
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.OWN_ACCOUNT


@pytest.mark.asyncio
async def test_E_every_own_account_unavailable_uses_the_normal_account(db):
    await _acc(db, "ACC0000001", own=True, daily_credit=100000.0)
    await _acc(db, "ACC0000002", own=True, status="INACTIVE")
    await _acc(db, "ACC0000003", own=False)
    await _use_today(db, "ACC0000001", 100000.0)

    r = await _allocate(db, 5000, successful=8)
    assert r.account.reference_number == "ACC0000003"
    info = r.detail["ownAccount"]
    assert (info["triggered"], info["candidates"], info["eligible"], info["fallbackEligible"]) \
        == (True, 2, 0, 1)


# ═══ Nothing eligible (19-22 / F) ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_F_19_20_21_nothing_eligible_is_account_requested_with_own_detail_for_the_admin(
        db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", own=True, daily_credit=100000.0)
    await _acc(db, "ACC0000002", own=True, status="INACTIVE")
    await _acc(db, "ACC0000003", own=False, credit=1000.0)
    await _use_today(db, "ACC0000001", 100000.0)
    await _received(db, 6)

    out = await txr.create_deposit(_dep(5000), db, merchant)

    # 19 — ACCOUNT_REQUESTED, nothing forced.
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED and out["adminRef"] is None
    # 20 — the merchant payload carries no Own Account or limit reason.
    blob = json.dumps(out, default=str)
    for leak in ("Own Account", "own account", "Daily Credit", "Highest Credit", "limit"):
        assert leak not in blob, f"merchant payload leaks {leak!r}"
    # 21 — the Admin sees the Own Account decision.
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    own = decision["ownAccount"]
    assert own["triggered"] is True and own["successfulDeposits"] == 6
    assert (own["candidates"], own["eligible"], own["fallbackEligible"]) == (2, 0, 0)
    assert {a["accountRef"]: a["rejectReason"] for a in own["accounts"]} == {
        "ACC0000001": alloc.REJECT_NO_CAPACITY, "ACC0000002": alloc.REJECT_INACTIVE}
    assert all(a["why"] for a in own["accounts"])
    assert "Own Account preference applied (6 successful deposits)" in decision["reason"]
    # The existing ACCOUNT_REQUESTED Telegram alert, once.
    assert len(telegram["alerts"]) == 1


@pytest.mark.asyncio
async def test_22_retries_still_send_one_alert(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", own=True, credit=1000.0)
    await _received(db, 6)
    out = await txr.create_deposit(_dep(5000), db, merchant)
    admin = await _admin(db)
    for _ in range(3):
        with pytest.raises(HTTPException):
            await txr.retry_allocation(out["id"], db, admin)
    assert len(telegram["alerts"]) == 1


# ═══ Explicit requests keep their priority (12-16 / G, H, I, J) ═════════════════════════════════

@pytest.mark.asyncio
async def test_G_12_an_exact_account_request_beats_the_own_account(db):
    await _acc(db, "ACC0000001", own=True)
    await _acc(db, "ACC0000002", own=False, number="1234567890")
    r = await _allocate(db, 5000, successful=8, note="Use account 1234567890")
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.EXACT_ACCOUNT


@pytest.mark.asyncio
async def test_12_an_ineligible_exact_account_is_not_replaced_by_an_own_account(db):
    await _acc(db, "ACC0000001", own=True)
    await _acc(db, "ACC0000002", own=False, number="1234567890", credit=1000.0)
    r = await _allocate(db, 5000, successful=8, note="Use account 1234567890")
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_REQUESTED_ACCOUNT_NOT_ELIGIBLE


@pytest.mark.asyncio
async def test_H_13_a_same_account_request_beats_the_own_account(db):
    await _acc(db, "ACC0000001", own=True)
    await _acc(db, "ACC0000002", own=False)
    await _row(db, "last", 1000.0, "ACC0000002", member="MBR1")
    r = await _allocate(db, 5000, successful=8, note="same account")
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.SAME_ACCOUNT


@pytest.mark.asyncio
async def test_13_an_ineligible_same_account_is_not_replaced_by_an_own_account(db):
    await _acc(db, "ACC0000001", own=True)
    await _acc(db, "ACC0000002", own=False, daily_credit=1000.0)
    await _row(db, "last", 1000.0, "ACC0000002", member="MBR1")
    r = await _allocate(db, 5000, successful=8, note="same account")
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_SAME_ACCOUNT_NOT_ELIGIBLE


@pytest.mark.asyncio
async def test_I_14_15_within_a_requested_bank_the_own_account_is_preferred(db):
    await _acc(db, "ACC0000001", own=True, bank="HDFC Bank", daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, bank="HDFC Bank", daily_credit=60000.0)   # nearer
    await _acc(db, "ACC0000003", own=True, bank="State Bank of India")
    r = await _allocate(db, 5000, successful=8, note="Use HDFC")
    assert r.account.reference_number == "ACC0000001"
    assert r.rule == alloc.RULES.NOTE_BANK
    assert "Own Account" in r.reason


@pytest.mark.asyncio
async def test_J_16_own_hdfc_unavailable_uses_normal_hdfc_never_sbi(db):
    await _acc(db, "ACC0000001", own=True, bank="HDFC Bank", credit=1000.0)
    await _acc(db, "ACC0000002", own=False, bank="HDFC Bank")
    await _acc(db, "ACC0000003", own=True, bank="State Bank of India")                # own, other bank
    r = await _allocate(db, 5000, successful=8, note="Use HDFC")
    assert r.account.reference_number == "ACC0000002"


@pytest.mark.asyncio
async def test_16_no_eligible_account_at_the_requested_bank_never_switches_to_an_own_account(db):
    await _acc(db, "ACC0000001", own=False, bank="HDFC Bank", credit=1000.0)
    await _acc(db, "ACC0000002", own=True, bank="State Bank of India")
    r = await _allocate(db, 5000, successful=8, note="Use HDFC")
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_REQUESTED_BANK_NOT_ELIGIBLE


@pytest.mark.asyncio
async def test_5_or_fewer_within_a_requested_bank_uses_the_normal_ranking(db):
    await _acc(db, "ACC0000001", own=True, bank="HDFC Bank", daily_credit=500000.0)
    await _acc(db, "ACC0000002", own=False, bank="HDFC Bank", daily_credit=60000.0)
    r = await _allocate(db, 5000, successful=5, note="Use HDFC")
    assert r.account.reference_number == "ACC0000002"


# ═══ Withdrawals are unaffected (§18) ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_withdrawal_allocation_ignores_the_own_account_flag(db):
    """The Own Account rule is deposit-side. A withdrawal for a long-standing member picks exactly
    what it picked before: the nearest remaining DEBIT capacity, own or not."""
    await _acc(db, "ACC0000001", own=True, debit=500000.0)
    await _acc(db, "ACC0000002", own=False, debit=60000.0)
    for ref in ("ACC0000001", "ACC0000002"):
        await _row(db, f"fund-{ref}", 1_000_000.0, ref, member="FUNDER")
    for i in range(8):
        await _row(db, f"hist{i}", 1000.0, None, member="MBR1")

    r = await walloc.allocate_withdrawal_accounts(
        db, amount=20000, mode="IMPS", member_id="MBR1",
        beneficiary=walloc.read_beneficiary(mode="IMPS", account_number="1234567890",
                                            ifsc="HDFC0009999", name="Rita"))
    assert [l.candidate.ref for l in r.legs] == ["ACC0000002"]
