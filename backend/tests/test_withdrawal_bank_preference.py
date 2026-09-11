"""The merchant's requested bank has priority WHENEVER IT CAN SATISFY THE WITHDRAWAL.

The rule these tests pin, in full:

  1. Evaluate all eligible accounts at the requested bank first.
  2. One of them covers the whole amount        -> allocate 100% to that account.
  3. Several of them together cover it          -> allocate the whole amount across that bank only.
  4. Their combined capacity is short           -> spend ALL of it first, then add other banks for
                                                   the remainder only.
  5. Another bank must NEVER replace a preferred-bank allocation that would have worked.
  6. Another bank enters ONLY when the requested bank cannot satisfy the full amount.
  7. The legs must total the requested amount EXACTLY.
  8. Every other rule — balance, Highest Debit, payout mode, beneficiary, availability, locking,
     idempotency, accounting — stays mandatory. A preference is never permission to skip one.
  9. If no combination across the preferred bank AND every other eligible bank reaches the amount,
     nothing is allocated. Never a part-payment.

Two failures made this necessary, both reproduced below as ``test_regression_*``:

  * "Use HDFC" with HDFC 5L + 3L against a single 9L ICICI account paid ICICI the whole 8L. The
    single-account rule outranked the merchant's own instruction.
  * "Use HDFC" with only 1L at HDFC dropped HDFC entirely, because putting it first cost one more
    leg than the pure-capacity answer and the preference was treated as a tie-break.

Where NO bank is requested the single-account preference is unchanged — pinned here too, because
that is the property most easily broken by fixing the ones above.

Run from the backend directory:

    python -m pytest tests/test_withdrawal_bank_preference.py -v
"""
from __future__ import annotations

import pytest

from app.api.routes import transactions as txr
from app.services import withdrawal_allocation as wa

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures)
    db, safe_refs_and_no_cache, _account, _admin, _allocate, _create, _fund, _merchant,
    _withdrawal,
)

L = 100000.0          # one lakh, so the worked examples read as the merchant states them


async def _acc(db, ref: str, bank: str, capacity: float, *, balance: float | None = None):
    """An account whose USABLE capacity is ``capacity`` — funded far beyond it unless told
    otherwise, so the daily Highest Debit is what binds."""
    await _account(db, ref, bank=bank, debit=capacity)
    await _fund(db, "F" + ref, ref, capacity * 3 if balance is None else balance)


# ═══ The four worked examples: ₹8,00,000 with a note reading "Use HDFC" ══════════════════════════

