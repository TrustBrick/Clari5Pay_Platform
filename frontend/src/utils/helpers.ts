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
// `approverRole` is the role actually recorded against the approval — always prefer it. The
// type-derived fallback only covers rows approved before the role was captured; it is an
// assumption ("a deposit must have been a Supervisor") and is wrong whenever the request was
// sent to an approver of a different role, which is exactly what this pair of arguments fixes.
export const clientApproverLabel = (type?: string | null, approverRole?: string | null): string => {
  const actual = merchantRoleLabel(String(approverRole || '').toUpperCase());
  if (actual) return actual;
  const t = String(type || '').toUpperCase();
  if (t.startsWith('DEPOSIT')) return 'Supervisor';
  if (t.startsWith('WITHDRAWAL') || t.startsWith('SETTLEMENT')) return 'Manager';
  return '—';
};

// INTERNAL-ONLY counterpart to clientApproverLabel, for Admin / Super Admin screens which ARE
// entitled to see the person. Shows "Name (Role)" — the role still resolved by
// clientApproverLabel, so the internal and client views can never disagree about WHICH role
// acted; this only ADDS the name the client view withholds.
// Falls back to the role alone when no name was recorded, so a row never reads worse than before.
// NEVER call this from a client-facing view — that is exactly what clientApproverLabel is for.
export const internalApproverLabel = (
  approvedBy?: string | null, type?: string | null, approverRole?: string | null,
): string => {
  const person = String(approvedBy || '').trim();
  const role = clientApproverLabel(type, approverRole);
  if (!person) return role;
  return role && role !== '—' ? `${person} (${role})` : person;
};

// The PERSON who approved, on the merchant's side.
//
// It deliberately reads neither `approvedBy` nor the supervisor/manager name columns, because
// neither identifies a person:
//   • `approvedBy` is written by whoever last touched the request. The Admin's account-send and
//     card-link steps overwrite it with the ADMIN's name, and deposit auto-allocation writes
//     "System (Auto Allocation)" — which is how "System (Auto Allocation) (Manager)" appeared.
//   • `supervisorName` / `managerName` store the reviewer's `name`, and for a merchant user that
//     is the BUSINESS. Every operator at one client shares it, so it can say BELLAGIO but never
//     which person at BELLAGIO approved.
// `approverUsername` / `approverFullName` are stamped by the review gate itself and are the only
// fields that identify the individual.
//
// When no person was recorded the row shows the ROLE alone. That is the honest reading: the
// merchant's workflow recorded no approver, and naming the Admin or the business there would be
// asserting something untrue rather than merely unhelpful. The Admin keeps its own column.
const SYSTEM_ACTORS = new Set(['system (auto allocation)', 'system']);

export const merchantApproverName = (row: {
  approverFullName?: string | null; approverUsername?: string | null;
  approvedBy?: string | null; processedBy?: string | null;
  merchant?: string | null; business?: string | null;
}): string => {
  // Stamped by the review gate, and only ever for a merchant-side reviewer.
  const stamped = String(row.approverFullName || '').trim()
    || String(row.approverUsername || '').trim();
  if (stamped) return stamped;

  // Nothing stamped — this row predates the field. `approvedBy` often still holds the right
  // person (it is what the column showed before), so it is used, but ONLY once the three ways it
  // is known to hold something else are ruled out. Dropping it outright would throw away a
  // correct name on every historical row; trusting it blindly is what produced
  // "System (Auto Allocation) (Manager)" and the Admin appearing in both columns.
  const legacy = String(row.approvedBy || '').trim();
  if (!legacy) return '';
  if (SYSTEM_ACTORS.has(legacy.toLowerCase())) return '';            // the allocation engine
  if (legacy.toLowerCase() === String(row.processedBy || '').trim().toLowerCase()) return '';
  const businessName = String(row.merchant || row.business || '').trim().toLowerCase();
  if (businessName && legacy.toLowerCase() === businessName) return '';  // the business, not a person
  return legacy;
};

