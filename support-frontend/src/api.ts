// Empty string = same origin (production behind nginx/Caddy). Use ?? so only an
// unset var falls back to the dev default.
export const BASE_URL = (import.meta as any).env.VITE_API_BASE_URL ?? 'http://localhost:8000';

const TOKEN_KEY = 'clari5pay_support_token';
const USER_KEY = 'clari5pay_support_user';

export interface SupportUser {
  id: number;
  username: string;
  name: string;
  email: string;
  role: string;
}

export interface Conversation {
  merchantId: number;
  merchantName: string;
  email: string;
  phone?: string | null;
  username: string;
  lastMessage?: string | null;
  lastAt?: string | null;
  unread: number;
  messageCount: number;
}

export interface Message {
  id: number;
  merchantId: number;
  sender: 'MERCHANT' | 'SUPPORT';
  senderName: string;
  content: string;
  read: boolean;
  createdAt: string;
}

export interface MerchantDetail {
  id: number;
  name: string;
  username: string;
  email: string;
  phone?: string | null;
  balance?: number;
  risk?: string;
  profile?: string;
  payIn?: string;
  payOut?: string;
  active: boolean;
  created: string;
}

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const getUser = (): SupportUser | null => {
  const s = localStorage.getItem(USER_KEY);
  return s ? JSON.parse(s) : null;
};
export const clearAuth = () => {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
};

const authHeaders = (): Record<string, string> => {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
};

export async function login(username: string, password: string): Promise<SupportUser> {
  const form = new URLSearchParams();
  form.append('username', username);
  form.append('password', password);
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: form,
  });
  if (!res.ok) throw new Error('Invalid credentials');
  const data = await res.json();
  if (data.user.role !== 'SUPPORT_AGENT') {
    throw new Error('This portal is for Customer Support agents only');
  }
  localStorage.setItem(TOKEN_KEY, data.access_token);
  localStorage.setItem(USER_KEY, JSON.stringify(data.user));
  return data.user;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { headers: authHeaders() });
  if (res.status === 401) { clearAuth(); window.location.reload(); }
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

export const fetchConversations = () => getJSON<Conversation[]>('/api/support/conversations');
export const fetchMessages = (merchantId: number) => getJSON<Message[]>(`/api/support/messages/${merchantId}`);
export const fetchMerchant = (merchantId: number) => getJSON<MerchantDetail>(`/api/support/merchant/${merchantId}`);

export async function sendMessage(content: string, merchantId: number): Promise<Message> {
  const res = await fetch(`${BASE_URL}/api/support/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ content, merchant_id: merchantId }),
  });
  if (!res.ok) throw new Error('Failed to send');
  return res.json();
}

export const wsUrl = () => {
  const q = `?token=${encodeURIComponent(getToken() || '')}`;
  // Explicit base (dev) → derive ws:// from it. Empty base (prod, same-origin) →
  // build from the current page (uses wss on https).
  if (BASE_URL) return `${BASE_URL.replace(/^http/, 'ws')}/api/support/ws${q}`;
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/support/ws${q}`;
};
