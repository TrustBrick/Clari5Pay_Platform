import React from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel } from '../utils/helpers';
import { Badge, Btn } from './UI';
import type { Transaction } from '../types';

type ActionMode = 'admin' | 'merchant' | 'view' | 'none';

interface TxTableProps {
  txns: Transaction[];
  onAction?: (t: Transaction, action: string) => void;
  actionMode?: ActionMode;
  viewerRole?: string;
}

// Pick the per-row action button based on mode + transaction type + status.
const rowAction = (mode: ActionMode, status: string, type: string): { label: string; action: string; variant: 'primary' | 'ghost' } | null => {
  const isDeposit = type.startsWith('DEPOSIT');
  if (mode === 'admin') {
    if (isDeposit && status === 'ACCOUNT_REQUESTED') return { label: '🏦 Choose Account', action: 'manage', variant: 'primary' };
    if (isDeposit && status === 'SLIP_SUBMITTED') return { label: '✓ Mark Deposited', action: 'manage', variant: 'primary' };
    if (!isDeposit && status === 'ACCOUNT_REQUESTED') return { label: '💳 Pay & Complete', action: 'manage', variant: 'primary' };
    return { label: '👁 View', action: 'view', variant: 'ghost' };
  }
  if (mode === 'merchant') {
    if (isDeposit && status === 'ACCOUNT_SUBMITTED') return { label: '⇪ Pay / Submit Proof', action: 'slip', variant: 'primary' };
    return { label: '👁 View', action: 'view', variant: 'ghost' };
  }
  if (mode === 'view') return { label: '👁 View', action: 'view', variant: 'ghost' };
  return null;
};

const typeColor = (type: string): { color: string; bg: string } => {
  if (type.startsWith('DEPOSIT')) return { color: T.success, bg: T.successBg };
  if (type.startsWith('WITHDRAWAL')) return { color: T.danger, bg: T.dangerBg };
  return { color: T.info, bg: T.infoBg };
};

const TxTable: React.FC<TxTableProps> = ({ txns, onAction, actionMode = 'none', viewerRole }) => {
  // No action handler (e.g. the dashboard preview) → drop the Action column entirely.
  const showAction = !!onAction && actionMode !== 'none';
  const headers = ['Reference Number', (viewerRole === 'ADMIN' || viewerRole === 'SUPER_ADMIN') ? 'Receiver Name' : 'Merchant Name', 'Member ID', 'Type', 'Amount', 'Date', 'Status'];
  if (showAction) headers.push('Action');
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
            <tr key={t.id} style={{ background: i % 2 === 0 ? T.surface : '#f8faff' }}>
              <td style={{ padding:'11px 14px' }}>
                <span style={{ fontWeight:700,color:T.textMain }}>{t.ref}</span>
              </td>
              <td style={{ padding:'11px 14px',color:T.textMain,fontWeight:700 }}>{t.merchant}</td>
              <td style={{ padding:'11px 14px',color:T.textMuted }}>{t.memberId || '—'}</td>
              <td style={{ padding:'11px 14px' }}>
                <span style={{ padding:'2px 8px',borderRadius:6,fontSize:11,fontWeight:700,background:tc.bg,color:tc.color,whiteSpace:'nowrap' }}>
                  {typeLabel(t.type)}
                </span>
              </td>
              <td style={{ padding:'11px 14px',fontWeight:800,color:T.textMain }}>{fmt(t.amount)}</td>
              <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{t.date} <span style={{ fontSize:10 }}>{t.time}</span></td>
              <td style={{ padding:'11px 14px' }}>
                <Badge status={t.status} type={t.type} viewerRole={viewerRole}/>
                {t.highRisk && (
                  <span style={{ display:'inline-block',marginLeft:6,padding:'2px 8px',borderRadius:6,fontSize:10,fontWeight:800,background:'#fdecea',color:'#b71c1c',whiteSpace:'nowrap',letterSpacing:'0.04em' }}>⚠ HIGH RISK</span>
                )}
              </td>
              {showAction && (
                <td style={{ padding:'11px 14px' }}>
                  {(() => {
                    const a = rowAction(actionMode, t.status, t.type);
                    if (!a) return <span style={{ color:T.textLight }}>—</span>;
                    return <Btn size="sm" variant={a.variant} onClick={() => onAction!(t, a.action)}>{a.label}</Btn>;
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
