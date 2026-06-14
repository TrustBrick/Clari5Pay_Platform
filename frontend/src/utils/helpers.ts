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
  };
  return map[s] || { color: T.textMuted, bg: T.borderLight };
};

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
