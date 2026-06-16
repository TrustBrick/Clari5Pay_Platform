export type UserRole = 'SUPER_ADMIN' | 'ADMIN' | 'MERCHANT' | 'SUPPORT_AGENT';

export interface User {
  id: number;
  username: string;
  password?: string;
  role: UserRole;
  email: string;
  name: string;
  phone?: string;
  active: boolean;
  created: string;
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
  // Super Admin monitoring
  merchantCount?: number;
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
  | 'ACCOUNT_SUBMITTED';

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
  merchantProof?: string | null;
  adminProof?: string | null;
  adminRef?: string | null;
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
  icon: string;
  color: string;
  msg: string;
  time: string;
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

export interface ApiError {
  detail: string;
}
