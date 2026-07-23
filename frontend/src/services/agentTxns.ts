// Isolated Agent Transaction subsystem — client for /api/agent-txns.
// This module NEVER calls any merchant Deposit/Withdrawal/Settlement/Treasury/Risk/Account/
// Transaction-History endpoint. Every figure it returns comes only from the agent ledger.
import api, { type Paged } from './api';
import { cachedRef } from '../utils/refCache';

/**
 * Agent deposit workflow — the same labels/order as the merchant deposit workflow, except the
 * Data Operator performs the steps the Admin performs for a merchant:
 *   ACCOUNT_REQUESTED → ACCOUNT_SUBMITTED → SUPERVISOR_REVIEW → SLIP_SUBMITTED → DEPOSITED
 * PENDING / APPROVED are legacy rows created before the chain existed.
 */
// Cash names its first two steps after the token, Crypto after the wallet, Bank/UPI after the agent
// account. SLIP_SUBMITTED means the slip is up and the Supervisor has NOT yet decided; their
// approval is SUPERVISOR_APPROVED. SUPERVISOR_REVIEW is retired — legacy rows only.
export type AgentTxnStatus =
  | 'TOKEN_REQUESTED' | 'TOKEN_SUBMITTED' | 'WALLET_REQUESTED' | 'WALLET_SUBMITTED'
  | 'ACCOUNT_REQUESTED' | 'ACCOUNT_SUBMITTED'
  | 'SLIP_SUBMITTED' | 'SUPERVISOR_APPROVED' | 'MANAGER_REVIEW' | 'MANAGER_APPROVED'
  | 'DEPOSITED' | 'COMPLETED' | 'REJECTED' | 'PENDING' | 'APPROVED'
  | 'SUPERVISOR_REVIEW'
  // A Cash deposit split among members by a DEO: the parent container, non-crediting and final.
  | 'DISTRIBUTED'
  // Settlement chain: the offline payment workflow. SETTLED is the completed state.
  | 'SETTLEMENT_REQUESTED' | 'SETTLEMENT_ACCEPTED' | 'PROOF_UPLOADED' | 'SETTLED';

/** Statuses that mean the money actually moved — the completed-only basis (mirrors the server). */
export const AGENT_COMPLETED_STATUSES: AgentTxnStatus[] = ['APPROVED', 'DEPOSITED', 'COMPLETED', 'SETTLED'];
// DISTRIBUTED is final (immutable) but NOT completed — the container moves no money; its children do.
export const AGENT_FINAL_STATUSES: AgentTxnStatus[] = [...AGENT_COMPLETED_STATUSES, 'REJECTED', 'DISTRIBUTED'];

/** A payout account saved against a Membership ID in the isolated agent register. */
export interface AgentMemberAccount {
  id: number;
  membershipId: string;
  memberName?: string | null;
  accountHolder?: string | null;
  accountNumber?: string | null;
  ifsc?: string | null;
  bankName?: string | null;
  branch?: string | null;
  upiId?: string | null;
  isDefault?: boolean;
  label?: string | null;
}

/** An AGENT account offered at the Account Submission step (never a merchant account). */
export interface AgentAccountOption {
  id: number;
  accountRef: string;
  accountType: string;      // BANK | UPI | QR | CRYPTO
  label?: string | null;
  currency?: string | null;
  isDefault?: boolean;
  detail?: string | null;
  qrImage?: string | null;
}

