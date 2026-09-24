"""Account selection priority + the ACCOUNT_REQUESTED workflow, end to end.

What this suite pins (numbers refer to the requirement list it was written against):

  PRIORITY   1. EXACT ACCOUNT in the note → 2. SAME / LAST ACCOUNT → 3. SPECIFIC BANK →
             4. NEW customer: fewest OTHER customers served → 5/6. EXISTING customer: the existing
             rules → 7. nothing placeable → ACCOUNT_REQUESTED.
  EXPLICIT   An explicit request is met exactly or refused. It never falls through to another
             account or bank, and it never bypasses a hard limit.
  STATUS     A request automation cannot place is in ACCOUNT_REQUESTED — the Admin's existing
             manual queue — never NO_ELIGIBLE_ACCOUNT.
  VISIBILITY The merchant payload carries the status and nothing about why; the Admin-only
             allocation journal carries the reason and the parsed request.
  TELEGRAM   One ACCOUNT_REQUESTED alert per entry into that state, with ref, amount, request and
             reason; account numbers masked; retries never repeat it.
  DAILY DATE A deposit consumes daily credit on the day its account is ASSIGNED, not the day the
             request was raised.

Automation is forced ON by conftest; the tests about OFF switch it off themselves.

    python -m pytest tests/test_account_request_workflow.py -v
"""
from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import transactions as txr
from app.core.deps import get_current_admin
from app.models.models import (
    AccountMaster, AccountType, AdminUpi, Transaction, TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import (
    AccountSubmitRequest, CompleteRequest, DepositCreate, RemarkRequest, WithdrawalCreate,
)
from app.services import automation_mode
from app.services import deposit_allocation as alloc
from app.services import tg_notify as tgn

from tests.test_deposit_allocation import db, safe_refs_and_no_cache  # noqa: F401  (fixtures)


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def telegram(monkeypatch):
    """Record Telegram traffic instead of sending it: the ACCOUNT_REQUESTED alert and every
    ordinary workflow notification."""
    sent = {"alerts": [], "notify": []}

    async def _alert(db, tx, *, requested, reason):
        sent["alerts"].append({"ref": tx.ref, "requested": requested, "reason": reason,
                               "amount": tx.amount, "note": tx.notes})

    async def _notify(db, tx, target, event, actor=None, reason=None):
        sent["notify"].append((target, event))

    monkeypatch.setattr(tgn, "notify_account_request", _alert)
    monkeypatch.setattr(tgn, "notify", _notify)
    return sent


@pytest.fixture
def automation_off(monkeypatch):
    async def _off(_db) -> bool:
        return False
    monkeypatch.setattr(automation_mode, "is_enabled", _off)


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

TODAY = alloc.ist_today()
YESTERDAY = TODAY - timedelta(days=1)


async def _acc(db, ref: str, *, name: str | None = None, bank: str = "HDFC Bank",
               number: str | None = None, credit: float = 100000.0,
               daily_credit: float | None = None, debit: float = 100000.0,
               daily_debit: float | None = None, status: str = "ACTIVE") -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name or f"acct-{ref.lower()}",
        account_number=number or f"50100{ref[-4:]}000{ref[-2:]}", ifsc_code="HDFC0001234",
        bank_name=bank, branch="Mumbai", account_type=AccountType.CURRENT, status=status,
        created_date=date.today(), created_time="10:00:00",
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=0.0,
        bank_daily_credit_limit=credit if daily_credit is None else daily_credit,
        bank_daily_debit_limit=debit if daily_debit is None else daily_debit,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _row(db, ref: str, amount: float, account: str | None, *, member: str = "OTHER",
               status: TxStatus = TxStatus.DEPOSITED, on: date = TODAY,
               assigned_on: date | None = None, type_: TxType = TxType.DEPOSIT_REQUEST,
               **kw) -> Transaction:
    tx = Transaction(ref=ref, type=type_, amount=amount, status=status, merchant_id=7,
                     merchant_name="BELLAGIO", tx_date=on, tx_time="10:00:00", member_id=member,
                     admin_ref=account, account_assigned_on=assigned_on,
                     created_at=datetime.utcnow(), **kw)
    db.add(tx)
    await db.flush()
    return tx


async def _merchant(db) -> User:
    u = User(id=7, username="op7", name="BELLAGIO", role=UserRole.MERCHANT, hashed_password="x",
             email="op7@test.local", merchant_role="DATA_OPERATOR", pay_in="DEP")
    db.add(u)
    await db.flush()
    return u


async def _manager(db) -> User:
    u = User(id=8, username="mgr", name="BELLAGIO", role=UserRole.MERCHANT, hashed_password="x",
             email="mgr@test.local", merchant_role="MANAGER")
    db.add(u)
    await db.flush()
    return u


async def _admin(db) -> User:
    u = User(id=1, username="admin1", name="Admin One", role=UserRole.ADMIN, hashed_password="x",
             email="admin1@test.local")
    db.add(u)
    await db.flush()
    return u


def _dep(amount: float, note: str | None = None, member: str = "MBR1") -> DepositCreate:
    return DepositCreate(amount=amount, depositType="IMPS", memberName="Member One", memberId=member,
                         accountHolder="Member One", accountNumber="999", ifsc="HDFC0001234",
                         bankName="HDFC Bank", accountType="CURRENT", notes=note)


def _wit(amount: float) -> WithdrawalCreate:
    return WithdrawalCreate(amount=amount, memberId="MBR1", memberName="Rita", payoutMode="IMPS",
                            accountHolder="Rita", accountNumber="1234567890", ifsc="HDFC0009999",
                            bankName="HDFC Bank", branch="Mumbai", accountType="SAVINGS")


def _txid(tx: Transaction) -> str:
    return f"TXN{str(tx.id).zfill(3)}"


async def _allocate(db, amount: float, **kw):
    return await alloc.allocate_deposit_account(db, amount=amount, **kw)


async def _serve(db, account: str, customers: int, prefix: str) -> None:
    """``customers`` distinct members, each with a completed deposit into ``account`` (yesterday,
    so today's capacity is untouched and only the customer count differs)."""
    for i in range(customers):
        await _row(db, f"{prefix}{i}", 100.0, account, member=f"{prefix}M{i}", on=YESTERDAY,
                   assigned_on=YESTERDAY)


# ═══ NEW CUSTOMER — fewest other customers served (1-4) ═════════════════════════════════════════

@pytest.mark.asyncio
async def test_1_2_new_customer_prefers_the_account_that_served_fewer_customers(db):
    """A has served 10 customers, B has served 2; both eligible → a NEW customer goes to B. A is
    given the NEARER capacity on purpose: the rule is customer distribution, not capacity."""
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=60000.0)    # A: less room left
    await _acc(db, "ACC0000002", credit=100000.0, daily_credit=500000.0)   # B: more room left
    await _serve(db, "ACC0000001", 10, "A")
    await _serve(db, "ACC0000002", 2, "B")

    r = await _allocate(db, 5000, member_id="BRANDNEW", sender_account_deposits=0)

    assert r.customer_type == "NEW"
    assert r.account.reference_number == "ACC0000002"
    assert r.rule == alloc.RULES.NEW_FEWEST_SERVED
    served = {a["accountRef"]: a["customersServed"] for a in r.detail["accounts"]}
    assert served == {"ACC0000001": 10, "ACC0000002": 2}


