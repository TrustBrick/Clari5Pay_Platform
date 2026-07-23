"""Agent Management → isolated Agent Transaction subsystem (operator workflow).

A COMPLETELY SEPARATE ledger for third-party agent deposit/withdrawal requests. It reads and
writes ONLY the ``agent_transaction`` / ``agent_transaction_audit`` tables (plus the shared
``AgentMaster`` for agent details and the shared ``AuditLog`` for the unified Agent Audit Trail).

It NEVER touches the merchant payment system — no import of, read from, or write to the
merchant ``transactions``/``account_master``/settlement/treasury/risk code. Agent transactions
therefore never affect merchant balances, settlements, treasury, risk, reports or Transaction
History. Business-scoped (``merchant_business`` = the caller's business) and demo-gated in main.py.

Roles (Data Operator = DEO): Deposit → SUPERVISOR/MANAGER/DEO/DEPOSIT_OPERATOR; Withdrawal →
SUPERVISOR/MANAGER/DEO/WITHDRAWAL_OPERATOR; Manage → SUPERVISOR/MANAGER/DEO; Approve/Reject →
SUPERVISOR/MANAGER. Overview/list/audit → any of the five (base access).
"""
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    AgentAccount, AgentMaster, AgentMemberBankAccount, AgentTransaction, AgentTransactionAudit,
    User, UserRole,
)
from app.core.deps import get_current_agent_operator, agent_role_in
from app.api.routes.system_logs import record_agent_audit

router = APIRouter(prefix="/api/agent-txns", tags=["agent-transactions-ledger"])

IST = timezone(timedelta(hours=5, minutes=30))

# Per-capability role sets (base module access is enforced by get_current_agent_operator).
DEPOSIT_ROLES = ("SUPERVISOR", "MANAGER", "DEO", "DEPOSIT_OPERATOR")
WITHDRAWAL_ROLES = ("SUPERVISOR", "MANAGER", "DEO", "WITHDRAWAL_OPERATOR")
MANAGE_ROLES = ("SUPERVISOR", "MANAGER", "DEO")
APPROVE_ROLES = ("SUPERVISOR", "MANAGER")

# Instruction options offered on the Agent Deposit/Withdrawal Request forms. "High Priority" and
# "No Call" were retired in favour of "Telegram"; legacy rows may still carry the old values, which
# stay valid for display but are no longer offered as new choices.
INSTRUCTIONS = {"WHATSAPP_ONLY", "CALL_ONLY", "WHATSAPP_CALL", "TELEGRAM", "OTHER"}
MEMBERSHIP_TYPES = {"ONLINE", "OFFLINE"}

# ── Deposit workflow — the SAME status labels and order as the merchant deposit workflow
# (app/api/routes/transactions.py), with one deliberate difference: every step the ADMIN performs
# for a merchant is performed by the DATA OPERATOR here.
#
#   Create Agent Deposit Request → ACCOUNT_REQUESTED
#   Submit Account   (Data Operator, from Agent Accounts only) → ACCOUNT_SUBMITTED
#   Upload Slip      (Data Operator)                            → SUPERVISOR_REVIEW
#   Approve          (Supervisor)                               → SLIP_SUBMITTED
#   Mark Deposit     (Data Operator — the merchant flow's Admin step) → DEPOSITED
ST_ACCOUNT_REQUESTED = "ACCOUNT_REQUESTED"
ST_ACCOUNT_SUBMITTED = "ACCOUNT_SUBMITTED"
# CASH names its first two steps after the token, CRYPTO after the wallet — what the operator
# actually submits at Submit Account. BANK/UPI keep ACCOUNT_*. Use _requested_status/
# _submitted_status rather than these directly, so the method picks the right pair.
ST_TOKEN_REQUESTED = "TOKEN_REQUESTED"
ST_TOKEN_SUBMITTED = "TOKEN_SUBMITTED"
ST_WALLET_REQUESTED = "WALLET_REQUESTED"
ST_WALLET_SUBMITTED = "WALLET_SUBMITTED"
ST_MANAGER_REVIEW = "MANAGER_REVIEW"
# SLIP_SUBMITTED means what it says: the operator has uploaded the slip and the Supervisor has NOT
# yet decided. It previously named the step AFTER the Supervisor approved — that step is now
# SUPERVISOR_APPROVED. Rows carrying the old meaning were migrated; see the deposit chain below.
# A SETTLEMENT also sits on SLIP_SUBMITTED as its "ready for the Supervisor to pay" gate, which is
# unchanged and deliberately left alone — every code path that reads it is txn_type-gated.
ST_SLIP_SUBMITTED = "SLIP_SUBMITTED"
ST_SUPERVISOR_APPROVED = "SUPERVISOR_APPROVED"
ST_MANAGER_APPROVED = "MANAGER_APPROVED"
ST_DEPOSITED = "DEPOSITED"
ST_COMPLETED = "COMPLETED"
ST_REJECTED = "REJECTED"
ST_PENDING = "PENDING"
ST_APPROVED = "APPROVED"
# A Cash deposit that a DEO has split among several members becomes a non-crediting CONTAINER: it
# never updates any member balance itself; its auto-completed child deposits credit the members.
# Final (immutable, and NOT in COMPLETED_STATUSES so it moves no money on its own). Fits the
# existing VARCHAR(24) status column — no schema change.
ST_DISTRIBUTED = "DISTRIBUTED"
# ── Settlement chain ──────────────────────────────────────────────────────────
# Settlement Requested → Settlement Accepted → Proof Uploaded → Settled, with Rejected reachable
# from either of the first two. The payment itself happens OFFLINE (cash / bank transfer / crypto);
# the platform only records the workflow and the proof, and never initiates or verifies a payment.
# These are plain strings in the existing VARCHAR(24) status column — no schema change.
ST_SETTLEMENT_REQUESTED = "SETTLEMENT_REQUESTED"
ST_SETTLEMENT_ACCEPTED = "SETTLEMENT_ACCEPTED"
ST_PROOF_UPLOADED = "PROOF_UPLOADED"
ST_SETTLED = "SETTLED"
# Retired: the deposit slip step is SLIP_SUBMITTED now. Kept only so legacy rows still render.
ST_SUPERVISOR_REVIEW = "SUPERVISOR_REVIEW"

# Roles that operate the chain. The Data Operator (DEO) drives every operator step.
OPERATOR_ROLES = ("DEO", "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR")
DEPOSIT_OPERATOR_ROLES = ("DEO", "DEPOSIT_OPERATOR")

# Which statuses count as money that actually moved — the completed-only basis for every financial
# figure, mirroring the merchant rule ("a completed deposit is COMPLETED (legacy) or DEPOSITED").
# APPROVED covers agent rows created before the chain existed; DEPOSITED ends the deposit chain;
# COMPLETED ends the withdrawal/settlement chains.
# SETTLED ends the settlement chain, so it counts exactly like DEPOSITED/COMPLETED do for the
# other two. A settlement that is still Requested/Accepted/Proof-Uploaded has not moved money yet
# and is excluded, and REJECTED never counts — so a rejected settlement cannot touch balances,
# reports or the ledger.
COMPLETED_STATUSES = {ST_APPROVED, ST_DEPOSITED, ST_COMPLETED, ST_SETTLED}
REJECTED_STATUSES = {ST_REJECTED}
# A distributed parent is FINAL (may no longer be managed/decided) but is deliberately NOT in
# COMPLETED_STATUSES, so it never contributes to any member balance or money figure — only its
# child deposits do.
FINAL_STATUSES = COMPLETED_STATUSES | REJECTED_STATUSES | {ST_DISTRIBUTED}

# How the money moves. CASH is the only method Manage Transaction may edit.
TXN_METHODS = {"CASH", "UPI", "BANK", "IMPS", "NEFT", "RTGS", "CRYPTO"}
BANK_LIKE_METHODS = {"BANK", "IMPS", "NEFT", "RTGS"}   # collect a sending bank account
# Settlement is Supervisor-only and needs NO approval: it mirrors the withdrawal chain minus the
# review gate, so it is created ready to pay. Methods are limited to Cash / Bank Transfer / Crypto.
SETTLEMENT_METHODS = {"CASH", "BANK", "CRYPTO"}
# Cash and Crypto follow their own Submit Account step (token / wallet instead of an Agent
# Account) and their own withdrawal gate order. BANK/UPI/IMPS/NEFT/RTGS are untouched.
TOKEN_METHODS = {"CASH"}          # Submit Account captures Token Details + Note + token image
WALLET_METHODS = {"CRYPTO"}       # Submit Account captures a Wallet Address + payment slip
SPECIAL_METHODS = TOKEN_METHODS | WALLET_METHODS


def _requested_status(method: str | None) -> str:
    """The chain's first step, named for what this method actually asks the operator to supply."""
    m = str(method or "").upper()
    if m in TOKEN_METHODS:
        return ST_TOKEN_REQUESTED
    if m in WALLET_METHODS:
        return ST_WALLET_REQUESTED
    return ST_ACCOUNT_REQUESTED


def _submitted_status(method: str | None) -> str:
    """The step after Submit Account — the token / wallet / agent account is now on the record."""
    m = str(method or "").upper()
    if m in TOKEN_METHODS:
        return ST_TOKEN_SUBMITTED
    if m in WALLET_METHODS:
        return ST_WALLET_SUBMITTED
    return ST_ACCOUNT_SUBMITTED


# A transaction can only be routed through an agent of the matching category — cash through a Cash
# agent, a bank transfer through a Bank Transfer agent, crypto through a Crypto agent. The UI narrows
# the agent list to the chosen method; this is the authority, so a request that bypasses the form is
# still refused. (The same category also decides the agent's allowed account types — see
# ALLOWED_ACCOUNT_TYPES in agent_accounts.py.)
_METHOD_CATEGORY = {"CASH": "CASH", "CRYPTO": "CRYPTO", "BANK": "BANK_TRANSFER"}
_CATEGORY_NAME = {"CASH": "Cash", "BANK_TRANSFER": "Bank Transfer", "CRYPTO": "Crypto"}


def _require_agent_serves_method(agent: AgentMaster, method: str | None) -> None:
    want = _METHOD_CATEGORY.get(str(method or "").upper())
    if want is None:
        return                      # unknown/legacy method — leave as-is rather than block
    have = (agent.category or "").upper()
    if have and have != want:
        raise HTTPException(
            status_code=400,
            detail=f"{_CATEGORY_NAME[want]} transactions must use a {_CATEGORY_NAME[want]} agent — "
                   f"{agent.agent_id} is a {_CATEGORY_NAME.get(have, have)} agent.",
        )


def _withdrawal_gate(method: str | None) -> str:
    """Where the Manager decides a withdrawal. CASH/CRYPTO are authorised before the operator
    confirms them, so they wait at TOKEN_SUBMITTED / WALLET_SUBMITTED — the state they are created
    in. BANK/UPI are paid first and reach the Manager at MANAGER_REVIEW (unchanged)."""
    m = str(method or "").upper()
    return _submitted_status(m) if m in SPECIAL_METHODS else ST_MANAGER_REVIEW

# Crypto wallet address — structural format check across the common networks. There is no network
# selector on an agent crypto transaction, so an address is accepted if it is a valid shape on ANY
# of these; this catches typos and garbage without over-constraining a legitimate address. It is a
# format check, not an on-chain existence/checksum proof.
_WALLET_FORMATS = (
    re.compile(r"^0x[0-9a-fA-F]{40}$"),                       # EVM: Ethereum / ERC20 / BSC / Polygon
    re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),               # TRON / TRC20 (base58, 34 chars)
    re.compile(r"^(bc1)[0-9ac-hj-np-z]{11,87}$"),             # Bitcoin bech32 (segwit)
    re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$"),         # Bitcoin legacy P2PKH / P2SH (base58)
    re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),             # Solana (base58, 32–44 chars)
)


def _valid_wallet(addr: str) -> bool:
    a = (addr or "").strip()
    return bool(a) and any(p.match(a) for p in _WALLET_FORMATS)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _business(user: User) -> str:
    return user.name


def _require(user: User, roles: tuple[str, ...], what: str) -> None:
    if not agent_role_in(user, roles):
        raise HTTPException(status_code=403, detail=f"Your role cannot {what}.")


# Which merchant roles may be chosen as — and act as — the Authorized Approver, per request type.
# A DEPOSIT may be approved by either review role; a WITHDRAWAL is a Manager-only authorisation, so
# Supervisors are excluded from the dropdown, the approval queue and the approve/reject actions
# alike. This mirrors APPROVER_ROLES in the merchant module (transactions.py) so both modules run
# the same rule. A MANAGE request keeps the both-roles default; a SETTLEMENT has no approver.
APPROVER_ROLES = {
    "DEPOSIT": ("SUPERVISOR", "MANAGER"),
    "WITHDRAWAL": ("MANAGER",),
}


def _require_sole_approver(user: User, t: AgentTransaction) -> None:
    """The chosen Authorized Approver is the SOLE reviewer of a request (deposit or withdrawal):
    only the specific Manager/Supervisor the operator selected on it may approve or reject — routing
    is by the selected user, not by role. Every other Manager/Supervisor is denied (403).

    On top of that, the reviewer must hold a role that may approve this request type at all: a
    WITHDRAWAL is Manager-only, so a Supervisor is refused even on a legacy row that still names
    one as its approver."""
    allowed = APPROVER_ROLES.get((t.txn_type or "").upper(), APPROVER_ROLES["DEPOSIT"])
    if not agent_role_in(user, allowed):
        raise HTTPException(
            status_code=403,
            detail=("Only a Manager can review Agent Withdrawal Requests." if allowed == ("MANAGER",)
                    else "Only a Manager or Supervisor can review Agent requests."))
    if t.approver_user_id and user.id != t.approver_user_id:
        raise HTTPException(status_code=403, detail="Only the selected Authorized Approver can review this request.")


def _ist_parts(dt: datetime | None):
    """(iso_utc, ist_date, ist_time) for a stored (naive-UTC) timestamp."""
    if not dt:
        return None, None, None
    aware = dt.replace(tzinfo=timezone.utc)
    ist = aware.astimezone(IST)
    return aware.isoformat().replace("+00:00", "Z"), ist.strftime("%Y-%m-%d"), ist.strftime("%I:%M:%S %p")


async def _next_serial(db: AsyncSession, model_col, prefix: str) -> str:
    """Next global serial for `prefix` (e.g. AGD000001) — mirrors _next_agent_id (max()+1)."""
    vals = (await db.execute(select(model_col).where(model_col.like(f"{prefix}%")))).scalars().all()
    maxn = 0
    for v in vals:
        try:
            maxn = max(maxn, int(str(v)[len(prefix):]))
        except (TypeError, ValueError):
            continue
    return f"{prefix}{maxn + 1:06d}"


def _agent_serial(agent: AgentMaster) -> int:
    """The Agent's numeric ID — the digits of its canonical AGT… serial (AGT000015 → 15).

    The match is anchored to the WHOLE serial on purpose. Grabbing whatever digits happen to trail
    the id turns a non-standard one like "AGTP0" into agent 0, and makes "AGTP1"/"AGTP2" collide
    with the real AGT000001/AGT000002. Anything not of the form AGT<digits> falls back to the row
    id, which is unique by definition.
    """
    m = re.fullmatch(r"AGT(\d+)", str(agent.agent_id or "").strip().upper())
    return int(m.group(1)) if m else int(agent.id)


