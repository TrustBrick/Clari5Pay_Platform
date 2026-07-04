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
  settlementFee?: number;
  country?: string | null;   // merchant company country (business-level)
  fullName?: string | null;  // personal name for a merchant user (distinct from business name)
  balance?: number;
  risk?: 'LOW' | 'MEDIUM' | 'HIGH';
  profile?: string;
  merchantRole?: MerchantRole | string | null;
  merchantCode?: string | null;   // serial Merchant ID, e.g. MID000001
  whatsappEnabled?: boolean;       // "Receive WhatsApp Notifications" preference (internal users)
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
  location?: string | null;
  createdAt: string;
}

export interface MerchantBankAccount {
  id: number;
  memberId?: string | null;
  accountHolder: string;
  accountNumber: string;
  ifsc: string;
  branch: string;
  bankName?: string | null;
  upiId?: string | null;
  isDefault?: boolean;
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
  | 'SLIP_SUBMITTED'
  // Supervisor (deposit) / Manager (withdrawal) review-gate workflow.
  | 'PENDING_APPROVAL'
  | 'SUPERVISOR_REVIEW'
  | 'MANAGER_REVIEW'
  | 'RESUBMITTED'
  | 'DEPOSITED';

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
  senderUpiId?: string | null;
  bank?: string;
  accountHolder?: string | null;
  accountNumber?: string | null;
  ifsc?: string | null;
  merchantProof?: string | null;
  merchantProofs?: string[] | null;
  merchantRef?: string | null;
  adminProof?: string | null;
  adminBankImage?: string | null;     // admin custom bank-details image (detail fetch only)
  hasAdminBankImage?: boolean;        // lightweight flag present in list payloads
  adminRef?: string | null;
  adminBankDetails?: string | null;
  adminUpiId?: string | null;
  adminUtr?: string | null;
  payoutMode?: string | null;
  payoutDetails?: Record<string, string> | null;
  depositDetails?: Record<string, string> | null;
  qrExpiresAt?: string | null;
  utr?: string | null;
  notes?: string | null;
  riskAnalysis?: boolean;
  highRisk?: boolean;
  rejectReason?: string | null;
  cancelReason?: string | null;
  cancelledBy?: string | null;
  cancelledAt?: string | null;
  // Permanent creator snapshot (stored at creation, survives later profile changes).
  creatorUsername?: string | null;
  creatorRole?: string | null;
  merchantCode?: string | null;
  riskLevel?: string | null;
  // Member/segment context for the Admin details view (segment on the row; the rest derived,
  // detail-fetch only — see get_transaction_detail).
  segment?: string | null;
  memberProfileType?: string | null;   // NEW / OLD (derived from member history)
  memberSegment?: string | null;       // A / B / C / D (this tx's, else member's latest)
  merchantUsername?: string | null;
  merchantBusinessName?: string | null;
  // Review-gate workflow record (Supervisor/Manager → Admin).
  approvedBy?: string | null;
  processedBy?: string | null;
  createdAt?: string | null;
  supervisorName?: string | null;
  supervisorActionAt?: string | null;
  managerName?: string | null;
  managerActionAt?: string | null;
  adminActionAt?: string | null;
  remarksHistory?: RemarkEntry[] | null;
  refPrefix?: string;
}

