"""Admin Override & Send — the deliberate, audited exception to a manual deposit send's credit rules.

A manual account send normally obeys the SAME two hard credit rules the allocation engine does — a
per-transaction Highest Credit and a cumulative Bank Daily Credit Limit. When a customer explicitly
asks for an account (same/last, a specific bank, a specific number or name) that cannot take the
money under those limits, the request waits in ACCOUNT_REQUESTED and an Admin may deliberately
choose to honour it with Override & Send.

What this suite pins (numbers refer to the requirement list it was written against):

  BUTTON      An eligible account is a normal Send; an over-limit account needs a deliberate
              Override & Send. The backend blocks the normal send and lets the override through.
  SCOPE       Override bypasses ONLY the two credit-capacity rules, for ONE transaction. It never
              touches an inactive / deleted / wrong-type / unsupported-mode account, and never
              changes the account's configured limits.
  USAGE       The overridden deposit still counts against the day. Remaining can go negative and is
              not clamped; future automatic allocation still rejects the account for being over.
  REASON      Mandatory, and validated server-side — empty, whitespace and missing are all refused.
  AUDIT       Every successful override writes one immutable journal row capturing the full state.
  IDEMPOTENT  A repeated confirm returns the placed state and writes no second row.
  PRIVACY     The merchant payload never carries the override, the reason, or the internal limits.

Real Postgres concurrency (§32) lives in test_postgres_concurrency.py. Automation is forced ON by
conftest.

    python -m pytest tests/test_admin_account_override.py -v
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import transactions as txr
from app.core.deps import get_current_admin
from app.models.models import (
    AccountMaster, DepositAccountOverride, Transaction, TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import AccountOverrideRequest
from app.services import deposit_allocation as alloc

from tests.test_deposit_allocation import db, safe_refs_and_no_cache  # noqa: F401  (fixtures)
from tests.test_account_request_workflow import (  # noqa: F401
    telegram, _acc, _row, _merchant, _admin, _dep, _txid, TODAY,
)


# ── Builders ─────────────────────────────────────────────────────────────────────────────────────

_SEQ = {"n": 0}


async def _req(db, amount, *, note=None, member="MBR1", deposit_type="IMPS",
               status=TxStatus.ACCOUNT_REQUESTED) -> Transaction:
    """A deposit request sitting in the Admin's manual queue (ACCOUNT_REQUESTED), no account yet."""
    _SEQ["n"] += 1
    return await _row(db, f"ovr{_SEQ['n']}", amount, None, member=member, status=status,
                      type_=TxType.DEPOSIT_REQUEST, deposit_type=deposit_type, notes=note,
                      member_name="Member One")


async def _get_admin(db) -> User:
    """One Admin per database — reused across repeated calls so a second confirm in the same test
    does not collide on the fixed id the builder assigns."""
    existing = (await db.execute(select(User).where(User.id == 1))).scalar_one_or_none()
    return existing or await _admin(db)


async def _override(db, tx, ref, reason, admin=None):
    return await txr.override_account(
        _txid(tx), AccountOverrideRequest(adminRef=ref, reason=reason), db,
        admin or await _get_admin(db))


async def _submit(db, tx, ref, admin=None):
    from app.schemas.schemas import AccountSubmitRequest
    return await txr.account_submit(_txid(tx), AccountSubmitRequest(adminRef=ref), db,
                                    admin or await _get_admin(db))


async def _used(db, ref) -> float:
    return round((await alloc.credit_used_today(db, [ref])).get(ref, 0.0), 2)


# ═══ 1-4  Eligibility: normal send works; over-limit blocks the normal send ═══════════════════════

@pytest.mark.asyncio
async def test_1_eligible_account_normal_send_works(db, telegram):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    out = await _submit(db, tx, "ACC0000001")
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC0000001"
    assert (await db.execute(select(DepositAccountOverride))).scalars().all() == []