async def _next_agent_txn_seq(db: AsyncSession, agent_master_id: int) -> int:
    """How many transactions this agent has created so far, + 1 — the per-agent running counter
    that ends every transaction code (…-01, …-02, …). Maintained SEPARATELY for each agent and
    shared across its Deposits, Withdrawals and Settlements, so it is that agent's total.

    Split children are excluded: they are not requests an operator created, and they take their
    code from the parent (``<parent code>-01``), which is why their reference carries a '-'.
    """
    n = (await db.execute(
        select(func.count()).select_from(AgentTransaction).where(
            AgentTransaction.agent_master_id == agent_master_id,
            AgentTransaction.reference_number.notlike("%-%"),
        )
    )).scalar() or 0
    return int(n) + 1


async def _transaction_code(db: AsyncSession, agent: AgentMaster, code_letter: str) -> str:
    """The agent transaction code — ``<agent code>-<leg>-<agent id>-<agent sequence>``.

    e.g. BBO-D-000001-02 = the 2nd transaction created by Agent 1 (BBO), a Deposit. The first
    numeric block identifies the AGENT (000001, 000002, 000015 …); the last is that agent's own
    transaction count, so every agent numbers its transactions from 01 independently. The same
    format applies to Deposits (D), Withdrawals (W) and Settlements (S).
    """
    seq = await _next_agent_txn_seq(db, agent.id)
    return f"{agent.transaction_code}-{code_letter}-{_agent_serial(agent):06d}-{seq:02d}"


def _member_account_row(a: AgentMemberBankAccount) -> dict:
    return {
        "id": a.id, "membershipId": a.membership_id, "memberName": a.member_name,
        "accountHolder": a.account_holder, "accountNumber": a.account_number,
        "ifsc": a.ifsc, "bankName": a.bank_name, "branch": a.branch, "upiId": a.upi_id,
        "isDefault": a.is_default,
        "label": " · ".join(str(b) for b in (a.account_holder, a.account_number or a.upi_id, a.bank_name) if b),
    }


async def _saved_member_accounts(db: AsyncSession, business: str, membership_id: str) -> list[AgentMemberBankAccount]:
    """Payout accounts already on file for this membership, in the ISOLATED agent register."""
    return list((await db.execute(
        select(AgentMemberBankAccount).where(
            AgentMemberBankAccount.merchant_business == business,
            AgentMemberBankAccount.membership_id == membership_id,
        ).order_by(AgentMemberBankAccount.is_default.desc(), AgentMemberBankAccount.id.desc())
    )).scalars().all())


async def _resolve_payout_account(db: AsyncSession, user: User, body: "AgentWithdrawalCreate",
                                  membership_id: str, member_name: str | None) -> AgentMemberBankAccount | None:
    """The account this withdrawal pays out to.

    An existing saved account is used as-is. New details are matched against what is already on
    file for this membership (same account number, or same UPI) and only inserted when genuinely
    new — so re-using a member's account never creates a duplicate.
    """
    business = _business(user)
    if body.payoutAccountId is not None:
        row = (await db.execute(select(AgentMemberBankAccount).where(
            AgentMemberBankAccount.id == body.payoutAccountId,
            AgentMemberBankAccount.merchant_business == business,
            AgentMemberBankAccount.membership_id == membership_id,
        ))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved payout account not found for this membership.")
        return row

    number = (body.payoutAccountNumber or "").strip() or None
    upi = (body.payoutUpiId or "").strip() or None
    if not number and not upi:
        return None                      # e.g. a cash payout — nothing to save

    existing = await _saved_member_accounts(db, business, membership_id)
    for row in existing:                 # de-dupe on the identifying field
        if (number and row.account_number == number) or (upi and row.upi_id and row.upi_id.lower() == upi.lower()):
            return row

    if not body.savePayoutAccount:
        # Use the details for this transaction only, without adding them to the register.
        return AgentMemberBankAccount(
            merchant_business=business, membership_id=membership_id, member_name=member_name,
            account_holder=(body.payoutAccountHolder or None), account_number=number,
            ifsc=(body.payoutIfsc or None), bank_name=(body.payoutBankName or None),
            branch=(body.payoutBranch or None), upi_id=upi,
        )
    row = AgentMemberBankAccount(
        merchant_business=business, membership_id=membership_id, member_name=member_name,
        account_holder=(body.payoutAccountHolder or None), account_number=number,
        ifsc=(body.payoutIfsc or None), bank_name=(body.payoutBankName or None),
        branch=(body.payoutBranch or None), upi_id=upi,
        is_default=not existing,         # the first account on file becomes the default
        created_by=user.username,
    )
    db.add(row)
    await db.flush()
    return row


async def _register_member_account(db: AsyncSession, business: str, user: User, membership_id: str,
                                   member_name: str | None, *, account_holder: str | None = None,
                                   account_number: str | None = None, ifsc: str | None = None,
                                   bank_name: str | None = None, branch: str | None = None,
                                   upi_id: str | None = None) -> AgentMemberBankAccount | None:
    """Register a member's bank/UPI account in the isolated agent register, de-duped on the identifying
    field (account number, or UPI). Returns the existing or newly-added row, or None when there is
    nothing identifying to save. This is the SAME save a withdrawal payout account gets — so a Bank
    Transfer DEPOSIT records the member's Sending Account for later auto-fill, exactly like a payout.
    Cash/Crypto carry no account and no-op here."""
    number = (account_number or "").strip() or None
    upi = (upi_id or "").strip() or None
    if not number and not upi:
        return None
    existing = await _saved_member_accounts(db, business, membership_id)
    for row in existing:                 # de-dupe on the identifying field
        if (number and row.account_number == number) or (upi and row.upi_id and row.upi_id.lower() == upi.lower()):
            return row
    row = AgentMemberBankAccount(
        merchant_business=business, membership_id=membership_id, member_name=member_name,
        account_holder=(account_holder or None), account_number=number,
        ifsc=(ifsc or None), bank_name=(bank_name or None),
        branch=(branch or None), upi_id=upi,
        is_default=not existing,         # the first account on file becomes the default
        created_by=user.username,
    )
    db.add(row)
    await db.flush()
    return row


def _account_detail(a: AgentAccount) -> str:
    """Human-readable payment details for a submitted AGENT account, by type. Stored on the
    transaction so the payer sees exactly what was sent, even if the account changes later."""
    t = str(a.account_type or "").upper()
    if t == "BANK":
        bits = [a.account_holder, a.account_number, a.ifsc, a.bank_name, a.branch]
    elif t == "UPI":
        bits = [a.upi_holder, a.upi_id]
    elif t == "QR":
        bits = [a.label, a.qr_linked_ref]
    elif t == "CRYPTO":
        bits = [a.crypto_asset, a.crypto_network, a.wallet_address]
    else:
        bits = [a.label]
    return " · ".join(str(b).strip() for b in bits if b and str(b).strip()) or (a.label or a.account_ref)


async def _log(db: AsyncSession, t: AgentTransaction, action: str, actor: User, *,
               old_amount: float | None = None, new_amount: float | None = None,
               note: str | None = None, approver_name: str | None = None) -> None:
    """Write the isolated audit row + a parallel AuditLog (AGENT_TXN_*) for the unified trail."""
    db.add(AgentTransactionAudit(
        agent_transaction_id=t.id, reference_number=t.reference_number, action=action,
        old_amount=old_amount, new_amount=new_amount, note=note, approver_name=approver_name,
        merchant_business=t.merchant_business, actor_username=actor.username,
        actor_role=str(actor.merchant_role or "").upper() or None,
    ))
    await record_agent_audit(
        db, f"AGENT_TXN_{action}", actor,
        entity_type="agent_transaction", entity_id=t.reference_number,
        old=None if old_amount is None else f"{old_amount:.2f}",
        new=note or (None if new_amount is None else f"{new_amount:.2f}"),
    )


def _row(t: AgentTransaction) -> dict:
    c_iso, c_date, c_time = _ist_parts(t.created_at)
    u_iso, u_date, u_time = _ist_parts(t.updated_at)
    a_iso, a_date, a_time = _ist_parts(t.approved_at)
    return {
        "id": t.id,
        "referenceNumber": t.reference_number,
        "transactionCode": t.transaction_code,
        "type": t.txn_type,
        "agentMasterId": t.agent_master_id,
        "agentCode": t.agent_code, "agentName": t.agent_name,
        "agentCountry": t.agent_country, "agentState": t.agent_state,
        "agentLocation": t.agent_location, "agentCategory": t.agent_category,
        "membershipId": t.membership_id, "membershipName": t.membership_name,
        "membershipType": t.membership_type,
        "amount": round(t.amount, 2),
        "country": t.txn_country, "state": t.txn_state, "location": t.txn_location,
        "mobile": t.mobile, "mobileCode": t.mobile_code,
        "tokenDetails": t.token_details, "noteNumber": t.note_number,
        # The member's own Reference Number (captured on the withdrawal request), NOT the system
        # serial in `referenceNumber` above.
        "memberReference": t.member_reference,
        "notes": t.notes, "instructions": t.instructions,
        "status": t.status,
        # Transaction type + Sending Account (shown in Transaction Details / All Transactions /
        # Reports / Audit).
        "txnMethod": t.txn_method,
        "senderUpiId": t.sender_upi_id,
        "senderAccountHolder": t.sender_account_holder,
        "senderAccountNumber": t.sender_account_number,
        "senderIfsc": t.sender_ifsc,
        "senderBankName": t.sender_bank_name,
        "senderBranch": t.sender_branch,
        # Account submission (agent account only)
        "agentAccountId": t.agent_account_id,
        "agentAccountRef": t.agent_account_ref,
        "agentAccountType": t.agent_account_type,
        "agentAccountDetail": t.agent_account_detail,
        "walletAddress": t.wallet_address, "accountProof": t.account_proof,
        "accountSubmittedBy": t.account_submitted_by,
        "accountSubmittedDate": _ist_parts(t.account_submitted_at)[1],
        "accountSubmittedTime": _ist_parts(t.account_submitted_at)[2],
        # Slip
        "slipImage": t.slip_image,
        "slipSubmittedBy": t.slip_submitted_by,
        "slipSubmittedDate": _ist_parts(t.slip_submitted_at)[1],
        "slipSubmittedTime": _ist_parts(t.slip_submitted_at)[2],
        # Review gate
        "supervisorName": t.supervisor_name,
        "managerName": t.manager_name,
        "reviewRemark": t.review_remark,
        # Withdrawal payout account (where the money is sent)
        "payoutAccountId": t.payout_account_id,
        "payoutAccountHolder": t.payout_account_holder,
        "payoutAccountNumber": t.payout_account_number,
        "payoutIfsc": t.payout_ifsc,
        "payoutBankName": t.payout_bank_name,
        "payoutBranch": t.payout_branch,
        "payoutUpiId": t.payout_upi_id,
        # Mark Deposit
        "depositedBy": t.deposited_by,
        "depositedDate": _ist_parts(t.deposited_at)[1],
        "depositedTime": _ist_parts(t.deposited_at)[2],
        "depositUtr": t.deposit_utr, "depositProof": t.deposit_proof,
        "sentForApproval": t.sent_for_approval,
        "approverName": t.approver_name,
        "approvedBy": t.approved_by, "approvedDate": a_date, "approvedTime": a_time, "approvedAt": a_iso,
        # When the money actually moved, whichever route completed it. Null until it completes.
        "completedAt": _ist_parts(t.completed_at)[0],
        "completedDate": _ist_parts(t.completed_at)[1],
        "completedTime": _ist_parts(t.completed_at)[2],
        "linkedDepositId": t.linked_deposit_id,
        "createdBy": t.created_by, "createdAt": c_iso, "createdDate": c_date, "createdTime": c_time,
        "updatedBy": t.updated_by, "updatedAt": u_iso, "updatedDate": u_date, "updatedTime": u_time,
    }


async def _get_agent(db: AsyncSession, business: str, agent_master_id: int) -> AgentMaster:
    agent = (await db.execute(
        select(AgentMaster).where(AgentMaster.id == agent_master_id, AgentMaster.merchant_business == business)
    )).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found for this business.")
    if str(agent.status).upper() != "ACTIVE":
        raise HTTPException(status_code=400, detail="Selected agent is inactive.")
    return agent


# ── Per-member Available Balance (isolated agent ledger) ──────────────────────────────────────
# The Agent Module keeps a balance PER Membership ID, computed from that member's own completed
# agent transactions — the transactions ARE the ledger, exactly as the Merchant module computes its
# balance from Transaction rows rather than a stored column (so it can never drift). The formula is
# the Merchant one, applied per leg with each transaction's OWN agent fee, and settlement charging
# the agent's Settlement Fee (the one deliberate difference the spec calls out vs the merchant, which
# pools settlement under pay-out):
#     deposit    → + amount · (1 − pay_in_fee%)      net deposit credited
#     withdrawal → − amount · (1 + pay_out_fee%)     amount + pay-out commission
#     settlement → − amount · (1 + settlement_fee%)  amount + settlement commission
# Balances are scoped to (business, membership_id) and never mix between members.
def _leg_rate(agent: AgentMaster | None, txn_type: str) -> float:
    """The agent's fee for this leg, as a fraction. NULL/legacy → 0."""
    if agent is None:
        return 0.0
    pct = {"DEPOSIT": agent.pay_in_fee, "WITHDRAWAL": agent.pay_out_fee,
           "SETTLEMENT": agent.settlement_fee}.get(txn_type)
    return (pct or 0.0) / 100.0


def _is_split_child(t: AgentTransaction) -> bool:
    """A child deposit produced by splitting a Cash Deposit (see distribute_deposit).

    ``linked_deposit_id`` on a DEPOSIT is only ever set by the split, so it identifies a child
    exactly (on a WITHDRAWAL the same column means something else — the deposit whose agent was
    auto-fetched — hence the txn_type guard).
    """
    return t.txn_type == "DEPOSIT" and t.linked_deposit_id is not None


def _txn_rate(t: AgentTransaction, agent: AgentMaster | None) -> float:
    """The commission rate that applies to ONE transaction.

    Identical to _leg_rate for every ordinary transaction. A split child charges NOTHING: the
    parent Cash Deposit already had the agent's Pay-In commission deducted at the deposit, and the
    split only allocates what remained. Charging the leg fee again on each child was deducting the
    same commission twice — once from the parent's distributable amount and once from every child.
    """
    return 0.0 if _is_split_child(t) else _leg_rate(agent, t.txn_type)


# An agent's Category decides how it settles, so the Settlement Method follows from it rather than
# being picked by a user: a Cash agent settles in cash, a Bank Transfer agent by bank transfer, a
# Crypto agent in crypto. Mirrors categoryForMethod() on the frontend, inverted.
_CATEGORY_SETTLEMENT_METHOD = {"CASH": "CASH", "BANK_TRANSFER": "BANK", "CRYPTO": "CRYPTO"}


def _settlement_method_for(agent: AgentMaster) -> str:
    method = _CATEGORY_SETTLEMENT_METHOD.get(str(agent.category or "").upper())
    if not method:
        raise HTTPException(
            status_code=400,
            detail="This agent has no settlement category configured (expected Cash, Bank Transfer or Crypto).",
        )
    return method


def _signed_leg(t: AgentTransaction, agent: AgentMaster | None) -> float:
    """A transaction's effect on the member balance: deposit credits net, withdrawal/settlement
    debit gross + their own commission."""
    r = _txn_rate(t, agent)
    amt = t.amount or 0.0
    if t.txn_type == "DEPOSIT":
        return amt * (1 - r)
    return -amt * (1 + r)     # WITHDRAWAL / SETTLEMENT


def _completion_note(t: AgentTransaction, agent: AgentMaster | None, actor: User, before: float) -> str:
    """Audit line for a completed leg — captures the amount, this leg's commission, the member's
    Available Balance before → after, the Membership ID, the Agent ID and who acted (the timestamp
    is on the audit row itself). Written into the existing audit note (no schema change)."""
    rate = _txn_rate(t, agent)
    commission = round((t.amount or 0.0) * rate, 2)
    after = round(before + _signed_leg(t, agent), 2)
    leg = {"DEPOSIT": "Deposit", "WITHDRAWAL": "Withdrawal", "SETTLEMENT": "Settlement"}.get(t.txn_type, t.txn_type)
    return (f"{leg} completed by {actor.username}. "
            f"Amount ₹{(t.amount or 0):,.2f}, {leg} Commission ₹{commission:,.2f}. "
            f"Available Balance ₹{before:,.2f} → ₹{after:,.2f}. "
            f"Membership {t.membership_id or '—'}, Agent {agent.agent_id if agent else '—'}.")


async def _member_balance(db: AsyncSession, business: str, membership_id: str) -> dict:
    """Available + spendable balance for one Membership ID within the agent ledger.

    `available` counts COMPLETED transactions only (a completed deposit is DEPOSITED, or the legacy
    APPROVED; a completed withdrawal/settlement is COMPLETED) — the figure shown to the operator and
    reports. `spendable` additionally reserves this member's IN-FLIGHT (not-yet-final) withdrawals
    and settlements, exactly like the Merchant spendable guard, so two requests cannot each pass and
    together overdraw. `spendable` is used only to validate a new withdrawal/settlement.
    """
    mid = (membership_id or "").strip()
    if not mid:
        return {"available": 0.0, "spendable": 0.0}
    rows = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business,
        AgentTransaction.membership_id == mid))).scalars().all()
    if not rows:
        return {"available": 0.0, "spendable": 0.0}
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()}

    available = 0.0
    reserved = 0.0
    for t in rows:
        ag = agents.get(t.agent_master_id)
        if t.status in COMPLETED_STATUSES:
            available += _signed_leg(t, ag)
        elif t.status not in REJECTED_STATUSES and t.txn_type in ("WITHDRAWAL", "SETTLEMENT"):
            # In-flight debit (pending withdrawal/settlement) — reserve it against spending.
            reserved += -_signed_leg(t, ag)     # _signed_leg is negative for these
    available = round(available, 2)
    return {"available": available, "spendable": round(available - reserved, 2)}


