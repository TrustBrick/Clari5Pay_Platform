import React from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, depositTypeLabel, memberLabel } from '../utils/helpers';
import { Badge, Btn, TableSkeleton } from './UI';
import { Icon, type IconName } from './Icon';
import type { Transaction } from '../types';

type ActionMode = 'admin' | 'merchant' | 'view' | 'none';

interface TxTableProps {
  txns: Transaction[];
  onAction?: (t: Transaction, action: string) => void;
  actionMode?: ActionMode;
  viewerRole?: string;
  loading?: boolean;
}

// Pick the per-row action button based on mode + transaction type + status.
const rowAction = (mode: ActionMode, status: string, type: string): { label: string; action: string; variant: 'primary' | 'ghost'; icon: IconName } | null => {
  const isDeposit = type.startsWith('DEPOSIT');
  if (mode === 'admin') {
    // Deposits reach the admin (SLIP_SUBMITTED) only after Supervisor approval; withdrawals/
    // settlements (SLIP_SUBMITTED) only after Manager approval. Legacy withdrawals may still
    // sit in ACCOUNT_REQUESTED.
    if (isDeposit && status === 'ACCOUNT_REQUESTED') return { label: 'Choose Account', action: 'manage', variant: 'primary', icon: 'bank' };
    if (isDeposit && status === 'SLIP_SUBMITTED') return { label: 'Mark Deposited', action: 'manage', variant: 'primary', icon: 'approve' };
    if (!isDeposit && (status === 'ACCOUNT_REQUESTED' || status === 'SLIP_SUBMITTED')) return { label: 'Pay & Complete', action: 'manage', variant: 'primary', icon: 'amount' };
    return { label: 'View Details', action: 'view', variant: 'ghost', icon: 'view' };
  }
  if (mode === 'merchant') {
    // Slip upload: awaiting payment (ACCOUNT_SUBMITTED) or returned by a Supervisor (RESUBMITTED).
    if (isDeposit && status === 'ACCOUNT_SUBMITTED') return { label: 'Pay / Submit Proof', action: 'slip', variant: 'primary', icon: 'upload' };
    if (isDeposit && status === 'RESUBMITTED') return { label: 'Re-submit Proof', action: 'slip', variant: 'primary', icon: 'refresh' };
    return { label: 'View Details', action: 'view', variant: 'ghost', icon: 'view' };
  }
  if (mode === 'view') return { label: 'View', action: 'view', variant: 'ghost', icon: 'view' };
  return null;
};

// Click-to-copy control for reference numbers (transient ✓ feedback).
const CopyRef: React.FC<{ value: string }> = ({ value }) => {
  const [copied, setCopied] = React.useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1200); } catch { /* ignore */ }
  };
  return (
    <button onClick={copy} title="Copy reference number" aria-label="Copy reference number"
      style={{ border:'none',background:'transparent',cursor:'pointer',color:copied?T.success:T.textMuted,lineHeight:1,padding:'0 2px',display:'inline-flex',alignItems:'center' }}>
      <Icon name={copied ? 'approve' : 'copy'} size={13} />
    </button>
  );
};

const typeColor = (type: string): { color: string; bg: string } => {
  if (type.startsWith('DEPOSIT')) return { color: T.success, bg: T.successBg };
  if (type.startsWith('WITHDRAWAL')) return { color: T.danger, bg: T.dangerBg };
  return { color: T.info, bg: T.infoBg };
};

const TxTable: React.FC<TxTableProps> = ({ txns, onAction, actionMode = 'none', viewerRole, loading }) => {
  // No action handler (e.g. the dashboard preview) → drop the Action column entirely.
  const showAction = !!onAction && actionMode !== 'none';
  const headers = ['Reference Number', (viewerRole === 'ADMIN' || viewerRole === 'SUPER_ADMIN') ? 'Receiver Name' : 'Merchant Name', 'Membership - Member', 'Type', 'Amount', 'Date', 'Status'];
  if (showAction) headers.push('Action');
  if (loading) return <div style={{ overflowX: 'auto' }}><TableSkeleton rows={6} cols={headers.length} /></div>;
  return (
  <div style={{ overflowX: 'auto' }}>
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
      <thead>
        <tr style={{ background: T.canvas }}>
          {headers.map(h => (
            <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {txns.length === 0 && (
          <tr><td colSpan={headers.length} style={{ padding:32,textAlign:'center',color:T.textMuted,fontSize:13 }}>No transactions found</td></tr>
        )}
        {txns.map((t, i) => {
          const tc = typeColor(t.type);
          return (
            <tr key={t.id} className="c5-row-in"
              style={{ background: i % 2 === 0 ? T.surface : '#f8faff', ['--c5-i' as any]: Math.min(i, 12) }}>
              <td style={{ padding:'11px 14px' }}>
                <span style={{ display:'inline-flex',alignItems:'center',gap:6 }}>
                  <span style={{ fontWeight:700,color:T.textMain }}>{t.ref}</span>
                  <CopyRef value={t.ref} />
                </span>
              </td>
              <td style={{ padding:'11px 14px',color:T.textMain,fontWeight:700 }}>{t.merchant}</td>
              <td style={{ padding:'11px 14px',color:T.textMain,fontWeight:600 }}>{memberLabel(t.memberId, t.member)}</td>
              <td style={{ padding:'11px 14px' }}>
                <span style={{ padding:'2px 8px',borderRadius:6,fontSize:11,fontWeight:700,background:tc.bg,color:tc.color,whiteSpace:'nowrap' }}>
                  {typeLabel(t.type)}
                </span>
                {t.type.startsWith('DEPOSIT') && t.depositType && (
                  <div style={{ fontSize:10,color:T.textMuted,marginTop:3 }}>{depositTypeLabel(t.depositType)}</div>
                )}
              </td>
              <td style={{ padding:'11px 14px',fontWeight:800,color:T.textMain }}>{fmt(t.amount)}</td>
              <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{t.date} <span style={{ fontSize:10 }}>{t.time}</span></td>
              <td style={{ padding:'11px 14px' }}>
                <Badge status={t.status} type={t.type} viewerRole={viewerRole} approverRole={t.approverRole}/>
                {t.highRisk && (
                  <span style={{ display:'inline-flex',alignItems:'center',gap:3,marginLeft:6,padding:'2px 8px',borderRadius:6,fontSize:10,fontWeight:800,background:'#fdecea',color:'#b71c1c',whiteSpace:'nowrap',letterSpacing:'0.04em' }}><Icon name="warning" size={11} weight="fill" /> HIGH RISK</span>
                )}
              </td>
              {showAction && (
                <td style={{ padding:'11px 14px' }}>
                  {(() => {
                    const a = rowAction(actionMode, t.status, t.type);
                    if (!a) return <span style={{ color:T.textLight }}>—</span>;
                    return <Btn size="sm" variant={a.variant} onClick={() => onAction!(t, a.action)}><Icon name={a.icon} size={14} /> {a.label}</Btn>;
                  })()}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
  );
};

export default TxTable;
