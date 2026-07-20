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
    phone: Optional[str] = None               # own contact number (used for WhatsApp notifications)
    new_password: Optional[str] = None
    current_password: Optional[str] = None
    avatar: Optional[str] = None
    whatsappEnabled: Optional[bool] = None   # "Receive WhatsApp Notifications" preference


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
    category: str = "Announcements"
    title: str
    body: str = ""
    image: Optional[str] = None
    published: bool = True
    featured: bool = False
    priority: str = "Normal"
    publish_date: Optional[date] = None


# ─── Blog Schemas ─────────────────────────────────────────────────────────────
class BlogIn(BaseModel):
    title: str
    category: str = "Announcements"
    short_description: Optional[str] = None
    content: str = ""
    cover_image: Optional[str] = None
    publish_date: Optional[date] = None
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
    # Type-specific fields for CASH (village/city/mobile) and CRYPTO (walletAddress/network/txHash).
    depositDetails: Optional[dict] = None
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
    # "Send To Approval" (demo only): the chosen Authorized Approver (a Supervisor/Manager of the
    # merchant's own business). Ignored on Production, where the section is not shown.
    sentForApproval: bool = False
    approverUserId: Optional[int] = None


class WithdrawalCreate(BaseModel):
    amount: float
    memberId: str
    memberName: Optional[str] = None   # captured/auto-filled membership name (Change 10)
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
    # "Send To Approval" (demo only): the chosen Authorized Approver (a Supervisor/Manager of the
    # merchant's own business). Ignored on Production, where the section is not shown.
    sentForApproval: bool = False
    approverUserId: Optional[int] = None


class SettlementCreate(BaseModel):
    amount: float
    memberId: Optional[str] = None
    memberName: Optional[str] = None   # captured/auto-filled membership name (Change 10)
    proof: Optional[str] = None
    proofs: Optional[list[str]] = None  # up to 3 proof/slip files (data URLs)


class AccountSubmitRequest(BaseModel):
    adminRef: Optional[str] = None
    adminProof: Optional[str] = None
    adminBankDetails: Optional[str] = None
    adminBankImage: Optional[str] = None   # custom bank-details image (overrides the auto card)
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


class RemarkRequest(BaseModel):
    """Reviewer (Supervisor/Manager) action with mandatory free-text remarks."""
    remark: str


class SettlementSupervisorComplete(BaseModel):
    """Supervisor completion of an agent-assigned settlement (demo): mandatory remark + UTR +
    settlement proof (image/PDF) — the same evidence the Admin supplies at /done."""
    remark: str
    utr: str
    proof: str   # base64 data-URL, image or PDF


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
    # Configurable initial Highest Credit / Highest Debit (₹, default 0). The Highest Debit value
    # seeds both the auto-raising highest_debit and the fixed low-debit alert threshold, then each
    # is auto-tracked thereafter.
    highest_credit: Optional[float] = 0.0
    highest_debit: Optional[float] = 0.0


# ─── Support Chat Schemas ─────────────────────────────────────────────────────
class SupportMessageCreate(BaseModel):
    merchant_id: Optional[int] = None  # required when sent by a support agent
    content: str = ""                  # may be empty when an attachment is present
    attachment: Optional[str] = None   # base64 data-URL (image/document)
    attachment_name: Optional[str] = None


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


# ─── Support Management Schemas ───────────────────────────────────────────────
class SupportMemberCreate(BaseModel):
    username: str
    password: str
    email: str
    fullName: str
    phone: Optional[str] = None
    department: Optional[str] = None
    shift: Optional[str] = None
    status: Optional[str] = "Active"          # "Active" | "Inactive"
    merchantIds: list[int] = []


class SupportMemberUpdate(BaseModel):
    fullName: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    shift: Optional[str] = None


class AssignMerchantsRequest(BaseModel):
    merchantIds: list[int] = []


class AvailabilityRequest(BaseModel):
    availability: str                          # "AVAILABLE" | "BUSY" | "ON_BREAK"


class SupportConfigUpdate(BaseModel):
    maxActiveConversations: Optional[int] = None
    strategy: Optional[str] = None             # "LEAST_ACTIVE" | "ROUND_ROBIN"


class ReassignConversationRequest(BaseModel):
    supportId: int


