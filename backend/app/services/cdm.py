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
credit** in the designated account. What makes that confirmation auditable rather than a bare
assertion is the **bank credit reference / UTR** — the credit entry as it appears on the bank
statement — recorded alongside the **ATM ID** printed on the receipt, which names the machine the
cash physically went into. Those two values are what the Admin records, and the gate requires both.

Everything else the receipt carries — the deposit amount, the receiving account, the merchant's
own CDM reference, the date and time — is ALREADY on the request and on the receipt in front of
the Admin. Re-typing it was never verification: it was transcription, and a transcription the
Admin could satisfy by copying the request back at itself. The panel shows those values read-only
and asks for the two facts that are not already known.

``blocking_reasons`` below is the whole rule, in one place, so a completion path cannot quietly
diverge from it.

Historical records keep everything an earlier regime stored — the six-box checklist, the verified
amount and account, the transcribed date — and ``summary`` still reports them. They are read, never
required: nothing is deleted from a record that already exists.

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

# What the Admin records, and what the gate requires. Two values, both read off something the
# Admin is looking at: the ATM ID from the receipt, the credit reference from the bank statement.
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("atmId", "Enter the ATM ID printed on the CDM receipt."),
    ("bankCreditRef", "Enter the bank credit reference / UTR that evidences the actual credit."),
)
REQUIRED_KEYS = tuple(k for k, _ in REQUIRED_FIELDS)

# The one that is not merely evidence: a credit reference is the Admin saying they have seen the
# money in the assigned account. Without it nothing completes, however convincing the receipt looks.
BANK_CREDIT_FIELD = "bankCreditRef"
# The stored boolean an earlier regime asserted by hand. Still written, now DERIVED from the
# reference above, so a record keeps one truthful shape for anything reading the history.
BANK_CREDIT_CONFIRMED_FLAG = "bankCreditConfirmed"

# Free-text evidence written by the current form.
TEXT_FIELDS = ("atmId", "bankCreditRef", "remarks")

# Written by the previous regime and still READ, so an old record keeps rendering everything it
# recorded. Nothing writes these any more and nothing requires them.
LEGACY_TEXT_FIELDS = ("cdmReference", "depositedOn")
LEGACY_CHECKS: tuple[tuple[str, str], ...] = (
    ("receiptVerified", "Receipt Verified"),
    ("amountMatches", "Amount Matches"),
    ("accountMatches", "Bank / Account Matches"),
    ("dateChecked", "Date / Time Checked"),
    ("referenceChecked", "CDM Reference Checked"),
    ("bankCreditConfirmed", "Actual Bank Credit Confirmed"),
)
LEGACY_CHECK_KEYS = tuple(k for k, _ in LEGACY_CHECKS)


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


def field(record: dict[str, Any], key: str) -> str:
    """A recorded text value, trimmed. Empty string when absent, blank or not a string at all —
    so "not entered" has ONE shape and whitespace can never pass for evidence."""
    value = record.get(key)
    return str(value).strip() if value is not None else ""


def bank_credit_confirmed(record: dict[str, Any]) -> bool:
    """Whether the actual bank credit has been confirmed. The credit reference IS the
    confirmation — there is no separate assertion to tick, and nothing to tick it with."""
    return bool(field(record, BANK_CREDIT_FIELD))


def blocking_reasons(tx, record: dict[str, Any]) -> list[str]:
    """Everything that still stands between this CDM deposit and completion.

    An empty list means an Admin may mark it done. This is the ONLY definition of that; the
    completion route asks this function rather than re-deriving the rule, so what the Admin is
    asked for and what the server enforces cannot drift apart.

    Ordered the way an operator would work through it — what is missing from the request first,
    then what the Admin has not yet recorded — so the first reason is the useful one.
    """
    reasons: list[str] = []

    # The receiving account. An Admin assigns it; without one there is no account in which a
    # credit could be confirmed, so there is nothing to verify against.
    if not (tx.admin_ref or "").strip():
        reasons.append("No receiving account is assigned to this CDM request.")

    # The receipt. The CDM prints one; the payer uploads it. It proves nothing on its own, but its
    # absence means the Admin has nothing to read the ATM ID from.
    if not _has_proof(tx):
        reasons.append("No CDM receipt has been uploaded.")

    # The two values the Admin records. Both mandatory, both trimmed: a space is not an ATM ID.
    for key, reason in REQUIRED_FIELDS:
        if not field(record, key):
            reasons.append(reason)

    return reasons


def _has_proof(tx) -> bool:
    """Whether any receipt is attached. Reads the multi-file array first, then the legacy column.

    Both are deferred columns, so callers must have loaded them (the completion route does).
    """
    from app.core import proofs as proofs_core
    return bool(proofs_core.parse_proofs(tx.merchant_proofs) or tx.merchant_proof)


def summary(tx, record: dict[str, Any]) -> dict[str, Any]:
    """The verification state as the Admin UI consumes it: what was recorded, who recorded it, and
    whether completion is currently allowed (with the reasons if not).

    Everything a previous regime stored is echoed alongside — the checklist, the verified figures,
    the transcribed date — so an old record still shows what an Admin confirmed at the time.
    """
    reasons = blocking_reasons(tx, record)
    return {
        **{f: record.get(f) for f in TEXT_FIELDS},
        "bankCreditConfirmed": bank_credit_confirmed(record),
        "verifiedBy": record.get("verifiedBy"),
        "verifiedByUsername": record.get("verifiedByUsername"),
        "verifiedAt": record.get("verifiedAt"),
        "bankCreditConfirmedAt": record.get("bankCreditConfirmedAt"),
        "canComplete": not reasons,
        "blockingReasons": reasons,
        # ── historical only ──
        "checks": {k: record.get(k) is True for k in LEGACY_CHECK_KEYS},
        **{f: record.get(f) for f in LEGACY_TEXT_FIELDS},
        "verifiedAmount": record.get("verifiedAmount"),
        "verifiedAccountRef": record.get("verifiedAccountRef"),
    }
