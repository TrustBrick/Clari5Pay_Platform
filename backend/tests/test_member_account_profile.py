"""A member's sending account: SAVINGS/CURRENT asked once, and NEW/OLD that cannot be claimed.

Two properties are worth more than the rest here, because both cost money or trust when wrong:

  1. **NEW/OLD is a fact, not a field.** It is counted from the account's own RECEIVED deposits
     every time, so a merchant cannot present an account that has funded us as new. The browser
     may send whatever it likes; the server answers from the database.
  2. **The question is asked once.** An account whose type is already recorded is never asked
     again and never duplicated, however differently it is typed the second time.

The classification is per ACCOUNT, not per member — the same person can hold one account that has
never paid us beside one that has — and it is driven by DEPOSITS ONLY, in the statuses the ledger
already treats as received. A pending, rejected or cancelled request leaves the account NEW,
because no money arrived.

Run from the backend directory:

    python -m pytest tests/test_member_account_profile.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.routes import transactions as txr
from app.api.routes import bank_accounts as bankr
from app.models.models import MerchantBankAccount, Transaction, TxStatus, TxType
from app.schemas.schemas import DepositCreate, WithdrawalCreate
from app.services import member_account as macct
from sqlalchemy import select, func

from tests.test_withdrawal_allocation import (  # noqa: F401  (fixtures)
    db, safe_refs_and_no_cache, _account, _admin, _fund, _merchant,
)

UPI_A = "satish@ybl"
UPI_B = "satish@okaxis"


# ── builders ───────────────────────────────────────────────────────────────────────────────────

def _dep(amount=5000.0, **kw) -> DepositCreate:
    base = dict(amount=amount, depositType="UPI", memberName="Satish", memberId="MBR20240001",
                senderUpiId=UPI_A)
    base.update(kw)
    return DepositCreate(**base)


def _wd(amount=5000.0, **kw) -> WithdrawalCreate:
    base = dict(amount=amount, memberId="MBR20240001", memberName="Satish", payoutMode="UPI",
                payoutDetails={"upiId": UPI_A})
    base.update(kw)
    return WithdrawalCreate(**base)


async def _received_deposit(db, merchant, ref: str, *, upi=None, number=None,
                            member="MBR20240001", status=TxStatus.DEPOSITED):
    """A deposit in whatever status the test needs, attributed to one sending account."""
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=5000.0, status=status,
        merchant_id=merchant.id, merchant_name=merchant.name,
        tx_date=datetime.utcnow().date(), tx_time="10:00:00",
        member_id=member, member_name="Satish",
        sender_upi_id=upi, account_number=number, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _view(db, merchant, *, upi=UPI_A, number=None, member="MBR20240001"):
    ident = macct.identity_from(member_id=member, upi_id=upi, account_number=number)
    return await macct.describe(db, merchant, ident)


# ═══ 1-2. A brand-new account is NEW, whichever type it is ══════════════════════════════════════

@pytest.mark.parametrize("chosen,stored", [("SAVINGS", "SAVINGS"), ("CURRENT", "CURRENT")])
@pytest.mark.asyncio
async def test_a_new_account_is_new_and_remembers_the_type_chosen(db, chosen, stored):
    merchant = await _merchant(db)
    out = await txr.create_deposit(_dep(accountType=chosen), db, merchant)

    assert out["accountProfile"] == "NEW", "nothing has ever funded us from this account"
    assert out["accountType"] == stored
    # And it is now remembered against the saved account, not just on the request.
    v = await _view(db, merchant)
    assert v.account_type == stored
    assert v.needs_account_type is False


# ═══ 3-4. A known account is never asked again ══════════════════════════════════════════════════

@pytest.mark.parametrize("stored", ["SAVINGS", "CURRENT"])
@pytest.mark.asyncio
async def test_a_saved_account_type_is_reused_without_asking(db, stored):
    merchant = await _merchant(db)
    await txr.create_deposit(_dep(accountType=stored), db, merchant)

    # Second request sends NO account type at all — and is accepted, because the answer is known.
    out = await txr.create_deposit(_dep(amount=7000.0), db, merchant)
    assert out["accountType"] == stored
    v = await _view(db, merchant)
    assert v.needs_account_type is False


@pytest.mark.asyncio
async def test_a_saved_type_cannot_be_changed_by_re_using_the_account(db):
    """Re-use must never silently reclassify an account — the first recorded answer stands."""
    merchant = await _merchant(db)
    await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    out = await txr.create_deposit(_dep(amount=7000.0, accountType="CURRENT"), db, merchant)
    assert out["accountType"] == "SAVINGS", "the stored type wins over the payload"
    assert (await _view(db, merchant)).account_type == "SAVINGS"


# ═══ 5-7, 11. NEW/OLD counted from RECEIVED deposits ════════════════════════════════════════════

@pytest.mark.asyncio
async def test_an_account_with_no_received_deposit_is_new(db):
    merchant = await _merchant(db)
    v = await _view(db, merchant)
    assert v.successful_deposits == 0 and v.profile == "NEW"


@pytest.mark.parametrize("n", [1, 2, 5])
@pytest.mark.asyncio
async def test_one_or_more_received_deposits_makes_the_account_old(db, n):
    merchant = await _merchant(db)
    for i in range(n):
        await _received_deposit(db, merchant, f"D{i}", upi=UPI_A)

    v = await _view(db, merchant)
    assert v.successful_deposits == n
    assert v.profile == "OLD"


@pytest.mark.asyncio
async def test_a_received_deposit_makes_the_NEXT_request_old(db):
    """The transition, end to end: raise, receive, raise again."""
    merchant = await _merchant(db)
    first = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    assert first["accountProfile"] == "NEW"

    tx = (await db.execute(select(Transaction).where(
        Transaction.ref == first["ref"]))).scalar_one()
    tx.status = TxStatus.DEPOSITED          # the money arrived
    await db.flush()

    second = await txr.create_deposit(_dep(amount=9000.0), db, merchant)
    assert second["accountProfile"] == "OLD"


# ═══ 8-10. A request that never delivered money leaves the account NEW ══════════════════════════

@pytest.mark.parametrize("status", [
    TxStatus.ACCOUNT_REQUESTED,     # pending — waiting for an account
    TxStatus.ACCOUNT_SUBMITTED,     # pending — waiting to be paid
    TxStatus.SLIP_SUBMITTED,        # pending — waiting for review
    TxStatus.REJECTED,
    TxStatus.CANCELLED,
])
@pytest.mark.asyncio
async def test_a_deposit_that_never_arrived_does_not_make_the_account_old(db, status):
    merchant = await _merchant(db)
    await _received_deposit(db, merchant, "D1", upi=UPI_A, status=status)

    v = await _view(db, merchant)
    assert v.successful_deposits == 0, f"{status.value} is not money received"
    assert v.profile == "NEW"


@pytest.mark.asyncio
async def test_a_withdrawal_never_makes_a_sending_account_old(db):
    """Money leaving says nothing about the account having funded us."""
    merchant = await _merchant(db)
    tx = Transaction(
        ref="W1", type=TxType.WITHDRAWAL_REQUEST, amount=5000.0, status=TxStatus.COMPLETED,
        merchant_id=merchant.id, merchant_name=merchant.name,
        tx_date=datetime.utcnow().date(), tx_time="10:00:00",
        member_id="MBR20240001", account_number="999", created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()

    v = await _view(db, merchant, upi=None, number="999")
    assert v.profile == "NEW"


# ═══ 12. Two accounts, one member, different standings ══════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_classification_is_per_account_not_per_member(db):
    """The requirement in one test: Satish's two UPIs must not share a verdict."""
    merchant = await _merchant(db)
    await _received_deposit(db, merchant, "D1", upi=UPI_B)      # only B has funded us

    a = await _view(db, merchant, upi=UPI_A)
    b = await _view(db, merchant, upi=UPI_B)
    assert (a.profile, b.profile) == ("NEW", "OLD")