@pytest.mark.asyncio
async def test_1_the_count_is_of_OTHER_customers_and_distinct(db):
    """Five deposits from ONE other customer is one customer served, and the member being placed
    is never counted against an account."""
    await _acc(db, "ACC0000001")
    await _acc(db, "ACC0000002")
    for i in range(5):
        await _row(db, f"x{i}", 100.0, "ACC0000001", member="SAMEPERSON", on=YESTERDAY,
                   assigned_on=YESTERDAY)
    await _serve(db, "ACC0000002", 2, "B")

    r = await _allocate(db, 1000, member_id="NEWBIE", sender_account_deposits=0)
    assert r.account.reference_number == "ACC0000001", "1 customer served beats 2"


@pytest.mark.asyncio
async def test_3_hard_limits_still_apply_to_the_least_served_account(db):
    """B has served fewer customers, but ₹60,000 is over B's per-transaction Highest Credit — B is
    not eligible, so the next account in the spread (A) takes it. Never B."""
    await _acc(db, "ACC0000001", credit=100000.0)
    await _acc(db, "ACC0000002", credit=50000.0)
    await _serve(db, "ACC0000001", 10, "A")
    await _serve(db, "ACC0000002", 2, "B")

    r = await _allocate(db, 60000, member_id="BRANDNEW", sender_account_deposits=0)
    assert r.account.reference_number == "ACC0000001"
    rejected = {a["accountRef"]: a["rejectReason"] for a in r.detail["accounts"]}
    assert rejected["ACC0000002"] == alloc.REJECT_OVER_PER_TXN


@pytest.mark.asyncio
async def test_3_daily_limit_also_applies_to_the_least_served_account(db):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=500000.0)
    await _acc(db, "ACC0000002", credit=100000.0, daily_credit=100000.0)
    await _serve(db, "ACC0000001", 10, "A")
    await _row(db, "t1", 90000.0, "ACC0000002", member="B0", assigned_on=TODAY)   # B: 10,000 left

    r = await _allocate(db, 20000, member_id="BRANDNEW", sender_account_deposits=0)
    assert r.account.reference_number == "ACC0000001"


