"""The FINAL limit semantics: per-transaction vs cumulative daily, on both sides.

This suite pins the one distinction the 2026-09-23 correction exists to make and that a future
change must never blur again:

  • **Highest Credit / Highest Debit are PER-TRANSACTION caps.** The most ONE deposit / ONE
    withdrawal may be. A ₹75,000 request passes a ₹75,000 cap and a ₹75,000.01 request fails it,
    whatever capacity the day has left. Earlier transactions do NOT reduce it.
  • **Bank Daily Credit / Debit Limit are CUMULATIVE daily ceilings.** The most an account may
    RECEIVE / SEND in TOTAL across one IST calendar day. Reaching it exactly is allowed; a single
    paisa over is not.
  • **A transaction is eligible only when BOTH pass**, and the two are enforced independently.
  • **Manual Account-Management credit/debit consumes the same daily bank capacity** — it never
    bypasses the daily limit, and there is no second counter.
  • **The daily total resets with the IST calendar day**; historical rows are never touched.

Numbers follow the brief's own worked examples (§7 A-D, §17 cases 1-22). The engines are exercised
directly (``allocate_deposit_account`` / ``allocate_withdrawal_accounts``) with per-transaction and
daily limits set APART, so a test that means to exercise one ceiling cannot accidentally trip the
other.

Run from the backend directory:

    python -m pytest tests/test_limit_semantics.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.models import AccountLedgerEntry, DepositAllocation, Transaction, TxStatus, TxType
from app.services import account_ledger as ledger
from app.services import deposit_allocation as alloc
from app.services import withdrawal_allocation as wa

# Fixtures + builders reused from the two engine suites so setup here is identical to theirs.
from tests.test_deposit_allocation import (  # noqa: F401
    db, safe_refs_and_no_cache, _account as _dep_account, _allocate as _dep_allocate,
    _deposit,
)
from tests.test_withdrawal_allocation import (  # noqa: F401
    _account as _wd_account, _allocate as _wd_allocate, _fund,
)


# ── A manual Account-Management adjustment, dated to an IST day ──────────────────────────────────

async def _manual(db, ref: str, amount: float, direction: str, *, on: date | None = None,
                  seq: int = 1) -> AccountLedgerEntry:
    """A manual CREDIT/DEBIT ledger entry on ``ref``, timed inside ``on``'s IST day.

    ``post_entry`` draws its reference from a Postgres sequence that the test schema does not
    create, so the row is built directly with an explicit ``entry_ref`` — the same technique the
    ledger tests use. ``created_at`` is placed at noon UTC of the target IST day so it lands
    squarely inside the window ``credit_used_today`` / ``debit_used_today`` compute for that day.
    """
    day = on or alloc.ist_today()
    start_utc, _ = alloc.ist_day_utc_bounds(day)
    entry = AccountLedgerEntry(
        entry_ref=f"ADJ-T{seq}", entry_type=ledger.MANUAL_ADJUSTMENT, direction=direction,
        amount=round(amount, 2), account_ref=ref, balance_before=0.0, balance_after=0.0,
        reason="Reconciliation Correction",
        created_at=start_utc + timedelta(hours=6), created_at_ist=alloc.ist_stamp(),
    )
    db.add(entry)
    await db.flush()
    return entry


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# DEPOSIT — §17 cases 1-12
# ═══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_1_deposit_equal_to_highest_credit_passes(db):
    """₹75,000 with Highest Credit ₹75,000 → PASS (equality is allowed)."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    r = await _dep_allocate(db, 75000)
    assert r.allocated is True


@pytest.mark.asyncio
async def test_2_deposit_one_paisa_over_highest_credit_fails(db):
    """₹75,000.01 with Highest Credit ₹75,000 → FAIL, even with the whole day's capacity free."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    r = await _dep_allocate(db, 75000.01)
    assert r.allocated is False
    assert r.candidates[0].reject_reason == alloc.REJECT_OVER_PER_TXN


@pytest.mark.asyncio
async def test_3_two_deposits_each_within_the_cap_both_pass(db):
    """§7 Example A (partial): ₹70,000 then ₹20,000, cap ₹75,000, daily ₹2,00,000 → both PASS.

    The second must NOT be rejected merely because the first was ₹70,000 — the per-transaction cap
    is not consumed by earlier transactions.
    """
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    assert (await _dep_allocate(db, 70000)).allocated is True
    await _deposit(db, "1", 70000, "ACC1")                # first one now live
    assert (await _dep_allocate(db, 20000)).allocated is True


@pytest.mark.asyncio
async def test_4_three_deposits_within_daily_all_pass(db):
    """§7 Example A in full: ₹70,000 + ₹20,000 + ₹30,000 = ₹1,20,000 under a ₹2,00,000 daily limit,
    every one within the ₹75,000 per-transaction cap → all three PASS."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    assert (await _dep_allocate(db, 70000)).allocated is True
    await _deposit(db, "1", 70000, "ACC1")
    assert (await _dep_allocate(db, 20000)).allocated is True
    await _deposit(db, "2", 20000, "ACC1")
    assert (await _dep_allocate(db, 30000)).allocated is True


