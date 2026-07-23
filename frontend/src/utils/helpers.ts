import { T } from './theme';
import type { TxStatus } from '../types';

export const fmt = (n: number) =>
  `INR ${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ── Indian-numbering amount input formatting ──────────────────────────────────
// Group a string of digits (integer part only) with Indian grouping: 1,00,00,000 style
// (last 3 digits, then groups of 2).
const groupIndianDigits = (digits: string): string => {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  return rest.replace(/\B(?=(\d\d)+(?!\d))/g, ',') + ',' + last3;
};

// Format an amount input string with Indian grouping in real time, preserving an
// in-progress decimal (clamped to 2 places). Commas are display-only — recover the raw
// numeric string for the API / parseFloat with parseIndianAmount(). Returns '' when empty.
export const formatIndianAmountInput = (value: string): string => {
  const cleaned = String(value ?? '').replace(/[^\d.]/g, '');
  if (cleaned === '') return '';
  const dot = cleaned.indexOf('.');
  let intPart = (dot === -1 ? cleaned : cleaned.slice(0, dot)).replace(/^0+(?=\d)/, '');
  const grouped = intPart === '' ? '0' : groupIndianDigits(intPart);
  if (dot === -1) return grouped;
  const decPart = cleaned.slice(dot + 1).replace(/\./g, '').slice(0, 2);
  return `${grouped}.${decPart}`;
};

// Strip the display commas to recover the raw numeric string (for the backend / parseFloat).
export const parseIndianAmount = (value: string): string =>
  String(value ?? '').replace(/,/g, '').replace(/[^\d.]/g, '');

// ── Client-facing Approver — the CLIENT's approval hierarchy, never a real user ───────────────
// Client-facing output (reports, exports, dashboards, transaction history) must never expose the
// name of the internal Clari5Pay admin who actioned a transaction. The client sees only the
// business approver role their own workflow defines:
//     Deposit → Supervisor · Withdrawal → Manager · Settlement → Manager
// Accepts either the report `type` ('deposit') or a raw TxType ('DEPOSIT_BANK'). Callers gate this
// on the transaction actually having been approved, so an unapproved row still shows '—' — this
// only swaps the displayed NAME for the ROLE, it never changes when an approver is shown.
// Internal audit logs / admin screens keep recording and showing the real system user.
export const clientApproverLabel = (type?: string | null): string => {
  const t = String(type || '').toUpperCase();
  if (t.startsWith('DEPOSIT')) return 'Supervisor';
  if (t.startsWith('WITHDRAWAL') || t.startsWith('SETTLEMENT')) return 'Manager';
  return '—';
};

// Roles that belong to Clari5Pay, not to the client. Their real names/usernames are recorded in
// the internal audit log and shown on internal/admin screens, but never surfaced to the client —
// a client-facing row attributed to one of these shows the role alone. That an Admin acted is
// already part of the base UI (e.g. the slip's "Admin Action" row); only the person is hidden.
const INTERNAL_ROLE_LABELS: Record<string, string> = {
  ADMIN: 'Admin',
  SUPER_ADMIN: 'Super Admin',
  SUPERADMIN: 'Super Admin',
  SUPPORT: 'Support',
};
export const isInternalRole = (role?: string | null) =>
  Object.prototype.hasOwnProperty.call(INTERNAL_ROLE_LABELS, String(role || '').toUpperCase());
// Display label for an internal role — never falls through to a raw enum like "SUPER_ADMIN".
const internalRoleLabel = (role?: string | null) =>
  INTERNAL_ROLE_LABELS[String(role || '').toUpperCase()] || String(role || '');

export const statusStyle = (s: TxStatus) => {
  const map: Record<string, { color: string; bg: string }> = {
    PENDING: { color: T.warning, bg: T.warningBg },
    ADMIN_APPROVED: { color: T.info, bg: T.infoBg },
    COMPLETED: { color: T.success, bg: T.successBg },
    SUCCESSFUL: { color: T.success, bg: T.successBg },
    REJECTED: { color: T.danger, bg: T.dangerBg },
    SA_REJECTED: { color: T.danger, bg: T.dangerBg },
    CANCELLED: { color: T.danger, bg: T.dangerBg },
    ACCOUNT_REQUESTED: { color: T.warning, bg: T.warningBg },
    ACCOUNT_SUBMITTED: { color: T.info, bg: T.infoBg },
    SLIP_SUBMITTED: { color: T.blue, bg: T.infoBg },
    // Supervisor (deposit) / Manager (withdrawal) review-gate workflow.
    PENDING_APPROVAL: { color: T.warning, bg: T.warningBg },
    SUPERVISOR_REVIEW: { color: T.blue, bg: T.infoBg },
    MANAGER_REVIEW: { color: T.blue, bg: T.infoBg },
    RESUBMITTED: { color: T.warning, bg: T.warningBg },
    DEPOSITED: { color: T.success, bg: T.successBg },
  };
  return map[s] || { color: T.textMuted, bg: T.borderLight };
};

// The Merchant Portal shows only the business-level withdrawal lifecycle:
//   Manager Review → Pending → Completed / Rejected
// The internal steps a withdrawal passes through in between — the Admin payout that follows the
// Manager's approval, and a return to the Data Operator for correction — are collapsed onto the
// nearest business status, so the merchant is never shown an internal workflow state. The stored
// status, the workflow, permissions and the Admin/Super Admin portals are all untouched: this
// resolves the status to DISPLAY, nothing more.
const MERCHANT_WITHDRAWAL_VIEW: Record<string, string> = {
  RESUBMITTED: 'MANAGER_REVIEW',     // returned to the operator — the Manager has not decided yet
  ACCOUNT_REQUESTED: 'PENDING',      // Manager approved; the Admin payout is in progress
  SLIP_SUBMITTED: 'PENDING',         // same, for rows approved before the status change (and legacy)
};

// The status to render for a viewer. Two display-only remaps, in order:
//  1. "Send To Approval": a request in a review gate reads as the CHOSEN approver's role — a deposit
//     sent to a Manager shows "Manager Review", a withdrawal sent to a Supervisor "Supervisor Review"
//     — so the label matches who must act, not the fixed deposit/supervisor·withdrawal/manager gate.
//     The stored status and the workflow are unchanged; this is presentation only.
//  2. A withdrawal seen from the Merchant Portal collapses its internal steps (MERCHANT_WITHDRAWAL_VIEW).
const REVIEW_STATUS_FOR_ROLE: Record<string, string> = {
  MANAGER: 'MANAGER_REVIEW',
  SUPERVISOR: 'SUPERVISOR_REVIEW',
};

export const displayStatus = (status: string, type?: string, viewerRole?: string, approverRole?: string | null): string => {
  const appr = String(approverRole || '').toUpperCase();
  if (appr && (status === 'SUPERVISOR_REVIEW' || status === 'MANAGER_REVIEW')) {
    // Only a role that maps to a real review gate may rewrite the label. An unrecognised role
    // leaves the stored status alone — silently collapsing it to "Supervisor Review" is exactly
    // how a Manager's request ends up reading as a Supervisor's.
    return REVIEW_STATUS_FOR_ROLE[appr] || status;
  }
  if (viewerRole !== 'MERCHANT' || !type || !type.startsWith('WITHDRAWAL')) return status;
  return MERCHANT_WITHDRAWAL_VIEW[status] || status;
};

// Role- and type-aware status label.
// Deposit: Account Requested → Account Submitted → Slip Submitted → Deposited.
// Withdrawal/Settlement: Submitted (merchant) / Pending (admin) → Completed.
export const statusLabel = (status: string, type?: string, viewerRole?: string): string => {
  const isDeposit = !!type && type.startsWith('DEPOSIT');
  const isSettlement = !!type && type.startsWith('SETTLEMENT');
  const isWithdrawOrSettle = !!type && (type.startsWith('WITHDRAWAL') || type.startsWith('SETTLEMENT'));
  if (status === 'COMPLETED') return isDeposit ? 'Deposited' : 'Completed';
  // A settlement forwarded to Admin (after Supervisor approval) reads "Settlement Submitted".
  if (status === 'SLIP_SUBMITTED' && isSettlement) return 'Settlement Submitted';
  if (status === 'ACCOUNT_REQUESTED' && isWithdrawOrSettle) {
    return viewerRole === 'MERCHANT' ? 'Submitted' : 'Pending';
  }
  return status
    .toLowerCase()
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
};

// Password complexity policy — mirrors the backend (app/core/security.py).
// Min 8 chars, 1 uppercase, 1 lowercase, 1 number, 1 special character.
export const PASSWORD_POLICY_TEXT =
  'At least 8 characters with an uppercase letter, a lowercase letter, a number and a special character.';

export const passwordPolicyError = (pw: string): string | null => {
  if (!pw || pw.length < 8) return 'Password must be at least 8 characters long.';
  if (!/[A-Z]/.test(pw)) return 'Password must contain at least one uppercase letter.';
  if (!/[a-z]/.test(pw)) return 'Password must contain at least one lowercase letter.';
  if (!/\d/.test(pw)) return 'Password must contain at least one number.';
  if (!/[^A-Za-z0-9]/.test(pw)) return 'Password must contain at least one special character.';
  return null;
};

// Merchant access-role labels (shared across header, profile, admin tables, forms).
export const MERCHANT_ROLE_LABELS: Record<string, string> = {
  ADMIN: 'Admin',
  USER: 'User',
  DEO: 'Data Operator',
  DEPOSIT_OPERATOR: 'Deposit Operator',
  WITHDRAWAL_OPERATOR: 'Withdrawal Operator',
  SUPERVISOR: 'Supervisor',
  MANAGER: 'Manager',
};
export const merchantRoleLabel = (r?: string | null) =>
  r ? (MERCHANT_ROLE_LABELS[String(r).toUpperCase()] || r) : '';

// Approval-record / remarks display: "Full Name (Role • username)", e.g.
// "BELLAGIO (Supervisor • harsha)". Role is resolved via MERCHANT_ROLE_LABELS (never
// hardcoded); `fallback` (e.g. "Merchant User") covers a missing role. The username is the
// actor's actual login username and is appended only when present — never generated.
export const nameWithRole = (name?: string | null, role?: string | null, fallback = '', username?: string | null): string => {
  const label = merchantRoleLabel(role) || fallback;
  const inside = [label, (username || '').trim()].filter(Boolean).join(' • ');
  return inside ? `${name ?? ''} (${inside})` : `${name ?? ''}`;
};

// Actor line for a remark / audit entry on a CLIENT-facing screen. An internal Clari5Pay role
// collapses to the role alone ("Admin"); the client's own staff keep the existing
// "Role · Name (Role • username)" format unchanged. Pairs with clientApproverLabel — same rule:
// the client sees the business role, never the internal person. Internal/admin screens must NOT
// use this (they show the real actor), and the audit log itself still records the real user.
export const clientRemarkActor = (role?: string | null, user?: string | null, username?: string | null): string =>
  isInternalRole(role)
    ? internalRoleLabel(role)
    : `${merchantRoleLabel(role) || String(role || '')} · ${nameWithRole(user, role, '', username)}`;

// Same rule for an audit row's actor column: internal actors show the role only — never their
// username, and never their IP (an internal operational detail with no client business value).
export const clientAuditActor = (role?: string | null, username?: string | null): string =>
  isInternalRole(role) ? internalRoleLabel(role) : `${username || ''}${role ? ` (${role})` : ''}`;

// ─── Customer Support chat: IST timestamps + attachment helpers ───────────────
// Chat timestamps are ALWAYS shown in Indian Standard Time (Asia/Kolkata), regardless of the
// viewer's device timezone. Backend sends UTC (…Z); we render it in IST here.
const IST_TZ = 'Asia/Kolkata';
export const chatTime = (iso: string) =>
  new Date(iso).toLocaleTimeString('en-US', { timeZone: IST_TZ, hour: '2-digit', minute: '2-digit', hour12: true });
export const chatDateLabel = (iso: string): string => {
  const key = (d: Date) => d.toLocaleDateString('en-CA', { timeZone: IST_TZ });   // YYYY-MM-DD in IST
  const k = key(new Date(iso));
  if (k === key(new Date())) return 'Today';
  if (k === key(new Date(Date.now() - 86400000))) return 'Yesterday';
  return new Date(iso).toLocaleDateString('en-GB', { timeZone: IST_TZ, day: '2-digit', month: 'short', year: 'numeric' });
};
// Open a base64 data-URL in a new tab reliably (via a blob URL — browsers block direct
// navigation to large data: URLs). Used for chat image "enlarge" and document "view".
export const openDataUrl = (dataUrl: string) => {
  try {
    const [head, b64] = dataUrl.split(',');
    const mime = (head.match(/data:([^;]+)/) || [])[1] || 'application/octet-stream';
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], { type: mime }));
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch { window.open(dataUrl, '_blank'); }
};
export const formatBytes = (n?: number | null): string => {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};

export const CHAT_IMAGE_TYPES = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp'];
export const CHAT_DOC_TYPES = [
  'application/pdf', 'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'text/plain', 'application/zip', 'application/x-zip-compressed',
];
// Accept attribute for the file picker.
export const CHAT_ACCEPT = '.jpg,.jpeg,.png,.webp,.pdf,.doc,.docx,.xls,.xlsx,.txt,.zip,image/*';
const _IMG_EXT = ['jpg', 'jpeg', 'png', 'webp'];
const _DOC_EXT = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'zip'];
const _EXT_MIME: Record<string, string> = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp',
  pdf: 'application/pdf', doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xls: 'application/vnd.ms-excel',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  txt: 'text/plain', zip: 'application/zip',
};
export const isChatImage = (type?: string | null, name?: string | null) => {
  const t = (type || '').toLowerCase();
  if (t.startsWith('image/')) return true;
  const ext = (name || '').split('.').pop()?.toLowerCase() || '';
  return _IMG_EXT.includes(ext);
};
// Client-side validation. Returns a friendly error string, or null when the file is allowed.
export const chatAttachmentError = (f: File): string | null => {
  const t = (f.type || '').toLowerCase();
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  const isImg = CHAT_IMAGE_TYPES.includes(t) || _IMG_EXT.includes(ext);
  const isDoc = CHAT_DOC_TYPES.includes(t) || _DOC_EXT.includes(ext);
  if (!isImg && !isDoc) return 'Unsupported file type. Allowed: images, PDF, Word, Excel, TXT, ZIP.';
  if (f.size > 8 * 1024 * 1024) return 'File too large. Maximum 8 MB.';
  return null;
};
// Read a file into a base64 data-URL, fixing a missing/generic MIME from the extension so the
// backend allowlist accepts it. Returns the fields sent with the chat message.
export const readChatAttachment = (f: File): Promise<{ dataUrl: string; name: string; type: string; size: number }> =>
  new Promise((resolve, reject) => {
    const ext = (f.name.split('.').pop() || '').toLowerCase();
    const mime = (f.type && f.type !== 'application/octet-stream') ? f.type : (_EXT_MIME[ext] || 'application/octet-stream');
    const r = new FileReader();
    r.onload = () => {
      let dataUrl = String(r.result || '');
      // Normalise the data-URL MIME to the resolved type so server validation matches.
      dataUrl = dataUrl.replace(/^data:[^;,]*;base64,/, `data:${mime};base64,`);
      resolve({ dataUrl, name: f.name, type: mime, size: f.size });
    };
    r.onerror = () => reject(new Error('read failed'));
    r.readAsDataURL(f);
  });

// Latest actual username recorded in the remarks trail for a given role — used to show the
// approver's username in the Approval Record (the reviewer/admin username lives in remarks).
export const remarkUsernameForRole = (
  remarks: ReadonlyArray<{ role: string; username?: string | null }> | null | undefined,
  role: string,
): string => {
  const list = remarks || [];
  for (let i = list.length - 1; i >= 0; i--) {
    if (String(list[i].role).toUpperCase() === role.toUpperCase() && list[i].username) return String(list[i].username);
  }
  return '';
};

// Maker = data-entry operators; Checker = review/approval roles. The admin "Create
// Merchant" form scopes the Roles dropdown to the selected Profile Type using these.
export const MAKER_ROLE_OPTIONS = [
  { value: 'DEO', label: 'Data Operator' },
  { value: 'DEPOSIT_OPERATOR', label: 'Deposit Operator' },
  { value: 'WITHDRAWAL_OPERATOR', label: 'Withdrawal Operator' },
];
export const CHECKER_ROLE_OPTIONS = [
  { value: 'SUPERVISOR', label: 'Supervisor' },
  { value: 'MANAGER', label: 'Manager' },
];
export const ADMIN_ROLE_OPTIONS = [{ value: 'ADMIN', label: 'Admin' }];
export const USER_ROLE_OPTIONS = [{ value: 'USER', label: 'User' }];
export const MERCHANT_ROLE_OPTIONS = [...MAKER_ROLE_OPTIONS, ...CHECKER_ROLE_OPTIONS];

// Role Type options for the merchant-user forms (Onboard Merchant / Create User).
export const ROLE_TYPE_OPTIONS = ['Admin', 'User', 'Maker', 'Checker'].map(v => ({ value: v, label: v }));

// Member Role options allowed for a given Role Type (Profile). Maker → operator roles;
// Checker → review roles; Admin → Admin; User → User.
export const rolesForProfile = (profile?: string | null) => {
  if (profile === 'Maker') return MAKER_ROLE_OPTIONS;
  if (profile === 'Checker') return CHECKER_ROLE_OPTIONS;
  if (profile === 'Admin') return ADMIN_ROLE_OPTIONS;
  if (profile === 'User') return USER_ROLE_OPTIONS;
  return MERCHANT_ROLE_OPTIONS;
};

// Unified "Membership Number - Member Name" label (number always first), e.g.
// "MBR02703 - Satish Kumar". Falls back to whichever part exists. Used everywhere a
// member is shown: tables, detail views, dashboard widgets, PDF + Excel exports.
export const memberLabel = (memberId?: string | null, memberName?: string | null): string => {
  const id = (memberId ?? '').toString().trim();
  const nm = (memberName ?? '').toString().trim();
  if (id && nm) return `${id} - ${nm}`;
  return id || nm || '—';
};

// Human-readable label for transaction types (handles the *_REQUEST variants).
export const typeLabel = (t: string) =>
  t
    .toLowerCase()
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');

// Deposit-type display labels. Codes UPI/IMPS/NEFT/RTGS are acronyms shown as-is;
// BANK/CASH/CRYPTO get friendly labels. Used across history, reports, analytics,
// audit logs and PDF/Excel exports so Cash/Crypto/Bank render consistently.
export const DEPOSIT_TYPE_LABELS: Record<string, string> = {
  UPI: 'UPI',
  BANK: 'Bank Transfer',
  IMPS: 'IMPS',
  NEFT: 'NEFT',
  RTGS: 'RTGS',
  CASH: 'Cash',
  CRYPTO: 'Crypto (USDT)',
};
export const depositTypeLabel = (code?: string | null) =>
  code ? (DEPOSIT_TYPE_LABELS[String(code).toUpperCase()] || code) : '';

// Friendly labels for the deposit-detail JSON keys (Cash / Crypto member-supplied fields).
export const DEPOSIT_DETAIL_LABELS: Record<string, string> = {
  village: 'Village', city: 'City', mobile: 'Mobile Number',
  walletAddress: 'Wallet Address', network: 'Network', txHash: 'Transaction Hash ID',
};
export const depositDetailLabel = (key: string) =>
  DEPOSIT_DETAIL_LABELS[key] ||
  key.replace(/([A-Z])/g, ' $1').replace(/^./, c => c.toUpperCase()).trim();

// Deposit-type dropdown options for the request form (code → display label).
export const DEPOSIT_TYPE_OPTIONS = [
  { value: 'UPI', label: 'UPI' },
  { value: 'BANK', label: 'Bank Transfer' },
  { value: 'IMPS', label: 'IMPS' },
  { value: 'NEFT', label: 'NEFT' },
  { value: 'RTGS', label: 'RTGS' },
  { value: 'CASH', label: 'Cash' },
  { value: 'CRYPTO', label: 'Crypto (USDT)' },
];

// Country dialing codes for the phone-number dropdown (India first, then alphabetical).
// Indian states + union territories, for the State pickers. The searchable dropdown still accepts
// free text, so a state outside this list (non-India agents) can simply be typed.
export const INDIAN_STATES = [
  'Andaman and Nicobar Islands', 'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar',
  'Chandigarh', 'Chhattisgarh', 'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Goa',
  'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jammu and Kashmir', 'Jharkhand', 'Karnataka',
  'Kerala', 'Ladakh', 'Lakshadweep', 'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya',
  'Mizoram', 'Nagaland', 'Odisha', 'Puducherry', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu',
  'Telangana', 'Tripura', 'Uttar Pradesh', 'Uttarakhand', 'West Bengal',
];

// Crypto wallet address — structural format check across the common networks (mirrors the agent
// backend's _valid_wallet). No network selector, so an address is valid if it is a valid shape on
// ANY network; a format check, not an on-chain proof.
const WALLET_FORMATS = [
  /^0x[0-9a-fA-F]{40}$/,                       // EVM: Ethereum / ERC20 / BSC / Polygon
  /^T[1-9A-HJ-NP-Za-km-z]{33}$/,               // TRON / TRC20
  /^(bc1)[0-9ac-hj-np-z]{11,87}$/,             // Bitcoin bech32
  /^[13][1-9A-HJ-NP-Za-km-z]{25,34}$/,         // Bitcoin legacy
  /^[1-9A-HJ-NP-Za-km-z]{32,44}$/,             // Solana
];
export const isValidWallet = (addr: string): boolean => {
  const a = (addr || '').trim();
  return !!a && WALLET_FORMATS.some((re) => re.test(a));
};

export const COUNTRY_CODES = [
  { code: '+91', label: '🇮🇳 +91 India' },
  { code: '+93', label: '🇦🇫 +93 Afghanistan' },
  { code: '+355', label: '🇦🇱 +355 Albania' },
  { code: '+213', label: '🇩🇿 +213 Algeria' },
  { code: '+54', label: '🇦🇷 +54 Argentina' },
  { code: '+374', label: '🇦🇲 +374 Armenia' },
  { code: '+61', label: '🇦🇺 +61 Australia' },
  { code: '+43', label: '🇦🇹 +43 Austria' },
  { code: '+994', label: '🇦🇿 +994 Azerbaijan' },
  { code: '+973', label: '🇧🇭 +973 Bahrain' },
  { code: '+880', label: '🇧🇩 +880 Bangladesh' },
  { code: '+375', label: '🇧🇾 +375 Belarus' },
  { code: '+32', label: '🇧🇪 +32 Belgium' },
  { code: '+591', label: '🇧🇴 +591 Bolivia' },
  { code: '+267', label: '🇧🇼 +267 Botswana' },
  { code: '+55', label: '🇧🇷 +55 Brazil' },
  { code: '+359', label: '🇧🇬 +359 Bulgaria' },
  { code: '+855', label: '🇰🇭 +855 Cambodia' },
  { code: '+237', label: '🇨🇲 +237 Cameroon' },
  { code: '+1', label: '🇨🇦 +1 Canada' },
  { code: '+56', label: '🇨🇱 +56 Chile' },
  { code: '+86', label: '🇨🇳 +86 China' },
  { code: '+57', label: '🇨🇴 +57 Colombia' },
  { code: '+506', label: '🇨🇷 +506 Costa Rica' },
  { code: '+385', label: '🇭🇷 +385 Croatia' },
  { code: '+357', label: '🇨🇾 +357 Cyprus' },
  { code: '+420', label: '🇨🇿 +420 Czechia' },
  { code: '+45', label: '🇩🇰 +45 Denmark' },
  { code: '+20', label: '🇪🇬 +20 Egypt' },
  { code: '+372', label: '🇪🇪 +372 Estonia' },
  { code: '+251', label: '🇪🇹 +251 Ethiopia' },
  { code: '+358', label: '🇫🇮 +358 Finland' },
  { code: '+33', label: '🇫🇷 +33 France' },
  { code: '+995', label: '🇬🇪 +995 Georgia' },
  { code: '+49', label: '🇩🇪 +49 Germany' },
  { code: '+233', label: '🇬🇭 +233 Ghana' },
  { code: '+30', label: '🇬🇷 +30 Greece' },
  { code: '+852', label: '🇭🇰 +852 Hong Kong' },
  { code: '+36', label: '🇭🇺 +36 Hungary' },
  { code: '+354', label: '🇮🇸 +354 Iceland' },
  { code: '+62', label: '🇮🇩 +62 Indonesia' },
  { code: '+98', label: '🇮🇷 +98 Iran' },
  { code: '+964', label: '🇮🇶 +964 Iraq' },
  { code: '+353', label: '🇮🇪 +353 Ireland' },
  { code: '+972', label: '🇮🇱 +972 Israel' },
  { code: '+39', label: '🇮🇹 +39 Italy' },
  { code: '+81', label: '🇯🇵 +81 Japan' },
  { code: '+962', label: '🇯🇴 +962 Jordan' },
  { code: '+254', label: '🇰🇪 +254 Kenya' },
  { code: '+965', label: '🇰🇼 +965 Kuwait' },
  { code: '+371', label: '🇱🇻 +371 Latvia' },
  { code: '+961', label: '🇱🇧 +961 Lebanon' },
  { code: '+370', label: '🇱🇹 +370 Lithuania' },
  { code: '+352', label: '🇱🇺 +352 Luxembourg' },
  { code: '+60', label: '🇲🇾 +60 Malaysia' },
  { code: '+960', label: '🇲🇻 +960 Maldives' },
  { code: '+356', label: '🇲🇹 +356 Malta' },
  { code: '+52', label: '🇲🇽 +52 Mexico' },
  { code: '+212', label: '🇲🇦 +212 Morocco' },
  { code: '+95', label: '🇲🇲 +95 Myanmar' },
  { code: '+977', label: '🇳🇵 +977 Nepal' },
  { code: '+31', label: '🇳🇱 +31 Netherlands' },
  { code: '+64', label: '🇳🇿 +64 New Zealand' },
  { code: '+234', label: '🇳🇬 +234 Nigeria' },
  { code: '+47', label: '🇳🇴 +47 Norway' },
  { code: '+968', label: '🇴🇲 +968 Oman' },
  { code: '+92', label: '🇵🇰 +92 Pakistan' },
  { code: '+507', label: '🇵🇦 +507 Panama' },
  { code: '+51', label: '🇵🇪 +51 Peru' },
  { code: '+63', label: '🇵🇭 +63 Philippines' },
  { code: '+48', label: '🇵🇱 +48 Poland' },
  { code: '+351', label: '🇵🇹 +351 Portugal' },
  { code: '+974', label: '🇶🇦 +974 Qatar' },
  { code: '+40', label: '🇷🇴 +40 Romania' },
  { code: '+7', label: '🇷🇺 +7 Russia' },
  { code: '+966', label: '🇸🇦 +966 Saudi Arabia' },
  { code: '+221', label: '🇸🇳 +221 Senegal' },
  { code: '+381', label: '🇷🇸 +381 Serbia' },
  { code: '+65', label: '🇸🇬 +65 Singapore' },
  { code: '+421', label: '🇸🇰 +421 Slovakia' },
  { code: '+386', label: '🇸🇮 +386 Slovenia' },
  { code: '+27', label: '🇿🇦 +27 South Africa' },
  { code: '+82', label: '🇰🇷 +82 South Korea' },
  { code: '+34', label: '🇪🇸 +34 Spain' },
  { code: '+94', label: '🇱🇰 +94 Sri Lanka' },
  { code: '+46', label: '🇸🇪 +46 Sweden' },
  { code: '+41', label: '🇨🇭 +41 Switzerland' },
  { code: '+886', label: '🇹🇼 +886 Taiwan' },
  { code: '+255', label: '🇹🇿 +255 Tanzania' },
  { code: '+66', label: '🇹🇭 +66 Thailand' },
  { code: '+216', label: '🇹🇳 +216 Tunisia' },
  { code: '+90', label: '🇹🇷 +90 Türkiye' },
  { code: '+256', label: '🇺🇬 +256 Uganda' },
  { code: '+380', label: '🇺🇦 +380 Ukraine' },
  { code: '+971', label: '🇦🇪 +971 UAE' },
  { code: '+44', label: '🇬🇧 +44 United Kingdom' },
  { code: '+1', label: '🇺🇸 +1 United States' },
  { code: '+598', label: '🇺🇾 +598 Uruguay' },
  { code: '+998', label: '🇺🇿 +998 Uzbekistan' },
  { code: '+58', label: '🇻🇪 +58 Venezuela' },
  { code: '+84', label: '🇻🇳 +84 Vietnam' },
  { code: '+260', label: '🇿🇲 +260 Zambia' },
  { code: '+263', label: '🇿🇼 +263 Zimbabwe' },
];

// Read a File (image/doc) into a base64 data URL for upload.
export const fileToDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });

// Parse a timestamp; if it carries no timezone, treat it as UTC (the backend
// stores UTC). Without this, "2026-06-16T07:08:30" is read as local time and the
// relative age is off by the local offset.
const parseTs = (iso: string): Date => {
  const hasTz = /[zZ]|[+-]\d\d:?\d\d$/.test(iso);
  // Only a date-TIME without a zone is ambiguous; append Z to treat it as UTC.
  if (!hasTz && iso.includes('T')) return new Date(iso + 'Z');
  return new Date(iso);
};

// Compact relative time, e.g. "just now", "5m ago", "3h ago", "2d ago".
export const timeAgo = (iso?: string | null) => {
  if (!iso) return '';
  const then = parseTs(iso).getTime();
  if (isNaN(then)) return '';
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hr ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} day${d === 1 ? '' : 's'} ago`;
  return parseTs(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
};

