"""Highest Debit is a CONFIGURED daily limit, never one inferred from history.

These tests cover the two production blockers found in the readiness audit of the withdrawal
allocation engine, and the lock-ordering correction made alongside them:

  1. **An unconfigured account is invisible to the engine.** ``highest_debit = 0`` means
     "no daily policy", not "unlimited", so the engine will not choose the account. That is
     correct — but on a database where every account is at 0 it means every withdrawal becomes an
     Admin exception, which is the manual step the feature removes. The failure must therefore be
     REPORTED, loudly and before a withdrawal is raised, not discovered one stuck payout at a time.
  2. **A daily limit is never guessed.** The migration used to seed ``highest_debit`` with the
     largest single debit an account had ever made. As a daily ceiling that is systematically too
     low. The figure is preserved as ``observed_max_debit`` and the limit is left to an Admin.
  3. **Locks are acquired in one deterministic order**, so two concurrent allocations cannot
     deadlock by each holding a row the other needs.

Run from the backend directory:

    python -m pytest tests/test_debit_limit_configuration.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.routes import accounts as acct_routes
from app.models.models import AccountMaster, AccountType, TxStatus
from app.schemas.schemas import AccountCreate, AccountLimitsUpdate
from app.services import account_ledger as ledger
from app.services import withdrawal_allocation as wa

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures)
    db, safe_refs_and_no_cache, _account, _admin, _allocate, _ben, _fund, _withdrawal,
)


# ── 1. An account with no configured daily limit ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_account_with_no_daily_limit_is_never_chosen(db):
    """``highest_debit = 0`` is UNCONFIGURED, not unlimited. Money and mode are not enough."""
    await _account(db, "A", debit=0.0)
    await _fund(db, "F", "A", 500000)

    result = await _allocate(db, 10000, mode="IMPS")

    assert result.outcome == wa.OUTCOME_NO_ACCOUNT
    assert result.failure_code == wa.FAIL_NO_LIMITS
    assert result.candidates[0].reject_reason == wa.REJECT_NO_LIMIT


@pytest.mark.asyncio
async def test_every_account_unconfigured_is_reported_not_silent(db):
    """The blocker case: a database of unconfigured accounts allocates NOTHING.

    The engine's answer is correct, and the readiness report is what stops it being a surprise —
    ``canAllocate`` is false and every account that needs a decision is named.
    """
    await _account(db, "A", debit=0.0)
    await _account(db, "B", debit=0.0)
    await _fund(db, "F", "A", 500000)

    assert (await _allocate(db, 1000, mode="IMPS")).outcome == wa.OUTCOME_NO_ACCOUNT

    ready = await wa.debit_limit_readiness(db)
    assert ready["canAllocate"] is False
    assert ready["allocatableAccounts"] == 0
    assert ready["needsConfiguration"] == 2
    assert sorted(ready["needsConfigurationRefs"]) == ["A", "B"]
    assert all(r["state"] == wa.READY_MISSING for r in ready["accounts"])


@pytest.mark.asyncio
async def test_an_inactive_account_is_not_asked_to_configure_a_limit(db):
    """Only an ACTIVE account can pay, so only an ACTIVE one needs a limit before go-live."""
    await _account(db, "OFF", debit=0.0, status="INACTIVE")

    ready = await wa.debit_limit_readiness(db)
    assert ready["needsConfiguration"] == 0
    assert ready["activeTotal"] == 0


# ── 2. An explicitly configured limit ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_explicitly_configured_limit_is_usable_and_reported_ready(db):
    acc = await _account(db, "A", debit=100000)
    acc.highest_debit_configured_at = datetime.utcnow()
    acc.highest_debit_configured_by = "Admin One"
    await _fund(db, "F", "A", 500000)
    await db.flush()

    result = await _allocate(db, 45000, mode="IMPS")
    assert result.outcome == wa.OUTCOME_ALLOCATED

    ready = await wa.debit_limit_readiness(db)
    assert ready["canAllocate"] is True
    assert ready["needsConfiguration"] == 0
    assert ready["accounts"][0]["state"] == wa.READY_OK
    assert ready["accounts"][0]["configuredBy"] == "Admin One"


@pytest.mark.asyncio
async def test_setting_the_limit_stamps_who_decided_it(db):
    """The Admin edit is the ONE place a daily limit is chosen, so it is where the stamp is made."""
    acc = await _account(db, "A", debit=50000)
    acc.highest_debit_configured_at = None
    await db.flush()
    admin = await _admin(db)

    assert wa.classify_debit_limit(acc) != wa.READY_OK

    await acct_routes.update_account_limits(
        "A", AccountLimitsUpdate(highest_credit=200000, highest_debit=300000),
        request=None, db=db, actor=admin)

    refreshed = await db.get(AccountMaster, acc.id)
    assert refreshed.highest_debit == 300000
    assert refreshed.highest_debit_configured_by == admin.name
    assert refreshed.highest_debit_configured_at is not None
    assert wa.classify_debit_limit(refreshed) == wa.READY_OK


@pytest.mark.asyncio
async def test_an_active_account_cannot_be_created_without_a_daily_limit(db):
    """An ACTIVE account with no limit is one the platform silently cannot pay from."""
    payload = AccountCreate(
        account_name="No Limit", account_number="123456", ifsc_code="HDFC0001234",
        bank_name="HDFC Bank", branch="Mumbai", account_type=AccountType.CURRENT,
        status="ACTIVE", highest_credit=100000, highest_debit=0)

    with pytest.raises(HTTPException) as err:
        acct_routes._created_debit_limit(payload)
    assert err.value.status_code == 400
    assert "Highest Debit is required" in err.value.detail

    # The same account created INACTIVE is allowed — it cannot pay anything yet.
    payload.status = "INACTIVE"
    assert acct_routes._created_debit_limit(payload) == 0.0


# ── 3. Migrated legacy accounts ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_legacy_limit_inherited_from_the_high_water_mark_is_flagged(db):
    """The migrated case: the limit equals the largest single debit ever made.

    That is the signature of the old high-water mark, not a daily policy. It still WORKS — the
    account stays usable, so no existing environment is broken by the change — but it is reported
    for review rather than quietly trusted.
    """
    acc = await _account(db, "A", debit=50000)
    acc.observed_max_debit = 50000
    acc.highest_debit_configured_at = None
    await _fund(db, "F", "A", 500000)
    await db.flush()

    assert wa.classify_debit_limit(acc) == wa.READY_SUSPICIOUS

    # Usable, deliberately: flagging must not disable an account that has been paying all along.
    assert (await _allocate(db, 30000, mode="IMPS")).outcome == wa.OUTCOME_ALLOCATED

    ready = await wa.debit_limit_readiness(db)
    assert ready["canAllocate"] is True
    assert ready["needsConfiguration"] == 1
    assert ready["accounts"][0]["observedMaxDebit"] == 50000


@pytest.mark.asyncio
async def test_a_limit_below_a_debit_already_made_is_flagged(db):
    """A daily ceiling under a single payout the account has demonstrably handled is suspect."""
    acc = await _account(db, "A", debit=20000)
    acc.observed_max_debit = 90000
    acc.highest_debit_configured_at = None
    await db.flush()
    assert wa.classify_debit_limit(acc) == wa.READY_SUSPICIOUS


@pytest.mark.asyncio
async def test_an_unstamped_limit_above_history_is_unconfirmed_not_suspicious(db):
    acc = await _account(db, "A", debit=500000)
    acc.observed_max_debit = 10000
    acc.highest_debit_configured_at = None
    await db.flush()
    assert wa.classify_debit_limit(acc) == wa.READY_UNCONFIRMED


@pytest.mark.asyncio
async def test_history_is_preserved_and_the_limit_is_never_raised_by_a_payout(db):
    """A completed debit records the observed maximum; it does NOT move the daily limit."""
    from app.api.routes import transactions as txr

    acc = await _account(db, "A", debit=100000)
    admin = await _admin(db)
    tx = await _withdrawal(db, "1", 250000, status=TxStatus.COMPLETED)
    tx.payout_account_ref = "A"
    tx.payout_payment_method = "BANK"
    await db.flush()

    await txr._track_account_debit(db, tx, admin, None)

    refreshed = await db.get(AccountMaster, acc.id)
    assert refreshed.highest_debit == 100000, "the daily limit must never auto-raise"
    assert refreshed.observed_max_debit == 250000, "the historical maximum must be preserved"


# ── 4. Concurrency and lock ordering ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_locks_are_taken_once_over_the_whole_candidate_set(db, monkeypatch):
    """The deadlock fix: ONE acquisition, every candidate, ascending by reference.

    Two allocations that request the same rows in the same sequence cannot each end up holding
    what the other needs. A second, narrower acquisition is what would reintroduce that risk, so
    this pins the call COUNT as well as the set.
    """
    for ref in ("C", "A", "B"):
        await _account(db, ref, debit=100000)
        await _fund(db, "F" + ref, ref, 500000)

    calls: list[list[str]] = []
    real = wa._lock_accounts

    async def spy(db_, refs):
        calls.append(list(refs))
        return await real(db_, refs)

    monkeypatch.setattr(wa, "_lock_accounts", spy)
    result = await _allocate(db, 45000, mode="IMPS")

    assert result.outcome == wa.OUTCOME_ALLOCATED
    assert len(calls) == 1, "expected exactly one lock acquisition, got " + str(calls)
    assert sorted(calls[0]) == ["A", "B", "C"], "every candidate must be locked, not just the plan"


@pytest.mark.asyncio
async def test_lock_accounts_always_locks_in_ascending_reference_order(db):
    """Whatever order it is handed, it acquires in ascending order — the deadlock-free direction."""
    for ref in ("C", "A", "B"):
        await _account(db, ref, debit=100000)

    acquired: list[str] = []
    real_execute = db.execute

    async def spy_execute(stmt, *a, **kw):
        text = str(stmt)
        if "FOR UPDATE" in text.upper():
            for ref in ("A", "B", "C"):
                if ref in str(stmt.compile().params.values()):
                    acquired.append(ref)
                    break
        return await real_execute(stmt, *a, **kw)

    db.execute = spy_execute
    try:
        await wa._lock_accounts(db, ["C", "A", "B"])
    finally:
        db.execute = real_execute

    # Guard against the test passing on an empty list: all three rows must really be locked.
    assert acquired == ["A", "B", "C"], "expected A,B,C acquired in order, got " + str(acquired)


@pytest.mark.asyncio
async def test_an_operator_directed_payout_locks_only_that_account(db, monkeypatch):
    """Narrowing to one named account must not widen the lock set — it is a single row."""
    for ref in ("A", "B"):
        await _account(db, ref, debit=100000)
        await _fund(db, "F" + ref, ref, 500000)

    calls: list[list[str]] = []
    real = wa._lock_accounts

    async def spy(db_, refs):
        calls.append(list(refs))
        return await real(db_, refs)

    monkeypatch.setattr(wa, "_lock_accounts", spy)
    result = await _allocate(db, 45000, mode="IMPS", force_account_ref="B")

    assert result.outcome == wa.OUTCOME_ALLOCATED
    assert calls == [["B"]]


@pytest.mark.asyncio
async def test_concurrent_withdrawals_cannot_breach_a_configured_daily_limit(db):
    """Two withdrawals against one account: the second is refused the capacity the first holds."""
    acc = await _account(db, "A", debit=100000)
    acc.highest_debit_configured_at = datetime.utcnow()
    await _fund(db, "F", "A", 1000000)
    await db.flush()

    first = await _allocate(db, 60000, mode="IMPS")
    assert first.outcome == wa.OUTCOME_ALLOCATED
    tx1 = await _withdrawal(db, "1", 60000)
    await wa.write_legs(db, first, transaction=tx1, allocated_by="test")

    second = await _allocate(db, 50000, mode="IMPS")
    assert second.outcome == wa.OUTCOME_NO_ACCOUNT, "60k + 50k would breach the 1,00,000 limit"

    # 40,000 exactly fills the remaining headroom.
    third = await _allocate(db, 40000, mode="IMPS")
    assert third.outcome == wa.OUTCOME_ALLOCATED
    assert third.legs[0].amount == 40000


@pytest.mark.asyncio
async def test_readiness_orders_the_worst_problems_first(db):
    """The report is read top-down, so it is ordered the way an Admin should act on it."""
    ok = await _account(db, "OK", debit=100000)
    ok.highest_debit_configured_at = datetime.utcnow()
    await _account(db, "MISS", debit=0.0)
    susp = await _account(db, "SUSP", debit=50000)
    susp.observed_max_debit = 50000
    await db.flush()

    ready = await wa.debit_limit_readiness(db)
    assert [r["accountRef"] for r in ready["accounts"]] == ["MISS", "SUSP", "OK"]
    assert ready["counts"][wa.READY_MISSING] == 1
    assert ready["counts"][wa.READY_SUSPICIOUS] == 1
    assert ready["counts"][wa.READY_OK] == 1


# ── Readiness reports what the ENGINE can do, not what configuration suggests ──────────────────
#
# The report used to decide eligibility itself, with `status == ACTIVE and highest_debit > 0`.
# That is two of the five gates `_evaluate` applies, and the three it omitted — mode, today's
# consumed capacity, and available balance — are the ones that actually fail. On a real
# environment it claimed 7 allocatable accounts where the engine could use exactly one. Each test
# below is one of the ways those two answers could drift apart again.


@pytest.mark.asyncio
async def test_a_funded_account_with_no_daily_limit_is_not_counted_eligible(db):
    """Money without a limit. The engine will not choose it, so readiness must not count it."""
    await _account(db, "A", debit=0.0)
    await _fund(db, "F", "A", 500000)

    ready = await wa.payout_readiness(db)

    assert ready["fundedAccounts"] == 1, "it does hold money — reported separately, on purpose"
    assert ready["eligibleAccounts"] == 0
    assert ready["canAllocate"] is False
    assert ready["totalUsableCapacity"] == 0.0
    assert ready["ineligibleActiveReasons"]["A"] == wa.REJECT_NO_LIMIT


@pytest.mark.asyncio
async def test_a_configured_limit_with_no_money_is_not_counted_eligible(db):
    """A limit without money. This is the shape that produced the 7x overcount."""
    acc = await _account(db, "A", debit=100000)
    acc.highest_debit_configured_at = datetime.utcnow()
    await db.flush()

    ready = await wa.payout_readiness(db)

    assert ready["accounts"][0]["state"] == wa.READY_OK, "configuration IS complete"
    assert ready["configurationComplete"] is True
    assert ready["fundedAccounts"] == 0
    assert ready["eligibleAccounts"] == 0, "configured is not the same as able to pay"
    assert ready["canAllocate"] is False
    assert ready["readyForTesting"] is False
    assert ready["ineligibleActiveReasons"]["A"] == wa.REJECT_NO_BALANCE


@pytest.mark.asyncio
async def test_a_negative_balance_is_not_counted_eligible(db):
    """The production case: an account whose completed debits exceed its deposits.

    Built the way the real one arose — a completed withdrawal attributed to the account with no
    deposit behind it — rather than by writing a negative number somewhere.
    """
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 10000)
    spent = await _withdrawal(db, "W1", 50000, status=TxStatus.COMPLETED)
    spent.payout_account_ref = "A"
    await db.flush()

    ready = await wa.payout_readiness(db)

    assert ready["fundedAccounts"] == 0, "a negative balance is not funded"
    assert ready["eligibleAccounts"] == 0
    assert ready["canAllocate"] is False
    assert ready["totalUsableCapacity"] == 0.0, "never negative — capacity floors at nothing"
    assert ready["ineligibleActiveReasons"]["A"] == wa.REJECT_NO_BALANCE


@pytest.mark.asyncio
async def test_an_account_that_cannot_do_the_mode_is_not_counted_eligible(db):
    """Configured, funded, and unable to pay by the rail asked about."""
    await _account(db, "UPIONLY", debit=100000, modes="UPI")
    await _fund(db, "F", "UPIONLY", 500000)

    bank = await wa.payout_readiness(db, mode=wa.BANK_MODE)
    assert bank["eligibleAccounts"] == 0
    assert bank["ineligibleActiveReasons"]["UPIONLY"] == wa.REJECT_MODE
    assert bank["mode"] == wa.BANK_MODE, "the payload always says what it measured"

    # Same account, same data, asked about the rail it actually supports.
    upi = await wa.payout_readiness(db, mode="UPI")
    assert upi["eligibleAccounts"] == 1
    assert upi["canAllocate"] is True


@pytest.mark.asyncio
async def test_an_account_whose_daily_limit_is_used_up_is_not_counted_eligible(db):
    """Configured and funded, but the day's headroom is gone. Readiness must follow the clock."""
    await _account(db, "A", debit=50000)
    await _fund(db, "F", "A", 500000)

    before = await wa.payout_readiness(db)
    assert before["eligibleAccounts"] == 1
    assert before["totalUsableCapacity"] == 50000.0

    tx = await _withdrawal(db, "W1", 50000)
    await wa.write_legs(db, await _allocate(db, 50000, mode="IMPS"), transaction=tx)

    after = await wa.payout_readiness(db)
    assert after["eligibleAccounts"] == 0, "the whole daily limit is committed"
    assert after["canAllocate"] is False
    assert after["totalUsableCapacity"] == 0.0
    assert after["ineligibleActiveReasons"]["A"] == wa.REJECT_NO_CAPACITY