# ═══ 13. Legacy rows ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_legacy_account_without_a_type_is_asked_once_then_never_again(db):
    """An account saved before this field existed must not be guessed at."""
    merchant = await _merchant(db)
    db.add(MerchantBankAccount(
        merchant_id=merchant.id, member_id="MBR20240001", upi_id=UPI_A, account_type=None))
    await db.flush()

    v = await _view(db, merchant)
    assert v.exists is True and v.account_type is None
    assert v.needs_account_type is True, "ask, do not guess"

    # A request that does not answer is refused rather than defaulted.
    with pytest.raises(HTTPException) as e:
        await txr.create_deposit(_dep(), db, merchant)
    assert e.value.status_code == 400

    out = await txr.create_deposit(_dep(accountType="CURRENT"), db, merchant)
    assert out["accountType"] == "CURRENT"
    after = await _view(db, merchant)
    assert after.needs_account_type is False
    # The legacy row was UPDATED, not replaced.
    assert after.account.id is not None
    assert (await db.execute(select(func.count()).select_from(MerchantBankAccount).where(
        MerchantBankAccount.member_id == "MBR20240001"))).scalar() == 1


# ═══ 14-15. What the Admin receives ═════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_admin_payload_carries_the_account_type_for_a_deposit(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    created = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    out = await txr.get_transaction_detail(created["id"], db, admin)
    assert out["accountType"] == "SAVINGS"
    assert out["accountTypeLabel"] == "Savings Account"
    assert out["accountProfile"] == "NEW"
    assert out["senderUpiId"] == UPI_A
    assert out["memberProfileType"] == "NEW"


