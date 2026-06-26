from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
    String, Integer, Boolean, Float, DateTime, Date,
    ForeignKey, Enum as SAEnum, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
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
    balance: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    risk: Mapped[Optional[RiskLevel]] = mapped_column(SAEnum(RiskLevel), nullable=True)
    profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

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
    merchant_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # first merchant slip image (data URL) — kept for back-compat
    merchant_proofs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of up to 3 proof/slip files (data URLs)
    merchant_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # merchant payment reference number
    admin_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # admin-uploaded bank-details image (data URL)
    admin_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True) # admin reference number
    admin_bank_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # admin manually-entered bank details
    admin_bank_image: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # admin custom bank-details image (data URL) — overrides the auto card
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
    # UPI/QR deposits: when the generated QR stops being valid (15 minutes after it is issued/regenerated).
    qr_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    merchant_user: Mapped["User"] = relationship("User", back_populates="transactions", foreign_keys=[merchant_id])


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
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


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