@pytest.mark.asyncio
async def test_eligible_counts_banks_and_capacity_come_from_the_engine(db):
    """Six accounts, one usable per failure mode plus two that work, at two different banks.

    This is the whole regression in one table: the old rule counted FIVE allocatable accounts
    here (every ACTIVE one with a limit above zero); the engine can use two.
    """
    await _account(db, "GOOD-HDFC", bank="HDFC Bank", debit=60000)
    await _fund(db, "F1", "GOOD-HDFC", 500000)
    await _account(db, "GOOD-ICICI", bank="ICICI Bank", debit=40000)
    await _fund(db, "F2", "GOOD-ICICI", 500000)
    await _account(db, "NOLIMIT", bank="Axis Bank", debit=0.0)          # funded, no limit
    await _fund(db, "F3", "NOLIMIT", 500000)
    await _account(db, "NOMONEY", bank="Axis Bank", debit=90000)        # limit, no money
    await _account(db, "WRONGMODE", bank="Axis Bank", debit=90000, modes="UPI")
    await _fund(db, "F5", "WRONGMODE", 500000)
    await _account(db, "OFF", bank="Axis Bank", debit=90000, status="INACTIVE")
    await _fund(db, "F6", "OFF", 500000)

    ready = await wa.payout_readiness(db)

    assert ready["eligibleAccounts"] == 2
    assert sorted(ready["eligibleAccountRefs"]) == ["GOOD-HDFC", "GOOD-ICICI"]
    assert ready["eligibleBanks"] == 2
    assert sorted(ready["eligibleBankNames"]) == ["HDFC Bank", "ICICI Bank"]
    assert ready["totalUsableCapacity"] == 100000.0, "60,000 + 40,000, the engine's own figure"
    assert ready["largestSingleAccountCapacity"] == 60000.0
    assert ready["canAllocate"] is True
    assert ready["allocatableAccounts"] == 2

    # The old predicate — ACTIVE and a limit above zero — would have said five.
    old_rule = [r for r in ready["accounts"] if r["active"] and r["highestDebit"] > 0]
    assert len(old_rule) == 4, "and every one of the extra two cannot pay a rupee"
    assert set(ready["ineligibleActiveReasons"]) == {"NOLIMIT", "NOMONEY", "WRONGMODE"}

    # The capacity figure must be the one a real refusal would quote.
    result = await _allocate(db, 100001, mode="IMPS")
    assert result.outcome == wa.OUTCOME_NO_ACCOUNT
    assert result.detail["totalUsableCapacity"] == ready["totalUsableCapacity"]


