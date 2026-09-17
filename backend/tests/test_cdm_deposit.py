"""Tests for the CDM (Cash Deposit Machine) deposit workflow.

A CDM deposit is physical cash pushed into a machine, and that changes what the platform can
believe. Every other deposit type arrives over a rail that issues a reference the bank can be
asked about; a CDM deposit arrives with a photograph of a slip a machine printed. A photograph
can be edited, reused from another deposit, or be a perfectly genuine receipt for cash paid into
somebody else's account — so "a receipt was uploaded" must never become "the merchant was paid".

The properties worth pinning down are the ones that would cost real money if they broke:

  1. **The allocation engine never touches a CDM request.** It ranks accounts by remaining daily
     credit capacity at the instant of the request, which is meaningless for a payer who will walk
     to a machine later. No account is assigned at creation; an Admin assigns one.
  2. **Only an Admin assigns the receiving account**, and it must be a real managed account — not
     a UPI ID (which cannot take cash) and not free-typed details (which would sit outside
     Account Management's controls). That account is what the later verification is checked
     against, so a merchant choosing it would defeat the whole control.
  3. **Nothing short of a confirmed bank credit completes the deposit.** Not the receipt, not the
     Admin having looked at the receipt, not five of the six checkboxes. `blocking_reasons` is the
     single definition of "ready", and the completion route asks it rather than re-deriving it.
  4. **A recorded mismatch is a hard stop, not a warning** — a receipt whose amount or receiving
     account disagrees with the request cannot be completed at all.
  5. **Completion happens once.** A second Mark as Done returns the existing completed state
     instead of re-running the credit path, re-notifying and appending a second approval.
  6. **The audit names the real person.** Every CDM action is attributed to the authenticated
     Admin — never to a synthesised identity.
  7. **Every other deposit type is untouched.** CDM adds no status, no accounting path and no
     completion route; it reuses the existing Deposit lifecycle exactly.

NO PRODUCTION REFERENCE IDS ARE CONSUMED — `_next_ref` is patched to a counter.

Run from the backend directory:

    python -m pytest tests/test_cdm_deposit.py -v
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import transactions as txr
from app.db.session import Base
from app.models.models import (
    AccountMaster, AccountType, AuditLog, Transaction, TxStatus, TxType, User, UserRole,
)
from app.schemas.schemas import (
    AccountSubmitRequest, CdmVerification, DepositCreate, ProofsAppend, SlipRequest,
)
from app.services import cdm
from app.services import deposit_allocation as alloc


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def safe_refs_and_no_cache(monkeypatch):
    """No production reference number is drawn, and no Redis connection is attempted."""
    counter = {"n": 0}

    async def fake_next_ref(db, kind, code=None):
        counter["n"] += 1
        return str(counter["n"])

    async def fake_cache_delete(key):
        return None

    monkeypatch.setattr(txr, "_next_ref", fake_next_ref)
    monkeypatch.setattr(txr, "cache_delete", fake_cache_delete)
    return counter


RECEIPT = "data:image/png;base64," + base64.b64encode(b"cdm-receipt").decode()
RECEIPT_2 = "data:image/png;base64," + base64.b64encode(b"cdm-receipt-2").decode()


# ── Builders ───────────────────────────────────────────────────────────────────────────────────

async def _merchant(db: AsyncSession, uid: int = 7) -> User:
    user = User(id=uid, username=f"op{uid}", name="BELLAGIO", role=UserRole.MERCHANT,
                hashed_password="x", email=f"op{uid}@test.local", merchant_role="DATA_OPERATOR",
                pay_in="DEP")
    db.add(user)
    await db.flush()
    return user


def _admin(uid: int = 1, name: str = "Priya Nair") -> User:
    return User(id=uid, username="admin1", name=name, role=UserRole.ADMIN)


async def _account(db: AsyncSession, ref: str = "ACC1", *, bank: str = "HDFC Bank",
                   credit: float = 500000.0) -> AccountMaster:
    acc = AccountMaster(
        reference_number=ref, account_name="sindu", account_number=f"AC{ref}",
        ifsc_code="HDFC0001234", bank_name=bank, branch="Mumbai",
        account_type=AccountType.CURRENT, status="ACTIVE", created_date=date.today(),
        created_time="10:00:00", highest_credit=credit, highest_debit=50000.0,
    )
    db.add(acc)
    await db.flush()
    return acc


def _payload(amount: float = 50000.0, **kw) -> DepositCreate:
    # No accountType: a bank-like deposit must name the SENDING account's type, but a CDM deposit
    # has no sending account at all — cash is pushed into a machine.
    base = dict(amount=amount, depositType="CDM", memberName="Test Member", memberId="MBR1")
    base.update(kw)
    return DepositCreate(**base)


def _verification(**kw) -> CdmVerification:
    """What an Admin records: the ATM ID off the receipt, the credit reference off the statement."""
    base = dict(atmId="317110RY", bankCreditRef="HDFCN5512340099")
    base.update(kw)
    return CdmVerification(**base)


async def _cdm_request(db: AsyncSession, merchant: User, amount: float = 50000.0, **kw):
    """A CDM deposit as the merchant raises it — through the real endpoint."""
    return await txr.create_deposit(_payload(amount, **kw), db, merchant)


async def _accepted(db: AsyncSession, merchant: User, admin: User, amount: float = 50000.0,
                    account_ref: str = "ACC1"):
    """A CDM request an Admin has accepted, with the receiving account assigned."""
    out = await _cdm_request(db, merchant, amount)
    await txr.account_submit(out["id"], AccountSubmitRequest(
        adminRef=account_ref, adminBankDetails=f"Account: {account_ref}"), db, admin)
    return out["id"]


async def _awaiting_completion(db: AsyncSession, merchant: User, admin: User,
                               amount: float = 50000.0, account_ref: str = "ACC1",
                               receipts: list[str] | None = None):
    """A CDM deposit that has reached the Admin's desk: account assigned, receipt uploaded,
    reviewer approved. Everything except the verification."""
    tx_id = await _accepted(db, merchant, admin, amount, account_ref)
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=receipts or [RECEIPT],
                                             merchantRef="CDM887766"), None, db, merchant)
    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.SLIP_SUBMITTED          # the Supervisor's approval, already tested elsewhere
    await db.flush()
    return tx_id


# ═══ 1. CDM is a deposit type, and the engine never touches it ══════════════════════════════════

@pytest.mark.asyncio
async def test_a_cdm_request_can_be_created(db):
    merchant = await _merchant(db)
    out = await _cdm_request(db, merchant)

    assert out["depositType"] == "CDM"
    assert out["type"] == TxType.DEPOSIT_REQUEST


def test_cdm_is_not_an_allocatable_deposit_type():
    """The single fact that keeps the engine away from a CDM request.

    Membership of this tuple is what `create_deposit` tests before running allocation, so this is
    the guarantee — not a code path that happens to skip it today.
    """
    assert "CDM" not in alloc.ALLOCATABLE_DEPOSIT_TYPES
    assert alloc.ALLOCATABLE_DEPOSIT_TYPES == ("UPI", "BANK", "IMPS", "NEFT", "RTGS")


@pytest.mark.asyncio
async def test_no_account_is_allocated_to_a_cdm_request(db):
    """An account with ample capacity sits there unused — a CDM request waits for an Admin."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=1000000.0)

    out = await _cdm_request(db, merchant)

    assert out["adminRef"] is None
    assert out["allocationSnapshot"] is None
    assert out["adminBankDetails"] is None
    assert out["status"] == TxStatus.ACCOUNT_REQUESTED, "waits for the Admin, not NO_ELIGIBLE_ACCOUNT"


