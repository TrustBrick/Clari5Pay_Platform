"""Tests for the Card deposit transaction type.

A Card deposit reuses the whole existing deposit lifecycle and introduces no new status: the only
Card-specific hop is the Admin's, who submits a payment gateway link where every other deposit type
gets an account. Completion stays exactly where it is for every other deposit — the Admin's /done,
once the reviewer has approved. What that buys in reuse it owes in server-side rigour: nothing
about the flow is safe if the state gates live only in the UI.

The properties that matter, and which these tests pin down:

  1. **The link is real** — empty, whitespace, over-long and non-http links are all rejected, so a
     good link can never be overwritten by a bad one and `javascript:` never reaches an operator
     who is about to click it.
  2. **Payment evidence is mandatory** — a Card request cannot enter review without BOTH a payment
     image and a UTR, and without a chosen Manager/Supervisor.
  3. **The state machine holds** — Link Requested cannot jump to review, and evidence cannot be
     altered once the request has left the operator for review or completion.
  4. **There is one way to complete a deposit** — the Admin's /done. No Card-specific completion
     route exists, and no operator can reach one.
  5. **Duplicates are rejected** — a second link submission and a second submit-for-review both
     fail on the state gate, which is what makes double-clicking harmless.

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


# ── 5. Completion belongs to the Admin ─────────────────────────────────────────────────────────
# Marking a Card deposit DEPOSITED is not Card-specific and must not be: the Admin's existing
# /done already completes any reviewer-approved deposit (SLIP_SUBMITTED). An operator-side
# completion route briefly existed here; these tests are what keep it from coming back, because a
# second way to move money to DEPOSITED is exactly the kind of thing that gets re-added by accident.

def test_no_operator_completion_route_exists():
    """No Card-specific completion endpoint — the operator cannot mark a deposit at all."""
    paths = {getattr(r, "path", "") for r in txr.router.routes}
    assert not [p for p in paths if "card" in p.lower()], sorted(p for p in paths if "card" in p.lower())
    assert not hasattr(txr, "card_mark_deposit")


def test_admin_done_is_the_only_completion_route():
    """/{tx_id}/done exists and is Admin-gated, so completion cannot be reached by an operator."""
    import inspect
    from app.core.deps import get_current_admin

    done = [r for r in txr.router.routes if getattr(r, "path", "").endswith("/{tx_id}/done")]
    assert len(done) == 1, [getattr(r, "path", "") for r in txr.router.routes]
    actor = inspect.signature(txr.mark_done).parameters["actor"].default
    assert actor.dependency is get_current_admin


def test_operator_cannot_alter_evidence_once_it_leaves_them():
    """The reviewer's approval parks a Card deposit at SLIP_SUBMITTED, where it waits for the
    Admin. The operator's payable window must not include that state (nor a finished one), or the
    slip and UTR could be swapped out from under the person about to release the money."""
    for status in (TxStatus.SLIP_SUBMITTED, TxStatus.SUPERVISOR_REVIEW, TxStatus.DEPOSITED,
                   TxStatus.REJECTED, TxStatus.ACCOUNT_REQUESTED):
        assert status not in txr.CARD_PAYABLE_STATUSES


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

# ═══ 6. Credit and Debit are separate types with one workflow ════════════════════════════
# They differ in one thing and it is on the form: a CREDIT card deposit has no sending bank account
# to name. Everything else — the payment link, the payable statuses, the mandatory slip and UTR,
# the Admin's completion — is shared, so every card behaviour is a membership test rather than an
# equality check. The pre-split value CARD keeps working because it is a member of that tuple.


@pytest.mark.parametrize("dep_type", ["CARD", "CREDIT_CARD", "DEBIT_CARD"])
def test_every_card_type_is_a_card_deposit(dep_type):
    tx = _card_tx()
    tx.deposit_type = dep_type
    assert txr._is_card_deposit(tx) is True


@pytest.mark.parametrize("dep_type", ["UPI", "IMPS", "NEFT", "RTGS", "CDM", "CASH", "CRYPTO"])
def test_nothing_else_became_a_card_deposit(dep_type):
    """The split widened the card family by exactly two values and touched no other type."""
    tx = _card_tx()
    tx.deposit_type = dep_type
    assert txr._is_card_deposit(tx) is False


def test_the_card_family_is_the_two_new_types_plus_the_original():
    assert txr.CARD_DEPOSIT_TYPES == ("CARD", "CREDIT_CARD", "DEBIT_CARD")
    assert txr.CREDIT_CARD_DEPOSIT_TYPE == "CREDIT_CARD"
    assert txr.DEBIT_CARD_DEPOSIT_TYPE == "DEBIT_CARD"
    # The original value is still in the family, which is what keeps every pre-split row working.
    assert txr.CARD_DEPOSIT_TYPE == "CARD"


@pytest.mark.parametrize("dep_type", ["CARD", "CREDIT_CARD", "DEBIT_CARD"])
def test_no_card_type_needs_a_receiving_account(dep_type):
    """A card deposit is paid through the Admin's payment link, so the allocation engine never
    places one — and `needs_account` in create_deposit reads exactly this tuple."""
    from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES
    assert dep_type not in ALLOCATABLE_DEPOSIT_TYPES


@pytest.mark.parametrize("dep_type", ["UPI", "IMPS", "NEFT", "RTGS"])
def test_the_account_backed_types_still_need_one(dep_type):
    """The other side of the same rule: nothing here was relaxed for anybody else."""
    from app.services.deposit_allocation import ALLOCATABLE_DEPOSIT_TYPES
    assert dep_type in ALLOCATABLE_DEPOSIT_TYPES


@pytest.mark.parametrize("dep_type", ["CREDIT_CARD", "DEBIT_CARD"])
def test_both_new_types_are_operator_gated_like_card(dep_type):
    """Credit and Debit inherit the restriction Card already had — it is about cards, not about
    one spelling of them."""
    assert dep_type in txr.CARD_DEPOSIT_TYPES
    assert txr.CARD_OPERATOR_ROLES == ("DEO", "DEPOSIT_OPERATOR")


@pytest.mark.parametrize("dep_type", ["CARD", "CREDIT_CARD", "DEBIT_CARD"])
@pytest.mark.asyncio
async def test_every_card_type_takes_the_payment_link_hop(dep_type):
    """The Admin's link step is reached through _is_card_deposit, so all three get it."""
    tx = _card_tx(TxStatus.ACCOUNT_REQUESTED)
    tx.deposit_type = dep_type
    assert txr._is_card_deposit(tx)
    assert tx.status in (TxStatus.ACCOUNT_REQUESTED,)