async def _agent_balance(db: AsyncSession, business: str, agent_master_id: int) -> dict:
    """Available + spendable balance held BY ONE AGENT, in the same completed-only, per-leg terms
    as _member_balance — just scoped to the agent rather than the member.

    This is the figure a withdrawal is validated against: a member may withdraw up to what the
    selected agent currently holds, net of the agent's withdrawal fee. (It replaces the old
    per-member limit, which capped a withdrawal at the member's own prior deposits.)
    `spendable` additionally reserves the agent's in-flight withdrawals/settlements, so two
    concurrent requests cannot each pass and together overdraw the agent.
    """
    rows = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business,
        AgentTransaction.agent_master_id == agent_master_id))).scalars().all()
    if not rows:
        return {"available": 0.0, "spendable": 0.0}
    agent = (await db.execute(select(AgentMaster).where(
        AgentMaster.id == agent_master_id))).scalar_one_or_none()

    available = 0.0
    reserved = 0.0
    for t in rows:
        if t.status in COMPLETED_STATUSES:
            available += _signed_leg(t, agent)
        elif t.status not in REJECTED_STATUSES and t.txn_type in ("WITHDRAWAL", "SETTLEMENT"):
            reserved += -_signed_leg(t, agent)     # _signed_leg is negative for these
    available = round(available, 2)
    return {"available": available, "spendable": round(available - reserved, 2)}


def _max_withdrawable(available: float, rate: float) -> float:
    """The largest withdrawal an available balance covers once the withdrawal fee is taken off.

    A withdrawal debits amount + its own commission (_signed_leg), so the amount that exactly
    consumes `available` is available ÷ (1 + rate) — i.e. the available balance less the
    withdrawal fee charged on it. Negative balances floor at zero.
    """
    return round(max(0.0, available) / (1 + rate), 2)


async def _member_summary(db: AsyncSession, business: str, membership_id: str) -> dict:
    """Full financial summary for one Membership ID — the Balance Enquiry payload. Reuses the same
    completed-only, per-leg logic as _member_balance (deposit net of Pay-In, withdrawal/settlement
    plus their Pay-Out/Settlement commission), just broken out per component. `found` distinguishes
    an unknown membership from a known one with no completed transactions (all zeros)."""
    mid = (membership_id or "").strip()
    rows = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business,
        AgentTransaction.membership_id == mid))).scalars().all() if mid else []
    if not rows:
        return {"found": False}
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()}

    agg = {"DEPOSIT": [0, 0.0, 0.0], "WITHDRAWAL": [0, 0.0, 0.0], "SETTLEMENT": [0, 0.0, 0.0]}  # [count, amount, commission]
    member_name = None
    last_dt = None
    for t in rows:
        member_name = member_name or t.membership_name
        if t.created_at and (last_dt is None or t.created_at > last_dt):
            last_dt = t.created_at
        if t.status in COMPLETED_STATUSES and t.txn_type in agg:
            amt = t.amount or 0.0
            agg[t.txn_type][0] += 1
            agg[t.txn_type][1] += amt
            agg[t.txn_type][2] += round(amt * _txn_rate(t, agents.get(t.agent_master_id)), 2)
    dep_n, dep_amt, dep_com = agg["DEPOSIT"]
    wd_n, wd_amt, wd_com = agg["WITHDRAWAL"]
    st_n, st_amt, st_com = agg["SETTLEMENT"]
    available = round((dep_amt - dep_com) - (wd_amt + wd_com) - (st_amt + st_com), 2)
    _, l_date, l_time = _ist_parts(last_dt)
    return {
        "found": True,
        "membershipId": mid, "memberName": member_name,
        "depositCount": dep_n, "totalDeposits": round(dep_amt, 2), "depositCommission": round(dep_com, 2),
        "withdrawalCount": wd_n, "totalWithdrawals": round(wd_amt, 2), "withdrawalCommission": round(wd_com, 2),
        "settlementCount": st_n, "totalSettlements": round(st_amt, 2), "settlementCommission": round(st_com, 2),
        "availableBalance": available,
        "lastTransactionDate": f"{l_date} {l_time}".strip() if last_dt else None,
    }


async def _agent_performance(db: AsyncSession, business: str) -> dict:
    """Agent financial performance for the Agent Dashboard — overall totals, a per-agent breakdown,
    rankings and single-agent highs. Completed-only (COMPLETED_STATUSES), commission per leg from
    each agent's own Pay-In / Pay-Out / Settlement fee — the SAME calculation as everywhere else, no
    new formula. This is AGENT performance; member balances live only on Balance Enquiry."""
    agents = (await db.execute(select(AgentMaster).where(
        AgentMaster.merchant_business == business).order_by(AgentMaster.id))).scalars().all()
    txns = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business))).scalars().all()

    # Per-agent accumulator: each leg → [count, amount, commission].
    zero = lambda: {"DEPOSIT": [0, 0.0, 0.0], "WITHDRAWAL": [0, 0.0, 0.0], "SETTLEMENT": [0, 0.0, 0.0]}
    acc = {a.id: zero() for a in agents}
    last_dt: dict = {}
    for t in txns:
        if t.created_at and (t.agent_master_id not in last_dt or t.created_at > last_dt[t.agent_master_id]):
            last_dt[t.agent_master_id] = t.created_at
        if t.status in COMPLETED_STATUSES and t.txn_type in acc.get(t.agent_master_id, {}):
            leg = acc[t.agent_master_id][t.txn_type]
            amt = t.amount or 0.0
            leg[0] += 1
            leg[1] += amt
            leg[2] += round(amt * _txn_rate(t, _agent_or(agents, t.agent_master_id)), 2)

    rows = []
    for a in agents:
        d = acc[a.id]
        dep_c, dep_a, dep_com = d["DEPOSIT"]
        wd_c, wd_a, wd_com = d["WITHDRAWAL"]
        st_c, st_a, st_com = d["SETTLEMENT"]
        total_com = round(dep_com + wd_com + st_com, 2)
        _, l_date, l_time = _ist_parts(last_dt.get(a.id))
        rows.append({
            "agentMasterId": a.id, "agentId": a.agent_id, "agentName": a.full_name,
            "category": a.category, "status": a.status,
            "country": a.country, "currency": a.currency,
            "createdDate": a.date_of_creation.isoformat() if a.date_of_creation else None,
            "depositCount": dep_c, "depositAmount": round(dep_a, 2), "depositCommission": round(dep_com, 2),
            "withdrawalCount": wd_c, "withdrawalAmount": round(wd_a, 2), "withdrawalCommission": round(wd_com, 2),
            "settlementCount": st_c, "settlementAmount": round(st_a, 2), "settlementCommission": round(st_com, 2),
            "totalCommission": total_com, "totalTransactions": dep_c + wd_c + st_c,
            "lastTransactionDate": f"{l_date} {l_time}".strip() if last_dt.get(a.id) else None,
        })

    def _top(key, n=5):
        return [{"agentId": r["agentId"], "agentName": r["agentName"], "value": r[key]}
                for r in sorted(rows, key=lambda r: -r[key]) if r[key] > 0][:n]

    def _high(key):
        best = max(rows, key=lambda r: r[key], default=None)
        return {"agentId": best["agentId"], "agentName": best["agentName"], "value": best[key]} \
            if best and best[key] > 0 else None

    return {
        "overall": {
            "totalDepositAmount": round(sum(r["depositAmount"] for r in rows), 2),
            "totalWithdrawalAmount": round(sum(r["withdrawalAmount"] for r in rows), 2),
            "totalSettlementAmount": round(sum(r["settlementAmount"] for r in rows), 2),
            "totalDepositCommission": round(sum(r["depositCommission"] for r in rows), 2),
            "totalWithdrawalCommission": round(sum(r["withdrawalCommission"] for r in rows), 2),
            "totalSettlementCommission": round(sum(r["settlementCommission"] for r in rows), 2),
            "totalCommission": round(sum(r["totalCommission"] for r in rows), 2),
            "activeAgents": sum(1 for a in agents if str(a.status).upper() == "ACTIVE"),
            "inactiveAgents": sum(1 for a in agents if str(a.status).upper() != "ACTIVE"),
            "totalTransactions": sum(r["totalTransactions"] for r in rows),
        },
        "agents": sorted(rows, key=lambda r: -r["totalCommission"]),
        "rankings": {
            "topDeposit": _top("depositAmount"), "topWithdrawal": _top("withdrawalAmount"),
            "topSettlement": _top("settlementAmount"), "topCommission": _top("totalCommission"),
        },
        "highest": {
            "deposit": _high("depositAmount"), "withdrawal": _high("withdrawalAmount"),
            "settlement": _high("settlementAmount"), "commission": _high("totalCommission"),
        },
    }


def _agent_or(agents, agent_id):
    for a in agents:
        if a.id == agent_id:
            return a
    return None


async def _agent_profile(db: AsyncSession, business: str, agent_master_id: int) -> dict:
    """One agent's profile — details, lifetime business/commission totals (completed-only, per-leg
    fee), the members it has served, and its recent activity. Reuses the same calculation as
    everywhere; scoped to the agent's own business. No document store exists, so no documents."""
    agent = (await db.execute(select(AgentMaster).where(
        AgentMaster.id == agent_master_id, AgentMaster.merchant_business == business))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found for this business.")
    txns = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business,
        AgentTransaction.agent_master_id == agent_master_id)
        .order_by(AgentTransaction.id.desc()))).scalars().all()

    leg = {"DEPOSIT": [0, 0.0, 0.0], "WITHDRAWAL": [0, 0.0, 0.0], "SETTLEMENT": [0, 0.0, 0.0]}
    members: dict = {}   # membership_id -> {name, deposits, withdrawals, settlements, count}
    for t in txns:
        m = members.setdefault(t.membership_id, {
            "membershipId": t.membership_id, "memberName": t.membership_name,
            "deposits": 0.0, "withdrawals": 0.0, "settlements": 0.0, "count": 0})
        m["count"] += 1
        if t.status in COMPLETED_STATUSES and t.txn_type in leg:
            amt = t.amount or 0.0
            leg[t.txn_type][0] += 1
            leg[t.txn_type][1] += amt
            leg[t.txn_type][2] += round(amt * _txn_rate(t, agent), 2)
            key = {"DEPOSIT": "deposits", "WITHDRAWAL": "withdrawals", "SETTLEMENT": "settlements"}[t.txn_type]
            m[key] += amt
    dep_c, dep_a, dep_com = leg["DEPOSIT"]
    wd_c, wd_a, wd_com = leg["WITHDRAWAL"]
    st_c, st_a, st_com = leg["SETTLEMENT"]
    total_commission = round(dep_com + wd_com + st_com, 2)
    return {
        "agent": {
            "agentId": agent.agent_id, "agentName": agent.full_name, "category": agent.category,
            "country": agent.country, "state": agent.state, "location": agent.location,
            "currency": agent.currency, "status": agent.status,
            "createdDate": agent.date_of_creation.isoformat() if agent.date_of_creation else None,
        },
        "totals": {
            "totalBusiness": round(dep_a + wd_a + st_a, 2),
            "depositCount": dep_c, "totalDeposits": round(dep_a, 2), "depositCommission": round(dep_com, 2),
            "withdrawalCount": wd_c, "totalWithdrawals": round(wd_a, 2), "withdrawalCommission": round(wd_com, 2),
            "settlementCount": st_c, "totalSettlements": round(st_a, 2), "settlementCommission": round(st_com, 2),
            "commissionEarned": total_commission, "totalTransactions": dep_c + wd_c + st_c,
        },
        "members": sorted(({**m, "deposits": round(m["deposits"], 2), "withdrawals": round(m["withdrawals"], 2),
                            "settlements": round(m["settlements"], 2)} for m in members.values()),
                          key=lambda x: -(x["deposits"] + x["withdrawals"] + x["settlements"])),
        "activity": [_row(t) for t in txns[:12]],
    }


