from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
    String, Integer, Boolean, Float, DateTime, Date,
    ForeignKey, Enum as SAEnum, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, column_property
import enum
from app.db.session import Base


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    MERCHANT = "MERCHANT"
    SUPPORT_AGENT = "SUPPORT_AGENT"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TxType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    SETTLEMENT = "SETTLEMENT"
    DEPOSIT_REQUEST = "DEPOSIT_REQUEST"
    WITHDRAWAL_REQUEST = "WITHDRAWAL_REQUEST"
    SETTLEMENT_REQUEST = "SETTLEMENT_REQUEST"


class TxStatus(str, enum.Enum):
    PENDING = "PENDING"
    ADMIN_APPROVED = "ADMIN_APPROVED"
    COMPLETED = "COMPLETED"
    SUCCESSFUL = "SUCCESSFUL"
    REJECTED = "REJECTED"
    SA_REJECTED = "SA_REJECTED"
    CANCELLED = "CANCELLED"
    # New workflow statuses
    ACCOUNT_REQUESTED = "ACCOUNT_REQUESTED"
    ACCOUNT_SUBMITTED = "ACCOUNT_SUBMITTED"
    SLIP_SUBMITTED = "SLIP_SUBMITTED"
    # Supervisor (deposit) / Manager (withdrawal) review-gate workflow.
    PENDING_APPROVAL = "PENDING_APPROVAL"      # slip/request submitted, awaiting reviewer pickup
    SUPERVISOR_REVIEW = "SUPERVISOR_REVIEW"    # deposit assigned to a Supervisor
    MANAGER_REVIEW = "MANAGER_REVIEW"          # withdrawal assigned to a Manager
    RESUBMITTED = "RESUBMITTED"                # reviewer sent it back to the Data Operator
    DEPOSITED = "DEPOSITED"                     # admin final-approved a deposit


class AccountType(str, enum.Enum):
    SAVINGS = "Savings Account"
    CURRENT = "Current Account"