export interface AgentTxnRow {
  id: number;
  referenceNumber: string;
  transactionCode: string;
  type: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT';
  agentMasterId: number;
  agentCode?: string | null;
  agentName?: string | null;
  agentCountry?: string | null;
  agentState?: string | null;
  agentLocation?: string | null;
  agentCategory?: string | null;
  membershipId: string;
  membershipName?: string | null;
  membershipType: string;
  amount: number;
  country?: string | null;
  state?: string | null;
  location?: string | null;
  mobile?: string | null;
  mobileCode?: string | null;
  tokenDetails?: string | null;
  noteNumber?: string | null;
  /** The Reference Number the MEMBER supplied during the withdrawal — not the system serial
   *  in `referenceNumber`. Captured on the Create Agent Withdrawal Request form. */
  memberReference?: string | null;
  notes?: string | null;
  instructions?: string | null;
  /** Same labels as the merchant deposit workflow. Legacy rows keep PENDING/APPROVED/REJECTED. */
  status: AgentTxnStatus;
  // Transaction type + Sending Account (mirrors the merchant Deposit Request).
  txnMethod?: string | null;
  senderUpiId?: string | null;
  senderAccountHolder?: string | null;
  senderAccountNumber?: string | null;
  senderIfsc?: string | null;
  senderBankName?: string | null;
  senderBranch?: string | null;
  // Account submission (always an AGENT account).
  agentAccountId?: number | null;
  agentAccountRef?: string | null;
  agentAccountType?: string | null;
  agentAccountDetail?: string | null;
  walletAddress?: string | null;
  accountProof?: string | null;
  accountSubmittedBy?: string | null;
  accountSubmittedDate?: string | null;
  accountSubmittedTime?: string | null;
  // Slip.
  slipImage?: string | null;
  slipSubmittedBy?: string | null;
  slipSubmittedDate?: string | null;
  slipSubmittedTime?: string | null;
  // Review gate.
  supervisorName?: string | null;
  managerName?: string | null;
  reviewRemark?: string | null;
  // Withdrawal payout account (where the money is sent).
  payoutAccountId?: number | null;
  payoutAccountHolder?: string | null;
  payoutAccountNumber?: string | null;
  payoutIfsc?: string | null;
  payoutBankName?: string | null;
  payoutBranch?: string | null;
  payoutUpiId?: string | null;
  // Mark Deposit.
  depositedBy?: string | null;
  depositedDate?: string | null;
  depositedTime?: string | null;
  depositUtr?: string | null;
  depositProof?: string | null;
  sentForApproval: boolean;
  approverName?: string | null;
  approvedBy?: string | null;
  approvedDate?: string | null;
  approvedTime?: string | null;
  /** When the money actually moved — set by whichever route completed the transaction
   *  (Mark Deposit, the Manager gate, payout, or the legacy approve). Null until completed. */
  completedAt?: string | null;
  completedDate?: string | null;
  completedTime?: string | null;
  linkedDepositId?: number | null;
  createdBy?: string | null;
  /** True instant (UTC ISO). Prefer this for ordering/relative windows — `createdTime` is an
   *  IST 12-hour display string ("10:34:24 AM") and is NOT machine-parseable by `new Date()`. */
  createdAt?: string | null;
  createdDate?: string | null;
  createdTime?: string | null;
  updatedDate?: string | null;
  updatedTime?: string | null;
  // Per-leg commission (attached by the list endpoint for the Reports columns).
  commissionPct?: number;
  commissionAmount?: number;
  netAmount?: number;
}

export interface AgentOverview {
  cards: {
    totalTransactions: number;
    depositCount: number; depositAmount: number;
    withdrawalCount: number; withdrawalAmount: number;
    settlementCount: number; settlementAmount: number;
    pending: number; approved: number; completed: number; rejected: number; today: number;
    approvedDeposits: number; approvedWithdrawals: number; approvedSettlements: number;
    grossAmount: number; depositCommission: number; withdrawalCommission: number;
    settlementCommission: number;
    netAmount: number; totalCommission: number;
  };
  byAgent: Array<{ agentCode: string | null; agentName: string | null; deposits: number; withdrawals: number; count: number }>;
  trend: Array<{ date: string; deposits: number; withdrawals: number }>;
  recent: AgentTxnRow[];
}

export interface AgentFormAgent {
  id: number; agentId: string; name: string; country: string; state: string;
  location: string; category: string; transactionCode: string; currency: string;
  /** Withdrawal commission % (pay_out_fee) — drives the Withdrawal form's Maximum Withdrawable Amount. */
  withdrawalFee?: number;
}
export interface AgentApprover { id: number; name: string; role: string }
export interface AgentFormData {
  agents: AgentFormAgent[];
  /** Deposit approvers — Supervisors + Managers. */
  approvers: AgentApprover[];
  /** Withdrawal approvers — Managers only; a Supervisor may not approve a withdrawal. */
  withdrawalApprovers: AgentApprover[];
  instructions: string[];
  membershipTypes: string[];
  txnMethods: string[];      // CASH | UPI | BANK | IMPS | NEFT | RTGS | CRYPTO
}

