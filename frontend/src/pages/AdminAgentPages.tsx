import React, { useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { fmt, formatDateTimeIST } from '../utils/helpers';
import { Card, Btn, Input, Sel, Modal, LoadingScreen, Pager } from '../components/UI';
import { usePoll, useDebouncedValue } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { type IconName } from '../components/Icon';
// The Merchant Agent module's own presentation, imported rather than reproduced: the same cards,
// section headings, balance-overview tiles, status badges, table styling and detail fields. The
// two dashboards therefore cannot drift apart — a change to the Agent Dashboard's look lands here
// with it. What is deliberately NOT imported is any action control: nothing on these pages
// submits, approves, rejects, uploads, edits or completes anything.
import {
  StatusPill, STATUS_FILTER_OPTIONS, METHOD_LABEL, methodLabel, thS, tdS,
  DASH_CARD, figure, DashIcon, FinCard, BoTile, BoOp, DashSection, DField, SlipView,
  PAGE_SIZE, instrLabel, isTokenMethod,
} from './AgentTxnPages';
import {
  adminAgentAPI, type AdminAgentOverview, type AdminAgentRow, type AdminAgentTxnRow,
  type AdminAgentQuery,
} from '../services/adminAgentTxns';
import type { AgentTxnAuditRow } from '../services/agentTxns';

// ─── Admin Portal → Agent Management (read-only) ───────────────────────────────
// Monitoring only. Every figure comes from /api/admin/agent-txns, which is GET-only and reuses the
// Merchant Agent Dashboard's aggregation across all merchant businesses — there is no second
// statistics system here. Admins cannot act on an agent transaction: the Agent Module's write
// endpoints admit merchant operator roles only, so hiding buttons is the presentation of that
// rule, never the enforcement of it.

/** The read-only banner every Agent Management page carries, so the Admin knows what this is. */
const ReadOnlyNote: React.FC<{ children?: React.ReactNode }> = ({ children }) => (
  <div style={{
    display: 'flex', alignItems: 'center', gap: 8, padding: '9px 14px', borderRadius: 10,
    background: `${T.blue}12`, border: `1px solid ${T.blue}33`, fontSize: 12, color: T.textMuted,
    marginBottom: 18,
  }}>
    <span style={{ fontSize: 11, fontWeight: 800, color: T.blue, letterSpacing: '0.05em' }}>READ ONLY</span>
    <span>{children || 'Agent Module activity across every merchant. Agent transactions are operated in the Merchant Portal — this view never changes one.'}</span>
  </div>
);

/** The merchant business filter, shared by all three pages. */
const BusinessFilter: React.FC<{ value: string; onChange: (v: string) => void; options: string[]; style?: React.CSSProperties }> =
  ({ value, onChange, options, style }) => (
  <Sel label="Merchant" value={value} onChange={e => onChange(e.target.value)} style={{ marginBottom: 0, ...style }}
    options={[{ value: '', label: 'All Merchants' }, ...options.map(b => ({ value: b, label: b }))]} />
);

/** Merchant businesses that have agent activity — fetched once per page. */
const useBusinesses = () => {
  const [businesses, setBusinesses] = useState<string[]>([]);
  useEffect(() => { adminAgentAPI.businesses().then(setBusinesses).catch(() => {}); }, []);
  return businesses;
};

// ─── Agent Dashboard (Admin) ───────────────────────────────────────────────────
// The Merchant Agent Dashboard's three sections — Operational Summary, Financial Summary, Balance
// Overview — over every merchant, plus the two things only an Admin monitoring all agents needs:
// the agent estate (total / active / inactive) and a per-merchant breakdown.
export const AdminAgentDashboardPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ onNavigate }) => {
  const { showToast } = useToast();
  const businesses = useBusinesses();
  const [business, setBusiness] = useState('');
  const [data, setData] = useState<AdminAgentOverview | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null);
  const load = useCallback(() => {
    adminAgentAPI.overview(business || undefined)
      .then(d => { setData(d); setRefreshedAt(new Date().toISOString()); })
      .catch(() => showToast('Failed to load Agent Management.', 'error'));
  }, [business, showToast]);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load());

  if (!data) return <LoadingScreen label="Loading Agent Management…" />;
  const c = data.cards; const o = data.performance.overall; const a = data.agents;
  const netDeposits = Math.round((o.totalDepositAmount - o.totalDepositCommission) * 100) / 100;
  const totalWithdrawals = Math.round((o.totalWithdrawalAmount + o.totalWithdrawalCommission) * 100) / 100;
  const totalSettlements = Math.round((o.totalSettlementAmount + o.totalSettlementCommission) * 100) / 100;
  const available = Math.round((netDeposits - totalWithdrawals - totalSettlements) * 100) / 100;

  // Same colour key as the Merchant Agent Dashboard: green deposits/completed, red
  // withdrawals/rejected, orange pending, purple settlements.
  const agentCards: Array<[string, number, string, IconName]> = [
    ['Total Agents', a.total, T.blue, 'agent'],
    ['Active Agents', a.active, T.success, 'completed-requests'],
    ['Inactive Agents', a.inactive, T.textMuted, 'transaction-rejected'],
    ['Merchants With Agents', a.merchants, T.blue, 'merchants'],
  ];
  const opsCards: Array<[string, number, string, IconName]> = [
    ['Total Transactions', c.totalTransactions, T.blue, 'transactions'],
    ['Total Deposit Requests', c.depositCount, T.success, 'total-deposits'],
    ['Total Withdrawal Requests', c.withdrawalCount, T.danger, 'total-withdrawals'],
    ['Total Settlement Requests', c.settlementCount, '#7c3aed', 'total-settlements'],
    ['Pending Requests', c.pending, T.warning, 'pending-requests'],
    ['Completed Requests', c.completed, T.success, 'completed-requests'],
    ['Rejected Requests', c.rejected, T.danger, 'transaction-rejected'],
    ['Created Today', c.today, T.blue, 'today-transactions'],
  ];

  return (
    <div style={{ maxWidth: 1280 }}>
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ margin: '0 0 5px', fontSize: 26, fontWeight: 800, color: T.textMain, letterSpacing: '-0.02em' }}>Agent Dashboard</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: T.textMuted }}>Agent operational status, financial summary and available balance — completed transactions.</p>
      </div>
      <ReadOnlyNote />

      <Card style={{ padding: 16, marginBottom: 24 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 12 }}>
          <BusinessFilter value={business} onChange={setBusiness} options={businesses} />
        </div>
      </Card>

      {/* Section 1 — the agent estate */}
      <DashSection title="Agent Estate" note={business || 'all merchants'} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 16, marginBottom: 36 }}>
        {agentCards.map(([label, value, color, icon]) => (
          <Card key={label} style={DASH_CARD}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
              <DashIcon name={icon} color={color} />
              <p style={{ margin: 0, fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', lineHeight: 1.3 }}>{label}</p>
            </div>
            <p style={{ margin: 0, fontSize: 30, fontWeight: 800, color: figure(value, color), letterSpacing: '-0.02em' }}>{value}</p>
          </Card>
        ))}
      </div>

      {/* Section 2 — Operational Summary (counts only) */}
      <DashSection title="Operational Summary" />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 16, marginBottom: 36 }}>
        {opsCards.map(([label, value, color, icon]) => (
          <Card key={label} style={DASH_CARD}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
              <DashIcon name={icon} color={color} />
              <p style={{ margin: 0, fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', lineHeight: 1.3 }}>{label}</p>
            </div>
            <p style={{ margin: 0, fontSize: 30, fontWeight: 800, color: figure(value, color), letterSpacing: '-0.02em' }}>{value}</p>
          </Card>
        ))}
      </div>

      {/* Section 3 — Financial Summary (identical to the Merchant Agent Dashboard) */}
      <DashSection title="Financial Summary" note="completed transactions" />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(250px,1fr))', gap: 16, marginBottom: 36 }}>
        <FinCard title="Total Deposits" icon="total-deposits" accent={T.success} a={['Gross Deposit Amount', o.totalDepositAmount]} b={['Deposit Commission', o.totalDepositCommission]} />
        <FinCard title="Total Withdrawals" icon="total-withdrawals" accent={T.danger} a={['Gross Withdrawal Amount', o.totalWithdrawalAmount]} b={['Withdrawal Commission', o.totalWithdrawalCommission]} />
        <FinCard title="Total Settlements" icon="total-settlements" accent={'#7c3aed'} a={['Gross Settlement Amount', o.totalSettlementAmount]} b={['Settlement Commission', o.totalSettlementCommission]} />
        <FinCard title="Total Commission Earned" icon="available-balance" accent={T.blue} a={['Total Commission Earned', o.totalCommission]} b={['Total Completed Transactions', o.totalTransactions]} bMoney={false} />
      </div>

      {/* Section 4 — Balance Overview (the same transparent calculation) */}
      <DashSection title="Balance Overview" />
      <Card style={{ padding: 22, marginBottom: 36 }}>
        <div style={{ display: 'flex', gap: 14, alignItems: 'stretch', flexWrap: 'wrap' }}>
          <BoTile label="Total Net Deposits" sub="Deposit Amount − Deposit Commission" value={netDeposits} color={T.success} icon="deposits-received" />
          <BoOp symbol="−" />
          <BoTile label="Total Withdrawals" sub="Withdrawal Amount + Commission" value={totalWithdrawals} color={T.danger} icon="total-withdrawals" />
          <BoOp symbol="−" />
          <BoTile label="Total Settlements" sub="Settlement Amount + Commission" value={totalSettlements} color={'#7c3aed'} icon="total-settlements" />
          <BoOp symbol="=" />
          <BoTile label="Current Available Balance" sub="Across all completed agent transactions" value={available} color={T.success} icon="available-balance" big />
        </div>
      </Card>

      {/* Section 5 — per-merchant breakdown (Admin only: a merchant never sees another's rows) */}
      <DashSection title="By Merchant" note="agent activity per merchant business" />
      <Card style={{ overflow: 'hidden', marginBottom: 36 }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>
              {['Merchant', 'Total', 'Deposits', 'Withdrawals', 'Settlements', 'Pending', 'Completed', 'Rejected', 'Completed Amount'].map(h => <th key={h} style={thS}>{h}</th>)}
            </tr></thead>
            <tbody>
              {data.byMerchant.length === 0 && <tr><td colSpan={9} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent activity yet.</td></tr>}
              {data.byMerchant.map((m, i) => (
                <tr key={m.business} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontWeight: 700 }}>{m.business}</td>
                  <td style={tdS}>{m.total}</td>
                  <td style={tdS}>{m.deposits}</td>
                  <td style={tdS}>{m.withdrawals}</td>
                  <td style={tdS}>{m.settlements}</td>
                  <td style={{ ...tdS, color: m.pending ? T.warning : T.textMuted, fontWeight: 700 }}>{m.pending}</td>
                  <td style={{ ...tdS, color: m.completed ? T.success : T.textMuted, fontWeight: 700 }}>{m.completed}</td>
                  <td style={{ ...tdS, color: m.rejected ? T.danger : T.textMuted, fontWeight: 700 }}>{m.rejected}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(m.completedAmount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Section 6 — the most recent activity, with a link into the full feed */}
      <DashSection title="Recent Activity" note="latest 10 agent transactions" />
      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>
              {['Reference', 'Merchant', 'Agent', 'Membership', 'Type', 'Amount', 'Status', 'Created (IST)'].map(h => <th key={h} style={thS}>{h}</th>)}
            </tr></thead>
            <tbody>
              {data.recent.length === 0 && <tr><td colSpan={8} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions yet.</td></tr>}
              {data.recent.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.merchantBusiness || '—'}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{x.membershipId || '—'}</td>
                  <td style={tdS}>{x.type.charAt(0) + x.type.slice(1).toLowerCase()}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} type={x.type} method={x.txnMethod} approverRole={x.approverRole} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {onNavigate && (
          <div style={{ padding: 12, display: 'flex', justifyContent: 'flex-end', borderTop: `1px solid ${T.border}` }}>
            <Btn size="sm" variant="ghost" onClick={() => onNavigate('admin-agent-txns')}>View all agent transactions →</Btn>
          </div>
        )}
      </Card>

      <p style={{ margin: '20px 0 0', fontSize: 12, color: T.textMuted, textAlign: 'right' }}>
        Last Updated: <span style={{ fontWeight: 700, color: T.textMain }}>{refreshedAt ? formatDateTimeIST(refreshedAt) : '—'}</span>
      </p>
    </div>
  );
};