@pytest.mark.asyncio
async def test_the_admin_payload_carries_the_account_type_for_a_withdrawal(db):
    merchant = await _merchant(db)
    admin = await _admin(db)
    await _account(db, "ACC1", debit=100000.0)
    await _fund(db, "F1", "ACC1", 500000.0)
    await _received_deposit(db, merchant, "D1", upi=UPI_A)      # the account has funded us

    created = await txr.create_withdrawal(_wd(accountType="CURRENT"), None, db, merchant)
    out = await txr.get_transaction_detail(created["id"], db, admin)
    assert out["accountType"] == "CURRENT"
    assert out["accountTypeLabel"] == "Current Account"
    assert out["accountProfile"] == "OLD", "counted from the DEPOSIT history of the same account"


# ═══ 16. The merchant is not the authority ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_merchant_cannot_claim_a_funded_account_is_new(db):
    merchant = await _merchant(db)
    await _received_deposit(db, merchant, "D1", upi=UPI_A)

    out = await txr.create_deposit(
        _dep(profile="NEW", accountType="SAVINGS"), db, merchant)
    assert out["accountProfile"] == "OLD", "the server counted; the payload was ignored"


@pytest.mark.asyncio
async def test_a_merchant_cannot_claim_an_unfunded_account_is_old(db):
    merchant = await _merchant(db)
    out = await txr.create_deposit(_dep(profile="OLD", accountType="SAVINGS"), db, merchant)
    assert out["accountProfile"] == "NEW"


@pytest.mark.asyncio
async def test_an_invalid_account_type_is_refused(db):
    merchant = await _merchant(db)
    with pytest.raises(HTTPException) as e:
        await txr.create_deposit(_dep(accountType="OVERDRAFT"), db, merchant)
    assert e.value.status_code == 400


# ═══ 17. Identity: one account, however it is typed ═════════════════════════════════════════════

@pytest.mark.parametrize("typed", ["satish@ybl", "  satish@ybl  ", "Satish@YBL", "SATISH@ybl"])
@pytest.mark.asyncio
async def test_the_same_upi_typed_differently_is_one_account(db, typed):
    merchant = await _merchant(db)
    await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    out = await txr.create_deposit(_dep(amount=7000.0, senderUpiId=typed), db, merchant)
    assert out["accountType"] == "SAVINGS", "resolved to the saved account, not asked again"
    n = (await db.execute(select(func.count()).select_from(MerchantBankAccount).where(
        MerchantBankAccount.member_id == "MBR20240001"))).scalar()
    assert n == 1, "re-using an account must not create a second row"


@pytest.mark.parametrize("typed", ["12 34 5678", "1234-5678", "12345678"])
@pytest.mark.asyncio
async def test_a_bank_account_number_is_matched_in_canonical_form(db, typed):
    """Spaces and dashes are formatting, not identity — the payout engine's own rule."""
    merchant = await _merchant(db)
    await _received_deposit(db, merchant, "D1", upi=None, number="12345678")

    v = await _view(db, merchant, upi=None, number=typed)
    assert v.profile == "OLD"