export interface AgentMemberLookup {
  membershipId: string;
  membershipName?: string | null;
  latestDeposit: null | {
    agentMasterId: number; agentCode?: string | null; agentName?: string | null;
    country?: string | null; state?: string | null; location?: string | null;
    category?: string | null; depositId: number; reference: string;
  };
  /** Payout accounts already on file for this membership (auto-fetched on the form). */
  savedAccounts?: AgentMemberAccount[];
  /** This member's Available Balance in the agent ledger (completed deposits net of commission,
   *  less completed withdrawals/settlements). Shown on the Withdrawal/Settlement form. */
  availableBalance?: number;
}

export interface AgentDepositBody {
  agentMasterId: number;
  membershipId: string;
  membershipName?: string;
  membershipType: string;
  amount: number;
  country?: string;
  state?: string;
  location?: string;
  mobile?: string;
  mobileCode?: string;      // dial code for `mobile`
  notes?: string;
  instructions?: string;
  sentForApproval: boolean;
  approverUserId?: number | null;
  // Supplied by the customer/agent and typed in by the operator — mandatory on a Deposit.
  tokenDetails?: string;
  noteNumber?: string;
  /** Member-supplied Reference Number — mandatory on a Withdrawal, alongside noteNumber. */
  memberReference?: string;
  walletAddress?: string;   // CRYPTO withdrawal
  // Transaction type + Sending Account (mirrors the merchant Deposit Request).
  txnMethod?: string;
  senderUpiId?: string;
  senderAccountHolder?: string;
  senderAccountNumber?: string;
  senderIfsc?: string;
  senderBankName?: string;
  senderBranch?: string;
}
/** Settlement methods — Supervisor-only, approval-free; a subset of the transaction types. */
export const AGENT_SETTLEMENT_METHODS = ['CASH', 'BANK', 'CRYPTO'];

export interface AgentWithdrawalBody extends AgentDepositBody {
  linkedDepositId?: number | null;
  // Payout account — an existing saved account, or new details saved for re-use.
  payoutAccountId?: number | null;
  payoutAccountHolder?: string;
  payoutAccountNumber?: string;
  payoutIfsc?: string;
  payoutBankName?: string;
  payoutBranch?: string;
  payoutUpiId?: string;
  savePayoutAccount?: boolean;
}
export interface AgentManageBody {
  amount: number;
  notes?: string;
  sentForApproval: boolean;
  approverUserId?: number | null;
}

/** One member row in a Cash Deposit distribution. */
export interface AgentDistributeMember {
  membershipId: string;
  membershipName?: string | null;
  amount: number;
}
export interface AgentDistributeBody { members: AgentDistributeMember[] }
/** The distributed parent container + the auto-completed child deposits it created. */
export interface AgentDistributeResult { parent: AgentTxnRow; children: AgentTxnRow[] }

export interface AgentTxnAuditRow {
  id: number; action: string; oldAmount: number | null; newAmount: number | null;
  note: string | null; approverName: string | null; actor: string | null; role: string | null;
  createdDate: string | null; createdTime: string | null;
}

export interface AgentTxnQuery {
  status?: string; txn_type?: string; search?: string; date?: string; date_from?: string; date_to?: string;
}

/** AgentTxnQuery + server-side paging. Every filter is applied in Postgres, never in the browser. */
export interface AgentTxnPagedQuery extends AgentTxnQuery {
  /** Payment method (CASH / BANK_TRANSFER / UPI / CRYPTO) — filtered in SQL, not in the browser. */
  txn_method?: string;
  /** Comma-separated statuses to EXCLUDE, e.g. the final ones for an in-flight worklist. */
  status_not?: string;
  /** Field-scoped partial matches, for screens that search these independently. */
  ref?: string; agent_code?: string; membership_id?: string;
  page?: number; page_size?: number;
}

export interface AgentMemberSummary {
  found: boolean;
  membershipId?: string;
  memberName?: string | null;
  depositCount?: number; totalDeposits?: number; depositCommission?: number;
  withdrawalCount?: number; totalWithdrawals?: number; withdrawalCommission?: number;
  settlementCount?: number; totalSettlements?: number; settlementCommission?: number;
  availableBalance?: number;
  lastTransactionDate?: string | null;
}