@pytest.mark.asyncio
async def test_ready_for_testing_requires_configuration_as_well_as_eligibility(db):
    """``readyForTesting`` is the composite that did not exist: chosen limits AND real capacity."""
    acc = await _account(db, "A", debit=100000)
    acc.highest_debit_configured_at = datetime.utcnow()
    acc.highest_debit_configured_by = "Admin One"
    await _fund(db, "F", "A", 500000)
    await db.flush()

    ready = await wa.payout_readiness(db)
    assert ready["configurationComplete"] is True
    assert ready["eligibleAccounts"] == 1
    assert ready["readyForTesting"] is True

    # One unconfigured ACTIVE account is enough to make the environment unready — while leaving
    # canAllocate true, because the engine can still pay from the account that IS configured.
    await _account(db, "B", debit=0.0)
    later = await wa.payout_readiness(db)
    assert later["configurationComplete"] is False
    assert later["canAllocate"] is True
    assert later["readyForTesting"] is False


@pytest.mark.asyncio
async def test_scenario_support_says_which_allocation_shapes_are_testable(db):
    """One bank with capacity cannot exercise a cross-bank or bank-preference scenario.

    Reported rather than left to be worked out, because this is exactly the question that had to
    be answered by hand about a real environment.
    """
    await _account(db, "IDBI-1", bank="IDBI", debit=99000)
    await _fund(db, "F1", "IDBI-1", 500000)

    one = await wa.payout_readiness(db, probe_amount=50000)
    assert one["scenarioSupport"]["anyAllocation"] is True
    assert one["scenarioSupport"]["canCoverProbeAmount"] is True
    assert one["scenarioSupport"]["singleAccountCoversProbeAmount"] is True
    assert one["scenarioSupport"]["sameBankSplit"] is False
    assert one["scenarioSupport"]["crossBankSplit"] is False
    assert one["probeAmount"] == 50000.0

    # An amount beyond the only account: coverable by nobody, single or combined.
    big = await wa.payout_readiness(db, probe_amount=200000)
    assert big["scenarioSupport"]["canCoverProbeAmount"] is False
    assert big["scenarioSupport"]["singleAccountCoversProbeAmount"] is False
    assert big["canAllocate"] is True, "it can still pay SOMETHING — just not that"

    # A second account at the same bank, then a third at another, open the split shapes.
    await _account(db, "IDBI-2", bank="IDBI", debit=99000)
    await _fund(db, "F2", "IDBI-2", 500000)
    await _account(db, "HDFC-1", bank="HDFC Bank", debit=99000)
    await _fund(db, "F3", "HDFC-1", 500000)

    many = await wa.payout_readiness(db, probe_amount=200000)
    assert many["scenarioSupport"]["sameBankSplit"] is True
    assert many["scenarioSupport"]["crossBankSplit"] is True
    assert many["scenarioSupport"]["canCoverProbeAmount"] is True
    assert many["scenarioSupport"]["singleAccountCoversProbeAmount"] is False
    assert many["eligibleBanks"] == 2