@pytest.mark.asyncio
async def test_the_allocation_engine_is_never_invoked_for_cdm(db, monkeypatch):
    """Not "it happened to assign nothing" — it is not called at all."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    called = {"n": 0}

    async def spy(*a, **kw):
        called["n"] += 1
        raise AssertionError("the allocation engine must never run for a CDM deposit")

    monkeypatch.setattr(alloc, "allocate_deposit_account", spy)
    await _cdm_request(db, merchant)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_a_cdm_request_consumes_no_account_capacity(db):
    """Capacity is consumed at allocation; a CDM request allocates nothing, so it consumes nothing."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=100000.0)

    await _cdm_request(db, merchant, 90000.0)

    assert (await alloc.credit_used_today(db)).get("ACC1", 0.0) == 0.0


@pytest.mark.asyncio
async def test_an_ordinary_bank_deposit_is_still_allocated_automatically(db):
    """The regression that matters: adding CDM changed nothing for the automated types."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=500000.0)

    out = await txr.create_deposit(
        DepositCreate(amount=45000.0, depositType="BANK", memberName="M", memberId="MBR2",
                      accountHolder="M", accountNumber="999", ifsc="HDFC0001234",
                      bankName="HDFC Bank", accountType="CURRENT"), db, merchant)

    assert out["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert out["adminRef"] == "ACC1"


# ═══ 2. The receiving account is assigned by an Admin, under Account Management's controls ══════

@pytest.mark.asyncio
async def test_admin_accepts_the_request_by_assigning_the_receiving_account(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    out = await _cdm_request(db, merchant)

    result = await txr.account_submit(out["id"], AccountSubmitRequest(
        adminRef="ACC1", adminBankDetails="Account: ACC1"), db, _admin())

    assert result["status"] == TxStatus.ACCOUNT_SUBMITTED
    assert result["adminRef"] == "ACC1"
    assert result["approvedBy"] == "Priya Nair", "the acceptance names the authenticated Admin"


@pytest.mark.asyncio
async def test_a_cdm_deposit_cannot_be_pointed_at_a_upi_id(db):
    """A UPI ID cannot receive cash from a machine."""
    merchant = await _merchant(db)
    out = await _cdm_request(db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(adminUpiId="sindu@ybl"), db, _admin())
    assert e.value.status_code == 400
    assert "cash" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_a_cdm_deposit_requires_a_managed_account_not_free_typed_details(db):
    """Free-typed bank details would put the request outside Account Management's controls."""
    merchant = await _merchant(db)
    out = await _cdm_request(db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.account_submit(out["id"], AccountSubmitRequest(
            adminBankDetails="Pay into A/C 123456 at Some Bank"), db, _admin())
    assert e.value.status_code == 400
    assert "managed bank account" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_assigning_the_account_is_admin_only(db):
    """`account_submit` depends on get_current_admin — a merchant cannot reach it at all."""
    import inspect
    from app.core.deps import get_current_admin
    sig = inspect.signature(txr.account_submit)
    assert sig.parameters["actor"].default.dependency is get_current_admin


# ═══ 3. Receipts — many of them, appended, and proving nothing on their own ═════════════════════

@pytest.mark.asyncio
async def test_the_merchant_uploads_one_receipt(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())

    out = await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="CDM1"),
                                None, db, merchant)

    assert out["merchantProofs"] == [RECEIPT]
    assert out["status"] == TxStatus.SUPERVISOR_REVIEW


