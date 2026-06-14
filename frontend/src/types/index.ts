export type UserRole = 'SUPER_ADMIN' | 'ADMIN' | 'MERCHANT';

export interface User {
  id: number;
  username: string;
  password?: string;
  role: UserRole;
  email: string;
  name: string;
  active: boolean;
  created: string;
  // Merchant-only fields
  payIn?: string;
  payOut?: string;
  settlement?: string;
  payInFee?: number;
  payOutFee?: number;
  balance?: number;
  risk?: 'LOW' | 'MEDIUM' | 'HIGH';
  profile?: string;
}

export type TxStatus =
  | 'PENDING'
  | 'ADMIN_APPROVED'
  | 'COMPLETED'
  | 'SUCCESSFUL'
  | 'REJECTED'
  | 'SA_REJECTED'
  | 'CANCELLED';

export type TxType = 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT';

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
  bank?: string;
  refPrefix?: string;
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
