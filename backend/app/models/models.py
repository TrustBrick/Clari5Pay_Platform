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


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TxType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    SETTLEMENT = "SETTLEMENT"


class TxStatus(str, enum.Enum):
    PENDING = "PENDING"
    ADMIN_APPROVED = "ADMIN_APPROVED"
    COMPLETED = "COMPLETED"
    SUCCESSFUL = "SUCCESSFUL"
    REJECTED = "REJECTED"
    SA_REJECTED = "SA_REJECTED"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), nullable=False)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)

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
    status: Mapped[TxStatus] = mapped_column(SAEnum(TxStatus), default=TxStatus.PENDING, nullable=False)
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    merchant_user: Mapped["User"] = relationship("User", back_populates="transactions", foreign_keys=[merchant_id])