@pytest.mark.asyncio
async def test_the_merchant_uploads_many_receipts(db):
    """A CDM run can span several machine visits; every receipt is kept."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())
    many = ["data:image/png;base64," + base64.b64encode(f"r{i}".encode()).decode() for i in range(8)]

    out = await txr.submit_slip(tx_id, SlipRequest(merchantProofs=many, merchantRef="CDM1"),
                                None, db, merchant)

    assert out["merchantProofs"] == many


@pytest.mark.asyncio
async def test_a_later_receipt_is_appended_not_substituted(db):
    """Scenario G — more proof after the first upload must never replace the evidence reviewed."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="CDM1"),
                          None, db, merchant)

    out = await txr.append_proofs(tx_id, ProofsAppend(proofs=[RECEIPT_2]), None, db, merchant)

    assert out["merchantProofs"] == [RECEIPT, RECEIPT_2]


@pytest.mark.asyncio
async def test_uploading_more_receipts_creates_no_second_deposit(db):
    """Scenario F — the files land on the SAME transaction; the ledger gains no new row."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="CDM1"),
                          None, db, merchant)
    await txr.append_proofs(tx_id, ProofsAppend(proofs=[RECEIPT_2]), None, db, merchant)

    rows = (await db.execute(select(Transaction))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_uploading_a_receipt_does_not_verify_or_complete_anything(db):
    """The heart of it: the file is on the record and the deposit is no closer to being paid."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="CDM1"),
                          None, db, merchant)

    out = await txr.append_proofs(tx_id, ProofsAppend(proofs=[RECEIPT_2]), None, db, merchant)

    assert out["status"] != TxStatus.DEPOSITED
    assert out["cdmVerification"]["canComplete"] is False
    assert out["cdmVerification"]["checks"]["bankCreditConfirmed"] is False


