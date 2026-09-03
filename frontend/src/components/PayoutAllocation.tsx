import React from 'react';
import { T } from '../utils/theme';
import { Icon } from './Icon';
import { BankLogo } from './BankLogo';
import { fmt } from '../utils/helpers';
import type { PayoutLeg } from '../types';

// THE payout allocation card. Every surface that shows where a withdrawal is paid FROM renders
// this one component — merchant details, the Manager's review, the Admin's pay screen — so the
// account, the amount and the transaction mode read identically wherever they appear.
//
// It displays a decision the BACKEND made. The withdrawal allocation engine chooses the paying
// account(s) the moment a request is raised (services/withdrawal_allocation); nothing here ranks,
// filters or re-checks anything, and there is no code path by which this component could change
// which account pays. That is deliberate: the rules live in one place, on the server.
//
// What it deliberately does NOT show is any capacity figure — the account's daily debit limit,
// what it has used today, what it has left, its balance. Those are internal and reach the Admin
// through their own endpoint (GET /api/transactions/{id}/payout-allocation), never through the
// payload a merchant receives.

const MODE_LABEL: Record<string, string> = {
  BANK: 'Bank Transfer', IMPS: 'IMPS', NEFT: 'NEFT', RTGS: 'RTGS', UPI: 'UPI',
};

const STATUS_STYLE: Record<string, { label: string; color: string; bg: string }> = {
  ALLOCATED: { label: 'Allocated', color: T.blue, bg: T.infoBg },
  PAID: { label: 'Paid', color: T.green, bg: T.successBg },
  RELEASED: { label: 'Released', color: T.textMuted, bg: T.canvas },
};

const Row: React.FC<{ leg: PayoutLeg; showAmount: boolean }> = ({ leg, showAmount }) => {
  const status = STATUS_STYLE[leg.status] || STATUS_STYLE.ALLOCATED;
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0',
      borderTop: `1px solid ${T.border}`,
    }}>
      <BankLogo name={leg.bankName || ''} ifsc={leg.ifsc || undefined} withName={false} size={26} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: T.textMain }}>
          {leg.bankName || leg.accountName || leg.accountRef}
          {leg.accountNumber ? <span style={{ color: T.textMuted, fontWeight: 600 }}> · A/C {leg.accountNumber}</span> : null}
        </p>
        <p style={{ margin: '2px 0 0', fontSize: 11, color: T.textMuted }}>
          {[leg.ifsc && `IFSC: ${leg.ifsc}`, leg.branch, leg.accountType].filter(Boolean).join(' · ') || '—'}
        </p>
      </div>
      <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
        {showAmount && (
          <p style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>{fmt(leg.amount)}</p>
        )}
        <span style={{
          display: 'inline-block', marginTop: showAmount ? 3 : 0, padding: '2px 7px', borderRadius: 6,
          fontSize: 10, fontWeight: 800, letterSpacing: '0.04em', textTransform: 'uppercase',
          color: status.color, background: status.bg,
        }}>{status.label}</span>
      </div>
    </div>
  );
};

export const PayoutAllocationPanel: React.FC<{
  legs?: PayoutLeg[] | null;
  /** The withdrawal's own amount, so a split can be reconciled against it at a glance. */
  amount?: number | null;
  /** The requested transaction mode, shown once for the whole allocation. */
  mode?: string | null;
  /** Rendered when the engine has not placed this withdrawal (the exception case). */
  emptyNote?: React.ReactNode;
  compact?: boolean;
}> = ({ legs, amount, mode, emptyNote, compact }) => {
  const rows = legs || [];
  if (!rows.length) {
    if (!emptyNote) return null;
    return (
      <div style={{
        background: T.warningBg, border: `1px solid ${T.warning}`, borderRadius: 10,
        padding: '10px 12px', marginBottom: 14, display: 'flex', gap: 8, alignItems: 'flex-start',
      }}>
        <Icon name="warning" size={14} />
        <div style={{ fontSize: 12, color: T.textMain }}>{emptyNote}</div>
      </div>
    );
  }

  const total = rows.reduce((sum, l) => sum + (l.amount || 0), 0);
  const split = rows.length > 1;
  const modeLabel = MODE_LABEL[String(mode || rows[0]?.transactionMode || '').toUpperCase()]
    || rows[0]?.transactionMode || mode;

  return (
    <div style={{
      background: T.canvas, borderRadius: 10, padding: compact ? '10px 12px' : 12, marginBottom: 14,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
        <p style={{
          fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase',
          letterSpacing: '0.05em', margin: 0,
        }}>
          {split ? `Payout Allocation · ${rows.length} accounts` : 'Payout Account'}
        </p>
        {modeLabel ? (
          <span style={{ fontSize: 11, fontWeight: 700, color: T.textMuted }}>{modeLabel}</span>
        ) : null}
      </div>

      {rows.map(leg => <Row key={`${leg.accountRef}-${leg.legNo}`} leg={leg} showAmount={split} />)}

      {/* A split is only correct if its legs add up to the withdrawal, so the total is stated
          rather than left to be worked out. It always equals the requested amount — the engine
          refuses to allocate at all when it cannot cover it exactly. */}
      {split && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          paddingTop: 10, borderTop: `2px solid ${T.border}`, marginTop: 2,
        }}>
          <span style={{ fontSize: 12, fontWeight: 800, color: T.textMuted }}>Total</span>
          <span style={{ fontSize: 15, fontWeight: 800, color: T.textMain }}>{fmt(total)}</span>
        </div>
      )}
      {!split && amount != null && (
        <p style={{ margin: '6px 0 0', fontSize: 12, fontWeight: 700, color: T.textMain }}>{fmt(amount)}</p>
      )}
    </div>
  );
};