// "Name (Role)" for that person, for INTERNAL screens entitled to see it. Falls back to the role
// alone — never to a business or an Admin name.
export const merchantApproverLabel = (row: {
  type?: string | null; approverRole?: string | null;
  approverFullName?: string | null; approverUsername?: string | null;
  approvedBy?: string | null; processedBy?: string | null;
  merchant?: string | null; business?: string | null;
}): string => internalApproverLabel(merchantApproverName(row), row.type, row.approverRole);

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
    // Nobody can pay until an Admin frees capacity — it reads as the blocker it is.
    NO_ELIGIBLE_ACCOUNT: { color: T.danger, bg: T.dangerBg },
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

// ── Card Deposit status wording ────────────────────────────────────────────────────────────────
// A Card deposit runs on the EXISTING deposit statuses — no new status was introduced — but the
// first two hops are spoken about differently by the people who work it: the Admin sends a payment
// link rather than an account. This map is presentation only; the stored status, the workflow, the
// permissions and every other deposit type are untouched.
//   ACCOUNT_REQUESTED  → Link Requested
//   ACCOUNT_SUBMITTED  → Link Submitted
//   RESUBMITTED        → Link Submitted   (returned for correction — the same phase, and the
//                                          resubmission reason is shown alongside it)
//
// The review and approved rungs are deliberately NOT in this map: they must name the person the
// request was actually sent to (Supervisor Review / Manager Review), which displayStatus already
// resolves from approver_role before the label is produced. Mapping them to a fixed
// "Manager/Supervisor …" string threw that resolution away and made every Card request read the
// same whoever was reviewing it.
const CARD_STATUS_LABELS: Record<string, string> = {
  ACCOUNT_REQUESTED: 'Link Requested',
  ACCOUNT_SUBMITTED: 'Link Submitted',
  RESUBMITTED: 'Link Submitted',
};

