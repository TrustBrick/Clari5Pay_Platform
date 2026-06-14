from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, EmailStr
from app.models.models import UserRole, RiskLevel, TxType, TxStatus


# ─── User Schemas ─────────────────────────────────────────────────────────────
class UserBase(BaseModel):
    username: str
    email: str
    name: str
    role: UserRole
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
    bank: Optional[str] = None

    class Config:
        from_attributes = True


class DepositCreate(BaseModel):
    amount: float
    depositType: str
    memberName: str
    memberId: str
    segment: str = "A"
    profile: str = "NEW"


class WithdrawalCreate(BaseModel):
    amount: float
    memberId: str
    accountHolder: str
    accountNumber: str
    ifsc: str
    bankName: str


class SettlementCreate(BaseModel):
    amount: float


# ─── AI Schemas ───────────────────────────────────────────────────────────────
class AIMessage(BaseModel):
    role: str
    content: str


class AIChatRequest(BaseModel):
    messages: list[AIMessage]


class AIChatResponse(BaseModel):
    reply: str
