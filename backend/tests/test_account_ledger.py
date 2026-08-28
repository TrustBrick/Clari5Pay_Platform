"""Tests for the managed-account accounting ledger, withdrawal payout and manual adjustment.

Three features share one ledger (``account_ledger``) and one authoritative balance
(``services/account_ledger.account_balance``). What that buys in reuse it owes in rigour: money
moves here, so the properties below are pinned down against a REAL database rather than a stub —
SQLite via aiosqlite, with the project's own models and ``Base.metadata.create_all``, so the
schema under test is the schema the app declares.

The properties that matter:

  1. **The balance is derived, never stored** — deposits in, withdrawals/settlements out, plus the
     net of manual adjustments. An adjustment takes effect purely by its ledger entry existing.
  2. **Attribution is explicit when it can be** — a completed withdrawal is charged to the account
     the payout step recorded; only a row without one falls back to the member's receiving account,
     and a MANUAL/offline payout is charged to no account at all.
  3. **Balance before/after are the server's** — computed from the authoritative balance, never
     from anything the caller sends.
  4. **A withdrawal can be paid exactly once** — the ledger's UNIQUE (entry_type, transaction_ref)
     makes a second payout entry impossible at the database level, not merely unlikely.
  5. **A replayed submit is not a second movement** — the client request id is UNIQUE, so a
     double-click resolves to the entry already written.
  6. **Validation is server-side** — bad amounts, unknown reasons, unknown payment methods,
     inactive accounts, missing references and overdrawing debits are all refused, and refused
     before anything is written.
  7. **RBAC is unchanged** — completing a withdrawal and adjusting an account both sit behind the
     dependencies that already guarded those modules; no merchant role passes either.

Run from the backend directory:

    python -m pytest tests/test_account_ledger.py -v
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.models import (
    AccountLedgerEntry, AccountMaster, AccountTransaction, AccountType,
    Transaction, TxStatus, TxType, User, UserRole,
)
from app.services import account_ledger as ledger


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    """A real, empty database built from the project's own models.

    SQLite has no sequences, so the one raw ``nextval`` the service issues is stubbed with a
    counter for the duration of the test. Everything else — the tables, the UNIQUE constraints,
    the SQL the balance is computed with — is the production definition.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    counter = {"n": 0}

    async def _fake_ref(_db, entry_type):
        counter["n"] += 1
        return f"{ledger._ENTRY_PREFIX.get(entry_type, 'LED')}{counter['n']:06d}"

    real = ledger._next_entry_ref
    ledger._next_entry_ref = _fake_ref
    async with Session() as session:
        yield session
    ledger._next_entry_ref = real
    await engine.dispose()


async def _account(db: AsyncSession, ref="ACC0000001", *, status="ACTIVE", name="HDFC Main",
                   number="12345678908890") -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name=name, account_number=number, ifsc_code="HDFC0001234",
        bank_name="HDFC Bank", branch="Mumbai", account_type=AccountType.CURRENT, status=status,
        created_date=date.today(), created_time="10:00:00",
    )
    db.add(acc)
    await db.flush()
    return acc


async def _deposit(db: AsyncSession, ref: str, amount: float, account_ref: str,
                   *, member="WININ25504", status=TxStatus.DEPOSITED) -> Transaction:
    tx = Transaction(
        ref=ref, type=TxType.DEPOSIT_REQUEST, amount=amount, status=status, merchant_id=7,
        merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id=member, admin_ref=account_ref, created_at=datetime.utcnow(),
    )
    db.add(tx)
    await db.flush()
    return tx


async def _withdrawal(db: AsyncSession, ref: str, amount: float, *, member="WININ25504",
                      status=TxStatus.SLIP_SUBMITTED, payout_ref=None, method=None) -> Transaction:
    tx = Transaction(
        ref=ref, type=TxType.WITHDRAWAL_REQUEST, amount=amount, status=status, merchant_id=7,
        merchant_name="BELLAGIO", tx_date=date.today(), tx_time="10:00:00",
        member_id=member, payout_mode="BANK", created_at=datetime.utcnow(),
        payout_account_ref=payout_ref, payout_payment_method=method,
    )
    db.add(tx)
    await db.flush()
    return tx


