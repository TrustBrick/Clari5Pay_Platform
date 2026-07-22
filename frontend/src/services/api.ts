import axios from 'axios';
import { cachedRef, invalidateRef } from '../utils/refCache';
import type { Account, AccountBalance, AccountUsers, ActiveUsersData, AdminUpi, Agent, AgentAccount, AgentAssignmentCurrent, AgentAssignmentResult, AgentAuditRow, AgentAssignmentHistoryRow, AgentDashboard, AgentTxRow, AssignableMerchant, AuditLogEntry, BalanceSummary, BlogAnalytics, BlogCategory, BlogPost, BlogStats, GlobalStatusCounts, GlobalSummary, LoginRequest, LoginResponse, MerchantBalance, MerchantStats, MerchantBankAccount, Notification, NewsPost, OtpChallenge, ReportData, ReportRow, RiskOverview, RiskProfile, RiskMemberBanks, Complaint, ComplaintList, SupportMembersData, SupportMemberRow, SupportConversationRow, SupportMessage, SystemLogEntry, Transaction, User } from '../types';

// Empty string is a valid value meaning "same origin" (production behind nginx),
// so use ?? — only fall back to the dev default when the var is truly unset.
export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

const api = axios.create({ baseURL: BASE_URL });

/** Set/clear the auth token on the client synchronously (used at login/logout). */
export const setAuthToken = (token: string | null) => {
  if (token) api.defaults.headers.common.Authorization = `Bearer ${token}`;
  else delete api.defaults.headers.common.Authorization;
  // The reference-data cache is per-session: what a user may see depends on their role and
  // business, so it must never survive a session change into the next login.
  invalidateRef();
};

// Initialise the header from storage on page load (so a refresh is authenticated immediately).
const _storedToken = localStorage.getItem('clari5pay_token');
if (_storedToken) api.defaults.headers.common.Authorization = `Bearer ${_storedToken}`;

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('clari5pay_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (res) => res,
  (err) => {
    // A 401 on an authenticated request means the session expired → clear it and bounce
    // to the login screen. But the pre-session auth endpoints (login, verify-otp,
    // forgot-password, …) legitimately return 401/403 for bad input; those must surface
    // as inline errors on the login page, NOT trigger a full-page redirect (which would
    // reload the page and swallow the error message). So skip the redirect for them.
    const url: string = err.config?.url || '';
    const isAuthEndpoint = url.includes('/api/auth/');
    if (err.response?.status === 401 && !isAuthEndpoint) {
      localStorage.removeItem('clari5pay_token');
      localStorage.removeItem('clari5pay_user');
      invalidateRef();
      window.location.href = '/';
    }
    return Promise.reject(err);
  }
);

export const authAPI = {
  // Step 1: validate credentials → returns an OTP challenge (OTP on) OR a token directly (OTP off).
  login: async (data: LoginRequest): Promise<OtpChallenge | LoginResponse> => {
    const form = new URLSearchParams();
    form.append('username', data.username);
    form.append('password', data.password);
    const res = await api.post<OtpChallenge | LoginResponse>('/api/auth/login', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return res.data;
  },
  otpStatus: async (): Promise<{ enabled: boolean }> => {
    const res = await api.get<{ enabled: boolean }>('/api/auth/otp-status');
    return res.data;
  },
  setOtpEnabled: async (enabled: boolean): Promise<{ enabled: boolean }> => {
    const res = await api.post<{ enabled: boolean }>('/api/auth/otp-config', { enabled });
    return res.data;
  },
  // Step 2: verify the OTP → returns the access token + user.
  verifyOtp: async (otpToken: string, code: string): Promise<LoginResponse> => {
    const res = await api.post<LoginResponse>('/api/auth/verify-otp', { otpToken, code });
    return res.data;
  },
  resendOtp: async (otpToken: string): Promise<OtpChallenge> => {
    const res = await api.post<OtpChallenge>('/api/auth/resend-otp', { otpToken });
    return res.data;
  },
  // ── Forgot Password (username → Email OTP) ──
  forgotPassword: async (username: string): Promise<{ resetToken: string; email: string; devOtp?: string }> => {
    const res = await api.post('/api/auth/forgot-password', { username });
    return res.data;
  },
  verifyResetOtp: async (resetToken: string, code: string): Promise<{ confirmedToken: string }> => {
    const res = await api.post('/api/auth/verify-reset-otp', { resetToken, code });
    return res.data;
  },
  resetPassword: async (confirmedToken: string, newPassword: string): Promise<{ message: string }> => {
    const res = await api.post('/api/auth/reset-password', { confirmedToken, newPassword });
    return res.data;
  },
  me: async (): Promise<User> => {
    const res = await api.get<User>('/api/auth/me');
    return res.data;
  },
  // Best-effort server-side logout (audit trail). Never throws — logout must not be
  // blocked by a backend hiccup; the client clears its own session regardless.
  // The token is passed explicitly so the call still carries auth even when the
  // caller clears localStorage immediately afterwards.
  logout: async (token?: string): Promise<void> => {
    try {
      await api.post('/api/auth/logout', null,
        token ? { headers: { Authorization: `Bearer ${token}` } } : undefined);
    } catch { /* best-effort */ }
  },
};

// Server-side transaction search/filter params. Keys match the backend query
// params 1:1. Date inputs are IST (YYYY-MM-DD); datetime inputs are IST
// (YYYY-MM-DDTHH:MM). Empty/blank values are stripped so blank inputs aren't sent.
export interface TxQuery {
  search?: string;       // matches reference OR Membership ID (legacy combined search)
  ref?: string;          // partial transaction reference number
  member_id?: string;    // partial Membership ID
  date_from?: string;
  date_to?: string;
  datetime_from?: string;
  datetime_to?: string;
  limit?: number;        // optional server-side pagination
  offset?: number;
}
const cleanTxParams = (p?: TxQuery): Record<string, string> | undefined => {
  if (!p) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) {
    if (v != null && String(v).trim() !== '') out[k] = String(v);
  }
  return Object.keys(out).length ? out : undefined;
};