@pytest.mark.asyncio
async def test_4_existing_customer_keeps_the_existing_rules(db):
    """An EXISTING customer is not spread: the account they already use is reused (the existing
    rule), even though it has served far more customers than the alternative."""
    await _acc(db, "ACC0000001")
    await _acc(db, "ACC0000002")
    await _serve(db, "ACC0000001", 10, "A")
    await _row(db, "mine", 1000.0, "ACC0000001", member="OLDMEMBER", on=YESTERDAY,
               assigned_on=YESTERDAY)

    r = await _allocate(db, 5000, member_id="OLDMEMBER", sender_account_deposits=3)
    assert r.customer_type == "OLD"
    assert r.account.reference_number == "ACC0000001"
    assert r.rule == alloc.RULES.OLD_ACCOUNT_HISTORY


# ═══ SAME / LAST ACCOUNT (5-9) ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["same account", "use same account", "give same account",
                                  "previous account", "last account", "use my last account",
                                  "USE MY LAST ACCOUNT"])
async def test_5_6_same_account_phrasings_select_the_last_successful_account(db, note):
    await _acc(db, "ACC0000001")
    await _acc(db, "ACC0000002", credit=60000.0)          # the nearer capacity — must NOT win
    await _row(db, "prev", 1000.0, "ACC0000001", member="MBR1", on=YESTERDAY,
               assigned_on=YESTERDAY)

    r = await _allocate(db, 5000, member_id="MBR1", note=note)
    assert r.note.request_type == alloc.REQUEST_SAME_ACCOUNT
    assert r.account.reference_number == "ACC0000001"
    assert r.rule == alloc.RULES.SAME_ACCOUNT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TxStatus.REJECTED, TxStatus.CANCELLED, TxStatus.SA_REJECTED,
                                    TxStatus.ACCOUNT_SUBMITTED, TxStatus.SLIP_SUBMITTED])
async def test_7_a_newer_non_successful_deposit_is_not_the_last_account(db, status):
    """ACC1 received the member's last SUCCESSFUL deposit. A NEWER request on ACC2 that was
    rejected, cancelled or is still pending does not make ACC2 "the last account"."""
    await _acc(db, "ACC0000001")
    await _acc(db, "ACC0000002")
    await _row(db, "ok", 1000.0, "ACC0000001", member="MBR1", on=YESTERDAY, assigned_on=YESTERDAY)
    await _row(db, "newer", 1000.0, "ACC0000002", member="MBR1", status=status,
               on=YESTERDAY, assigned_on=YESTERDAY)

    assert await alloc.last_successful_account(db, "MBR1") == "ACC0000001"
    r = await _allocate(db, 5000, member_id="MBR1", note="same account")
    assert r.account.reference_number == "ACC0000001"


