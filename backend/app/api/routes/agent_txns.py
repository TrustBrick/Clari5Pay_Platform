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
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
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
ST_SUPERVISOR_REVIEW = "SUPERVISOR_REVIEW"
ST_MANAGER_REVIEW = "MANAGER_REVIEW"
ST_SLIP_SUBMITTED = "SLIP_SUBMITTED"
ST_DEPOSITED = "DEPOSITED"
ST_COMPLETED = "COMPLETED"
ST_REJECTED = "REJECTED"
ST_PENDING = "PENDING"
ST_APPROVED = "APPROVED"

# Roles that operate the chain. The Data Operator (DEO) drives every operator step.
OPERATOR_ROLES = ("DEO", "DEPOSIT_OPERATOR", "WITHDRAWAL_OPERATOR")
DEPOSIT_OPERATOR_ROLES = ("DEO", "DEPOSIT_OPERATOR")

# Which statuses count as money that actually moved — the completed-only basis for every financial
# figure, mirroring the merchant rule ("a completed deposit is COMPLETED (legacy) or DEPOSITED").
# APPROVED covers agent rows created before the chain existed; DEPOSITED ends the deposit chain;
# COMPLETED ends the withdrawal/settlement chains.
COMPLETED_STATUSES = {ST_APPROVED, ST_DEPOSITED, ST_COMPLETED}
REJECTED_STATUSES = {ST_REJECTED}
FINAL_STATUSES = COMPLETED_STATUSES | REJECTED_STATUSES