// Generic query-param cleaner (same blank-stripping as cleanTxParams) for the
// server-side paginated endpoints, whose params are a superset of TxQuery.
const cleanPagedParams = <T extends object>(p?: T): Record<string, string> | undefined => {
  if (!p) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) {
    if (v != null && String(v).trim() !== '') out[k] = String(v);
  }
  return Object.keys(out).length ? out : undefined;
};

// The server-side paginated envelope every /paged endpoint returns. Search / filter /
// sort / count all execute in the database; the browser only ever holds one page.
export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/**
 * Walk a paginated endpoint to completion.
 *
 * Used ONLY by exports and by aggregate views that must cover the whole filtered result set now
 * that the tables themselves fetch a single page — never for rendering a table. Reads the largest
 * allowed page (100) and stops on the server's own totalPages, so it cannot spin.
 */
export const fetchAllPages = async <T,>(fetchPage: (page: number) => Promise<Paged<T>>): Promise<T[]> => {
  const first = await fetchPage(1);
  const out = [...first.items];
  for (let p = 2; p <= first.totalPages; p++) {
    const next = await fetchPage(p);
    if (!next.items.length) break;
    out.push(...next.items);
  }
  return out;
};

// Superset of TxQuery accepted by the /paged endpoints (all filtering is server-side).
export interface TxPagedQuery extends TxQuery {
  status?: string;                 // one or comma-separated statuses (backend matches enum)
  type?: string;                   // DEPOSIT / WITHDRAWAL / SETTLEMENT group, or exact type, or ALL
  amount_min?: number;
  amount_max?: number;
  page?: number;                   // 1-based
  page_size?: number;              // 10 | 25 | 50 | 100 (backend clamps to these)
}

// One Membership-ID group in the Merchant management pages (all aggregates DB-computed).
export interface MemberGroup {
  membershipId: string;
  memberName?: string | null;
  depositRequests: number;
  withdrawalRequests: number;
  settlementRequests: number;
  requests: number;                // count within the active type filter (drives "Total {noun} Requests")
  totalAmount: number;             // sum within the active type filter
  latestStatus?: string | null;
  latestType?: string | null;
  latestDate?: string | null;
  latestTime?: string | null;
  latestCreatedAt?: string | null;
}

export interface MemberGroupQuery {
  type?: string;                   // DEPOSIT / WITHDRAWAL / SETTLEMENT
  member?: string;                 // member group key (drill-down only)
  search?: string;
  date_from?: string;
  date_to?: string;
  datetime_from?: string;
  datetime_to?: string;
  page?: number;
  page_size?: number;
}