@pytest.mark.asyncio
async def test_8_9_same_account_unavailable_goes_to_account_requested_without_fallback(
        db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    await _acc(db, "ACC0000002", credit=100000.0)                 # perfectly good — must NOT be used
    await _row(db, "prev", 1000.0, "ACC0000001", member="MBR1", on=YESTERDAY, assigned_on=YESTERDAY)
    await _row(db, "full", 99000.0, "ACC0000001", member="X", assigned_on=TODAY)   # 1,000 left

    out = await txr.create_deposit(_dep(5000, "use same account"), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None, "no silent fallback to ACC0000002"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["failure"] == alloc.FAIL_SAME_ACCOUNT_NOT_ELIGIBLE
    assert decision["request"]["sameAccountRef"] == "ACC0000001"
    assert "Bank Daily Credit Limit" in decision["reason"]


@pytest.mark.asyncio
async def test_same_account_with_no_successful_history_goes_to_the_admin(db):
    await _acc(db, "ACC0000001")
    await _row(db, "pending", 1000.0, "ACC0000001", member="MBR1", status=TxStatus.ACCOUNT_SUBMITTED)

    r = await _allocate(db, 5000, member_id="MBR1", note="use my last account")
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_NO_PREVIOUS_ACCOUNT


# ═══ SPECIFIC BANK (10-14) ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["Use HDFC", "Give me HDFC", "use hdfc bank please"])
async def test_10_a_requested_bank_is_used_when_eligible(db, note):
    await _acc(db, "ACC0000001", bank="HDFC Bank", credit=100000.0, daily_credit=500000.0)
    await _acc(db, "ACC0000002", bank="State Bank of India", credit=100000.0, daily_credit=60000.0)

    r = await _allocate(db, 5000, member_id="OLDM", sender_account_deposits=2, note=note)
    assert r.account.reference_number == "ACC0000001", "SBI's nearer capacity does not beat the request"
    assert r.rule == alloc.RULES.NOTE_BANK


@pytest.mark.asyncio
@pytest.mark.parametrize("note,bank", [("Use SBI", "State Bank of India"),
                                        ("Use Bank of Baroda", "Bank of Baroda"),
                                        ("give me bob", "Bank of Baroda")])
async def test_10_bank_aliases_and_full_names(db, note, bank):
    await _acc(db, "ACC0000001", bank="HDFC Bank")
    await _acc(db, "ACC0000002", bank="State Bank of India")
    await _acc(db, "ACC0000003", bank="Bank of Baroda")
    r = await _allocate(db, 5000, member_id="OLDM", note=note)
    assert r.note.bank_name == bank
    assert r.account.bank_name == bank


@pytest.mark.asyncio
async def test_10_use_SINDU_names_the_account_called_SINDU(db):
    """"SINDU" is an account NAME in Account Management, which is more specific than a bank."""
    await _acc(db, "ACC0000001", name="SINDU", bank="Bank of Baroda")
    await _acc(db, "ACC0000002", name="other", bank="Bank of Baroda", credit=60000.0)
    r = await _allocate(db, 5000, member_id="OLDM", note="Use SINDU")
    assert r.note.request_type == alloc.REQUEST_EXACT_ACCOUNT
    assert r.account.reference_number == "ACC0000001"


@pytest.mark.asyncio
async def test_10_several_accounts_at_the_bank_are_ranked_by_the_normal_rules(db):
    """Three HDFC accounts: one inactive, and a NEW customer goes to the least-served eligible one."""
    await _acc(db, "ACC0000001", bank="HDFC Bank", status="INACTIVE")
    await _acc(db, "ACC0000002", bank="HDFC Bank")
    await _acc(db, "ACC0000003", bank="HDFC Bank")
    await _acc(db, "ACC0000004", bank="State Bank of India")
    await _serve(db, "ACC0000002", 5, "B")
    await _serve(db, "ACC0000003", 1, "C")

    r = await _allocate(db, 5000, member_id="NEWBIE", sender_account_deposits=0, note="Use HDFC")
    assert r.account.reference_number == "ACC0000003"


@pytest.mark.asyncio
async def test_11_12_13_14_requested_bank_unavailable_never_switches_bank(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", bank="HDFC Bank", credit=10000.0)          # can't take 50,000
    await _acc(db, "ACC0000002", bank="State Bank of India", credit=100000.0)

    out = await txr.create_deposit(_dep(50000, "Use HDFC"), db, merchant)

    # 11 / 12 — no switch to SBI; the request waits for the Admin.
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None
    # 13 — the Admin sees the internal reason and what was asked for.
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["failure"] == alloc.FAIL_REQUESTED_BANK_NOT_ELIGIBLE
    assert decision["request"]["type"] == alloc.REQUEST_BANK
    assert decision["request"]["bankName"] == "HDFC Bank"
    assert "Highest Credit" in decision["reason"] and "NOT substituted" in decision["reason"]
    assert decision["merchantNote"] == "Use HDFC"
    # 14 — the merchant's payload says ACCOUNT_REQUESTED and nothing about why.
    blob = json.dumps(out, default=str)
    for leak in ("Highest Credit", "Daily Credit", "NOT substituted", "REQUESTED_BANK",
                 "NO_ELIGIBLE", "not eligible", "capacity"):
        assert leak not in blob, f"merchant payload leaks {leak!r}"


# ═══ EXACT ACCOUNT (15-20) ══════════════════════════════════════════════════════════════════════

async def _three_accounts(db):
    await _acc(db, "ACC0000123", number="1234567890", credit=100000.0, daily_credit=500000.0)
    await _acc(db, "ACC0000124", number="5550001111", credit=100000.0, daily_credit=60000.0)
    await _acc(db, "ACC0000125", number="9990002222", bank="State Bank of India")


@pytest.mark.asyncio
@pytest.mark.parametrize("note,match", [
    ("Use account 1234567890", alloc.MATCH_ACCOUNT_NUMBER),     # 15 — full number
    ("a/c no: 1234567890 pls", alloc.MATCH_ACCOUNT_NUMBER),
    ("Give account ending 7890", alloc.MATCH_ACCOUNT_ENDING),   # 16 — unique trailing digits
    ("ends with 7890", alloc.MATCH_ACCOUNT_ENDING),
    ("last 4 digits 7890", alloc.MATCH_ACCOUNT_ENDING),
    ("Use ACC0000123", alloc.MATCH_ACCOUNT_ID),                 # 17 — the account ID
    ("use acc0000123", alloc.MATCH_ACCOUNT_ID),
])
async def test_15_16_17_exact_account_is_identified_and_used(db, note, match):
    await _three_accounts(db)
    r = await _allocate(db, 5000, member_id="OLDM", sender_account_deposits=2, note=note)
    assert r.note.request_type == alloc.REQUEST_EXACT_ACCOUNT
    assert r.note.account_match == match
    assert r.account.reference_number == "ACC0000123"
    assert r.rule == alloc.RULES.EXACT_ACCOUNT


@pytest.mark.asyncio
async def test_15_an_account_number_with_stray_spaces_in_the_catalogue_still_matches(db):
    await _acc(db, "ACC0000123", number=" 1234567890 ")
    r = await _allocate(db, 5000, member_id="OLDM", note="Use account 1234567890")
    assert r.account.reference_number == "ACC0000123"


@pytest.mark.asyncio
async def test_18_20_exact_account_unavailable_goes_to_account_requested_not_elsewhere(
        db, telegram):
    merchant = await _merchant(db)
    await _three_accounts(db)
    await _row(db, "busy", 100000.0, "ACC0000123", member="X", assigned_on=TODAY)
    await db.execute(AccountMaster.__table__.update()
                     .where(AccountMaster.reference_number == "ACC0000123")
                     .values(bank_daily_credit_limit=100000.0))

    out = await txr.create_deposit(_dep(5000, "Use account 1234567890"), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["adminRef"] is None, "the other two eligible accounts are NOT substituted"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["failure"] == alloc.FAIL_REQUESTED_ACCOUNT_NOT_ELIGIBLE
    assert decision["request"]["accountRef"] == "ACC0000123"
    assert decision["request"]["account"]["bankDailyCreditLimit"] == 100000.0


@pytest.mark.asyncio
async def test_18_exact_account_never_bypasses_a_hard_limit(db):
    """Asked for by number, but ₹1,50,000 is over its per-transaction Highest Credit: refused."""
    await _three_accounts(db)
    r = await _allocate(db, 150000, member_id="OLDM", note="Use ACC0000123")
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_REQUESTED_ACCOUNT_NOT_ELIGIBLE
    assert "Highest Credit" in r.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["Give account ending 7890",       # fits two accounts
                                  "Use ACC0000123 or 5550001111"])   # two identifiers disagree
async def test_19_an_ambiguous_account_request_is_never_guessed(db, note):
    await _acc(db, "ACC0000123", number="1234567890")
    await _acc(db, "ACC0000124", number="5550001111")
    await _acc(db, "ACC0000126", number="4444447890")
    r = await _allocate(db, 5000, member_id="OLDM", note=note)
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_REQUESTED_ACCOUNT_AMBIGUOUS
    assert r.note.account_status == alloc.ACCOUNT_AMBIGUOUS


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["Use ACC0009999", "Use account 8888888888", "Use this account",
                                  "give account ending 4321"])
async def test_20_an_unknown_or_unspecified_account_is_not_substituted(db, note):
    await _three_accounts(db)
    r = await _allocate(db, 5000, member_id="OLDM", note=note)
    assert r.allocated is False
    assert r.failure_code == alloc.FAIL_REQUESTED_ACCOUNT_NOT_FOUND


@pytest.mark.asyncio
async def test_a_utr_or_amount_in_the_note_is_not_an_account_request(db):
    """Digits that are not one of OUR account numbers, and not presented as an account, are
    ignored — a UTR in a note must not send the deposit to the Admin."""
    await _three_accounts(db)
    r = await _allocate(db, 5000, member_id="OLDM", note="paid UTR 412345678901 amount 50000")
    assert r.note.request_type is None
    assert r.allocated is True


# ═══ PRIORITY ═══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_priority_exact_account_beats_same_account_beats_bank(db):
    await _three_accounts(db)                                      # 123/124 HDFC, 125 SBI
    await _row(db, "prev", 1000.0, "ACC0000124", member="MBR1", on=YESTERDAY, assigned_on=YESTERDAY)

    both = await _allocate(db, 5000, member_id="MBR1", note="same account, ACC0000125 please")
    assert both.account.reference_number == "ACC0000125", "exact account first"

    same_bank = await _allocate(db, 5000, member_id="MBR1", note="use same account at SBI")
    assert same_bank.account.reference_number == "ACC0000124", "same account before bank"


# ═══ STATUS / VISIBILITY / MANUAL WORKFLOW (21-25) ══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_21_24_no_eligible_deposit_is_account_requested_with_the_reason_for_the_admin(
        db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", credit=10000.0)

    out = await txr.create_deposit(_dep(50000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    row = (await db.execute(select(Transaction))).scalars().one()
    assert row.status == TxStatus.ACCOUNT_REQUESTED, "the STORED status, not a relabel"
    decision = (await txr.get_allocation_decision(out["id"], db, await _admin(db)))["decision"]
    assert decision["outcome"] == alloc.OUTCOME_NO_ACCOUNT
    assert decision["failure"] == alloc.FAIL_AMOUNT_TOO_LARGE
    assert "Highest Credit" in decision["reason"]


def test_24_the_allocation_decision_is_admin_only():
    dep = inspect.signature(txr.get_allocation_decision).parameters["_"].default
    assert dep.dependency is get_current_admin
    dep = inspect.signature(txr.get_payout_allocation).parameters["_"].default
    assert dep.dependency is get_current_admin


@pytest.mark.asyncio
async def test_23_a_legacy_no_eligible_row_is_reported_as_account_requested(db):
    """Even a row still stored as NO_ELIGIBLE_ACCOUNT (the migration moves them) is never shown as
    that to anyone."""
    tx = await _row(db, "legacy", 5000.0, None, status=TxStatus.NO_ELIGIBLE_ACCOUNT)
    await txr._refresh_with_images(db, tx)          # the serializer reads deferred columns
    assert txr._t(tx, full=False)["status"] == TxStatus.ACCOUNT_REQUESTED


@pytest.mark.asyncio
async def test_25_the_admin_places_it_with_the_existing_manual_send(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", credit=10000.0)
    await _acc(db, "ACC0000002", bank="State Bank of India", credit=100000.0)
    out = await txr.create_deposit(_dep(50000, "Use HDFC"), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED

    # The Admin decides to use the SBI account after all — the existing manual send, which still
    # enforces every hard rule itself.
    sent = await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC0000002"), db,
                                    await _admin(db))

    assert sent["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert sent["adminRef"] == "ACC0000002"
    row = (await db.execute(select(Transaction))).scalars().one()
    assert row.account_assigned_on == TODAY
    assert row.account_request_alerted_at is None, "placed — a later re-entry would alert again"


@pytest.mark.asyncio
async def test_25_the_manual_send_still_refuses_an_account_that_breaks_a_limit(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", credit=10000.0)
    out = await txr.create_deposit(_dep(50000), db, merchant)
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminRef="ACC0000001"), db,
                                 await _admin(db))
    assert e.value.status_code == 400 and "Highest Credit" in e.value.detail


# ═══ WITHDRAWAL → ACCOUNT_REQUESTED (22) ════════════════════════════════════════════════════════

async def _fund(db, account: str, amount: float) -> None:
    await _row(db, f"fund-{account}", amount, account, member="FUNDER", on=YESTERDAY,
               assigned_on=YESTERDAY)


@pytest.mark.asyncio
async def test_22_an_approved_withdrawal_with_no_eligible_account_is_account_requested(
        db, telegram):
    merchant, manager = await _merchant(db), await _manager(db)
    await _acc(db, "ACC0000001", debit=10000.0)                  # cannot pay 45,000
    await _fund(db, "ACC0000001", 500000.0)

    created = await txr.create_withdrawal(_wit(45000), None, db, merchant)
    assert ("ADMIN", "withdrawal_request") not in telegram["notify"], \
        "no Admin Telegram at creation — the Manager owns that step"

    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert len(telegram["alerts"]) == 1, "the ACCOUNT_REQUESTED alert, once"
    assert ("ADMIN", "manager_verified") not in telegram["notify"], \
        "not 'Complete the withdrawal payment' — there is no account to pay from yet"
    assert telegram["alerts"][0]["reason"]


@pytest.mark.asyncio
async def test_22_a_placeable_withdrawal_still_gets_the_ordinary_admin_message(db, telegram):
    merchant, manager = await _merchant(db), await _manager(db)
    await _acc(db, "ACC0000001")
    await _fund(db, "ACC0000001", 500000.0)
    created = await txr.create_withdrawal(_wit(45000), None, db, merchant)
    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert ("ADMIN", "manager_verified") in telegram["notify"]
    assert telegram["alerts"] == []


# ═══ TELEGRAM (26-32) ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_26_to_30_the_alert_carries_ref_amount_request_and_reason(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000123", number="1234567890", credit=10000.0)

    out = await txr.create_deposit(_dep(50000, "Use account 1234567890"), db, merchant)

    assert len(telegram["alerts"]) == 1                                         # 26
    alert = telegram["alerts"][0]
    assert alert["ref"] == out["ref"]                                           # 27
    assert alert["amount"] == 50000                                             # 28
    assert "ACC0000123" in alert["requested"] and "XXXXXX7890" in alert["requested"]   # 29
    assert "1234567890" not in alert["requested"], "the full account number is masked"
    assert alert["reason"] == "Requested account could not be automatically assigned."  # 30
    # The generic "upload account details" message is not sent as well.
    assert ("ADMIN", "deposit_request") not in telegram["notify"]


def test_29_30_the_message_body_itself():
    tx = Transaction(ref="DEP000123", type=TxType.DEPOSIT_REQUEST, amount=50000.0,
                     member_id="MID000123", member_name="A Member", notes="Use my same account")
    body = tgn._build_account_request(
        tx, requested="Same / last account — HDFC Bank / sindu (ACC0000001) · A/C XXXXXX1234",
        reason="Requested same / last account could not be automatically assigned.",
        kind="Transaction")
    for part in ("ACCOUNT REQUEST REQUIRES ADMIN ACTION", "DEP000123", "MID000123",
                 "₹50,000.00", "XXXXXX1234", '"Use my same account"',
                 "could not be automatically assigned", "ACCOUNT_REQUESTED",
                 "Admin review required"):
        assert part in body, part


@pytest.mark.asyncio
async def test_31_32_retries_never_repeat_the_alert(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001", credit=10000.0)
    out = await txr.create_deposit(_dep(50000), db, merchant)
    admin = await _admin(db)

    for _ in range(3):
        with pytest.raises(HTTPException) as e:
            await txr.retry_allocation(out["id"], db, admin)
        assert e.value.status_code == 409 and "Highest Credit" in e.value.detail

    assert len(telegram["alerts"]) == 1


@pytest.mark.asyncio
async def test_31_the_claim_is_once_per_entry_and_re_arms_after_placement(db, telegram):
    tx = await _row(db, "W1", 5000.0, None, status=TxStatus.ACCOUNT_REQUESTED)
    assert await txr._alert_account_request_once(db, tx, requested=None, code=None) is True
    assert await txr._alert_account_request_once(db, tx, requested=None, code=None) is False
    txr._clear_account_request_alert(tx)                    # placed…
    await db.flush()
    assert await txr._alert_account_request_once(db, tx, requested=None, code=None) is True
    assert len(telegram["alerts"]) == 2                     # …and a NEW entry alerts again


@pytest.mark.asyncio
async def test_a_placed_deposit_sends_no_alert(db, telegram):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001")
    out = await txr.create_deposit(_dep(5000), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert telegram["alerts"] == []


# ═══ AUTOMATION OFF (33-36) ═════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_33_off_a_new_deposit_is_account_requested(db, telegram, automation_off):
    merchant = await _merchant(db)
    await _acc(db, "ACC0000001")
    out = await txr.create_deposit(_dep(5000), db, merchant)
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert ("ADMIN", "deposit_request") in telegram["notify"], "the ordinary Admin request"
    assert telegram["alerts"] == [], "automation did not run, so there is no exception"


@pytest.mark.asyncio
async def test_34_off_an_approved_withdrawal_is_account_requested_without_noise(
        db, telegram, automation_off):
    merchant, manager = await _merchant(db), await _manager(db)
    await _acc(db, "ACC0000001")
    await _fund(db, "ACC0000001", 500000.0)
    created = await txr.create_withdrawal(_wit(45000), None, db, merchant)
    assert ("ADMIN", "withdrawal_request") not in telegram["notify"]
    out = await txr.manager_approve(created["id"], RemarkRequest(remark="ok"), None, db, manager)
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert telegram["alerts"] == []


@pytest.mark.asyncio
async def test_35_36_a_legacy_no_eligible_withdrawal_is_recovered_and_payable(
        db, telegram, automation_off):
    await _acc(db, "ACC0000001")
    await _fund(db, "ACC0000001", 500000.0)
    tx = await _row(db, "WIT-OLD", 45000.0, None, member="MBR1", type_=TxType.WITHDRAWAL_REQUEST,
                    status=TxStatus.NO_ELIGIBLE_ACCOUNT, payout_mode="IMPS",
                    account_number="1234567890", ifsc="HDFC0009999", account_holder="Rita")
    admin = await _admin(db)

    out = await txr.retry_payout_allocation(_txid(tx), None, db, admin)     # 35 — no 409
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED

    # 36 — and the Admin can pay it from a named account; the engine still validates it.
    legs = await txr._resolve_payout_legs(
        db, tx, CompleteRequest(paymentMethod="BANK", payoutAccountRef="ACC0000001"), admin, None)
    assert [(l.account_ref, l.amount) for l in legs] == [("ACC0000001", 45000.0)]


@pytest.mark.asyncio
async def test_35_a_legacy_no_eligible_deposit_is_recovered_too(db, telegram, automation_off):
    await _acc(db, "ACC0000001")
    tx = await _row(db, "DEP-OLD", 5000.0, None, member="MBR1", status=TxStatus.NO_ELIGIBLE_ACCOUNT,
                    deposit_type="IMPS")
    out = await txr.retry_allocation(_txid(tx), db, await _admin(db))
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED


# ═══ DAILY DATE (37-39) ═════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_37_a_request_raised_yesterday_and_placed_today_counts_today(db, telegram):
    """Yesterday: ₹1,50,000 requested, no account. Today: the Admin assigns ACC1 → it consumes
    TODAY's ₹2,00,000 daily limit, so a further ₹60,000 today is refused."""
    await _acc(db, "ACC0000001", credit=200000.0, daily_credit=200000.0)
    old = await _row(db, "DEP-Y", 150000.0, None, member="MBR1", on=YESTERDAY,
                     status=TxStatus.ACCOUNT_REQUESTED, deposit_type="IMPS")

    await txr.account_submit(_txid(old), AccountSubmitRequest(adminRef="ACC0000001"), db,
                             await _admin(db))

    assert old.account_assigned_on == TODAY
    assert (await alloc.credit_used_today(db, ["ACC0000001"]))["ACC0000001"] == 150000.0
    assert (await _allocate(db, 60000)).allocated is False
    assert (await _allocate(db, 50000)).allocated is True


@pytest.mark.asyncio
async def test_37_the_automatic_retry_path_dates_it_today_as_well(db, telegram):
    await _acc(db, "ACC0000001", credit=200000.0, daily_credit=200000.0)
    old = await _row(db, "DEP-Y", 150000.0, None, member="MBR1", on=YESTERDAY,
                     status=TxStatus.ACCOUNT_REQUESTED, deposit_type="IMPS")
    out = await txr.retry_allocation(_txid(old), db, await _admin(db))
    assert out["adminRef"] == "ACC0000001"
    assert (await alloc.credit_used_today(db, ["ACC0000001"]))["ACC0000001"] == 150000.0


@pytest.mark.asyncio
async def test_38_usage_belongs_to_the_assignment_day_across_midnight(db):
    await _acc(db, "ACC0000001", credit=200000.0, daily_credit=200000.0)
    # Assigned yesterday: counts yesterday, not today — whatever day it was raised.
    await _row(db, "a", 200000.0, "ACC0000001", on=YESTERDAY - timedelta(days=3),
               assigned_on=YESTERDAY, status=TxStatus.ACCOUNT_SUBMITTED)
    assert (await alloc.credit_used_today(db, ["ACC0000001"], on=YESTERDAY))["ACC0000001"] == 200000.0
    assert (await alloc.credit_used_today(db, ["ACC0000001"])).get("ACC0000001", 0.0) == 0.0
    assert (await _allocate(db, 200000)).allocated is True, "today starts at ₹0"


@pytest.mark.asyncio
async def test_38_rows_from_before_the_column_fall_back_to_tx_date(db):
    await _acc(db, "ACC0000001", credit=200000.0, daily_credit=200000.0)
    await _row(db, "legacy", 120000.0, "ACC0000001", assigned_on=None)      # tx_date = today
    assert (await alloc.credit_used_today(db, ["ACC0000001"]))["ACC0000001"] == 120000.0


@pytest.mark.asyncio
async def test_39_historical_reporting_keeps_the_request_date(db, telegram):
    """Assigning today never rewrites when the request was RAISED — reports and the Payin Summary
    still read tx_date, so history does not move."""
    await _acc(db, "ACC0000001", credit=200000.0)
    old = await _row(db, "DEP-Y", 1000.0, None, on=YESTERDAY, status=TxStatus.ACCOUNT_REQUESTED,
                     deposit_type="IMPS")
    await txr.account_submit(_txid(old), AccountSubmitRequest(adminRef="ACC0000001"), db,
                             await _admin(db))
    assert old.tx_date == YESTERDAY


# ═══ LIMITS (40-45) — the per-transaction / daily split is pinned in depth by
# test_limit_semantics.py (§17 cases 1-22) and test_limit_final_rules.py. These are the headline
# figures once more, through the new priority code path. ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_40_highest_credit_is_per_transaction(db):
    await _acc(db, "ACC0000001", credit=75000.0, daily_credit=10_000_000.0)
    for i, amt in enumerate([70000, 20000, 30000, 75000]):
        assert (await _allocate(db, amt)).allocated is True, amt
        await _row(db, f"d{i}", amt, "ACC0000001", assigned_on=TODAY,
                   status=TxStatus.ACCOUNT_SUBMITTED)
    assert (await _allocate(db, 75000.01)).allocated is False


@pytest.mark.asyncio
async def test_42_44_bank_daily_credit_is_cumulative_and_includes_manual_credit(db):
    from tests.test_limit_semantics import _manual
    await _acc(db, "ACC0000001", credit=75000.0, daily_credit=200000.0)
    await _row(db, "a", 60000.0, "ACC0000001", assigned_on=TODAY)
    await _row(db, "b", 60000.0, "ACC0000001", assigned_on=TODAY)
    await _manual(db, "ACC0000001", 50000.0, "CREDIT")
    assert (await _allocate(db, 40000)).allocated is False       # 1,70,000 + 40,000 > 2,00,000
    assert (await _allocate(db, 30000)).allocated is True        # exactly 2,00,000