export interface RemarkEntry {
  role: string;
  user: string;
  action: string;
  remark: string;
  at: string;
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

export interface AccountMerchantBalance {
  merchantName: string;
  merchantCode?: string | null;
  deposited: number;
  available: number;        // AB
  runningBalance: number;   // RB
  mab: number;              // MAB
}

export interface AccountBalance {
  referenceNumber: string;
  accountName: string;
  accountHolder: string;
  accountNumber: string;
  ifscCode: string;
  branch: string;
  bankName: string;
  status: string;
  bankDeposited?: number;
  upiDeposited?: number;
  totalDeposited: number;
  totalFees?: number;
  withdrawals?: number;
  settlements?: number;
  available: number;        // deposits − withdrawals − settlements (all channels)
  linkedUpis?: { id: number; label: string; upiId: string; status: string }[];
  merchants: AccountMerchantBalance[];
}

export interface MerchantBalance {
  name: string;
  available: number;
  runningBalance: number;
}

export interface MerchantStats {
  name: string;
  merchantId: number;
  merchantIds: number[];
  username: string;
  email: string;
  payInFee: number;
  payOutFee: number;
  depositCount: number;
  depositAmount: number;
  withdrawalCount: number;
  withdrawalAmount: number;
  settlementCount: number;
  settlementAmount: number;
  // New financial-summary figures (single source of truth — backend compute_balance):
  totalAvailableBalance: number;      // Total Deposits − Total Withdrawals − Total Settlements
  available: number;                  // Available Balance = Total Available Balance − Deposit Commission − Pay-Out Fee
  availableBalance?: number;
  depositCommission: number;
  withdrawalCommission: number;
  settlementCommission: number;
  totalCommission: number;            // Deposit + Withdrawal + Settlement commission
  payoutFee?: number;                 // withdrawalCommission + settlementCommission (Total Pay-Out Fee)
}

// Platform-wide financial summary — the SINGLE source of truth shared by the Admin and
// Super Admin dashboard finance cards (backend /global-summary → compute_global_summary).
// Identical for every admin: these are system-wide totals, not per-admin values.
export interface GlobalSummary {
  totalAvailableBalance: number;
  totalDeposit: number;
  totalWithdrawn: number;
  totalSettled: number;
  depositCommission: number;
  withdrawalCommission: number;
  settlementCommission: number;
  totalCommission: number;
  payoutFee: number;
  available: number;
  availableBalance: number;
}

export interface AdminUpi {
  id: number;
  label: string;
  upiId: string;
  accountRef?: string | null;
  status: string;
  createdDate: string;
  createdTime: string;
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
  // External link: when set, the sidebar opens this URL in a new tab instead of
  // switching the in-app page (legacy).
  href?: string;
  // Submenu: a group item renders an expandable list of children.
  children?: NavItem[];
  // Gate a child to ADMIN / SUPER_ADMIN only (e.g. Create Blog, Analytics).
  adminOnly?: boolean;
}

export interface Notification {
  id: number;
  icon: string;
  message: string;
  read: boolean;
  createdAt: string;
}

export interface BalanceSummary {
  // New financial-summary figures (single source of truth — backend compute_balance):
  totalAvailableBalance: number;      // Total Deposits − Total Withdrawals − Total Settlements
  available: number;                  // Available Balance = Total Available Balance − Deposit Commission − Pay-Out Fee
  availableBalance?: number;          // explicit alias of `available`
  depositCommission: number;          // pay-in fee on completed deposits
  withdrawalCommission: number;       // pay-out fee on completed withdrawals
  settlementCommission: number;       // pay-out fee on completed settlements
  totalCommission: number;            // Deposit + Withdrawal + Settlement commission
  payoutFee?: number;                 // withdrawalCommission + settlementCommission (Total Pay-Out Fee)
  spendableLimit?: number;            // guard — validation only, never displayed
  runningBalance?: number;            // RB (reserved by pending requests)
  maxSettleable?: number;
  maxWithdrawable?: number;           // spend limit net of the pay-out fee on a new withdrawal
  totalDeposit: number;
  payInFees: number;
  totalSettled: number;
  settlementFees?: number;
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

export interface NewsPost {
  id: number;
  section: string;
  category: string;
  title: string;
  body: string;
  image?: string | null;
  author: string;
  published: boolean;
  featured: boolean;
  views: number;
  priority?: string;
  publishDate?: string | null;
  createdAt: string;
  updatedAt?: string | null;
}

export interface BlogCategory {
  id: number;
  name: string;
  slug?: string | null;
  description?: string | null;
  postCount?: number | null;
  createdAt?: string | null;
}

export interface BlogPost {
  id: number;
  title: string;
  slug?: string | null;
  categoryId?: number | null;
  category?: string | null;
  shortDescription?: string | null;
  coverImage?: string | null;
  content?: string;
  images?: string[];
  status: 'DRAFT' | 'PUBLISHED';
  author: string;
  authorId?: number | null;
  views: number;
  likes: number;
  shares: number;
  commentsCount: number;
  readMinutes?: number;
  tags?: string[];
  createdAt?: string | null;
  updatedAt?: string | null;
  publishedAt?: string | null;
}

export interface BlogStats {
  total: number;
  published: number;
  draft: number;
  totalViews: number;
  totalCategories: number;
  mostViewed?: { id: number; title: string; views: number } | null;
}

export interface BlogAnalytics {
  topViewed: Array<{ id: number; title: string; views: number; reads: number; avgReadTime: number }>;
  categoryPerformance: Array<{ category: string; views: number; posts: number }>;
  mostPopularCategory?: string | null;
  monthly: Array<{ month: string; key: string; published: number; views: number }>;
}

// ─── Reports module (merchant) ────────────────────────────────────────────────
export interface ReportWindow {
  count: number;
  totalAmount: number;
  deposits: number;
  withdrawals: number;
  settlements: number;
  depositCount: number;
  withdrawalCount: number;
  settlementCount: number;
}
export interface ReportMemberRow {
  rank?: number;
  memberId: string;
  memberName: string;
  count?: number;
  deposit?: number;
  withdrawal?: number;
  settlement?: number;
  total?: number;
}
export interface ReportLargest {
  memberId: string;
  memberName: string;
  amount: number;
  date: string;
  time: string;
}
export interface ReportRow {
  ref: string;
  memberId: string | null;
  member: string;
  business?: string | null;   // merchant business name (admin Reports — all-merchants view)
  type: 'deposit' | 'withdrawal' | 'settlement' | null;
  depositType?: string | null;
  amount: number;
  status: string;
  date: string;
  time: string;
  createdAt: string | null;
  completed: boolean;
  cancelReason?: string | null;
  paymentMethod?: string | null;
  approvedBy?: string | null;
  processedBy?: string | null;
  agentCode?: string | null;
  riskLevel?: string | null;
  availableBalance?: number | null;
}
export interface ReportData {
  cards: {
    totalTransactions: number;
    totalDeposits: number;
    totalWithdrawals: number;
    totalSettlements: number;
    totalDepositAmount: number;
    totalWithdrawalAmount: number;
    totalSettlementAmount: number;
    // New financial-summary figures (single source of truth — backend compute_balance):
    totalAvailableBalance: number;     // Total Deposits − Total Withdrawals − Total Settlements
    availableBalance: number;          // Available Balance = Total Available Balance − Deposit Commission − Pay-Out Fee
    depositCommission: number;
    withdrawalCommission: number;
    settlementCommission: number;
    totalCommission: number;           // Deposit + Withdrawal + Settlement commission
    payoutFee?: number;                // withdrawalCommission + settlementCommission
    totalTransactionAmount: number;
    activeMemberships: number;
    mostActiveMember: { memberId: string; memberName: string; count: number } | null;
    largestTransactionToday: { memberId: string; memberName: string; amount: number; type: string | null; date: string; time: string } | null;
  };
  windows: Record<'10m' | '20m' | '30m' | '1h' | 'today' | 'yesterday' | '7d' | '30d', ReportWindow>;
  memberAnalytics: {
    mostActive: ReportMemberRow[];
    largestDeposit: ReportMemberRow[];
    largestWithdrawal: ReportMemberRow[];
    largestSettlement: ReportMemberRow[];
    highestValue: ReportMemberRow[];
  };
  intelligence: {
    largestDepositEver: ReportLargest | null;
    largestWithdrawalEver: ReportLargest | null;
    largestSettlementEver: ReportLargest | null;
  };
  trends: {
    deposits: Array<{ date: string; amount: number }>;
    withdrawals: Array<{ date: string; amount: number }>;
    settlements: Array<{ date: string; amount: number }>;
    membershipGrowth: Array<{ date: string; count: number }>;
  };
  insights: string[];
  transactions: ReportRow[];
}

// ─── Risk Management module ───────────────────────────────────────────────────
export type RiskLevelStr = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

export interface RiskMember {
  memberId: string;
  memberName: string;
  merchantName: string;
  riskLevel: RiskLevelStr;
  totalTransactions: number;
  totalVolume: number;
  lastActivity: string | null;
}
export interface RiskOverview {
  scope: 'MERCHANT' | 'ADMIN' | 'SUPER_ADMIN';
  members: RiskMember[];
  stats: { low: number; medium: number; high: number; critical: number };
  topMembers: RiskMember[];
  topMerchants?: Array<{ merchantName: string; members: number; volume: number }>;
}
export interface RiskTxnStat { count: number; total: number; largest: number; average: number }
export interface RiskProfile {
  profile: {
    memberId: string; memberName: string; merchantName: string;
    registrationDate: string | null; firstTransactionDate: string | null; lastTransactionDate: string | null;
    totalDeposits: number; totalWithdrawals: number; totalSettlements: number; totalVolume: number;
    riskLevel: RiskLevelStr;
  };
  txnIntel: { deposits: RiskTxnStat; withdrawals: RiskTxnStat; settlements: RiskTxnStat };
  relationships: {
    linkedAccounts: Array<{ accountHolder: string | null; accountNumber: string | null; bankName: string | null; ifsc: string | null; branch: string | null }>;
    linkedUpis: Array<{ upiId: string }>;
    repeatedSenders: Array<{ upiId: string; count: number }>;
    relatedMemberships: Array<{ memberId: string; via: string | null }>;
  };
  summary: { strengths: string[]; indicators: string[] };
  transactions: Array<{ ref: string; type: string | null; amount: number; status: string; date: string; time: string; createdAt: string | null }>;
}

// ─── Cyber Crime Complaint ────────────────────────────────────────────────────
export interface BankDetail {
  accountHolder: string | null;
  accountNumber: string | null;
  bankName: string | null;
  branch: string | null;
  ifsc: string | null;
  upiId: string | null;
}
export interface RiskMemberBanks {
  accounts: BankDetail[];
  upis: string[];
}
export interface ComplaintDoc { name: string; type: string; dataUrl: string; kind?: string }
export type ComplaintStatus = 'DRAFT' | 'OPEN' | 'UNDER_REVIEW' | 'ESCALATED' | 'COMPLAINT_FILED' | 'CLOSED' | 'SUBMITTED';
export type ComplaintPriority = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export interface ComplaintNote { author: string; role: string; text: string; at: string }
export interface Complaint {
  id: number;
  caseId: string;
  ref: string;
  memberId: string;
  memberName: string;
  merchantName: string;
  status: ComplaintStatus;
  priority: ComplaintPriority;
  riskLevel: string;
  assignedTo: string | null;
  assignedToId: number | null;
  accountHolder?: string | null;
  accountNumber?: string | null;
  bankName?: string | null;
  branch?: string | null;
  ifsc?: string | null;
  upiId?: string | null;
  description?: string;
  documents?: ComplaintDoc[];
  notes?: ComplaintNote[];
  resolutionNotes?: string | null;
  timeline?: ComplaintTimeline;
  createdBy: string;
  createdAt: string | null;
  updatedAt?: string | null;
  submittedAt: string | null;
  closedAt: string | null;
}
export interface ComplaintTimeline {
  openedAt: string | null; openedBy?: string | null;
  underReviewAt: string | null; underReviewBy?: string | null;
  escalatedAt: string | null; escalatedBy?: string | null;
  complaintFiledAt: string | null; complaintFiledBy?: string | null;
  closedAt: string | null; closedBy?: string | null;
}
export interface ComplaintList {
  scope: 'MERCHANT' | 'ADMIN' | 'SUPER_ADMIN';
  complaints: Complaint[];
  statuses: ComplaintStatus[];
  priorities: ComplaintPriority[];
}

// ── Active Users (real-time presence) ──
export interface ActiveUserRow {
  id: number;
  name: string;
  username: string;
  merchant?: string | null;
  role: string;
  merchantRole?: string | null;
  phone?: string | null;
  email?: string | null;
  avatar?: string | null;
  country?: string | null;
  status: 'online' | 'offline' | 'busy' | 'break';
  // Support members carry a manual availability shown while online (Available/Busy/On-Break).
  availability?: 'AVAILABLE' | 'BUSY' | 'ON_BREAK' | null;
  supportCode?: string | null;
  loginTime?: string | null;
  lastActivity?: string | null;
  lastSeen?: string | null;
  logoutTime?: string | null;
  sessionDuration?: number | null;   // seconds
  ip?: string | null;
  device?: string | null;
  browser?: string | null;
  os?: string | null;
}
export interface ActiveMerchantRow {
  name: string; online: number; offline: number; total: number; status: string;
}
export interface ActiveUsersData {
  summary: { online: number; offline: number; totalLoggedIn: number; totalRegistered: number };
  merchants: ActiveMerchantRow[];
  users: ActiveUserRow[];
}

// ── Support Management ──
export type SupportAvailability = 'AVAILABLE' | 'BUSY' | 'ON_BREAK';
export type SupportStatus = 'online' | 'busy' | 'break' | 'offline';

export interface SupportAssignedMerchant { id: number; name: string; }

export interface SupportMemberRow {
  id: number;
  supportCode?: string | null;
  fullName: string;
  username: string;
  email: string;
  phone?: string | null;
  avatar?: string | null;
  department?: string | null;
  shift?: string | null;
  status: SupportStatus;
  availability: SupportAvailability;
  active: boolean;
  assignedMerchants: SupportAssignedMerchant[];
  assignedMerchantCount: number;
  loginTime?: string | null;
  lastActivity?: string | null;
  lastSeen?: string | null;
  logoutTime?: string | null;
  currentSession: boolean;
  sessionDuration?: number | null;
  ip?: string | null;
  device?: string | null;
  browser?: string | null;
  os?: string | null;
  createdAt?: string | null;
  created?: string | null;
  createdBy?: number | null;
  activeConversations?: number;   // populated by the profile endpoint
}
export interface SupportMembersData {
  summary: { members: number; online: number; busy: number; onBreak: number; offline: number; assignedMerchants: number; openTickets: number };
  members: SupportMemberRow[];
}
export interface AssignableMerchant { id: number; name: string; merchantCode?: string | null; username?: string; }