export const transactionAPI = {
  getAll: async (params?: TxQuery) => {
    const res = await api.get<Transaction[]>('/api/transactions', { params: cleanTxParams(params) });
    return res.data;
  },
  getMine: async (params?: TxQuery) => {
    const res = await api.get<Transaction[]>('/api/transactions/mine', { params: cleanTxParams(params) });
    return res.data;
  },
  // Read-only, system-wide feed for oversight merchant roles (Supervisor / Manager).
  // Returns every transaction (all types) newest-first; the backend enforces access.
  getAllOverseer: async (params?: TxQuery) => {
    const res = await api.get<Transaction[]>('/api/transactions/all', { params: cleanTxParams(params) });
    return res.data;
  },
  // ── Server-side paginated feeds (additive; the bare-array getters above stay until every
  // caller is migrated). Return {items,total,page,pageSize,totalPages}; default 10 per page.
  getMinePaged: async (params?: TxPagedQuery) => {
    const res = await api.get<Paged<Transaction>>('/api/transactions/mine/paged', { params: cleanPagedParams(params) });
    return res.data;
  },
  getAllPaged: async (params?: TxPagedQuery) => {
    const res = await api.get<Paged<Transaction>>('/api/transactions/paged', { params: cleanPagedParams(params) });
    return res.data;
  },
  getAllOverseerPaged: async (params?: TxPagedQuery) => {
    const res = await api.get<Paged<Transaction>>('/api/transactions/all/paged', { params: cleanPagedParams(params) });
    return res.data;
  },
  // Merchant management pages: paginated Membership-ID groups (aggregates computed in the DB).
  memberGroups: async (params?: MemberGroupQuery) => {
    const res = await api.get<Paged<MemberGroup>>('/api/transactions/mine/members', { params: cleanPagedParams(params) });
    return res.data;
  },
  // Drill-down: one member group's own transactions, server-paginated.
  memberTransactions: async (params?: MemberGroupQuery) => {
    const res = await api.get<Paged<Transaction>>('/api/transactions/mine/member-transactions', { params: cleanPagedParams(params) });
    return res.data;
  },
  // Full single transaction incl. proof/receipt images (lists omit those for speed).
  getDetail: async (id: string) => {
    const res = await api.get<Transaction>(`/api/transactions/${id}/detail`);
    return res.data;
  },
  summary: async () => {
    const res = await api.get<BalanceSummary>('/api/transactions/summary');
    return res.data;
  },
  memberProfile: async (memberId: string) => {
    const res = await api.get<{ memberName?: string|null; upiId?: string|null; accountHolder?: string|null; accountNumber?: string|null; ifsc?: string|null; branch?: string|null; bankName?: string|null }>(`/api/transactions/member-profile/${encodeURIComponent(memberId)}`);
    return res.data;
  },
  // "Send To Approval": the approval roles of the caller's business, for the Authorized Approver
  // selector. Scoped to the request type — a Deposit takes a Supervisor or a Manager, a Withdrawal
  // a Manager only. 404s when the feature is switched off.
  approvers: async (txnType: 'DEPOSIT' | 'WITHDRAWAL' = 'DEPOSIT') => cachedRef(`ref:approvers:${txnType}`, async () => {
    const res = await api.get<{ id: number; name: string; role: string }[]>('/api/transactions/approvers', { params: { txnType } });
    return res.data;
  }),
  createDeposit: async (data: Record<string, unknown>) => {
    const res = await api.post<Transaction>('/api/transactions/deposit', data);
    return res.data;
  },
  createWithdrawal: async (data: Record<string, unknown>) => {
    const res = await api.post<Transaction>('/api/transactions/withdrawal', data);
    return res.data;
  },
  createSettlement: async (data: Record<string, unknown>) => {
    const res = await api.post<Transaction>('/api/transactions/settlement', data);
    return res.data;
  },
  approve: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/approve`);
    return res.data;
  },
  complete: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/complete`);
    return res.data;
  },
  saReject: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/sa-reject`);
    return res.data;
  },
  reject: async (id: string, reason: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/reject`, { reason });
    return res.data;
  },
  submitAccount: async (
    id: string,
    data: { adminRef?: string; adminProof?: string; adminBankDetails?: string; adminBankImage?: string; adminUpiId?: string },
  ) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/account-submit`, data);
    return res.data;
  },
  submitSlip: async (id: string, data: { merchantProof?: string; merchantProofs?: string[]; merchantRef?: string; approverUserId?: number }) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/slip`, data);
    return res.data;
  },
  markDone: async (id: string, data?: { adminProof?: string; adminUtr?: string }) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/done`, data ?? {});
    return res.data;
  },
  // Supervisor (deposit) review-gate actions — remarks are mandatory.
  supervisorReview: async (id: string, decision: 'approve' | 'reject' | 'resubmit', remark: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/supervisor/${decision}`, { remark });
    return res.data;
  },
  // Manager (withdrawal/settlement) review-gate actions — remarks are mandatory.
  managerReview: async (id: string, decision: 'approve' | 'reject' | 'resubmit', remark: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/manager/${decision}`, { remark });
    return res.data;
  },
  // Supervisor completes an AGENT-ASSIGNED settlement (demo) with the mandatory UTR + proof —
  // no Admin needed. Non-agent settlements are rejected server-side (they still go to the Admin).
  // `utr` is omitted for a CASH settlement — there is no bank reference to record, so the
  // settlement proof is the only evidence (the backend applies the same rule).
  supervisorSettle: async (id: string, data: { remark: string; utr?: string; proof: string }) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/supervisor/settle`, data);
    return res.data;
  },
  // Record a "<role> Viewed" audit entry when a reviewer/admin opens a request's details.
  recordView: async (id: string) => {
    try { await api.post(`/api/transactions/${id}/view`); } catch { /* best-effort audit */ }
  },
  // Read-only audit history for a single transaction (owner merchant / Supervisor / Manager / admin).
  getAudit: async (id: string) => {
    const res = await api.get<AuditLogEntry[]>(`/api/transactions/${id}/audit`);
    return res.data;
  },
  cancel: async (id: string, reason: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/cancel`, { reason });
    return res.data;
  },
  regenerateQr: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/regenerate-qr`);
    return res.data;
  },
  merchantBalances: async () => {
    const res = await api.get<MerchantBalance[]>('/api/transactions/merchant-balances');
    return res.data;
  },
  merchantStats: async () => {
    const res = await api.get<MerchantStats[]>('/api/transactions/merchant-stats');
    return res.data;
  },
  // Platform-wide financial summary — single source of truth, identical for every admin.
  globalSummary: async () => {
    const res = await api.get<GlobalSummary>('/api/transactions/global-summary');
    return res.data;
  },
  /**
   * Platform-wide per-type × status COUNTS from a single GROUP BY. Lets the Admin / Super Admin
   * dashboards render their tiles and status charts without pulling the transaction table.
   */
  globalStatusCounts: async () => {
    const res = await api.get<GlobalStatusCounts>('/api/transactions/status-counts');
    return res.data;
  },
  recheck: async (id: string, reason?: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/recheck`, { reason });
    return res.data;
  },
  flagRisk: async (id: string, reason?: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/flag-risk`, { reason });
    return res.data;
  },
  reports: async () => {
    const res = await api.get<ReportData>('/api/transactions/reports');
    return res.data;
  },
  // Admin Reports — same payload as merchant reports but system-wide. Pass a business name
  // to scope to one merchant; omit it for the consolidated all-merchants view.
  adminReports: async (merchant?: string) => {
    const res = await api.get<ReportData>('/api/transactions/admin-reports', {
      params: merchant ? { merchant } : undefined,
    });
    return res.data;
  },
};