// Role- and type-aware status label.
// Deposit: Account Requested → Account Submitted → Slip Submitted → Deposited.
// Withdrawal/Settlement: Submitted (merchant) / Pending (admin) → Completed.
// A Card deposit (`depositType='CARD'`) uses its own wording for the shared statuses — see above.
// `approverRole` names who the request was sent to, so a Card deposit reads "Supervisor Approved"
// or "Manager Approved" on the rung between the review and the operator's Mark Deposit.
export const statusLabel = (status: string, type?: string, viewerRole?: string, depositType?: string | null, approverRole?: string | null): string => {
  const isDeposit = !!type && type.startsWith('DEPOSIT');
  if (isDeposit && String(depositType || '').toUpperCase() === CARD_TXN_TYPE) {
    const card = CARD_STATUS_LABELS[status];
    if (card) return card;
    // Reviewer-approved, awaiting the operator's Mark Deposit. SLIP_SUBMITTED is the shared
    // "approved by the reviewer" status; name the reviewer who actually approved it, falling back
    // to the deposit gate (Supervisor) when no approver was recorded.
    if (status === 'SLIP_SUBMITTED') {
      const who = merchantRoleLabel(reviewerRoleCode(type, approverRole, 'SUPERVISOR')) || 'Supervisor';
      return `${who} Approved`;
    }
  }
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

// Which role a review-gate row on the Approval Record belongs to.
// The reviewer's NAME is stored in the gate's slot (supervisor_name for a deposit, manager_name
// for a withdrawal), but under "Send To Approval" the person who acted may hold a different role
// — a Manager can approve a deposit. `approverRole` records who actually acted, so prefer it.
// Deliberately narrow: it applies ONLY to the gate that reviews this transaction type. A
// settlement's supervisor_name is its CREATOR (settlements skip the review gate), so a settlement
// keeps the plain gate label and is never relabelled from approverRole.
export const reviewerRoleCode = (
  txType?: string | null, approverRole?: string | null, gate: 'SUPERVISOR' | 'MANAGER' = 'SUPERVISOR',
): string => {
  const t = String(txType || '').toUpperCase();
  const gateReviewsThisType = gate === 'SUPERVISOR' ? t.startsWith('DEPOSIT') : t.startsWith('WITHDRAWAL');
  const actual = String(approverRole || '').toUpperCase();
  return gateReviewsThisType && actual ? actual : gate;
};

// Display label for an audit-log action CODE (e.g. "SUPERVISOR_APPROVED"). The review-gate codes —
// <gate>_APPROVED | _REJECTED | _RESUBMITTED — store the GATE the request passed through, but under
// "Send To Approval" the person who actually acted may hold a different role (a Manager can approve a
// deposit). Those codes are relabelled to the actual approver's role via reviewerRoleCode, so the
// history reads "Manager Approved" when a Manager approved it — never hardcoded. This also resolves
// historical rows already stored with the gate name, so no data backfill is needed for display.
// Every other action code is returned unchanged, so no unrelated audit row's wording shifts.
const REVIEW_AUDIT_ACTION = /^(SUPERVISOR|MANAGER)_(APPROVED|REJECTED|RESUBMITTED)$/;
export const auditActionLabel = (action?: string | null, type?: string | null, approverRole?: string | null): string => {
  const a = String(action || '');
  const m = a.match(REVIEW_AUDIT_ACTION);
  if (!m) return a;
  const gate = m[1] as 'SUPERVISOR' | 'MANAGER';
  const role = reviewerRoleCode(type, approverRole, gate);
  const decision = m[2].charAt(0) + m[2].slice(1).toLowerCase();
  return `${merchantRoleLabel(role) || gate} ${decision}`;
};

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
// A chat bubble's time, in IST — the timezone the conversation is stamped in. Shape comes from
// the shared formatter, so it matches every other time in the product.
export const chatTime = (iso: string) => formatTime(iso, { ist: true });
/** The day separator in a conversation. "Today"/"Yesterday" are kept — a relative label is more
 *  useful than a date here — and any other day prints the standard date. */
export const chatDateLabel = (iso: string): string => {
  // en-CA gives YYYY-MM-DD, used ONLY to compare which IST day two instants fall on. This is a
  // comparison key, never displayed, so it is deliberately not the display format.
  const key = (d: Date) => d.toLocaleDateString('en-CA', { timeZone: IST_TZ });
  const k = key(new Date(iso));
  if (k === key(new Date())) return 'Today';
  if (k === key(new Date(Date.now() - 86400000))) return 'Yesterday';
  return formatDate(iso, { ist: true });
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
  CARD: 'Credit/Debit Card',
  CDM: 'CDM (Cash Deposit Machine)',
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
  { value: 'CARD', label: 'Credit/Debit Card' },
  { value: 'CDM', label: 'CDM (Cash Deposit Machine)' },
];

// ── CDM (Cash Deposit Machine) ─────────────────────────────────────────────────────────────────
// Physical cash pushed into a machine. It shares the whole Deposit lifecycle with every other
// type, and differs in exactly two places: the automatic allocation engine never picks its
// receiving account (an Admin assigns one, because the payer walks to a machine later), and it
// cannot be completed until an Admin has confirmed the ACTUAL bank credit. Both of those are
// enforced on the server; this constant only drives what the forms ask for.
export const CDM_TXN_TYPE = 'CDM';
/** True for a deposit raised with Deposit Type = CDM. */
export const isCdmDeposit = (tx: { type?: string | null; depositType?: string | null }): boolean =>
  String(tx.type || '').toUpperCase().startsWith('DEPOSIT') &&
  String(tx.depositType || '').toUpperCase() === CDM_TXN_TYPE;

// The Admin's CDM verification checklist. Each entry is a distinct thing a HUMAN compared — the
// system never ticks one on the Admin's behalf. Mirrors app.services.cdm.CHECKS, which is what
// the server actually enforces; this list only decides what the form renders and in what order.
// `bankCreditConfirmed` is the control that releases the money: a receipt can be edited, an
// actual credit in the assigned account cannot.
export const CDM_CHECKS = [
  { key: 'receiptVerified', label: 'Receipt Verified' },
  { key: 'amountMatches', label: 'Amount Matches' },
  { key: 'accountMatches', label: 'Bank / Account Matches' },
  { key: 'dateChecked', label: 'Date / Time Checked' },
  { key: 'referenceChecked', label: 'CDM Reference Checked' },
  { key: 'bankCreditConfirmed', label: 'Actual Bank Credit Confirmed' },
] as const;

// ── Transaction types temporarily withheld from the operator request forms ─────────────────────
// Cash and Crypto are not currently offered to the Data / Deposit / Withdrawal Operators on the
// MERCHANT Deposit Type and Payout Mode selectors — those two move through the Agent Module
// instead, where they stay fully available.
//
// This hides the OPTIONS ONLY. Nothing is deleted or disabled: the Cash/Crypto branches of both
// request forms, their member-detail fields, proof upload, APIs, transaction statuses and database
// columns are all untouched, every existing Cash/Crypto transaction still displays and processes
// exactly as before, and any other role (e.g. a merchant with no operator role) keeps the full
// list. Emptying WITHHELD_TXN_TYPES re-offers both types immediately.
export const WITHHELD_TXN_TYPES = ['CASH', 'CRYPTO'];
const WITHHELD_TXN_TYPE_ROLES = ['DEO', 'DEPOSIT_OPERATOR', 'WITHDRAWAL_OPERATOR'];

// ── Card Deposit — offered to the Data Operator and Deposit Operator only ──────────────────────
// The inverse of the withheld list above: instead of hiding a type from certain roles, CARD is
// shown to these roles alone and hidden from every other. The backend applies the same rule on
// create, so this is the selector half of one restriction, not the restriction itself.
export const CARD_TXN_TYPE = 'CARD';
const CARD_TXN_TYPE_ROLES = ['DEO', 'DEPOSIT_OPERATOR'];
/** True for a deposit raised with Transaction Type = Card. */
export const isCardDeposit = (tx: { type?: string | null; depositType?: string | null }): boolean =>
  String(tx.type || '').toUpperCase().startsWith('DEPOSIT') &&
  String(tx.depositType || '').toUpperCase() === CARD_TXN_TYPE;

/** `options` filtered to the transaction types this merchant role may currently pick. */
export const txnTypeOptionsFor = <O extends { value: string }>(options: O[], merchantRole?: string | null): O[] => {
  const role = String(merchantRole || '').toUpperCase();
  const withheld = WITHHELD_TXN_TYPE_ROLES.includes(role) ? WITHHELD_TXN_TYPES : [];
  // CARD only exists in the Deposit Type list, so filtering it here is a no-op for payout modes.
  const cardHidden = CARD_TXN_TYPE_ROLES.includes(role) ? [] : [CARD_TXN_TYPE];
  const hide = [...withheld, ...cardHidden];
  return hide.length ? options.filter(o => !hide.includes(String(o.value).toUpperCase())) : options;
};

// ── Crypto Balance module ──────────────────────────────────────────────────────
// A transaction is "crypto" iff its deposit leg is CRYPTO or its withdrawal payout mode is
// CRYPTO — the same predicate the backend uses in compute_balance / tx_class filtering.
// Settlements are never crypto. Used to badge/identify crypto rows in Transaction History
// while they still display inline alongside business transactions (never hidden, never
// mixed into business totals).
export const isCryptoTx = (tx: { depositType?: string | null; payoutMode?: string | null }): boolean =>
  String(tx.depositType || '').toUpperCase() === 'CRYPTO' ||
  String(tx.payoutMode || '').toUpperCase() === 'CRYPTO';

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
// ─── Payment proof / slip files ────────────────────────────────────────────────
// Accepted types and the per-file size limit mirror app.core.uploads on the server — the server
// is the authority, this only lets an obviously bad file fail fast and locally. There is
// deliberately NO limit on HOW MANY files may be attached to a request; a payment can need one
// slip or a dozen, and dropping the eleventh would leave the record unable to show how the money
// actually moved.
export const PROOF_ACCEPT = 'image/jpeg,image/jpg,image/png,application/pdf,.jpg,.jpeg,.png,.pdf';
export const PROOF_MAX_BYTES = 5 * 1024 * 1024;
export const PROOF_TYPE_MSG = 'Unsupported file type. Allowed: JPG, JPEG, PNG, PDF.';
export const PROOF_SIZE_MSG = 'Each file must be 5 MB or smaller.';
export const isAllowedProof = (f: File): boolean => {
  const t = (f.type || '').toLowerCase();
  if (['image/jpeg', 'image/jpg', 'image/png', 'application/pdf'].includes(t)) return true;
  return /\.(jpe?g|png|pdf)$/i.test(f.name);
};

// Split a set of proofs into request-sized batches.
//
// The reverse proxy caps a single request body at 12 MB (see Caddyfile), and base64 inflates a
// file by about a third. Without this, attaching a genuinely large set would fail at the proxy
// with an opaque error and the user would have no way to attach them at all — the count limit
// would simply have moved from the application to the network. Instead the first batch travels
// with the submission and the rest are appended afterwards, so "as many as required" holds.
const PROOF_BATCH_BYTES = 8 * 1024 * 1024;
export const proofBatches = (proofs: string[]): string[][] => {
  const batches: string[][] = [];
  let current: string[] = [];
  let size = 0;
  for (const p of proofs) {
    // A single file over the batch budget still goes on its own — the server decides whether
    // it is too large, and it will say so clearly.
    if (current.length && size + p.length > PROOF_BATCH_BYTES) { batches.push(current); current = []; size = 0; }
    current.push(p);
    size += p.length;
  }
  if (current.length) batches.push(current);
  return batches.length ? batches : [[]];
};

// Attach the batches that did not fit the primary request, and report whether all of them
// landed. By the time this runs the primary call has SUCCEEDED — the request exists, or the slip
// is submitted, or the payout is complete — so a failure here is a partial attachment, not a
// failed submission, and must never be reported as one. Returns false so the caller can tell the
// user exactly which of the two happened; the files that did land are already on the record.
export const attachRemainingProofs = async (
  batches: string[][], send: (batch: string[]) => Promise<unknown>,
): Promise<boolean> => {
  for (const batch of batches) {
    try { await send(batch); } catch { return false; }
  }
  return true;
};

export const PARTIAL_ATTACH_MSG =
  'Some files could not be attached — open the request and upload them again.';

// The proofs to display for a transaction, newest schema first: the array if the row has one,
// otherwise the single legacy column (rows written before multi-file uploads existed).
export const proofList = (many?: string[] | null, one?: string | null): string[] =>
  (many && many.length) ? many : (one ? [one] : []);

// Is this proof a PDF? A stored file reaches the browser either as a base64 data URL (legacy
// rows) or as a presigned object-storage link, so the type has to be read from whichever form
// arrived — a PDF shown through an <img> is just a broken image.
export const isPdfProof = (src?: string | null): boolean =>
  !!src && (src.startsWith('data:application/pdf') || /\.pdf(\?|$)/i.test(src.split('#')[0]));

// A sensible filename for one downloaded proof: the right extension, and an index when the
// request carries several, so a user saving all of them does not overwrite the same file.
export const proofFileName = (src: string, ref: string, i: number, total: number, kind = 'slip'): string => {
  const ext = isPdfProof(src) ? 'pdf'
    : (src.match(/^data:image\/(jpeg|jpg|png|webp)/) || src.split('#')[0].split('?')[0].match(/\.(jpe?g|png|webp)$/i) || [])[1]
        ?.toLowerCase().replace('jpeg', 'jpg') || 'png';
  return `${kind}-${ref}${total > 1 ? `-${i + 1}` : ''}.${ext}`;
};

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
  // Past a week a relative label stops being useful, so this becomes an ACTUAL date — and an
  // actual date takes the standard shape, year included.
  return formatDate(iso);
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

// ─── THE date/time formatter ────────────────────────────────────────────────────────────────────
// ONE shape for every actual date/time the platform shows a user, in every portal:
//
//     04 Sep 2026, 04:06 PM
//
// Built from Intl PARTS rather than from a locale's own string, on purpose. `toLocaleString`
// output is not a contract — the separator, the digit padding and the case of am/pm all vary by
// ICU version and by browser, so the same build rendered "04 Sep 2026, 4:06 pm" on one machine
// and "04 Sep 2026, 04:06 PM" on another. Assembling the parts ourselves makes the output the
// same everywhere, which is the entire point of standardising it.
//
// TIMEZONE IS DELIBERATELY NOT CHANGED HERE. `formatDateTime` renders in the viewer's own
// timezone exactly as it always has; `formatDateTimeIST` / `formatDateTimeInIST` render in IST
// exactly as they always have. This function standardises the SHAPE of a timestamp, never which
// moment it refers to.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = (n: number) => String(n).padStart(2, '0');

/** The wall-clock fields of `dt` in `tz` (or the viewer's own zone when tz is undefined). */
const wallClock = (dt: Date, tz?: string) => {
  const parts = new Intl.DateTimeFormat('en-US', {
    ...(tz ? { timeZone: tz } : {}),
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hour12: false,
  }).formatToParts(dt);
  const get = (t: string) => Number(parts.find(p => p.type === t)?.value ?? 0);
  // hourCycle h23 reports midnight as 24 in some engines; normalise it to 0.
  return { y: get('year'), mo: get('month'), d: get('day'), h: get('hour') % 24, mi: get('minute') };
};

/** "04 Sep 2026" */
const datePart = (c: ReturnType<typeof wallClock>) => `${pad2(c.d)} ${MONTHS[c.mo - 1]} ${c.y}`;
/** "04:06 PM" — 12-hour, zero-padded hour, uppercase meridiem. */
const timePart = (c: ReturnType<typeof wallClock>) =>
  `${pad2(c.h % 12 === 0 ? 12 : c.h % 12)}:${pad2(c.mi)} ${c.h < 12 ? 'AM' : 'PM'}`;

/** The platform's empty-value convention. A null/blank/unparseable timestamp never renders as
 *  "Invalid Date" — it renders as nothing-to-show, the same em dash used everywhere else. */
export const EMPTY_VALUE = '—';

/** Parse anything the API or a form may hand us into a Date, or null if it is not a timestamp.
 *  Accepts ISO with or without a zone (a zoneless date-TIME is read as UTC, which is what the
 *  backend sends), plain dates, and epoch numbers. */
const toDate = (v?: string | number | Date | null): Date | null => {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  if (typeof v === 'number') { const d = new Date(v); return isNaN(d.getTime()) ? null : d; }
  const raw = String(v).trim();
  if (!raw) return null;
  const d = parseTs(raw);
  return isNaN(d.getTime()) ? null : d;
};

type DtOpts = {
  /** Render in IST rather than the viewer's own timezone. */
  ist?: boolean;
  /** Append " IST" — for screens that state the timezone as part of the value. */
  suffix?: boolean;
  /** What to show when there is no usable timestamp. Defaults to the em dash. */
  empty?: string;
};

/** "04 Sep 2026, 04:06 PM" — the standard display format for an actual date/time. */
export const formatDateTime = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  const c = wallClock(dt, opts.ist ? 'Asia/Kolkata' : undefined);
  return `${datePart(c)}, ${timePart(c)}${opts.suffix ? ' IST' : ''}`;
};