def _admin(uid=1) -> User:
    return User(id=uid, username="admin1", name="Admin One", role=UserRole.ADMIN)


# ── 1. The balance is derived, never stored ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_balance_is_deposits_minus_debits(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    assert await ledger.account_balance(db, acc.reference_number) == 200000.0

    # A completed withdrawal explicitly paid from this account reduces it.
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED,
                      payout_ref=acc.reference_number, method="BANK")
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0


@pytest.mark.asyncio
async def test_only_completed_debits_count(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)
    # Still awaiting completion — it must not reduce the balance yet.
    await _withdrawal(db, "WIT000001", 40000, status=TxStatus.SLIP_SUBMITTED,
                      payout_ref=acc.reference_number, method="BANK")
    assert await ledger.account_balance(db, acc.reference_number) == 100000.0


@pytest.mark.asyncio
async def test_adjustments_move_the_balance(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 125000, acc.reference_number)

    await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.CREDIT, amount=25000,
        account=acc, balance_before=125000.0, reason="Interest Credit",
    )
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0

    await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.DEBIT, amount=10000,
        account=acc, balance_before=150000.0, reason="Bank Charges",
    )
    assert await ledger.account_balance(db, acc.reference_number) == 140000.0


@pytest.mark.asyncio
async def test_a_payout_entry_does_not_double_count(db):
    """The payout ledger entry RECORDS the debit; the transaction is what carries it.

    Posting the entry must not subtract the amount a second time, or completing a withdrawal would
    take twice the money out of the account.
    """
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED,
                           payout_ref=acc.reference_number, method="BANK")
    await ledger.post_entry(
        db, entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=50000,
        account=acc, balance_before=200000.0, transaction_ref=tx.ref, payment_method="BANK",
    )
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0


# ── 2. Attribution ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_payout_account_wins_over_the_member_map(db):
    """A withdrawal is charged to the account it was actually paid from, not the guessed one."""
    paid_from = await _account(db, "ACC0000001", name="HDFC Main")
    receiving = await _account(db, "ACC0000002", name="Bank of Baroda", number="99999999994521")
    await _deposit(db, "DEP000001", 200000, paid_from.reference_number)
    await _deposit(db, "DEP000002", 200000, receiving.reference_number, member="WININ99999")
    # The member's receiving account is the SECOND one …
    db.add(AccountTransaction(reference_number=receiving.reference_number, member_id="WININ25504",
                              transaction_date=date.today(), transaction_time="10:00:00"))
    await db.flush()
    # … but the payout was explicitly made from the first.
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED,
                      payout_ref=paid_from.reference_number, method="BANK")

    assert await ledger.account_balance(db, paid_from.reference_number) == 150000.0
    assert await ledger.account_balance(db, receiving.reference_number) == 200000.0


@pytest.mark.asyncio
async def test_legacy_withdrawal_falls_back_to_the_member_map(db):
    """A row completed before the payout step existed keeps its historical attribution."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    db.add(AccountTransaction(reference_number=acc.reference_number, member_id="WININ25504",
                              transaction_date=date.today(), transaction_time="10:00:00"))
    await db.flush()
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED)   # no payout_ref, no method
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0


@pytest.mark.asyncio
async def test_manual_payout_debits_no_account(db):
    """An offline payment came out of no managed account, so no account's balance moves."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    db.add(AccountTransaction(reference_number=acc.reference_number, member_id="WININ25504",
                              transaction_date=date.today(), transaction_time="10:00:00"))
    await db.flush()
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED, method="MANUAL")
    assert await ledger.account_balance(db, acc.reference_number) == 200000.0


