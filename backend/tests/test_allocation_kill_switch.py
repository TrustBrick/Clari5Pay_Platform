"""Tests for the allocation engine's kill switch.

Every other allocation suite asks "does the engine decide well?". This one asks the question that
comes first in a rollout: **can this code sit in an environment that is not ready for it?**

Production carries accounts with no configured daily debit ceiling and debits that predate the
ledger, so an engine turned loose there would refuse withdrawals it cannot measure and park
deposits in NO_ELIGIBLE_ACCOUNT — a status no merchant on that platform has ever been shown. The
switch is what lets the code ship anyway, and these tests pin the three properties that make that
safe:

  1. **OFF is the default, and it is hardcoded.** A missing variable, a truncated ``.env``, a fresh
     container or a rollback must all land on "off". Only the exact word "on" starts the engine —
     "true", "1", "yes" and a typo are all *not* "on", so every way of getting this wrong fails
     closed.
  2. **OFF means the engine never runs**, not that its answer is discarded. Running it and then
     ignoring the result would still move a request into NO_ELIGIBLE_ACCOUNT on the way. What the
     platform does with the switch off is exactly what it did before allocation existed: a deposit
     waits in ACCOUNT_REQUESTED for an Admin, and a withdrawal carries no payout account.
  3. **SHADOW journals a decision and applies none of it.** The engine runs so its judgement can be
     compared against an Admin's, but no account is assigned, no payout leg is written, no status
     moves — and, critically, **no capacity is consumed**, because capacity is measured from real
     assignments and real legs and never from the journal. A shadow run cannot starve the live
     manual workflow it is being compared against.

Run from the backend directory:

    python -m pytest tests/test_allocation_kill_switch.py -v
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import transactions as txr
from app.core.config import Settings, settings
from app.models.models import (
    DepositAllocation, TxStatus, WithdrawalAllocation, WithdrawalPayoutLeg,
)
from app.schemas.schemas import RemarkRequest

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures + withdrawal builders)
    db, safe_refs_and_no_cache, _account, _admin, _create, _fund, _manager, _merchant,
)
from tests.test_deposit_allocation import (
    _account as _dep_account, _merchant as _dep_merchant, _payload as _dep_payload,
)

# NOTE: no ``pytestmark = pytest.mark.usefixtures("allocation_engine_on")`` here, deliberately.
# This is the one module that owns the switch, so every test states the mode it is testing.


def _mode(monkeypatch, value: str) -> None:
    """Put the platform in one of the three modes for the duration of one test."""
    monkeypatch.setattr(settings, "ALLOCATION_ENGINE_MODE", value)


async def _rows(db: AsyncSession, model) -> list:
    return (await db.execute(select(model))).scalars().all()


# ═══ 1 — the default, and the ways of getting it wrong ══════════════════════════════════════════

def test_the_engine_is_off_unless_someone_deliberately_switches_it_on():
    """The default lives on the FIELD, not in a ``.env`` — so it survives a missing file, a fresh
    container and a rollback, none of which can quietly hand a live platform to the engine."""
    assert Settings.model_fields["ALLOCATION_ENGINE_MODE"].default == "off"


@pytest.mark.parametrize(
    "value", ["true", "True", "1", "yes", "y", "enabled", "onn", "no", "", "  "])
def test_everything_that_merely_looks_enabled_reads_as_off(monkeypatch, value):
    """The switch is NOT a truthiness test. ``ALLOCATION_ENGINE_MODE=true`` is a plausible thing
    for someone to write in an ``.env``, and it must not start an engine that moves real money."""
    _mode(monkeypatch, value)
    assert settings.allocation_mode == "off"
    assert settings.allocation_active is False
    assert settings.allocation_shadow is False


@pytest.mark.parametrize("value,mode", [
    ("on", "on"), ("ON", "on"), ("  on  ", "on"), ("shadow", "shadow"), ("SHADOW", "shadow"),
])
def test_the_two_words_that_do_something_are_read_case_and_space_insensitively(
        monkeypatch, value, mode):
    """Tolerant about how the word is typed, strict about which word it is."""
    _mode(monkeypatch, value)
    assert settings.allocation_mode == mode
    assert settings.allocation_active is (mode == "on")
    assert settings.allocation_shadow is (mode == "shadow")


# ═══ 2 — OFF: the engine never runs ═════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_the_engine_off_a_deposit_waits_for_an_admin_exactly_as_it_used_to(
        db, monkeypatch):
    """THE property that makes shipping to an unprepared environment safe.

    A perfectly allocatable deposit — eligible account, ample headroom — must still come back in
    ACCOUNT_REQUESTED and not NO_ELIGIBLE_ACCOUNT. The second is a status this platform has never
    shown a merchant, and it is what a "run it and ignore the answer" gate would have produced.
    """
    merchant = await _dep_merchant(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=100000.0)
    _mode(monkeypatch, "off")

    out = await txr.create_deposit(_dep_payload(45000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_REQUESTED
    assert out["status"] != TxStatus.NO_ELIGIBLE_ACCOUNT
    assert out["adminRef"] is None
    assert out["allocationSnapshot"] is None
    # The engine was never CALLED — an empty journal is the proof. A gate that ran the engine and
    # discarded its answer would have left a row here.
    assert await _rows(db, DepositAllocation) == []


@pytest.mark.asyncio
async def test_with_the_engine_off_a_withdrawal_carries_no_paying_account(db, monkeypatch):
    """The pre-allocation withdrawal: it goes to the Manager, and an Admin records the paying
    account by hand afterwards. No leg is written, so no capacity is held."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    _mode(monkeypatch, "off")

    out = await _create(db, merchant, 45000)

    assert out["status"] == TxStatus.MANAGER_REVIEW      # the review gate is untouched
    assert out["status"] != TxStatus.NO_ELIGIBLE_ACCOUNT
    assert out["payoutLegs"] == []
    assert out["payoutAccountRef"] is None
    assert await _rows(db, WithdrawalAllocation) == []
    assert await _rows(db, WithdrawalPayoutLeg) == []