class SupportSender(str, enum.Enum):
    MERCHANT = "MERCHANT"
    SUPPORT = "SUPPORT"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), nullable=False)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    # Profile picture (data URL) — uploaded by the user, shown in the header & profile.
    avatar: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Brute-force protection: failed login attempts and lockout expiry.
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    # Full creation timestamp (date + time) — shown in the SA "merchants by admin" popup
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)

    # Which admin created this merchant (null for admins / super admin)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    # Merchant access role (DEO / DEPOSIT_OPERATOR / WITHDRAWAL_OPERATOR / SUPERVISOR / MANAGER).
    merchant_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Unique serial Merchant ID (bank-account style, e.g. MID000001) assigned at creation.
    merchant_code: Mapped[Optional[str]] = mapped_column(String(16), unique=True, index=True, nullable=True)

    # Merchant-specific
    pay_in: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    pay_out: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    settlement: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    pay_in_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_out_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    settlement_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Merchant company country (business-level; owner user holds it for the business).
    country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Personal name for a merchant user, distinct from the business name (`name`).
    full_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    balance: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    risk: Mapped[Optional[RiskLevel]] = mapped_column(SAEnum(RiskLevel), nullable=True)
    profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Per-user preference: also deliver notifications to WhatsApp (internal users only). Default on.
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Telegram chat id (set once the user starts the notification bot) — enables Telegram delivery.
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Session-token generation. Every issued access token carries this value as a `ver` claim;
    # authentication rejects a token whose `ver` no longer matches. Incrementing it therefore
    # invalidates EVERY token previously issued to this user — the platform's only way to revoke
    # a JWT, which is otherwise valid until it expires (Admin/Super Admin: 10 years).
    # See SECURITY_REVIEW.md SEC-002 and AUTH_SESSION_ARCHITECTURE.md.
    # A token minted before this column existed has no `ver` claim and is read as 0, so the
    # default of 0 keeps every pre-existing session working.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")

    # ── Support member fields (a SUPPORT_AGENT enriched via the Support Management module) ──
    # Unique auto Support ID (e.g. SUP000001). Only members onboarded through the module have one.
    support_code: Mapped[Optional[str]] = mapped_column(String(16), index=True, nullable=True)
    support_department: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    support_shift: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    # Manual availability while logged in: "AVAILABLE" | "BUSY" (Offline is derived from presence).
    support_availability: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    support_availability_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Soft-delete flag (Super Admin "Delete"): hidden from lists but preserved for audit/history.
    support_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="merchant_user", foreign_keys="Transaction.merchant_id"
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    type: Mapped[TxType] = mapped_column(SAEnum(TxType), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[TxStatus] = mapped_column(SAEnum(TxStatus), default=TxStatus.ACCOUNT_REQUESTED, nullable=False)
    merchant_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Permanent snapshot of the creating merchant user — kept on the row so historical
    # records stay accurate even if the user's profile/role changes later.
    creator_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    creator_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    tx_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    tx_time: Mapped[str] = mapped_column(String(16), nullable=False)

    # Deposit-specific
    deposit_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    member_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    member_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    segment: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    # For UPI deposits: the merchant's own UPI the payment is sent FROM.
    sender_upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Withdrawal-specific
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # UTR / notes / risk
    utr: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)            # bank UTR number
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                # merchant free-text note to admin
    risk_analysis: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # whether risk analysis was requested
    high_risk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # agent-flagged high risk (payment not received)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # admin rejection reason

    # Cancellation (merchant cancels their own pending request — reason is mandatory).
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # merchant cancellation reason
    cancelled_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # name of the user who cancelled
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # when it was cancelled

    # Proof / verification workflow
    # These base64 image data-URLs are large and are ONLY needed on the single-row detail fetch
    # (_serialize full=True). deferred=True keeps them OUT of every bulk query (lists, dashboards,
    # balances, reports, risk aggregates) so those SELECTs don't drag megabytes of base64 across
    # the wire — the root cause of the DB `Client:ClientWrite` saturation. They load lazily on the
    # detail row when accessed within the request's session.
    merchant_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True, deferred=True)  # first merchant slip image (data URL) — kept for back-compat
    merchant_proofs: Mapped[Optional[str]] = mapped_column(Text, nullable=True, deferred=True)  # JSON array of up to 3 proof/slip files (data URLs)
    merchant_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # merchant payment reference number
    admin_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True, deferred=True)     # admin-uploaded bank-details image (data URL)
    admin_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True) # admin reference number
    admin_bank_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # admin manually-entered bank details
    # Another large base64 image — deferred so bulk/list/report/balance SELECTs never drag it
    # (loaded explicitly on the detail view, like the proof columns above).
    admin_bank_image: Mapped[Optional[str]] = mapped_column(Text, nullable=True, deferred=True)  # admin custom bank-details image (data URL) — overrides the auto card
    admin_upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)   # admin UPI ID (when merchant chose UPI)
    admin_utr: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)       # agent's payment UTR (withdrawal/settlement payout)
    payout_mode: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)     # withdrawal: BANK / UPI / CASH / CRYPTO
    payout_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # withdrawal: mode-specific fields as JSON
    # Deposit: type-specific fields as JSON (CASH → village/city/mobile; CRYPTO → walletAddress/network/txHash).
    deposit_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Reporting/intelligence: who approved (sent account / approved) and who processed
    # (marked deposited / completed) the request, plus the creating operator's agent code.
    approved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    processed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Supervisor (deposit) / Manager (withdrawal) review-gate workflow tracking.
    # remarks_history is a JSON array of {role, user, action, remark, at} entries.
    remarks_history: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supervisor_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    supervisor_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manager_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    admin_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Merchant-chosen Authorized Approver ("Send To Approval" — demo only; mirrors AgentTransaction).
    # The request is routed to the same Supervisor (deposit) / Manager (withdrawal) review queue as
    # before; these columns just record WHO the operator addressed it to. NULL on Production.
    approver_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approver_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # The chosen approver's role (SUPERVISOR / MANAGER) — lets the review status DISPLAY as the
    # selected person's role (e.g. a deposit sent to a Manager reads "Manager Review"). NULL on Prod.
    approver_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # UPI/QR deposits: when the generated QR stops being valid (15 minutes after it is issued/regenerated).
    qr_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Agent Management (Phase 4): which Non-EPS agent + agent account handles this transaction.
    # All nullable; only ever written by the demo-gated agent-assignment endpoint. Untouched (NULL)
    # on Production and by the existing deposit/withdrawal/settlement create/approval logic.
    assigned_agent_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("agent_master.id"), nullable=True)
    assigned_agent_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("agent_account.id"), nullable=True)
    assigned_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)   # actor name
    assigned_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    merchant_user: Mapped["User"] = relationship("User", back_populates="transactions", foreign_keys=[merchant_id])


