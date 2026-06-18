export type UserRole = 'SUPER_ADMIN' | 'ADMIN' | 'MERCHANT' | 'SUPPORT_AGENT';

export interface User {
  id: number;
  username: string;
  password?: string;
  role: UserRole;
  email: string;
  name: string;
  phone?: string;
  avatar?: string | null;
  active: boolean;
  locked?: boolean;
  lockedUntil?: string | null;
  failedAttempts?: number;
  created: string;
  createdAt?: string | null;
  createdBy?: number | null;
  // Merchant-only fields
  payIn?: string;
  payOut?: string;
  settlement?: string;
  payInFee?: number;
  payOutFee?: number;
  balance?: number;
  risk?: 'LOW' | 'MEDIUM' | 'HIGH';
  profile?: string;
  merchantRole?: MerchantRole | string | null;
  // Super Admin monitoring
  merchantCount?: number;
}

export type MerchantRole = 'DEO' | 'DEPOSIT_OPERATOR' | 'WITHDRAWAL_OPERATOR' | 'SUPERVISOR' | 'MANAGER';

export interface AuditLogEntry {
  id: number;
  userId: number | null;
  username: string;
  role: string | null;
  action: string;
  entityType: string | null;
  entityId: string | null;
  oldValue: string | null;
  newValue: string | null;
  reason: string | null;
  ip: string | null;
  createdAt: string;
}

export interface MerchantBankAccount {
  id: number;
  accountHolder: string;
  accountNumber: string;
  ifsc: string;
  branch: string;
  bankName?: string | null;
}

export type TxStatus =
  | 'PENDING'
  | 'ADMIN_APPROVED'
  | 'COMPLETED'
  | 'SUCCESSFUL'
  | 'REJECTED'
  | 'SA_REJECTED'
  | 'CANCELLED'
  | 'ACCOUNT_REQUESTED'
  | 'ACCOUNT_SUBMITTED'
  | 'SLIP_SUBMITTED';

export type TxType =
  | 'DEPOSIT'
  | 'WITHDRAWAL'
  | 'SETTLEMENT'
  | 'DEPOSIT_REQUEST'
  | 'WITHDRAWAL_REQUEST'
  | 'SETTLEMENT_REQUEST';

export interface Transaction {
  id: string;
  ref: string;
  type: TxType;
  amount: number;
  status: TxStatus;
  merchantId: number;
  merchant: string;
  date: string;
  time: string;
  depositType?: string;
  member?: string;
  memberId?: string;
  bank?: string;
  accountHolder?: string | null;
  accountNumber?: string | null;
  ifsc?: string | null;
  merchantProof?: string | null;
  merchantRef?: string | null;
  adminProof?: string | null;
  adminRef?: string | null;
  adminBankDetails?: string | null;
  adminUpiId?: string | null;
  qrExpiresAt?: string | null;
  utr?: string | null;
  notes?: string | null;
  riskAnalysis?: boolean;
  rejectReason?: string | null;
  refPrefix?: string;
}

export interface Account {
  id: number;
  referenceNumber: string;
  accountName: string;
  accountNumber: string;
  ifscCode: string;
  bankName: string;
  branch: string;
  accountType: string;
  status: string;
  createdDate: string;
  createdTime: string;
  lastMaintenanceDate?: string | null;
  lastMaintenanceTime?: string | null;
  merchantName: string;
}

export interface SupportMessage {
  id: number;
  merchantId: number;
  sender: 'MERCHANT' | 'SUPPORT';
  senderName: string;
  content: string;
  read: boolean;
  createdAt: string;
}

export interface NavItem {
  key: string;
  icon: string;
  label: string;
  badge?: number;
}

export interface Notification {
  id: number;
  icon: string;
  message: string;
  read: boolean;
  createdAt: string;
}

export interface BalanceSummary {
  available: number;
  totalDeposit: number;
  payInFees: number;
  totalSettled: number;
  totalWithdrawn: number;
  payOutFees: number;
  depositCount: number;
  withdrawalCount: number;
}

export interface SystemLogEntry {
  id: number;
  actorId: number | null;
  actor: string;
  action: string;
  detail: string;
  createdAt: string;
}

export interface ChartDataPoint {
  day: string;
  deposit: number;
  withdrawal: number;
}

export interface RiskFactor {
  label: string;
  score: number;
  max: number;
  color: string;
}

export interface AIMessage {
  role: 'user' | 'assistant';
  content: string;
}

// API types
export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface OtpChallenge {
  otpRequired: boolean;
  otpToken: string;
  email: string;   // masked
  devOtp?: string; // present only in local dev (no SMTP configured)
}

export interface ApiError {
  detail: string;
}