# ═══ 4. Verification records evidence; it does not complete anything ════════════════════════════

@pytest.mark.asyncio
async def test_saving_the_verification_changes_no_status(db):
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    out = await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    assert out["status"] == TxStatus.SLIP_SUBMITTED, "still awaiting the Admin's explicit completion"
    assert out["processedBy"] is None
    assert out["cdmVerification"]["canComplete"] is True


@pytest.mark.asyncio
async def test_the_verification_records_the_authenticated_admin(db):
    """Requirement 12 — never a fake or minted identity."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    out = await txr.save_cdm_verification(tx_id, _verification(), None, db,
                                          _admin(uid=9, name="Rahul Verma"))

    v = out["cdmVerification"]
    assert v["verifiedBy"] == "Rahul Verma", "recorded server-side even though no panel shows it"
    assert v["verifiedByUsername"] == "admin1"
    assert v["verifiedAt"]
    assert v["bankCreditConfirmedAt"], "the moment the money became real is stamped"


@pytest.mark.asyncio
async def test_the_bank_credit_confirmation_is_audited_separately(db):
    """It is the control that releases the money, so it gets its own audit entry."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    rows = (await db.execute(select(AuditLog))).scalars().all()
    actions = [r.action_type for r in rows]
    assert "CDM_VERIFICATION_UPDATED" in actions
    assert "CDM_BANK_CREDIT_CONFIRMED" in actions
    confirmed = next(r for r in rows if r.action_type == "CDM_BANK_CREDIT_CONFIRMED")
    assert confirmed.username == "Priya Nair", "the real actor, not a synthesised one"
    assert confirmed.reason == "HDFCN5512340099", "the evidence is recorded with it"


@pytest.mark.asyncio
async def test_withdrawing_the_bank_credit_confirmation_is_audited_too(db):
    """An Admin who realises the credit is not there must leave a trail, and re-block completion."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    out = await txr.save_cdm_verification(tx_id, _verification(bankCreditRef=None),
                                          None, db, _admin())

    assert out["cdmVerification"]["canComplete"] is False
    actions = [r.action_type for r in (await db.execute(select(AuditLog))).scalars().all()]
    assert "CDM_BANK_CREDIT_WITHDRAWN" in actions


@pytest.mark.asyncio
async def test_verification_applies_to_cdm_deposits_only(db):
    merchant = await _merchant(db)
    out = await txr.create_deposit(
        DepositCreate(amount=1000.0, depositType="CASH", memberName="M", memberId="MBR3",
                      depositDetails={"village": "v", "city": "c", "mobile": "9"},
                      proofs=[RECEIPT]), db, merchant)

    with pytest.raises(HTTPException) as e:
        await txr.save_cdm_verification(out["id"], _verification(), None, db, _admin())
    assert e.value.status_code == 400


# ═══ 5. Mark as Done — the gate ═════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_fully_verified_cdm_deposit_completes(db):
    """The happy path, through the ordinary Deposit completion — no CDM-specific route."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    out = await txr.mark_done(tx_id, None, None, db, _admin())

    assert out["status"] == TxStatus.DEPOSITED, "the existing Deposit lifecycle value, not a CDM one"
    assert out["processedBy"] == "Priya Nair"


