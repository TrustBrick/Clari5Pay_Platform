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
    merchant_role: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserOut(UserBase):
    id: int
    created: date
    created_at: Optional[datetime] = None

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
    avatar: Optional[str] = None


# ─── Auth Schemas ─────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class TokenData(BaseModel):
    user_id: Optional[int] = None


class OtpVerifyRequest(BaseModel):
    otpToken: str
    code: str


class OtpResendRequest(BaseModel):
    otpToken: str


class OtpConfigRequest(BaseModel):
    enabled: bool


class ForgotPasswordRequest(BaseModel):
    username: str


class VerifyResetOtpRequest(BaseModel):
    resetToken: str
    code: str


class ResetPasswordRequest(BaseModel):
    confirmedToken: str
    newPassword: str


class AdminResetPasswordRequest(BaseModel):
    new_password: str


# ─── News Schemas ─────────────────────────────────────────────────────────────
class NewsIn(BaseModel):
    section: str = "Announcements"
    title: str
    body: str = ""
    image: Optional[str] = None
    published: bool = True
    priority: str = "Normal"
    publish_date: Optional[date] = None


# ─── Blog Schemas ─────────────────────────────────────────────────────────────
class BlogCategoryIn(BaseModel):
    name: str
    description: Optional[str] = None


class BlogIn(BaseModel):
    title: str
    category_id: Optional[int] = None
    short_description: Optional[str] = None
    content: str = ""
    cover_image: Optional[str] = None
    images: list[str] = []
    tags: list[str] = []
    status: str = "DRAFT"          # DRAFT | PUBLISHED


class BlogStatusIn(BaseModel):
    status: str                    # DRAFT | PUBLISHED


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
    merchantRef: Optional[str] = None
    adminProof: Optional[str] = None
    adminRef: Optional[str] = None
    adminBankDetails: Optional[str] = None
    adminUpiId: Optional[str] = None

    class Config:
        from_attributes = True


class DepositCreate(BaseModel):
    amount: float
    depositType: str
    memberName: str
    memberId: str
    segment: str = "A"
    profile: str = "NEW"
    senderUpiId: Optional[str] = None   # merchant's own UPI the payment is sent from (UPI deposits)
    proof: Optional[str] = None
    proofs: Optional[list[str]] = None  # up to 3 proof/slip files (data URLs)
    # Selected/added merchant bank account sent to admin with the request
    accountHolder: Optional[str] = None
    accountNumber: Optional[str] = None
    ifsc: Optional[str] = None
    bankName: Optional[str] = None
    branch: Optional[str] = None
    utr: Optional[str] = None
    notes: Optional[str] = None
    riskAnalysis: bool = False
    saveBankAccount: bool = False


class WithdrawalCreate(BaseModel):
    amount: float
    memberId: str
    # Payout mode + its mode-specific fields (BANK / UPI / CASH / CRYPTO).
    payoutMode: Optional[str] = None
    payoutDetails: Optional[dict] = None
    accountHolder: Optional[str] = None
    accountNumber: Optional[str] = None
    ifsc: Optional[str] = None
    bankName: Optional[str] = None
    branch: Optional[str] = None
    proof: Optional[str] = None
    proofs: Optional[list[str]] = None  # up to 3 proof/slip files (data URLs)
    utr: Optional[str] = None
    notes: Optional[str] = None
    saveBankAccount: bool = False


class SettlementCreate(BaseModel):
    amount: float
    memberId: Optional[str] = None
    proof: Optional[str] = None
    proofs: Optional[list[str]] = None  # up to 3 proof/slip files (data URLs)


class AccountSubmitRequest(BaseModel):
    adminRef: Optional[str] = None
    adminProof: Optional[str] = None
    adminBankDetails: Optional[str] = None
    adminUpiId: Optional[str] = None


class SlipRequest(BaseModel):
    merchantProof: Optional[str] = None
    merchantProofs: Optional[list[str]] = None  # up to 3 proof/slip files (data URLs)
    merchantRef: Optional[str] = None


class CompleteRequest(BaseModel):
    adminProof: Optional[str] = None  # payment receipt image for withdrawals/settlements
    adminUtr: Optional[str] = None    # agent's payment UTR number


class ReasonRequest(BaseModel):
    reason: Optional[str] = None


class RejectRequest(BaseModel):
    reason: str


class BankAccountCreate(BaseModel):
    accountHolder: str
    accountNumber: str
    ifsc: str
    branch: str
    bankName: Optional[str] = None
    memberId: Optional[str] = None


# ─── Admin UPI Schemas ────────────────────────────────────────────────────────
class AdminUpiCreate(BaseModel):
    label: Optional[str] = None
    upiId: str
    accountRef: Optional[str] = None   # the receiving account this UPI belongs to


class AdminUpiLink(BaseModel):
    accountRef: Optional[str] = None


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
    upiId: Optional[str] = None   # optional UPI to link to this account on creation


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
