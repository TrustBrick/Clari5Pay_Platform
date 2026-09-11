"""CDM (Cash Deposit Machine) deposits — the manual verification policy.

A CDM deposit is physical cash pushed into a machine. Nothing about it is self-evidencing:

  * No rail confirms it. A UPI/IMPS/NEFT/RTGS deposit arrives with a reference the bank issued;
    a CDM deposit arrives with a slip the machine printed and the payer photographed.
  * A receipt is an *image*. It can be edited, reused from another deposit, or belong to a real
    deposit made into somebody else's account. Treating "a receipt was uploaded" as "the money
    arrived" is precisely the fraud this module exists to prevent.
  * The receiving account cannot be chosen by a machine. The automatic allocation engine picks an
    account by remaining daily credit capacity at the instant of the request — meaningless for a
    payer who will walk to a CDM at some point later, so an Admin assigns the account instead.

The single fact that credits a merchant is therefore an Admin confirming the **actual bank
credit** in the designated account. Everything else — the receipt, the OCR of the receipt, the
Admin having looked at it, even the Admin ticking every other box — is supporting evidence and
explicitly not sufficient. ``blocking_reasons`` below is the whole rule, in one place, so a
completion path cannot quietly diverge from it.

The lifecycle is the ordinary Deposit lifecycle. There is no CDM status, no CDM accounting and no
CDM completion route: a CDM request moves through ACCOUNT_REQUESTED → ACCOUNT_SUBMITTED →
SUPERVISOR_REVIEW → SLIP_SUBMITTED → DEPOSITED exactly like every other deposit, and is credited
through the same Mark-as-Done path. Only the gate in front of that path is CDM-specific.
"""
from __future__ import annotations

import json
from typing import Any

# The deposit type code. Kept out of app.services.deposit_allocation.ALLOCATABLE_DEPOSIT_TYPES on
# purpose: a CDM deposit must never be handed to the automatic allocation engine, and leaving it
# out of that tuple is what guarantees it (see create_deposit's `needs_account`).
CDM_DEPOSIT_TYPE = "CDM"

# The Admin's checklist. Each is a distinct thing a human compared; none is inferred from another,
# and the system never ticks one on the Admin's behalf.
CHECKS: tuple[tuple[str, str], ...] = (
    ("receiptVerified", "Receipt Verified"),
    ("amountMatches", "Amount Matches"),
    ("accountMatches", "Bank / Account Matches"),
    ("dateChecked", "Date / Time Checked"),
    ("referenceChecked", "CDM Reference Checked"),
    ("bankCreditConfirmed", "Actual Bank Credit Confirmed"),
)
CHECK_KEYS = tuple(k for k, _ in CHECKS)
CHECK_LABELS = dict(CHECKS)

# The one check that is not merely evidence: the Admin has seen the money in the account. Without
# it nothing completes, however convincing the receipt looks.
BANK_CREDIT_CHECK = "bankCreditConfirmed"

# Free-text evidence recorded alongside the ticks.
TEXT_FIELDS = ("cdmReference", "bankCreditRef", "depositedOn", "remarks")


def is_cdm(deposit_type: str | None) -> bool:
    return (deposit_type or "").upper() == CDM_DEPOSIT_TYPE


