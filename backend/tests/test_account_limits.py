"""Tests for the Admin edit of an account's Highest Credit / Highest Debit limits.

These two fields are CONFIGURATION that happens to be denominated in rupees, which is exactly what
makes them worth pinning down: they sit one popup away from Current Balance, Deposits Received and
the accounting ledger, and none of those may move when a limit does.

The properties that matter:

  1. **A limit is not money** — changing either value leaves the derived balance, the deposits and
     the ledger byte-for-byte identical. This is the whole point of the feature.
  2. **Only the addressed account changes** — the account comes from the URL, so a request cannot
     reach past it into another account's configuration.
  3. **Validation is server-side** — required, numeric, finite, greater than zero. Nothing the
     browser sends is trusted, and a rejected edit writes nothing at all.
  4. **The change is audited, append-only** — every edit leaves a SystemLog line and an AuditLog
     row carrying both before/after pairs. A later edit adds a row; it never rewrites one.
  5. **RBAC is the existing gate** — ``get_current_admin``. No merchant role and no support member
     can call the route, with or without a button on screen.

Run from the backend directory:

    python -m pytest tests/test_account_limits.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import accounts as acct
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountType, AuditLog, SystemLog, Transaction, TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import AccountLimitsUpdate
from app.services import account_ledger as ledger


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    """A real, empty database built from the project's own models."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def dropped_cache_keys(monkeypatch):
    """Record the cache keys the route drops instead of talking to Redis.

    The short-TTL cache is fail-open in production, so leaving it live would still pass — it would
    just spend a connection timeout per call. Recording the keys is both faster and stricter: the
    invalidation is asserted below rather than merely tolerated.
    """
    dropped: list[str] = []

    async def fake_delete(key: str):
        dropped.append(key)

    monkeypatch.setattr(acct, "cache_delete", fake_delete)
    return dropped


class _Req:
    """The minimal request the route reads (client IP for the audit row)."""
    client = None


def _admin(uid=1, role=UserRole.ADMIN) -> User:
    return User(id=uid, username="admin1", name="Admin One", role=role)


async def _account(db: AsyncSession, ref="ACC0000001", *, name="sindu",
                   credit=800000.0, debit=100000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name, account_number="12345678908890",
        ifsc_code="HDFC0001234", bank_name="HDFC Bank", branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE",
        created_date=date.today(), created_time="10:00:00",
        highest_credit=credit, highest_debit=debit, debit_alert_threshold=debit,
    )
    db.add(acc)
    await db.flush()
    return acc


async def _deposit(db: AsyncSession, ref: str, amount: float, account_ref: str) -> None:
    db.add(Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=TxStatus.DEPOSITED,
        merchant_id=7, merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id="WININ25504", admin_ref=account_ref, created_at=datetime.utcnow(),
    ))
    await db.flush()


async def _set_limits(db, ref, credit, debit, *, actor=None, reason=None):
    return await acct.update_account_limits(
        ref, AccountLimitsUpdate(highest_credit=credit, highest_debit=debit, reason=reason),
        _Req(), db, actor or _admin(),
    )


async def _audits(db) -> list[AuditLog]:
    return list((await db.execute(
        select(AuditLog).where(AuditLog.action_type == "ACCOUNT_LIMITS_UPDATED")
        .order_by(AuditLog.id)
    )).scalars().all())


# ── 1. A limit is not money ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_raising_the_credit_limit_leaves_the_balance_alone(db):
    """The brief's own example: limits 8,00,000 / 1,00,000 with 9,19,000 available. Raise the
    credit limit to 10,00,000 — available must still read 9,19,000."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 919000, acc.reference_number)
    before = await ledger.account_balance(db, acc.reference_number)
    assert before == 919000.0

    out = await _set_limits(db, acc.reference_number, 1000000, 100000)

    assert out["highestCredit"] == 1000000.0
    assert await ledger.account_balance(db, acc.reference_number) == before


@pytest.mark.asyncio
async def test_changing_both_limits_leaves_the_balance_alone(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 919000, acc.reference_number)
    before = await ledger.account_balance(db, acc.reference_number)

    await _set_limits(db, acc.reference_number, 1000000, 200000)

    assert await ledger.account_balance(db, acc.reference_number) == before


@pytest.mark.asyncio
async def test_the_deposits_behind_the_balance_are_untouched(db):
    """Nothing about the transaction that produced the balance is rewritten either."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 919000, acc.reference_number)
    tx = (await db.execute(select(Transaction).where(Transaction.ref == "DEP000001"))).scalar_one()
    snapshot = (tx.amount, tx.status, tx.admin_ref, tx.member_id)

    await _set_limits(db, acc.reference_number, 1000000, 200000)

    tx = (await db.execute(select(Transaction).where(Transaction.ref == "DEP000001"))).scalar_one()
    assert (tx.amount, tx.status, tx.admin_ref, tx.member_id) == snapshot


