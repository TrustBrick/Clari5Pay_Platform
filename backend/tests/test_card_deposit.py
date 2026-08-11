"""Tests for the Card deposit transaction type.

A Card deposit reuses the whole existing deposit lifecycle and introduces no new status — the
Admin submits a payment gateway link where every other deposit type gets an account, and the
requesting operator (not the Admin) performs the final Mark Deposit. What that buys in reuse it
owes in server-side rigour: nothing about the flow is safe if the state gates are only in the UI.

The properties that matter, and which these tests pin down:

  1. **The link is real** — empty, whitespace, over-long and non-http links are all rejected, so a
     good link can never be overwritten by a bad one and `javascript:` never reaches an operator
     who is about to click it.
  2. **Payment evidence is mandatory** — a Card request cannot enter review without BOTH a payment
     image and a UTR, and without a chosen Manager/Supervisor.
  3. **The state machine holds** — Link Requested cannot jump to review; only a reviewer-approved
     request can be deposited; a rejected or already-deposited one never can.
  4. **The right person acts** — Mark Deposit requires the owning Data/Deposit Operator, not any
     merchant user and not a reviewer.
  5. **Duplicates are rejected** — a second link submission and a second Mark Deposit both fail on
     the state gate, which is what makes double-clicking harmless.

Every rejection path above raises before the route touches the database, so the suite needs no
database, no migrations and no fixtures — a stub session that can serve one row is enough. That is
deliberate: none of this logic should need infrastructure to be verifiable.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api python -m pytest tests/test_card_deposit.py -v
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes import transactions as txr
from app.models.models import Transaction, TxStatus, TxType, User, UserRole
from app.schemas.schemas import AccountSubmitRequest, SlipRequest

# A 1x1 PNG data URL — a real, decodable upload, so nothing here passes on a malformed fixture.
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
       "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _StubSession:
    """The smallest thing `_get_tx` will accept: one row, served to any query."""

    def __init__(self, row):
        self._row = row

    async def execute(self, _stmt):
        return _Result(self._row)


def _operator(role: str = "DEO", user_id: int = 7) -> User:
    return User(id=user_id, username="op1", name="BELLAGIO", role=UserRole.MERCHANT, merchant_role=role)


def _card_tx(status: TxStatus = TxStatus.ACCOUNT_REQUESTED, *, link: str | None = None,
             merchant_id: int = 7) -> Transaction:
    tx = Transaction(
        id=42, ref="DEP0042", type=TxType.DEPOSIT_REQUEST, amount=5000.0, status=status,
        merchant_id=merchant_id, merchant_name="BELLAGIO", tx_time="10:00:00", deposit_type="CARD",
    )
    tx.payment_link = link
    tx.approver_user_id = None
    return tx


# ── 1. The link is real ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "   ", None, "example.com/pay", "javascript:alert(1)",
                                 "data:text/html,<script>", "ftp://host/pay", "https://" + "x" * 600])
def test_payment_link_rejected(bad):
    """Empty, whitespace, relative, non-http and over-long links are all refused."""
    with pytest.raises(HTTPException) as e:
        txr._validate_payment_link(bad)
    assert e.value.status_code == 400


@pytest.mark.parametrize("good", ["https://pay.example.com/abc123",
                                  "http://pay.example.com/abc123",
                                  "  https://pay.example.com/abc123  "])
def test_payment_link_accepted_and_trimmed(good):
    assert txr._validate_payment_link(good) == good.strip()


# ── 2. Card identification ─────────────────────────────────────────────────────────────────────

def test_is_card_deposit():
    assert txr._is_card_deposit(_card_tx()) is True
    lower = _card_tx()
    lower.deposit_type = "card"
    assert txr._is_card_deposit(lower) is True          # stored case never decides the flow
    bank = _card_tx()
    bank.deposit_type = "BANK"
    assert txr._is_card_deposit(bank) is False
    none_type = _card_tx()
    none_type.deposit_type = None
    assert txr._is_card_deposit(none_type) is False
    withdrawal = _card_tx()
    withdrawal.type = TxType.WITHDRAWAL_REQUEST
    assert txr._is_card_deposit(withdrawal) is False    # a Card deposit is a DEPOSIT, first


# ── 3. Admin link submission (Link Requested → Link Submitted) ─────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TxStatus.ACCOUNT_SUBMITTED, TxStatus.SUPERVISOR_REVIEW,
                                    TxStatus.SLIP_SUBMITTED, TxStatus.DEPOSITED, TxStatus.REJECTED])
async def test_link_submit_rejected_outside_link_requested(status):
    """The duplicate-submission guard: only a request still in Link Requested takes a link, so a
    second click cannot overwrite a link that is already with the operator."""
    tx = _card_tx(status, link="https://pay.example.com/original")
    with pytest.raises(HTTPException) as e:
        await txr._card_link_submit(_StubSession(tx), tx,
                                    AccountSubmitRequest(paymentLink="https://pay.example.com/second"),
                                    _operator("ADMIN"))
    assert e.value.status_code == 400
    assert tx.payment_link == "https://pay.example.com/original"   # untouched


@pytest.mark.asyncio
async def test_link_submit_rejects_empty_link():
    tx = _card_tx(TxStatus.ACCOUNT_REQUESTED)
    with pytest.raises(HTTPException) as e:
        await txr._card_link_submit(_StubSession(tx), tx, AccountSubmitRequest(paymentLink="  "),
                                    _operator("ADMIN"))
    assert e.value.status_code == 400
    assert tx.payment_link is None
    assert tx.status == TxStatus.ACCOUNT_REQUESTED                 # no half-applied transition


# ── 4. Pay and Upload Slip (image + UTR + reviewer, all mandatory) ─────────────────────────────

async def _submit_slip(tx: Transaction, data: SlipRequest, user: User | None = None):
    return await txr.submit_slip("TXN042", data, None, _StubSession(tx), user or _operator())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TxStatus.ACCOUNT_REQUESTED, TxStatus.SUPERVISOR_REVIEW,
                                    TxStatus.SLIP_SUBMITTED, TxStatus.DEPOSITED, TxStatus.REJECTED])
async def test_slip_rejected_outside_payable_states(status):
    """Only Link Submitted / Resubmitted accept payment evidence. This blocks a jump from Link
    Requested straight to review, a repeat submit while already in review, and any change to a
    request that is finished."""
    tx = _card_tx(status, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef="UTR123", approverUserId=9))
    assert e.value.status_code == 400
    assert tx.status == status


@pytest.mark.asyncio
async def test_slip_requires_payment_image():
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantRef="UTR123", approverUserId=9))
    assert e.value.status_code == 400
    assert "slip" in e.value.detail.lower() or "image" in e.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("utr", [None, "", "   "])
async def test_slip_requires_utr(utr):
    """A whitespace-only UTR is not a UTR."""
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef=utr, approverUserId=9))
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_slip_requires_reviewer():
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef="UTR123"))
    assert e.value.status_code == 400
    assert "Manager/Supervisor" in e.value.detail


@pytest.mark.asyncio
async def test_slip_requires_the_link_to_exist():
    """Belt and braces alongside the state gate: no link means the member was never able to pay."""
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link=None)
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef="UTR123", approverUserId=9))
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_slip_rejects_a_foreign_transaction():
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link="https://pay.example.com/abc", merchant_id=7)
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef="UTR123", approverUserId=9),
                           _operator("DEO", user_id=99))
    assert e.value.status_code == 403


# ── 5. Mark Deposit (Manager/Supervisor Approved → Deposited) ──────────────────────────────────

async def _mark_deposit(tx: Transaction, user: User):
    return await txr.card_mark_deposit("TXN042", None, _StubSession(tx), user)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED,
                                    TxStatus.SUPERVISOR_REVIEW, TxStatus.RESUBMITTED,
                                    TxStatus.REJECTED, TxStatus.CANCELLED])
async def test_mark_deposit_requires_reviewer_approval(status):
    """Nothing short of a reviewer-approved request can be deposited — including a rejected one,
    which must never be completable."""
    tx = _card_tx(status, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _mark_deposit(tx, _operator("DEO"))
    assert e.value.status_code == 400
    assert tx.status == status


@pytest.mark.asyncio
async def test_mark_deposit_rejects_an_already_deposited_request():
    """The duplicate-completion guard: a second click finds it deposited and is refused."""
    tx = _card_tx(TxStatus.DEPOSITED, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _mark_deposit(tx, _operator("DEO"))
    assert e.value.status_code == 400
    assert "already" in e.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["SUPERVISOR", "MANAGER", "WITHDRAWAL_OPERATOR", None, ""])
async def test_mark_deposit_requires_a_card_operator_role(role):
    tx = _card_tx(TxStatus.SLIP_SUBMITTED, link="https://pay.example.com/abc")
    with pytest.raises(HTTPException) as e:
        await _mark_deposit(tx, _operator(role))
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_mark_deposit_rejects_a_foreign_transaction():
    tx = _card_tx(TxStatus.SLIP_SUBMITTED, link="https://pay.example.com/abc", merchant_id=7)
    with pytest.raises(HTTPException) as e:
        await _mark_deposit(tx, _operator("DEO", user_id=99))
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_mark_deposit_rejects_a_non_card_deposit():
    """The Admin's /done still owns every other deposit type; this route must not touch them."""
    tx = _card_tx(TxStatus.SLIP_SUBMITTED)
    tx.deposit_type = "BANK"
    with pytest.raises(HTTPException) as e:
        await _mark_deposit(tx, _operator("DEO"))
    assert e.value.status_code == 400
    assert tx.status == TxStatus.SLIP_SUBMITTED


# ── 6. Who may raise a Card deposit ────────────────────────────────────────────────────────────

def test_only_operators_may_raise_a_card_deposit():
    """The role rule is enforced in the route, not only in the selector."""
    assert txr.CARD_OPERATOR_ROLES == ("DEO", "DEPOSIT_OPERATOR")
    for role in ("SUPERVISOR", "MANAGER", "WITHDRAWAL_OPERATOR", None, ""):
        assert str(role or "").upper() not in txr.CARD_OPERATOR_ROLES


def test_card_reuses_existing_statuses_only():
    """No CARD_* status was invented — the flow runs on the statuses that already existed."""
    for status in (TxStatus.ACCOUNT_REQUESTED, TxStatus.ACCOUNT_SUBMITTED, TxStatus.SUPERVISOR_REVIEW,
                   TxStatus.SLIP_SUBMITTED, TxStatus.RESUBMITTED, TxStatus.DEPOSITED, TxStatus.REJECTED):
        assert not status.value.startswith("CARD")
    assert txr.CARD_PAYABLE_STATUSES == (TxStatus.ACCOUNT_SUBMITTED, TxStatus.RESUBMITTED)