async def _resolve_approver(db: AsyncSession, business: str, approver_user_id: int | None,
                            txn_type: str = "DEPOSIT"):
    """Validate the chosen Authorized Approver against the roles that may approve THIS request type
    (see APPROVER_ROLES): a deposit takes a Supervisor or a Manager, a withdrawal a Manager only.
    Sending a Supervisor's id on a withdrawal — from the API or any other manual route — is a 400,
    which is what keeps the rule enforceable outside the UI."""
    if approver_user_id is None:
        return None, None
    allowed = APPROVER_ROLES.get((txn_type or "").upper(), APPROVER_ROLES["DEPOSIT"])
    u = (await db.execute(select(User).where(User.id == approver_user_id))).scalar_one_or_none()
    ok = (u and u.role == UserRole.MERCHANT and u.name == business
          and str(u.merchant_role or "").upper() in allowed)
    if not ok:
        who = "a Manager" if allowed == ("MANAGER",) else "a Supervisor or Manager"
        raise HTTPException(
            status_code=400,
            detail=f"Authorized approver for an Agent {txn_type.title()} must be {who} of your business.")
    return u.id, u.username


# ── Request models ──────────────────────────────────────────────────────────────
class _Base(BaseModel):
    agentMasterId: int
    membershipId: str
    membershipName: str | None = None
    membershipType: str
    amount: float
    country: str | None = None
    state: str | None = None
    location: str | None = None
    mobile: str | None = None
    mobileCode: str | None = None            # dial code for `mobile`
    notes: str | None = None
    instructions: str | None = None
    sentForApproval: bool = False
    approverUserId: int | None = None
    # ── Transaction type + Sending Account (mirrors the merchant Deposit Request) ──
    # Supplied by the customer/agent and typed in by the Data Operator — NOT generated. Mandatory
    # on a Deposit; a Withdrawal/Settlement has no customer-supplied token, so those keep their
    # generated values.
    tokenDetails: str | None = None
    noteNumber: str | None = None
    # The Reference Number the MEMBER supplies during the withdrawal, typed in by the operator.
    # Mandatory on a WITHDRAWAL (with noteNumber); unused by deposits/settlements.
    memberReference: str | None = None
    walletAddress: str | None = None         # CRYPTO: replaces token/note on a withdrawal
    txnMethod: str | None = None            # CASH | UPI | BANK | IMPS | NEFT | RTGS | CRYPTO
    senderUpiId: str | None = None          # UPI the payment is sent from
    senderAccountHolder: str | None = None
    senderAccountNumber: str | None = None
    senderIfsc: str | None = None
    senderBankName: str | None = None
    senderBranch: str | None = None


class AgentAccountSubmit(BaseModel):
    """What the Data Operator submits, by method:
      • BANK/UPI/IMPS/NEFT/RTGS → agentAccountId (one of that agent's own Agent Accounts)
      • CASH                    → tokenDetails + noteNumber + accountProof (token image)
      • CRYPTO                  → walletAddress + accountProof (crypto payment slip)
    """
    agentAccountId: int | None = None
    tokenDetails: str | None = None
    noteNumber: str | None = None
    walletAddress: str | None = None
    accountProof: str | None = None          # data URL


class AgentSlipSubmit(BaseModel):
    """Payment evidence. Both are mandatory: the UTR is the payment reference (there is no
    separate Reference Number) and the slip image is the proof."""
    slipImage: str | None = None            # data URL — required
    utr: str | None = None                  # required; the transaction's only payment reference
    # "Send To Approval" — the Authorized Approver, now chosen at this Pay/Upload Slip step (deposits),
    # once the slip has uploaded. Optional server-side; the frontend enforces it.
    approverUserId: int | None = None


class AgentReviewAction(BaseModel):
    remark: str


class AgentPaymentDetails(BaseModel):
    """Method-specific execution details the CREATING operator submits AFTER approval: CASH →
    tokenDetails; CRYPTO → walletAddress (+ optional txHash); BANK → slipImage + utr (reference);
    UPI → utr + slipImage (screenshot).

    Submitting these SAVES the payment information; it does not complete the withdrawal (see
    /payout vs /complete). The Unique Note Number is captured on the request form now — it stays
    accepted here so a legacy request, or a correction, can still set it."""
    noteNumber: str | None = None
    tokenDetails: str | None = None
    walletAddress: str | None = None
    txHash: str | None = None
    slipImage: str | None = None       # data URL
    utr: str | None = None


class AgentMarkDeposit(BaseModel):
    """Mark Deposit is a confirmation, not an upload: the slip and UTR were captured at the
    Pay / Upload Slip step and are only displayed for review. Kept as an empty body so the
    endpoint contract is unchanged for callers."""
    pass


class AgentDepositCreate(_Base):
    pass


class AgentWithdrawalCreate(_Base):
    linkedDepositId: int | None = None
    # ── Payout account — where the money is SENT. Either an existing saved account for this
    # membership, or new details that get saved for re-use.
    payoutAccountId: int | None = None
    payoutAccountHolder: str | None = None
    payoutAccountNumber: str | None = None
    payoutIfsc: str | None = None
    payoutBankName: str | None = None
    payoutBranch: str | None = None
    payoutUpiId: str | None = None
    savePayoutAccount: bool = True


class AgentSettlementCreate(AgentWithdrawalCreate):
    """Same shape as a withdrawal (incl. the payout account) — settlement simply skips approval."""
    pass


class AgentManage(BaseModel):
    amount: float
    notes: str | None = None
    sentForApproval: bool = False
    approverUserId: int | None = None


def _validate_common(body: _Base, txn_type: str) -> None:
    if body.amount is None or body.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction Amount must be greater than zero.")
    # A SETTLEMENT is an offline merchant<->agent payment with no member on either side, so it
    # carries no membership at all. Deposits and withdrawals still require one.
    if txn_type != "SETTLEMENT":
        if body.membershipType.upper() not in MEMBERSHIP_TYPES:
            raise HTTPException(status_code=400, detail="Membership Type must be Online or Offline.")
        if not body.membershipId.strip():
            raise HTTPException(status_code=400, detail="Membership ID is required.")
    if body.notes and len(body.notes) > 100:
        raise HTTPException(status_code=400, detail="Notes must be 100 characters or fewer.")
    if body.instructions and body.instructions.upper() not in INSTRUCTIONS:
        raise HTTPException(status_code=400, detail="Invalid instruction option.")
    if body.txnMethod and body.txnMethod.upper() not in TXN_METHODS:
        raise HTTPException(status_code=400, detail="Invalid Transaction Type.")
    method = (body.txnMethod or "").upper()
    # No method-specific payment detail (Token / Note / Wallet / slip) is captured at CREATE for
    # ANY transaction type — the create step only records the request. Which detail is collected,
    # and when, depends on the method but always happens LATER:
    #   • DEPOSIT     → CASH: Token + Note, CRYPTO: Wallet, BANK/UPI: an agent account — all at
    #                   the Submit Account step.
    #   • WITHDRAWAL  → approve-first: after the chosen approver approves, the creating operator
    #                   submits the method-specific Payment Details (CASH → Token, CRYPTO → Wallet,
    #                   BANK → payment slip + reference, UPI → UTR + screenshot).
    #   • SETTLEMENT  → an offline merchant<->agent payment; proof is uploaded in its own chain.
    # Requiring a Token / Note / Wallet here was a leftover from the pre-approval design and wrongly
    # rejected a Bank Transfer (or UPI/Cash) withdrawal at creation for missing "Token Details".
    # Tokens belong to CASH and a wallet to CRYPTO — never to a bank transfer — and none of them
    # exist yet at creation, so the create step enforces none of them.
    #
    # The two EXCEPTIONS are the Unique Note Number and the Reference Number on a WITHDRAWAL: the
    # member hands both over during the withdrawal, so they exist before the request is raised and
    # are captured here rather than at the post-approval payment step.
    if txn_type == "WITHDRAWAL":
        if not (body.noteNumber or "").strip():
            raise HTTPException(status_code=400, detail="Unique Note Number is required.")
        if not (body.memberReference or "").strip():
            raise HTTPException(status_code=400, detail="Reference Number is required.")
    if txn_type == "SETTLEMENT" and method and method not in SETTLEMENT_METHODS:
        raise HTTPException(status_code=400, detail="Settlement method must be Cash, Bank Transfer or Crypto.")
    # A deposit names the Sending Account it comes FROM (same rule the merchant form applies). A
    # withdrawal names a payout account instead, validated in create_withdrawal.
    if txn_type == "DEPOSIT":
        if method in BANK_LIKE_METHODS and not (body.senderAccountHolder or "").strip():
            raise HTTPException(status_code=400, detail="Sending Account Holder is required for a bank transfer.")
        if method in BANK_LIKE_METHODS and not (body.senderAccountNumber or "").strip():
            raise HTTPException(status_code=400, detail="Sending Account Number is required for a bank transfer.")
        if method == "UPI" and "@" not in (body.senderUpiId or ""):
            raise HTTPException(status_code=400, detail="Enter a valid Sender UPI ID (name@bank).")


async def _create(db: AsyncSession, user: User, body: _Base, txn_type: str,
                  prefix: str, code_letter: str, linked_deposit_id: int | None,
                  payout: AgentMemberBankAccount | None = None) -> dict:
    business = _business(user)
    _validate_common(body, txn_type)
    method = (body.txnMethod or "").upper()
    agent = await _get_agent(db, business, body.agentMasterId)
    _require_agent_serves_method(agent, method)

    # A withdrawal draws on what the SELECTED AGENT currently holds — not on the member's own
    # prior transactions. A member may withdraw up to the agent's Available Balance after the
    # agent's configured withdrawal fee, so the amount PLUS this leg's commission must fit inside
    # it (same completed-only basis every other figure uses). The member's own history no longer
    # caps the withdrawal.
    if txn_type == "WITHDRAWAL":
        rate = _leg_rate(agent, txn_type)
        bal = await _agent_balance(db, business, agent.id)
        required = (body.amount or 0.0) * (1 + rate)
        if required > bal["spendable"] + 1e-6:
            raise HTTPException(
                status_code=400,
                detail=(f"Insufficient Agent Balance.\n"
                        f"Available with {agent.agent_id}: ₹{bal['spendable']:,.2f}\n"
                        f"Maximum Withdrawable: ₹{_max_withdrawable(bal['spendable'], rate):,.2f}\n"
                        f"Requested Amount + Commission: ₹{required:,.2f}"),
            )

    # Approver roles are scoped to the request type: a WITHDRAWAL accepts a Manager only.
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId, txn_type)

    ref = await _next_serial(db, AgentTransaction.reference_number, prefix)
    # The operator-entered note number must stay unique, so a clash is reported plainly instead
    # of surfacing as a database integrity error. A CASH/CRYPTO deposit has none yet.
    note_no = (body.noteNumber or "").strip() or None
    entered_token = (body.tokenDetails or "").strip() or None
    if note_no:
        clash = (await db.execute(select(AgentTransaction.id).where(
            AgentTransaction.note_number == note_no))).scalars().first()
        if clash:
            raise HTTPException(status_code=400, detail="This Unique Note Number is already used.")
    member_ref = (body.memberReference or "").strip() or None
    txn_code = await _transaction_code(db, agent, code_letter)

    # Membership name: use the entered value, else auto-fill from a prior agent transaction.
    membership_name = (body.membershipName or "").strip()
    if not membership_name:
        prior = (await db.execute(
            select(AgentTransaction.membership_name).where(
                AgentTransaction.merchant_business == business,
                AgentTransaction.membership_id == body.membershipId.strip(),
                AgentTransaction.membership_name.is_not(None),
            ).order_by(AgentTransaction.id.desc())
        )).scalars().first()
        membership_name = prior or None

    # A Bank Transfer DEPOSIT registers the member's Sending Account (the account the money comes FROM
    # is the member's own), so their bank details auto-fill on the next deposit/withdrawal — the same
    # registration a withdrawal payout account gets. De-duped; Cash/Crypto carry no account and no-op.
    if txn_type == "DEPOSIT":
        await _register_member_account(
            db, business, user, body.membershipId.strip(), membership_name,
            account_holder=body.senderAccountHolder, account_number=body.senderAccountNumber,
            ifsc=body.senderIfsc, bank_name=body.senderBankName, branch=body.senderBranch,
            upi_id=body.senderUpiId,
        )

    t = AgentTransaction(
        reference_number=ref, transaction_code=txn_code, txn_type=txn_type,
        merchant_business=business,
        agent_master_id=agent.id, agent_code=agent.agent_id, agent_name=agent.full_name,
        agent_country=agent.country, agent_state=agent.state, agent_location=agent.location,
        agent_category=agent.category,
        membership_id=("" if txn_type == "SETTLEMENT" else body.membershipId.strip()),
        membership_name=(None if txn_type == "SETTLEMENT" else membership_name),
        membership_type=("" if txn_type == "SETTLEMENT" else body.membershipType.upper()),
        amount=round(body.amount, 2),
        txn_country=body.country, txn_state=body.state, txn_location=body.location,
        mobile=body.mobile, mobile_code=(body.mobileCode or None),
        token_details=entered_token, note_number=note_no, member_reference=member_ref,
        wallet_address=((body.walletAddress or '').strip() or None),
        notes=(body.notes or None), instructions=(body.instructions.upper() if body.instructions else None),
        # Transaction type + Sending Account, captured exactly like the merchant Deposit Request.
        txn_method=(body.txnMethod.upper() if body.txnMethod else None),
        sender_upi_id=(body.senderUpiId or None),
        sender_account_holder=(body.senderAccountHolder or None),
        sender_account_number=(body.senderAccountNumber or None),
        sender_ifsc=(body.senderIfsc or None),
        sender_bank_name=(body.senderBankName or None),
        sender_branch=(body.senderBranch or None),
        # Chain entry points, mirroring the merchant workflow: a DEPOSIT starts at Account
        # Request; a WITHDRAWAL goes straight to the Manager review gate.
        # Chain entry points. A WITHDRAWAL is created with its payout account already captured,
        # so it lands ready for the operator to pay and upload the slip (ACCOUNT_SUBMITTED is
        # the merchant status for exactly that "details known, now pay" step). A SETTLEMENT
        # needs no approval at all and is created ready for the Supervisor to pay.
        # CASH/CRYPTO withdrawals are authorised BEFORE the operator confirms them, so they open
        # at the Manager gate; BANK/UPI withdrawals are unchanged (operator pays, Manager last).
        # A WITHDRAWAL now always enters approval FIRST: every method is created at MANAGER_REVIEW
        # ("Waiting for Approval"). The chosen approver approves/rejects, then the creating operator
        # submits the method-specific payment details (which completes it). Payment capture no longer
        # happens at creation.
        status=(_requested_status(method) if txn_type == "DEPOSIT"
                else ST_MANAGER_REVIEW if txn_type == "WITHDRAWAL"
                else ST_SETTLEMENT_REQUESTED if txn_type == "SETTLEMENT" else ST_PENDING),
        # Payout account (withdrawals) — where the money is sent.
        payout_account_id=(payout.id if payout is not None else None),
        payout_account_holder=(payout.account_holder if payout else None),
        payout_account_number=(payout.account_number if payout else None),
        payout_ifsc=(payout.ifsc if payout else None),
        payout_bank_name=(payout.bank_name if payout else None),
        payout_branch=(payout.branch if payout else None),
        payout_upi_id=(payout.upi_id if payout else None),
        sent_for_approval=bool(body.sentForApproval),
        approver_user_id=approver_id, approver_name=approver_name,
        linked_deposit_id=linked_deposit_id,
        created_by=user.username, created_by_id=user.id,
    )
    db.add(t)
    await db.flush()   # assign t.id for the audit FK
    await _log(db, t, "CREATED", user, new_amount=t.amount, note=f"{txn_type.title()} request created")
    if t.sent_for_approval:
        await _log(db, t, "SENT_FOR_APPROVAL", user, approver_name=approver_name,
                   note=f"Sent to {approver_name or 'an approver'} for approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── Form data & membership lookup ────────────────────────────────────────────────
@router.get("/form-data")
async def form_data(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """Dropdown data for the create/manage forms — active agents + authorized approvers."""
    business = _business(user)
    agents = (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business, AgentMaster.status == "ACTIVE")
        .order_by(AgentMaster.id.desc())
    )).scalars().all()
    approvers = (await db.execute(
        select(User).where(User.role == UserRole.MERCHANT, User.name == business)
    )).scalars().all()
    return {
        "agents": [{
            "id": a.id, "agentId": a.agent_id, "name": a.full_name, "country": a.country,
            "state": a.state, "location": a.location, "category": a.category,
            "transactionCode": a.transaction_code, "currency": a.currency,
            # Withdrawal commission % (pay_out_fee) — lets the Withdrawal form show the member's
            # Maximum Withdrawable Amount after commission, using the same fee the server charges.
            "withdrawalFee": a.pay_out_fee or 0,
        } for a in agents],
        # Deposit approvers — Supervisors + Managers (unchanged).
        "approvers": [{"id": u.id, "name": u.username, "role": str(u.merchant_role or "").upper()}
                      for u in approvers if str(u.merchant_role or "").upper() in APPROVER_ROLES["DEPOSIT"]],
        # Withdrawal approvers — Managers only, so the Withdrawal form can never offer a Supervisor.
        "withdrawalApprovers": [{"id": u.id, "name": u.username, "role": str(u.merchant_role or "").upper()}
                                for u in approvers if str(u.merchant_role or "").upper() in APPROVER_ROLES["WITHDRAWAL"]],
        "instructions": sorted(INSTRUCTIONS),
        "membershipTypes": ["ONLINE", "OFFLINE"],
        # Transaction types offered on the request form (drives the Sending Account fields and
        # gates Manage Transaction, which is CASH-only).
        # Agent transactions move by Cash, Bank Transfer or Crypto only — these mirror the three
        # agent categories, so an agent of a category can always serve its own method.
        "txnMethods": ["CASH", "BANK", "CRYPTO"],
    }