@pytest.mark.asyncio
async def test_member_id_casing_and_spacing_do_not_break_attribution(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)
    db.add(AccountTransaction(reference_number=acc.reference_number, member_id=" winin25504 ",
                              transaction_date=date.today(), transaction_time="10:00:00"))
    await db.flush()
    await _withdrawal(db, "WIT000001", 40000, status=TxStatus.COMPLETED, member="WININ25504")
    assert await ledger.account_balance(db, acc.reference_number) == 60000.0


# ── 3. Balance before/after are the server's ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_entry_records_the_server_computed_snapshot(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 125000, acc.reference_number)
    before = await ledger.account_balance(db, acc.reference_number)
    entry = await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.DEBIT, amount=10000,
        account=acc, balance_before=before, reason="Offline Payment", reference="OFF12345",
        performed_by="Admin One", performed_by_role="ADMIN",
    )
    assert entry.balance_before == 125000.0
    assert entry.balance_after == 115000.0          # derived here, never sent by a caller
    assert entry.entry_ref.startswith("ADJ")
    assert entry.created_at_ist and entry.created_at_ist.endswith("IST")
    # …and the derived balance now agrees with the snapshot.
    assert await ledger.account_balance(db, acc.reference_number) == 115000.0


@pytest.mark.asyncio
async def test_manual_payout_entry_has_no_account_or_snapshot(db):
    entry = await ledger.post_entry(
        db, entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=50000,
        account=None, balance_before=None, transaction_ref="WIT000001", payment_method="MANUAL",
        reference="OFF12345",
    )
    assert entry.account_ref is None
    assert entry.balance_before is None and entry.balance_after is None


