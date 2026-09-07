"""Independent probe of the two worked examples in the feature specification (§38, §39).

These are not derived from the engine's own test suite — they encode the spec's stated inputs and
assert the spec's stated ANSWER, so they fail if the engine is merely self-consistent.
"""
from __future__ import annotations

import pytest

from app.services import withdrawal_allocation as wa

from tests.test_withdrawal_allocation import (  # noqa: F401  (db / safe_refs fixtures)
    db, safe_refs_and_no_cache, _account, _allocate, _ben, _fund,
)


@pytest.mark.asyncio
async def test_spec_38_nearest_capacity_bank_of_baroda(db):
    """§38 — ₹45,000, note "Use Bank of Baroda", IMPS, four BOB accounts.

    BOB-1 unavailable, BOB-2 only ₹30,000 available, BOB-3 remaining ₹50,000 / available ₹80,000,
    BOB-4 remaining ₹1,80,000 / available ₹3,00,000.  The spec's answer is BOB-3 — the closest
    valid capacity above ₹45,000 — NOT the far larger BOB-4.
    """
    await _account(db, "BOB1", bank="Bank of Baroda", status="INACTIVE", debit=100000)
    await _account(db, "BOB2", bank="Bank of Baroda", debit=100000)
    await _fund(db, "F2", "BOB2", 30000)                      # available ₹30,000 — too little
    await _account(db, "BOB3", bank="Bank of Baroda", debit=100000)
    # Funded 1,30,000 of which 50,000 is already committed today -> available 80,000.
    await _fund(db, "F3", "BOB3", 130000)
    await _account(db, "BOB4", bank="Bank of Baroda", debit=200000)
    # Funded 3,20,000 of which 20,000 is already committed today -> available 3,00,000.
    await _fund(db, "F4", "BOB4", 320000)

    # BOB-3 has already paid out ₹50,000 today → remaining ₹50,000. BOB-4 has paid ₹20,000
    # → remaining ₹1,80,000.
    await _spend(db, "BOB3", 50000)
    await _spend(db, "BOB4", 20000)

    result = await _allocate(db, 45000, mode="IMPS", note="Use Bank of Baroda")

    assert result.outcome == wa.OUTCOME_ALLOCATED, result.failure_message
    assert len(result.legs) == 1
    assert result.legs[0].ref == "BOB3", (
        f"spec says BOB-3 (nearest capacity ₹50,000); engine chose {result.legs[0].ref}")
    assert result.legs[0].amount == 45000


@pytest.mark.asyncio
async def test_spec_39_multi_account_split(db):
    """§39 — ₹1,50,000 across A(₹70,000) / B(₹50,000) / C(₹40,000) capacity.

    No single account can carry it. The spec's answer is A ₹70,000 + B ₹50,000 + C ₹30,000,
    summing to EXACTLY ₹1,50,000, with every leg inside its own account's two ceilings.
    """
    await _account(db, "A", debit=70000)
    await _fund(db, "FA", "A", 80000)                          # available ₹80,000
    await _account(db, "B", debit=50000)
    await _fund(db, "FB", "B", 60000)                          # available ₹60,000
    await _account(db, "C", debit=40000)
    await _fund(db, "FC", "C", 200000)                         # available ₹2,00,000

    result = await _allocate(db, 150000, mode="IMPS")

    assert result.outcome == wa.OUTCOME_SPLIT, result.failure_message
    shares = {l.ref: l.amount for l in result.legs}
    assert shares == {"A": 70000, "B": 50000, "C": 30000}, shares
    assert round(sum(shares.values()), 2) == 150000.00


@pytest.mark.asyncio
async def test_spec_18_no_partial_completion(db):
    """§18 — ₹2,00,000 requested against ₹1,65,000 of total capacity allocates NOTHING."""
    await _account(db, "A", debit=70000)
    await _fund(db, "FA", "A", 80000)
    await _account(db, "B", debit=55000)
    await _fund(db, "FB", "B", 60000)
    await _account(db, "C", debit=40000)
    await _fund(db, "FC", "C", 200000)                          # 70+55+40 = ₹1,65,000

    result = await _allocate(db, 200000, mode="IMPS")

    assert result.outcome == wa.OUTCOME_NO_ACCOUNT
    assert result.legs == []
    assert result.failure_code == wa.FAIL_CAPACITY


@pytest.mark.asyncio
async def test_spec_10_boundary_exact_and_one_rupee_over(db):
    """§10 — ₹1,00,000 ceiling, ₹70,000 used: ₹30,000 allocates, ₹30,001 does not."""
    await _account(db, "A", debit=100000)
    await _fund(db, "FA", "A", 500000)
    await _spend(db, "A", 70000)

    ok = await _allocate(db, 30000, mode="IMPS")
    assert ok.outcome == wa.OUTCOME_ALLOCATED and ok.legs[0].amount == 30000

    over = await _allocate(db, 30001, mode="IMPS")
    assert over.outcome == wa.OUTCOME_NO_ACCOUNT, "₹30,001 would breach the ₹1,00,000 ceiling"


# ── helper ─────────────────────────────────────────────────────────────────────────────────────

async def _spend(db, account_ref: str, amount: float) -> None:
    """Consume `amount` of today's debit capacity on an account via a live allocated leg."""
    from app.models.models import WithdrawalPayoutLeg
    from app.services import account_ledger as ledger
    db.add(WithdrawalPayoutLeg(
        transaction_ref=f"SPENT-{account_ref}-{int(amount)}", account_ref=account_ref,
        leg_no=1, amount=amount, status=ledger.LEG_ALLOCATED, leg_date=wa.ist_today(),
        transaction_mode="IMPS",
    ))
    await db.flush()