export const riskAPI = {
  members: async () => {
    const res = await api.get<RiskOverview>('/api/risk/members');
    return res.data;
  },
  member: async (memberId: string) => {
    const res = await api.get<RiskProfile>(`/api/risk/member/${encodeURIComponent(memberId)}`);
    return res.data;
  },
  memberBanks: async (memberId: string) => {
    const res = await api.get<RiskMemberBanks>(`/api/risk/member/${encodeURIComponent(memberId)}/banks`);
    return res.data;
  },
  createComplaint: async (payload: Record<string, unknown>) => {
    const res = await api.post<Complaint>('/api/risk/complaints', payload);
    return res.data;
  },
  complaints: async (params?: { status?: string; priority?: string; q?: string }) => {
    const res = await api.get<ComplaintList>('/api/risk/complaints', { params });
    return res.data;
  },
  complaint: async (id: number) => {
    const res = await api.get<Complaint>(`/api/risk/complaints/${id}`);
    return res.data;
  },
  updateComplaint: async (id: number, patch: Record<string, unknown>) => {
    const res = await api.patch<Complaint>(`/api/risk/complaints/${id}`, patch);
    return res.data;
  },
};

export const accountAPI = {
  list: async (q?: string) => {
    const res = await api.get<Account[]>('/api/accounts', { params: q ? { q } : undefined });
    return res.data;
  },
  get: async (ref: string) => {
    const res = await api.get<Account>(`/api/accounts/${ref}`);
    return res.data;
  },
  balances: async () => {
    const res = await api.get<AccountBalance[]>('/api/accounts/balances');
    return res.data;
  },
  // Bank-statement ledger rows for a single account (deposits via admin_ref; withdrawals/
  // settlements via the member→account map). Shaped as ReportRow[] so the shared Agent
  // Ledger renderer computes Opening/Running/Closing balance — no duplicated balance logic.
  statement: async (ref: string) => {
    const res = await api.get<{ referenceNumber: string; accountName: string; transactions: ReportRow[] }>(`/api/accounts/${ref}/statement`);
    return res.data;
  },
  lastForMember: async (memberId: string) => {
    const res = await api.get<{ referenceNumber: string | null }>(`/api/accounts/for-member/${encodeURIComponent(memberId)}`);
    return res.data;
  },
  // Users (merchant operators) who deposited into an account, each with their Players
  // (Membership / Player IDs). Powers the Account → User → Player drill-down popup.
  users: async (ref: string) => {
    const res = await api.get<AccountUsers>(`/api/accounts/${ref}/users`);
    return res.data;
  },
  create: async (data: Record<string, unknown>) => {
    const res = await api.post<Account>('/api/accounts', data);
    return res.data;
  },
  toggle: async (ref: string, reason: string) => {
    const res = await api.patch<Account>(`/api/accounts/${ref}/toggle`, { reason });
    return res.data;
  },
};