@pytest.mark.asyncio
async def test_a_fake_receipt_with_no_bank_credit_cannot_be_completed(db):
    """Scenario A — the receipt looks perfect and the money never arrived."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    # Everything a forger can influence is recorded; the one thing they cannot is not. A receipt
    # can be edited, reused or belong to somebody else's deposit \u2014 the credit on the statement cannot.
    await txr.save_cdm_verification(tx_id, _verification(bankCreditRef=None), None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert e.value.status_code == 400
    assert "bank credit reference" in e.value.detail.lower()

    tx = await txr._get_tx(tx_id, db)
    assert tx.status == TxStatus.SLIP_SUBMITTED, "left pending, exactly as the workflow says"


@pytest.mark.asyncio
async def test_no_verification_at_all_blocks_completion(db):
    """Scenario H — an Admin pressing Mark as Done with nothing confirmed."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", list(cdm.REQUIRED_KEYS))
async def test_both_recorded_values_are_required(db, missing):
    """One of two is not enough. Each names a different thing: the machine, and the credit."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(**{missing: None}), None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert dict(cdm.REQUIRED_FIELDS)[missing] in e.value.detail

    tx = await txr._get_tx(tx_id, db)
    assert tx.status == TxStatus.SLIP_SUBMITTED, "nothing moved"


# \u2500\u2500 The two values the Admin records are MANDATORY \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Neither is on the request already. The ATM ID names the machine the cash physically went into;
# the bank credit reference is the credit as the BANK states it. A blank one is an omission, and
# an omission blocks \u2014 otherwise a receipt alone would be releasing money.

@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
async def test_a_blank_or_whitespace_atm_id_blocks_completion(db, blank):
    """Whitespace is not an entry. It is stored as NULL, so "not entered" has exactly one shape."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    out = await txr.save_cdm_verification(tx_id, _verification(atmId=blank), None, db, _admin())

    assert out["cdmVerification"]["atmId"] is None
    assert out["cdmVerification"]["canComplete"] is False
    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert "ATM ID" in e.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
async def test_a_blank_or_whitespace_credit_reference_blocks_completion(db, blank):
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    out = await txr.save_cdm_verification(tx_id, _verification(bankCreditRef=blank), None, db, _admin())

    assert out["cdmVerification"]["bankCreditRef"] is None
    assert out["cdmVerification"]["bankCreditConfirmed"] is False, "no reference, no confirmation"
    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert "bank credit reference" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_both_values_allow_completion(db):
    """The positive case for exactly this rule: with both recorded, it completes."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin(), amount=50000.0)
    out = await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())
    assert out["cdmVerification"]["canComplete"] is True

    done = await txr.mark_done(tx_id, None, None, db, _admin())

    assert done["status"] == TxStatus.DEPOSITED


@pytest.mark.asyncio
async def test_both_values_are_stored_on_the_record(db):
    """They live in cdm_verification, not only in the request that set them \u2014 and trimmed."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    await txr.save_cdm_verification(
        tx_id, _verification(atmId="  317110RY  ", bankCreditRef=" HDFCN5512340099 "),
        None, db, _admin())

    stored = json.loads((await txr._get_tx(tx_id, db)).cdm_verification)
    assert stored["atmId"] == "317110RY"
    assert stored["bankCreditRef"] == "HDFCN5512340099"
    assert stored["bankCreditConfirmed"] is True, "derived from the reference, not asserted apart"