# ─── AI Schemas ───────────────────────────────────────────────────────────────
class AIMessage(BaseModel):
    role: str
    content: str


class AIChatRequest(BaseModel):
    messages: list[AIMessage]


class AIChatResponse(BaseModel):
    reply: str


# ─── Agent Master Schemas (Agent Management → Agents) ─────────────────────────
# camelCase in/out to match the frontend convention (see the route serializer). Field
# validation (required, exactly-3-char code, non-negative fees, category enum) is enforced
# in the route so error messages stay user-friendly.
class AgentCreate(BaseModel):
    fullName: str
    country: str
    state: str
    location: str
    mobile: Optional[str] = None
    mobileCode: Optional[str] = None          # dial code, e.g. "+91"
    email: Optional[str] = None
    currency: str
    dateOfCreation: Optional[str] = None          # IST YYYY-MM-DD; defaults to today
    reference: Optional[str] = None
    # Per-leg fees, set manually. Deposit -> payInFee, withdrawal -> payOutFee,
    # settlement -> settlementFee. These replace the retired single feesPct.
    payInFee: float
    payOutFee: float
    settlementFee: float
    transactionCode: str                          # exactly 3 alphanumeric chars
    category: str                                 # CASH | BANK_TRANSFER | CRYPTO
    notes: Optional[str] = None
    riskAnalysis: bool = False
    sendForApproval: bool = False


class AgentUpdate(BaseModel):
    mobileCode: Optional[str] = None          # dial code, e.g. "+91"
    # Agent ID and Transaction Code are immutable — intentionally absent here.
    fullName: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    location: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    currency: Optional[str] = None
    reference: Optional[str] = None
    payInFee: Optional[float] = None
    payOutFee: Optional[float] = None
    settlementFee: Optional[float] = None
    category: Optional[str] = None
    notes: Optional[str] = None
    riskAnalysis: Optional[bool] = None
    status: Optional[str] = None                   # ACTIVE | INACTIVE


class AgentStatusUpdate(BaseModel):
    status: str                                    # ACTIVE | INACTIVE


# ─── Agent Account Schemas (Agent Management → Agent Accounts) ────────────────
# One agent → many accounts (Bank / UPI / QR / Crypto). Type-specific fields are all optional
# on the wire; the route validates the ones required for the chosen accountType.
class AgentAccountCreate(BaseModel):
    accountType: str                               # BANK | UPI | QR | CRYPTO
    label: Optional[str] = None
    currency: Optional[str] = None                 # defaults to the agent's currency
    notes: Optional[str] = None
    isDefault: bool = False
    # Bank
    accountHolder: Optional[str] = None
    accountNumber: Optional[str] = None
    ifsc: Optional[str] = None
    bankName: Optional[str] = None
    branch: Optional[str] = None
    # UPI
    upiId: Optional[str] = None
    upiHolder: Optional[str] = None
    # QR
    qrImage: Optional[str] = None                  # base64 data-URL
    qrLinkedRef: Optional[str] = None
    # Crypto
    walletAddress: Optional[str] = None
    cryptoNetwork: Optional[str] = None
    cryptoAsset: Optional[str] = None


class AgentAccountUpdate(BaseModel):
    # accountType and accountRef are immutable — intentionally absent.
    label: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None                    # ACTIVE | INACTIVE
    accountHolder: Optional[str] = None
    accountNumber: Optional[str] = None
    ifsc: Optional[str] = None
    bankName: Optional[str] = None
    branch: Optional[str] = None
    upiId: Optional[str] = None
    upiHolder: Optional[str] = None
    qrImage: Optional[str] = None
    qrLinkedRef: Optional[str] = None
    walletAddress: Optional[str] = None
    cryptoNetwork: Optional[str] = None
    cryptoAsset: Optional[str] = None


class AgentAccountStatusUpdate(BaseModel):
    status: str                                    # ACTIVE | INACTIVE


# ─── Agent Assignment Schemas (Phase 4 — assign an agent+account to a transaction) ──
class AgentAssignmentCreate(BaseModel):
    agentId: int                                   # AgentMaster.id
    agentAccountId: int                            # AgentAccount.id (must belong to the agent)
    paymentMethod: Optional[str] = None            # account type BANK|UPI|QR|CRYPTO (cross-checked)