# Lightweight "does this row have an admin bank image?" flag for list payloads — computed as a
# cheap `admin_bank_image IS NOT NULL` in SQL (Postgres checks the null bitmap; it never detoasts
# the large base64 value), so lists get the flag without transferring the deferred blob.
Transaction.has_admin_bank_image = column_property(
    Transaction.admin_bank_image.isnot(None), deferred=False
)


class AccountMaster(Base):
    """Bank accounts managed by Admins."""
    __tablename__ = "account_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    reference_number: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    ifsc_code: Mapped[str] = mapped_column(String(16), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    branch: Mapped[str] = mapped_column(String(128), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(SAEnum(AccountType), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False)
    created_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    created_time: Mapped[str] = mapped_column(String(16), nullable=False)
    last_maintenance_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_maintenance_time: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Account high-water marks / thresholds (auto-updated by completed transactions):
    #  • highest_credit — largest single Deposit credited to this account (configurable at
    #    creation, default 0; auto-updated when a deposit is approved).
    #  • highest_debit  — largest single Debit (withdrawal/settlement) processed from this account.
    #    Configurable starting value at creation (default 0); auto-raised whenever a larger debit
    #    completes (never decreased). Replaces the former "lowest_credit".
    #  • debit_alert_threshold — the "Highest Debit" value the admin sets at creation, kept FIXED
    #    (unlike highest_debit, which drifts upward). When >0, a completed debit BELOW it raises a
    #    low-debit alert. Seeded from the same field as highest_debit's starting value.
    highest_credit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    highest_debit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    debit_alert_threshold: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class AccountTransaction(Base):
    """Links a managed bank account to a merchant transaction / member."""
    __tablename__ = "account_transaction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    reference_number: Mapped[str] = mapped_column(
        String(40), ForeignKey("account_master.reference_number"), index=True, nullable=False
    )
    member_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    transaction_reference_number: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    transaction_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    transaction_time: Mapped[str] = mapped_column(String(16), nullable=False)


class AdminUpi(Base):
    """A UPI ID managed by Admins for receiving merchant deposits — the UPI counterpart of
    AccountMaster (bank accounts). Kept separate so the agent can pick a saved UPI instead of
    re-typing it on every UPI/QR deposit."""
    __tablename__ = "admin_upis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)        # holder / nickname
    upi_id: Mapped[str] = mapped_column(String(64), nullable=False)        # the VPA, e.g. name@bank
    # The receiving Account this UPI belongs to — deposits via this UPI credit that account.
    account_ref: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False)
    created_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    created_time: Mapped[str] = mapped_column(String(16), nullable=False, default="")


class SystemLog(Base):
    """Audit log of key actions across the platform (viewable by the Super Admin)."""
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(Base):
    """Detailed audit trail (action, actor, old/new value, reason, IP)."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Business name of the actor (merchant users), stored separately so the actor's login username
    # can live in `username` while still scoping the Agent Management audit trail by business.
    business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # city/region/country resolved from the IP
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AppSetting(Base):
    """Simple key/value runtime settings (e.g. whether login OTP is enabled)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class LoginOtp(Base):
    """A one-time code emailed to the user — used for both login and password reset."""
    __tablename__ = "login_otps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    otp: Mapped[str] = mapped_column(String(6), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), default="login", nullable=False)  # "login" | "reset"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # successfully used
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # used or invalidated (single-use)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)        # wrong-code guesses (locks at MAX_OTP_ATTEMPTS)


