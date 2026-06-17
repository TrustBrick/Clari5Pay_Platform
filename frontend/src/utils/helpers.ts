import { T } from './theme';
import type { TxStatus } from '../types';

export const fmt = (n: number) =>
  `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

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
  };
  return map[s] || { color: T.textMuted, bg: T.borderLight };
};

// Role- and type-aware status label.
// Deposit: Account Requested → Account Submitted → Slip Submitted → Deposited.
// Withdrawal/Settlement: Submitted (merchant) / Pending (admin) → Completed.
export const statusLabel = (status: string, type?: string, viewerRole?: string): string => {
  const isDeposit = !!type && type.startsWith('DEPOSIT');
  const isWithdrawOrSettle = !!type && (type.startsWith('WITHDRAWAL') || type.startsWith('SETTLEMENT'));
  if (status === 'COMPLETED') return isDeposit ? 'Deposited' : 'Completed';
  if (status === 'ACCOUNT_REQUESTED' && isWithdrawOrSettle) {
    return viewerRole === 'MERCHANT' ? 'Submitted' : 'Pending';
  }
  return status
    .toLowerCase()
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
};

// Human-readable label for transaction types (handles the *_REQUEST variants).
export const typeLabel = (t: string) =>
  t
    .toLowerCase()
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');

// Country codes for the phone-number dropdown.
export const COUNTRY_CODES = [
  { code: '+91', label: '🇮🇳 +91 India' },
  { code: '+1', label: '🇺🇸 +1 USA/Canada' },
  { code: '+44', label: '🇬🇧 +44 UK' },
  { code: '+61', label: '🇦🇺 +61 Australia' },
  { code: '+971', label: '🇦🇪 +971 UAE' },
  { code: '+65', label: '🇸🇬 +65 Singapore' },
  { code: '+49', label: '🇩🇪 +49 Germany' },
  { code: '+33', label: '🇫🇷 +33 France' },
  { code: '+81', label: '🇯🇵 +81 Japan' },
  { code: '+86', label: '🇨🇳 +86 China' },
  { code: '+92', label: '🇵🇰 +92 Pakistan' },
  { code: '+880', label: '🇧🇩 +880 Bangladesh' },
  { code: '+94', label: '🇱🇰 +94 Sri Lanka' },
  { code: '+27', label: '🇿🇦 +27 South Africa' },
  { code: '+55', label: '🇧🇷 +55 Brazil' },
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

export const CHART_DATA = [
  { day: 'Mon', deposit: 125000, withdrawal: 45000 },
  { day: 'Tue', deposit: 98000, withdrawal: 32000 },
  { day: 'Wed', deposit: 210000, withdrawal: 88000 },
  { day: 'Thu', deposit: 175000, withdrawal: 55000 },
  { day: 'Fri', deposit: 290000, withdrawal: 120000 },
  { day: 'Sat', deposit: 145000, withdrawal: 40000 },
  { day: 'Sun', deposit: 88000, withdrawal: 25000 },
];