@pytest.mark.asyncio
async def test_both_values_reach_the_audit_trail(db):
    """A reviewer reading the trail must see what was recorded, and against which request."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    row = next(r for r in (await db.execute(select(AuditLog))).scalars().all()
               if r.action_type == "CDM_VERIFICATION_UPDATED")
    assert "317110RY" in row.new_value, "the ATM ID is recorded"
    assert "HDFCN5512340099" in row.new_value, "the credit reference is recorded"
    assert "50,000.00" in row.reason and "ACC1" in row.reason, "\u2026alongside what it is measured against"


def test_the_gate_trims_before_deciding():
    """A trailing space must not pass for evidence, and must not turn an entry into an omission."""
    assert cdm.field({"atmId": "  317110RY "}, "atmId") == "317110RY"
    assert cdm.field({"atmId": "   "}, "atmId") == ""
    assert cdm.field({}, "atmId") == ""
    assert cdm.bank_credit_confirmed({"bankCreditRef": "  "}) is False
    assert cdm.bank_credit_confirmed({"bankCreditRef": "HDFCN1"}) is True


@pytest.mark.asyncio
async def test_the_fields_the_form_no_longer_sends_are_ignored_not_rejected(db):
    """An older client still posting the six boxes and the figures must not 400 \u2014 those values
    are simply not part of the payload any more."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())

    out = await txr.save_cdm_verification(
        tx_id, CdmVerification(atmId="317110RY", bankCreditRef="HDFCN1",
                               **{"receiptVerified": True, "verifiedAmount": 1.0}),
        None, db, _admin())

    assert out["cdmVerification"]["canComplete"] is True
    stored = json.loads((await txr._get_tx(tx_id, db)).cdm_verification)
    assert "verifiedAmount" not in stored, "an ignored field is not a recorded one"


@pytest.mark.asyncio
async def test_a_historical_record_is_preserved_by_a_new_save(db):
    """An older record carries a checklist and figures this form no longer writes. Recording the
    two values today must ADD to it, never erase what an Admin confirmed under the old regime."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    tx = await txr._get_tx(tx_id, db)
    tx.cdm_verification = json.dumps({
        "receiptVerified": True, "amountMatches": True, "accountMatches": True,
        "dateChecked": True, "referenceChecked": True, "bankCreditConfirmed": True,
        "cdmReference": "CDM887766", "depositedOn": "11 Sep 2026, 03:40 PM",
        "verifiedAmount": 50000.0, "verifiedAccountRef": "ACC1",
        "remarks": "counted at the branch", "verifiedBy": "An Earlier Admin",
    })
    await db.flush()

    out = await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    stored = json.loads((await txr._get_tx(tx_id, db)).cdm_verification)
    assert stored["verifiedAmount"] == 50000.0 and stored["verifiedAccountRef"] == "ACC1"
    assert stored["cdmReference"] == "CDM887766" and stored["depositedOn"] == "11 Sep 2026, 03:40 PM"
    assert stored["remarks"] == "counted at the branch", "a field the form no longer has is not blanked"
    assert stored["receiptVerified"] is True
    assert stored["atmId"] == "317110RY", "…and today's values are recorded alongside"
    # The historical fields are still REPORTED, so an old record keeps showing what it recorded.
    assert out["cdmVerification"]["verifiedAmount"] == 50000.0
    assert out["cdmVerification"]["checks"]["dateChecked"] is True
    assert out["cdmVerification"]["remarks"] == "counted at the branch"


@pytest.mark.asyncio
async def test_a_historical_checklist_alone_no_longer_completes(db):
    """The rule did not get weaker. A record with all six old boxes ticked and no ATM ID or credit
    reference is NOT completable: the two values are required of every CDM deposit."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    tx = await txr._get_tx(tx_id, db)
    tx.cdm_verification = json.dumps({k: True for k in cdm.LEGACY_CHECK_KEYS}
                                     | {"verifiedAmount": 50000.0, "verifiedAccountRef": "ACC1"})
    await db.flush()

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert "ATM ID" in e.value.detail
    assert (await txr._get_tx(tx_id, db)).status == TxStatus.SLIP_SUBMITTED