# ═══ 3 — SHADOW: journalled, and applied to nothing ═════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_shadow_deposit_is_journalled_and_the_request_still_waits(db, monkeypatch):
    """The engine ran — there is a journal row to compare against the Admin's own choice — and the
    merchant's request is untouched by it."""
    merchant = await _dep_merchant(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=100000.0)
    _mode(monkeypatch, "shadow")

    out = await txr.create_deposit(_dep_payload(45000), db, merchant)

    assert len(await _rows(db, DepositAllocation)) == 1   # it ran…
    assert out["adminRef"] is None                        # …and nothing was applied
    assert out["allocationSnapshot"] is None
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED


@pytest.mark.asyncio
async def test_a_shadow_withdrawal_writes_a_journal_row_but_never_a_payout_leg(db, monkeypatch):
    """A leg is a claim on an account's money. Shadow mode forms an opinion; it does not stake
    anything on it."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)
    _mode(monkeypatch, "shadow")

    out = await _create(db, merchant, 45000)

    assert len(await _rows(db, WithdrawalAllocation)) == 1
    assert await _rows(db, WithdrawalPayoutLeg) == []
    assert out["payoutLegs"] == []
    assert out["payoutAccountRef"] is None
    assert out["status"] == TxStatus.MANAGER_REVIEW


@pytest.mark.asyncio
async def test_a_shadow_decision_consumes_none_of_the_days_capacity(db, monkeypatch):
    """The property that makes shadow mode safe to leave running.

    ₹50,000 of daily credit headroom. A shadow deposit for ₹45,000 forms a decision and discards
    it; a real ₹45,000 deposit immediately afterwards must still be allocatable. If a shadow run
    reserved anything, the second request would starve — and the manual workflow being compared
    against would have been quietly sabotaged by its own observer.
    """
    merchant = await _dep_merchant(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=50000.0)

    _mode(monkeypatch, "shadow")
    shadowed = await txr.create_deposit(_dep_payload(45000), db, merchant)
    assert shadowed["adminRef"] is None
    assert len(await _rows(db, DepositAllocation)) == 1          # the engine did run

    _mode(monkeypatch, "on")
    real = await txr.create_deposit(_dep_payload(45000, memberId="MBR2"), db, merchant)

    assert real["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert real["adminRef"] == "ACC1"                            # the full day's headroom survived


# ═══ 4 — the retry endpoints, which cannot place anything with the engine down ══════════════════

@pytest.mark.asyncio
async def test_retrying_a_deposit_with_the_engine_off_says_so_instead_of_blaming_the_accounts(
        db, monkeypatch):
    """The 409 this endpoint normally raises means "every account is full". With the engine off
    that would be a lie, and an Admin would go hunting for a capacity problem that does not
    exist."""
    merchant = await _dep_merchant(db)
    admin = await _admin(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=100000.0)
    _mode(monkeypatch, "off")
    out = await txr.create_deposit(_dep_payload(45000), db, merchant)

    with pytest.raises(HTTPException) as exc:
        await txr.retry_allocation(out["id"], db, admin)

    assert exc.value.status_code == 409
    assert "switched off" in exc.value.detail
    tx = await txr._get_tx(out["id"], db)
    assert tx.status == TxStatus.ACCOUNT_REQUESTED       # and it is left exactly where it was


@pytest.mark.asyncio
async def test_retrying_a_withdrawal_with_the_engine_off_never_demotes_one_that_has_an_account(
        db, monkeypatch):
    """A REGRESSION the switch itself introduced, and the sharpest edge of the whole change.

    ``retry_payout_allocation`` reads "the engine placed nothing" as "this withdrawal has become
    unpayable" and moves an ACCOUNT_SUBMITTED withdrawal into NO_ELIGIBLE_ACCOUNT. With the engine
    off it always places nothing — so, unguarded, a single retry would push a perfectly healthy,
    fully allocated withdrawal into an exception state it could only escape by being allocated, on
    the very box where allocation is switched off. The endpoint must refuse instead.
    """
    merchant = await _merchant(db)
    manager = await _manager(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "D1", "ACC1", 500000.0)

    _mode(monkeypatch, "on")
    created = await _create(db, merchant, 45000)
    approved = await txr.manager_approve(
        created["id"], RemarkRequest(remark="ok"), None, db, manager)
    assert approved["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert [l["accountRef"] for l in approved["payoutLegs"]] == ["ACC1"]

    _mode(monkeypatch, "off")
    with pytest.raises(HTTPException) as exc:
        await txr.retry_payout_allocation(created["id"], None, db, admin)

    assert exc.value.status_code == 409
    assert "switched off" in exc.value.detail
    tx = await txr._get_tx(created["id"], db)
    assert tx.status == TxStatus.ACCOUNT_SUBMITTED
    assert tx.status != TxStatus.NO_ELIGIBLE_ACCOUNT
    assert len(await _rows(db, WithdrawalPayoutLeg)) == 1        # its leg is still intact


@pytest.mark.asyncio
async def test_shadow_mode_cannot_place_an_account_through_the_retry_endpoint_either(
        db, monkeypatch):
    """Shadow mode observes. An Admin pressing Retry must not be the one path that lets an
    observation become a decision."""
    merchant = await _dep_merchant(db)
    admin = await _admin(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=100000.0)
    _mode(monkeypatch, "shadow")
    out = await txr.create_deposit(_dep_payload(45000), db, merchant)

    with pytest.raises(HTTPException) as exc:
        await txr.retry_allocation(out["id"], db, admin)

    assert exc.value.status_code == 409
    assert "switched off" in exc.value.detail


# ═══ 5 — ON: the switch is not inverted ═════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_the_engine_on_the_platform_allocates_as_the_other_suites_describe(
        db, monkeypatch):
    """A deliberate anchor. Everything above asserts that something does NOT happen, and a gate
    stuck shut would satisfy all of it. This is the one test here that fails if the switch is
    wired backwards."""
    merchant = await _dep_merchant(db)
    await _dep_account(db, "ACC1", name="Acc One", credit=100000.0)
    _mode(monkeypatch, "on")

    out = await txr.create_deposit(_dep_payload(45000), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["adminRef"] == "ACC1"
    assert len(await _rows(db, DepositAllocation)) == 1