@pytest.mark.asyncio
async def test_5_daily_total_reaching_exactly_the_limit_passes(db):
    """§17 case 5 / §7 Example D: ₹1,50,000 used, new ₹50,000, daily ₹2,00,000 → exactly at the
    limit → PASS."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "1", 75000, "ACC1")
    await _deposit(db, "2", 75000, "ACC1")                # ₹1,50,000 used today
    assert (await _dep_allocate(db, 50000)).allocated is True


@pytest.mark.asyncio
async def test_6_daily_total_one_paisa_over_the_limit_fails(db):
    """§17 case 6: ₹1,50,000 used, new ₹50,000.01, daily ₹2,00,000 → over by ₹0.01 → FAIL."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "1", 75000, "ACC1")
    await _deposit(db, "2", 75000, "ACC1")
    r = await _dep_allocate(db, 50000.01)
    assert r.allocated is False
    assert r.candidates[0].reject_reason == alloc.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_7_highest_credit_passes_but_daily_limit_fails(db):
    """§7 Example C / §17 case 7: ₹60,000 ≤ ₹75,000 cap, but ₹1,50,000 + ₹60,000 = ₹2,10,000 over
    the ₹2,00,000 daily limit → FAIL on the daily ceiling, not the per-transaction cap."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "1", 75000, "ACC1")
    await _deposit(db, "2", 75000, "ACC1")                # ₹1,50,000 used today
    r = await _dep_allocate(db, 60000)
    assert r.allocated is False
    assert r.candidates[0].reject_reason == alloc.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_8_daily_limit_passes_but_highest_credit_fails(db):
    """§17 case 8: the whole day's ₹2,00,000 is free, but ₹90,000 exceeds the ₹75,000
    per-transaction cap → FAIL on the per-transaction cap, not the daily ceiling."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    r = await _dep_allocate(db, 90000)
    assert r.allocated is False
    assert r.candidates[0].reject_reason == alloc.REJECT_OVER_PER_TXN