# ── 4. A withdrawal can be paid exactly once ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_second_payout_entry_for_one_withdrawal_is_impossible(db):
    """Enforced by the database, so no amount of retrying or racing can produce a double debit."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    await ledger.post_entry(
        db, entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=50000,
        account=acc, balance_before=200000.0, transaction_ref="WIT000001", payment_method="BANK",
    )
    with pytest.raises(IntegrityError):
        await ledger.post_entry(
            db, entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=50000,
            account=acc, balance_before=150000.0, transaction_ref="WIT000001", payment_method="BANK",
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_many_manual_adjustments_share_a_null_transaction_ref(db):
    """The payout uniqueness must not stop an account from having several adjustments."""
    acc = await _account(db)
    for i in range(3):
        await ledger.post_entry(
            db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.CREDIT, amount=100 * (i + 1),
            account=acc, balance_before=0.0, reason="Other",
        )
    assert await ledger.account_balance(db, acc.reference_number) == 600.0


@pytest.mark.asyncio
async def test_find_payout_entry_locates_the_existing_completion(db):
    acc = await _account(db)
    await ledger.post_entry(
        db, entry_type=ledger.WITHDRAWAL_PAYOUT, direction=ledger.DEBIT, amount=5000,
        account=acc, balance_before=5000.0, transaction_ref="WIT000009", payment_method="BANK",
    )
    found = await ledger.find_payout_entry(db, "WIT000009")
    assert found is not None and found.transaction_ref == "WIT000009"
    assert await ledger.find_payout_entry(db, "WIT000010") is None


# ── 5. A replayed submit is not a second movement ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_request_id_is_unique(db):
    acc = await _account(db)
    await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.CREDIT, amount=1000,
        account=acc, balance_before=0.0, reason="Other", client_request_id="key-1",
    )
    with pytest.raises(IntegrityError):
        await ledger.post_entry(
            db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.CREDIT, amount=1000,
            account=acc, balance_before=1000.0, reason="Other", client_request_id="key-1",
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_replayed_submit_resolves_to_the_existing_entry(db):
    acc = await _account(db)
    first = await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.CREDIT, amount=1000,
        account=acc, balance_before=0.0, reason="Other", client_request_id="key-1",
    )
    again = await ledger.find_by_client_request(db, "key-1")
    assert again is not None and again.id == first.id
    assert await ledger.find_by_client_request(db, "key-2") is None
    assert await ledger.find_by_client_request(db, None) is None


# ── 6. Server-side validation (manual adjustment) ──────────────────────────────────────────────

async def _adjust(db, ref, **kw):
    """Call the adjustment route directly with a minimal request object."""
    from app.api.routes import accounts as acct
    from app.schemas.schemas import AdjustmentCreate

    class _Req:
        client = None

    payload = {"adjustmentType": "DEBIT", "amount": 1000.0, "reason": "Bank Charges"}
    payload.update(kw)
    return await acct.create_adjustment(ref, AdjustmentCreate(**payload), _Req(), db, _admin())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, -0.01])
async def test_adjustment_amount_must_be_positive(db, bad):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number, amount=bad)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_adjustment_type_must_be_credit_or_debit(db):
    acc = await _account(db)
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number, adjustmentType="TRANSFER")
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "Made it up"])
async def test_adjustment_reason_must_be_from_the_list(db, bad):
    acc = await _account(db)
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number, reason=bad)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_adjustment_on_a_missing_account_is_404(db):
    with pytest.raises(HTTPException) as e:
        await _adjust(db, "ACC9999999")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_adjustment_on_an_inactive_account_is_refused(db):
    acc = await _account(db, status="INACTIVE")
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_debit_cannot_drive_the_balance_negative(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 5000, acc.reference_number)
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number, amount=6000.0)
    assert e.value.status_code == 400
    # Nothing was written — the balance is untouched.
    assert await ledger.account_balance(db, acc.reference_number) == 5000.0


@pytest.mark.asyncio
async def test_adjustment_writes_the_entry_and_moves_the_balance(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 125000, acc.reference_number)
    out = await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=10000.0,
                        reason="Offline Payment", reference="OFF12345", remarks="cash paid out")
    assert out["duplicate"] is False
    e = out["entry"]
    assert e["direction"] == "DEBIT" and e["amount"] == 10000.0
    assert e["balanceBefore"] == 125000.0 and e["balanceAfter"] == 115000.0
    assert e["reason"] == "Offline Payment" and e["reference"] == "OFF12345"
    assert e["performedBy"] == "Admin One" and e["performedByRole"] == "ADMIN"
    assert await ledger.account_balance(db, acc.reference_number) == 115000.0


@pytest.mark.asyncio
async def test_adjustment_ignores_a_replayed_submit(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 125000, acc.reference_number)
    first = await _adjust(db, acc.reference_number, adjustmentType="CREDIT", amount=5000.0,
                          reason="Other", clientRequestId="dup-key")
    second = await _adjust(db, acc.reference_number, adjustmentType="CREDIT", amount=5000.0,
                           reason="Other", clientRequestId="dup-key")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["entry"]["entryRef"] == first["entry"]["entryRef"]
    # Credited once, not twice.
    assert await ledger.account_balance(db, acc.reference_number) == 130000.0


@pytest.mark.asyncio
async def test_a_correction_is_a_compensating_entry_not_an_edit(db):
    """History is append-only: correcting a wrong debit leaves BOTH entries on the record."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)
    await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=10000.0, reason="Other")
    await _adjust(db, acc.reference_number, adjustmentType="CREDIT", amount=10000.0,
                  reason="Reconciliation Correction")
    from sqlalchemy import select
    rows = (await db.execute(select(AccountLedgerEntry).order_by(AccountLedgerEntry.id))).scalars().all()
    assert [r.direction for r in rows] == ["DEBIT", "CREDIT"]
    assert await ledger.account_balance(db, acc.reference_number) == 100000.0


# ── 7. Server-side validation (withdrawal payout) ──────────────────────────────────────────────

async def _payout(db, tx, **kw):
    from app.api.routes import transactions as txr
    from app.schemas.schemas import CompleteRequest
    return await txr._record_withdrawal_payout(db, tx, CompleteRequest(**kw), _admin(), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "CHEQUE", "bank ", None])
