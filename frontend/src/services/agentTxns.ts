// Isolated Agent Transaction subsystem — client for /api/agent-txns.
// This module NEVER calls any merchant Deposit/Withdrawal/Settlement/Treasury/Risk/Account/
// Transaction-History endpoint. Every figure it returns comes only from the agent ledger.
import api from './api';

export interface AgentTxnRow {
  id: number;
  referenceNumber: string;
  transactionCode: string;
  type: 'DEPOSIT' | 'WITHDRAWAL';
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
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
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
}

export interface AgentMemberLookup {
  membershipId: string;
  membershipName?: string | null;
  latestDeposit: null | {
    agentMasterId: number; agentCode?: string | null; agentName?: string | null;
    country?: string | null; state?: string | null; location?: string | null;
    category?: string | null; depositId: number; reference: string;
  };
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
}
export interface AgentWithdrawalBody extends AgentDepositBody {
  linkedDepositId?: number | null;
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
  list: async (params?: AgentTxnQuery) => (await api.get<AgentTxnRow[]>('/api/agent-txns', { params })).data,
  manage: async (id: number, body: AgentManageBody) => (await api.patch<AgentTxnRow>(`/api/agent-txns/${id}/manage`, body)).data,
  approve: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/approve`)).data,
  reject: async (id: number) => (await api.post<AgentTxnRow>(`/api/agent-txns/${id}/reject`)).data,
  audit: async (id: number) => (await api.get<AgentTxnAuditRow[]>(`/api/agent-txns/${id}/audit`)).data,
};

/** Human-readable message from an agent-txn API error. */
export const agentTxnError = (err: unknown, fallback: string): string => {
  const e = err as { response?: { data?: { detail?: string } } };
  return e?.response?.data?.detail || fallback;
};