@router.get("/agent-accounts/{agent_master_id}")
async def agent_accounts_for(agent_master_id: int, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_agent_operator)):
    """Active AGENT accounts for one agent — the ONLY source the Account Submission step may pick
    from. Never reads merchant accounts, preserving the subsystem's isolation."""
    rows = (await db.execute(
        select(AgentAccount).where(
            AgentAccount.merchant_business == _business(user),
            AgentAccount.agent_master_id == agent_master_id,
            AgentAccount.status == "ACTIVE",
        ).order_by(AgentAccount.is_default.desc(), AgentAccount.id.desc())
    )).scalars().all()
    return [{
        "id": a.id, "accountRef": a.account_ref, "accountType": a.account_type,
        "label": a.label, "currency": a.currency, "isDefault": a.is_default,
        "detail": _account_detail(a), "qrImage": a.qr_image if str(a.account_type).upper() == "QR" else None,
    } for a in rows]


@router.get("/member/{membership_id}")
async def member_lookup(membership_id: str, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_agent_operator)):
    """Auto-fetch (isolated): membership name from prior agent transactions, and the agent from the
    latest agent DEPOSIT for this membership (for the Withdrawal form). Never reads merchant members."""
    business = _business(user)
    rows = (await db.execute(
        select(AgentTransaction).where(
            AgentTransaction.merchant_business == business,
            AgentTransaction.membership_id == membership_id.strip(),
        ).order_by(AgentTransaction.id.desc())
    )).scalars().all()
    membership_name = next((r.membership_name for r in rows if r.membership_name), None)
    dep = next((r for r in rows if r.txn_type == "DEPOSIT"), None)
    latest_deposit = None if dep is None else {
        "agentMasterId": dep.agent_master_id, "agentCode": dep.agent_code, "agentName": dep.agent_name,
        "country": dep.agent_country, "state": dep.agent_state, "location": dep.agent_location,
        "category": dep.agent_category, "depositId": dep.id, "reference": dep.reference_number,
    }
    saved = await _saved_member_accounts(db, business, membership_id.strip())
    bal = await _member_balance(db, business, membership_id.strip())
    return {
        "membershipId": membership_id.strip(), "membershipName": membership_name,
        "latestDeposit": latest_deposit,
        # Payout accounts already on file for this membership (isolated agent register).
        "savedAccounts": [_member_account_row(a) for a in saved],
        # This member's Available Balance in the agent ledger — shown on the Withdrawal/Settlement
        # form so the operator sees it before submitting. The server still validates on create.
        "availableBalance": bal["available"],
    }


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("/deposit")
async def create_deposit(body: AgentDepositCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    _require(user, DEPOSIT_ROLES, "create Agent Deposit Requests")
    return await _create(db, user, body, "DEPOSIT", "AGD", "D", None)


@router.post("/withdrawal")
async def create_withdrawal(body: AgentWithdrawalCreate, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    _require(user, WITHDRAWAL_ROLES, "create Agent Withdrawal Requests")
    linked = None
    if body.linkedDepositId is not None:
        d = (await db.execute(select(AgentTransaction).where(
            AgentTransaction.id == body.linkedDepositId,
            AgentTransaction.merchant_business == _business(user),
            AgentTransaction.txn_type == "DEPOSIT",
        ))).scalar_one_or_none()
        linked = d.id if d else None
    # Resolve the payout account first: an existing saved account, or new details that are saved
    # against this membership for re-use (matched so a repeat account is never duplicated).
    membership_id = body.membershipId.strip()
    payout = await _resolve_payout_account(db, user, body, membership_id, (body.membershipName or None))
    return await _create(db, user, body, "WITHDRAWAL", "AGW", "W", linked, payout=payout)


@router.post("/settlement")
async def create_settlement(body: AgentSettlementCreate, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Create an Agent Settlement, opening the settlement chain at Settlement Requested.

    The Settlement Method is NOT chosen by the user: it is derived from the assigned Agent's
    configured Category (Cash / Bank Transfer / Crypto), so the recorded method can never disagree
    with the agent actually performing the offline payment. Anything sent in txnMethod is ignored.
    """
    _require(user, ("SUPERVISOR",), "create Agent Settlement Requests")
    agent = await _get_agent(db, _business(user), body.agentMasterId)
    # The Transaction Type chosen on the form narrows the agent list to that category, so the two
    # always agree; deriving the method from the agent here is the server-side guard that makes a
    # mismatched pair impossible even if the client sends one.
    body.txnMethod = _settlement_method_for(agent)
    return await _create(db, user, body, "SETTLEMENT", "AGS", "S", None, payout=None)


# ── Deposit chain: Submit Account → Upload Slip → Supervisor Approval → Mark Deposit ──────
# Mirrors the merchant deposit workflow step-for-step; the Data Operator performs the steps the
# Admin performs for a merchant. Every step writes an isolated audit row via _log().
async def _load_own(db: AsyncSession, business: str, txn_id: int) -> AgentTransaction:
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    return t


def _require_status(t: AgentTransaction, expected: str, what: str) -> None:
    if t.status != expected:
        raise HTTPException(status_code=400, detail=f"This transaction is not awaiting {what}.")


def _require_deposit(t: AgentTransaction) -> None:
    if t.txn_type != "DEPOSIT":
        raise HTTPException(status_code=400, detail="This action applies to Agent Deposits only.")


@router.post("/{txn_id}/account-submit")
async def account_submit(txn_id: int, body: AgentAccountSubmit, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    """Data Operator submits the AGENT account the payer should send to → ACCOUNT_SUBMITTED.

    The account is looked up in ``agent_account`` scoped to this business and to the transaction's
    own agent — a merchant account can never be selected, keeping the subsystem isolated.
    """
    _require(user, DEPOSIT_OPERATOR_ROLES, "submit an account for Agent Deposits")
    business = _business(user)
    t = await _load_own(db, business, txn_id)
    _require_deposit(t)
    _require_status(t, _requested_status(t.txn_method), "an account submission")
    method = str(t.txn_method or "").upper()

    if method in TOKEN_METHODS:
        # CASH — the customer's token IS the reference; there is no image. The operator enters the
        # token and note exactly as the customer supplied them, and the Supervisor verifies those
        # against the slip uploaded at the next step. accountProof stays accepted (and is still
        # required for CRYPTO below) but is never collected for cash.
        token = (body.tokenDetails or "").strip()
        note = (body.noteNumber or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="Token Details are required.")
        if not note:
            raise HTTPException(status_code=400, detail="Unique Note Number is required.")
        clash = (await db.execute(select(AgentTransaction.id).where(
            AgentTransaction.note_number == note, AgentTransaction.id != t.id))).scalars().first()
        if clash:
            raise HTTPException(status_code=400, detail="This Unique Note Number is already used.")
        t.token_details = token
        t.note_number = note
        note_txt = f"Token {token} / note {note} submitted"

    elif method in WALLET_METHODS:
        # CRYPTO — the operator types the wallet the funds were sent to, plus the payment slip.
        wallet = (body.walletAddress or "").strip()
        if not wallet:
            raise HTTPException(status_code=400, detail="Crypto Wallet Address is required.")
        if not _valid_wallet(wallet):
            raise HTTPException(status_code=400, detail="Enter a valid crypto wallet address.")
        if not body.accountProof:
            raise HTTPException(status_code=400, detail="Crypto payment slip is required.")
        t.wallet_address = wallet
        t.account_proof = body.accountProof
        note_txt = f"Wallet {wallet} submitted"

    else:
        # BANK / UPI / IMPS / NEFT / RTGS — unchanged: pick one of this agent's Agent Accounts.
        if body.agentAccountId is None:
            raise HTTPException(status_code=400, detail="Select an agent account to send.")
        acct = (await db.execute(select(AgentAccount).where(
            AgentAccount.id == body.agentAccountId,
            AgentAccount.merchant_business == business,
            AgentAccount.agent_master_id == t.agent_master_id,
        ))).scalar_one_or_none()
        if acct is None:
            raise HTTPException(status_code=404, detail="Agent account not found for this agent.")
        if str(acct.status).upper() != "ACTIVE":
            raise HTTPException(status_code=400, detail="Selected agent account is inactive.")
        t.agent_account_id = acct.id
        t.agent_account_ref = acct.account_ref
        t.agent_account_type = acct.account_type
        t.agent_account_detail = _account_detail(acct)
        note_txt = f"Agent account {acct.account_ref} submitted"

    t.account_submitted_by = user.username
    t.account_submitted_at = datetime.utcnow()
    t.status = _submitted_status(t.txn_method)
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, t.status, user, note=note_txt)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/slip")
async def submit_slip(txn_id: int, body: AgentSlipSubmit, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_agent_operator)):
    """Data Operator pays and uploads the slip → enters the Supervisor review gate."""
    _require(user, DEPOSIT_OPERATOR_ROLES, "submit a slip for Agent Deposits")
    business = _business(user)
    t = await _load_own(db, business, txn_id)
    _require_deposit(t)
    _require_status(t, _submitted_status(t.txn_method), "a slip")
    # The slip image is always mandatory — captured once here and reused unchanged for the rest of
    # the workflow (Approvals, Mark Deposit, Details, Reports). Cash has no UTR: money changes hands
    # in person and no rail issues a reference, so the slip is the only proof. Every other method is
    # paid over a rail that does issue one, and still requires it.
    if not body.slipImage:
        raise HTTPException(status_code=400, detail="Payment slip image is required.")
    _is_cash = str(t.txn_method or "").upper() in TOKEN_METHODS
    if not _is_cash and not (body.utr or "").strip():
        raise HTTPException(status_code=400, detail="UTR Number is required.")

    t.slip_image = body.slipImage
    if (body.utr or "").strip():
        t.deposit_utr = body.utr.strip()      # the payment reference; Mark Deposit displays it
    t.slip_submitted_by = user.username
    t.slip_submitted_at = datetime.utcnow()
    t.status = ST_SLIP_SUBMITTED             # straight to the Supervisor queue (as the merchant flow does)
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    # "Send To Approval": the Authorized Approver is chosen at this step (after the slip uploads),
    # not at creation. Record who the deposit is addressed to; the Supervisor review is unchanged.
    if body.approverUserId is not None:
        t.approver_user_id, t.approver_name = await _resolve_approver(db, business, body.approverUserId, "DEPOSIT")
        t.sent_for_approval = True
    await _log(db, t, "SLIP_SUBMITTED", user, note="Slip submitted — awaiting Supervisor approval")
    if t.approver_name:
        await _log(db, t, "SENT_FOR_APPROVAL", user, approver_name=t.approver_name,
                   note=f"Sent to {t.approver_name} for approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/supervisor/approve")
async def supervisor_approve(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_agent_operator)):
    """The chosen Authorized Approver approves a deposit under review → SUPERVISOR_APPROVED
    (awaiting Mark Deposit). Only the specific approver selected on the request may act (403 otherwise)."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_deposit(t)
    _require_sole_approver(user, t)
    _require_status(t, ST_SLIP_SUBMITTED, "Supervisor review")
    t.status = ST_SUPERVISOR_APPROVED
    t.supervisor_name = user.username
    t.supervisor_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SUPERVISOR_APPROVED", user, note=remark)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/supervisor/reject")
async def supervisor_reject(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """The chosen Authorized Approver rejects a deposit under review → REJECTED. Only the specific
    approver selected on the request may act (403 otherwise)."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_deposit(t)
    _require_sole_approver(user, t)
    _require_status(t, ST_SLIP_SUBMITTED, "Supervisor review")
    t.status = ST_REJECTED
    t.supervisor_name = user.username
    t.supervisor_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SUPERVISOR_REJECTED", user, note=remark)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/mark-deposit")
async def mark_deposit(txn_id: int, body: AgentMarkDeposit, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_agent_operator)):
    """Data Operator marks an approved deposit as Deposited → DEPOSITED.

    The merchant workflow's Admin 'Mark Deposited' step, performed by the Data Operator. It is a
    confirmation only: the slip and UTR captured at Pay / Upload Slip are reused as-is and are
    never re-uploaded or overwritten here.
    """
    _require(user, DEPOSIT_OPERATOR_ROLES, "mark Agent Deposits as deposited")
    business = _business(user)
    t = await _load_own(db, business, txn_id)
    _require_deposit(t)
    _require_status(t, ST_SUPERVISOR_APPROVED, "Mark Deposit")
    # Balance BEFORE this deposit counts (it is not yet DEPOSITED, so excluded), for the audit line.
    _before = (await _member_balance(db, business, t.membership_id))["available"]
    _agent = (await db.execute(select(AgentMaster).where(AgentMaster.id == t.agent_master_id))).scalar_one_or_none()
    t.status = ST_DEPOSITED
    t.deposited_by = user.username
    t.deposited_at = datetime.utcnow()
    t.completed_at = t.deposited_at       # the deposit completes here

    # deposit_utr / slip_image are left exactly as captured at the slip step — no duplicate
    # upload, no overwrite of the original.
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "DEPOSITED", user, new_amount=t.amount, note=_completion_note(t, _agent, user, _before))
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── Withdrawal chain: Manager Approval → Pay / Upload Slip → Completed ────────────
# Mirrors the merchant withdrawal workflow (create → MANAGER_REVIEW → approved → SLIP_SUBMITTED →
# paid → COMPLETED); the Data Operator performs the payout the Admin performs for a merchant.
def _require_withdrawal(t: AgentTransaction) -> None:
    if t.txn_type != "WITHDRAWAL":
        raise HTTPException(status_code=400, detail="This action applies to Agent Withdrawals only.")


@router.get("/member-accounts/{membership_id}")
async def member_accounts(membership_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_agent_operator)):
    """Payout accounts saved against a Membership ID in the isolated agent register."""
    rows = await _saved_member_accounts(db, _business(user), membership_id.strip())
    return [_member_account_row(a) for a in rows]


@router.get("/balance-enquiry/{membership_id}")
async def balance_enquiry(membership_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_agent_operator)):
    """Read-only financial summary for a Membership ID (Balance Enquiry). Uses the same completed-
    only, per-leg agent calculation as every other balance figure. Access is the standard agent
    operator/manager gate — no new permission. `found:false` when the membership is unknown."""
    return await _member_summary(db, _business(user), membership_id.strip())


@router.get("/agent/{agent_master_id}/balance")
async def agent_balance(agent_master_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_agent_operator)):
    """What the selected agent currently holds, and the most a member may withdraw from it.

    Drives the Agent Withdrawal Request form: a member may withdraw up to the agent's Available
    Balance after the agent's configured withdrawal fee. Same completed-only, per-leg calculation
    as every other balance; `spendable` reserves the agent's in-flight withdrawals/settlements and
    is what the create endpoint validates against, so the form shows the figure the server enforces.
    """
    business = _business(user)
    agent = await _get_agent(db, business, agent_master_id)
    bal = await _agent_balance(db, business, agent_master_id)
    rate = _leg_rate(agent, "WITHDRAWAL")
    return {
        "agentMasterId": agent.id, "agentId": agent.agent_id, "agentName": agent.full_name,
        "available": bal["available"], "spendable": bal["spendable"],
        "withdrawalFeePct": round(rate * 100, 4),
        "withdrawalFee": round(bal["spendable"] - _max_withdrawable(bal["spendable"], rate), 2),
        "maxWithdrawable": _max_withdrawable(bal["spendable"], rate),
    }


@router.get("/{txn_id}/commission")
async def txn_commission(txn_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    """Commission breakdown for one transaction — the exact figures behind it (item 6): the leg's
    commission %, commission amount, net amount, and the member's Available Balance before → after.
    Computed with the same per-leg agent fee; balance is completed-only. For an in-flight row the
    'after' is the projected balance once it completes."""
    business = _business(user)
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    agent = (await db.execute(select(AgentMaster).where(AgentMaster.id == t.agent_master_id))).scalar_one_or_none()
    rate = _txn_rate(t, agent)
    amt = t.amount or 0.0
    commission = round(amt * rate, 2)
    # Deposit credits net (amount − commission); withdrawal/settlement deduct amount + commission.
    net = round(amt - commission, 2) if t.txn_type == "DEPOSIT" else round(amt + commission, 2)
    # Balance before = the member's completed balance EXCLUDING this row; after = before + its leg.
    rows = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business,
        AgentTransaction.membership_id == t.membership_id))).scalars().all()
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()}
    before = round(sum(_signed_leg(r, agents.get(r.agent_master_id))
                       for r in rows if r.id != t.id and r.status in COMPLETED_STATUSES), 2)
    after = round(before + _signed_leg(t, agent), 2)
    return {
        "agentId": t.agent_code, "agentName": t.agent_name, "membershipId": t.membership_id,
        "amount": round(amt, 2), "commissionPct": round(rate * 100, 4),
        "commissionAmount": commission, "netAmount": net,
        "balanceBefore": before, "balanceAfter": after,
    }


@router.get("/performance")
async def agent_performance(db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Agent financial performance for the Agent Dashboard — overall totals, per-agent breakdown,
    rankings and single-agent highs. Completed-only; commission per leg from each agent's own fee.
    AGENT performance only — no member/membership balances (those live on Balance Enquiry)."""
    return await _agent_performance(db, _business(user))


@router.get("/agent/{agent_master_id}/profile")
async def agent_profile(agent_master_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_agent_operator)):
    """One agent's profile: details, lifetime totals/commission, the members it served, and its
    recent activity. Same completed-only per-leg calculation; the standard agent access gate."""
    return await _agent_profile(db, _business(user), agent_master_id)


@router.post("/{txn_id}/manager/approve")
async def manager_approve(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_agent_operator)):
    """The chosen Authorized Approver (Manager or Supervisor) approves a withdrawal.

    Approval is the business authorisation only — it does NOT move money. Every method goes
    MANAGER_REVIEW ("Waiting for Approval") → MANAGER_APPROVED ("Approved"); the creating operator
    then submits the method-specific payment details, which completes it.
    """
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_withdrawal(t)
    _require_sole_approver(user, t)
    _require_status(t, ST_MANAGER_REVIEW, "approval")
    t.status = ST_MANAGER_APPROVED
    t.manager_name = user.username
    t.manager_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "MANAGER_APPROVED", user, new_amount=t.amount,
               note=f"{remark} — approved; awaiting payment details from the operator")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/manager/reject")
