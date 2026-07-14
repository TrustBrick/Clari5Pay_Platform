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
  supportAvailability?: 'AVAILABLE' | 'BUSY' | 'ON_BREAK';
}

export type Availability = 'AVAILABLE' | 'BUSY' | 'ON_BREAK';

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
  attachment?: string | null;
  attachmentName?: string | null;
  attachmentType?: string | null;
  attachmentSize?: number | null;
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
  online?: boolean;
  lastSeen?: string | null;
}

export interface Presence {
  online: boolean;
  lastSeen?: string | null;
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
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form,
    });
  } catch {
    // The request never reached the server (offline, DNS, CORS/network failure).
    throw new Error('Unable to connect. Please try again.');
  }
  if (res.status >= 500) throw new Error('Something went wrong. Please try again later.');
  if (!res.ok) {
    // Prefer a specific auth error from the backend (locked, deactivated, attempts-left…);
    // fall back to a generic message that never reveals which credential was wrong.
    let detail = '';
    try { detail = (await res.json())?.detail || ''; } catch { /* non-JSON body */ }
    throw new Error(detail || 'Invalid username or password.');
  }
  const data = await res.json();
  // Defensive: an OTP challenge (or any non-token payload) has no `user` → treat as a failed login
  // rather than surfacing a raw JS error.
  if (!data?.user) throw new Error('Invalid username or password.');
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
export const fetchMerchantPresence = (merchantId: number) => getJSON<Presence>(`/api/support/merchant/${merchantId}/presence`);

export async function setAvailability(availability: Availability): Promise<Availability> {
  const res = await fetch(`${BASE_URL}/api/support-management/me/availability`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ availability }),
  });
  if (!res.ok) throw new Error('Failed to update availability');
  // Keep the cached user in sync so the toggle survives a reload.
  const u = getUser();
  if (u) localStorage.setItem(USER_KEY, JSON.stringify({ ...u, supportAvailability: availability }));
  const data = await res.json();
  return (data.availability as Availability) ?? availability;
}

export async function sendMessage(
  content: string, merchantId: number, attachment?: { dataUrl: string; name: string } | null,
): Promise<Message> {
  const res = await fetch(`${BASE_URL}/api/support/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      content, merchant_id: merchantId,
      attachment: attachment?.dataUrl, attachment_name: attachment?.name,
    }),
  });
  if (!res.ok) {
    let detail = 'Failed to send';
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
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