export const adminUpiAPI = {
  list: async () => {
    const res = await api.get<AdminUpi[]>('/api/admin-upis');
    return res.data;
  },
  listActive: async () => {
    const res = await api.get<AdminUpi[]>('/api/admin-upis/active');
    return res.data;
  },
  create: async (data: { label?: string; upiId: string; accountRef?: string }) => {
    const res = await api.post<AdminUpi>('/api/admin-upis', data);
    return res.data;
  },
  link: async (id: number, accountRef: string | null) => {
    const res = await api.patch<AdminUpi>(`/api/admin-upis/${id}/account`, { accountRef });
    return res.data;
  },
  toggle: async (id: number, reason?: string) => {
    const res = await api.patch<AdminUpi>(`/api/admin-upis/${id}/toggle`, { reason });
    return res.data;
  },
};

export const bankAccountAPI = {
  // Scoped to a Member ID — each member only sees its own saved accounts.
  listMine: async (memberId?: string) => {
    const res = await api.get<MerchantBankAccount[]>('/api/merchant-bank-accounts', { params: memberId ? { memberId } : undefined });
    return res.data;
  },
  add: async (data: { accountHolder: string; accountNumber: string; ifsc: string; branch: string; bankName?: string; memberId?: string }) => {
    const res = await api.post<MerchantBankAccount>('/api/merchant-bank-accounts', data);
    return res.data;
  },
  addUpi: async (memberId: string, upiId: string) => {
    const res = await api.post<MerchantBankAccount>('/api/merchant-bank-accounts/upi', { memberId, upiId });
    return res.data;
  },
  setDefaultUpi: async (id: number) => {
    const res = await api.patch<MerchantBankAccount>(`/api/merchant-bank-accounts/${id}/default`);
    return res.data;
  },
};

export const supportAPI = {
  conversations: async () => {
    const res = await api.get('/api/support/conversations');
    return res.data;
  },
  messages: async (merchantId: number) => {
    const res = await api.get<SupportMessage[]>(`/api/support/messages/${merchantId}`);
    return res.data;
  },
  myMessages: async () => {
    const res = await api.get<SupportMessage[]>('/api/support/my-messages');
    return res.data;
  },
  myConversation: async () => {
    const res = await api.get<{ status: string; queued: boolean; agentName: string | null }>('/api/support/my-conversation');
    return res.data;
  },
  send: async (content: string, merchantId?: number, attachment?: { dataUrl: string; name: string } | null) => {
    const res = await api.post<SupportMessage>('/api/support/messages', {
      content, merchant_id: merchantId,
      attachment: attachment?.dataUrl, attachment_name: attachment?.name,
    });
    return res.data;
  },
  merchant: async (merchantId: number) => {
    const res = await api.get(`/api/support/merchant/${merchantId}`);
    return res.data;
  },
};