async def manager_reject(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    """The chosen Authorized Approver (Manager or Supervisor) rejects a withdrawal → REJECTED."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_withdrawal(t)
    _require_sole_approver(user, t)
    _require_status(t, ST_MANAGER_REVIEW, "rejection")
    t.status = ST_REJECTED
    t.manager_name = user.username
    t.manager_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "MANAGER_REJECTED", user, note=remark)
    await db.commit()
    await db.refresh(t)
    return _row(t)


async def _load_own_withdrawal_for_payment(db: AsyncSession, user: User, txn_id: int) -> AgentTransaction:
    """The withdrawal whose payment details / completion the CREATING operator is acting on."""
    t = await _load_own(db, _business(user), txn_id)
    if t.txn_type == "SETTLEMENT":
        raise HTTPException(
            status_code=400,
            detail="Agent Settlements are completed through the settlement workflow (accept, upload proof, settle).",
        )
    if t.txn_type != "WITHDRAWAL":
        raise HTTPException(status_code=400, detail="This action applies to Agent Withdrawals only.")
    _require(user, ("DEO", "WITHDRAWAL_OPERATOR"), "pay Agent Withdrawals")
    # Only the operator who CREATED the request may submit its payment details / complete it.
    if t.created_by_id and user.id != t.created_by_id:
        raise HTTPException(status_code=403,
                            detail="Only the operator who created this withdrawal can submit its payment details.")
    return t


def _missing_payment_detail(t: AgentTransaction) -> str | None:
    """Which method-specific payment detail is still missing, as a message — None when the record
    holds everything its Withdrawal Type needs. The same rule the Submit Payment Details popup
    applies, expressed against the STORED row so it can also gate completion."""
    method = str(t.txn_method or "").upper()
    if method in TOKEN_METHODS:                        # CASH → Token Number
        return None if (t.token_details or "").strip() else "Token Number is required."
    if method in WALLET_METHODS:                       # CRYPTO → Wallet Address
        return None if (t.wallet_address or "").strip() else "Wallet Address is required."
    if not t.slip_image:                               # BANK → Slip; UPI → Screenshot
        return "Payment slip image is required." if method == "BANK" else "Payment screenshot is required."
    if not (t.deposit_utr or "").strip():
        return "Reference Number is required." if method == "BANK" else "UTR Number is required."
    return None


@router.post("/{txn_id}/payout")
async def payout_withdrawal(txn_id: int, body: AgentPaymentDetails, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Submit Payment Details — the CREATING operator records the method-specific execution
    details and uploads the payment proof AFTER approval.

    This is a DATA UPDATE ONLY: it saves the file, saves the payment details and updates the
    payment information, and deliberately leaves the transaction's status exactly where the
    approval workflow put it. Uploading proof used to flip the row straight to COMPLETED, which
    moved the money as a side effect of an upload; completing is now the separate, explicit
    /complete step below, so a status only ever changes through the workflow. It may be called
    again to correct a detail while the withdrawal is still awaiting completion.
    """
    t = await _load_own_withdrawal_for_payment(db, user, txn_id)
    # Payment details are submitted only AFTER the chosen approver has approved the request.
    _require_status(t, ST_MANAGER_APPROVED, "payment details")
    method = str(t.txn_method or "").upper()
    # Unique Note Number — captured on the Create Agent Withdrawal Request form now (the member
    # supplies it during the withdrawal). It stays accepted here so a legacy request raised before
    # that change, or a correction, can still set it; the uniqueness rule is unchanged.
    note = (body.noteNumber or "").strip()
    if not note and not (t.note_number or "").strip():
        raise HTTPException(status_code=400, detail="Unique Note Number is required.")
    if note and note != (t.note_number or ""):
        clash = (await db.execute(select(AgentTransaction.id).where(
            AgentTransaction.note_number == note, AgentTransaction.id != t.id))).scalars().first()
        if clash:
            raise HTTPException(status_code=400, detail="This Unique Note Number is already used.")
        t.note_number = note
    # Method-specific execution details — mandatory ones enforced per Withdrawal Type.
    if method in TOKEN_METHODS:                        # CASH → Token Number
        if not (body.tokenDetails or "").strip():
            raise HTTPException(status_code=400, detail="Token Number is required.")
        t.token_details = body.tokenDetails.strip()
    elif method in WALLET_METHODS:                     # CRYPTO → Wallet Address (+ optional Tx Hash)
        if not (body.walletAddress or "").strip():
            raise HTTPException(status_code=400, detail="Wallet Address is required.")
        t.wallet_address = body.walletAddress.strip()
        if (body.txHash or "").strip():
            t.deposit_utr = body.txHash.strip()
    else:                                              # BANK → Slip + Reference; UPI → UTR + Screenshot
        if not body.slipImage:
            raise HTTPException(status_code=400,
                                detail=("Payment slip image is required." if method == "BANK"
                                        else "Payment screenshot is required."))
        if not (body.utr or "").strip():
            raise HTTPException(status_code=400,
                                detail=("Reference Number is required." if method == "BANK"
                                        else "UTR Number is required."))
        t.slip_image = body.slipImage
        t.deposit_utr = body.utr.strip()
    t.slip_submitted_by = user.username
    t.slip_submitted_at = datetime.utcnow()
    # NOTE: t.status is intentionally NOT touched here.
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "PAYMENT_DETAILS_SUBMITTED", user,
               note=f"Payment details saved by {user.username} — status unchanged, awaiting completion")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/complete")
async def complete_withdrawal(txn_id: int, db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_agent_operator)):
    """Complete an approved withdrawal whose payment details are on the record → COMPLETED.

    The explicit act that moves the money (the member's balance is deducted here and the
    completion is audited), separated from the proof upload so that uploading a file can never
    change a status on its own. Only reachable after the chosen approver approved the request, so
    the approval workflow still owns every transition into a final state.
    """
    t = await _load_own_withdrawal_for_payment(db, user, txn_id)
    _require_status(t, ST_MANAGER_APPROVED, "completion")
    missing = _missing_payment_detail(t)
    if missing:
        raise HTTPException(status_code=400, detail=f"Submit the payment details first — {missing}")
    if not (t.note_number or "").strip():
        raise HTTPException(status_code=400, detail="Submit the payment details first — Unique Note Number is required.")
    # Balance BEFORE this leg completes (still in-flight here, so excluded), for the audit line.
    _before = (await _member_balance(db, _business(user), t.membership_id))["available"]
    _agent = (await db.execute(select(AgentMaster).where(AgentMaster.id == t.agent_master_id))).scalar_one_or_none()
    t.status = ST_COMPLETED
    t.completed_at = datetime.utcnow()
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, t.completed_at
    await _log(db, t, "COMPLETED", user, new_amount=t.amount,
               note=_completion_note(t, _agent, user, _before))
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── Settlement chain ─────────────────────────────────────────────────────────────
# Settlement Requested → Settlement Accepted → Proof Uploaded → Settled, Rejected reachable from
# the first two. The payment happens OFFLINE; these endpoints only record the workflow and the
# proof. Nothing here initiates, verifies or simulates a payment.
def _require_settlement(t: AgentTransaction) -> None:
    if t.txn_type != "SETTLEMENT":
        raise HTTPException(status_code=400, detail="This action applies to Agent Settlements only.")


async def _settlement_step(db: AsyncSession, user: User, txn_id: int, expected: str, what: str) -> AgentTransaction:
    """Load a settlement and assert it is at the expected point in the chain. Supervisor-only —
    the same role that raises a settlement drives it to completion."""
    _require(user, ("SUPERVISOR",), "manage Agent Settlements")
    t = await _load_own(db, _business(user), txn_id)
    _require_settlement(t)
    _require_status(t, expected, what)
    return t