// Trigger a browser download for a data URL (e.g. the admin's account-details PNG).
export const downloadDataUrl = (dataUrl: string, filename: string) => {
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
};

// Download plain text as a .txt file (fallback when only text details exist).
export const downloadText = (text: string, filename: string) =>
  downloadDataUrl('data:text/plain;charset=utf-8,' + encodeURIComponent(text), filename);

export const today = () => new Date().toISOString().split('T')[0];
export const nowTime = () => new Date().toTimeString().split(' ')[0];

export const formatDate = (d: string) =>
  new Date(d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });

// Date + time, e.g. "01 Jun 2025, 10:15 AM". Falls back to the raw value if unparseable.
export const formatDateTime = (d?: string | null) => {
  if (!d) return '—';
  const dt = parseTs(d);
  if (isNaN(dt.getTime())) return d;
  return dt.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
};

// Date + time ALWAYS in Indian Standard Time, e.g. "01 Jun 2025, 10:15 AM IST", regardless of
// the viewer's device timezone. Backend sends UTC (…Z). Used where IST is a stated requirement.
export const formatDateTimeIST = (d?: string | null) => {
  if (!d) return '—';
  const dt = parseTs(d);
  if (isNaN(dt.getTime())) return d;
  return dt.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }) + ' IST';
};

export const CHART_DATA = [
  { day: 'Mon', deposit: 125000, withdrawal: 45000 },
  { day: 'Tue', deposit: 98000, withdrawal: 32000 },
  { day: 'Wed', deposit: 210000, withdrawal: 88000 },
  { day: 'Thu', deposit: 175000, withdrawal: 55000 },
  { day: 'Fri', deposit: 290000, withdrawal: 120000 },
  { day: 'Sat', deposit: 145000, withdrawal: 40000 },
  { day: 'Sun', deposit: 88000, withdrawal: 25000 },
];