def parse(raw: str | None) -> dict[str, Any]:
    """The stored verification record, tolerating an absent or corrupt value.

    Degrades to "nothing verified" rather than raising — which fails CLOSED, because every gate
    below reads this and an empty record blocks completion.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _checked(record: dict[str, Any], key: str) -> bool:
    return record.get(key) is True


def blocking_reasons(tx, record: dict[str, Any]) -> list[str]:
    """Everything that still stands between this CDM deposit and completion.

    An empty list means an Admin may mark it done. This is the ONLY definition of that; the
    completion route asks this function rather than re-deriving the rule, so the checklist shown
    to the Admin and the gate enforced by the server cannot drift apart.

    Ordered the way an operator would work through it — what is missing from the request first,
    then what the Admin has not yet confirmed — so the first reason is the useful one.
    """
    reasons: list[str] = []

    # 3 — the receiving account. An Admin assigns it; without one there is no account in which a
    # credit could be confirmed, so there is nothing to verify against.
    if not (tx.admin_ref or "").strip():
        reasons.append("No receiving account is assigned to this CDM request.")

    # 4 — the receipt. The CDM prints one; the payer uploads it. It proves nothing on its own, but
    # its absence means the Admin has nothing to compare against the bank statement.
    if not _has_proof(tx):
        reasons.append("No CDM receipt has been uploaded.")

    # 5/7 — the Admin's own comparisons. Each is a separate human act.
    for key in CHECK_KEYS:
        if not _checked(record, key):
            reasons.append(f"Not yet confirmed by an Admin: {CHECK_LABELS[key]}.")

    # 6/8 — the bank-credit reference. The Admin confirmed the credit against something; recording
    # what makes the confirmation auditable rather than a bare assertion.
    if _checked(record, BANK_CREDIT_CHECK) and not str(record.get("bankCreditRef") or "").strip():
        reasons.append("Enter the bank credit reference / UTR that evidences the credit.")

    # 8.6 — the amount the Admin actually read off the receipt. MANDATORY, and it must equal the
    # requested amount to the paisa. Ticking "Amount Matches" is the Admin asserting they compared
    # two figures; this is the second figure, so the assertion can be checked rather than trusted.
    # An omitted value used to pass silently — which meant the checkbox alone could release money.
    reasons.extend(_amount_reasons(tx, record))

    # 8.7 — the account named on the receipt. MANDATORY for the same reason, and it must be the
    # account this request was assigned. Cash paid into a real account that is not THIS one is
    # somebody else's money.
    reasons.extend(_account_reasons(tx, record))

    return reasons


def verified_amount(record: dict[str, Any]) -> float | None:
    """The verified amount as a number, or None when it is absent or not a number at all.

    Deliberately tolerant of the shapes a record can arrive in — a float, an int, or the string a
    form posted — and deliberately strict about the result: anything unparseable is None, which
    reads as "not entered" and therefore blocks. Nothing here guesses.
    """
    raw = record.get("verifiedAmount")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _amount_reasons(tx, record: dict[str, Any]) -> list[str]:
    amount = verified_amount(record)
    if amount is None:
        return ["Enter the verified amount read from the CDM receipt."]
    if amount <= 0:
        return ["The verified amount must be a positive figure."]
    if round(amount, 2) != round(float(tx.amount or 0), 2):
        return ["The verified amount does not match the requested deposit amount."]
    return []


def verified_account_ref(record: dict[str, Any]) -> str:
    """The verified receiving account, trimmed. Empty string when it is absent or only whitespace."""
    value = record.get("verifiedAccountRef")
    return str(value).strip() if value is not None else ""


def _account_reasons(tx, record: dict[str, Any]) -> list[str]:
    account = verified_account_ref(record)
    if not account:
        return ["Enter the verified receiving account read from the CDM receipt."]
    if account != (tx.admin_ref or "").strip():
        return ["The verified receiving account is not the account assigned to this request."]
    return []


def _has_proof(tx) -> bool:
    """Whether any receipt is attached. Reads the multi-file array first, then the legacy column.

    Both are deferred columns, so callers must have loaded them (the completion route does).
    """
    from app.core import proofs as proofs_core
    return bool(proofs_core.parse_proofs(tx.merchant_proofs) or tx.merchant_proof)


def summary(tx, record: dict[str, Any]) -> dict[str, Any]:
    """The verification state as the Admin UI consumes it: the ticks, the references, who
    confirmed, and whether completion is currently allowed (with the reasons if not)."""
    reasons = blocking_reasons(tx, record)
    return {
        "checks": {k: _checked(record, k) for k in CHECK_KEYS},
        **{f: record.get(f) for f in TEXT_FIELDS},
        "verifiedAmount": record.get("verifiedAmount"),
        "verifiedAccountRef": record.get("verifiedAccountRef"),
        "verifiedBy": record.get("verifiedBy"),
        "verifiedByUsername": record.get("verifiedByUsername"),
        "verifiedAt": record.get("verifiedAt"),
        "bankCreditConfirmedAt": record.get("bankCreditConfirmedAt"),
        "canComplete": not reasons,
        "blockingReasons": reasons,
    }
