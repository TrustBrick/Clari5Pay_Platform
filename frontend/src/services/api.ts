import axios from 'axios';
import type { Account, AccountBalance, AdminUpi, AuditLogEntry, BalanceSummary, BlogAnalytics, BlogCategory, BlogPost, BlogStats, GlobalSummary, LoginRequest, LoginResponse, MerchantBalance, MerchantStats, MerchantBankAccount, Notification, NewsPost, OtpChallenge, ReportData, RiskOverview, RiskProfile, RiskMemberBanks, Complaint, ComplaintList, SupportMessage, SystemLogEntry, Transaction, User } from '../types';

// Empty string is a valid value meaning "same origin" (production behind nginx),
// so use ?? — only fall back to the dev default when the var is truly unset.
export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

const api = axios.create({ baseURL: BASE_URL });

/** Set/clear the auth token on the client synchronously (used at login/logout). */
export const setAuthToken = (token: string | null) => {
  if (token) api.defaults.headers.common.Authorization = `Bearer ${token}`;
  else delete api.defaults.headers.common.Authorization;
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
    if (err.response?.status === 401) {
      localStorage.removeItem('clari5pay_token');
      localStorage.removeItem('clari5pay_user');
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
  submitSlip: async (id: string, data: { merchantProof?: string; merchantProofs?: string[]; merchantRef?: string }) => {
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
  lastForMember: async (memberId: string) => {
    const res = await api.get<{ referenceNumber: string | null }>(`/api/accounts/for-member/${encodeURIComponent(memberId)}`);
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
  send: async (content: string, merchantId?: number) => {
    const res = await api.post<SupportMessage>('/api/support/messages', { content, merchant_id: merchantId });
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
  getMerchants: async () => {
    const res = await api.get<User[]>('/api/users/merchants');
    return res.data;
  },
  getAdmins: async () => {
    const res = await api.get<User[]>('/api/users/admins');
    return res.data;
  },
  createAdmin: async (data: Record<string, unknown>) => {
    const res = await api.post<User>('/api/users/admins', data);
    return res.data;
  },
  createMerchant: async (data: Record<string, unknown>) => {
    const res = await api.post<User>('/api/users/merchants', data);
    return res.data;
  },
  getAdminMerchants: async (adminId: number) => {
    const res = await api.get<User[]>(`/api/users/admins/${adminId}/merchants`);
    return res.data;
  },
  toggleStatus: async (id: number, reason: string) => {
    const res = await api.patch<User>(`/api/users/${id}/toggle`, { reason });
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
  updateProfile: async (data: { email?: string; new_password?: string; current_password?: string; avatar?: string; whatsappEnabled?: boolean }) => {
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

export const demoAPI = {
  reset: async () => (await api.post<{ ok: boolean; resetBy: string; tables: string[] }>('/api/demo/reset', { confirm: 'RESET' })).data,
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

export default api;