/** Build a WebSocket URL for the support chat using the stored auth token. */
export const supportWsUrl = () => {
  const token = localStorage.getItem('clari5pay_token') || '';
  const q = `?token=${encodeURIComponent(token)}`;
  // Explicit base (dev) → derive ws:// from it. Empty base (prod, same-origin) →
  // build from the current page so it works on any host/domain (and uses wss on https).
  if (BASE_URL) return `${BASE_URL.replace(/^http/, 'ws')}/api/support/ws${q}`;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/support/ws${q}`;
};

export const userAPI = {
  // Merchant/admin lists back several selectors and dashboard tiles and are re-read on every
  // poll. Short-TTL cached + de-duplicated; user mutations below clear them.
  getMerchants: async () => cachedRef('ref:merchants', async () => {
    const res = await api.get<User[]>('/api/users/merchants');
    return res.data;
  }),
  getAdmins: async () => cachedRef('ref:admins', async () => {
    const res = await api.get<User[]>('/api/users/admins');
    return res.data;
  }),
  createAdmin: async (data: Record<string, unknown>) => {
    const res = await api.post<User>('/api/users/admins', data);
    invalidateRef('ref:');           // the cached admin/merchant lists are now stale
    return res.data;
  },
  createMerchant: async (data: Record<string, unknown>) => {
    const res = await api.post<User>('/api/users/merchants', data);
    invalidateRef('ref:');
    return res.data;
  },
  getAdminMerchants: async (adminId: number) => {
    const res = await api.get<User[]>(`/api/users/admins/${adminId}/merchants`);
    return res.data;
  },
  toggleStatus: async (id: number, reason: string) => {
    const res = await api.patch<User>(`/api/users/${id}/toggle`, { reason });
    invalidateRef('ref:');           // active/inactive is rendered from the cached lists
    return res.data;
  },
  unlock: async (id: number) => {
    const res = await api.patch<User>(`/api/users/${id}/unlock`);
    return res.data;
  },
  resetPassword: async (id: number, new_password: string) => {
    const res = await api.post<{ message: string }>(`/api/users/${id}/reset-password`, { new_password });
    return res.data;
  },
  changePassword: async (data: { current_password: string; new_password: string }) => {
    const res = await api.post('/api/users/change-password', data);
    return res.data;
  },
  updateProfile: async (data: { email?: string; phone?: string; new_password?: string; current_password?: string; avatar?: string; whatsappEnabled?: boolean }) => {
    const res = await api.patch<User>('/api/users/me', data);
    return res.data;
  },
};

export const notificationAPI = {
  list: async () => {
    const res = await api.get<Notification[]>('/api/notifications');
    return res.data;
  },
  markAllRead: async () => {
    const res = await api.post('/api/notifications/read');
    return res.data;
  },
  clear: async () => {
    const res = await api.delete('/api/notifications');
    return res.data;
  },
};

export interface WhatsappSettings {
  configured: boolean; provider: string | null; businessNumber: string | null; businessAccountId: string | null;
  phoneIdSet: boolean; templateSet: boolean; webhookConfigured: boolean;
  roles: Record<string, boolean>; roleKeys: string[];
  events: Record<string, boolean>; eventKeys: string[];
}
export interface WhatsappStats { sentToday: number; delivered: number; read: number; failed: number; pending: number; total: number; successRate: number }
export interface WhatsappLog {
  id: number; user: string | null; role: string | null; phone: string | null; type: string | null;
  message: string; status: string; deliveryStatus: string | null; messageId: string | null;
  retryCount: number; provider: string | null; failureReason: string | null;
  sentAt: string | null; deliveredAt: string | null; readAt: string | null;
}
export const whatsappAPI = {
  getSettings: async () => (await api.get<WhatsappSettings>('/api/whatsapp/settings')).data,
  setSettings: async (body: { roles?: Record<string, boolean>; events?: Record<string, boolean> }) =>
    (await api.put<{ roles: Record<string, boolean>; events: Record<string, boolean> }>('/api/whatsapp/settings', body)).data,
  getStats: async () => (await api.get<WhatsappStats>('/api/whatsapp/stats')).data,
  sendTest: async () => (await api.post<{ ok: boolean; messageId: string | null; reason: string | null }>('/api/whatsapp/test')).data,
  getLogs: async (limit = 100) => (await api.get<WhatsappLog[]>('/api/whatsapp/logs', { params: { limit } })).data,
};

export interface TelegramLinkUser { id: number; name: string; username: string; role: string; linked: boolean }
export interface TelegramStatus {
  configured: boolean; webhookSecretSet: boolean; linkedUsers: number; totalEligible: number;
  users: TelegramLinkUser[];
}
export const telegramAPI = {
  getStatus: async () => (await api.get<TelegramStatus>('/api/telegram/status')).data,
};

export const demoAPI = {
  reset: async () => (await api.post<{ ok: boolean; resetBy: string; tables: string[] }>('/api/demo/reset', { confirm: 'RESET' })).data,
};

// Agent Management → Agents (Non-EPS agents). Supervisor/Manager only; demo-gated backend.
export interface AgentQuery {
  q?: string; category?: string; country?: string; state?: string; status?: string;
}
export interface AgentCreatePayload {
  fullName: string; country: string; state: string; location: string;
  mobile?: string; email?: string; currency: string; dateOfCreation?: string;
  reference?: string; feesPct: number; transactionCode: string; category: string;
  notes?: string; riskAnalysis?: boolean; sendForApproval?: boolean;
}
export type AgentUpdatePayload = Partial<Omit<AgentCreatePayload, 'transactionCode' | 'dateOfCreation' | 'sendForApproval'>> & { status?: string };

const cleanParams = (p?: AgentQuery): Record<string, string> | undefined => {
  if (!p) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) if (v != null && String(v).trim() !== '') out[k] = String(v);
  return Object.keys(out).length ? out : undefined;
};

export const agentAPI = {
  list: async (params?: AgentQuery) =>
    (await api.get<Agent[]>('/api/agents', { params: cleanParams(params) })).data,
  get: async (id: number) => (await api.get<Agent>(`/api/agents/${id}`)).data,
  // Anything that changes the agent master list clears the cached agent form-data, so the
  // selectors on the agent transaction forms pick the change up immediately.
  create: async (data: AgentCreatePayload) => {
    const r = (await api.post<Agent>('/api/agents', data)).data; invalidateRef('agent:'); return r;
  },
  update: async (id: number, data: AgentUpdatePayload) => {
    const r = (await api.put<Agent>(`/api/agents/${id}`, data)).data; invalidateRef('agent:'); return r;
  },
  setStatus: async (id: number, status: 'ACTIVE' | 'INACTIVE') => {
    const r = (await api.patch<Agent>(`/api/agents/${id}/status`, { status })).data; invalidateRef('agent:'); return r;
  },
  remove: async (id: number) => {
    const r = (await api.delete<{ ok: boolean }>(`/api/agents/${id}`)).data; invalidateRef('agent:'); return r;
  },
};

// Agent Accounts (Bank / UPI / QR / Crypto) — nested under an agent (numeric agent_master id).
export interface AgentAccountQuery { q?: string; accountType?: string; currency?: string; status?: string; }
export interface AgentAccountPayload {
  accountType?: string; label?: string; currency?: string; notes?: string; isDefault?: boolean;
  accountHolder?: string; accountNumber?: string; ifsc?: string; bankName?: string; branch?: string;
  upiId?: string; upiHolder?: string; qrImage?: string; qrLinkedRef?: string;
  walletAddress?: string; cryptoNetwork?: string; cryptoAsset?: string;
}
const cleanAcctParams = (p?: AgentAccountQuery): Record<string, string> | undefined => {
  if (!p) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(p)) if (v != null && String(v).trim() !== '') out[k] = String(v);
  return Object.keys(out).length ? out : undefined;
};

export const agentAccountAPI = {
  list: async (agentId: number, params?: AgentAccountQuery) =>
    (await api.get<AgentAccount[]>(`/api/agents/${agentId}/accounts`, { params: cleanAcctParams(params) })).data,
  get: async (agentId: number, accId: number) =>
    (await api.get<AgentAccount>(`/api/agents/${agentId}/accounts/${accId}`)).data,
  create: async (agentId: number, data: AgentAccountPayload) =>
    (await api.post<AgentAccount>(`/api/agents/${agentId}/accounts`, data)).data,
  update: async (agentId: number, accId: number, data: AgentAccountPayload) =>
    (await api.put<AgentAccount>(`/api/agents/${agentId}/accounts/${accId}`, data)).data,
  setStatus: async (agentId: number, accId: number, status: 'ACTIVE' | 'INACTIVE') =>
    (await api.patch<AgentAccount>(`/api/agents/${agentId}/accounts/${accId}/status`, { status })).data,
  setDefault: async (agentId: number, accId: number) =>
    (await api.patch<AgentAccount>(`/api/agents/${agentId}/accounts/${accId}/default`, {})).data,
  remove: async (agentId: number, accId: number) =>
    (await api.delete<{ ok: boolean }>(`/api/agents/${agentId}/accounts/${accId}`)).data,
};

// Agent Assignment (Phase 4) — assign/reassign a Non-EPS agent + account to a transaction.
export const agentAssignmentAPI = {
  get: async (ref: string) => (await api.get<AgentAssignmentResult>(`/api/transactions/${ref}/agent-assignment`)).data,
  assign: async (ref: string, data: { agentId: number; agentAccountId: number; paymentMethod?: string }) =>
    (await api.post<AgentAssignmentCurrent>(`/api/transactions/${ref}/assign-agent`, data)).data,
};

export const agentDashboardAPI = {
  get: async () => (await api.get<AgentDashboard>('/api/agent-dashboard')).data,
};

export const agentTransactionAPI = {
  assigned: async () => (await api.get<AgentTxRow[]>('/api/agent-transactions')).data,
  unassigned: async () => (await api.get<AgentTxRow[]>('/api/agent-transactions/unassigned')).data,
  assignmentHistory: async () => (await api.get<AgentAssignmentHistoryRow[]>('/api/agent-transactions/assignment-history')).data,
  allAccounts: async () => (await api.get<Record<string, unknown>[]>('/api/agent-transactions/all-accounts')).data,
  audit: async () => (await api.get<AgentAuditRow[]>('/api/agent-transactions/audit')).data,
};

export const systemLogAPI = {
  list: async () => {
    const res = await api.get<SystemLogEntry[]>('/api/system-logs');
    return res.data;
  },
};

export const auditLogAPI = {
  list: async () => {
    const res = await api.get<AuditLogEntry[]>('/api/audit-logs');
    return res.data;
  },
};

export const newsAPI = {
  list: async () => {
    const res = await api.get<NewsPost[]>('/api/news');
    return res.data;
  },
  sections: async () => {
    const res = await api.get<string[]>('/api/news/sections');
    return res.data;
  },
  create: async (data: { section?: string; category: string; title: string; body: string; image?: string | null; published: boolean; featured?: boolean; priority?: string; publish_date?: string | null }) => {
    const res = await api.post<NewsPost>('/api/news', data);
    return res.data;
  },
  update: async (id: number, data: { section?: string; category: string; title: string; body: string; image?: string | null; published: boolean; featured?: boolean; priority?: string; publish_date?: string | null }) => {
    const res = await api.patch<NewsPost>(`/api/news/${id}`, data);
    return res.data;
  },
  view: async (id: number) => {
    try { await api.post(`/api/news/${id}/view`); } catch { /* best-effort view count */ }
  },
  remove: async (id: number) => {
    const res = await api.delete(`/api/news/${id}`);
    return res.data;
  },
};

export interface BlogListParams {
  status?: string;
  category_id?: number;
  author?: string;
  q?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export interface BlogInput {
  title: string;
  category_id?: number | null;
  short_description?: string;
  content: string;
  cover_image?: string | null;
  images?: string[];
  tags?: string[];
  status: string;
}

export const blogAPI = {
  list: async (params: BlogListParams = {}) => {
    const res = await api.get<{ items: BlogPost[]; total: number }>('/api/blogs', { params });
    return res.data;
  },
  stats: async () => {
    const res = await api.get<BlogStats>('/api/blogs/stats');
    return res.data;
  },
  analytics: async () => {
    const res = await api.get<BlogAnalytics>('/api/blogs/analytics');
    return res.data;
  },
  get: async (id: number) => {
    const res = await api.get<BlogPost>(`/api/blogs/${id}`);
    return res.data;
  },
  create: async (data: BlogInput) => {
    const res = await api.post<BlogPost>('/api/blogs', data);
    return res.data;
  },
  update: async (id: number, data: BlogInput) => {
    const res = await api.patch<BlogPost>(`/api/blogs/${id}`, data);
    return res.data;
  },
  setStatus: async (id: number, status: string) => {
    const res = await api.patch<BlogPost>(`/api/blogs/${id}/status`, { status });
    return res.data;
  },
  remove: async (id: number) => {
    const res = await api.delete(`/api/blogs/${id}`);
    return res.data;
  },
};

export const blogCategoryAPI = {
  list: async () => {
    const res = await api.get<BlogCategory[]>('/api/blogs/categories');
    return res.data;
  },
  create: async (data: { name: string; description?: string | null }) => {
    const res = await api.post<BlogCategory>('/api/blogs/categories', data);
    return res.data;
  },
  update: async (id: number, data: { name: string; description?: string | null }) => {
    const res = await api.patch<BlogCategory>(`/api/blogs/categories/${id}`, data);
    return res.data;
  },
  remove: async (id: number) => {
    const res = await api.delete(`/api/blogs/categories/${id}`);
    return res.data;
  },
};

export const aiAPI = {
  chat: async (messages: Array<{ role: string; content: string }>) => {
    const res = await api.post<{ reply: string }>('/api/ai/chat', { messages });
    return res.data;
  },
};

export const activeUsersAPI = {
  list: async () => {
    const res = await api.get<ActiveUsersData>('/api/active-users');
    return res.data;
  },
  heartbeat: async () => {
    try { await api.post('/api/active-users/heartbeat'); } catch { /* best-effort presence */ }
  },
};

export const supportManagementAPI = {
  list: async () => {
    const res = await api.get<SupportMembersData>('/api/support-management/agents');
    return res.data;
  },
  create: async (data: Record<string, unknown>) => {
    const res = await api.post<SupportMemberRow>('/api/support-management/agents', data);
    return res.data;
  },
  update: async (id: number, data: Record<string, unknown>) => {
    const res = await api.patch<SupportMemberRow>(`/api/support-management/agents/${id}`, data);
    return res.data;
  },
  toggle: async (id: number, reason: string) => {
    const res = await api.patch<SupportMemberRow>(`/api/support-management/agents/${id}/toggle`, { reason });
    return res.data;
  },
  resetPassword: async (id: number, newPassword: string) => {
    const res = await api.post<{ message: string }>(`/api/support-management/agents/${id}/reset-password`, { new_password: newPassword });
    return res.data;
  },
  profile: async (id: number) => {
    const res = await api.get<SupportMemberRow>(`/api/support-management/agents/${id}/profile`);
    return res.data;
  },
  archive: async (id: number) => {
    const res = await api.delete<{ message: string }>(`/api/support-management/agents/${id}`);
    return res.data;
  },
  setAvailability: async (availability: 'AVAILABLE' | 'BUSY' | 'ON_BREAK') => {
    const res = await api.patch<{ availability: string }>('/api/support-management/me/availability', { availability });
    return res.data;
  },
  // Admin: force a member's availability.
  forceAvailability: async (id: number, availability: 'AVAILABLE' | 'BUSY' | 'ON_BREAK') => {
    const res = await api.patch<SupportMemberRow>(`/api/support-management/agents/${id}/availability`, { availability });
    return res.data;
  },
  // Assignment config (max active conversations + strategy).
  getConfig: async () => {
    const res = await api.get<{ maxActiveConversations: number; strategy: string }>('/api/support-management/config');
    return res.data;
  },
  updateConfig: async (data: { maxActiveConversations?: number; strategy?: string }) => {
    const res = await api.put<{ maxActiveConversations: number; strategy: string }>('/api/support-management/config', data);
    return res.data;
  },
  // Conversations (active + queued).
  conversations: async (status?: 'open' | 'queued') => {
    const res = await api.get<SupportConversationRow[]>('/api/support-management/conversations', { params: status ? { status } : {} });
    return res.data;
  },
  reassignConversation: async (conversationId: number, supportId: number) => {
    const res = await api.post<{ ok: boolean; supportId: number; supportName: string }>(`/api/support-management/conversations/${conversationId}/reassign`, { supportId });
    return res.data;
  },
  closeConversation: async (conversationId: number) => {
    const res = await api.post<{ ok: boolean }>(`/api/support-management/conversations/${conversationId}/close`, {});
    return res.data;
  },
};

export default api;
