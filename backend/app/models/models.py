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
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)

    # Which admin created this merchant (null for admins / super admin)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

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
    tx_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    tx_time: Mapped[str] = mapped_column(String(16), nullable=False)

    # Deposit-specific
    deposit_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    member_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    member_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    segment: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)

    # Withdrawal-specific
    bank_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Proof / verification (Admin "Check" workflow)
    merchant_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # merchant-uploaded image (data URL)
    admin_proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # admin-uploaded image (data URL)
    admin_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True) # admin reference number

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