# ═══ The resolve endpoint the form asks before it renders ═══════════════════════════════════════

@pytest.mark.asyncio
async def test_the_resolve_endpoint_answers_all_three_questions(db):
    merchant = await _merchant(db)

    fresh = await bankr.resolve_member_account("MBR20240001", UPI_A, None, db, merchant)
    assert fresh["exists"] is False
    assert fresh["needsAccountType"] is True
    assert fresh["profile"] == "NEW"

    await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)
    await _received_deposit(db, merchant, "D9", upi=UPI_A)

    known = await bankr.resolve_member_account("MBR20240001", UPI_A, None, db, merchant)
    assert known["exists"] is True
    assert known["needsAccountType"] is False
    assert known["accountType"] == "SAVINGS"
    assert known["accountTypeLabel"] == "Savings Account"
    assert known["profile"] == "OLD"


@pytest.mark.asyncio
async def test_normalisation_accepts_the_labels_the_ui_shows(db):
    assert macct.normalize_account_type("Savings Account") == "SAVINGS"
    assert macct.normalize_account_type("current account") == "CURRENT"
    assert macct.normalize_account_type("savings") == "SAVINGS"
    assert macct.normalize_account_type("") is None
    assert macct.normalize_account_type("OVERDRAFT") is None


# ═══ Legacy rows: a blank beats a plausible wrong answer ════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_request_raised_before_this_feature_reports_no_profile(db):
    """A row with no stored snapshot must NOT be re-derived from today's history.

    The deposit below was this account's first, and it later completed. Re-deriving would look at
    the account now, find a received deposit, and confidently label the request OLD — the exact
    opposite of what was true when it was raised. So nothing is reported and the screen says
    "Not recorded".
    """
    merchant = await _merchant(db)
    admin = await _admin(db)
    legacy = await _received_deposit(db, merchant, "OLDDEP", upi=UPI_A)
    legacy.account_profile = None          # predates the feature
    legacy.sender_account_type = None
    await db.flush()

    out = await txr.get_transaction_detail(str(legacy.id), db, admin)
    assert out["memberProfileType"] is None, "no honest historical value exists — report none"
    assert out["accountProfile"] is None
    assert out["accountType"] is None


@pytest.mark.asyncio
async def test_a_new_request_still_snapshots_its_own_profile(db):
    """The counterpart: anything raised from now on carries its own answer."""
    merchant = await _merchant(db)
    admin = await _admin(db)
    created = await txr.create_deposit(_dep(accountType="SAVINGS"), db, merchant)

    tx = (await db.execute(select(Transaction).where(
        Transaction.ref == created["ref"]))).scalar_one()
    tx.status = TxStatus.DEPOSITED          # completing must not rewrite the stored snapshot
    await db.flush()

    out = await txr.get_transaction_detail(created["id"], db, admin)
    assert out["memberProfileType"] == "NEW", "it was the account's first — and stays so"
    assert out["accountProfile"] == "NEW"


# ═══ One identity for saving AND for history ════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_saving_and_history_resolve_an_account_the_same_way(db):
    """The two lookups must agree, or an account saved under one spelling would be classified
    under another and read as NEW forever."""
    merchant = await _merchant(db)
    # Saved from one spelling, funded under a different one.
    await txr.create_deposit(_dep(accountType="SAVINGS", senderUpiId="Satish@YBL"), db, merchant)
    await _received_deposit(db, merchant, "D1", upi="  SATISH@ybl  ")

    for typed in ("satish@ybl", "Satish@YBL", " satish@YBL "):
        v = await _view(db, merchant, upi=typed)
        assert v.exists is True, f"{typed!r} must resolve to the saved account"
        assert v.account_type == "SAVINGS"
        assert v.profile == "OLD", f"{typed!r} must see the same history"
    assert (await db.execute(select(func.count()).select_from(MerchantBankAccount).where(
        MerchantBankAccount.member_id == "MBR20240001"))).scalar() == 1
