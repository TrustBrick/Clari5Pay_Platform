"""A withdrawal cannot reach COMPLETED without payout accounting.

Completing a withdrawal records that money left the platform. Doing that with no debit and no
ledger entry loses the record of a real payment, and before this guard it was reachable: a request
the engine could not place (NO_ELIGIBLE_ACCOUNT, no capacity, an invalid beneficiary) has no payout
legs, and "Mark as Done" with no payment method completed it silently, debiting nothing.

The line drawn here is between a withdrawal RAISED UNDER automatic allocation and one that
genuinely predates it. ``record_allocation`` journals every attempt — placements AND failures —
so any withdrawal the engine touched leaves a trace even when it ended with no legs. That trace is
what separates "must be accounted for" from the one legacy allowance, which is itself audited so a
completion without a ledger entry is always traceable to a deliberate decision.

Run from the backend directory:

    python -m pytest tests/test_markdone_payout_accounting.py -v
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import transactions as txr
from app.models.models import (
    AccountLedgerEntry, AuditLog, Transaction, TxStatus, WithdrawalPayoutLeg,
)
from app.schemas.schemas import CompleteRequest
from app.services import account_ledger as ledger
from app.services import withdrawal_allocation as wa

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures)
    db, safe_refs_and_no_cache, _account, _admin, _allocate, _ben, _fund, _withdrawal,
)


def _tid(tx: Transaction) -> str:
    """The id ``mark_done`` addresses a transaction by.

    It resolves by numeric DATABASE id, not by reference — and the funding deposits these fixtures
    create take the low ids, so passing the withdrawal's ref would quietly act on a deposit.
    """
    return f"TXN{tx.id:03d}"


async def _payout_entries(db, ref: str) -> list[AccountLedgerEntry]:
    return list((await db.execute(
        select(AccountLedgerEntry).where(
            AccountLedgerEntry.entry_type == ledger.WITHDRAWAL_PAYOUT,
            AccountLedgerEntry.transaction_ref == ref)
    )).scalars().all())


async def _unplaced_withdrawal(db, ref: str, amount: float) -> Transaction:
    """A withdrawal the engine tried and failed to place — journalled, but with no legs.

    This is what a real NO_ELIGIBLE_ACCOUNT row looks like: the allocation attempt is recorded, no
    account is assigned, and nothing is reserved.
    """
    tx = await _withdrawal(db, ref, amount, status=TxStatus.NO_ELIGIBLE_ACCOUNT)
    result = await _allocate(db, amount, mode="IMPS")
    assert result.outcome == wa.OUTCOME_NO_ACCOUNT, "fixture expects allocation to fail"
    await wa.record_allocation(db, result, transaction=tx, triggered_by="test")
    await db.flush()
    return tx


# ── 1. A new unplaced withdrawal cannot be completed with no payment method ─────────────────────

@pytest.mark.asyncio
async def test_unplaced_withdrawal_marked_done_without_a_method_is_rejected(db):
    """The gap this guard closes: no legs, no method, and the engine HAS seen this withdrawal."""
    await _account(db, "A", debit=10000)          # far too small to carry it
    await _fund(db, "F", "A", 5000)
    admin = await _admin(db)
    tx = await _unplaced_withdrawal(db, "1", 500000)

    with pytest.raises(HTTPException) as err:
        await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    assert err.value.status_code == 400
    assert "no payout account allocated" in err.value.detail

    refreshed = await db.get(Transaction, tx.id)
    assert refreshed.status == TxStatus.NO_ELIGIBLE_ACCOUNT, "it must not have been completed"
    assert await _payout_entries(db, "1") == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_three_ways_forward(db):
    """A refusal that does not say what to do next just moves the problem to a human."""
    await _account(db, "A", debit=10000)
    admin = await _admin(db)
    tx = await _unplaced_withdrawal(db, "1", 500000)

    with pytest.raises(HTTPException) as err:
        await txr.mark_done(_tid(tx), request=None, data=None, db=db, actor=admin)

    detail = err.value.detail
    assert "Retry the automatic allocation" in detail
    assert "choose a payout account" in detail
    assert "Manual / Offline" in detail


# ── 2. The same withdrawal paid manually completes and is accounted for ─────────────────────────

@pytest.mark.asyncio
async def test_unplaced_withdrawal_completes_via_a_manual_offline_payout(db):
    """The explicit escape valve: an offline payment, with its mandatory reference."""
    await _account(db, "A", debit=10000)
    admin = await _admin(db)
    tx = await _unplaced_withdrawal(db, "1", 500000)

    await txr.mark_done(
        _tid(tx), request=None,
        data=CompleteRequest(paymentMethod="MANUAL", manualReference="NEFT-OFFLINE-991",
                             payoutRemarks="paid at the branch"),
        db=db, actor=admin)

    refreshed = await db.get(Transaction, tx.id)
    assert refreshed.status == TxStatus.COMPLETED
    assert refreshed.payout_payment_method == "MANUAL"
    assert refreshed.payout_manual_reference == "NEFT-OFFLINE-991"
    # An offline payment comes out of no managed account, so the debit carries no account.
    entries = await _payout_entries(db, "1")
    assert len(entries) == 1
    assert entries[0].direction == ledger.DEBIT
    assert entries[0].amount == 500000
    assert entries[0].account_ref is None
    assert entries[0].payment_method == "MANUAL"


@pytest.mark.asyncio
async def test_a_manual_payout_still_requires_its_payment_reference(db):
    """The existing mandatory reference is not weakened by being the way out of a failed
    allocation — an offline payment with nothing to trace it by is not accounting."""
    await _account(db, "A", debit=10000)
    admin = await _admin(db)
    tx = await _unplaced_withdrawal(db, "1", 500000)

    with pytest.raises(HTTPException) as err:
        await txr.mark_done(_tid(tx), request=None,
                            data=CompleteRequest(paymentMethod="MANUAL", manualReference="  "),
                            db=db, actor=admin)
    assert err.value.status_code == 400
    assert "Manual Payment Reference is required" in err.value.detail
    assert await _payout_entries(db, "1") == []


# ── 3. An allocated withdrawal debits every leg ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_allocated_withdrawal_debits_its_leg_on_completion(db):
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 45000, status=TxStatus.ACCOUNT_SUBMITTED)
    result = await _allocate(db, 45000, mode="IMPS")
    await wa.record_allocation(db, result, transaction=tx, triggered_by="test")
    await wa.write_legs(db, result, transaction=tx, allocated_by="test")

    # No payment method supplied: the allocation is what pays it.
    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    refreshed = await db.get(Transaction, tx.id)
    assert refreshed.status == TxStatus.COMPLETED
    assert refreshed.payout_account_ref == "A"
    entries = await _payout_entries(db, "1")
    assert len(entries) == 1
    assert entries[0].amount == 45000
    assert entries[0].account_ref == "A"
    assert round(await ledger.account_balance(db, "A"), 2) == 455000


@pytest.mark.asyncio
async def test_a_split_withdrawal_debits_every_leg_atomically(db):
    """Three paying accounts, three ledger entries, and the legs sum to the withdrawal exactly."""
    await _account(db, "A", debit=70000)
    await _fund(db, "FA", "A", 80000)
    await _account(db, "B", debit=50000)
    await _fund(db, "FB", "B", 60000)
    await _account(db, "C", debit=40000)
    await _fund(db, "FC", "C", 200000)
    admin = await _admin(db)

    tx = await _withdrawal(db, "1", 150000, status=TxStatus.ACCOUNT_SUBMITTED)
    result = await _allocate(db, 150000, mode="IMPS")
    assert result.outcome == wa.OUTCOME_SPLIT
    await wa.record_allocation(db, result, transaction=tx, triggered_by="test")
    await wa.write_legs(db, result, transaction=tx, allocated_by="test")

    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    refreshed = await db.get(Transaction, tx.id)
    assert refreshed.status == TxStatus.COMPLETED
    # A split cannot be expressed by the single column, so the legs are the record.
    assert refreshed.payout_account_ref is None

    entries = await _payout_entries(db, "1")
    assert len(entries) == 3
    assert round(sum(e.amount for e in entries), 2) == 150000
    assert {e.account_ref for e in entries} == {"A", "B", "C"}
    assert {e.leg_no for e in entries} == {1, 2, 3}

    legs = (await db.execute(
        select(WithdrawalPayoutLeg).where(WithdrawalPayoutLeg.transaction_ref == "1")
    )).scalars().all()
    assert all(l.status == ledger.LEG_PAID for l in legs)


# ── 4. Idempotency ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_completing_twice_never_debits_twice(db):
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 45000, status=TxStatus.ACCOUNT_SUBMITTED)
    result = await _allocate(db, 45000, mode="IMPS")
    await wa.record_allocation(db, result, transaction=tx, triggered_by="test")
    await wa.write_legs(db, result, transaction=tx, allocated_by="test")

    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)
    balance_after_first = round(await ledger.account_balance(db, "A"), 2)

    # The second click. The already-COMPLETED short-circuit is what makes it harmless.
    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    assert len(await _payout_entries(db, "1")) == 1
    assert round(await ledger.account_balance(db, "A"), 2) == balance_after_first


@pytest.mark.asyncio
async def test_completing_a_manual_payout_twice_never_debits_twice(db):
    await _account(db, "A", debit=10000)
    admin = await _admin(db)
    tx = await _unplaced_withdrawal(db, "1", 500000)
    payload = CompleteRequest(paymentMethod="MANUAL", manualReference="OFFLINE-1")

    await txr.mark_done(_tid(tx), request=None, data=payload, db=db, actor=admin)
    await txr.mark_done(_tid(tx), request=None, data=payload, db=db, actor=admin)

    assert len(await _payout_entries(db, "1")) == 1


# ── 5. Legacy withdrawals stay completable, and are explicitly identified ──────────────────────

@pytest.mark.asyncio
async def test_a_genuine_legacy_withdrawal_still_completes(db):
    """No journal row and no leg: the engine never saw it, so it predates the feature.

    Refusing these would strand rows nobody can fix — the accounting was never captured for them
    and cannot be reconstructed now.
    """
    await _account(db, "A", debit=100000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 45000, status=TxStatus.ACCOUNT_REQUESTED)

    assert await wa.engine_has_seen(db, "1") is False

    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    refreshed = await db.get(Transaction, tx.id)
    assert refreshed.status == TxStatus.COMPLETED
    assert await _payout_entries(db, "1") == []


@pytest.mark.asyncio
async def test_a_legacy_completion_without_accounting_is_audited(db):
    """The ONLY route to a completed withdrawal with no ledger entry, so it must be traceable."""
    await _account(db, "A", debit=100000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 45000, status=TxStatus.ACCOUNT_REQUESTED)

    await txr.mark_done(_tid(tx), request=None, data=CompleteRequest(), db=db, actor=admin)

    audits = (await db.execute(
        select(AuditLog).where(AuditLog.action_type == "WITHDRAWAL_COMPLETED_WITHOUT_PAYOUT",
                               AuditLog.entity_id == "1")
    )).scalars().all()
    assert len(audits) == 1
    assert "raised before automatic payout allocation" in (audits[0].reason or "")


@pytest.mark.asyncio
async def test_a_legacy_withdrawal_paid_from_an_account_is_still_accounted_for(db):
    """The legacy allowance is only for a completion that names no payment at all."""
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 45000, status=TxStatus.ACCOUNT_REQUESTED)

    await txr.mark_done(
        _tid(tx), request=None,
        data=CompleteRequest(paymentMethod="BANK", payoutAccountRef="A"), db=db, actor=admin)

    entries = await _payout_entries(db, "1")
    assert len(entries) == 1 and entries[0].account_ref == "A"


# ── The discriminator itself ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_allocation_still_counts_as_seen_by_the_engine(db):
    """The load-bearing property: a FAILED allocation leaves a trace.

    If it did not, a NO_ELIGIBLE_ACCOUNT withdrawal would be indistinguishable from a legacy row
    and would take the legacy allowance — which is exactly the gap being closed.
    """
    await _account(db, "A", debit=10000)
    assert await wa.engine_has_seen(db, "1") is False
    tx = await _unplaced_withdrawal(db, "1", 500000)
    assert await wa.engine_has_seen(db, "1") is True


@pytest.mark.asyncio
async def test_a_withdrawal_whose_legs_were_released_still_counts_as_seen(db):
    """Releasing an allocation must not hand a withdrawal back the legacy allowance."""
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000)
    tx = await _withdrawal(db, "1", 45000)
    result = await _allocate(db, 45000, mode="IMPS")
    await wa.write_legs(db, result, transaction=tx, allocated_by="test")
    await wa.release_legs(db, "1", reason=wa.RELEASE_REALLOCATED)

    assert await wa.live_legs(db, "1") == []
    assert await wa.engine_has_seen(db, "1") is True