@router.post("/{txn_id}/settlement/accept")
async def settlement_accept(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Supervisor accepts the request. The agent then settles OFFLINE — outside Clari5Pay."""
    t = await _settlement_step(db, user, txn_id, ST_SETTLEMENT_REQUESTED, "acceptance")
    t.status = ST_SETTLEMENT_ACCEPTED
    t.supervisor_name = user.username
    t.supervisor_action_at = datetime.utcnow()
    t.review_remark = (body.remark or "").strip() or None
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SETTLEMENT_ACCEPTED", user, new_amount=t.amount,
               note=f"Settlement accepted by {user.username} — payment to be made offline"
                    + (f". {t.review_remark}" if t.review_remark else ""))
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/settlement/reject")
async def settlement_reject(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Reject before the payment process begins — allowed while Requested or Accepted. A rejected
    settlement never reaches SETTLED, so it can never affect balances, reports or the ledger."""
    _require(user, ("SUPERVISOR",), "manage Agent Settlements")
    t = await _load_own(db, _business(user), txn_id)
    _require_settlement(t)
    if t.status not in (ST_SETTLEMENT_REQUESTED, ST_SETTLEMENT_ACCEPTED):
        raise HTTPException(status_code=400,
                            detail="Only a settlement still awaiting payment can be rejected.")
    t.status = ST_REJECTED
    t.supervisor_name = user.username
    t.supervisor_action_at = datetime.utcnow()
    t.review_remark = (body.remark or "").strip() or None
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "REJECTED", user, new_amount=t.amount,
               note=f"Settlement rejected by {user.username}"
                    + (f". {t.review_remark}" if t.review_remark else ""))
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/settlement/proof")
async def settlement_proof(txn_id: int, body: AgentSlipSubmit, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_agent_operator)):
    """Supervisor uploads the proof of the completed offline payment (bank receipt, cash
    acknowledgement or crypto transfer proof). The proof is mandatory — that is the whole point
    of the step. An optional reference (UTR / txn hash) is stored alongside it."""
    t = await _settlement_step(db, user, txn_id, ST_SETTLEMENT_ACCEPTED, "proof upload")
    if not body.slipImage:
        raise HTTPException(status_code=400, detail="Payment proof is required.")
    t.status = ST_PROOF_UPLOADED
    t.slip_image = body.slipImage
    t.slip_submitted_by = user.username
    t.slip_submitted_at = datetime.utcnow()
    if (body.utr or "").strip():
        t.deposit_utr = body.utr.strip()
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "PROOF_UPLOADED", user, new_amount=t.amount,
               note=f"Payment proof uploaded by {user.username}")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/settlement/settle")