@pytest.mark.asyncio
async def test_9_manual_credit_contributes_to_daily_usage(db):
    """§8 / §17 case 9: a manual CREDIT consumes daily bank capacity like any deposit.

    ₹1,50,000 of the ₹2,00,000 daily limit is a manual credit; a new ₹60,000 deposit then exceeds
    the daily limit and is refused, exactly as if the ₹1,50,000 had arrived as deposits.
    """
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _manual(db, "ACC1", 150000, ledger.CREDIT)
    used = await alloc.credit_used_today(db, ["ACC1"])
    assert used["ACC1"] == 150000.0
    r = await _dep_allocate(db, 60000)
    assert r.allocated is False
    assert r.candidates[0].reject_reason == alloc.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_10_automated_plus_manual_credit_cannot_exceed_daily(db):
    """§8 example / §17 case 10: automated ₹1,20,000 + manual ₹50,000 = ₹1,70,000 leaves ₹30,000;
    a new ₹40,000 deposit is refused (₹2,10,000 > ₹2,00,000), while ₹30,000 is allowed exactly."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    await _deposit(db, "1", 70000, "ACC1")
    await _deposit(db, "2", 50000, "ACC1")               # automated ₹1,20,000
    await _manual(db, "ACC1", 50000, ledger.CREDIT)      # manual ₹50,000 → ₹1,70,000 used
    assert (await alloc.credit_used_today(db, ["ACC1"]))["ACC1"] == 170000.0
    assert (await _dep_allocate(db, 40000)).allocated is False
    assert (await _dep_allocate(db, 30000)).allocated is True


@pytest.mark.asyncio
async def test_11_new_calendar_day_provides_fresh_capacity(db):
    """§10 / §17 case 11: yesterday's ₹2,00,000 does not reduce today's capacity."""
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    yesterday = alloc.ist_today() - timedelta(days=1)
    await _deposit(db, "1", 75000, "ACC1", on=yesterday)
    await _deposit(db, "2", 75000, "ACC1", on=yesterday)
    await _deposit(db, "3", 50000, "ACC1", on=yesterday)  # yesterday fully used (₹2,00,000)

    # An account with no activity today is simply absent from the map (read as 0.0) — today is fresh.
    assert (await alloc.credit_used_today(db, ["ACC1"])).get("ACC1", 0.0) == 0.0
    assert (await _dep_allocate(db, 50000)).allocated is True


@pytest.mark.asyncio
async def test_12_capacity_is_consumed_at_allocation_so_concurrent_requests_cannot_both_pass(db):
    """§14 / §17 case 12: the daily count includes LIVE allocated deposits, not just completed ones,
    which is what stops two near-simultaneous requests from spending the same headroom.

    A deposit sitting in ACCOUNT_SUBMITTED (allocated, unpaid) already consumes capacity, so once
    ₹1,50,000 is committed the next ₹60,000 is refused even though nothing has been marked complete.
    """
    await _dep_account(db, "ACC1", name="a", credit=75000.0, daily_credit=200000.0)
    # A live, not-yet-completed allocation holds its capacity.
    await _deposit(db, "1", 75000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)
    await _deposit(db, "2", 75000, "ACC1", status=TxStatus.ACCOUNT_SUBMITTED)
    assert (await alloc.credit_used_today(db, ["ACC1"]))["ACC1"] == 150000.0
    assert (await _dep_allocate(db, 60000)).allocated is False
    # A rejected/cancelled one releases it again.
    await _deposit(db, "3", 75000, "ACC1", status=TxStatus.CANCELLED)
    assert (await alloc.credit_used_today(db, ["ACC1"]))["ACC1"] == 150000.0


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# WITHDRAWAL — §17 cases 13-22
# ═══════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_13_withdrawal_equal_to_highest_debit_passes(db):
    """₹75,000 with Highest Debit ₹75,000 → PASS."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    assert (await _wd_allocate(db, 75000)).allocated is True


@pytest.mark.asyncio
async def test_14_withdrawal_one_paisa_over_highest_debit_fails(db):
    """₹75,000.01 with Highest Debit ₹75,000 → FAIL on the per-transaction cap.

    A single account cannot carry it, and with only one account there is nothing to split across,
    so the whole allocation fails."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    r = await _wd_allocate(db, 75000.01)
    assert r.allocated is False
    assert {c.ref: c.reject_reason for c in r.candidates}["ACC1"] == wa.REJECT_OVER_PER_TXN


@pytest.mark.asyncio
async def test_15_multiple_withdrawals_each_below_the_cap_pass_within_daily(db):
    """§17 case 15: ₹70,000 then ₹20,000, cap ₹75,000, daily ₹2,00,000 → both PASS; the cap is not
    consumed by the earlier payout."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    r1 = await _wd_allocate(db, 70000)
    assert r1.allocated is True
    await wa.write_legs(db, r1, transaction=await _wd_txn(db, "W1", 70000))  # first leg now live
    assert (await _wd_allocate(db, 20000)).allocated is True


@pytest.mark.asyncio
async def test_16_daily_debit_reaching_exactly_the_limit_passes(db):
    """§17 case 16: ₹1,50,000 already paid today, new ₹50,000, daily ₹2,00,000 → exactly at the
    limit → PASS."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    await _paid_leg(db, "ACC1", 75000, "W1")
    await _paid_leg(db, "ACC1", 75000, "W2")             # ₹1,50,000 debited today
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 150000.0
    assert (await _wd_allocate(db, 50000)).allocated is True


@pytest.mark.asyncio
async def test_17_daily_debit_over_the_limit_fails(db):
    """§17 case 17: ₹1,50,000 used, new ₹50,000.01 → over the ₹2,00,000 daily limit → FAIL."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    await _paid_leg(db, "ACC1", 75000, "W1")
    await _paid_leg(db, "ACC1", 75000, "W2")
    r = await _wd_allocate(db, 50000.01)
    assert r.allocated is False
    assert {c.ref: c.reject_reason for c in r.candidates}["ACC1"] == wa.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_18_highest_debit_passes_but_daily_limit_fails(db):
    """§17 case 18: ₹60,000 ≤ ₹75,000 cap, but ₹1,50,000 + ₹60,000 over the ₹2,00,000 daily → FAIL
    on the daily ceiling."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    await _paid_leg(db, "ACC1", 75000, "W1")
    await _paid_leg(db, "ACC1", 75000, "W2")
    r = await _wd_allocate(db, 60000)
    assert r.allocated is False
    assert {c.ref: c.reject_reason for c in r.candidates}["ACC1"] == wa.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_19_daily_limit_passes_but_highest_debit_fails(db):
    """§17 case 19: the whole day's ₹2,00,000 is free, but ₹90,000 exceeds the ₹75,000
    per-transaction cap. One account, so nothing to split → FAIL on the per-transaction cap."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    r = await _wd_allocate(db, 90000)
    assert r.allocated is False
    assert {c.ref: c.reject_reason for c in r.candidates}["ACC1"] == wa.REJECT_OVER_PER_TXN