# How the money moves. CASH is the only method Manage Transaction may edit.
TXN_METHODS = {"CASH", "UPI", "BANK", "IMPS", "NEFT", "RTGS", "CRYPTO"}
BANK_LIKE_METHODS = {"BANK", "IMPS", "NEFT", "RTGS"}   # collect a sending bank account
# Settlement is Supervisor-only and needs NO approval: it mirrors the withdrawal chain minus the
# review gate, so it is created ready to pay. Methods are limited to Cash / Bank Transfer / Crypto.
SETTLEMENT_METHODS = {"CASH", "BANK", "CRYPTO"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _business(user: User) -> str:
    return user.name


def _require(user: User, roles: tuple[str, ...], what: str) -> None:
    if not agent_role_in(user, roles):
        raise HTTPException(status_code=403, detail=f"Your role cannot {what}.")


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


async def _resolve_approver(db: AsyncSession, business: str, approver_user_id: int | None):
    if approver_user_id is None:
        return None, None
    u = (await db.execute(select(User).where(User.id == approver_user_id))).scalar_one_or_none()
    ok = u and u.role == UserRole.MERCHANT and u.name == business and str(u.merchant_role or "").upper() in ("SUPERVISOR", "MANAGER")
    if not ok:
        raise HTTPException(status_code=400, detail="Authorized approver must be a Supervisor or Manager of your business.")
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
    txnMethod: str | None = None            # CASH | UPI | BANK | IMPS | NEFT | RTGS | CRYPTO
    senderUpiId: str | None = None          # UPI the payment is sent from
    senderAccountHolder: str | None = None
    senderAccountNumber: str | None = None
    senderIfsc: str | None = None
    senderBankName: str | None = None
    senderBranch: str | None = None


class AgentAccountSubmit(BaseModel):
    """The Data Operator submits an AGENT account for the payer to send to."""
    agentAccountId: int


class AgentSlipSubmit(BaseModel):
    """Payment evidence. Both are mandatory: the UTR is the payment reference (there is no
    separate Reference Number) and the slip image is the proof."""
    slipImage: str | None = None            # data URL — required
    utr: str | None = None                  # required; the transaction's only payment reference


class AgentReviewAction(BaseModel):
    remark: str


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
    if body.membershipType.upper() not in MEMBERSHIP_TYPES:
        raise HTTPException(status_code=400, detail="Membership Type must be Online or Offline.")
    if not body.membershipId.strip():
        raise HTTPException(status_code=400, detail="Membership ID is required.")
    if body.notes and len(body.notes) > 100:
        raise HTTPException(status_code=400, detail="Notes must be 100 characters or fewer.")
    if body.instructions and body.instructions.upper() not in INSTRUCTIONS:
        raise HTTPException(status_code=400, detail="Invalid instruction option.")
    # Token Details / Unique Note Number come from the customer/agent, so the operator enters
    # them on every agent transaction; they are never generated.
    if not (body.tokenDetails or "").strip():
        raise HTTPException(status_code=400, detail="Token Details are required.")
    if not (body.noteNumber or "").strip():
        raise HTTPException(status_code=400, detail="Unique Note Number is required.")
    if body.txnMethod and body.txnMethod.upper() not in TXN_METHODS:
        raise HTTPException(status_code=400, detail="Invalid Transaction Type.")
    method = (body.txnMethod or "").upper()
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
    agent = await _get_agent(db, business, body.agentMasterId)
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId)

    ref = await _next_serial(db, AgentTransaction.reference_number, prefix)
    # The operator-entered note number must stay unique, so a clash is reported plainly instead
    # of surfacing as a database integrity error.
    note_no = (body.noteNumber or "").strip()
    entered_token = (body.tokenDetails or "").strip()
    clash = (await db.execute(select(AgentTransaction.id).where(
        AgentTransaction.note_number == note_no))).scalars().first()
    if clash:
        raise HTTPException(status_code=400, detail="This Unique Note Number is already used.")
    txn_code = f"{agent.transaction_code}-{code_letter}-{ref[len(prefix):]}"

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

    t = AgentTransaction(
        reference_number=ref, transaction_code=txn_code, txn_type=txn_type,
        merchant_business=business,
        agent_master_id=agent.id, agent_code=agent.agent_id, agent_name=agent.full_name,
        agent_country=agent.country, agent_state=agent.state, agent_location=agent.location,
        agent_category=agent.category,
        membership_id=body.membershipId.strip(), membership_name=membership_name,
        membership_type=body.membershipType.upper(),
        amount=round(body.amount, 2),
        txn_country=body.country, txn_state=body.state, txn_location=body.location,
        mobile=body.mobile, mobile_code=(body.mobileCode or None),
        token_details=entered_token, note_number=note_no,
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
        status=(ST_ACCOUNT_REQUESTED if txn_type == "DEPOSIT"
                else ST_ACCOUNT_SUBMITTED if txn_type == "WITHDRAWAL"
                else ST_SLIP_SUBMITTED if txn_type == "SETTLEMENT" else ST_PENDING),
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
        } for a in agents],
        "approvers": [{"id": u.id, "name": u.username, "role": str(u.merchant_role or "").upper()}
                      for u in approvers if str(u.merchant_role or "").upper() in ("SUPERVISOR", "MANAGER")],
        "instructions": sorted(INSTRUCTIONS),
        "membershipTypes": ["ONLINE", "OFFLINE"],
        # Transaction types offered on the request form (drives the Sending Account fields and
        # gates Manage Transaction, which is CASH-only).
        "txnMethods": ["CASH", "UPI", "BANK", "IMPS", "NEFT", "RTGS", "CRYPTO"],
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
    return {
        "membershipId": membership_id.strip(), "membershipName": membership_name,
        "latestDeposit": latest_deposit,
        # Payout accounts already on file for this membership (isolated agent register).
        "savedAccounts": [_member_account_row(a) for a in saved],
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
    """Create an Agent Settlement. Supervisor-only and approval-free: the Supervisor performs
    every step, so it is created at SLIP_SUBMITTED, ready for them to pay and complete."""
    _require(user, ("SUPERVISOR",), "create Agent Settlement Requests")
    membership_id = body.membershipId.strip()
    payout = await _resolve_payout_account(db, user, body, membership_id, (body.membershipName or None))
    return await _create(db, user, body, "SETTLEMENT", "AGS", "S", None, payout=payout)


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
    _require_status(t, ST_ACCOUNT_REQUESTED, "an account submission")

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
    t.account_submitted_by = user.username
    t.account_submitted_at = datetime.utcnow()
    t.status = ST_ACCOUNT_SUBMITTED
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "ACCOUNT_SUBMITTED", user, note=f"Agent account {acct.account_ref} submitted")
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
    _require_status(t, ST_ACCOUNT_SUBMITTED, "a slip")
    # Both the proof and its reference are mandatory — captured once here and reused unchanged
    # for the rest of the workflow (Approvals, Mark Deposit, Details, Reports).
    if not body.slipImage:
        raise HTTPException(status_code=400, detail="Payment slip image is required.")
    if not (body.utr or "").strip():
        raise HTTPException(status_code=400, detail="UTR Number is required.")

    t.slip_image = body.slipImage
    t.deposit_utr = body.utr.strip()          # the only payment reference; Mark Deposit displays it
    t.slip_submitted_by = user.username
    t.slip_submitted_at = datetime.utcnow()
    t.status = ST_SUPERVISOR_REVIEW          # straight to the Supervisor queue (as the merchant flow does)
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SLIP_SUBMITTED", user, note="Slip submitted — awaiting Supervisor approval")
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/supervisor/approve")
async def supervisor_approve(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_agent_operator)):
    """Supervisor approves a deposit under review → SLIP_SUBMITTED (awaiting Mark Deposit)."""
    _require(user, ("SUPERVISOR",), "approve Agent Deposits")
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_deposit(t)
    _require_status(t, ST_SUPERVISOR_REVIEW, "Supervisor review")
    t.status = ST_SLIP_SUBMITTED
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
    """Supervisor rejects a deposit under review → REJECTED."""
    _require(user, ("SUPERVISOR",), "reject Agent Deposits")
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_deposit(t)
    _require_status(t, ST_SUPERVISOR_REVIEW, "Supervisor review")
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
    t = await _load_own(db, _business(user), txn_id)
    _require_deposit(t)
    _require_status(t, ST_SLIP_SUBMITTED, "Mark Deposit")
    t.status = ST_DEPOSITED
    t.deposited_by = user.username
    t.deposited_at = datetime.utcnow()
    # deposit_utr / slip_image are left exactly as captured at the slip step — no duplicate
    # upload, no overwrite of the original.
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "DEPOSITED", user, new_amount=t.amount, note=f"Marked deposited by {user.username}")
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