/** "04 Sep 2026" — the date half, same shape, for where a time would be noise. */
export const formatDate = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  return datePart(wallClock(dt, opts.ist ? 'Asia/Kolkata' : undefined));
};

/** "04:06 PM" — the time half, for a chat bubble or anywhere the date is already stated. */
export const formatTime = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  return timePart(wallClock(dt, opts.ist ? 'Asia/Kolkata' : undefined));
};

/** IST with the zone stated: "04 Sep 2026, 04:06 PM IST". Used where IST is a requirement of the
 *  screen rather than an implementation detail. Unchanged behaviour, standardised shape. */
export const formatDateTimeIST = (v?: string | number | Date | null) =>
  formatDateTime(v, { ist: true, suffix: true });

/** IST WITHOUT the suffix — for screens whose own label already says "(IST)", so the zone is not
 *  printed twice. Same moment as formatDateTimeIST. */
export const formatDateTimeInIST = (v?: string | number | Date | null) =>
  formatDateTime(v, { ist: true });

/** Reshape the Agent API's IST display PARTS into the standard format.
 *
 * That API returns each timestamp pre-split and pre-converted to IST — `createdDate` as
 * "2026-09-04" and `createdTime` as "04:06:47 PM" — and for several workflow steps it returns
 * ONLY those parts, with no ISO instant alongside. So this restates strings that are already in
 * the right timezone; it does no parsing into a Date and no timezone conversion, which is exactly
 * why it cannot shift the moment being displayed. Seconds are dropped, matching every other
 * timestamp in the product.
 *
 * Returns the empty convention when either half is missing, so a step that has not happened yet
 * never renders as a half-formed date.
 */
