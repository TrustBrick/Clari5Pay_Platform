"""Deposit allocation: the sending account decides the type, and NEW/OLD is that account's own.

Two rules join the deposit engine here, and both are HARD — they sit with the disqualifications in
``_evaluate``, not among the preference tiers, so no later pool can reinstate an account they
refused:

  * **Savings pays into Savings, Current into Current.** Money must land in an account of the kind
    it was sent from. A managed account of the wrong type is not a worse choice, it is not a
    choice.
  * **NEW/OLD belongs to the SENDING ACCOUNT**, counted from deposits the platform actually
    RECEIVED from it. One member can hold an account that has never funded us beside one that has;
    they are not the same customer as far as placing this deposit goes.

Both figures come from ``services/member_account`` — the same answers the merchant form and the
Admin screen show. Deriving them a second time inside the engine is how the screen and the
allocation would come to disagree about one account.

The other half of the file is the Admin's manual fallback. When the engine finds nothing the
request waits for a human, and that human is checked by exactly the rules the engine obeys: an
account it would have refused cannot be let in by hand.

Run from the backend directory:

    python -m pytest tests/test_deposit_account_type_rules.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.api.routes import transactions as txr
from app.models.models import (
    AccountMaster, AccountType, DepositAllocation, Transaction, TxStatus, TxType,
)
from app.schemas.schemas import AccountSubmitRequest, DepositCreate
from app.services import deposit_allocation as alloc

from tests.test_deposit_allocation import (  # noqa: F401  (fixtures + builders)
    db, safe_refs_and_no_cache, _account, _merchant,
)
from tests.test_withdrawal_allocation import _admin  # noqa: F401

UPI_A = "member.a@ybl"
UPI_B = "member.b@okaxis"
MEMBER = "MBR20240001"

SAVINGS = AccountType.SAVINGS.value      # "Savings Account"
CURRENT = AccountType.CURRENT.value      # "Current Account"


def _dep(amount=45000.0, **kw) -> DepositCreate:
    base = dict(amount=amount, depositType="UPI", memberName="Member A", memberId=MEMBER,
                senderUpiId=UPI_A)
    base.update(kw)
    return DepositCreate(**base)


async def _received(db, merchant, ref: str, *, upi=UPI_A, member=MEMBER,
                    status=TxStatus.DEPOSITED, admin_ref=None):
    """A deposit in whatever status, attributed to one sending account."""
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=5000.0, status=status,
        merchant_id=merchant.id, merchant_name=merchant.name,
        tx_date=date.today(), tx_time="10:00:00", member_id=member, member_name="Member A",
        sender_upi_id=upi, admin_ref=admin_ref, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


# ═══ Rule 2 — account type is a hard gate ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("sender,managed,allocated", [
    ("SAVINGS", AccountType.SAVINGS, True),
    ("CURRENT", AccountType.CURRENT, True),
    ("SAVINGS", AccountType.CURRENT, False),
    ("CURRENT", AccountType.SAVINGS, False),
])
@pytest.mark.asyncio
async def test_money_lands_in_an_account_of_the_kind_it_was_sent_from(db, sender, managed, allocated):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="only", credit=100000.0, atype=managed)

    out = await txr.create_deposit(_dep(accountType=sender), db, merchant)

    if allocated:
        assert out["adminRef"] == "ACC1"
        assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    else:
        assert out["adminRef"] is None, "a mismatched account is not a choice"
        assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT


@pytest.mark.asyncio
async def test_the_wrong_type_is_refused_even_when_it_is_the_only_account_with_room(db):
    """The gate is hard: a Current account with capacity loses to no account at all."""
    merchant = await _merchant(db)
    await _account(db, "CUR", name="current", credit=500000.0, atype=AccountType.CURRENT)

    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    assert out["adminRef"] is None

    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.outcome != alloc.OUTCOME_ALLOCATED
    # The per-account reasons live on the engine's result rather than the journal row.
    r = await alloc.allocate_deposit_account(
        db, amount=45000, member_id=MEMBER, sender_account_type=SAVINGS)
    rejected = {c.ref: c.reject_reason for c in r.candidates}
    assert rejected["CUR"] == alloc.REJECT_ACCOUNT_TYPE


@pytest.mark.asyncio
async def test_the_refusal_names_the_type_problem_rather_than_blaming_a_limit(db):
    """An Admin sent to raise a limit that was never the obstacle has been told the wrong thing."""
    merchant = await _merchant(db)
    await _account(db, "CUR", name="current", credit=500000.0, atype=AccountType.CURRENT)

    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert "Savings Account" in (row.reason or "")
    assert "daily limit" not in (row.reason or "").lower()
    r = await alloc.allocate_deposit_account(
        db, amount=45000, member_id=MEMBER, sender_account_type=SAVINGS)
    assert r.detail["failure"] == alloc.FAIL_NO_MATCHING_TYPE


@pytest.mark.asyncio
async def test_the_right_type_is_chosen_from_a_mixed_pool(db):
    merchant = await _merchant(db)
    await _account(db, "CUR", name="c", credit=50000.0, atype=AccountType.CURRENT)
    await _account(db, "SAV", name="s", credit=60000.0, atype=AccountType.SAVINGS)

    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    assert out["adminRef"] == "SAV", "the nearer CURRENT account is not eligible at all"


@pytest.mark.asyncio
async def test_a_deposit_that_names_no_sending_account_carries_no_type_constraint(db):
    """Cash and crypto name no account. Inventing a constraint from missing data would send every
    one of them to the Admin queue."""
    merchant = await _merchant(db)
    await _account(db, "CUR", name="c", credit=100000.0, atype=AccountType.CURRENT)

    r = await alloc.allocate_deposit_account(db, amount=45000, member_id=MEMBER,
                                             sender_account_type=None)
    assert r.allocated is True and r.account.reference_number == "CUR"


# ═══ Rule 1 — NEW/OLD is the SENDING ACCOUNT's own received history ══════════════════════════════

@pytest.mark.asyncio
async def test_an_account_that_has_never_funded_us_is_a_new_customer(db):
    merchant = await _merchant(db)
    await _account(db, "USED", name="used", credit=100000.0)
    await _account(db, "FRESH", name="fresh", credit=100000.0)
    await _received(db, merchant, "OLD1", upi=UPI_B, admin_ref="USED")   # a DIFFERENT account

    # UPI_A has funded nothing, so this is a NEW customer and prefers the unused account.
    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "NEW"
    assert out["adminRef"] == "FRESH", "a new customer prefers an unused account"


@pytest.mark.asyncio
async def test_an_account_that_has_funded_us_is_an_existing_customer(db):
    merchant = await _merchant(db)
    await _account(db, "USED", name="used", credit=100000.0)
    await _account(db, "FRESH", name="fresh", credit=100000.0)
    await _received(db, merchant, "OLD1", upi=UPI_A, admin_ref="USED")   # THIS account

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "OLD"
    assert out["adminRef"] == "USED", "an existing customer's own account history comes first"


@pytest.mark.asyncio
async def test_two_accounts_of_one_member_are_classified_separately(db):
    """The requirement in a single test: same person, two accounts, two verdicts."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _received(db, merchant, "OLD1", upi=UPI_B, admin_ref="ACC1")

    from_a = await txr.create_deposit(_dep(accountType="CURRENT", senderUpiId=UPI_A), db, merchant)
    from_b = await txr.create_deposit(_dep(accountType="CURRENT", senderUpiId=UPI_B), db, merchant)

    def verdict(out):
        return (db.execute(select(DepositAllocation).where(
            DepositAllocation.transaction_ref == out["ref"])))
    a_row = (await verdict(from_a)).scalar_one()
    b_row = (await verdict(from_b)).scalar_one()
    assert (a_row.customer_type, b_row.customer_type) == ("NEW", "OLD")