@router.post("/{txn_id}/manager/approve")
async def manager_approve(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_agent_operator)):
    """Manager approves a paid withdrawal after reviewing the slip → COMPLETED (final gate)."""
    _require(user, ("MANAGER",), "approve Agent Withdrawals")
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_withdrawal(t)
    _require_status(t, ST_MANAGER_REVIEW, "Manager review")
    t.status = ST_COMPLETED
    t.manager_name = user.username
    t.manager_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "MANAGER_APPROVED", user, new_amount=t.amount, note=remark)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/manager/reject")
async def manager_reject(txn_id: int, body: AgentReviewAction, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_agent_operator)):
    """Manager rejects a withdrawal → REJECTED."""
    _require(user, ("MANAGER",), "reject Agent Withdrawals")
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="Remarks are required for every review action.")
    t = await _load_own(db, _business(user), txn_id)
    _require_withdrawal(t)
    _require_status(t, ST_MANAGER_REVIEW, "Manager review")
    t.status = ST_REJECTED
    t.manager_name = user.username
    t.manager_action_at = datetime.utcnow()
    t.review_remark = remark
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "MANAGER_REJECTED", user, note=remark)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.post("/{txn_id}/payout")
async def payout_withdrawal(txn_id: int, body: AgentSlipSubmit, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_agent_operator)):
    """Data Operator pays the member and uploads the slip → COMPLETED.

    The merchant workflow's Admin payout step, performed here by the Data Operator.
    """
    t = await _load_own(db, _business(user), txn_id)
    # Withdrawals are paid by the operator; settlements by the Supervisor who raised them.
    if t.txn_type == "SETTLEMENT":
        _require(user, ("SUPERVISOR",), "complete Agent Settlements")
    elif t.txn_type == "WITHDRAWAL":
        _require(user, ("DEO", "WITHDRAWAL_OPERATOR"), "pay Agent Withdrawals")
    else:
        raise HTTPException(status_code=400, detail="This action applies to Agent Withdrawals and Settlements only.")
    # A withdrawal is paid before its single Manager gate; a settlement has no gate at all.
    _require_status(t, ST_ACCOUNT_SUBMITTED if t.txn_type == "WITHDRAWAL" else ST_SLIP_SUBMITTED, "payment")
    if not body.slipImage:
        raise HTTPException(status_code=400, detail="Payment slip image is required.")
    if not (body.utr or "").strip():
        raise HTTPException(status_code=400, detail="UTR Number is required.")
    t.slip_image = body.slipImage
    t.slip_submitted_by = user.username
    t.slip_submitted_at = datetime.utcnow()
    if body.utr is not None and (body.utr or '').strip():
        t.deposit_utr = body.utr.strip()          # UTR of the payment, shown wherever the txn is viewed
    # A paid withdrawal goes to the Manager to review the slip; a settlement is done.
    t.status = ST_MANAGER_REVIEW if t.txn_type == 'WITHDRAWAL' else ST_COMPLETED
    t.updated_by, t.updated_by_id, t.updated_at = user.username, user.id, datetime.utcnow()
    await _log(db, t, "SLIP_SUBMITTED" if t.txn_type == "WITHDRAWAL" else "COMPLETED", user,
               new_amount=t.amount,
               note=(f"Paid by {user.username} — awaiting Manager approval" if t.txn_type == "WITHDRAWAL"
                     else f"Settlement paid by {user.username}"))
    await db.commit()
    await db.refresh(t)
    return _row(t)