// ─── Agent Transaction details (Admin, read-only) ──────────────────────────────
// The same grouped detail sections, payment evidence and audit history the Merchant module shows,
// with every action control absent. "Performed By" is answered from the fields the workflow
// already records plus the existing audit trail — no new audit system.
const AdminAgentTxnModal: React.FC<{ row: AdminAgentTxnRow; onClose: () => void }> = ({ row, onClose }) => {
  const [audit, setAudit] = useState<AgentTxnAuditRow[]>([]);
  useEffect(() => { adminAgentAPI.audit(row.id).then(setAudit).catch(() => {}); }, [row.id]);

  const sections: Array<{ title: string; fields: Array<[string, React.ReactNode]> }> = [
    { title: 'Transaction Information', fields: [
      ['Reference Number', row.referenceNumber],
      ['Transaction Code', row.transactionCode],
      ['Merchant', row.merchantBusiness],
      ['Type', row.type],
      ['Transaction Type', methodLabel(row.txnMethod)],
      ['Status', <StatusPill status={row.status} type={row.type} method={row.txnMethod} approverRole={row.approverRole} />],
      ['Amount', fmt(row.amount)],
      ['Commission', row.commissionAmount == null ? null : `${fmt(row.commissionAmount)} (${row.commissionPct}%)`],
      ['Net Amount', row.netAmount == null ? null : fmt(row.netAmount)],
      ['Unique Note Number', row.noteNumber],
      ['Token Details', row.tokenDetails],
      ['Crypto Wallet Address', row.walletAddress],
      ['UTR Number', row.depositUtr],
      ['Reference Number (Member)', row.memberReference],
      ['Instructions', row.instructions ? instrLabel(row.instructions) : null],
      ['Notes', row.notes],
    ] },
    { title: 'Agent', fields: [
      ['Agent ID', row.agentCode],
      ['Agent Name', row.agentName],
      ['Agent Category', row.agentCategory],
      ['Agent Location', [row.agentLocation, row.agentState, row.agentCountry].filter(Boolean).join(', ') || null],
    ] },
    { title: 'Member Information', fields: [
      ['Membership ID', row.membershipId],
      ['Membership Name', row.membershipName],
      ['Membership Type', row.membershipType],
      ['Mobile', row.mobile],
    ] },
    // Who did what, and when — every performer the workflow records.
    { title: 'Performed By', fields: [
      ['Created By (Operator)', row.createdBy],
      ['Created (IST)', `${row.createdDate || ''} ${row.createdTime || ''}`.trim() || null],
      ['Account / Token Submitted By', row.accountSubmittedBy],
      ['Account / Token Submitted (IST)', row.accountSubmittedDate ? `${row.accountSubmittedDate} ${row.accountSubmittedTime || ''}` : null],
      ['Sent For Approval', row.sentForApproval ? 'Yes' : 'No'],
      ['Authorized Approver', row.approverName ? `${row.approverName}${row.approverRole ? ` (${row.approverRole})` : ''}` : null],
      ['Supervisor', row.supervisorName],
      ['Manager', row.managerName],
      ['Review Remark', row.reviewRemark],
      ['Approved By', row.approvedBy],
      ['Approved (IST)', row.approvedDate ? `${row.approvedDate} ${row.approvedTime || ''}` : null],
      ['Proof Uploaded By', row.slipSubmittedBy],
      ['Proof Uploaded (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
      ['Deposited By', row.depositedBy],
      ['Deposited (IST)', row.depositedDate ? `${row.depositedDate} ${row.depositedTime || ''}` : null],
      ['Last Updated By', row.updatedBy],
      ['Completed (IST)', row.completedDate ? `${row.completedDate} ${row.completedTime || ''}` : null],
    ] },
    { title: 'Payment Routing', fields: [
      ['Sent To (Agent A/C)', row.agentAccountRef ? `${row.agentAccountRef} · ${row.agentAccountDetail || ''}` : null],
      ['Sending Account', [row.senderAccountHolder, row.senderAccountNumber || row.senderUpiId, row.senderBankName].filter(Boolean).join(' · ') || null],
      ['Paid To', [row.payoutAccountHolder, row.payoutAccountNumber || row.payoutUpiId, row.payoutBankName].filter(Boolean).join(' · ') || null],
    ] },
  ];

  const proofLabel = isTokenMethod(row.txnMethod) ? 'Token Image' : 'Uploaded Slip';
  const images: Array<[string, string]> = [
    ...(row.accountProof ? [['Account Proof', row.accountProof] as [string, string]] : []),
    ...(row.slipImage ? [[proofLabel, row.slipImage] as [string, string]] : []),
    ...(row.depositProof ? [['Deposit Proof', row.depositProof] as [string, string]] : []),
  ];

  const has = (v: React.ReactNode) => !(v === null || v === undefined || v === '' || v === '—');

  return (
    <Modal title={`Agent ${row.type.charAt(0) + row.type.slice(1).toLowerCase()} — ${row.referenceNumber}`} onClose={onClose} wide>
      {sections.map(sec => {
        const fs = sec.fields.filter(([, v]) => has(v));
        if (!fs.length) return null;
        return (
          <div key={sec.title} style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>{sec.title}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
              {fs.map(([k, v]) => <DField key={k} k={k} v={v} />)}
            </div>
          </div>
        );
      })}

      {images.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Payment Evidence</div>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {images.map(([label, src]) => (
              <SlipView key={label} label={label} src={src} filename={`${row.referenceNumber}-${label.toLowerCase().replace(/\s+/g, '-')}`} />
            ))}
          </div>
        </div>
      )}

      <h3 style={{ margin: '0 0 8px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Audit History</h3>
      <div style={{ overflowX: 'auto', border: `1px solid ${T.border}`, borderRadius: 10, marginBottom: 16 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ background: T.canvas }}>{['Date & Time (IST)', 'Action', 'Old', 'New', 'Performed By', 'Note'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
          <tbody>
            {audit.length === 0 && <tr><td colSpan={6} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 18 }}>No history yet.</td></tr>}
            {audit.map(x => (
              <tr key={x.id} style={{ background: T.surface }}>
                <td style={{ ...tdS, whiteSpace: 'nowrap', color: T.textMuted }}>{x.createdDate} {x.createdTime}</td>
                <td style={{ ...tdS, fontWeight: 700 }}>{x.action.replace(/_/g, ' ')}</td>
                <td style={tdS}>{x.oldAmount == null ? '—' : fmt(x.oldAmount)}</td>
                <td style={tdS}>{x.newAmount == null ? '—' : fmt(x.newAmount)}</td>
                <td style={tdS}>{x.actor || '—'}{x.role ? ` (${x.role})` : ''}</td>
                <td style={{ ...tdS, color: T.textMuted }}>{x.note || (x.approverName ? `→ ${x.approverName}` : '—')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}><Btn variant="secondary" onClick={onClose}>Close</Btn></div>
    </Modal>
  );
};

// ─── Agent Transactions (Admin, read-only) ─────────────────────────────────────
export const AdminAgentTransactionsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const businesses = useBusinesses();
  const [rows, setRows] = useState<AdminAgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailRow, setDetailRow] = useState<AdminAgentTxnRow | null>(null);
  const [business, setBusiness] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [type, setType] = useState('');
  const [method, setMethod] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const debouncedSearch = useDebouncedValue(search, 400);

  const load = useCallback(async (opts?: { page?: number; pageSize?: number }) => {
    const p = opts?.page ?? page;
    const ps = opts?.pageSize ?? pageSize;
    setLoading(true);
    try {
      const q: AdminAgentQuery = { page: p, page_size: ps };
      if (business) q.business = business;
      if (status) q.status = status;
      if (type) q.txn_type = type;
      if (method) q.txn_method = method;
      if (debouncedSearch.trim()) q.search = debouncedSearch.trim();
      if (fromF) q.date_from = fromF;
      if (toF) q.date_to = toF;
      const res = await adminAgentAPI.listPaged(q);
      setRows(res.items);
      setTotal(res.total);
      setTotalPages(Math.max(1, res.totalPages));
    } catch { showToast('Failed to load agent transactions.', 'error'); }
    finally { setLoading(false); }
  }, [business, status, type, method, debouncedSearch, fromF, toF, page, pageSize, showToast]);

  useEffect(() => { load(); }, [page, pageSize, business, status, type, method, debouncedSearch, fromF, toF]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setPage(1); }, [business, status, type, method, debouncedSearch, fromF, toF]);

  const clearFilters = () => {
    setBusiness(''); setSearch(''); setStatus(''); setType(''); setMethod(''); setFromF(''); setToF(''); setPage(1);
  };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Transactions</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Every Agent Deposit, Withdrawal and Settlement in the isolated Agent ledger, across all merchants.</p>
      </div>
      <ReadOnlyNote>View only — Agent transactions are created, approved and completed by the merchant's own operators.</ReadOnlyNote>

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(170px,1fr))', gap: 12 }}>
          <BusinessFilter value={business} onChange={setBusiness} options={businesses} />
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent / Operator" style={{ marginBottom: 0 }} />
          <Sel label="Request Type" value={type} onChange={e => setType(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All' }, { value: 'DEPOSIT', label: 'Deposit' }, { value: 'WITHDRAWAL', label: 'Withdrawal' }, { value: 'SETTLEMENT', label: 'Settlement' }]} />
          <Sel label="Transaction Type" value={method} onChange={e => setMethod(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All' }, ...Object.keys(METHOD_LABEL).map(v => ({ value: v, label: METHOD_LABEL[v] }))]} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, ...STATUS_FILTER_OPTIONS]} />
          <Input label="From Date" type="date" value={fromF} onChange={e => setFromF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="To Date" type="date" value={toF} onChange={e => setToF(e.target.value)} style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <Btn size="sm" onClick={() => { setPage(1); load({ page: 1 }); }} disabled={loading}>{loading ? 'Searching…' : 'Apply Filters'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters}>Clear</Btn>
          <span style={{ fontSize: 12, color: T.textMuted, marginLeft: 'auto' }}>{total} transaction{total === 1 ? '' : 's'}</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                {['Reference', 'Merchant', 'Agent', 'Membership', 'Request', 'Transaction Type', 'Amount',
                  'Status', 'Operator', 'Reviewer', 'Created (IST)', 'Completed (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={13} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && rows.length === 0 && <tr><td colSpan={13} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions match the filters.</td></tr>}
              {rows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.merchantBusiness || '—'}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{x.membershipId || '—'}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={tdS}>{x.type.charAt(0) + x.type.slice(1).toLowerCase()}</td>
                  <td style={tdS}>{methodLabel(x.txnMethod)}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} type={x.type} method={x.txnMethod} approverRole={x.approverRole} /></td>
                  <td style={tdS}>{x.createdBy || '—'}</td>
                  <td style={tdS}>{x.managerName || x.supervisorName || x.approverName || '—'}</td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.completedDate ? `${x.completedDate} ${x.completedTime || ''}` : '—'}</td>
                  <td style={tdS}><Btn size="sm" variant="ghost" onClick={() => setDetailRow(x)}>View Details</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pager page={page} pageSize={pageSize} total={total} totalPages={totalPages} loading={loading}
          onPage={setPage} onPageSize={n => { setPageSize(n); setPage(1); }} />
      </Card>

      {detailRow && <AdminAgentTxnModal row={detailRow} onClose={() => setDetailRow(null)} />}
    </div>
  );
};

// ─── Agents (Admin, read-only) ─────────────────────────────────────────────────
// Every agent with its lifetime performance — the same completed-only, per-leg calculation the
// Merchant module uses, listed across all merchants.
export const AdminAgentsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const businesses = useBusinesses();
  const [business, setBusiness] = useState('');
  const [search, setSearch] = useState('');
  const [rows, setRows] = useState<AdminAgentRow[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    adminAgentAPI.agents(business || undefined)
      .then(d => setRows(d.agents))
      .catch(() => showToast('Failed to load agents.', 'error'))
      .finally(() => setLoading(false));
  }, [business, showToast]);
  useEffect(() => { load(); }, [load]);

  const q = search.trim().toLowerCase();
  const shown = q
    ? rows.filter(r => [r.agentId, r.agentName, r.merchantBusiness, r.category, r.location]
        .some(v => String(v || '').toLowerCase().includes(q)))
    : rows;

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agents</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Every agent and its lifetime business — completed transactions only, commission per leg.</p>
      </div>
      <ReadOnlyNote>View only — agents are created and maintained by the merchant in the Merchant Portal.</ReadOnlyNote>

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 12 }}>
          <BusinessFilter value={business} onChange={setBusiness} options={businesses} />
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Agent ID / Name / Category" style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center' }}>
          <span style={{ fontSize: 12, color: T.textMuted, marginLeft: 'auto' }}>{shown.length} agent{shown.length === 1 ? '' : 's'}</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                {['Agent ID', 'Agent Name', 'Merchant', 'Category', 'Status', 'Deposits', 'Deposit Amount',
                  'Withdrawals', 'Withdrawal Amount', 'Settlements', 'Total Commission', 'Transactions',
                  'Last Transaction (IST)'].map(h => <th key={h} style={thS}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={13} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && shown.length === 0 && <tr><td colSpan={13} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agents match the filters.</td></tr>}
              {shown.map((r, i) => (
                <tr key={r.agentMasterId} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{r.agentId}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{r.agentName}</td>
                  <td style={tdS}>{r.merchantBusiness || '—'}</td>
                  <td style={tdS}>{r.category}</td>
                  <td style={tdS}>
                    <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, whiteSpace: 'nowrap',
                      color: String(r.status).toUpperCase() === 'ACTIVE' ? T.success : T.textMuted,
                      background: String(r.status).toUpperCase() === 'ACTIVE' ? `${T.success}18` : T.borderLight }}>{r.status}</span>
                  </td>
                  <td style={tdS}>{r.depositCount}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(r.depositAmount)}</td>
                  <td style={tdS}>{r.withdrawalCount}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(r.withdrawalAmount)}</td>
                  <td style={tdS}>{r.settlementCount}</td>
                  <td style={{ ...tdS, fontWeight: 700, color: r.totalCommission ? T.blue : T.textMuted }}>{fmt(r.totalCommission)}</td>
                  <td style={tdS}>{r.totalTransactions}</td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{r.lastTransactionDate || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};