@pytest.mark.asyncio
async def test_the_report_never_disagrees_with_a_real_allocation(db):
    """The property the whole rewrite exists to guarantee, asserted directly.

    Whatever readiness calls eligible, the engine must be able to allocate from — and whatever it
    calls ineligible, the engine must refuse for the same stated reason.
    """
    await _account(db, "OK", bank="HDFC Bank", debit=70000)
    await _fund(db, "F1", "OK", 500000)
    await _account(db, "NOMONEY", bank="ICICI Bank", debit=70000)
    await _account(db, "NOLIMIT", bank="Axis Bank", debit=0.0)
    await _fund(db, "F3", "NOLIMIT", 500000)

    ready = await wa.payout_readiness(db)
    engine = {c.ref: c for c in await wa.evaluate_accounts(
        db, 0.01, mode=wa.BANK_MODE, require_full=False)}

    assert set(ready["eligibleAccountRefs"]) == {r for r, c in engine.items() if c.eligible}
    for ref, why in ready["ineligibleActiveReasons"].items():
        assert engine[ref].eligible is False
        assert engine[ref].reject_reason == why


@pytest.mark.asyncio
async def test_the_original_readiness_path_and_helper_still_work(db):
    """The endpoint URL and the old function name are kept, so nothing that called them breaks."""
    await _account(db, "A", debit=60000)
    await _fund(db, "F", "A", 500000)
    admin = await _admin(db)

    alias = await wa.debit_limit_readiness(db)
    assert alias["canAllocate"] is True
    assert alias["eligibleAccounts"] == 1

    out = await acct_routes.payout_readiness(0.01, wa.BANK_MODE, db, admin)
    assert out["eligibleAccounts"] == 1
    assert out["totalUsableCapacity"] == 60000.0


