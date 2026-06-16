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
    ACCOUNT_SUBMITTED: { color: T.success, bg: T.successBg },
  };
  return map[s] || { color: T.textMuted, bg: T.borderLight };
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

export const today = () => new Date().toISOString().split('T')[0];
export const nowTime = () => new Date().toTimeString().split(' ')[0];

export const formatDate = (d: string) =>
  new Date(d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });

export const CHART_DATA = [
  { day: 'Mon', deposit: 125000, withdrawal: 45000 },
  { day: 'Tue', deposit: 98000, withdrawal: 32000 },
  { day: 'Wed', deposit: 210000, withdrawal: 88000 },
  { day: 'Thu', deposit: 175000, withdrawal: 55000 },
  { day: 'Fri', deposit: 290000, withdrawal: 120000 },
  { day: 'Sat', deposit: 145000, withdrawal: 40000 },
  { day: 'Sun', deposit: 88000, withdrawal: 25000 },
];
