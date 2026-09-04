"""Cross-bank exact splitting, and explaining a refusal honestly.

A withdrawal larger than any single account is NOT a withdrawal the platform cannot pay. The
engine combines accounts — across banks when it has to — and the legs must sum to the requested
amount exactly. What no combination may ever do is exceed an account's daily Highest Debit or its
available balance, or part-pay the withdrawal.

The refusal message matters as much as the refusal. Reporting "the largest account's Highest Debit
is X" in an engine that splits invites an Admin to raise one limit when the shortfall is across
all of them; the honest figure is the COMBINED capacity and the gap.

Run from the backend directory:

    python -m pytest tests/test_cross_bank_split.py -v
"""
from __future__ import annotations

import pytest

from app.services import withdrawal_allocation as wa

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures)
    db, safe_refs_and_no_cache, _account, _admin, _allocate, _ben, _fund, _withdrawal,
)


async def _bank(db, ref: str, bank: str, capacity: float, *, balance: float | None = None):
    """An account whose USABLE capacity is `capacity` — funded well beyond it unless told
    otherwise, so the daily limit is what binds."""
    await _account(db, ref, bank=bank, debit=capacity)
    await _fund(db, "F" + ref, ref, capacity * 3 if balance is None else balance)


# ── The worked example ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eight_hundred_thousand_splits_across_five_banks_exactly(db):
    """800,000 over five banks whose capacities are 100k/200k/150k/175k/175k.

    No single account can carry it. The five together are exactly enough, and the legs must total
    the requested amount to the paisa.
    """
    await _bank(db, "HDFC-01", "HDFC Bank", 100000)
    await _bank(db, "ICICI-02", "ICICI Bank", 200000)
    await _bank(db, "SBI-03", "State Bank of India", 150000)
    await _bank(db, "BOB-04", "Bank of Baroda", 175000)
    await _bank(db, "AXIS-05", "Axis Bank", 175000)

    r = await _allocate(db, 800000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_SPLIT, r.reason
    shares = {l.ref: l.amount for l in r.legs}
    assert shares == {"HDFC-01": 100000, "ICICI-02": 200000, "SBI-03": 150000,
                      "BOB-04": 175000, "AXIS-05": 175000}, shares
    assert round(sum(shares.values()), 2) == 800000.00
    # Five banks, so this is a genuine cross-bank allocation.
    assert len({l.candidate.account.bank_name for l in r.legs}) == 5


@pytest.mark.asyncio
async def test_no_leg_exceeds_its_own_daily_limit_or_balance(db):
    """The split may combine freely; it may never lift a single account past either ceiling."""
    await _bank(db, "A", "HDFC Bank", 100000)
    await _bank(db, "B", "ICICI Bank", 200000)
    await _bank(db, "C", "Axis Bank", 150000, balance=60000)   # balance binds, not the limit

    r = await _allocate(db, 330000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert round(r.total, 2) == 330000
    for leg in r.legs:
        assert leg.amount <= leg.candidate.remaining + 1e-9, "daily limit breached"
        assert leg.amount <= leg.candidate.balance + 1e-9, "balance breached"
    # Largest usable first (B 200k, A 100k), so C supplies only the 30k remainder — the final
    # leg is trimmed to what is left, never rounded up to the account's whole capacity.
    assert {l.ref: l.amount for l in r.legs}["C"] == 30000


# ── Single account still preferred ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_account_that_can_carry_it_is_still_preferred(db):
    await _bank(db, "A", "HDFC Bank", 100000)
    await _bank(db, "BIG", "ICICI Bank", 900000)

    r = await _allocate(db, 800000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert [l.ref for l in r.legs] == ["BIG"]


@pytest.mark.asyncio
async def test_the_requested_bank_is_used_when_it_can_cover_the_amount(db):
    await _bank(db, "HDFC-01", "HDFC Bank", 900000)
    await _bank(db, "ICICI-02", "ICICI Bank", 900000)

    r = await _allocate(db, 800000, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert [l.ref for l in r.legs] == ["HDFC-01"]


# ── Cross-bank fallback ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_requested_bank_that_cannot_cover_it_expands_to_other_banks(db):
    """The preference narrows the choice; it must never cap what the platform can pay."""
    await _bank(db, "HDFC-01", "HDFC Bank", 100000)
    await _bank(db, "ICICI-02", "ICICI Bank", 200000)

    r = await _allocate(db, 250000, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert round(r.total, 2) == 250000
    assert r.requested_unavailable is True, "the unmet preference must be recorded"
    assert {l.candidate.account.bank_name for l in r.legs} == {"HDFC Bank", "ICICI Bank"}


@pytest.mark.asyncio
async def test_a_split_stays_inside_the_requested_bank_when_that_bank_can_cover_it(db):
    """Two HDFC accounts that together cover the amount beat pulling in another bank.

    Note the precedence this depends on: the bank preference shapes the SPLIT, but a single
    account that can carry the whole amount still wins first, even at another bank — see
    ``test_a_single_account_elsewhere_beats_splitting_inside_the_requested_bank``. So ICICI here
    is deliberately too small to carry it alone.
    """
    await _bank(db, "HDFC-01", "HDFC Bank", 150000)
    await _bank(db, "HDFC-02", "HDFC Bank", 150000)
    await _bank(db, "ICICI-01", "ICICI Bank", 200000)

    r = await _allocate(db, 250000, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert {l.candidate.account.bank_name for l in r.legs} == {"HDFC Bank"}
    assert round(r.total, 2) == 250000


@pytest.mark.asyncio
async def test_every_account_at_the_requested_bank_is_evaluated(db):
    """Four accounts at one bank: all four are measured, not the first match."""
    await _bank(db, "BOB-1", "Bank of Baroda", 40000)
    await _bank(db, "BOB-2", "Bank of Baroda", 40000)
    await _bank(db, "BOB-3", "Bank of Baroda", 40000)
    await _bank(db, "BOB-4", "Bank of Baroda", 40000)

    r = await _allocate(db, 160000, mode="IMPS", note="Use Bank of Baroda")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert len(r.legs) == 4
    assert round(r.total, 2) == 160000


# ── Boundaries ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exactly_the_combined_capacity_is_allowed(db):
    await _bank(db, "A", "HDFC Bank", 100000)
    await _bank(db, "B", "ICICI Bank", 200000)

    r = await _allocate(db, 300000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert round(r.total, 2) == 300000


@pytest.mark.asyncio
async def test_one_paisa_over_the_combined_capacity_allocates_nothing(db):
    """No part-payment, ever — a rupee short of coverable is not coverable."""
    await _bank(db, "A", "HDFC Bank", 100000)
    await _bank(db, "B", "ICICI Bank", 200000)

    r = await _allocate(db, 300000.01, mode="IMPS")

    assert r.outcome == wa.OUTCOME_NO_ACCOUNT
    assert r.legs == []
    assert r.failure_code == wa.FAIL_CAPACITY


@pytest.mark.asyncio
async def test_balance_can_block_an_allocation_the_limits_would_allow(db):
    """Generous daily limits, empty accounts: the money still has to be there."""
    await _bank(db, "A", "HDFC Bank", 500000, balance=50000)
    await _bank(db, "B", "ICICI Bank", 500000, balance=50000)

    r = await _allocate(db, 300000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_NO_ACCOUNT
    assert r.legs == []


# ── The refusal explains itself honestly ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_refusal_reports_combined_capacity_not_the_largest_single_limit(db):
    """The message that sent Admins to raise one limit.

    800,000 against accounts capped at 100,588 used to read "larger than every account's Highest
    Debit (the largest is 100,588)" — true, irrelevant, and misleading in an engine that splits.
    """
    await _bank(db, "A", "IDBI", 100588)
    await _bank(db, "B", "HDFC Bank", 90000)

    r = await _allocate(db, 800000, mode="IMPS")

    assert r.outcome == wa.OUTCOME_NO_ACCOUNT
    assert r.failure_code == wa.FAIL_CAPACITY
    assert "190,588" in r.reason, f"combined capacity must be stated: {r.reason}"
    assert "609,412" in r.reason, f"the shortfall must be stated: {r.reason}"
    assert "across all banks" in r.reason
    assert "largest is" not in r.reason, "must not blame the biggest single limit"
    assert r.detail["totalUsableCapacity"] == 190588
    assert r.detail["shortfall"] == 609412


# ── The record the Admin screen reads ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_allocation_record_names_every_account_and_its_share(db):
    await _bank(db, "HDFC-01", "HDFC Bank", 100000)
    await _bank(db, "ICICI-02", "ICICI Bank", 200000)
    tx = await _withdrawal(db, "1", 250000)

    r = await _allocate(db, 250000, mode="IMPS")
    await wa.record_allocation(db, r, transaction=tx, triggered_by="test")
    legs = await wa.write_legs(db, r, transaction=tx, allocated_by="test")

    assert len(legs) == 2
    assert round(sum(l.amount for l in legs), 2) == 250000
    assert {l.bank_name for l in legs} == {"HDFC Bank", "ICICI Bank"}
    assert [l.leg_no for l in legs] == [1, 2]


@pytest.mark.asyncio
async def test_admin_leg_serialization_carries_capacity_and_merchant_does_not(db):
    """The daily-capacity columns are the Admin table's whole point — and exactly what the
    merchant payload must never carry."""
    await _bank(db, "HDFC-01", "HDFC Bank", 100000)
    tx = await _withdrawal(db, "1", 40000)
    r = await _allocate(db, 40000, mode="IMPS")
    legs = await wa.write_legs(db, r, transaction=tx, allocated_by="test")

    admin = wa.serialize_leg(legs[0], mask=False, capacity=True)
    assert admin["highestDebit"] == 100000
    assert admin["remainingBefore"] == 100000
    assert admin["remainingCapacity"] == 60000, "what is left AFTER this leg"
    assert "availableBalance" in admin

    merchant = wa.serialize_leg(legs[0])
    for internal in ("highestDebit", "debitUsedToday", "remainingCapacity", "availableBalance"):
        assert internal not in merchant, f"{internal} must never reach a merchant"
    assert merchant["accountNumber"].startswith("•"), "merchant sees a masked number"


# ── Capacity is consumed once ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_second_withdrawal_cannot_reuse_the_capacity_a_split_already_holds(db):
    await _bank(db, "A", "HDFC Bank", 100000)
    await _bank(db, "B", "ICICI Bank", 200000)

    first = await _allocate(db, 300000, mode="IMPS")
    assert first.outcome == wa.OUTCOME_SPLIT
    tx1 = await _withdrawal(db, "1", 300000)
    await wa.write_legs(db, first, transaction=tx1, allocated_by="test")

    second = await _allocate(db, 1000, mode="IMPS")
    assert second.outcome == wa.OUTCOME_NO_ACCOUNT, "the whole capacity is already committed"


@pytest.mark.asyncio
async def test_same_account_preference_survives_but_never_bypasses_eligibility(db):
    """A "same account" request is honoured when the account is still eligible, and quietly
    dropped — never forced — when it is not."""
    await _bank(db, "A", "HDFC Bank", 500000)
    await _bank(db, "B", "ICICI Bank", 500000)
    prior = await _withdrawal(db, "P1", 10000, status=wa.TxStatus.COMPLETED)
    prior.payout_account_ref = "B"
    await db.flush()

    r = await _allocate(db, 50000, mode="IMPS", note="use the same account", member_id="MBR1")
    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert r.legs[0].ref == "B", "the previously used account is preferred"

    # Now put B beyond reach: the preference must give way, not override the limit.
    b = [c.account for c in r.candidates if c.ref == "B"][0]
    b.status = "INACTIVE"
    await db.flush()
    r2 = await _allocate(db, 50000, mode="IMPS", note="use the same account", member_id="MBR1")
    assert r2.outcome == wa.OUTCOME_ALLOCATED
    assert r2.legs[0].ref == "A"


@pytest.mark.asyncio
async def test_a_single_account_elsewhere_beats_splitting_inside_the_requested_bank(db):
    """The precedence between two rules that can disagree, pinned deliberately.

    "Use HDFC" with two 150,000 HDFC accounts could cover 250,000 as a split. One ICICI account
    could cover it alone. The engine takes the single account: preferring ONE account is the
    stronger rule, and a note is a preference rather than an instruction. Splitting a payout three
    ways to honour a hint would put money through more accounts than the platform needs.
    """
    await _bank(db, "HDFC-01", "HDFC Bank", 150000)
    await _bank(db, "HDFC-02", "HDFC Bank", 150000)
    await _bank(db, "ICICI-01", "ICICI Bank", 900000)

    r = await _allocate(db, 250000, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert [l.ref for l in r.legs] == ["ICICI-01"]
    assert r.requested_unavailable is True, "the unmet HDFC preference is still recorded"
