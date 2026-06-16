from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, EmailStr
from app.models.models import UserRole, RiskLevel, TxType, TxStatus, AccountType


# ─── User Schemas ─────────────────────────────────────────────────────────────
class UserBase(BaseModel):
    username: str
    email: str
    name: str
    role: UserRole
    phone: Optional[str] = None
    active: bool = True
    pay_in: Optional[str] = None
    pay_out: Optional[str] = None
    settlement: Optional[str] = None
    pay_in_fee: Optional[float] = None
    pay_out_fee: Optional[float] = None
    balance: Optional[float] = None
    risk: Optional[RiskLevel] = None
    profile: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserOut(UserBase):
    id: int
    created: date

    class Config:
        from_attributes = True


class UserToggle(BaseModel):
    active: bool


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    email: Optional[str] = None
    new_password: Optional[str] = None
    current_password: Optional[str] = None


# ─── Auth Schemas ─────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class TokenData(BaseModel):
    user_id: Optional[int] = None


# ─── Transaction Schemas ──────────────────────────────────────────────────────
class TransactionOut(BaseModel):
    id: str
    ref: str
    type: TxType
    amount: float
    status: TxStatus
    merchantId: int
    merchant: str
    date: str
    time: str
    depositType: Optional[str] = None
    member: Optional[str] = None
    memberId: Optional[str] = None
    bank: Optional[str] = None
    merchantProof: Optional[str] = None
    adminProof: Optional[str] = None
    adminRef: Optional[str] = None

    class Config:
        from_attributes = True


class DepositCreate(BaseModel):
    amount: float
    depositType: str
    memberName: str
    memberId: str
    segment: str = "A"
    profile: str = "NEW"
    proof: Optional[str] = None


class WithdrawalCreate(BaseModel):
    amount: float
    memberId: str
    accountHolder: str
    accountNumber: str
    ifsc: str
    bankName: str
    proof: Optional[str] = None


class SettlementCreate(BaseModel):
    amount: float
    memberId: Optional[str] = None
    proof: Optional[str] = None


class CheckRequest(BaseModel):
    adminRef: str
    adminProof: Optional[str] = None


# ─── Account Schemas ──────────────────────────────────────────────────────────
class AccountCreate(BaseModel):
    reference_number: Optional[str] = None
    account_name: str
    account_number: str
    ifsc_code: str
    bank_name: str
    branch: str
    account_type: AccountType
    status: str = "ACTIVE"
    merchant_id: Optional[int] = None


# ─── Support Chat Schemas ─────────────────────────────────────────────────────
class SupportMessageCreate(BaseModel):
    merchant_id: Optional[int] = None  # required when sent by a support agent
    content: str


class SupportMessageOut(BaseModel):
    id: int
    merchant_id: int
    sender: str
    sender_name: str
    content: str
    read: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ─── AI Schemas ───────────────────────────────────────────────────────────────
class AIMessage(BaseModel):
    role: str
    content: str


class AIChatRequest(BaseModel):
    messages: list[AIMessage]


class AIChatResponse(BaseModel):
    reply: str