# ── List / search (Manage Transaction worklist) ──────────────────────────────────
@router.get("")
async def list_txns(status: str | None = None, txn_type: str | None = None, search: str | None = None,
                    date: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_agent_operator)):
    """Business-scoped list of agent transactions with optional status/type/search/date filters.
    Manage Transaction calls this with status=PENDING. Search matches reference / membership / agent."""
    business = _business(user)
    stmt = select(AgentTransaction).where(AgentTransaction.merchant_business == business)
    if status:
        stmt = stmt.where(AgentTransaction.status == status.upper())
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
    return [_row(r) for r in rows]


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

# The in-flight order of each chain, and where its approval gate sits. An amount change sends the
# transaction BACK to its gate — never forward — so a deposit still awaiting account submission or
# a slip is not jumped past those steps into an approval it has not earned.
_CHAIN_ORDER = {
    "DEPOSIT": [ST_ACCOUNT_REQUESTED, ST_ACCOUNT_SUBMITTED, ST_SUPERVISOR_REVIEW, ST_SLIP_SUBMITTED],
    "WITHDRAWAL": [ST_ACCOUNT_SUBMITTED, ST_MANAGER_REVIEW],
    "SETTLEMENT": [ST_SLIP_SUBMITTED],
}
# Deposit → Supervisor Approval · Withdrawal → Manager Approval · Settlement → Supervisor Completion.
_APPROVAL_GATE = {
    "DEPOSIT": ST_SUPERVISOR_REVIEW,
    "WITHDRAWAL": ST_MANAGER_REVIEW,
    "SETTLEMENT": ST_SLIP_SUBMITTED,
}


def _restart_approval(t: AgentTransaction) -> str | None:
    """Send a transaction back to its approval gate after its amount changed, as if newly created.

    Returns the new status when the transaction had already reached (or passed) its gate, else
    None — a deposit still awaiting account submission or a slip keeps its place, because no
    approval has happened yet and there is nothing to redo. Any prior decision is voided so the
    gate must be passed again from scratch; the audit trail of it is append-only and untouched.
    """
    order = _CHAIN_ORDER.get(t.txn_type or "", [])
    gate = _APPROVAL_GATE.get(t.txn_type or "")
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
    approver_id, approver_name = await _resolve_approver(db, business, body.approverUserId)

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


async def _decide(db: AsyncSession, user: User, txn_id: int, approve: bool) -> dict:
    _require(user, APPROVE_ROLES, "approve or reject Agent Transactions")
    business = _business(user)
    t = await _load_pending(db, business, txn_id)
    t.status = "APPROVED" if approve else "REJECTED"
    t.approved_by = user.username
    t.approved_by_id = user.id
    t.approved_at = datetime.utcnow()
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
    agent_transaction table (never merchant transactions). Commission uses each agent's fees_pct."""
    business = _business(user)
    txns = (await db.execute(select(AgentTransaction).where(
        AgentTransaction.merchant_business == business).order_by(AgentTransaction.id.desc()))).scalars().all()
    fees = {a.id: (a.fees_pct or 0.0) for a in (await db.execute(
        select(AgentMaster).where(AgentMaster.merchant_business == business))).scalars().all()}

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
    # the ISOLATED agent ledger only. Each agent's commission = its fees_pct on the approved
    # leg's amount (the same percentage for every leg). Computed on read (not stored), exactly
    # like the merchant modules, so figures always reflect the latest approvals.
    #   Gross Amount (Approved)  = Σ approved deposit amounts (before commission)
    #   <leg> Commission         = Σ (approved <leg> amount × fees_pct)
    #   Total Commission         = Deposit + Withdrawal + Settlement Commission
    #   Net (Approved)           = Gross − Deposit Commission − Withdrawals − Withdrawal Commission
    #                                    − Settlements − Settlement Commission
    # Settlements move money OUT of the agent ledger, so they are deducted exactly as the merchant
    # formula deducts them (available = deposits − withdrawals − settlements − depComm − payoutFee).
    approved_dep = [t for t in approved if t.txn_type == "DEPOSIT"]
    approved_wd = [t for t in approved if t.txn_type == "WITHDRAWAL"]
    approved_st = [t for t in approved if t.txn_type == "SETTLEMENT"]

    def _commission(rows):
        return sum(t.amount * fees.get(t.agent_master_id, 0.0) / 100 for t in rows)

    gross_amount = sum(t.amount for t in approved_dep)
    total_withdrawal_amount = sum(t.amount for t in approved_wd)
    total_settlement_amount = sum(t.amount for t in approved_st)
    deposit_commission = _commission(approved_dep)
    withdrawal_commission = _commission(approved_wd)
    settlement_commission = _commission(approved_st)
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
            "approved": len(approved),
            "rejected": sum(1 for t in txns if t.status in REJECTED_STATUSES),
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
