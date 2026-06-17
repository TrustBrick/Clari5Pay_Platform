import axios from 'axios';
import type { Account, BalanceSummary, LoginRequest, LoginResponse, Notification, SupportMessage, SystemLogEntry, Transaction, User } from '../types';

export const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

const api = axios.create({ baseURL: BASE_URL });

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
  login: async (data: LoginRequest): Promise<LoginResponse> => {
    const form = new URLSearchParams();
    form.append('username', data.username);
    form.append('password', data.password);
    const res = await api.post<LoginResponse>('/api/auth/login', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return res.data;
  },
  me: async (): Promise<User> => {
    const res = await api.get<User>('/api/auth/me');
    return res.data;
  },
};

export const transactionAPI = {
  getAll: async (params?: { type?: string; status?: string; search?: string }) => {
    const res = await api.get<Transaction[]>('/api/transactions', { params });
    return res.data;
  },
  getMine: async () => {
    const res = await api.get<Transaction[]>('/api/transactions/mine');
    return res.data;
  },
  summary: async () => {
    const res = await api.get<BalanceSummary>('/api/transactions/summary');
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
  reject: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/reject`);
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
  submitAccount: async (
    id: string,
    data: { adminRef?: string; adminProof?: string; adminBankDetails?: string; adminUpiId?: string },
  ) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/account-submit`, data);
    return res.data;
  },
  submitSlip: async (id: string, data: { merchantProof?: string; merchantRef?: string }) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/slip`, data);
    return res.data;
  },
  markDone: async (id: string, data?: { adminProof?: string }) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/done`, data ?? {});
    return res.data;
  },
  cancel: async (id: string) => {
    const res = await api.post<Transaction>(`/api/transactions/${id}/cancel`);
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
  create: async (data: Record<string, unknown>) => {
    const res = await api.post<Account>('/api/accounts', data);
    return res.data;
  },
  toggle: async (ref: string) => {
    const res = await api.patch<Account>(`/api/accounts/${ref}/toggle`);
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
  const base = BASE_URL.replace(/^http/, 'ws');
  return `${base}/api/support/ws?token=${encodeURIComponent(token)}`;
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
  toggleStatus: async (id: number) => {
    const res = await api.patch<User>(`/api/users/${id}/toggle`);
    return res.data;
  },
  changePassword: async (data: { current_password: string; new_password: string }) => {
    const res = await api.post('/api/users/change-password', data);
    return res.data;
  },
  updateProfile: async (data: { email?: string; new_password?: string; current_password?: string }) => {
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

export const systemLogAPI = {
  list: async () => {
    const res = await api.get<SystemLogEntry[]>('/api/system-logs');
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