@pytest.mark.asyncio
async def test_20_manual_debit_contributes_to_daily_debit_usage(db):
    """§16 / §17 case 20: a manual DEBIT consumes daily debit capacity like a payout."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    await _manual(db, "ACC1", 150000, ledger.DEBIT)
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 150000.0
    r = await _wd_allocate(db, 60000)
    assert r.allocated is False
    assert {c.ref: c.reject_reason for c in r.candidates}["ACC1"] == wa.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_21_automated_plus_manual_debit_cannot_exceed_daily(db):
    """§16 / §17 case 21: automated ₹1,20,000 + manual ₹50,000 = ₹1,70,000; a new ₹40,000 payout is
    refused (₹2,10,000 > ₹2,00,000), while ₹30,000 is allowed exactly at the limit."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    await _paid_leg(db, "ACC1", 70000, "W1")
    await _paid_leg(db, "ACC1", 50000, "W2")            # automated ₹1,20,000
    await _manual(db, "ACC1", 50000, ledger.DEBIT)      # manual ₹50,000 → ₹1,70,000 used
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 170000.0
    assert (await _wd_allocate(db, 40000)).allocated is False
    assert (await _wd_allocate(db, 30000)).allocated is True


@pytest.mark.asyncio
async def test_22_capacity_is_consumed_at_allocation_so_concurrent_payouts_cannot_both_pass(db):
    """§14 / §17 case 22: an ALLOCATED-but-unpaid leg already consumes daily debit capacity, so a
    second request cannot spend the same headroom before the first is paid."""
    await _wd_account(db, "ACC1", debit=75000.0, daily_debit=200000.0)
    await _fund(db, "F1", "ACC1", 1000000.0)
    # Two live allocated (unpaid) legs hold ₹1,50,000 of the day's capacity.
    await _allocated_leg(db, "ACC1", 75000, "W1")
    await _allocated_leg(db, "ACC1", 75000, "W2")
    assert (await wa.debit_used_today(db, ["ACC1"]))["ACC1"] == 150000.0
    assert (await _wd_allocate(db, 60000)).allocated is False


# ── Withdrawal-side helpers (payout legs / a withdrawal row) ─────────────────────────────────────

async def _wd_txn(db, ref: str, amount: float) -> Transaction:
    tx = Transaction(
        ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=TxStatus.MANAGER_REVIEW,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=wa.ist_today(), tx_time="10:00:00",
        member_id="MBR1", created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _leg(db, ref: str, amount: float, txn_ref: str, status: str, *, seq: int) -> None:
    from app.models.models import WithdrawalPayoutLeg
    db.add(WithdrawalPayoutLeg(
        transaction_ref=txn_ref, transaction_id=None, leg_no=1, account_ref=ref, amount=amount,
        status=status, leg_date=wa.ist_today(), created_at=datetime.utcnow(),
    ))
    await db.flush()


async def _paid_leg(db, ref: str, amount: float, txn_ref: str) -> None:
    """A PAID leg — money that has left the account today, counted against the daily debit limit."""
    await _leg(db, ref, amount, txn_ref, ledger.LEG_PAID, seq=1)


async def _allocated_leg(db, ref: str, amount: float, txn_ref: str) -> None:
    """An ALLOCATED (unpaid) leg — capacity held from the moment of allocation."""
    await _leg(db, ref, amount, txn_ref, ledger.LEG_ALLOCATED, seq=1)