async def test_payment_method_must_be_bank_or_manual(db, bad):
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod=bad)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_bank_payout_requires_an_account(db):
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod="BANK")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_bank_payout_rejects_an_unknown_account(db):
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod="BANK", payoutAccountRef="ACC9999999")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_bank_payout_rejects_an_inactive_account(db):
    acc = await _account(db, status="INACTIVE")
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod="BANK", payoutAccountRef=acc.reference_number)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_bank_payout_rejects_an_insufficient_balance(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 10000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod="BANK", payoutAccountRef=acc.reference_number)
    assert e.value.status_code == 400
    assert "Insufficient balance" in e.value.detail
    # Nothing recorded on the transaction, so a failed payout leaves no trace of a debit.
    assert tx.payout_account_ref is None and tx.payout_payment_method is None


@pytest.mark.asyncio
async def test_bank_payout_records_the_account_and_the_ledger_entry(db):
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000123", 50000)
    await _payout(db, tx, paymentMethod="BANK", payoutAccountRef=acc.reference_number,
                  payoutRemarks="paid via NEFT")

    assert tx.payout_payment_method == "BANK"
    assert tx.payout_account_ref == acc.reference_number
    assert tx.payout_remarks == "paid via NEFT"

    entry = await ledger.find_payout_entry(db, "WIT000123")
    assert entry is not None
    assert entry.direction == "DEBIT" and entry.amount == 50000.0
    assert entry.balance_before == 200000.0 and entry.balance_after == 150000.0
    assert entry.merchant_business == "BELLAGIO" and entry.member_id == "WININ25504"
    assert entry.performed_by == "Admin One" and entry.performed_by_role == "ADMIN"

    # The transaction is still not COMPLETED here, so the balance only drops once the caller
    # flips the status — which is what makes balance_after the value it will settle at.
    tx.status = TxStatus.COMPLETED
    await db.flush()
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0


@pytest.mark.asyncio
async def test_manual_payout_requires_a_reference(db):
    tx = await _withdrawal(db, "WIT000001", 50000)
    with pytest.raises(HTTPException) as e:
        await _payout(db, tx, paymentMethod="MANUAL", manualReference="   ")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_manual_payout_records_no_account(db):
    tx = await _withdrawal(db, "WIT000001", 50000)
    await _payout(db, tx, paymentMethod="MANUAL", manualReference="OFF12345", payoutRemarks="cash")
    assert tx.payout_payment_method == "MANUAL"
    assert tx.payout_account_ref is None
    assert tx.payout_manual_reference == "OFF12345"
    entry = await ledger.find_payout_entry(db, "WIT000001")
    assert entry.account_ref is None and entry.balance_before is None
    assert entry.reference == "OFF12345" and entry.remarks == "cash"