async def settlement_settle(txn_id: int, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Mark the settlement Settled. Only reachable once the proof is on the record, so a
    settlement can never complete without evidence of the offline payment."""
    t = await _settlement_step(db, user, txn_id, ST_PROOF_UPLOADED, "settlement")
    _before = (await _member_balance(db, _business(user), t.membership_id))["available"]
    _agent = (await db.execute(select(AgentMaster).where(AgentMaster.id == t.agent_master_id))).scalar_one_or_none()
    t.status = ST_SETTLED
    t.completed_at = datetime.utcnow()
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SETTLED", user, new_amount=t.amount,
               note=_completion_note(t, _agent, user, _before))
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── List / search (Manage Transaction worklist) ──────────────────────────────────
@router.get("")
async def list_txns(status: str | None = None, txn_type: str | None = None, search: str | None = None,
                    date: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """Business-scoped list of agent transactions with optional status/type/search/date filters.
    Manage Transaction calls this with status=PENDING. Search matches reference / membership / agent.

    `status` accepts a comma-separated list as well as a single value — the Manager's approval queue
    needs it, because a withdrawal waits at its gate under a method-dependent name (TOKEN_SUBMITTED /
    WALLET_SUBMITTED for cash/crypto, MANAGER_REVIEW for bank/UPI)."""
    business = _business(user)
    stmt = select(AgentTransaction).where(AgentTransaction.merchant_business == business)
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        if wanted:
            stmt = stmt.where(AgentTransaction.status.in_(wanted))
    if txn_type:
        stmt = stmt.where(AgentTransaction.txn_type == txn_type.upper())
    rows = (await db.execute(stmt.order_by(AgentTransaction.id.desc()))).scalars().all()

    q = (search or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r.reference_number or "").lower()
                or q in (r.membership_id or "").lower() or q in (r.agent_code or "").lower()
                or q in (r.membership_name or "").lower()]

    def _d(r):
        _, d, _t = _ist_parts(r.created_at)
        return d or ""
    if date:
        rows = [r for r in rows if _d(r) == date]
    if date_from:
        rows = [r for r in rows if _d(r) >= date_from]
    if date_to:
        rows = [r for r in rows if _d(r) <= date_to]
    # Attach the per-leg commission to each row (item 4 report columns): Commission %, Commission
    # Amount and Net Amount, using each agent's own fee — the same calculation everywhere else.
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()} if rows else {}
    out = []
    for r in rows:
        rate = _txn_rate(r, agents.get(r.agent_master_id))
        amt = r.amount or 0.0
        commission = round(amt * rate, 2)
        d = _row(r)
        d["commissionPct"] = round(rate * 100, 4)
        d["commissionAmount"] = commission
        d["netAmount"] = round(amt - commission, 2) if r.txn_type == "DEPOSIT" else round(amt + commission, 2)
        out.append(d)
    return out


def _ist_day_bounds(day_str: str):
    """IST calendar day (YYYY-MM-DD) → [start, end) in stored naive-UTC created_at space."""
    y, m, d = (int(p) for p in day_str.split("-"))
    start = datetime(y, m, d) - timedelta(hours=5, minutes=30)
    return start, start + timedelta(days=1)


@router.get("/paged")
async def list_txns_paged(status: str | None = None, status_not: str | None = None,
                          txn_type: str | None = None, txn_method: str | None = None,
                          ref: str | None = None, agent_code: str | None = None,
                          membership_id: str | None = None,
                          search: str | None = None, date: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          page: int = 1, page_size: int = 10,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_agent_operator)):
    """Server-side paginated + filtered Agent Portal feed (additive sibling of the bare-array
    list above; that endpoint stays until every caller is migrated). Status/type/search/date
    filtering and the row count all run in Postgres — never over a full in-memory table load.
    Returns {items, total, page, pageSize, totalPages}; page sizes restricted to 10/25/50/100."""
    business = _business(user)
    stmt = select(AgentTransaction).where(AgentTransaction.merchant_business == business)
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        if wanted:
            stmt = stmt.where(AgentTransaction.status.in_(wanted))
    # Exclusion list (comma-separated), so a worklist can ask for "everything still in flight"
    # without having to enumerate — and stay correct when a new status is added.
    if status_not:
        unwanted = [s.strip().upper() for s in status_not.split(",") if s.strip()]
        if unwanted:
            stmt = stmt.where(AgentTransaction.status.notin_(unwanted))
    if txn_type:
        stmt = stmt.where(AgentTransaction.txn_type == txn_type.strip().upper())
    # Payment method (Cash / Bank Transfer / UPI / Crypto). The All-Transactions screen used to
    # refine this in the browser, which is only correct while it holds the whole table.
    if txn_method:
        stmt = stmt.where(AgentTransaction.txn_method == txn_method.strip().upper())
    # Field-scoped partial matches — the Manage Transaction worklist searches these three
    # independently, which a single combined `search` term cannot express.
    if ref and ref.strip():
        stmt = stmt.where(func.lower(AgentTransaction.reference_number).like(f"%{ref.strip().lower()}%"))
    if agent_code and agent_code.strip():
        stmt = stmt.where(func.lower(AgentTransaction.agent_code).like(f"%{agent_code.strip().lower()}%"))
    if membership_id and membership_id.strip():
        stmt = stmt.where(func.lower(AgentTransaction.membership_id).like(f"%{membership_id.strip().lower()}%"))
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(AgentTransaction.reference_number).like(like),
            func.lower(AgentTransaction.membership_id).like(like),
            func.lower(AgentTransaction.agent_code).like(like),
            func.lower(AgentTransaction.membership_name).like(like),
        ))
    if date:
        s, e = _ist_day_bounds(date)
        stmt = stmt.where(AgentTransaction.created_at >= s, AgentTransaction.created_at < e)
    if date_from:
        s, _e = _ist_day_bounds(date_from)
        stmt = stmt.where(AgentTransaction.created_at >= s)
    if date_to:
        _s, e = _ist_day_bounds(date_to)
        stmt = stmt.where(AgentTransaction.created_at < e)

    page_size = page_size if page_size in (10, 25, 50, 100) else 10
    page = page if page and page >= 1 else 1
    total = int((await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar() or 0)
    rows = (await db.execute(
        stmt.order_by(AgentTransaction.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # Per-leg commission enrichment — batched agent fetch (no N+1), identical maths to the list above.
    agents = {a.id: a for a in (await db.execute(select(AgentMaster).where(
        AgentMaster.id.in_({r.agent_master_id for r in rows})))).scalars().all()} if rows else {}
    items = []
    for r in rows:
        rate = _txn_rate(r, agents.get(r.agent_master_id))
        amt = r.amount or 0.0
        commission = round(amt * rate, 2)
        d = _row(r)
        d["commissionPct"] = round(rate * 100, 4)
        d["commissionAmount"] = commission
        d["netAmount"] = round(amt - commission, 2) if r.txn_type == "DEPOSIT" else round(amt + commission, 2)
        items.append(d)
    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": (total + page_size - 1) // page_size if page_size else 0,
    }


@router.get("/{txn_id}/audit")
async def txn_audit(txn_id: int, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_agent_operator)):
    business = _business(user)
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    rows = (await db.execute(select(AgentTransactionAudit).where(
        AgentTransactionAudit.agent_transaction_id == txn_id).order_by(AgentTransactionAudit.id.desc()))).scalars().all()
    out = []
    for r in rows:
        iso, d, tm = _ist_parts(r.created_at)
        out.append({
            "id": r.id, "action": r.action, "oldAmount": r.old_amount, "newAmount": r.new_amount,
            "note": r.note, "approverName": r.approver_name, "actor": r.actor_username, "role": r.actor_role,
            "createdAt": iso, "createdDate": d, "createdTime": tm,
        })
    return out


# ── Manage Transaction — CASH only; an amount change restarts the approval workflow ─────────
# Only a CASH transaction may be edited. Every other method has moved money over a rail whose
# amount cannot be restated after the fact, so Manage is refused here and hidden in the UI.
MANAGEABLE_METHOD = "CASH"

# Deposit Distribution (DEO): a Cash Deposit may be split among several members once its token has
# been INITIALISED (submitted) and before it is finalised — i.e. any pre-DEPOSITED state after the
# token/account step. In all of these the parent has NOT yet credited its own member, so turning it
# into a non-crediting container loses no money and double-credits no one.
DISTRIBUTABLE_STATUSES = {ST_TOKEN_SUBMITTED, ST_SLIP_SUBMITTED, ST_SUPERVISOR_APPROVED}

# The in-flight order of each chain, and where its approval gate sits. An amount change sends the
# transaction BACK to its gate — never forward — so a deposit still awaiting account submission or
# a slip is not jumped past those steps into an approval it has not earned.
# Both are computed per transaction because the first two deposit steps, and the CASH/CRYPTO
# withdrawal gate, are named after the method (token / wallet / agent account).
def _chain_order(t: AgentTransaction) -> list[str]:
    ty = t.txn_type or ""
    m = str(t.txn_method or "").upper()
    if ty == "DEPOSIT":
        return [_requested_status(m), _submitted_status(m), ST_SLIP_SUBMITTED, ST_SUPERVISOR_APPROVED]
    if ty == "WITHDRAWAL":
        # CASH/CRYPTO: gate → confirm. BANK/UPI: pay → gate (unchanged).
        return ([_submitted_status(m), ST_MANAGER_APPROVED] if m in SPECIAL_METHODS
                else [ST_ACCOUNT_SUBMITTED, ST_MANAGER_REVIEW])
    if ty == "SETTLEMENT":
        return [ST_SETTLEMENT_REQUESTED, ST_SETTLEMENT_ACCEPTED, ST_PROOF_UPLOADED]
    return []


def _gate_for(t: AgentTransaction) -> str | None:
    ty = t.txn_type or ""
    if ty == "DEPOSIT":
        return ST_SLIP_SUBMITTED            # the Supervisor decides once the slip is up
    if ty == "WITHDRAWAL":
        return _withdrawal_gate(t.txn_method)
    if ty == "SETTLEMENT":
        return ST_SETTLEMENT_REQUESTED     # the Supervisor's accept/reject decision point
    return None


def _restart_approval(t: AgentTransaction) -> str | None:
    """Send a transaction back to its approval gate after its amount changed, as if newly created.

    Returns the new status when the transaction had already reached (or passed) its gate, else
    None — a deposit still awaiting account submission or a slip keeps its place, because no
    approval has happened yet and there is nothing to redo. Any prior decision is voided so the
    gate must be passed again from scratch; the audit trail of it is append-only and untouched.
    """
    # _chain_order already accounts for the CASH/CRYPTO withdrawal being gated BEFORE the operator
    # confirms it — the reverse of BANK/UPI. Using the wrong order would let an amount change on an
    # already-approved cash withdrawal skip the Manager gate entirely.
    order = _chain_order(t)
    gate = _gate_for(t)
    if not gate or gate not in order or t.status not in order:
        return None
    if order.index(t.status) < order.index(gate):
        return None                       # not yet at the gate — nothing to restart
    t.supervisor_name = None
    t.supervisor_action_at = None
    t.manager_name = None
    t.manager_action_at = None
    t.status = gate
    return gate


# ── Manage (amount correction) + approval ────────────────────────────────────────
async def _load_pending(db: AsyncSession, business: str, txn_id: int) -> AgentTransaction:
    """An in-flight transaction (not yet Deposited/Completed/Approved/Rejected) — the only kind
    that may still be modified or decided. Accepts every mid-chain status, not just PENDING."""
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    if t.status in FINAL_STATUSES:
        raise HTTPException(status_code=400, detail="This transaction is already finalised and can no longer be modified.")
    return t


@router.patch("/{txn_id}/manage")
async def manage_txn(txn_id: int, body: AgentManage, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_agent_operator)):
    _require(user, MANAGE_ROLES, "manage Agent Transactions")
    business = _business(user)
    t = await _load_pending(db, business, txn_id)
    # CASH only — enforced here, not just hidden in the UI.
    if str(t.txn_method or "").upper() != MANAGEABLE_METHOD:
        raise HTTPException(status_code=400, detail="Only Cash transactions can be managed.")
    if body.amount is None or body.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction Amount must be greater than zero.")
    if body.notes and len(body.notes) > 100:
        raise HTTPException(status_code=400, detail="Notes must be 100 characters or fewer.")
    # Forwarding a managed request follows the same per-type rule as creating one.
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId, t.txn_type)

    old_amount = t.amount
    new_amount = round(body.amount, 2)
    t.updated_by = user.username
    t.updated_by_id = user.id
    t.updated_at = datetime.utcnow()
    if body.notes is not None:
        t.notes = body.notes or None
    if body.sentForApproval:
        t.sent_for_approval = True
        t.approver_user_id = approver_id
        t.approver_name = approver_name
    if new_amount != old_amount:
        t.amount = new_amount
        # Old → new amount, actor and timestamp; append-only, so no prior record is overwritten.
        await _log(db, t, "AMOUNT_UPDATED", user, old_amount=old_amount, new_amount=new_amount,
                   note=(body.notes or None))
        # The amount changed, so any approval already given no longer applies: restart the workflow
        # at this type's gate, exactly as if the transaction had just been created.
        restarted = _restart_approval(t)
        if restarted:
            await _log(db, t, "APPROVAL_RESTARTED", user, old_amount=old_amount, new_amount=new_amount,
                       note=f"Amount changed — approval restarted at {restarted.replace('_', ' ').title()}")
    if body.sentForApproval:
        await _log(db, t, "SENT_FOR_APPROVAL", user, approver_name=approver_name,
                   note=f"Sent to {approver_name or 'an approver'} for approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── Deposit Distribution (DEO — Cash Deposit split among multiple members) ───────
class _DistMember(BaseModel):
    membershipId: str
    membershipName: str | None = None
    amount: float


class AgentDistribute(BaseModel):
    members: list[_DistMember]


async def _prior_member_name(db: AsyncSession, business: str, membership_id: str) -> str | None:
    """Latest known name for a membership from the isolated agent ledger (same auto-fill rule the
    create forms use). Agent memberships have no master record, so there is no status to validate."""
    return (await db.execute(
        select(AgentTransaction.membership_name).where(
            AgentTransaction.merchant_business == business,
            AgentTransaction.membership_id == membership_id,
            AgentTransaction.membership_name.is_not(None),
        ).order_by(AgentTransaction.id.desc())
    )).scalars().first()


@router.post("/{txn_id}/distribute")
async def distribute_deposit(txn_id: int, body: AgentDistribute, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_agent_operator)):
    """Split ONE initialised Cash Deposit among several members. The original becomes a non-crediting
    CONTAINER (DISTRIBUTED); each member gets an auto-completed child deposit (DEPOSITED) that credits
    only that member. Everything stays in the isolated agent ledger.

    COMMISSION IS NOT CHARGED AGAIN HERE. The agent's Pay-In commission was already deducted on the
    deposit itself, so the split only allocates what remained: Σ children must equal the parent's
    DISTRIBUTABLE amount (gross − the parent's commission), and each child credits its member the
    full amount allotted to it. Previously the children were charged the pay-in fee a second time,
    so the same commission came off twice.
    """
    _require(user, MANAGE_ROLES, "distribute Agent Cash Deposits")
    business = _business(user)
    t = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.id == txn_id, AgentTransaction.merchant_business == business))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Agent transaction not found.")
    # CASH DEPOSIT only, and only in the post-token / pre-finalised window (enforced here, not just
    # hidden in the UI).
    if t.txn_type != "DEPOSIT" or str(t.txn_method or "").upper() != MANAGEABLE_METHOD:
        raise HTTPException(status_code=400, detail="Only Cash Deposit transactions can be distributed.")
    if t.status not in DISTRIBUTABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="This deposit can be distributed only after its token has been initialised and before it is finalised.",
        )

    members = body.members or []
    if not members:
        raise HTTPException(status_code=400, detail="Add at least one member to distribute to.")
    cleaned: list[tuple[str, str | None, float]] = []
    for m in members:
        mid = (m.membershipId or "").strip().upper()
        if not mid:
            raise HTTPException(status_code=400, detail="Every distribution row needs a Member ID.")
        amt = round(m.amount or 0.0, 2)
        if amt <= 0:
            raise HTTPException(status_code=400, detail=f"Enter a valid deposit amount for member {mid}.")
        cleaned.append((mid, (m.membershipName or "").strip() or None, amt))

    agent = await _get_agent(db, business, t.agent_master_id)
    # The commission was taken on the deposit, so only the REMAINING balance is allocated. The
    # children carry no further commission (see _txn_rate), which is why Σ children is checked
    # against the distributable amount rather than the gross.
    original = round(t.amount or 0.0, 2)
    commission = round(original * _leg_rate(agent, "DEPOSIT"), 2)
    distributable = round(original - commission, 2)
    total = round(sum(a for _, _, a in cleaned), 2)
    if total - distributable > 0.01:
        raise HTTPException(
            status_code=400,
            detail=(f"Total distributed amount cannot exceed the distributable amount "
                    f"(₹{distributable:,.2f} = ₹{original:,.2f} less ₹{commission:,.2f} commission already deducted)."),
        )
    if abs(total - distributable) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=(f"Total distributed (₹{total:,.2f}) must equal the distributable amount "
                    f"(₹{distributable:,.2f} = ₹{original:,.2f} less ₹{commission:,.2f} commission already deducted)."),
        )

    now = datetime.utcnow()
    children: list[AgentTransaction] = []
    for i, (mid, name, amt) in enumerate(cleaned, start=1):
        if not name:
            name = await _prior_member_name(db, business, mid)
        # Member's Available Balance BEFORE this credit (completed-only; earlier children in this same
        # run are already flushed, so a member appearing twice sees the running balance).
        before = round((await _member_balance(db, business, mid)).get("available", 0.0), 2)
        child = AgentTransaction(
            reference_number=f"{t.reference_number}-{i:02d}",
            transaction_code=f"{t.transaction_code}-{i:02d}",
            txn_type="DEPOSIT", merchant_business=business,
            agent_master_id=agent.id, agent_code=agent.agent_id, agent_name=agent.full_name,
            agent_country=agent.country, agent_state=agent.state, agent_location=agent.location,
            agent_category=agent.category,
            membership_id=mid, membership_name=name, membership_type=t.membership_type,
            amount=amt, txn_method="CASH", token_details=t.token_details,
            instructions=t.instructions, notes=f"Distributed from {t.reference_number}",
            # Auto-completed on save: the parent's token was already initialised, so each child is
            # created DEPOSITED and credits its member immediately (no re-approval chain).
            status=ST_DEPOSITED, completed_at=now,
            linked_deposit_id=t.id,          # child → parent link for reporting / reconciliation
            created_by=user.username, created_by_id=user.id,
            approved_by=user.username, approved_by_id=user.id, approved_at=now,
        )
        db.add(child)
        await db.flush()                     # assign child.id for the audit FK + running balance
        await _log(db, child, "CREATED", user, new_amount=amt,
                   note=f"Child deposit of {t.reference_number} — Membership {mid}")
        await _log(db, child, "DEPOSITED", user, new_amount=amt,
                   note=_completion_note(child, agent, user, before))
        children.append(child)

    # Turn the original into a non-crediting container. It is now FINAL (immutable) and excluded from
    # every money figure, while its children carry the credits.
    t.status = ST_DISTRIBUTED
    t.updated_by = user.username
    t.updated_by_id = user.id
    t.updated_at = now
    refs = ", ".join(c.reference_number for c in children)
    await _log(db, t, "DISTRIBUTED", user, old_amount=original, new_amount=total,
               note=(f"Cash deposit distributed across {len(children)} member(s): {refs}. "
                     f"Commission ₹{commission:,.2f} was already deducted on the deposit and is not "
                     f"charged again; ₹{distributable:,.2f} allocated in full. "
                     f"Parent is a non-crediting container."))
    await db.commit()
    await db.refresh(t)
    for c in children:
        await db.refresh(c)
    return {"parent": _row(t), "children": [_row(c) for c in children]}


async def _decide(db: AsyncSession, user: User, txn_id: int, approve: bool) -> dict:
    _require(user, APPROVE_ROLES, "approve or reject Agent Transactions")
    business = _business(user)
    t = await _load_pending(db, business, txn_id)
    # Withdrawal approval is a Manager-only authority, so this legacy one-step decide honours the
    # same rule as the review gate — a Supervisor cannot approve or reject a withdrawal here either.
    if (t.txn_type or "").upper() == "WITHDRAWAL" and not agent_role_in(user, APPROVER_ROLES["WITHDRAWAL"]):
        raise HTTPException(status_code=403, detail="Only a Manager can approve or reject Agent Withdrawal Requests.")
    t.status = "APPROVED" if approve else "REJECTED"
    t.approved_by = user.username
    t.approved_by_id = user.id
    t.approved_at = datetime.utcnow()
    if approve:
        t.completed_at = t.approved_at    # legacy one-step approval completes the transaction
    t.updated_by = user.username
    t.updated_by_id = user.id
    t.updated_at = datetime.utcnow()
    await _log(db, t, "APPROVED" if approve else "REJECTED", user,
               note=f"{'Approved' if approve else 'Rejected'} by {user.username}")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/approve")
async def approve_txn(txn_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_agent_operator)):
    return await _decide(db, user, txn_id, True)


@router.post("/{txn_id}/reject")
async def reject_txn(txn_id: int, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_agent_operator)):
    return await _decide(db, user, txn_id, False)


# ── Overview (isolated KPIs — summarizes ONLY agent_transactions) ────────────────
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """KPIs / summaries / trend for the Agent Overview — computed exclusively from the isolated
    agent_transaction table (never merchant transactions). Commission uses the agent fee for that
    leg: deposit -> Pay-In, withdrawal -> Pay-Out, settlement -> Settlement."""
    business = _business(user)
    txns = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business).order_by(AgentTransaction.id.desc()))).scalars().all()
    # A distributed cash deposit is a reconciliation CONTAINER, not a real member transaction — its
    # child deposits already represent the full amount. Exclude containers from every KPI/count/trend
    # so they neither inflate the deposit count nor double-count the amount. They stay visible in All
    # Transactions and Reports for parent→child traceability.
    txns = [t for t in txns if t.status != ST_DISTRIBUTED]
    _agents = (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business))).scalars().all()
    # One fee table per leg — an agent can charge a different rate on each.
    fees_in = {a.id: (a.pay_in_fee or 0.0) for a in _agents}
    fees_out = {a.id: (a.pay_out_fee or 0.0) for a in _agents}
    fees_set = {a.id: (a.settlement_fee or 0.0) for a in _agents}

    def _sum(pred):
        return round(sum(t.amount for t in txns if pred(t)), 2)

    dep = [t for t in txns if t.txn_type == "DEPOSIT"]
    wd = [t for t in txns if t.txn_type == "WITHDRAWAL"]
    st = [t for t in txns if t.txn_type == "SETTLEMENT"]
    # Completed-only basis: DEPOSITED (deposit chain) / COMPLETED (withdrawal+settlement chains) /
    # APPROVED (legacy rows predating the chain). Keeps every financial figure correct as
    # transactions move onto the merchant-mirroring workflow.
    approved = [t for t in txns if t.status in COMPLETED_STATUSES]

    # ── Financial summary — mirrors the Merchant canonical formula (transactions.py) applied to
    # the ISOLATED agent ledger only. Each agent's commission = its per-leg fee on the approved
    # leg's amount (the same percentage for every leg). Computed on read (not stored), exactly
    # like the merchant modules, so figures always reflect the latest approvals.
    #   Gross Amount (Approved)  = Σ approved deposit amounts (before commission)
    #   <leg> Commission         = Σ (approved <leg> amount × that leg's agent fee)
    #   Total Commission         = Deposit + Withdrawal + Settlement Commission
    #   Net (Approved)           = Gross − Deposit Commission − Withdrawals − Withdrawal Commission
    #                                    − Settlements − Settlement Commission
    # Settlements move money OUT of the agent ledger, so they are deducted exactly as the merchant
    # formula deducts them (available = deposits − withdrawals − settlements − depComm − payoutFee).
    approved_dep = [t for t in approved if t.txn_type == "DEPOSIT"]
    approved_wd = [t for t in approved if t.txn_type == "WITHDRAWAL"]
    approved_st = [t for t in approved if t.txn_type == "SETTLEMENT"]

    def _commission(rows, table):
        # A split child charges nothing — its parent Cash Deposit already collected the commission
        # (see _txn_rate). Anything else uses the agent's own fee for that leg.
        return sum(0.0 if _is_split_child(t) else t.amount * table.get(t.agent_master_id, 0.0) / 100
                   for t in rows)

    gross_amount = sum(t.amount for t in approved_dep)
    total_withdrawal_amount = sum(t.amount for t in approved_wd)
    total_settlement_amount = sum(t.amount for t in approved_st)
    deposit_commission = _commission(approved_dep, fees_in)
    withdrawal_commission = _commission(approved_wd, fees_out)
    settlement_commission = _commission(approved_st, fees_set)
    total_commission = deposit_commission + withdrawal_commission + settlement_commission
    net_amount = (gross_amount - deposit_commission
                  - total_withdrawal_amount - withdrawal_commission
                  - total_settlement_amount - settlement_commission)

    by_agent: dict = defaultdict(lambda: {"deposits": 0.0, "withdrawals": 0.0, "settlements": 0.0, "count": 0})
    for t in txns:
        k = (t.agent_code, t.agent_name)
        by_agent[k]["count"] += 1
        # Explicit buckets: a SETTLEMENT is neither a deposit nor a withdrawal and must not be
        # folded into either.
        if t.txn_type == "DEPOSIT":
            by_agent[k]["deposits"] += t.amount
        elif t.txn_type == "WITHDRAWAL":
            by_agent[k]["withdrawals"] += t.amount
        else:
            by_agent[k]["settlements"] += t.amount

    trend: dict = defaultdict(lambda: {"deposits": 0.0, "withdrawals": 0.0})
    for t in txns:
        _, d, _t = _ist_parts(t.created_at)
        if d:
            if t.txn_type == "DEPOSIT":
                trend[d]["deposits"] += t.amount
            elif t.txn_type == "WITHDRAWAL":
                trend[d]["withdrawals"] += t.amount

    return {
        "cards": {
            "totalTransactions": len(txns),
            "depositCount": len(dep), "depositAmount": _sum(lambda t: t.txn_type == "DEPOSIT"),
            "withdrawalCount": len(wd), "withdrawalAmount": _sum(lambda t: t.txn_type == "WITHDRAWAL"),
            "settlementCount": len(st), "settlementAmount": _sum(lambda t: t.txn_type == "SETTLEMENT"),
            # Pending = still in flight anywhere in the chain (PENDING, ACCOUNT_REQUESTED,
            # ACCOUNT_SUBMITTED, SUPERVISOR_REVIEW, SLIP_SUBMITTED …) — i.e. not yet final.
            "pending": sum(1 for t in txns if t.status not in FINAL_STATUSES),
            # "approved" is the Completed count (COMPLETED_STATUSES); alias kept for existing readers.
            "approved": len(approved), "completed": len(approved),
            "rejected": sum(1 for t in txns if t.status in REJECTED_STATUSES),
            # Today's transactions (IST) — created today, any status.
            "today": sum(1 for t in txns if t.created_at
                         and t.created_at.replace(tzinfo=timezone.utc).astimezone(IST).date() == datetime.now(IST).date()),
            "approvedDeposits": round(gross_amount, 2),
            "approvedWithdrawals": round(total_withdrawal_amount, 2),
            "approvedSettlements": round(total_settlement_amount, 2),
            "grossAmount": round(gross_amount, 2),
            "depositCommission": round(deposit_commission, 2),
            "withdrawalCommission": round(withdrawal_commission, 2),
            "settlementCommission": round(settlement_commission, 2),
            "netAmount": round(net_amount, 2),
            "totalCommission": round(total_commission, 2),
        },
        "byAgent": [{"agentCode": k[0], "agentName": k[1], "deposits": round(v["deposits"], 2),
                     "withdrawals": round(v["withdrawals"], 2), "settlements": round(v["settlements"], 2),
                     "count": v["count"]}
                    for k, v in sorted(by_agent.items(),
                                       key=lambda kv: -(kv[1]["deposits"] + kv[1]["withdrawals"] + kv[1]["settlements"]))][:10],
        "trend": [{"date": d, "deposits": round(v["deposits"], 2), "withdrawals": round(v["withdrawals"], 2)}
                  for d, v in sorted(trend.items())][-14:],
        "recent": [_row(t) for t in txns[:10]],
    }