@pytest.mark.asyncio
async def test_case_1_one_hdfc_account_covers_it_so_only_that_account_pays(db):
    """HDFC has one account with ₹8L of capacity -> HDFC-01 ₹8,00,000, and nothing else.

    ICICI and BOB are deliberately present, eligible and larger. Neither may appear.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 8 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 20 * L)
    await _acc(db, "BOB-01", "Bank of Baroda", 20 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_ALLOCATED, r.reason
    assert {l.ref: l.amount for l in r.legs} == {"HDFC-01": 800000.0}
    assert r.total == 800000.0
    assert r.requested_unavailable is False


@pytest.mark.asyncio
async def test_case_2_two_hdfc_accounts_cover_it_so_no_other_bank_is_used(db):
    """HDFC-01 ₹5L + HDFC-02 ₹3L -> both, totalling ₹8L. ICICI is not touched.

    This is the case that used to hand the whole ₹8L to ICICI, because one ICICI account could
    carry it alone and "prefer ONE account" was treated as the stronger rule.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _acc(db, "HDFC-02", "HDFC Bank", 3 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 9 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_SPLIT, r.reason
    assert {l.ref: l.amount for l in r.legs} == {"HDFC-01": 500000.0, "HDFC-02": 300000.0}
    assert {l.candidate.account.bank_name for l in r.legs} == {"HDFC Bank"}
    assert r.total == 800000.0
    assert r.requested_unavailable is False, "the preference was met in full"


@pytest.mark.asyncio
async def test_case_3_hdfc_is_short_so_other_banks_complete_the_remainder_only(db):
    """HDFC total ₹5L against an ₹8L withdrawal -> HDFC ₹5L, then ICICI ₹2L + BOB ₹1L.

    Other banks appear only because HDFC cannot finish the job, and only for the ₹3L it cannot
    cover. HDFC's own capacity is spent to the last rupee first.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 2 * L)
    await _acc(db, "BOB-01", "Bank of Baroda", 1 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_SPLIT, r.reason
    assert {l.ref: l.amount for l in r.legs} == {
        "HDFC-01": 500000.0, "ICICI-01": 200000.0, "BOB-01": 100000.0}
    assert r.legs[0].ref == "HDFC-01", "the requested bank is drawn on FIRST"
    assert r.total == 800000.0
    # The preference was only partly honourable, and the figures that explain that are recorded.
    assert r.requested_unavailable is True
    assert r.detail["requestedBankPartial"] == {
        "bank": "HDFC Bank", "usableCapacity": 500000.0, "shortfall": 300000.0}
    # The sentence printed under the Admin's allocation table must say the bank WAS used. Calling
    # this "unavailable — fallback applied", as it once did, contradicts the first row of the
    # table it sits beneath.
    assert "HDFC Bank covered ₹500,000.00 of ₹800,000.00" in r.reason, r.reason
    assert "unavailable" not in r.reason, r.reason


@pytest.mark.asyncio
async def test_case_4_no_combination_reaches_the_amount_so_nothing_is_allocated(db):
    """HDFC ₹5L plus every other eligible bank ₹2.5L = ₹7.5L against ₹8L -> rejected.

    ₹7,50,000 IS allocatable, which is exactly why this must not be allocated: a part-payment is
    worse than an exception. The refusal states the combined figure and the gap.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 1.5 * L)
    await _acc(db, "BOB-01", "Bank of Baroda", 1 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_NO_ACCOUNT
    assert r.legs == []
    assert r.failure_code == wa.FAIL_CAPACITY
    assert r.detail["totalUsableCapacity"] == 750000.0
    assert r.detail["shortfall"] == 50000.0
    assert "750,000" in r.reason and "50,000" in r.reason, r.reason


# ═══ The two regressions, stated as the bugs they were ══════════════════════════════════════════

@pytest.mark.asyncio
async def test_regression_a_single_foreign_account_never_replaces_a_sufficient_preferred_bank(db):
    """Rule 5. The requested bank can do the whole job; another bank must not take it.

    Tested across the three shapes that all used to fail the same way: the other bank being
    bigger, the requested bank needing two accounts, and needing three.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 3 * L)
    await _acc(db, "HDFC-02", "HDFC Bank", 3 * L)
    await _acc(db, "HDFC-03", "HDFC Bank", 2 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 50 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert {l.candidate.account.bank_name for l in r.legs} == {"HDFC Bank"}
    assert "ICICI-01" not in {l.ref for l in r.legs}
    assert r.total == 800000.0


@pytest.mark.asyncio
async def test_regression_preferred_capacity_is_used_even_when_it_costs_an_extra_leg(db):
    """Rule 4. HDFC ₹1L, ICICI ₹5L, BOB ₹3L against ₹8L.

    The pure-capacity answer is two legs (ICICI ₹5L + BOB ₹3L) and does not touch HDFC at all.
    Honouring the note costs a third leg — and is still the answer, because the merchant named a
    bank that has money and asked for it to be used. This is the case the old "keep the preference
    only if it needs no more accounts" tie-break silently discarded.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 1 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 5 * L)
    await _acc(db, "BOB-01", "Bank of Baroda", 3 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_SPLIT
    assert r.legs[0].ref == "HDFC-01" and r.legs[0].amount == 100000.0
    assert {l.ref: l.amount for l in r.legs} == {
        "HDFC-01": 100000.0, "ICICI-01": 500000.0, "BOB-01": 200000.0}
    assert r.total == 800000.0
    assert len(r.legs) == 3, "three legs, deliberately, rather than dropping the requested bank"


# ═══ What the preference must NOT change ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_no_bank_preference_one_account_still_beats_a_split(db):
    """The explicit carve-out: nothing above applies when the merchant named no bank.

    Same accounts as case 2 and no note — and the answer is the single ICICI account, not the
    HDFC pair.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _acc(db, "HDFC-02", "HDFC Bank", 3 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 9 * L)

    r = await _allocate(db, 8 * L, mode="IMPS")

    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert [l.ref for l in r.legs] == ["ICICI-01"]


@pytest.mark.asyncio
async def test_a_preference_never_lifts_a_daily_limit_or_a_balance(db):
    """Rule 8. The named bank has plenty of headroom and no money; it still cannot pay.

    An engine that honoured the note by relaxing a ceiling would move real money it does not have.
    HDFC contributes exactly its ₹50,000 balance and no more.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 20 * L, balance=50000.0)
    await _acc(db, "ICICI-01", "ICICI Bank", 20 * L)

    r = await _allocate(db, 8 * L, mode="IMPS", note="Use HDFC")

    assert r.total == 800000.0
    shares = {l.ref: l.amount for l in r.legs}
    assert shares["HDFC-01"] == 50000.0, "capped by the balance, not by the preference"
    assert shares["ICICI-01"] == 750000.0
    for leg in r.legs:
        assert leg.amount <= leg.candidate.remaining + 1e-9, "daily limit breached"
        assert leg.amount <= leg.candidate.balance + 1e-9, "balance breached"


@pytest.mark.asyncio
async def test_a_bank_with_no_eligible_account_falls_back_to_the_ordinary_rules(db):
    """The one case that is still an all-or-nothing miss: the named bank can pay NOTHING.

    With no capacity to place first there is nothing to honour, so the single-account rule applies
    unchanged and the unmet preference is recorded for the Admin.
    """
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _account(db, "HDFC-01X", bank="HDFC Bank", status="INACTIVE", debit=20 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 9 * L)
    # Exhaust HDFC-01's day, leaving the requested bank with no usable capacity at all.
    first = await _allocate(db, 5 * L, mode="IMPS")
    assert first.legs[0].ref == "HDFC-01"
    await wa.write_legs(db, first, transaction=await _withdrawal(db, "HOLD", 5 * L),
                        allocated_by="test")

    r = await _allocate(db, 4 * L, mode="IMPS", note="Use HDFC")

    assert r.outcome == wa.OUTCOME_ALLOCATED
    assert [l.ref for l in r.legs] == ["ICICI-01"]
    assert r.requested_unavailable is True
    assert r.detail["requestedBankUnavailable"] == "HDFC Bank"
    assert "requestedBankPartial" not in r.detail, "nothing at the bank could be placed"


# ═══ The Admin allocation table shows every bank, account and amount ════════════════════════════

@pytest.mark.asyncio
async def test_the_admin_allocation_table_names_every_bank_account_and_amount(db):
    """The Admin's payout screen is fed entirely by GET /transactions/{id}/payout-allocation, so
    what that endpoint returns IS what the table renders: one row per leg (bank, account,
    allocated amount, remaining capacity, status), a TOTAL, and the cross-bank badge.

    Case 3 is used deliberately — a three-bank split is the allocation an Admin most needs to see
    stated rather than inferred.
    """
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _acc(db, "HDFC-01", "HDFC Bank", 5 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 2 * L)
    await _acc(db, "BOB-01", "Bank of Baroda", 1 * L)

    created = await _create(db, merchant, 8 * L, notes="Use HDFC")
    out = await txr.get_payout_allocation(created["id"], db, admin)

    # One row per paying account, in allocation order, with the bank and the share on each.
    rows = [(l["bankName"], l["accountRef"], l["amount"]) for l in out["legs"]]
    assert rows == [
        ("HDFC Bank", "HDFC-01", 500000.0),
        ("ICICI Bank", "ICICI-01", 200000.0),
        ("Bank of Baroda", "BOB-01", 100000.0),
    ], rows
    # The table's own header figures.
    assert out["accountCount"] == 3
    assert out["banks"] == ["HDFC Bank", "ICICI Bank", "Bank of Baroda"]
    assert out["crossBank"] is True
    # TOTAL reconciles against the withdrawal — the property the table exists to make visible.
    assert out["allocatedTotal"] == 800000.0 == out["requestedAmount"]
    # And the reason line beneath the table agrees with the rows above it.
    assert "HDFC Bank covered" in out["decision"]["reason"], out["decision"]["reason"]
    # The Remaining Debit Capacity column is populated per row, and is what is left AFTER the leg.
    assert [l["remainingCapacity"] for l in out["legs"]] == [0.0, 0.0, 0.0]
    # The account number reaches the Admin unmasked; the merchant's copy of the same allocation
    # never carries a capacity figure.
    assert all(not l["accountNumber"].startswith("•") for l in out["legs"])
    assert "remainingCapacity" not in str(created["payoutLegs"])


@pytest.mark.asyncio
async def test_the_admin_table_shows_a_single_row_when_the_preferred_bank_covers_it(db):
    """The counterpart: no split, no cross-bank badge, and the one row is the requested bank."""
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _acc(db, "HDFC-01", "HDFC Bank", 8 * L)
    await _acc(db, "ICICI-01", "ICICI Bank", 20 * L)

    created = await _create(db, merchant, 8 * L, notes="Use HDFC")
    out = await txr.get_payout_allocation(created["id"], db, admin)

    assert [(l["bankName"], l["accountRef"], l["amount"]) for l in out["legs"]] == [
        ("HDFC Bank", "HDFC-01", 800000.0)]
    assert out["accountCount"] == 1
    assert out["crossBank"] is False
    assert out["allocatedTotal"] == 800000.0