class PasswordHistory(Base):
    """Previous password hashes for a user, to prevent reuse of the last N passwords."""
    __tablename__ = "password_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class MerchantBankAccount(Base):
    """A merchant's saved bank account, reusable across deposit/withdrawal requests."""
    __tablename__ = "merchant_bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    # Saved bank accounts are scoped to a Member ID — each member has its own set.
    member_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    # Bank fields are optional so a member can have a saved UPI without a full bank account.
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # saved UPI for this member
    # The default saved UPI for a member (the first one saved; merchant can change it).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Notification(Base):
    """A per-user notification capturing an action in the system."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(String(8), default="🔔", nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class WhatsAppLog(Base):
    """Delivery log for the WhatsApp notification integration — one row per attempt."""
    __tablename__ = "whatsapp_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    notification_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")  # SENT / FAILED / SKIPPED (send result)
    provider: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    provider_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Provider message id + delivery-receipt tracking (populated by the provider webhook).
    message_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    delivery_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)  # sent / delivered / read / failed
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class News(Base):
    """A news/announcement post created by an authorized editor, shown to merchants & admins."""
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    section: Mapped[str] = mapped_column(String(32), default="Announcements", nullable=False)  # one of 4 sections
    # Category (absorbed from the old Blog module). Featured + view-count power the
    # News sidebar (Featured / Most Viewed).
    category: Mapped[str] = mapped_column(String(64), default="Announcements", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # optional image (data URL)
    author_name: Mapped[str] = mapped_column(String(128), default="Admin", nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Priority (Normal / High / Critical) and an optional scheduled publish date.
    priority: Mapped[str] = mapped_column(String(16), default="Normal", nullable=False)
    publish_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SupportMessage(Base):
    """Chat messages between a merchant and customer support."""
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    sender: Mapped[SupportSender] = mapped_column(SAEnum(SupportSender), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional attachment (image/document) stored as a base64 data-URL — same pattern as
    # transaction proofs. content may be empty when a message is attachment-only.
    attachment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)   # sanitized original filename
    attachment_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)   # MIME type
    attachment_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # bytes
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SupportAssignment(Base):
    """Legacy: mapped a support member (SUPPORT_AGENT) to a merchant they were allowed to service.
    Superseded by per-conversation ownership (SupportConversation); kept for historical rows and
    no longer consulted for routing. Not written to by new code."""
    __tablename__ = "support_assignments"
    __table_args__ = (UniqueConstraint("support_id", "merchant_id", name="uq_support_merchant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    support_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    merchant_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    assigned_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class SupportConversation(Base):
    """One support conversation thread per customer (a merchant-role user). Each thread is owned by
    exactly one support agent (``support_id``); it is *queued* when ``support_id`` is NULL and no
    agent was available at open time. Status is OPEN until an agent/admin closes it. Message history
    still lives in SupportMessage keyed by the same customer id (``merchant_id``)."""
    __tablename__ = "support_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    support_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)  # OPEN | CLOSED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # when it entered the wait queue
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Response-time metric: first agent reply timestamp for the current open span.
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    assigned_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # admin id on manual (re)assignment


class SupportConfig(Base):
    """Singleton (id=1) global support-assignment configuration, editable by Admins."""
    __tablename__ = "support_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_active_conversations: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    strategy: Mapped[str] = mapped_column(String(24), default="LEAST_ACTIVE", nullable=False)  # LEAST_ACTIVE | ROUND_ROBIN
    last_assigned_support_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # round-robin pointer


class BlogPost(Base):
    """A simple company news/update post (News-style), authored by an admin / super admin.
    Category is a plain string drawn from a fixed list (no separate categories table)."""
    __tablename__ = "blog_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="Announcements", nullable=False)
    short_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cover_image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # data URL
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)  # DRAFT | PUBLISHED
    author_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    author_name: Mapped[str] = mapped_column(String(128), default="Admin", nullable=False)
    publish_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KycVerificationHistory(Base):
    """One row per KYC verification API request (Merchant Portal → KYC Update).

    Every Aadhaar generate-link and PAN verification creates a NEW row storing the complete
    request/response JSON exactly as exchanged with Melento.ai — prior records are never
    overwritten. The Aadhaar status poll (getAadhaarDetails) updates its own originating row's
    verification_status/response (the spec's "Update Verification Status → Verified/Failed").
    Access is limited to Supervisor/Manager merchant users; the list is scoped to the caller's
    merchant business pool via ``merchant_business``.
    """
    __tablename__ = "kyc_verification_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    membership_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    member_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    verification_type: Mapped[str] = mapped_column(String(16), nullable=False)  # AADHAAR | PAN | PASSPORT | OCR
    # How the verification was performed: "ID Number" | "Image Upload" | "DigiLocker".
    verification_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    reference_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    document_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # OCR doc_type (passport/pan_card/…)
    transaction_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)  # PENDING | SUCCESS | FAILED
    # Name-match confidence between the member's registered name and the official KYC name.
    # match_score (0–100) is stored for audit but never shown; match_status drives the Verification
    # History "Status" column: VERIFIED (≥85) | MANUAL_REVIEW (70–84) | NOT_VERIFIED (<70).
    match_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    match_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    request_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # full outbound request, as sent
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # full provider response, as received
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # Aadhaar DigiLocker verification URL
    # Aadhaar cardholder photo (JPEG data URL) extracted from the response's XML.
    # MUST be captured at verification time: the provider returns `xml_file` as a PRESIGNED S3 URL
    # that expires after 48h (X-Amz-Expires=172800), so it cannot be fetched later. Only ever set
    # from an XML that parsed successfully — an invalid/absent one leaves this NULL.
    aadhaar_photo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True) # provider "status" field / HTTP status
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # actor name
    # Merchant business name (scopes the history list to the caller's shared member pool).
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)


class UserSession(Base):
    """Login-session presence tracking for the Active Users feature. One row is created per
    login; the newest active row is a user's current session. Online = an active session with a
    recent last_activity heartbeat. Stores ONLY session metadata — never tokens or passwords."""
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    login_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    logout_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AgentMaster(Base):
    """A Non-EPS Agent (Agent Management module — Merchant Portal, Supervisor/Manager only).

    Agents are operational entities ONLY: they never log in, have no username/password, no
    dashboard and no portal. Managers/Supervisors contact them out-of-band (phone / WhatsApp /
    Telegram / email). This table just stores agent information; Phase 4 links agents to the
    Deposit / Withdrawal / Settlement transactions they help process.

    Agents are shared across a merchant *business* (``merchant_business`` = the owning user's
    business name), mirroring KYC history and saved bank accounts — every Supervisor/Manager of
    the same business sees the same agent pool. The ``agent_id`` (AGT000001…) is a global serial
    and never changes; duplicate name/mobile/email/transaction_code checks are scoped to the
    business pool.
    """
    __tablename__ = "agent_master"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # System-generated, globally-unique, immutable serial ID (e.g. AGT000001).
    agent_id: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)

    # ── Basic information ──
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False)

    # ── Contact information (both optional) ──
    mobile: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    # Dial code for `mobile` (e.g. "+91"). The national number stays 10 digits in `mobile`,
    # so existing rows are unaffected; the UI shows +91 when this is unset.
    mobile_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── Business information ──
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # User-set "Date of Creation" (date picker, defaults to today) — distinct from the audit
    # created_at timestamp below.
    date_of_creation: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # RETIRED — the agent module no longer reads or writes this. Kept as a column only because
    # dropping it is irreversible and old rows still carry the pre-split value that seeded the
    # three per-leg fees below. Use pay_in_fee / pay_out_fee / settlement_fee instead.
    fees_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Per-leg fees, set manually on the agent. A deposit charges pay_in_fee, a withdrawal
    # pay_out_fee, a settlement settlement_fee. Nullable so the migration can seed them from the
    # retired fees_pct; treat NULL as 0.
    pay_in_fee: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    pay_out_fee: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    settlement_fee: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    # Exactly 3 alphanumeric chars, stored uppercased; unique within the business pool. Immutable
    # after creation (embedded in transaction references from Phase 4).
    transaction_code: Mapped[str] = mapped_column(String(3), nullable=False)
    # Canonical category: CASH | BANK_TRANSFER | CRYPTO.
    category: Mapped[str] = mapped_column(String(24), nullable=False)

    # ── Additional information ──
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk_analysis: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Approval intent + status. The approval *workflow* is Phase 6; here we only record whether the
    # agent was sent for approval. NOT_REQUIRED | PENDING | APPROVED | REJECTED.
    sent_for_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(24), default="NOT_REQUIRED", nullable=False)

    # Lifecycle status: ACTIVE | INACTIVE. Inactive agents cannot be picked for new assignments.
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)

    # Scope: shared across the owning user's merchant business.
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)

    # ── Standard audit columns ──
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)   # actor name
    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentAssignmentHistory(Base):
    """Audit trail of every agent assignment / reassignment on a transaction (Phase 4).

    One row per assign or reassign action. Snapshots the agent + account (code/ref/name/type) so
    history stays accurate even if the agent or account is later edited, and records the previous
    agent/account on a reassignment. Powers the "agents/accounts with assignment history cannot be
    deleted" guards. Business-scoped like the rest of the module.
    """
    __tablename__ = "agent_assignment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transaction_ref: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(24), nullable=False)   # DEPOSIT | WITHDRAWAL | SETTLEMENT
    payment_method: Mapped[str] = mapped_column(String(16), nullable=False)      # account type: BANK | UPI | QR | CRYPTO
    action: Mapped[str] = mapped_column(String(16), nullable=False)             # ASSIGN | REASSIGN

    # New agent + account (snapshots).
    agent_master_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(16), nullable=False)            # AGT… snapshot
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_account_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    account_ref: Mapped[str] = mapped_column(String(16), nullable=False)         # AAC… snapshot
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # Previous agent/account on a reassignment (NULL on the first assignment).
    prev_agent_master_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    prev_agent_account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)

    assigned_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    assigned_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AgentMemberBankAccount(Base):
    """A payout account saved against a Membership ID in the ISOLATED agent ledger.

    Mirrors ``merchant_bank_accounts`` in shape, but is a SEPARATE table: the agent subsystem
    never reads or writes the merchant's member accounts. Scoped to the owning merchant business,
    exactly like ``AgentMaster`` / ``AgentTransaction``.

    A row is created the first time a membership is paid out to an account, and re-used on every
    later withdrawal for that membership — the (business, membership, account_number/upi) pair is
    matched before inserting, so returning members never accumulate duplicate accounts.
    """
    __tablename__ = "agent_member_bank_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    membership_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    member_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── Bank ──
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # ── UPI ──
    upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentAccount(Base):
    """A settlement account owned by a Non-EPS Agent (Agent Management → Agent Accounts).

    One Agent can hold MANY accounts across four types — Bank / UPI / QR / Crypto. Type-specific
    columns are all nullable; ``account_type`` discriminates which apply (single-table design, the
    same shape as ``transactions`` holding deposit/withdrawal-specific fields). Accounts are
    shared across the owning agent's merchant *business* (``merchant_business``), exactly like the
    Agent Master. Phase 4 links accounts to the Deposit/Withdrawal/Settlement they were used in.

    Default account: at most ONE default per (agent, account_type) — used first when an agent is
    assigned in Phase 4. Setting a new default of a type clears the previous one of that type.
    """
    __tablename__ = "agent_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # System-generated, globally-unique, immutable serial ref (e.g. AAC000001).
    account_ref: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    # Owning agent (FK to AgentMaster.id). Named *_master_id to avoid confusion with the AGT… code.
    agent_master_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_master.id"), index=True, nullable=False)

    # BANK | UPI | QR | CRYPTO — determines which type-specific fields apply. Immutable after create.
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # ── Common ──
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)   # nickname / holder label
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)  # ACTIVE | INACTIVE

    # ── Bank ──
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── UPI ──
    upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upi_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── QR ──
    qr_image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # base64 data-URL (same as proofs)
    qr_linked_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # optional linked UPI/bank note

    # ── Crypto ──
    wallet_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    crypto_network: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)   # e.g. TRC20 / ERC20 / BTC
    crypto_asset: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)      # e.g. USDT / BTC / ETH

    # Scope: shared across the owning agent's merchant business (denormalized for fast scoping).
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)

    # ── Standard audit columns ──
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentTransaction(Base):
    """An isolated Agent deposit/withdrawal request (Agent Management → new operator workflow).

    COMPLETELY SEPARATE from the merchant's own payment system: this table has NO foreign key to
    ``transactions`` and is never read/written by the merchant Deposit/Withdrawal/Settlement,
    Treasury, Risk, Account-Management or Transaction-History code paths. It records third-party
    agent transactions purely for reporting, reconciliation and operational visibility — it must
    never affect merchant balances, settlements, treasury, risk or reports.

    Scope is the owning ``merchant_business`` (like AgentMaster / AgentAccount). Agent fields are
    snapshotted at creation so historical rows stay stable even if the agent is later edited.
    """
    __tablename__ = "agent_transaction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # System-generated, unique, immutable serials. reference_number: AGD000001 (deposit) /
    # AGW000001 (withdrawal). transaction_code is <agent 3-char code>-<D|W|S>-<agent id>-<that
    # agent's own sequence>, e.g. "ABC-D-000123-04" -- see _transaction_code() in agent_txns.
    reference_number: Mapped[str] = mapped_column(String(24), unique=True, index=True, nullable=False)
    transaction_code: Mapped[str] = mapped_column(String(32), nullable=False)
    txn_type: Mapped[str] = mapped_column(String(12), nullable=False)      # DEPOSIT | WITHDRAWAL

    # Scope — owning merchant business (denormalized for fast scoping, like the other agent tables).
    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)

    # ── Agent (FK + snapshot for immutable history) ──
    agent_master_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_master.id"), index=True, nullable=False)
    agent_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # AGT…
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    agent_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    agent_location: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_category: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    # ── Membership (manual or auto-fetched; NOT linked to merchant member records) ──
    membership_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    membership_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    membership_type: Mapped[str] = mapped_column(String(12), nullable=False)   # ONLINE | OFFLINE

    # ── Transaction body ──
    amount: Mapped[float] = mapped_column(Float, nullable=False)               # only field editable via Manage
    txn_country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    txn_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    txn_location: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    mobile: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    # Dial code for `mobile` (e.g. "+91"). The national number stays 10 digits in `mobile`,
    # so existing rows are unaffected; the UI shows +91 when this is unset.
    mobile_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    # System-generated, immutable.
    # Nullable: a CASH/CRYPTO deposit has no token at creation — it is captured at Submit Account.
    token_details: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    note_number: Mapped[Optional[str]] = mapped_column(String(24), unique=True, index=True, nullable=True)
    # The Reference Number the MEMBER supplies during a withdrawal, captured on the request form
    # alongside the Unique Note Number. Distinct from `reference_number` above, which is the
    # system-generated serial — this one is the member's own reference and is not unique.
    member_reference: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)   # max 100 chars
    instructions: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    # ── Approval lifecycle (isolated — does not touch the merchant approval workflow) ──
    # Workflow status — the SAME labels the merchant deposit workflow uses (see TxStatus), so the
    # agent chain mirrors it exactly: ACCOUNT_REQUESTED → ACCOUNT_SUBMITTED → SUPERVISOR_REVIEW →
    # SLIP_SUBMITTED → DEPOSITED. Legacy rows keep PENDING|APPROVED|REJECTED. Plain VARCHAR (not a
    # PG enum) so new labels need no ALTER TYPE.
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    # How the money moves — CASH | UPI | BANK | IMPS | NEFT | RTGS | CRYPTO. Mirrors the merchant
    # Deposit Request's depositType, and gates Manage Transaction (CASH only).
    txn_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # ── Sending Account (mirrors the merchant Deposit Request's sending-account capture) ──
    sender_upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sender_account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sender_account_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sender_ifsc: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    sender_bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sender_branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── Account Submission — the AGENT ACCOUNT the Data Operator submits. Sourced ONLY from
    # agent_account (never a merchant account), preserving the subsystem's isolation.
    agent_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("agent_account.id"), nullable=True)
    agent_account_ref: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # AAC…
    agent_account_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)    # BANK|UPI|QR|CRYPTO
    agent_account_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What Submit Account captured, by method: BANK/UPI pick an Agent Account (above); CASH enters
    # token details + note + a token image; CRYPTO enters a wallet address + a payment slip.
    wallet_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # token image / crypto slip
    account_submitted_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Slip upload (the payer's proof) ──
    slip_image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)      # data URL
    # NOTE: the former `slip_ref` (Reference Number) was removed — the UTR (`deposit_utr`) is
    # the only payment reference. Any physical slip_ref column on an existing database is
    # left orphaned rather than dropped, so no historical value is destroyed.
    slip_submitted_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    slip_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Review gate (Supervisor for deposits, Manager for withdrawals — mirrors _REVIEW_CONFIG) ──
    supervisor_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    supervisor_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manager_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_remark: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Withdrawal payout — the member account the money is sent TO. Saved/re-used per membership
    # in agent_member_bank_account (never the merchant's member accounts).
    payout_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("agent_member_bank_account.id"), nullable=True)
    payout_account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payout_account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payout_ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    payout_bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payout_branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payout_upi_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Mark Deposit — the Data Operator performs what the Admin does in the merchant workflow ──
    deposited_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    deposited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    deposit_utr: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    deposit_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    sent_for_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approver_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)     # chosen Authorized Approver
    approver_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    approved_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # The single moment the money actually moved — set wherever the status enters a COMPLETED
    # state, whichever route it took. Each route records its own step timestamp (deposited_at at
    # Mark Deposit, manager_action_at at the Manager gate, only updated_at at payout, approved_at
    # on the legacy approve), so none of those alone can answer "when did this complete?".
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Withdrawal only — the deposit whose agent was auto-fetched (latest deposit for the membership).
    linked_deposit_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("agent_transaction.id"), nullable=True)

    # ── Standard audit columns ──
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentTransactionAudit(Base):
    """Immutable audit history for an AgentTransaction — every create, amount correction and
    approval decision. Powers the Manage-Transaction old→new amount trail. Isolated to the agent
    subsystem (a parallel AuditLog row with action_type AGENT_TXN_* is also written so the existing
    Agent Audit Trail keeps a single unified view)."""
    __tablename__ = "agent_transaction_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    agent_transaction_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_transaction.id"), index=True, nullable=False)
    reference_number: Mapped[Optional[str]] = mapped_column(String(24), index=True, nullable=True)  # denormalized for search
    # CREATED | AMOUNT_UPDATED | SENT_FOR_APPROVAL | APPROVED | REJECTED
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    old_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    new_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approver_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    merchant_business: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    actor_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TransactionAttachment(Base):
    """Metadata for one uploaded file that now lives in object storage, not in the row.

    Historically a proof/bank image was a base64 data URL held directly in a `transactions`
    TEXT column — ~2 MB per row, 162 MB in total (see S3_IMAGE_MIGRATION.md). Under the object
    storage backend the bytes move to object storage and the column keeps only a provider-neutral
    ``storage://<key>`` reference;
    this table records everything ABOUT that object which the column alone cannot express.

    Deliberately NOT on the read path. `_t()` serialises a transaction synchronously and cannot
    issue a query, so it resolves the reference held on the row itself. This table exists for
    metadata, operations and audit — "what was uploaded, how big, what type, when" — and can be
    joined when that is actually wanted. Losing a row here degrades reporting, never rendering.

    One row per (transaction, field, key). `merchant_proofs` holds up to three files, so a
    single transaction/field pair can legitimately have several rows; the key distinguishes them.
    """
    __tablename__ = "transaction_attachment"
    __table_args__ = (
        UniqueConstraint("transaction_id", "field", "object_key", name="uq_txn_attachment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transaction_id: Mapped[int] = mapped_column(Integer, ForeignKey("transactions.id"), index=True, nullable=False)
    # Which column the file belongs to: merchant_proof | merchant_proofs | admin_proof | admin_bank_image.
    field: Mapped[str] = mapped_column(String(32), nullable=False)

    # Which backend actually holds the bytes ("s3", and later "minio" / "azure" / "gcs"). The
    # reference on the transactions row stays provider-NEUTRAL (`storage://<key>`) so the data
    # never hard-codes today's provider; this column is where the answer lives, per file, so a
    # future migration can tell what needs moving without rewriting a single reference.
    storage_backend: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # `object_key` is the container/bucket-relative key — the portable part, identical whichever
    # provider stores it. `object_url` is the provider-native DURABLE URI (e.g. s3://bucket/key)
    # kept for operators and tooling; never a presigned link, which expires and would be useless
    # once stored (the KYC module records being caught by exactly that with the provider's 48h URLs).
    object_key: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    object_url: Mapped[Optional[str]] = mapped_column(String(768), nullable=True)

    file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # decoded bytes
    # SHA-256 of the content. The key embeds its first 16 chars, so this is both an integrity
    # check and what makes a re-run of the backfill recognise an object it already uploaded.
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # Set when the original base64 was cleared from the transactions row, i.e. the point of no
    # return for that file. NULL means the column still holds the inline copy and rollback is
    # still possible.
    source_cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