/** What one agent currently holds, and the ceiling it puts on a withdrawal. */
export interface AgentBalance {
  agentMasterId: number;
  agentId: string;
  agentName: string;
  /** Completed-only balance held by the agent. */
  available: number;
  /** `available` less the agent's in-flight withdrawals/settlements — what the server validates. */
  spendable: number;
  withdrawalFeePct: number;
  /** The fee taken off `spendable` to reach `maxWithdrawable`. */
  withdrawalFee: number;
  /** Available Agent Balance − Withdrawal Fee. */
  maxWithdrawable: number;
}

export interface AgentPerfRow {
  agentMasterId: number; agentId: string; agentName: string; category: string; status: string;
  country?: string | null; currency?: string | null; createdDate?: string | null;
  depositCount: number; depositAmount: number; depositCommission: number;
  withdrawalCount: number; withdrawalAmount: number; withdrawalCommission: number;
  settlementCount: number; settlementAmount: number; settlementCommission: number;
  totalCommission: number; totalTransactions: number; lastTransactionDate?: string | null;
}
export interface AgentPerformance {
  overall: {
    totalDepositAmount: number; totalWithdrawalAmount: number; totalSettlementAmount: number;
    totalDepositCommission: number; totalWithdrawalCommission: number; totalSettlementCommission: number;
    totalCommission: number; activeAgents: number; inactiveAgents: number; totalTransactions: number;
  };
  agents: AgentPerfRow[];
  rankings: Record<'topDeposit'|'topWithdrawal'|'topSettlement'|'topCommission', Array<{ agentId: string; agentName: string; value: number }>>;
  highest: Record<'deposit'|'withdrawal'|'settlement'|'commission', null | { agentId: string; agentName: string; value: number }>;
}
export interface AgentTxnCommission {
  agentId?: string | null; agentName?: string | null; membershipId: string;
  amount: number; commissionPct: number; commissionAmount: number; netAmount: number;
  balanceBefore: number; balanceAfter: number;
}

export interface AgentProfile {
  agent: { agentId: string; agentName: string; category: string; country?: string | null; state?: string | null;
    location?: string | null; currency?: string | null; status: string; createdDate?: string | null };
  totals: {
    totalBusiness: number;
    depositCount: number; totalDeposits: number; depositCommission: number;
    withdrawalCount: number; totalWithdrawals: number; withdrawalCommission: number;
    settlementCount: number; totalSettlements: number; settlementCommission: number;
    commissionEarned: number; totalTransactions: number;
  };
  members: Array<{ membershipId: string; memberName?: string | null; deposits: number; withdrawals: number; settlements: number; count: number }>;
  activity: AgentTxnRow[];
}

