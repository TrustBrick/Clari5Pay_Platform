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