@pytest.mark.parametrize("status", [
    TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED, TxStatus.SLIP_SUBMITTED,
    TxStatus.REJECTED, TxStatus.CANCELLED,
])
@pytest.mark.asyncio
async def test_a_deposit_that_never_arrived_leaves_the_account_new(db, status):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _received(db, merchant, "D1", upi=UPI_A, status=status, admin_ref="ACC1")

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "NEW", f"{status.value} is not money received"


@pytest.mark.asyncio
async def test_a_withdrawal_from_the_account_leaves_it_new(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    db.add(Transaction(
        ref="W1", type=TxType.WITHDRAWAL_REQUEST, amount=5000.0, status=TxStatus.COMPLETED,
        merchant_id=merchant.id, merchant_name=merchant.name, tx_date=date.today(),
        tx_time="10:00:00", member_id=MEMBER, sender_upi_id=UPI_A, created_at=datetime.utcnow()))
    await db.flush()

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "NEW", "money leaving says nothing about the account funding us"


@pytest.mark.asyncio
async def test_the_journal_records_the_count_the_verdict_rested_on(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    for i in range(3):
        await _received(db, merchant, f"D{i}", upi=UPI_A, admin_ref="ACC1")

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "OLD"
    # The figures the verdict rested on are carried on the engine's result detail.
    r = await alloc.allocate_deposit_account(
        db, amount=45000, member_id=MEMBER,
        sender_account_type=CURRENT, sender_account_deposits=3)
    assert r.detail["senderAccountDeposits"] == 3
    assert r.detail["senderAccountType"] == CURRENT
    assert r.detail["customerType"] == "OLD"


# ═══ Rule 7 — computed, still no action ═════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_five_plus_deposits_is_carried_but_changes_nothing(db):
    """Its business action is undefined, so none is invented — the flag is journalled and the
    allocation is exactly what it would have been at four deposits."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    for i in range(6):
        await _received(db, merchant, f"D{i}", upi=UPI_A, admin_ref="ACC1")

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    assert out["adminRef"] == "ACC1", "same account history rule, unchanged by the 5+ flag"
    r = await alloc.allocate_deposit_account(
        db, amount=45000, member_id=MEMBER,
        sender_account_type=CURRENT, sender_account_deposits=6)
    assert r.detail["fivePlusDeposits"] is True


# ═══ No eligible account — the manual fallback ══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_nothing_is_allocated_or_consumed_when_no_account_matches(db):
    merchant = await _merchant(db)
    await _account(db, "CUR", name="c", credit=500000.0, atype=AccountType.CURRENT)

    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    assert out["status"] == TxStatus.NO_ELIGIBLE_ACCOUNT
    assert out["adminRef"] is None, "no fake allocation"
    # And no capacity was taken: the account is still wholly free today.
    used = await alloc.credit_used_today(db, ["CUR"])
    assert round(used.get("CUR", 0.0), 2) == 0.0, "a refusal must not consume capacity"


@pytest.mark.asyncio
async def test_the_failure_is_journalled_so_the_admin_can_see_why(db):
    merchant = await _merchant(db)
    await _account(db, "CUR", name="c", credit=500000.0, atype=AccountType.CURRENT)
    admin = await _admin(db)

    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    decision = (await txr.get_allocation_decision(out["id"], db, admin))["decision"]
    assert decision["outcome"] != alloc.OUTCOME_ALLOCATED
    assert "Savings Account" in decision["reason"]


# ═══ The Admin's manual choice obeys the engine's rules ═════════════════════════════════════════

async def _stuck(db, merchant):
    """A deposit the engine could not place: Savings sender, Current-only pool."""
    await _account(db, "CUR", name="c", credit=500000.0, atype=AccountType.CURRENT)
    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    assert out["adminRef"] is None
    return out


@pytest.mark.asyncio
async def test_an_admin_can_place_a_deposit_the_engine_could_not(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)
    await _account(db, "SAV", name="s", credit=500000.0, atype=AccountType.SAVINGS)

    sent = await txr.account_submit(
        out["id"], AccountSubmitRequest(adminRef="SAV", adminBankDetails="SAV details"), db, admin)
    assert sent["adminRef"] == "SAV"
    assert sent["status"] == TxStatus.ACCOUNT_SUBMITTED


@pytest.mark.asyncio
async def test_an_admin_cannot_hand_pick_the_wrong_account_type(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(
            out["id"], AccountSubmitRequest(adminRef="CUR", adminBankDetails="x"), db, admin)
    assert e.value.status_code == 400
    assert "same type" in e.value.detail


@pytest.mark.asyncio
async def test_an_admin_cannot_hand_pick_past_the_highest_credit(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _account(db, "SAV", name="s", credit=100000.0, atype=AccountType.SAVINGS)
    out = await txr.create_deposit(_dep(amount=90000, accountType="SAVINGS"), db, merchant)
    assert out["adminRef"] == "SAV"                       # the engine placed the first one

    # A second 90,000 would take the account to 180,000 against a 100,000 ceiling.
    second = await txr.create_deposit(_dep(amount=90000, accountType="SAVINGS"), db, merchant)
    assert second["adminRef"] is None
    with pytest.raises(HTTPException) as e:
        await txr.account_submit(
            second["id"], AccountSubmitRequest(adminRef="SAV", adminBankDetails="x"), db, admin)
    assert e.value.status_code == 400
    assert "Highest Credit" in e.value.detail


@pytest.mark.asyncio
async def test_an_admin_cannot_hand_pick_an_inactive_account(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)
    await _account(db, "OFF", name="off", credit=500000.0,
                   atype=AccountType.SAVINGS, status="INACTIVE")

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(
            out["id"], AccountSubmitRequest(adminRef="OFF", adminBankDetails="x"), db, admin)
    assert e.value.status_code == 400
    assert "INACTIVE" in e.value.detail


@pytest.mark.asyncio
async def test_an_admin_cannot_hand_pick_an_account_with_no_credit_limit(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)
    await _account(db, "NOLIM", name="n", credit=0.0, atype=AccountType.SAVINGS)

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(
            out["id"], AccountSubmitRequest(adminRef="NOLIM", adminBankDetails="x"), db, admin)
    assert e.value.status_code == 400
    assert "Highest Credit" in e.value.detail


@pytest.mark.asyncio
async def test_an_admin_cannot_hand_pick_an_account_that_does_not_exist(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(
            out["id"], AccountSubmitRequest(adminRef="ACC9999", adminBankDetails="x"), db, admin)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_a_manual_send_that_passes_every_rule_is_accepted_exactly_at_the_ceiling(db):
    """The boundary: used + amount == Highest Credit is allowed by hand, as it is by the engine."""
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _account(db, "SAV", name="s", credit=100000.0, atype=AccountType.SAVINGS)
    await txr.create_deposit(_dep(amount=60000, accountType="SAVINGS"), db, merchant)

    exact = await txr.create_deposit(_dep(amount=40000, accountType="SAVINGS"), db, merchant)
    # The engine already placed it; a manual re-send of the same account must also be accepted.
    sent = await txr.account_submit(
        exact["id"], AccountSubmitRequest(adminRef="SAV", adminBankDetails="x"), db, admin)
    assert sent["adminRef"] == "SAV"


# ═══ What the Admin sees ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_admin_detail_carries_the_account_type_and_profile(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _account(db, "SAV", name="s", credit=100000.0, atype=AccountType.SAVINGS)
    out = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    detail = await txr.get_transaction_detail(out["id"], db, admin)
    assert detail["accountType"] == "SAVINGS"
    assert detail["accountTypeLabel"] == "Savings Account"
    assert detail["accountProfile"] == "NEW"
    assert detail["senderUpiId"] == UPI_A
    assert detail["adminRef"] == "SAV", "the automatically chosen account is visible"


@pytest.mark.asyncio
async def test_a_manually_assigned_account_is_visible_to_the_admin(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    out = await _stuck(db, merchant)
    await _account(db, "SAV", name="s", credit=500000.0, atype=AccountType.SAVINGS)
    await txr.account_submit(
        out["id"], AccountSubmitRequest(adminRef="SAV", adminBankDetails="x"), db, admin)

    detail = await txr.get_transaction_detail(out["id"], db, admin)
    assert detail["adminRef"] == "SAV"
    assert detail["status"] == TxStatus.ACCOUNT_SUBMITTED
    # And the journal still records that automation found nothing — the two together are what say
    # "this was assigned by hand".
    decision = (await txr.get_allocation_decision(out["id"], db, admin))["decision"]
    assert decision["outcome"] != alloc.OUTCOME_ALLOCATED


# ═══ Identity — one account however it is typed ═════════════════════════════════════════════════

@pytest.mark.parametrize("typed", ["MEMBER.A@YBL", "  member.a@ybl  ", "Member.A@Ybl"])
@pytest.mark.asyncio
async def test_history_and_saving_share_one_canonical_identity(db, typed):
    merchant = await _merchant(db)
    await _account(db, "ACC1", name="a", credit=100000.0)
    await _received(db, merchant, "D1", upi=UPI_A, admin_ref="ACC1")

    out = await txr.create_deposit(_dep(accountType="CURRENT", senderUpiId=typed), db, merchant)
    row = (await db.execute(select(DepositAllocation).where(
        DepositAllocation.transaction_ref == out["ref"]))).scalar_one()
    assert row.customer_type == "OLD", f"{typed!r} is the same account"


@pytest.mark.asyncio
async def test_a_capacity_refusal_says_which_type_it_measured(db):
    """The trap this closes: a Savings sender, a big CURRENT account, and a refusal that reads
    "larger than every account's Highest Credit".

    That sentence is false — a larger account exists — and it sends an Admin to raise a limit on
    an account this deposit can never use. Once a type constraint is in play every capacity
    message has to name the pool it measured.
    """
    merchant = await _merchant(db)
    await _account(db, "SAV", name="s", credit=100000.0, atype=AccountType.SAVINGS)
    await _account(db, "BIGCUR", name="c", credit=500000.0, atype=AccountType.CURRENT)

    r = await alloc.allocate_deposit_account(
        db, amount=150000, member_id=MEMBER, sender_account_type=SAVINGS)

    assert r.allocated is False, "must not cross over to the larger CURRENT account"
    assert "Savings Account" in r.reason, r.reason
    assert "every account's Highest Credit" not in r.reason, (
        "a bigger account DOES exist — it is just the wrong type")
    assert "100,000" in r.reason, "the ceiling quoted is the Savings pool's, not the platform's"


@pytest.mark.asyncio
async def test_a_capacity_refusal_without_a_type_constraint_is_unchanged(db):
    """Cash/crypto name no account, so the message keeps its original wording."""
    merchant = await _merchant(db)
    await _account(db, "A", name="a", credit=100000.0, atype=AccountType.CURRENT)

    r = await alloc.allocate_deposit_account(db, amount=150000, member_id=MEMBER)
    assert r.allocated is False
    assert "of the required type" not in r.reason, r.reason