export const agentTxnsAPI = {
  overview: async () => (await api.get<AgentOverview>('/api/agent-txns/overview')).data,
  // Reference data (agents, members, dropdown options) — five screens fetch this on mount and
  // again on their polls. Short-TTL cached and de-duplicated; call invalidateRef('agent:') after
  // anything that changes the agent master list.
  formData: async () => cachedRef('agent:form-data',
    async () => (await api.get<AgentFormData>('/api/agent-txns/form-data')).data),
  member: async (id: string) => (await api.get<AgentMemberLookup>(`/api/agent-txns/member/${encodeURIComponent(id)}`)).data,
  /** Read-only financial summary for a Membership ID (Balance Enquiry). */
  balanceEnquiry: async (id: string) => (await api.get<AgentMemberSummary>(`/api/agent-txns/balance-enquiry/${encodeURIComponent(id)}`)).data,
  performance: async () => (await api.get<AgentPerformance>('/api/agent-txns/performance')).data,
  agentProfile: async (agentMasterId: number) => (await api.get<AgentProfile>(`/api/agent-txns/agent/${agentMasterId}/profile`)).data,
  txnCommission: async (id: number) => (await api.get<AgentTxnCommission>(`/api/agent-txns/${id}/commission`)).data,
  createDeposit: async (body: AgentDepositBody) => (await api.post<AgentTxnRow>('/api/agent-txns/deposit', body)).data,
  createWithdrawal: async (body: AgentWithdrawalBody) => (await api.post<AgentTxnRow>('/api/agent-txns/withdrawal', body)).data,
  /** Settlement — Supervisor-only and approval-free; created ready for them to pay. */
  createSettlement: async (body: AgentWithdrawalBody) => (await api.post<AgentTxnRow>('/api/agent-txns/settlement', body)).data,
  list: async (params?: AgentTxnQuery) => (await api.get<AgentTxnRow[]>('/api/agent-txns', { params })).data,
  /**
   * Server-side paginated + filtered feed. Same ordering (newest first), same search fields and
   * the same commission enrichment as `list`, but only one page crosses the wire and the count
   * comes from Postgres. Prefer this for any listing screen.
   */
  listPaged: async (params?: AgentTxnPagedQuery) =>
    (await api.get<Paged<AgentTxnRow>>('/api/agent-txns/paged', { params })).data,
  manage: async (id: number, body: AgentManageBody) => (await api.patch<AgentTxnRow>(`/api/agent-txns/${id}/manage`, body)).data,
  /** Split an initialised Cash Deposit among members — parent becomes a container, children credit. */
  distribute: async (id: number, body: AgentDistributeBody) =>
    (await api.post<AgentDistributeResult>(`/api/agent-txns/${id}/distribute`, body)).data,

  // ── Deposit chain (mirrors the merchant deposit workflow) ──
  /** Active AGENT accounts for one agent — the only source the Account Submission step may use. */
  agentAccounts: async (agentMasterId: number) =>
    (await api.get<AgentAccountOption[]>(`/api/agent-txns/agent-accounts/${agentMasterId}`)).data,
  accountSubmit: async (id: number, body: { agentAccountId?: number; tokenDetails?: string; noteNumber?: string; walletAddress?: string; accountProof?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/account-submit`, body)).data,
  /** Both are mandatory — the UTR is the only payment reference (no Reference Number).
   *  `approverUserId` is the "Send To Approval" Authorized Approver, now chosen at this step. */
  submitSlip: async (id: number, body: { slipImage?: string; utr?: string; approverUserId?: number }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/slip`, body)).data,
  supervisorApprove: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/supervisor/approve`, { remark })).data,
  supervisorReject: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/supervisor/reject`, { remark })).data,
  /** Confirmation only — the slip and UTR were captured at the slip step and are reused as-is. */
  markDeposit: async (id: number, body: Record<string, never> = {}) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/mark-deposit`, body)).data,

  // ── Withdrawal chain (mirrors the merchant withdrawal workflow) ──
  memberAccounts: async (membershipId: string) =>
    (await api.get<AgentMemberAccount[]>(`/api/agent-txns/member-accounts/${encodeURIComponent(membershipId)}`)).data,
  managerApprove: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/manager/approve`, { remark })).data,
  managerReject: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/manager/reject`, { remark })).data,
  /** Submit Payment Details (creator, after approval) — method-specific, completes the withdrawal. */
  /** Submit Payment Details — saves the proof + payment information ONLY. The status is left
   *  exactly where the approval workflow put it; `completeWithdrawal` is the explicit step that
   *  completes the transaction. */
  payout: async (id: number, body: { noteNumber?: string; tokenDetails?: string; walletAddress?: string; txHash?: string; slipImage?: string; utr?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/payout`, body)).data,
  /** Complete an approved withdrawal whose payment details are already on the record. */
  completeWithdrawal: async (id: number) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/complete`, {})).data,
  /** What the selected agent currently holds + the most a member may withdraw from it. */
  agentBalance: async (agentMasterId: number) =>
    (await api.get<AgentBalance>(`/api/agent-txns/agent/${agentMasterId}/balance`)).data,
  // ── Settlement chain: Requested → Accepted → Proof Uploaded → Settled (payment is offline) ──
  settlementAccept: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/settlement/accept`, { remark })).data,
  settlementReject: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/settlement/reject`, { remark })).data,
  /** Proof of the completed offline payment — mandatory before a settlement can be settled. */
  settlementProof: async (id: number, body: { slipImage?: string; utr?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/settlement/proof`, body)).data,
  settlementSettle: async (id: number) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/settlement/settle`)).data,
  approve: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/approve`)).data,
  reject: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/reject`)).data,
  audit: async (id: number) => (await api.get<AgentTxnAuditRow[]>(`/api/agent-txns/${id}/audit`)).data,
};

/** Human-readable message from an agent-txn API error. */
export const agentTxnError = (err: unknown, fallback: string): string => {
  const e = err as { response?: { data?: { detail?: string } } };
  return e?.response?.data?.detail || fallback;
};