@pytest.mark.asyncio
async def test_2_highest_credit_exceeded_blocks_the_normal_send(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _submit(db, tx, "ACC0000001")
    assert e.value.status_code == 400 and "Highest Credit" in e.value.detail


@pytest.mark.asyncio
async def test_3_daily_credit_exceeded_blocks_the_normal_send(db, telegram):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _submit(db, tx, "ACC0000001")
    assert e.value.status_code == 400 and "Bank Daily Credit Limit" in e.value.detail


@pytest.mark.asyncio
async def test_4_both_limits_exceeded_blocks_the_normal_send(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _submit(db, tx, "ACC0000001")
    assert e.value.status_code == 400            # Highest Credit reported first, then daily


# ═══ 5-7  Override succeeds for each breach ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_5_override_succeeds_for_highest_credit(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    out = await _override(db, tx, "ACC0000001", "Customer requested this account.")
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED and out["adminRef"] == "ACC0000001"
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.highest_credit_exceeded and not row.daily_credit_exceeded


@pytest.mark.asyncio
async def test_6_override_succeeds_for_daily_credit(db, telegram):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000)
    out = await _override(db, tx, "ACC0000001", "Customer requested same account.")
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.daily_credit_exceeded and not row.highest_credit_exceeded


@pytest.mark.asyncio
async def test_7_override_succeeds_when_both_limits_exceeded(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000)
    out = await _override(db, tx, "ACC0000001", "Approved by Admin after customer confirmation.")
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.highest_credit_exceeded and row.daily_credit_exceeded
    assert row.exceeded_limits == "Highest Credit, Bank Daily Credit Limit"


# ═══ 8-10  Reason is mandatory and validated by the BACKEND ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_8_empty_reason_rejected(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _override(db, tx, "ACC0000001", "")
    assert e.value.status_code == 400
    assert (await db.execute(select(DepositAccountOverride))).scalars().all() == []


@pytest.mark.asyncio
async def test_9_whitespace_only_reason_rejected(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _override(db, tx, "ACC0000001", "   \t\n ")
    assert e.value.status_code == 400
    assert (await db.execute(select(DepositAccountOverride))).scalars().all() == []


def test_10_reason_is_a_required_field_so_it_cannot_be_omitted():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AccountOverrideRequest(adminRef="ACC0000001")     # no reason


# ═══ 11-12  Authorization — Admin only ════════════════════════════════════════════════════════════

def test_11_12_override_is_admin_only():
    """The route is gated on get_current_admin, so a merchant, an agent, or any other role is denied
    server-side by the dependency before the handler runs."""
    dep = inspect.signature(txr.override_account).parameters["actor"].default
    assert dep.dependency is get_current_admin


# ═══ 13  Fundamental validations are NOT bypassed ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_13_inactive_account_cannot_be_overridden(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0, status="INACTIVE")
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _override(db, tx, "ACC0000001", "Customer requested this account.")
    assert e.value.status_code == 400 and "cannot receive a deposit" in e.value.detail
    assert (await db.execute(select(DepositAccountOverride))).scalars().all() == []


@pytest.mark.asyncio
async def test_13_nonexistent_account_cannot_be_overridden(db, telegram):
    tx = await _req(db, 46000)
    with pytest.raises(HTTPException) as e:
        await _override(db, tx, "ACC9999999", "Customer requested this account.")
    assert e.value.status_code == 400 and "does not exist" in e.value.detail


@pytest.mark.asyncio
async def test_13_card_and_cdm_do_not_take_this_route(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    card = await _req(db, 46000, deposit_type="CARD")
    with pytest.raises(HTTPException) as e:
        await _override(db, card, "ACC0000001", "x")
    assert e.value.status_code == 400
    cdm_tx = await _req(db, 46000, deposit_type="CDM")
    with pytest.raises(HTTPException) as e2:
        await _override(db, cdm_tx, "ACC0000001", "x")
    assert e2.value.status_code == 400


# ═══ 14-17  Any explicitly-requested account may be overridden (never silently substituted) ═══════

@pytest.mark.asyncio
async def test_14_exact_requested_account_can_be_overridden(db, telegram):
    await _acc(db, "ACC0000001", name="GURUBACHAN SINGH", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000, note="Use account ACC0000001")
    out = await _override(db, tx, "ACC0000001", "Customer requested this account number.")
    assert out["adminRef"] == "ACC0000001"
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.parsed_request == alloc.REQUEST_EXACT_ACCOUNT


@pytest.mark.asyncio
async def test_15_same_last_requested_account_can_be_overridden(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000, note="same account")
    out = await _override(db, tx, "ACC0000001", "Customer requested same account.")
    assert out["adminRef"] == "ACC0000001"
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.parsed_request == alloc.REQUEST_SAME_ACCOUNT


@pytest.mark.asyncio
async def test_16_specific_bank_account_can_be_overridden(db, telegram):
    await _acc(db, "ACC0000001", bank="HDFC Bank", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000, note="Use HDFC")
    out = await _override(db, tx, "ACC0000001", "Customer requested HDFC account.")
    assert out["adminRef"] == "ACC0000001"
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.parsed_request == alloc.REQUEST_BANK


@pytest.mark.asyncio
async def test_17_own_account_can_be_explicitly_overridden(db, telegram):
    acc = await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    acc.is_own_account = True
    await db.flush()
    tx = await _req(db, 46000, note="same account")
    out = await _override(db, tx, "ACC0000001", "Customer requested their own account.")
    assert out["adminRef"] == "ACC0000001"
    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.is_own_account is True
    # Own-account preference is untouched — the override does not flip the flag off.
    refreshed = (await db.execute(select(AccountMaster).where(
        AccountMaster.reference_number == "ACC0000001"))).scalar_one()
    assert refreshed.is_own_account is True


# ═══ 18-22  Limits unchanged; usage real; remaining can go negative; automation still respects ════

@pytest.mark.asyncio
async def test_18_19_configured_limits_are_unchanged_by_the_override(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    await _override(db, tx, "ACC0000001", "Customer requested this account.")
    acc = (await db.execute(select(AccountMaster).where(
        AccountMaster.reference_number == "ACC0000001"))).scalar_one()
    assert acc.highest_credit == 26000.0             # NOT raised to 46,000
    assert acc.bank_daily_credit_limit == 100000.0   # unchanged


@pytest.mark.asyncio
async def test_20_21_actual_daily_usage_increases_and_remaining_goes_negative(db, telegram):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    assert await _used(db, "ACC0000001") == 90000.0
    tx = await _req(db, 46000)
    await _override(db, tx, "ACC0000001", "Customer requested same account.")
    # 90,000 + 46,000 = 1,36,000 used → remaining 1,00,000 − 1,36,000 = −36,000, not clamped.
    assert await _used(db, "ACC0000001") == 136000.0
    assert 100000.0 - await _used(db, "ACC0000001") == -36000.0


@pytest.mark.asyncio
async def test_22_future_automatic_allocation_still_respects_the_configured_limit(db, telegram):
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000)
    await _override(db, tx, "ACC0000001", "Customer requested same account.")
    # The account is now at 1,36,000 of a 1,00,000 daily limit → a fresh ₹5,000 request must NOT be
    # auto-allocated to it just because the last one was overridden.
    result = await alloc.allocate_deposit_account(db, amount=5000, member_id="SOMEONE")
    assert result.allocated is False


# ═══ 23-30  The audit record captures the exact state at override time ════════════════════════════

@pytest.mark.asyncio
async def test_23_to_30_override_creates_a_full_audit_record(db, telegram):
    await _acc(db, "ACC0000001", name="GURUBACHAN SINGH", bank="Union Bank of India",
               credit=26000.0, daily_credit=100000.0)
    await _row(db, "used", 90000, "ACC0000001", member="OTHER", assigned_on=TODAY)
    tx = await _req(db, 46000, note="Use ACC0000001", member="MID000123")
    admin = await _admin(db)
    await _override(db, tx, "ACC0000001", "Customer specifically requested same account.", admin)

    row = (await db.execute(select(DepositAccountOverride))).scalars().one()
    assert row.override is True                                              # 23 override flag
    assert row.admin_id == admin.id and row.admin_username == admin.username # 24 admin identity
    assert row.admin_name == admin.name
    assert row.created_at is not None and row.created_at_ist                 # 25 timestamp
    assert row.reason == "Customer specifically requested same account."     # 26 reason
    assert row.configured_highest_credit == 26000.0                          # 27 original limits
    assert row.configured_bank_daily_credit_limit == 100000.0
    assert row.credit_used_today == 90000.0 and row.credit_remaining_today == 10000.0
    assert row.account_ref == "ACC0000001" and row.account_name == "GURUBACHAN SINGH"  # 28 account
    assert row.bank_name == "Union Bank of India"
    assert row.amount == 46000.0                                             # 29 amount
    assert row.highest_credit_exceeded and row.daily_credit_exceeded         # 30 which exceeded
    assert row.transaction_ref == tx.ref and row.member_id == "MID000123"


# ═══ 31  Idempotency ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_31_duplicate_override_is_safely_handled(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    first = await _override(db, tx, "ACC0000001", "Customer requested this account.")
    second = await _override(db, tx, "ACC0000001", "Customer requested this account.")
    assert first["status"] == second["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert second["adminRef"] == "ACC0000001"
    # Exactly ONE audit row and ONE assignment — the second confirm is a no-op.
    assert len((await db.execute(select(DepositAccountOverride))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_31_override_on_a_moved_on_request_is_refused(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000, status=TxStatus.DEPOSITED)     # already completed, not in the queue
    with pytest.raises(HTTPException) as e:
        await _override(db, tx, "ACC0000001", "Customer requested this account.")
    assert e.value.status_code == 400


# ═══ 33  The merchant payload never exposes the override ══════════════════════════════════════════

@pytest.mark.asyncio
async def test_33_merchant_response_does_not_expose_override_details(db, telegram):
    await _acc(db, "ACC0000001", credit=26000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    await _override(db, tx, "ACC0000001", "Customer requested this account.")
    await txr._refresh_with_images(db, tx)
    merchant_view = txr._t(tx, full=False)
    for leak in ("override", "overrideReason", "reason", "highestCredit", "bankDailyCreditLimit",
                 "creditUsedToday", "exceededLimits"):
        assert leak not in merchant_view, f"{leak} must never reach the merchant"


# ═══ Race resolved before confirm → no override needed, no override row ═══════════════════════════

@pytest.mark.asyncio
async def test_override_endpoint_on_a_now_eligible_account_places_it_without_an_override_row(
        db, telegram):
    """If usage fell back under the limit between the Admin's screen and the confirm, there is
    nothing to override: the account is placed normally and NO override row is written."""
    await _acc(db, "ACC0000001", credit=100000.0, daily_credit=100000.0)
    tx = await _req(db, 46000)
    out = await _override(db, tx, "ACC0000001", "Customer requested this account.")
    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert (await db.execute(select(DepositAccountOverride))).scalars().all() == []