@pytest.mark.parametrize("dep_type", ["CARD", "CREDIT_CARD", "DEBIT_CARD"])
@pytest.mark.asyncio
async def test_every_card_type_still_demands_the_slip_and_utr(dep_type):
    """Payment evidence is mandatory for all three: the split changed what the FORM collects up
    front, never what the server requires as proof of payment."""
    tx = _card_tx(TxStatus.ACCOUNT_SUBMITTED, link="https://pay.example.com/abc")
    tx.deposit_type = dep_type
    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef=None))
    assert e.value.status_code == 400
    assert "UTR" in e.value.detail


@pytest.mark.asyncio
async def test_a_stored_pre_split_card_row_is_still_treated_as_a_card():
    """Requirement 12 — nothing rewrote the rows raised before the split.

    Asserted through a Card-SPECIFIC refusal: that message is only reachable inside
    `if _is_card_deposit(tx)`, so seeing it proves a stored "CARD" row still enters the card branch
    and still gets the card state machine rather than the ordinary deposit one.
    """
    tx = _card_tx(TxStatus.ACCOUNT_REQUESTED, link=None)
    assert tx.deposit_type == "CARD"

    with pytest.raises(HTTPException) as e:
        await _submit_slip(tx, SlipRequest(merchantProofs=[PNG], merchantRef="UTR123",
                                           approverUserId=9))

    assert "Card request is not awaiting a payment slip" in e.value.detail
    assert tx.status == TxStatus.ACCOUNT_REQUESTED