@pytest.mark.asyncio
async def test_the_new_values_persist_and_are_returned(db):
    acc = await _account(db)
    out = await _set_limits(db, acc.reference_number, 1000000, 200000)

    assert (out["highestCredit"], out["highestDebit"]) == (1000000.0, 200000.0)
    stored = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == acc.reference_number)
    )).scalar_one()
    assert (stored.highest_credit, stored.highest_debit) == (1000000.0, 200000.0)


@pytest.mark.asyncio
async def test_decimal_amounts_are_kept_to_paise(db):
    acc = await _account(db)
    out = await _set_limits(db, acc.reference_number, 1000000.567, 50000.5)
    assert (out["highestCredit"], out["highestDebit"]) == (1000000.57, 50000.5)


@pytest.mark.asyncio
async def test_the_low_debit_alert_threshold_is_not_moved(db):
    """``debit_alert_threshold`` is the FIXED level set at account creation — editing the Highest
    Debit limit must not silently change which debits raise an alert."""
    acc = await _account(db, credit=800000, debit=100000)
    assert acc.debit_alert_threshold == 100000.0

    await _set_limits(db, acc.reference_number, 800000, 500000)

    stored = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == acc.reference_number)
    )).scalar_one()
    assert stored.highest_debit == 500000.0
    assert stored.debit_alert_threshold == 100000.0


# ── 2. Only the addressed account changes ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_second_account_is_untouched(db):
    """The account is resolved from the URL alone — there is no id in the body to point elsewhere."""
    a = await _account(db, "ACC0000001", name="sindu", credit=800000, debit=100000)
    b = await _account(db, "ACC0000002", name="other", credit=300000, debit=50000)

    await _set_limits(db, a.reference_number, 1000000, 200000)

    other = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == b.reference_number)
    )).scalar_one()
    assert (other.highest_credit, other.highest_debit) == (300000.0, 50000.0)


@pytest.mark.asyncio
async def test_an_unknown_account_is_a_404(db):
    await _account(db)
    with pytest.raises(HTTPException) as e:
        await _set_limits(db, "ACC9999999", 1000000, 200000)
    assert e.value.status_code == 404


# ── 3. Validation is server-side ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("credit, debit", [
    (0, 100000),            # zero credit
    (800000, 0),            # zero debit
    (-1, 100000),           # negative credit
    (800000, -0.01),        # negative debit
    (float("nan"), 100000), # not a number
    (float("inf"), 100000), # not a finite amount
])
async def test_bad_limits_are_rejected(db, credit, debit):
    acc = await _account(db)
    with pytest.raises(HTTPException) as e:
        await _set_limits(db, acc.reference_number, credit, debit)
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("credit, debit", [(0, 100000), (-5, 100000), (800000, 0)])
async def test_a_rejected_edit_writes_nothing(db, credit, debit):
    """Validation runs before the account is even loaded, so a bad request cannot half-apply."""
    acc = await _account(db, credit=800000, debit=100000)
    with pytest.raises(HTTPException):
        await _set_limits(db, acc.reference_number, credit, debit)

    stored = (await db.execute(
        select(AccountMaster).where(AccountMaster.reference_number == acc.reference_number)
    )).scalar_one()
    assert (stored.highest_credit, stored.highest_debit) == (800000.0, 100000.0)
    assert await _audits(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "1,00,000", "₹500", None])
async def test_non_numeric_input_never_reaches_the_route(bad):
    """The schema is the first gate: anything that is not a number is refused before the handler
    runs, so the route only ever sees a real amount."""
    with pytest.raises(ValidationError):
        AccountLimitsUpdate(highest_credit=bad, highest_debit=100000)


@pytest.mark.asyncio
async def test_both_limits_are_required():
    with pytest.raises(ValidationError):
        AccountLimitsUpdate(highest_credit=800000)      # no highest_debit


