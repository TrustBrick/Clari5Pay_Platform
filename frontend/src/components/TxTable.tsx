import React from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel } from '../utils/helpers';
import { Badge, Btn } from './UI';
import type { Transaction } from '../types';

type ActionMode = 'check' | 'view' | 'none';

interface TxTableProps {
  txns: Transaction[];
  onAction?: (t: Transaction, action: string) => void;
  actionMode?: ActionMode;
}

const typeColor = (type: string): { color: string; bg: string } => {
  if (type.startsWith('DEPOSIT')) return { color: T.success, bg: T.successBg };
  if (type.startsWith('WITHDRAWAL')) return { color: T.danger, bg: T.dangerBg };
  return { color: T.info, bg: T.infoBg };
};

const TxTable: React.FC<TxTableProps> = ({ txns, onAction, actionMode = 'none' }) => (
  <div style={{ overflowX: 'auto' }}>
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
      <thead>
        <tr style={{ background: T.canvas }}>
          {['Reference Number', 'Merchant Name', 'Type', 'Amount', 'Date', 'Status', 'Action'].map(h => (
            <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {txns.length === 0 && (
          <tr><td colSpan={7} style={{ padding:32,textAlign:'center',color:T.textMuted,fontSize:13 }}>No transactions found</td></tr>
        )}
        {txns.map((t, i) => {
          const tc = typeColor(t.type);
          return (
            <tr key={t.id} style={{ background: i % 2 === 0 ? T.surface : '#f8faff' }}>
              <td style={{ padding:'11px 14px' }}>
                <code style={{ fontSize:11,background:T.canvas,padding:'2px 6px',borderRadius:4,fontWeight:700 }}>{t.ref}</code>
              </td>
              <td style={{ padding:'11px 14px',color:T.textMain,fontWeight:700 }}>{t.merchant}</td>
              <td style={{ padding:'11px 14px' }}>
                <span style={{ padding:'2px 8px',borderRadius:6,fontSize:11,fontWeight:700,background:tc.bg,color:tc.color,whiteSpace:'nowrap' }}>
                  {typeLabel(t.type)}
                </span>
              </td>
              <td style={{ padding:'11px 14px',fontWeight:800,color:T.textMain }}>{fmt(t.amount)}</td>
              <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{t.date} <span style={{ fontSize:10 }}>{t.time}</span></td>
              <td style={{ padding:'11px 14px' }}><Badge status={t.status}/></td>
              <td style={{ padding:'11px 14px' }}>
                {onAction && actionMode === 'check' && (
                  <Btn size="sm" onClick={() => onAction(t, 'check')}>✓ Check</Btn>
                )}
                {onAction && actionMode === 'view' && (
                  <Btn size="sm" variant="ghost" onClick={() => onAction(t, 'view')}>👁 View</Btn>
                )}
                {actionMode === 'none' && <span style={{ color:T.textLight }}>—</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

export default TxTable;