@pytest.mark.asyncio
async def test_a_confirmed_credit_without_its_reference_cannot_be_completed(db):
    """Requirement 8.8 — a confirmation with nothing behind it is an assertion, not evidence."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(bankCreditRef="   "), None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert "bank credit reference" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_a_cdm_deposit_with_no_receipt_cannot_be_completed(db):
    """There is nothing to compare against the bank statement."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _accepted(db, merchant, _admin())
    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(tx_id, None, None, db, _admin())
    assert "No CDM receipt" in e.value.detail


@pytest.mark.asyncio
async def test_a_cdm_deposit_with_no_receiving_account_cannot_be_completed(db):
    """Without an assigned account there is no account in which a credit could be confirmed."""
    merchant = await _merchant(db)
    out = await _cdm_request(db, merchant)
    tx = await txr._get_tx(out["id"], db)
    tx.status = TxStatus.SLIP_SUBMITTED
    tx.merchant_proof = RECEIPT
    await db.flush()
    await txr.save_cdm_verification(out["id"], _verification(), None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.mark_done(out["id"], None, None, db, _admin())
    assert "No receiving account" in e.value.detail


@pytest.mark.asyncio
async def test_a_blocked_completion_credits_nothing_and_leaves_the_request_pending(db):
    """Scenario E — the receipt is in, the credit has not appeared. Nothing moves."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(bankCreditRef=None), None, db, _admin())

    with pytest.raises(HTTPException):
        await txr.mark_done(tx_id, None, None, db, _admin())

    tx = await txr._get_tx(tx_id, db)
    assert tx.status == TxStatus.SLIP_SUBMITTED
    assert tx.processed_by is None and tx.admin_action_at is None
    balance = await txr.compute_balance(db, merchant)
    assert balance["totalDeposit"] == 0, "no completed deposit, so nothing was credited"


# ═══ 6. Accounting — credited once, through the existing path ═══════════════════════════════════

@pytest.mark.asyncio
async def test_the_merchant_is_credited_only_when_the_deposit_completes(db):
    """Requirement 9 — creation, acceptance, upload and verification all credit nothing."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")

    tx_id = await _accepted(db, merchant, _admin())
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 0
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="CDM1"),
                          None, db, merchant)
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 0
    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 0, "verifying is not paying"

    await txr.mark_done(tx_id, None, None, db, _admin())
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 50000.0


@pytest.mark.asyncio
async def test_a_second_mark_as_done_credits_nothing_further(db):
    """Scenario I — a double click must not produce a second completion."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    first = await txr.mark_done(tx_id, None, None, db, _admin())
    second = await txr.mark_done(tx_id, None, None, db, _admin())

    assert first["status"] == second["status"] == TxStatus.DEPOSITED
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 50000.0
    approvals = [r for r in json.loads((await txr._get_tx(tx_id, db)).remarks_history or "[]")
                 if r.get("action") == "APPROVED"]
    assert len(approvals) == 1, "the second call recorded nothing at all"


@pytest.mark.asyncio
async def test_concurrent_completion_produces_one_completion(db):
    """Scenario J — two Admins pressing Mark as Done against the same request.

    The second caller re-reads the committed status under the row lock and returns it; it never
    re-runs the completion. Only one credit exists whichever order they arrive in.
    """
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())

    a = await txr.mark_done(tx_id, None, None, db, _admin(uid=1, name="Priya Nair"))
    b = await txr.mark_done(tx_id, None, None, db, _admin(uid=2, name="Rahul Verma"))

    assert a["status"] == b["status"] == TxStatus.DEPOSITED
    assert a["processedBy"] == b["processedBy"] == "Priya Nair", "the first completion stands"
    assert (await txr.compute_balance(db, merchant))["totalDeposit"] == 50000.0


@pytest.mark.asyncio
async def test_a_completed_cdm_deposit_cannot_have_its_verification_rewritten(db):
    """The evidence behind a paid deposit is sealed."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    tx_id = await _awaiting_completion(db, merchant, _admin())
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())
    await txr.mark_done(tx_id, None, None, db, _admin())

    with pytest.raises(HTTPException) as e:
        await txr.save_cdm_verification(tx_id, _verification(bankCreditRef=None),
                                        None, db, _admin())
    assert e.value.status_code == 400