@pytest.mark.asyncio
async def test_a_repeated_payout_call_is_a_no_op(db):
    """The second call debits nothing and posts nothing — the money moves exactly once."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000123", 50000)
    await _payout(db, tx, paymentMethod="BANK", payoutAccountRef=acc.reference_number)
    await _payout(db, tx, paymentMethod="BANK", payoutAccountRef=acc.reference_number)

    from sqlalchemy import func, select
    n = (await db.execute(
        select(func.count()).select_from(AccountLedgerEntry)
        .where(AccountLedgerEntry.transaction_ref == "WIT000123")
    )).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_mark_done_returns_the_completed_state_without_re_debiting(db, monkeypatch):
    """The outermost duplicate guard: a second "Mark as Done" on an already-COMPLETED withdrawal
    returns the existing state and never re-enters the payout path at all.

    This is the click-twice case as the operator experiences it — the first call completes and
    debits, the second is answered from the row that is already there.
    """
    from app.api.routes import transactions as txr
    from app.schemas.schemas import CompleteRequest

    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    tx = await _withdrawal(db, "WIT000123", 50000, status=TxStatus.COMPLETED,
                           payout_ref=acc.reference_number, method="BANK")

    # If the guard let this through, the payout path would run and raise/insert; make that loud.
    called = {"n": 0}

    async def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("the payout path must not run on an already-completed withdrawal")

    monkeypatch.setattr(txr, "_record_withdrawal_payout", _boom)
    monkeypatch.setattr(txr, "_get_tx", lambda *a, **kw: _return(tx))
    # _refresh_with_images runs for real: the serializer reads deferred image columns, which
    # cannot async-lazy-load, and loading them is exactly what that helper is for.

    out = await txr.mark_done(
        "WIT000123", request=None,
        data=CompleteRequest(paymentMethod="BANK", payoutAccountRef=acc.reference_number),
        db=db, actor=_admin(),
    )
    assert called["n"] == 0
    assert out["status"] == "COMPLETED"
    # The balance moved exactly once.
    assert await ledger.account_balance(db, acc.reference_number) == 150000.0


async def _return(v):
    return v


# ── 8. Concurrency: an adjustment reads the current balance, not a stale one ────────────────────

@pytest.mark.asyncio
async def test_sequential_adjustments_each_start_from_the_previous_result(db):
    """Two operators debiting the same account must not both compute from the opening balance.

    The route reads the balance under the account's row lock, so the second adjustment sees the
    first one's effect. The lock is what serialises concurrent writers; this pins the arithmetic
    that follows it — ₹1,00,000 − ₹30,000 − ₹50,000 = ₹20,000, never ₹50,000 twice over.
    """
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)

    a = await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=30000.0, reason="Other")
    b = await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=50000.0, reason="Other")

    assert a["entry"]["balanceBefore"] == 100000.0 and a["entry"]["balanceAfter"] == 70000.0
    assert b["entry"]["balanceBefore"] == 70000.0 and b["entry"]["balanceAfter"] == 20000.0
    assert await ledger.account_balance(db, acc.reference_number) == 20000.0


@pytest.mark.asyncio
async def test_a_debit_that_would_overdraw_after_a_concurrent_one_is_refused(db):
    """The second operator's debit is rejected on the balance the first one left behind."""
    acc = await _account(db)
    await _deposit(db, "DEP000001", 100000, acc.reference_number)
    await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=80000.0, reason="Other")
    with pytest.raises(HTTPException) as e:
        await _adjust(db, acc.reference_number, adjustmentType="DEBIT", amount=50000.0, reason="Other")
    assert e.value.status_code == 400
    assert await ledger.account_balance(db, acc.reference_number) == 20000.0


@pytest.mark.asyncio
async def test_lock_account_returns_none_for_an_unknown_account(db):
    assert await ledger.lock_account(db, "ACC9999999") is None


# ── 9. RBAC is unchanged ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_role", ["DEO", "SUPERVISOR", "MANAGER",
                                           "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR", None])
async def test_no_merchant_role_can_reach_account_management(merchant_role):
    """Adjustments and payouts sit behind `get_current_admin`, which this pins down directly.

    Ordinary merchants are never granted the ability to debit an account: the dependency the
    Account Management module and the withdrawal completion already used is the only gate, and it
    admits Admin / Super Admin alone.
    """
    from app.core.deps import get_current_admin
    user = User(id=9, username="op", name="BELLAGIO", role=UserRole.MERCHANT,
                merchant_role=merchant_role)
    with pytest.raises(HTTPException) as e:
        await get_current_admin(user)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_support_agent_cannot_reach_account_management():
    from app.core.deps import get_current_admin
    user = User(id=9, username="sup", name="Support", role=UserRole.SUPPORT_AGENT)
    with pytest.raises(HTTPException) as e:
        await get_current_admin(user)
    assert e.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
async def test_admins_pass_the_gate(role):
    from app.core.deps import get_current_admin
    user = User(id=1, username="a", name="A", role=role)
    assert await get_current_admin(user) is user


# ── 10. Serialisation ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_serialize_exposes_the_full_audit_shape(db):
    acc = await _account(db)
    entry = await ledger.post_entry(
        db, entry_type=ledger.MANUAL_ADJUSTMENT, direction=ledger.DEBIT, amount=10000,
        account=acc, balance_before=125000.0, reason="Offline Payment", reference="OFF12345",
        remarks="note", performed_by="Admin One", performed_by_id=1, performed_by_role="ADMIN",
        merchant_business="BELLAGIO",
    )
    out = ledger.serialize(entry)
    for key in ("entryRef", "entryType", "direction", "amount", "accountRef", "balanceBefore",
                "balanceAfter", "reason", "reference", "remarks", "performedBy", "createdAt",
                "createdAtIst", "merchantBusiness"):
        assert key in out
    assert out["balanceAfter"] == 115000.0