# ── 4. The change is audited, append-only ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_edit_records_both_before_and_after_values(db):
    acc = await _account(db, name="sindu", credit=800000, debit=100000)
    await _set_limits(db, acc.reference_number, 1000000, 200000, reason="Limit review")

    rows = await _audits(db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.entity_type == "account"
    assert entry.entity_id == acc.reference_number         # Account ID
    assert "sindu" in entry.reason                          # Account Name
    assert "Limit review" in entry.reason                   # Reason / description
    assert "IST" in entry.reason                            # Changed At, in IST
    assert entry.old_value == "Highest Credit ₹800,000.00 · Highest Debit ₹100,000.00"
    assert entry.new_value == "Highest Credit ₹1,000,000.00 · Highest Debit ₹200,000.00"
    assert entry.username == "Admin One"                    # Changed By
    assert entry.role == "ADMIN"
    assert entry.created_at is not None                     # Changed At


@pytest.mark.asyncio
async def test_an_edit_also_lands_in_the_system_log(db):
    acc = await _account(db)
    await _set_limits(db, acc.reference_number, 1000000, 200000)

    rows = list((await db.execute(select(SystemLog).where(
        SystemLog.action == "ACCOUNT_LIMITS_UPDATED"))).scalars().all())
    assert len(rows) == 1
    assert acc.reference_number in rows[0].detail
    assert "Highest Credit" in rows[0].detail and "Highest Debit" in rows[0].detail


@pytest.mark.asyncio
async def test_history_is_appended_never_overwritten(db):
    """A second edit adds a second row whose 'previous' is the first edit's 'new'."""
    acc = await _account(db, credit=800000, debit=100000)
    await _set_limits(db, acc.reference_number, 1000000, 200000)
    await _set_limits(db, acc.reference_number, 1500000, 250000)

    rows = await _audits(db)
    assert len(rows) == 2
    assert "₹800,000.00" in rows[0].old_value and "₹1,000,000.00" in rows[0].new_value
    assert "₹1,000,000.00" in rows[1].old_value and "₹1,500,000.00" in rows[1].new_value


@pytest.mark.asyncio
async def test_saving_the_same_values_is_not_an_audited_change(db):
    """Re-submitting the form untouched is a no-op, not a phantom entry in a financial log."""
    acc = await _account(db, credit=800000, debit=100000)
    out = await _set_limits(db, acc.reference_number, 800000, 100000)

    assert (out["highestCredit"], out["highestDebit"]) == (800000.0, 100000.0)
    assert await _audits(db) == []


@pytest.mark.asyncio
async def test_a_change_drops_the_cached_account_views(db, dropped_cache_keys):
    """The table reads /accounts and /balances, both served from a few-second cache — an edit
    invalidates them so the new limits show on the next load rather than on the next TTL."""
    acc = await _account(db)
    await _set_limits(db, acc.reference_number, 1000000, 200000)
    assert set(dropped_cache_keys) == {"c:accounts:balances", "c:accounts:list"}


@pytest.mark.asyncio
async def test_a_no_op_edit_drops_nothing(db, dropped_cache_keys):
    acc = await _account(db, credit=800000, debit=100000)
    await _set_limits(db, acc.reference_number, 800000, 100000)
    assert dropped_cache_keys == []


# ── 5. RBAC is the existing gate ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_role", ["DEO", "SUPERVISOR", "MANAGER",
                                           "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR", None])
async def test_no_merchant_role_can_edit_limits(merchant_role):
    """Merchants may view the account information they are already permitted to see; the limits
    route sits behind the same dependency the rest of Account Management uses, so none of them can
    call it — hiding the button is presentation, this is the control."""
    from app.core.deps import get_current_admin
    user = User(id=9, username="op", name="BELLAGIO", role=UserRole.MERCHANT,
                merchant_role=merchant_role)
    with pytest.raises(HTTPException) as e:
        await get_current_admin(user)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_a_support_member_cannot_edit_limits():
    from app.core.deps import get_current_admin
    user = User(id=9, username="sup", name="Support", role=UserRole.SUPPORT_AGENT)
    with pytest.raises(HTTPException) as e:
        await get_current_admin(user)
    assert e.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
async def test_admin_roles_are_admitted(db, role):
    from app.core.deps import get_current_admin
    user = _admin(role=role)
    assert await get_current_admin(user) is user

    acc = await _account(db)
    out = await _set_limits(db, acc.reference_number, 1000000, 200000, actor=user)
    assert out["highestCredit"] == 1000000.0


@pytest.mark.asyncio
async def test_the_route_is_declared_behind_the_admin_dependency():
    """Belt and braces: the gate is wired to the endpoint, not merely available to it."""
    import inspect
    from app.core.deps import get_current_admin

    actor = inspect.signature(acct.update_account_limits).parameters["actor"]
    assert actor.default.dependency is get_current_admin