# ── The balance query must be valid on PostgreSQL, not just on SQLite ──────────────────────────

@pytest.mark.asyncio
async def test_account_balances_groups_by_the_same_expression_it_selects(db):
    """`account_balances` must produce SQL PostgreSQL accepts.

    Its legacy-debit term groups by a COMPUTED expression. Spelling that expression out separately
    in the select list, the filter and the GROUP BY builds three different expressions whose
    empty-string default becomes three different bind parameters; Postgres compares GROUP BY
    against the select list syntactically, sees `coalesce(member_id, $5)` next to
    `coalesce(member_id, $6)`, and refuses the query. SQLite does not care, so every test here
    passes while the function fails on the only database that matters.

    This asserts the rendered PostgreSQL, which is where the difference is visible.
    """
    import re
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql
    from app.models.models import Transaction

    member_key = func.upper(func.trim(func.coalesce(Transaction.member_id, "")))
    stmt = (select(member_key, func.coalesce(func.sum(Transaction.amount), 0.0))
            .where(member_key.in_(["MM01"]))
            .group_by(member_key))
    sql = str(stmt.compile(dialect=postgresql.dialect()))

    selected = re.search(r"SELECT (.*?) \nFROM", sql, re.S).group(1)
    selected_expr = selected.split(", coalesce(sum")[0].split(" AS ")[0].strip()
    grouped_expr = sql.split("GROUP BY ")[1].strip()
    assert selected_expr == grouped_expr, (
        f"GROUP BY must render identically to the selected expression.\n"
        f"  select:   {selected_expr}\n  group by: {grouped_expr}")

    # And the real function runs end to end against a populated database.
    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000)
    balances = await ledger.account_balances(db, ["A"])
    assert balances["A"] == 500000


@pytest.mark.asyncio
async def test_account_balances_survives_a_legacy_debit_with_no_payout_account(db):
    """The legacy-debit branch only runs when a member maps to an account — the path that failed
    on demo. It must return the balance with that debit subtracted."""
    from app.models.models import AccountTransaction

    await _account(db, "A", debit=100000)
    await _fund(db, "F", "A", 500000, member="MM01")
    db.add(AccountTransaction(reference_number="A", member_id="MM01",
                              transaction_date=wa.ist_today(), transaction_time="10:00:00"))
    tx = await _withdrawal(db, "W1", 20000, member="MM01", status=TxStatus.COMPLETED)
    tx.payout_account_ref = None          # legacy: names no paying account
    await db.flush()

    balances = await ledger.account_balances(db, ["A"])
    assert balances["A"] == 480000, "the legacy debit must be attributed to the member's account"