# ── 11. Commission is reported, never deducted ─────────────────────────────────────────────────
#
# Commission is the company's PROFIT. It does not leave the bank account when a merchant is paid —
# it stays in it. So the account's cash figure must keep including it, and the commission number is
# reported alongside purely so the merchant-funds vs company-earnings share of that cash is
# visible. These pin the distinction, because getting it backwards would silently understate every
# account's real cash and block legitimate payouts.

async def _merchant(db: AsyncSession, name="BELLAGIO", *, pay_in=5.0, pay_out=5.0, settle=None):
    u = User(username=name.lower(), name=name, email=f"{name.lower()}@x.com", hashed_password="x",
             role=UserRole.MERCHANT, active=True,
             pay_in_fee=pay_in, pay_out_fee=pay_out, settlement_fee=settle)
    db.add(u)
    await db.flush()
    return u


async def _balances(db):
    from app.api.routes import accounts as acct
    return await acct.account_balances(db, None)


@pytest.mark.asyncio
async def test_commission_is_reported_and_split_by_leg(db):
    await _merchant(db, pay_in=5.0, pay_out=5.0)
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED,
                      payout_ref=acc.reference_number, method="BANK")

    r = (await _balances(db))[0]
    assert r["commissionPayIn"] == 10000.0        # 5% of the 200,000 deposited in
    assert r["commissionPayOut"] == 2500.0        # 5% of the 50,000 paid out
    assert r["commission"] == 12500.0


@pytest.mark.asyncio
async def test_commission_is_not_deducted_from_available(db):
    """The property that matters: Available is cash, and cash still contains the profit."""
    await _merchant(db, pay_in=5.0, pay_out=5.0)
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED,
                      payout_ref=acc.reference_number, method="BANK")

    r = (await _balances(db))[0]
    assert r["available"] == 150000.0             # 200,000 - 50,000, fees NOT applied
    assert r["commission"] == 12500.0             # reported, but not subtracted
    # …and the service the payout/adjustment paths validate against agrees with the screen.
    assert await ledger.account_balance(db, acc.reference_number) == r["available"]


@pytest.mark.asyncio
async def test_a_merchant_with_no_configured_fee_earns_no_commission(db):
    await _merchant(db, pay_in=None, pay_out=None)
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    r = (await _balances(db))[0]
    assert r["commission"] == 0.0
    assert r["available"] == 200000.0


@pytest.mark.asyncio
async def test_settlement_commission_uses_the_settlement_fee(db):
    """A settlement is charged at settlement_fee, not the pay-out rate."""
    await _merchant(db, pay_in=0.0, pay_out=5.0, settle=0.5)
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    st = await _withdrawal(db, "SET000001", 100000, status=TxStatus.COMPLETED,
                           payout_ref=acc.reference_number, method="BANK")
    st.type = TxType.SETTLEMENT_REQUEST
    await db.flush()

    r = (await _balances(db))[0]
    assert r["settlements"] == 100000.0
    assert r["commissionPayOut"] == 500.0         # 0.5% settlement fee, not 5% pay-out
    assert r["available"] == 100000.0             # still pure cash


@pytest.mark.asyncio
async def test_a_manual_offline_payout_earns_no_account_commission(db):
    """It touched no managed account, so it contributes neither a debit nor commission here."""
    await _merchant(db, pay_in=0.0, pay_out=5.0)
    acc = await _account(db)
    await _deposit(db, "DEP000001", 200000, acc.reference_number)
    await _withdrawal(db, "WIT000001", 50000, status=TxStatus.COMPLETED, method="MANUAL")

    r = (await _balances(db))[0]
    assert r["available"] == 200000.0
    assert r["commissionPayOut"] == 0.0