# ═══ 7. Nothing else changed ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_non_cdm_deposit_is_not_gated_by_the_cdm_rule(db):
    """The gate is CDM-only: an ordinary deposit still completes on the Admin's judgement alone."""
    merchant = await _merchant(db)
    await _account(db, "ACC1", credit=500000.0)
    out = await txr.create_deposit(
        DepositCreate(amount=45000.0, depositType="BANK", memberName="M", memberId="MBR2",
                      accountHolder="M", accountNumber="999", ifsc="HDFC0001234",
                      bankName="HDFC Bank", accountType="CURRENT"), db, merchant)
    await txr.submit_slip(out["id"], SlipRequest(merchantProofs=[RECEIPT], merchantRef="U1"),
                          None, db, merchant)
    tx = await txr._get_tx(out["id"], db)
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()

    done = await txr.mark_done(out["id"], None, None, db, _admin())

    assert done["status"] == TxStatus.DEPOSITED
    assert done["cdmVerification"] is None


@pytest.mark.asyncio
async def test_cdm_introduces_no_new_status_values(db):
    """Requirement 10 — the lifecycle is the existing Deposit lifecycle, exactly."""
    merchant = await _merchant(db)
    await _account(db, "ACC1")
    seen = []
    tx_id = await _cdm_request(db, merchant)
    tx_id = tx_id["id"]
    seen.append((await txr._get_tx(tx_id, db)).status)
    await txr.account_submit(tx_id, AccountSubmitRequest(adminRef="ACC1", adminBankDetails="x"), db, _admin())
    seen.append((await txr._get_tx(tx_id, db)).status)
    await txr.submit_slip(tx_id, SlipRequest(merchantProofs=[RECEIPT], merchantRef="C1"), None, db, merchant)
    seen.append((await txr._get_tx(tx_id, db)).status)
    tx = await txr._get_tx(tx_id, db)
    tx.status = TxStatus.SLIP_SUBMITTED
    await db.flush()
    await txr.save_cdm_verification(tx_id, _verification(), None, db, _admin())
    await txr.mark_done(tx_id, None, None, db, _admin())
    seen.append((await txr._get_tx(tx_id, db)).status)

    assert seen == [TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED,
                    TxStatus.SUPERVISOR_REVIEW, TxStatus.DEPOSITED]
    assert all(s in set(TxStatus) for s in seen)


# ═══ The policy itself ══════════════════════════════════════════════════════════════════════════

def test_a_corrupt_verification_record_fails_closed():
    """Unreadable evidence must read as "nothing verified", never as "verified"."""
    assert cdm.parse("{not json") == {}
    assert cdm.parse(None) == {}
    assert cdm.parse('["a list"]') == {}


def test_the_form_and_the_gate_come_from_one_list():
    """The form and the server cannot drift: both enumerate cdm.REQUIRED_FIELDS, and every one of
    them is a field the save route actually writes."""
    assert cdm.BANK_CREDIT_FIELD in cdm.REQUIRED_KEYS
    assert len(cdm.REQUIRED_KEYS) == 2
    assert set(cdm.REQUIRED_KEYS) <= set(cdm.TEXT_FIELDS)