/** The TIME half of the Agent API's IST parts: "04:06:47 PM" or "16:06:47" -> "04:06 PM".
 *
 * For the few exports that keep Date and Time as SEPARATE columns — restructuring those into one
 * column would change the shape of a file people already import elsewhere, so each half is
 * standardised in place instead. Like `formatIstParts`, this restates an already-IST string and
 * performs no timezone conversion.
 */
export const formatIstTime = (time?: string | null): string => {
  const t = (time || '').trim();
  if (!t) return EMPTY_VALUE;
  const m = /^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$/.exec(t);
  if (!m) return EMPTY_VALUE;
  let h = Number(m[1]);
  const mer = m[3] ? m[3].toUpperCase() : (h < 12 ? 'AM' : 'PM');
  if (!m[3]) h = h % 12 === 0 ? 12 : h % 12;
  return `${pad2(h)}:${m[2]} ${mer}`;
};

export const formatIstParts = (date?: string | null, time?: string | null): string => {
  const d = (date || '').trim();
  const t = (time || '').trim();
  if (!d) return EMPTY_VALUE;
  const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);
  // An unrecognised value is an invalid timestamp, and an invalid timestamp takes the empty
  // convention rather than being echoed back — a date column showing a raw string reads as a
  // rendering bug to an operator, and "no usable date" is the honest thing to say.
  if (!dm) return EMPTY_VALUE;
  const datePiece = `${dm[3]} ${MONTHS[Number(dm[2]) - 1]} ${dm[1]}`;
  if (!t) return datePiece;
  const tm = /^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$/.exec(t);
  // A date we can read with a time we cannot: show the date, drop the unreadable half.
  if (!tm) return datePiece;
  let h = Number(tm[1]);
  const mer = tm[3] ? tm[3].toUpperCase() : (h < 12 ? 'AM' : 'PM');
  if (!tm[3]) h = h % 12 === 0 ? 12 : h % 12;            // 24-hour input -> 12-hour
  return `${datePiece}, ${pad2(h)}:${tm[2]} ${mer}`;
};

