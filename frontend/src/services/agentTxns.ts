// Isolated Agent Transaction subsystem — client for /api/agent-txns.
// This module NEVER calls any merchant Deposit/Withdrawal/Settlement/Treasury/Risk/Account/
// Transaction-History endpoint. Every figure it returns comes only from the agent ledger.
import api from './api';

/**
 * Agent deposit workflow — the same labels/order as the merchant deposit workflow, except the
 * Data Operator performs the steps the Admin performs for a merchant:
 *   ACCOUNT_REQUESTED → ACCOUNT_SUBMITTED → SUPERVISOR_REVIEW → SLIP_SUBMITTED → DEPOSITED
 * PENDING / APPROVED are legacy rows created before the chain existed.
 */
export type AgentTxnStatus =
  | 'ACCOUNT_REQUESTED' | 'ACCOUNT_SUBMITTED' | 'SUPERVISOR_REVIEW' | 'MANAGER_REVIEW'
  | 'SLIP_SUBMITTED' | 'DEPOSITED' | 'COMPLETED' | 'REJECTED' | 'PENDING' | 'APPROVED';

/** Statuses that mean the money actually moved — the completed-only basis (mirrors the server). */
export const AGENT_COMPLETED_STATUSES: AgentTxnStatus[] = ['APPROVED', 'DEPOSITED', 'COMPLETED'];
export const AGENT_FINAL_STATUSES: AgentTxnStatus[] = [...AGENT_COMPLETED_STATUSES, 'REJECTED'];

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
  tokenDetails: string;
  noteNumber: string;
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
  accountSubmittedBy?: string | null;
  accountSubmittedDate?: string | null;
  accountSubmittedTime?: string | null;
  // Slip.
  slipImage?: string | null;
  slipRef?: string | null;
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
  linkedDepositId?: number | null;
  createdBy?: string | null;
  createdDate?: string | null;
  createdTime?: string | null;
  updatedDate?: string | null;
  updatedTime?: string | null;
}

export interface AgentOverview {
  cards: {
    totalTransactions: number;
    depositCount: number; depositAmount: number;
    withdrawalCount: number; withdrawalAmount: number;
    pending: number; approved: number; rejected: number;
    approvedDeposits: number; approvedWithdrawals: number;
    grossAmount: number; depositCommission: number; withdrawalCommission: number;
    netAmount: number; totalCommission: number;
  };
  byAgent: Array<{ agentCode: string | null; agentName: string | null; deposits: number; withdrawals: number; count: number }>;
  trend: Array<{ date: string; deposits: number; withdrawals: number }>;
  recent: AgentTxnRow[];
}

export interface AgentFormAgent {
  id: number; agentId: string; name: string; country: string; state: string;
  location: string; category: string; transactionCode: string; currency: string;
}
export interface AgentApprover { id: number; name: string; role: string }
export interface AgentFormData {
  agents: AgentFormAgent[];
  approvers: AgentApprover[];
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
  notes?: string;
  instructions?: string;
  sentForApproval: boolean;
  approverUserId?: number | null;
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

export interface AgentTxnAuditRow {
  id: number; action: string; oldAmount: number | null; newAmount: number | null;
  note: string | null; approverName: string | null; actor: string | null; role: string | null;
  createdDate: string | null; createdTime: string | null;
}

export interface AgentTxnQuery {
  status?: string; txn_type?: string; search?: string; date?: string; date_from?: string; date_to?: string;
}

export const agentTxnsAPI = {
  overview: async () => (await api.get<AgentOverview>('/api/agent-txns/overview')).data,
  formData: async () => (await api.get<AgentFormData>('/api/agent-txns/form-data')).data,
  member: async (id: string) => (await api.get<AgentMemberLookup>(`/api/agent-txns/member/${encodeURIComponent(id)}`)).data,
  createDeposit: async (body: AgentDepositBody) => (await api.post<AgentTxnRow>('/api/agent-txns/deposit', body)).data,
  createWithdrawal: async (body: AgentWithdrawalBody) => (await api.post<AgentTxnRow>('/api/agent-txns/withdrawal', body)).data,
  /** Settlement — Supervisor-only and approval-free; created ready for them to pay. */
  createSettlement: async (body: AgentWithdrawalBody) => (await api.post<AgentTxnRow>('/api/agent-txns/settlement', body)).data,
  list: async (params?: AgentTxnQuery) => (await api.get<AgentTxnRow[]>('/api/agent-txns', { params })).data,
  manage: async (id: number, body: AgentManageBody) => (await api.patch<AgentTxnRow>(`/api/agent-txns/${id}/manage`, body)).data,

  // ── Deposit chain (mirrors the merchant deposit workflow) ──
  /** Active AGENT accounts for one agent — the only source the Account Submission step may use. */
  agentAccounts: async (agentMasterId: number) =>
    (await api.get<AgentAccountOption[]>(`/api/agent-txns/agent-accounts/${agentMasterId}`)).data,
  accountSubmit: async (id: number, agentAccountId: number) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/account-submit`, { agentAccountId })).data,
  submitSlip: async (id: number, body: { slipImage?: string; slipRef?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/slip`, body)).data,
  supervisorApprove: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/supervisor/approve`, { remark })).data,
  supervisorReject: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/supervisor/reject`, { remark })).data,
  markDeposit: async (id: number, body: { utr?: string; proof?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/mark-deposit`, body)).data,

  // ── Withdrawal chain (mirrors the merchant withdrawal workflow) ──
  memberAccounts: async (membershipId: string) =>
    (await api.get<AgentMemberAccount[]>(`/api/agent-txns/member-accounts/${encodeURIComponent(membershipId)}`)).data,
  managerApprove: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/manager/approve`, { remark })).data,
  managerReject: async (id: number, remark: string) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/manager/reject`, { remark })).data,
  payout: async (id: number, body: { slipImage?: string; slipRef?: string }) =>
    (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/payout`, body)).data,
  approve: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/approve`)).data,
  reject: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/reject`)).data,
  audit: async (id: number) => (await api.get<AgentTxnAuditRow[]>(`/api/agent-txns/${id}/audit`)).data,
};

/** Human-readable message from an agent-txn API error. */
export const agentTxnError = (err: unknown, fallback: string): string => {
  const e = err as { response?: { data?: { detail?: string } } };
  return e?.response?.data?.detail || fallback;
};
