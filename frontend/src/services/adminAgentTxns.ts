// Admin Portal → Agent Management — READ-ONLY client for /api/admin/agent-txns.
//
// Every call here is a GET. There is deliberately no create / approve / reject / upload / complete
// / edit / cancel method on this client, and none exists on the server either: the Admin router is
// GET-only, and the Agent Module's write endpoints admit MERCHANT operator roles only, so an Admin
// token is refused by them with 403. The Admin Portal monitors the agent ledger; it never acts on
// it.
//
// The row and overview shapes are the SAME ones the Merchant Agent module uses (imported from
// ./agentTxns), so the two portals can never render a transaction differently.
import api, { type Paged } from './api';
import type { AgentOverview, AgentTxnRow, AgentTxnAuditRow, AgentPerformance, AgentPerfRow } from './agentTxns';

/** An agent transaction as the Admin sees it — the merchant row plus its owning business. */
export interface AdminAgentTxnRow extends AgentTxnRow {
  merchantBusiness?: string | null;
}

/** One merchant's Agent Module activity, for the Admin's per-merchant breakdown. */
export interface AdminAgentMerchantRow {
  business: string;
  total: number;
  deposits: number;
  withdrawals: number;
  settlements: number;
  completed: number;
  rejected: number;
  pending: number;
  completedAmount: number;
}

/** The Merchant Agent Dashboard's own payload, widened to every business, plus what only an
 *  Admin monitoring all agents needs: the agent inventory and the per-merchant breakdown. */
export interface AdminAgentOverview extends AgentOverview {
  /** The latest activity, each row naming the merchant it belongs to. */
  recent: AdminAgentTxnRow[];
  performance: AgentPerformance;
  agents: {
    total: number;
    active: number;
    inactive: number;
    byCategory: Record<string, number>;
    merchants: number;
  };
  byMerchant: AdminAgentMerchantRow[];
  /** The business this payload was scoped to, or null for every business. */
  scope: string | null;
}

/** An agent's lifetime performance, plus the fields the Admin view adds. */
export interface AdminAgentRow extends AgentPerfRow {
  merchantBusiness?: string | null;
  state?: string | null;
  location?: string | null;
  payInFee: number;
  payOutFee: number;
  settlementFee: number;
}

export interface AdminAgentQuery {
  business?: string;
  status?: string;
  txn_type?: string;
  txn_method?: string;
  search?: string;
  date?: string;
  date_from?: string;
  date_to?: string;
  page?: number;
  page_size?: number;
}

export const adminAgentAPI = {
  /** Platform-wide (or single-business) Agent Module overview. */
  overview: async (business?: string) =>
    (await api.get<AdminAgentOverview>('/api/admin/agent-txns/overview',
      { params: business ? { business } : undefined })).data,
  /** Merchant businesses that have Agent Module activity — the business filter's options. */
  businesses: async () => (await api.get<string[]>('/api/admin/agent-txns/businesses')).data,
  /** Every agent with its lifetime performance. */
  agents: async (business?: string) =>
    (await api.get<{ overall: AdminAgentOverview['performance']['overall']; agents: AdminAgentRow[];
      rankings: AdminAgentOverview['performance']['rankings'] }>('/api/admin/agent-txns/agents',
      { params: business ? { business } : undefined })).data,
  /** Server-side paginated + filtered agent transaction feed, across every merchant. */
  listPaged: async (params?: AdminAgentQuery) =>
    (await api.get<Paged<AdminAgentTxnRow>>('/api/admin/agent-txns/paged', { params })).data,
  /** One transaction's audit trail — who performed each action, in which role, and when. */
  audit: async (id: number) =>
    (await api.get<AgentTxnAuditRow[]>(`/api/admin/agent-txns/${id}/audit`)).data,
};