/** "Just now" / "12 min ago" / "3 hr ago" / "Yesterday 04:06 PM" / "04 Sep 2026, 04:06 PM".
 *
 * Presence-style relative time, shared by the Active Users and Support Management screens (which
 * each carried their own byte-identical copy). Past two days a relative label stops meaning
 * anything, so it falls through to the standard absolute format — and the "Yesterday" case keeps
 * its label while printing the time in the standard shape.
 */
export const relativeTime = (iso?: string | null): string => {
  if (!iso) return EMPTY_VALUE;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return EMPTY_VALUE;
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 45) return 'Just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hr ago`;
  if (s < 172800) return `Yesterday ${formatTime(iso)}`;
  return formatDateTime(iso);
};

/**
 * A one-shot idempotency key for a financial submit (withdrawal completion, manual adjustment).
 *
 * Minted ONCE when a form opens and sent with the request, so a double-click, an impatient retry
 * or a dropped-response replay all carry the SAME key: the backend recognises it and returns the
 * entry it already wrote instead of debiting or adjusting a second time. `crypto.randomUUID` is
 * used where available, with a plain random fallback for older browsers.
 */
/**
 * A bank account number shown the way a statement shows it: the last four digits behind bullets
 * (`••••8890`). Used wherever an account is being *identified* rather than
 * entered — the payout picker, the adjustment dialog, the ledger. Short or empty values are
 * returned untouched, since there is nothing to hide.
 */
export const maskAccount = (acc?: string | null): string => {
  const s = (acc || '').trim();
  if (s.length <= 4) return s;
  return '••••' + s.slice(-4);
};

export const newRequestId = (): string => {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch { /* fall through */ }
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
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
