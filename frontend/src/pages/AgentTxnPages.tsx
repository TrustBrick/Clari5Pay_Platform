import React, { useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { fmt, formatIndianAmountInput, parseIndianAmount, fileToDataUrl, downloadDataUrl } from '../utils/helpers';
import { Card, Btn, Input, Sel, Modal, LoadingScreen, PhoneField, SearchSelect } from '../components/UI';
import { COUNTRY_CODES, INDIAN_STATES, isValidWallet } from '../utils/helpers';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { Icon } from '../components/Icon';
import { IfscField } from '../components/IfscField';
import { useIfscAutoFill } from '../utils/useIfscAutoFill';
import { today } from '../utils/helpers';
import { downloadXlsx, INR_NUMFMT } from '../utils/xlsx';
// Shared reporting primitives — the Agent Reports module renders with the SAME table styling,
// tab pills, section titles, export toolbar and print-to-PDF letterhead as the Merchant/Admin
// Reports. Imported (not copied) so the three modules can never drift apart.
import {
  thR, tdR, pill, RSectionTitle, ReportExportBar, printColumnarReport, downloadCsv, DATE_PRESETS,
} from './ReportsPage';
import {
  agentTxnsAPI, agentTxnError, AGENT_FINAL_STATUSES, AGENT_COMPLETED_STATUSES, AGENT_SETTLEMENT_METHODS,
  type AgentOverview, type AgentFormData, type AgentFormAgent, type AgentDepositBody,
  type AgentWithdrawalBody, type AgentMemberLookup, type AgentMemberSummary, type AgentTxnRow,
  type AgentPerformance, type AgentProfile, type AgentTxnCommission,
  type AgentTxnAuditRow, type AgentTxnQuery, type AgentAccountOption, type AgentMemberAccount,
} from '../services/agentTxns';

// ─── Isolated Agent Transaction subsystem — Merchant operator workflow ─────────
// Every figure and record on these pages comes ONLY from /api/agent-txns (the isolated agent
// ledger). Nothing here reads the merchant Deposit/Withdrawal/Settlement/Treasury/Risk/Account/
// Transaction-History data.

const INSTR_LABEL: Record<string, string> = {
  WHATSAPP_ONLY: 'WhatsApp Only', CALL_ONLY: 'Call Only', WHATSAPP_CALL: 'WhatsApp + Call',
  TELEGRAM: 'Telegram', OTHER: 'Other',
  // Retired options — kept for display of legacy records only (no longer offered in the dropdown).
  NO_CALL: 'No Call', HIGH_PRIORITY: 'High Priority',
};
const instrLabel = (v: string) => INSTR_LABEL[v] || v;


// Workflow statuses — same labels as the merchant deposit workflow. PENDING/APPROVED are legacy
// rows created before the chain existed.
const STATUS_STYLE: Record<string, { c: string; bg: string; label: string }> = {
  // Cash names its first two steps after the token, Crypto after the wallet, Bank/UPI after the
  // agent account — each is what the operator actually submits, so one status = one label.
  TOKEN_REQUESTED: { c: T.warning, bg: T.warningBg, label: 'Token Requested' },
  TOKEN_SUBMITTED: { c: T.info, bg: T.infoBg, label: 'Token Submitted' },
  WALLET_REQUESTED: { c: T.warning, bg: T.warningBg, label: 'Wallet Requested' },
  WALLET_SUBMITTED: { c: T.info, bg: T.infoBg, label: 'Wallet Submitted' },
  ACCOUNT_REQUESTED: { c: T.warning, bg: T.warningBg, label: 'Account Requested' },
  ACCOUNT_SUBMITTED: { c: T.info, bg: T.infoBg, label: 'Account Submitted' },
  SLIP_SUBMITTED: { c: T.blue, bg: `${T.blue}18`, label: 'Slip Submitted' },
  SUPERVISOR_APPROVED: { c: '#7c3aed', bg: '#7c3aed18', label: 'Approved by Supervisor' },
  MANAGER_APPROVED: { c: '#7c3aed', bg: '#7c3aed18', label: 'Approved by Manager' },
  MANAGER_REVIEW: { c: '#7c3aed', bg: '#7c3aed18', label: 'Manager Review' },
  DEPOSITED: { c: T.success, bg: T.successBg, label: 'Deposited' },
  COMPLETED: { c: T.success, bg: T.successBg, label: 'Completed' },
  APPROVED: { c: T.success, bg: T.successBg, label: 'Approved' },
  PENDING: { c: T.warning, bg: T.warningBg, label: 'Pending' },
  REJECTED: { c: T.danger, bg: T.dangerBg, label: 'Rejected' },
  // Retired — the deposit slip step is SLIP_SUBMITTED now. Kept so legacy rows still render.
  SUPERVISOR_REVIEW: { c: '#7c3aed', bg: '#7c3aed18', label: 'Supervisor Review' },
  // Settlement chain — the payment itself happens offline; these only record the workflow.
  SETTLEMENT_REQUESTED: { c: T.warning, bg: T.warningBg, label: 'Settlement Requested' },
  SETTLEMENT_ACCEPTED: { c: T.info, bg: T.infoBg, label: 'Settlement Accepted' },
  PROOF_UPLOADED: { c: T.blue, bg: `${T.blue}18`, label: 'Proof Uploaded' },
  SETTLED: { c: T.success, bg: T.successBg, label: 'Settled' },
};

const StatusPill: React.FC<{ status: string; type?: string | null; method?: string | null }> = ({ status, type, method }) => {
  const s = STATUS_STYLE[status] || { c: T.textMuted, bg: T.borderLight, label: status };
  // A cash deposit uploads a token image, not a slip, so its SLIP_SUBMITTED — the awaiting-Supervisor
  // state — reads "Supervisor Review". Same colour/state; only the word "Slip" is wrong for cash.
  const label = (status === 'SLIP_SUBMITTED' && type === 'DEPOSIT' && isTokenMethod(method))
    ? 'Supervisor Review' : s.label;
  return <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, color: s.c, background: s.bg, whiteSpace: 'nowrap' }}>{label}</span>;
};

// Status filter — the chain in workflow order, then the legacy values.
const STATUS_FILTER_OPTIONS = [
  'TOKEN_REQUESTED', 'TOKEN_SUBMITTED', 'WALLET_REQUESTED', 'WALLET_SUBMITTED',
  'ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SLIP_SUBMITTED', 'SUPERVISOR_APPROVED',
  'MANAGER_REVIEW', 'MANAGER_APPROVED',
  'SETTLEMENT_REQUESTED', 'SETTLEMENT_ACCEPTED', 'PROOF_UPLOADED', 'SETTLED',
  'DEPOSITED', 'COMPLETED', 'REJECTED', 'PENDING', 'APPROVED',
].map(v => ({ value: v, label: STATUS_STYLE[v]?.label || v }));

const METHOD_LABEL: Record<string, string> = {
  CASH: 'Cash', UPI: 'UPI', BANK: 'Bank Transfer', IMPS: 'IMPS', NEFT: 'NEFT', RTGS: 'RTGS', CRYPTO: 'Crypto (USDT)',
};
const methodLabel = (v?: string | null) => (v ? METHOD_LABEL[v] || v : '—');
const BANK_LIKE = ['BANK', 'IMPS', 'NEFT', 'RTGS'];
// Cash/Crypto capture their reference at Submit Account (token image / wallet), not at create.
const isTokenMethod = (m?: string | null) => m === 'CASH';
const isWalletMethod = (m?: string | null) => m === 'CRYPTO';
const isSpecialMethod = (m?: string | null) => isTokenMethod(m) || isWalletMethod(m);

// A transaction can only be routed through an agent of the matching category: cash moves through a
// Cash agent, a bank transfer through a Bank Transfer agent, crypto through a Crypto agent. The
// method's category also decides which account types that agent may hold (see ALLOWED_ACCOUNT_TYPES
// in agent_accounts.py). Selecting a method therefore narrows the agent list to that category.
const CATEGORY_LABEL_A: Record<string, string> = { CASH: 'Cash', BANK_TRANSFER: 'Bank Transfer', CRYPTO: 'Crypto' };
// A Settlement's method is dictated by the agent's Category, never chosen. Mirrors
// _CATEGORY_SETTLEMENT_METHOD in backend/app/api/routes/agent_txns.py — keep the two in step.
const SETTLEMENT_METHOD_FOR_CATEGORY: Record<string, string> = { CASH: 'CASH', BANK_TRANSFER: 'BANK', CRYPTO: 'CRYPTO' };

const categoryForMethod = (m?: string | null): string | null => {
  const v = String(m || '').toUpperCase();
  if (!v) return null;
  if (isTokenMethod(v)) return 'CASH';
  if (isWalletMethod(v)) return 'CRYPTO';
  return 'BANK_TRANSFER';
};
const agentsForMethod = <A extends { category?: string | null }>(agents: A[], m?: string | null): A[] => {
  const want = categoryForMethod(m);
  return want ? agents.filter(a => String(a.category || '').toUpperCase() === want) : agents;
};

// The chain's first two steps are named for what the method actually asks the operator to supply —
// mirrors _requested_status / _submitted_status in backend/app/api/routes/agent_txns.py. Keep the
// two in step: the backend rejects an action attempted from the wrong status.
const requestedStatus = (m?: string | null) =>
  isTokenMethod(m) ? 'TOKEN_REQUESTED' : isWalletMethod(m) ? 'WALLET_REQUESTED' : 'ACCOUNT_REQUESTED';
const submittedStatus = (m?: string | null) =>
  isTokenMethod(m) ? 'TOKEN_SUBMITTED' : isWalletMethod(m) ? 'WALLET_SUBMITTED' : 'ACCOUNT_SUBMITTED';

// Membership IDs are uppercase letters + digits only (auto-converted; lowercase/spaces/symbols
// blocked) — the same rule the Merchant Deposit form applies.
const upperAlphaNum = (raw: string) => (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
const normalizeMemberId = upperAlphaNum;   // Membership ID: uppercase letters + digits only
const normalizeNoteNumber = upperAlphaNum; // Unique Note Number: same rule (9ja123 → 9JA123)

// Country / dial-code options from the shared phone-code list (same source as onboarding).
const DIAL_OPTIONS = COUNTRY_CODES.map(c => ({ value: c.code, label: c.label }));
const COUNTRY_OPTIONS = COUNTRY_CODES
  .map(c => c.label.split(' ').slice(2).join(' '))
  .filter((n, i, a) => !!n && a.indexOf(n) === i)
  .sort()
  .map(n => ({ value: n, label: n }));
const STATE_OPTIONS = INDIAN_STATES.map(n => ({ value: n, label: n }));

const thS: React.CSSProperties = { padding: '10px 14px', textAlign: 'left', fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `2px solid ${T.border}` };
const tdS: React.CSSProperties = { padding: '11px 14px', fontSize: 12, color: T.textMain };

// ─── Agent Overview (isolated KPIs / summaries) ────────────────────────────────
export const AgentOverviewPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const [data, setData] = useState<AgentOverview | null>(null);
  const load = useCallback(() => {
    agentTxnsAPI.overview().then(setData).catch(() => showToast('Failed to load Agent Overview', 'error'));
  }, [showToast]);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load());

  if (!data) return <LoadingScreen label="Loading Agent Overview…" />;
  const c = data.cards;
  const kpis: Array<[string, React.ReactNode, string]> = [
    ['Total Transactions', c.totalTransactions, T.blue],
    ['Deposits', <>{c.depositCount} · {fmt(c.depositAmount)}</>, T.success],
    ['Withdrawals', <>{c.withdrawalCount} · {fmt(c.withdrawalAmount)}</>, T.danger],
    ['Settlements', <>{c.settlementCount} · {fmt(c.settlementAmount)}</>, '#7c3aed'],
    ['Pending', c.pending, T.warning],
    ['Approved', c.approved, T.success],
    ['Rejected', c.rejected, T.danger],
    ['Gross Amount (Approved)', fmt(c.grossAmount), T.blue],
    ['Net (Approved)', fmt(c.netAmount), '#1d4ed8'],
    ['Total Commission', fmt(c.totalCommission), T.green],
  ];
  const maxTrend = Math.max(1, ...data.trend.flatMap(t => [t.deposits, t.withdrawals]));

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Overview</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Summary of the isolated Agent Transaction subsystem.</p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12, marginBottom: 18 }}>
        {kpis.map(([label, value, color]) => (
          <Card key={label} style={{ padding: '14px 16px', borderTop: `3px solid ${color}` }}>
            <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
            <p style={{ margin: 0, fontSize: 18, fontWeight: 800, color }}>{value}</p>
          </Card>
        ))}
      </div>

      {data.trend.length > 0 && (
        <Card style={{ padding: 18, marginBottom: 18 }}>
          <h2 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 800, color: T.textMain }}>Deposits vs Withdrawals (last {data.trend.length} days)</h2>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, height: 120, overflowX: 'auto' }}>
            {data.trend.map(t => (
              <div key={t.date} style={{ flex: '1 0 26px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3 }}>
                <div style={{ width: '100%', display: 'flex', gap: 2, alignItems: 'flex-end', height: 92 }}>
                  <div title={`Deposits ${fmt(t.deposits)}`} style={{ flex: 1, background: T.success, borderRadius: '3px 3px 0 0', height: `${(t.deposits / maxTrend) * 100}%`, minHeight: t.deposits > 0 ? 3 : 0 }} />
                  <div title={`Withdrawals ${fmt(t.withdrawals)}`} style={{ flex: 1, background: T.danger, borderRadius: '3px 3px 0 0', height: `${(t.withdrawals / maxTrend) * 100}%`, minHeight: t.withdrawals > 0 ? 3 : 0 }} />
                </div>
                <span style={{ fontSize: 8.5, color: T.textMuted, whiteSpace: 'nowrap' }}>{t.date.slice(5)}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card style={{ marginBottom: 18, overflow: 'hidden' }}>
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${T.border}` }}><h2 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>Top Agents</h2></div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Agent', 'Deposits', 'Withdrawals', 'Transactions'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {data.byAgent.length === 0 && <tr><td colSpan={4} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions yet.</td></tr>}
              {data.byAgent.map((a, i) => (
                <tr key={i} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontWeight: 700 }}>{a.agentCode || '—'} {a.agentName ? `· ${a.agentName}` : ''}</td>
                  <td style={{ ...tdS, color: T.success, fontWeight: 700 }}>{fmt(a.deposits)}</td>
                  <td style={{ ...tdS, color: T.danger, fontWeight: 700 }}>{fmt(a.withdrawals)}</td>
                  <td style={tdS}>{a.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${T.border}` }}><h2 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>Recent Agent Transactions</h2></div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Type', 'Membership', 'Amount', 'Status', 'Created (IST)'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {data.recent.length === 0 && <tr><td colSpan={6} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions yet.</td></tr>}
              {data.recent.map(r => (
                <tr key={r.id} style={{ background: T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{r.referenceNumber}</td>
                  <td style={tdS}>{r.type}</td>
                  <td style={tdS}>{r.membershipId}{r.membershipName ? ` · ${r.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(r.amount)}</td>
                  <td style={tdS}><StatusPill status={r.status} type={r.type} method={r.txnMethod} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{r.createdDate} {r.createdTime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};

// ─── Agent Dashboard — Agent performance & earnings (isolated ledger, completed-only) ─────────
// Shows AGENT financial performance: overall amounts/commissions, per-agent breakdown, rankings and
// highs, plus operational counts. NO member/membership balances (those live on Balance Enquiry) and
// NO merchant data. Commission per leg from each agent's own fee — the same calculation everywhere.
// ─── Agent Dashboard — three-section executive overview (isolated ledger, completed-only) ─────
// Operational counts, a financial summary, and a transparent Balance Overview. NO performance
// table / rankings / highs / recent list — detailed analytics live in Agents, All Transactions,
// Reports and Balance Enquiry. Same per-leg calculation as everywhere; no merchant data.
const FinCard: React.FC<{ title: string; accent: string; a: [string, number]; b: [string, number]; bMoney?: boolean; strong?: boolean }> =
  ({ title, accent, a, b, bMoney = true, strong }) => (
  <Card style={{ padding: '18px 20px', borderTop: `4px solid ${accent}` }}>
    <p style={{ margin: '0 0 12px', fontSize: 13, fontWeight: 800, color: T.textMain }}>{title}</p>
    <p style={{ margin: '0 0 3px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{a[0]}</p>
    <p style={{ margin: '0 0 12px', fontSize: 22, fontWeight: 800, color: strong ? accent : T.textMain }}>{fmt(a[1])}</p>
    <p style={{ margin: '0 0 3px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{b[0]}</p>
    <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: bMoney ? T.warning : T.blue }}>{bMoney ? fmt(b[1]) : b[1]}</p>
  </Card>
);
// A single value tile in the Balance Overview flow.
const BoTile: React.FC<{ label: string; sub: string; value: number; color: string; big?: boolean }> = ({ label, sub, value, color, big }) => (
  <Card style={{ padding: '16px 18px', borderTop: `3px solid ${color}`, flex: '1 1 200px', minWidth: 180 }}>
    <p style={{ margin: '0 0 3px', fontSize: 11, fontWeight: 800, color: T.textMain }}>{label}</p>
    <p style={{ margin: '0 0 8px', fontSize: 10.5, color: T.textMuted }}>{sub}</p>
    <p style={{ margin: 0, fontSize: big ? 24 : 18, fontWeight: 800, color }}>{fmt(value)}</p>
  </Card>
);

export const AgentDashboardPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const [data, setData] = useState<AgentOverview | null>(null);
  const [perf, setPerf] = useState<AgentPerformance | null>(null);
  const load = useCallback(() => {
    agentTxnsAPI.overview().then(setData).catch(() => showToast('Failed to load the Agent Dashboard', 'error'));
    agentTxnsAPI.performance().then(setPerf).catch(() => {});
  }, [showToast]);
  useEffect(() => { load(); }, [load]);
  usePoll(() => load());

  if (!data || !perf) return <LoadingScreen label="Loading Agent Dashboard…" />;
  const c = data.cards; const o = perf.overall;
  const netDeposits = Math.round((o.totalDepositAmount - o.totalDepositCommission) * 100) / 100;
  const totalWithdrawals = Math.round((o.totalWithdrawalAmount + o.totalWithdrawalCommission) * 100) / 100;
  const totalSettlements = Math.round((o.totalSettlementAmount + o.totalSettlementCommission) * 100) / 100;
  const available = Math.round((netDeposits - totalWithdrawals - totalSettlements) * 100) / 100;

  const opsCards: Array<[string, number, string]> = [
    ['Total Deposit Requests', c.depositCount, T.success],
    ['Total Withdrawal Requests', c.withdrawalCount, T.danger],
    ['Total Settlement Requests', c.settlementCount, '#7c3aed'],
    ['Pending Requests', c.pending, T.warning],
    ['Completed Requests', c.completed, T.success],
    ['Rejected Requests', c.rejected, T.danger],
  ];
  const op = <span style={{ fontSize: 26, fontWeight: 800, color: T.textMuted, alignSelf: 'center', padding: '0 4px' }}>−</span>;

  return (
    <div style={{ maxWidth: 1100 }}>
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Dashboard</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Agent operational status, financial summary and available balance — completed transactions.</p>
      </div>

      {/* Section 1 — Operational Summary (counts only) */}
      <p style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Operational Summary</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 14, marginBottom: 26 }}>
        {opsCards.map(([label, value, color]) => (
          <Card key={label} style={{ padding: '16px 18px', borderTop: `3px solid ${color}` }}>
            <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
            <p style={{ margin: 0, fontSize: 26, fontWeight: 800, color }}>{value}</p>
          </Card>
        ))}
      </div>

      {/* Section 2 — Financial Summary (4 cards) */}
      <p style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Financial Summary <span style={{ fontWeight: 600, color: T.textMuted }}>· completed transactions</span></p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(230px,1fr))', gap: 14, marginBottom: 26 }}>
        <FinCard title="Total Deposits" accent={T.success} a={['Gross Deposit Amount', o.totalDepositAmount]} b={['Deposit Commission', o.totalDepositCommission]} />
        <FinCard title="Total Withdrawals" accent={T.danger} a={['Gross Withdrawal Amount', o.totalWithdrawalAmount]} b={['Withdrawal Commission', o.totalWithdrawalCommission]} />
        <FinCard title="Total Settlements" accent={'#7c3aed'} a={['Gross Settlement Amount', o.totalSettlementAmount]} b={['Settlement Commission', o.totalSettlementCommission]} />
        <FinCard title="Total Commission Earned" accent={T.blue} a={['Total Commission Earned', o.totalCommission]} b={['Total Completed Transactions', o.totalTransactions]} bMoney={false} strong />
      </div>

      {/* Section 3 — Balance Overview (transparent calculation) */}
      <p style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Balance Overview</p>
      <Card style={{ padding: 18 }}>
        <div style={{ display: 'flex', gap: 12, alignItems: 'stretch', flexWrap: 'wrap' }}>
          <BoTile label="Total Net Deposits" sub="Deposit Amount − Deposit Commission" value={netDeposits} color={T.success} />
          {op}
          <BoTile label="Total Withdrawals" sub="Withdrawal Amount + Commission" value={totalWithdrawals} color={T.danger} />
          {op}
          <BoTile label="Total Settlements" sub="Settlement Amount + Commission" value={totalSettlements} color={'#7c3aed'} />
          <span style={{ fontSize: 26, fontWeight: 800, color: T.textMuted, alignSelf: 'center', padding: '0 4px' }}>=</span>
          <BoTile label="Current Available Balance" sub="Across all completed agent transactions" value={available} color={T.success} big />
        </div>
      </Card>
    </div>
  );
};

// ─── Agent Profile — details, lifetime business/commission, members served, recent activity ───
// Opened by clicking an agent. Completed-only, same per-leg calculation. No document store exists,
// so no Documents section. Read-only.
export const AgentProfileModal: React.FC<{ agentMasterId: number; onClose: () => void }> = ({ agentMasterId, onClose }) => {
  const { showToast } = useToast();
  const [p, setP] = useState<AgentProfile | null>(null);
  useEffect(() => { agentTxnsAPI.agentProfile(agentMasterId).then(setP).catch(() => showToast('Failed to load the agent profile', 'error')); }, [agentMasterId, showToast]);
  if (!p) return <Modal title="Agent Profile" onClose={onClose} wide><div style={{ padding: 24, textAlign: 'center', color: T.textMuted }}>Loading…</div></Modal>;
  const a = p.agent, t = p.totals;
  const info: Array<[string, React.ReactNode]> = [
    ['Agent ID', a.agentId], ['Agent Name', a.agentName], ['Category', a.category],
    ['Country', a.country || '—'], ['Currency', a.currency || '—'],
    ['Created Date', a.createdDate || '—'], ['Status', a.status === 'ACTIVE' ? 'Active' : 'Inactive'],
  ];
  const cards: Array<[string, string, string]> = [
    ['Total Business', fmt(t.totalBusiness), T.blue],
    ['Total Deposits', `${fmt(t.totalDeposits)}`, T.success],
    ['Total Withdrawals', `${fmt(t.totalWithdrawals)}`, T.danger],
    ['Total Settlements', `${fmt(t.totalSettlements)}`, '#7c3aed'],
    ['Commission Earned', fmt(t.commissionEarned), T.green],
  ];
  return (
    <Modal title={`Agent Profile — ${a.agentId}`} onClose={onClose} wide>
      <p style={{ margin: '0 0 8px', fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Agent Details</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: '10px 18px', marginBottom: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {info.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))', gap: 12, marginBottom: 16 }}>
        {cards.map(([label, value, color]) => (
          <Card key={label} style={{ padding: '14px 16px', borderTop: `3px solid ${color}` }}>
            <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 800, color }}>{value}</p>
          </Card>
        ))}
      </div>

      <p style={{ margin: '0 0 8px', fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Assigned Members <span style={{ fontWeight: 600 }}>· {p.members.length}</span></p>
      <div style={{ overflowX: 'auto', border: `1px solid ${T.border}`, borderRadius: 10, marginBottom: 16 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ background: T.canvas }}>{['Membership', 'Deposits', 'Withdrawals', 'Settlements', 'Txns'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
          <tbody>
            {p.members.length === 0 && <tr><td colSpan={5} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 18 }}>No members yet.</td></tr>}
            {p.members.map(m => (
              <tr key={m.membershipId} style={{ background: T.surface }}>
                <td style={{ ...tdS, fontWeight: 700 }}>{m.membershipId}{m.memberName ? ` · ${m.memberName}` : ''}</td>
                <td style={{ ...tdS, textAlign: 'right', color: T.success }}>{fmt(m.deposits)}</td>
                <td style={{ ...tdS, textAlign: 'right', color: T.danger }}>{fmt(m.withdrawals)}</td>
                <td style={{ ...tdS, textAlign: 'right', color: '#7c3aed' }}>{fmt(m.settlements)}</td>
                <td style={{ ...tdS, textAlign: 'center' }}>{m.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p style={{ margin: '0 0 8px', fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Activity Timeline <span style={{ fontWeight: 600 }}>· recent</span></p>
      <div style={{ overflowX: 'auto', border: `1px solid ${T.border}`, borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ background: T.canvas }}>{['Date & Time', 'Reference', 'Type', 'Membership', 'Amount', 'Status'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
          <tbody>
            {p.activity.length === 0 && <tr><td colSpan={6} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 18 }}>No activity yet.</td></tr>}
            {p.activity.map(x => (
              <tr key={x.id} style={{ background: T.surface }}>
                <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                <td style={{ ...tdS, fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                <td style={tdS}>{x.type.charAt(0) + x.type.slice(1).toLowerCase()}</td>
                <td style={tdS}>{x.membershipId}</td>
                <td style={{ ...tdS, textAlign: 'right', fontWeight: 700 }}>{fmt(x.amount)}</td>
                <td style={tdS}><StatusPill status={x.status} type={x.type} method={x.txnMethod} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  );
};

// ─── Balance Enquiry — read-only per-member financial summary (isolated agent ledger) ─────────
// One labelled figure inside a summary card.
const BeRow: React.FC<{ k: string; v: React.ReactNode; strong?: boolean; color?: string }> = ({ k, v, strong, color }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '7px 0', gap: 12 }}>
    <span style={{ fontSize: 12, color: T.textMuted }}>{k}</span>
    <span style={{ fontSize: strong ? 15 : 13, fontWeight: strong ? 800 : 700, color: color || T.textMain, whiteSpace: 'nowrap' }}>{v}</span>
  </div>
);
// A Deposit / Withdrawal / Settlement summary card (count, gross, commission, net/deducted).
const BeSummaryCard: React.FC<{ title: string; accent: string; count: number; gross: number; commission: number; lastLabel: string; lastValue: number; lastColor: string }> =
  ({ title, accent, count, gross, commission, lastLabel, lastValue, lastColor }) => (
  <Card style={{ padding: 16, borderTop: `3px solid ${accent}` }}>
    <p style={{ margin: '0 0 8px', fontSize: 13, fontWeight: 800, color: T.textMain }}>{title}</p>
    <BeRow k="Total Transactions" v={count} />
    <BeRow k="Gross Amount" v={fmt(gross)} />
    <BeRow k="Commission" v={fmt(commission)} color={T.warning} />
    <div style={{ borderTop: `1px solid ${T.border}`, marginTop: 4 }}>
      <BeRow k={lastLabel} v={fmt(lastValue)} strong color={lastColor} />
    </div>
  </Card>
);

export const AgentBalanceEnquiryPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ onNavigate }) => {
  const { showToast } = useToast();
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AgentMemberSummary | null>(null);
  const [recent, setRecent] = useState<AgentTxnRow[]>([]);
  const [searched, setSearched] = useState('');

  const search = async () => {
    const id = query.trim();
    if (!id) { showToast('Enter a Membership ID.', 'error'); return; }
    setBusy(true);
    try {
      const r = await agentTxnsAPI.balanceEnquiry(id);
      setResult(r); setSearched(id);
      // Recent COMPLETED transactions for exactly this member (the list carries commission fields).
      if (r.found) {
        const list = await agentTxnsAPI.list({ search: id });
        setRecent(list
          .filter(t => t.membershipId === id && (AGENT_COMPLETED_STATUSES as string[]).includes(t.status))
          .slice(0, 10));
      } else { setRecent([]); }
    } catch (e) { showToast(agentTxnError(e, 'Balance enquiry failed.'), 'error'); }
    finally { setBusy(false); }
  };

  const r = result?.found ? result : null;
  const netDeposits = r ? (r.totalDeposits ?? 0) - (r.depositCommission ?? 0) : 0;
  const totalWd = r ? (r.totalWithdrawals ?? 0) + (r.withdrawalCommission ?? 0) : 0;
  const totalSt = r ? (r.totalSettlements ?? 0) + (r.settlementCommission ?? 0) : 0;

  return (
    <div style={{ maxWidth: 1000 }}>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Balance Enquiry</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Read-only member financial breakdown from the isolated Agent ledger — completed transactions only.</p>
      </div>

      {/* 1. Search */}
      <Card style={{ padding: 18, marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}
          onKeyDown={(e) => { if (e.key === 'Enter') search(); }}>
          <Input label="Membership ID" value={query} onChange={e => setQuery(normalizeMemberId(e.target.value))}
            placeholder="Enter Membership ID" style={{ marginBottom: 0, flex: '1 1 240px' }} />
          <Btn onClick={search} disabled={busy}>{busy ? 'Searching…' : 'Search'}</Btn>
        </div>
      </Card>

      {result && !result.found && (
        <Card style={{ padding: 18, borderLeft: `4px solid ${T.danger}` }}>
          <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: T.danger }}>No member found for the entered Membership ID.</p>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: T.textMuted }}>Searched: {searched}</p>
        </Card>
      )}

      {r && (
        <>
          {/* 2. Member Information — no transaction totals mixed in */}
          <Card style={{ padding: 18, marginBottom: 16 }}>
            <p style={{ margin: '0 0 12px', fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Member Information</p>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: '14px 24px' }}>
              <div><p style={{ margin: '0 0 4px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Membership ID</p><p style={{ margin: 0, fontSize: 14, fontWeight: 700, color: T.textMain }}>{r.membershipId}</p></div>
              <div><p style={{ margin: '0 0 4px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Member Name</p><p style={{ margin: 0, fontSize: 14, fontWeight: 700, color: T.textMain }}>{r.memberName || '—'}</p></div>
              <div><p style={{ margin: '0 0 4px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Last Transaction Date</p><p style={{ margin: 0, fontSize: 14, fontWeight: 700, color: T.textMain }}>{r.lastTransactionDate || '—'}</p></div>
              <div><p style={{ margin: '0 0 4px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Current Available Balance</p><p style={{ margin: 0, fontSize: 20, fontWeight: 800, color: T.success }}>{fmt(r.availableBalance ?? 0)}</p></div>
            </div>
          </Card>

          {/* 3-5. Deposit / Withdrawal / Settlement summary cards */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(260px,1fr))', gap: 14, marginBottom: 16 }}>
            <BeSummaryCard title="Deposit Summary" accent={T.success} count={r.depositCount ?? 0} gross={r.totalDeposits ?? 0} commission={r.depositCommission ?? 0} lastLabel="Net Deposited" lastValue={netDeposits} lastColor={T.success} />
            <BeSummaryCard title="Withdrawal Summary" accent={T.danger} count={r.withdrawalCount ?? 0} gross={r.totalWithdrawals ?? 0} commission={r.withdrawalCommission ?? 0} lastLabel="Total Deducted" lastValue={totalWd} lastColor={T.danger} />
            <BeSummaryCard title="Settlement Summary" accent={'#7c3aed'} count={r.settlementCount ?? 0} gross={r.totalSettlements ?? 0} commission={r.settlementCommission ?? 0} lastLabel="Total Deducted" lastValue={totalSt} lastColor={'#7c3aed'} />
          </div>

          {/* 6. Balance Calculation — fully transparent */}
          <Card style={{ padding: 18, marginBottom: 16 }}>
            <p style={{ margin: '0 0 12px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Balance Calculation</p>
            <div style={{ maxWidth: 520 }}>
              <BeRow k="Total Net Deposits (Gross − Commission)" v={fmt(netDeposits)} color={T.success} />
              <BeRow k="Less Total Withdrawals (Amount + Commission)" v={`− ${fmt(totalWd)}`} color={T.danger} />
              <BeRow k="Less Total Settlements (Amount + Commission)" v={`− ${fmt(totalSt)}`} color={'#7c3aed'} />
              <div style={{ borderTop: `2px solid ${T.border}`, marginTop: 6, paddingTop: 4 }}>
                <BeRow k="Current Available Balance" v={fmt(r.availableBalance ?? 0)} strong color={T.success} />
              </div>
            </div>
          </Card>

          {/* 7. Recent completed transactions + View All */}
          <Card style={{ overflow: 'hidden' }}>
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${T.border}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <h2 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>Recent Completed Transactions</h2>
              <Btn size="sm" variant="ghost" onClick={() => onNavigate?.('agent-all-txns')}>View All Transactions →</Btn>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr style={{ background: T.canvas }}>{['Date & Time', 'Type', 'Reference', 'Amount', 'Commission', 'Net Amount', 'Status'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
                <tbody>
                  {recent.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No completed transactions for this member.</td></tr>}
                  {recent.map(t => (
                    <tr key={t.id} style={{ background: T.surface }}>
                      <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{t.createdDate} {t.createdTime}</td>
                      <td style={tdS}>{t.type.charAt(0) + t.type.slice(1).toLowerCase()}</td>
                      <td style={{ ...tdS, fontWeight: 700, color: T.blue }}>{t.referenceNumber}</td>
                      <td style={{ ...tdS, textAlign: 'right', fontWeight: 700 }}>{fmt(t.amount)}</td>
                      <td style={{ ...tdS, textAlign: 'right', color: T.warning }}>{t.commissionAmount != null ? fmt(t.commissionAmount) : '—'}</td>
                      <td style={{ ...tdS, textAlign: 'right', fontWeight: 700 }}>{t.netAmount != null ? fmt(t.netAmount) : '—'}</td>
                      <td style={tdS}><StatusPill status={t.status} type={t.type} method={t.txnMethod} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
};

// ─── Reusable read-only field (auto-fetched agent details) ─────────────────────
const ReadField: React.FC<{ label: string; value?: string | null; placeholder?: string }> = ({ label, value, placeholder = '—' }) => (
  <Input label={label} value={value || ''} onChange={() => {}} placeholder={placeholder} readOnly />
);

// ─── Agent Deposit Request form ────────────────────────────────────────────────
// `embedded` renders the bare form (no page heading / isolation note) for use inside the Agent
// Deposit Management modal; `onSubmitted` closes that modal and refreshes its list on success.
export const AgentDepositRequestPage: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }> = ({ embedded, onSubmitted }) => {
  const { showToast } = useToast();
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [agentId, setAgentId] = useState('');
  const [membershipId, setMembershipId] = useState('');
  const [membershipName, setMembershipName] = useState('');
  const [memberLocked, setMemberLocked] = useState(false);  // name auto-filled from an existing membership → read-only
  // Supplied by the customer/agent and typed in by the operator — never generated.
  const [membershipType, setMembershipType] = useState('');
  const [amount, setAmount] = useState('');
  // Transaction type + Sending Account — captured exactly like the merchant Deposit Request.
  const [txnMethod, setTxnMethod] = useState('');
  const [senderUpiId, setSenderUpiId] = useState('');
  const [senderAccountHolder, setSenderAccountHolder] = useState('');
  const [senderAccountNumber, setSenderAccountNumber] = useState('');
  const [senderIfsc, setSenderIfsc] = useState('');
  const [senderBankName, setSenderBankName] = useState('');
  const [senderBranch, setSenderBranch] = useState('');
  // Same shared IFSC auto-fill as the Agent Account form.
  const senderIfscFill = useIfscAutoFill(senderIfsc, setSenderIfsc, (bank, branch) => {
    setSenderBankName(bank); setSenderBranch(branch);
  });
  const [country, setCountry] = useState('');
  const [state, setState] = useState('');
  const [location, setLocation] = useState('');
  const [mobile, setMobile] = useState('');
  const [mobileCode, setMobileCode] = useState('+91');
  const [notes, setNotes] = useState('');
  const [instructions, setInstructions] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AgentTxnRow | null>(null);

  useEffect(() => {
    agentTxnsAPI.formData().then(setFd).catch(() => showToast('Failed to load form data', 'error'));
  }, [showToast]);

  const agent: AgentFormAgent | undefined = fd?.agents.find(a => String(a.id) === agentId);

  // Auto-fetch the Membership Name as the ID is entered (debounced), mirroring the Merchant Deposit
  // Request flow: an existing membership auto-fills + locks the name; a new ID stays manually editable.
  useEffect(() => {
    const id = membershipId.trim();
    if (id.length < 3) { setMemberLocked(false); return; }
    let alive = true;
    const t = setTimeout(() => {
      agentTxnsAPI.member(id).then(r => {
        if (!alive) return;
        if (r.membershipName) { setMembershipName(r.membershipName); setMemberLocked(true); }
        else setMemberLocked(false);
      }).catch(() => { if (alive) setMemberLocked(false); });
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [membershipId]);

  const reset = () => {
    setAgentId(''); setMembershipId(''); setMembershipName(''); setMemberLocked(false); setMembershipType(''); setAmount('');
    setTxnMethod(''); setSenderUpiId(''); setSenderAccountHolder(''); setSenderAccountNumber('');
    setSenderIfsc(''); setSenderBankName(''); setSenderBranch(''); senderIfscFill.reset();
    setCountry(''); setState(''); setLocation(''); setMobile(''); setMobileCode('+91'); setNotes(''); setInstructions('');
  };

  const submit = async () => {
    if (!agent) { showToast('Select an Agent ID.', 'error'); return; }
    if (!membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    const amt = Number(parseIndianAmount(amount));
    if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
    if (!txnMethod) { showToast('Select a Transaction Type.', 'error'); return; }
    if (txnMethod === 'UPI' && !senderUpiId.includes('@')) { showToast('Enter a valid Sender UPI ID (name@bank).', 'error'); return; }
    if (BANK_LIKE.includes(txnMethod) && (!senderAccountHolder.trim() || !senderAccountNumber.trim())) {
      showToast('Enter the Sending Account holder and number.', 'error'); return;
    }
    if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
    setBusy(true); setResult(null);
    const body: AgentDepositBody = {
      agentMasterId: agent.id, membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined,
      mobileCode: mobile ? mobileCode : undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: false,
      // No deposit collects Token Details / Note Number at creation: Cash captures the token and
      // Crypto the wallet at Submit Account, and a Bank Transfer never has one — the operator
      // supplies the agent's bank account and the player pays into it.
      txnMethod,
      senderUpiId: senderUpiId.trim() || undefined,
      senderAccountHolder: senderAccountHolder.trim() || undefined,
      senderAccountNumber: senderAccountNumber.trim() || undefined,
      senderIfsc: senderIfsc.trim() || undefined,
      senderBankName: senderBankName.trim() || undefined,
      senderBranch: senderBranch.trim() || undefined,
      // Approval routing follows the business workflow automatically — no manual approver.
    };
    try {
      const row = await agentTxnsAPI.createDeposit(body);
      setResult(row);
      showToast(`Agent deposit ${row.referenceNumber} created.`, 'success');
      reset();
      onSubmitted?.();
    } catch (e) {
      showToast(agentTxnError(e, 'Failed to create Agent Deposit Request.'), 'error');
    } finally { setBusy(false); }
  };

  if (!fd) return <LoadingScreen label="Loading…" />;

  return (
    <div style={embedded ? undefined : { maxWidth: 860 }}>
      {!embedded && (<>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Deposit Request</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Record a third-party agent deposit in the isolated Agent ledger.</p>
      </div>
      </>)}

      {result && !embedded && (
        <Card style={{ padding: 16, marginBottom: 18, borderLeft: `4px solid ${T.success}` }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: T.success, marginBottom: 10 }}>✓ Agent Deposit Request created</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: '10px 18px' }}>
            {[['Reference Number', result.referenceNumber], ['Transaction Code', result.transactionCode], ['Note Number', result.noteNumber], ['Token Details', result.tokenDetails], ['Status', result.status], ['Created (IST)', `${result.createdDate} ${result.createdTime}`]].map(([k, v]) => (
              <div key={k}><div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{k}</div><div style={{ fontSize: 13, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{v}</div></div>
            ))}
          </div>
        </Card>
      )}

      <Card style={{ padding: 22 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          {/* Transaction Type is chosen FIRST and narrows the agent list to that category, so a
              mismatched pair cannot be built. Changing it clears the agent, and every Agent field
              below is derived from `agent`, so they clear with it. The backend enforces the same
              rule (_require_agent_serves_method) for anything that bypasses this form. */}
          <Sel label="Transaction Type" value={txnMethod} onChange={e => { setTxnMethod(e.target.value); setAgentId(''); }} required
            options={[{ value: '', label: '— Select —' }, ...(fd.txnMethods || []).map(v => ({ value: v, label: methodLabel(v) }))]} />
          <Sel label="Select Agent ID" value={agentId} onChange={e => setAgentId(e.target.value)} required
            options={[{ value: '', label: txnMethod ? '— Select an agent —' : '— Choose a Transaction Type first —' },
                ...agentsForMethod(fd.agents, txnMethod).map(a => ({ value: String(a.id), label: `${a.agentId} — ${a.name}` }))]} />
          <ReadField label="Agent Name" value={agent?.name} />
          <ReadField label="Agent Country" value={agent?.country} />
          <ReadField label="Agent State" value={agent?.state} />
          <ReadField label="Agent Location" value={agent?.location} />
          <ReadField label="Agent Category" value={agent?.category} />

          <Input label="Membership ID" value={membershipId} onChange={e => setMembershipId(normalizeMemberId(e.target.value))}
            required placeholder="Enter Membership ID" hint="Uppercase letters and numbers only" />
          <Input label="Membership Name" value={membershipName} onChange={e => setMembershipName(e.target.value)} placeholder="Manual or auto-fetched" readOnly={memberLocked} hint={memberLocked ? 'Auto-filled from existing membership' : undefined} />
          <Sel label="Membership Type" value={membershipType} onChange={e => setMembershipType(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...fd.membershipTypes.map(t => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))]} />
          <Input label="Transaction Amount" type="text" value={amount} onChange={e => setAmount(formatIndianAmountInput(e.target.value))} required inputMode="decimal" />
        </div>

        {/* Sending Account — the account the payment is sent FROM (mirrors the merchant Deposit
            Request). Only bank-style and UPI sends name an account; Cash/Crypto do not. */}
        {(txnMethod === 'UPI' || BANK_LIKE.includes(txnMethod)) && (
          <div style={{ margin: '4px 0 14px', padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
            <p style={{ margin: '0 0 10px', fontSize: 11, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Sending Account Details</p>
            {txnMethod === 'UPI' ? (
              <Input label="Sender UPI ID" value={senderUpiId} onChange={e => setSenderUpiId(e.target.value)} required
                placeholder="e.g. satish@ybl" hint="The UPI the payment is sent from" style={{ marginBottom: 0 }} />
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
                <Input label="Account Holder" value={senderAccountHolder} onChange={e => setSenderAccountHolder(e.target.value)} required />
                <Input label="Account Number" value={senderAccountNumber} onChange={e => setSenderAccountNumber(e.target.value)} required />
                <IfscField label="IFSC Code" value={senderIfsc} ifsc={senderIfscFill} />
                <Input label="Bank Name" value={senderBankName} onChange={e => setSenderBankName(e.target.value)} readOnly={senderIfscFill.locked} />
                <Input label="Branch" value={senderBranch} onChange={e => setSenderBranch(e.target.value)} readOnly={senderIfscFill.locked} />
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <SearchSelect label="Country" value={country} onChange={setCountry} options={COUNTRY_OPTIONS} placeholder="Type to search…" />
          <SearchSelect label="State" value={state} onChange={setState} options={STATE_OPTIONS} placeholder="Type to search…" />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <PhoneField code={mobileCode} onCode={setMobileCode} value={mobile} onValue={setMobile}
            codeOptions={DIAL_OPTIONS} style={{ marginBottom: 16 }} />

          {/* A deposit never asks for Token Details / Unique Note Number here: Cash captures the
              token and Crypto the wallet at Submit Account, and a Bank Transfer has neither — the
              operator supplies the agent's bank account and the player pays into it. */}
          <Sel label="Instructions" value={instructions} onChange={e => setInstructions(e.target.value)}
            options={[{ value: '', label: '— None —' }, ...fd.instructions.map(i => ({ value: i, label: instrLabel(i) }))]} />
        </div>

        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
          <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Up to 100 characters"
            style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
        </div>

        <div style={{ marginTop: 18, display: 'flex', gap: 10 }}>
          <Btn onClick={submit} disabled={busy}>{busy ? 'Submitting…' : 'Submit Agent Deposit'}</Btn>
          <Btn variant="secondary" onClick={reset} disabled={busy}>Clear</Btn>
        </div>
      </Card>
    </div>
  );
};

// ─── Agent Withdrawal Request form ─────────────────────────────────────────────
// `embedded` / `onSubmitted` mirror AgentDepositRequestPage — used inside the Agent Withdrawal
// Management modal.
export const AgentWithdrawalRequestPage: React.FC<{
  user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void;
  /** 'settlement' reuses this exact form: same payout capture, minus the approval gate. */
  mode?: 'withdrawal' | 'settlement';
}> = ({ embedded, onSubmitted, mode = 'withdrawal' }) => {
  const isSettlement = mode === 'settlement';
  const NOUN = isSettlement ? 'Settlement' : 'Withdrawal';
  const { showToast } = useToast();
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [membershipId, setMembershipId] = useState('');
  const [membershipName, setMembershipName] = useState('');
  const [membershipType, setMembershipType] = useState('');
  // This member's Available Balance in the agent ledger, fetched with the membership lookup and
  // shown so the operator sees it before submitting. The server validates the balance on create.
  const [memberBalance, setMemberBalance] = useState<number | null>(null);
  const [agentId, setAgentId] = useState('');
  const [autoAgent, setAutoAgent] = useState<AgentMemberLookup['latestDeposit']>(null);
  const [txnMethod, setTxnMethod] = useState('');
  // Supplied by the customer/agent and typed in by the operator — never generated.
  const [tokenDetails, setTokenDetails] = useState('');
  const [noteNumber, setNoteNumber] = useState('');
  const [walletAddress, setWalletAddress] = useState('');
  const [manualOverride, setManualOverride] = useState(false);
  const [looking, setLooking] = useState(false);
  const [amount, setAmount] = useState('');
  const [country, setCountry] = useState('');
  const [state, setState] = useState('');
  const [location, setLocation] = useState('');
  const [mobile, setMobile] = useState('');
  const [mobileCode, setMobileCode] = useState('+91');
  const [notes, setNotes] = useState('');
  const [instructions, setInstructions] = useState('');
  // Approval is mandatory on an Agent Withdrawal — the operator must always route it to an
  // approver, so this is fixed on rather than a choice.
  const sendApproval = true;
  const [approverId, setApproverId] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AgentTxnRow | null>(null);

  useEffect(() => {
    agentTxnsAPI.formData().then(setFd).catch(() => showToast('Failed to load form data', 'error'));
  }, [showToast]);

  // Membership lookup → auto-fetch the agent from the latest agent DEPOSIT for this membership.
  const lookupMember = async () => {
    const id = membershipId.trim();
    setAutoAgent(null); setManualOverride(false); setMemberBalance(null);
    if (!id) return;
    setLooking(true);
    try {
      const r = await agentTxnsAPI.member(id);
      if (r.membershipName) setMembershipName(r.membershipName);
      setMemberBalance(typeof r.availableBalance === 'number' ? r.availableBalance : null);
      // The member's latest agent deposit still auto-links its agent (and its depositId), but only
      // when that agent serves the Transaction Type already chosen — the type is picked before the
      // agent now, so an out-of-category auto-link would drop in an agent the filtered list does
      // not even offer, and the backend would reject it on submit. When it does not match, the
      // operator's own choice stands and no deposit link is made.
      const linked = r.latestDeposit;
      const linkedFits = linked
        && (!txnMethod || String(linked.category || '').toUpperCase() === categoryForMethod(txnMethod));
      if (linked && linkedFits) { setAutoAgent(linked); setAgentId(String(linked.agentMasterId)); }
      else { setManualOverride(true); }   // no prior deposit, or its agent serves another type
    } catch {
      setManualOverride(true);
    } finally { setLooking(false); }
  };

  const usingAuto = Boolean(autoAgent) && !manualOverride && !!agentId && String(autoAgent!.agentMasterId) === agentId;
  const fdAgent: AgentFormAgent | undefined = fd?.agents.find(a => String(a.id) === agentId);
  const disp = fdAgent
    ? { code: fdAgent.agentId, name: fdAgent.name, country: fdAgent.country, state: fdAgent.state, location: fdAgent.location, category: fdAgent.category }
    : (autoAgent && String(autoAgent.agentMasterId) === agentId
        ? { code: autoAgent.agentCode || '', name: autoAgent.agentName || '', country: autoAgent.country || '', state: autoAgent.state || '', location: autoAgent.location || '', category: autoAgent.category || '' }
        : undefined);
  const reset = () => {
    setMembershipId(''); setMembershipName(''); setMembershipType(''); setAgentId(''); setAutoAgent(null);
    setManualOverride(false); setAmount(''); setCountry(''); setState(''); setLocation(''); setMobile(''); setMobileCode('+91');
    setNotes(''); setInstructions(''); setApproverId('');
    setTxnMethod(''); setMemberBalance(null);
    setTokenDetails(''); setNoteNumber('');
  };

  const submit = async () => {
    if (!isSettlement && !membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!isSettlement && !membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    if (!agentId) { showToast('Select an Agent ID.', 'error'); return; }
    const amt = Number(parseIndianAmount(amount));
    if (!amt || amt <= 0) { showToast(`Enter a valid ${isSettlement ? 'Settlement' : 'Transaction'} Amount.`, 'error'); return; }
    if (notes.length > 100) { showToast(`${isSettlement ? 'Remarks' : 'Notes'} must be 100 characters or fewer.`, 'error'); return; }
    if (!isSettlement && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    if (!txnMethod) { showToast('Select a Transaction Type.', 'error'); return; }
    if (!agentId) { showToast('Select an Assigned Agent.', 'error'); return; }
    // The agent list is already filtered by Transaction Type; this is the last-line guard that a
    // stale selection can never be submitted against a mismatched category.
    if (disp?.category && categoryForMethod(txnMethod) &&
        String(disp.category).toUpperCase() !== categoryForMethod(txnMethod)) {
      showToast('The selected Agent does not belong to the chosen Transaction Type.', 'error'); return;
    }
    // Token / wallet capture is a Withdrawal concern — a settlement is paid offline.
    if (!isSettlement) {
      if (isWalletMethod(txnMethod)) {
        if (!walletAddress.trim()) { showToast('Enter the Crypto Wallet Address.', 'error'); return; }
        if (!isValidWallet(walletAddress)) { showToast('Enter a valid crypto wallet address.', 'error'); return; }
      } else {
        if (!tokenDetails.trim()) { showToast('Enter the Token Details.', 'error'); return; }
        if (!noteNumber.trim()) { showToast('Enter the Unique Note Number.', 'error'); return; }
      }
    }
    setBusy(true); setResult(null);
    const body: AgentWithdrawalBody = isSettlement ? {
      // Settlement: agent, method, amount and remarks only. No membership, no contact details,
      // no token/wallet, no approver — the payment is made offline.
      agentMasterId: Number(agentId), membershipId: '', membershipType: '',
      amount: amt, notes: notes || undefined, sentForApproval: false, txnMethod,
    } : {
      agentMasterId: Number(agentId), membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined,
      mobileCode: mobile ? mobileCode : undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: true,
      approverUserId: Number(approverId),
      txnMethod,
      ...(isWalletMethod(txnMethod)
        ? { walletAddress: walletAddress.trim() }
        : { tokenDetails: tokenDetails.trim(), noteNumber: noteNumber.trim() }),
      linkedDepositId: usingAuto ? autoAgent!.depositId : undefined,
    };
    try {
      const row = await (isSettlement ? agentTxnsAPI.createSettlement(body) : agentTxnsAPI.createWithdrawal(body));
      setResult(row);
      showToast(`Agent ${NOUN.toLowerCase()} ${row.referenceNumber} created.`, 'success');
      reset();
      onSubmitted?.();
    } catch (e) {
      showToast(agentTxnError(e, `Failed to create Agent ${NOUN} Request.`), 'error');
    } finally { setBusy(false); }
  };

  if (!fd) return <LoadingScreen label="Loading…" />;

  return (
    <div style={embedded ? undefined : { maxWidth: 860 }}>
      {!embedded && (<>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent {NOUN} Request</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Record a third-party agent {NOUN.toLowerCase()} in the isolated Agent ledger.</p>
      </div>
      </>)}

      {result && !embedded && (
        <Card style={{ padding: 16, marginBottom: 18, borderLeft: `4px solid ${T.success}` }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: T.success, marginBottom: 10 }}>✓ Agent {NOUN} Request created</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: '10px 18px' }}>
            {[['Reference Number', result.referenceNumber], ['Transaction Code', result.transactionCode], ['Note Number', result.noteNumber], ['Token Details', result.tokenDetails], ['Status', result.status], ['Created (IST)', `${result.createdDate} ${result.createdTime}`]].map(([k, v]) => (
              <div key={k}><div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{k}</div><div style={{ fontSize: 13, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{v}</div></div>
            ))}
          </div>
        </Card>
      )}

      <Card style={{ padding: 22 }}>
        {/* The member's latest agent deposit pre-selects its agent and links this request to that
            deposit (linkedDepositId). Picking a different agent below simply drops the link — the
            Select is always live, so no override toggle is needed. */}
        {usingAuto && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: T.success, background: T.successBg, padding: '2px 10px', borderRadius: 20 }}>✓ Auto-fetched</span>
            <span style={{ fontSize: 12, color: T.textMuted }}>Agent taken from the latest agent deposit {autoAgent!.reference} for this membership — choose another agent to unlink.</span>
          </div>
        )}

        {/* Same order and two-column grid as the Agent Deposit Request: the Transaction Type is
            chosen first and narrows the agent list to that category; the agent details below are
            all auto-fetched from the chosen agent. */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          {/* Transaction Type is chosen first and narrows the agent list to that category, so the
              selected agent always matches it. A settlement offers only Cash / Bank Transfer /
              Crypto; deposits and withdrawals keep the full method list. */}
          <Sel label="Transaction Type" value={txnMethod} onChange={e => { setTxnMethod(e.target.value); setAgentId(''); setAutoAgent(null); }} required
            options={[{ value: '', label: '— Select —' },
              ...(isSettlement ? AGENT_SETTLEMENT_METHODS : (fd.txnMethods || [])).map(v => ({ value: v, label: methodLabel(v) }))]} />
          <Sel label={isSettlement ? 'Assigned Agent' : 'Select Agent ID'} value={agentId} onChange={e => setAgentId(e.target.value)} required
            options={[{ value: '', label: txnMethod ? '— Select an agent —' : '— Choose a Transaction Type first —' },
              ...agentsForMethod(fd.agents, txnMethod).map(a => ({ value: String(a.id), label: `${a.agentId} — ${a.name}` }))]} />
          <ReadField label="Agent Name" value={disp?.name} />
          {!isSettlement && <ReadField label="Agent Country" value={disp?.country} />}
          {!isSettlement && <ReadField label="Agent State" value={disp?.state} />}
          {!isSettlement && <ReadField label="Agent Location" value={disp?.location} />}
          <ReadField label="Agent Category" value={disp?.category ? (CATEGORY_LABEL_A[String(disp.category).toUpperCase()] || disp.category) : undefined} />

          {/* A settlement is an offline merchant↔agent payment — no member on either side, so it
              captures no membership at all. */}
          {!isSettlement && <Input label="Membership ID" value={membershipId} onChange={e => setMembershipId(normalizeMemberId(e.target.value))}
            onBlur={lookupMember} required placeholder="Enter Membership ID"
            hint={looking ? 'Looking up…' : 'Uppercase letters and numbers only'} />}
          {!isSettlement && <Input label="Membership Name" value={membershipName} onChange={e => setMembershipName(e.target.value)} placeholder="Manual or auto-fetched" />}
          {!isSettlement && <Sel label="Membership Type" value={membershipType} onChange={e => setMembershipType(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...fd.membershipTypes.map(t => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))]} />}
          <Input label={isSettlement ? 'Settlement Amount' : 'Transaction Amount'} type="text" value={amount} onChange={e => setAmount(formatIndianAmountInput(e.target.value))} required inputMode="decimal"
            hint={!isSettlement && memberBalance != null ? `Member Available Balance: ₹${fmt(memberBalance)}` : undefined} />
        </div>

        {/* Country / State / Location / Mobile / Token / Note / Instructions describe the member
            and how the operator contacts them — all Deposit/Withdrawal concerns. A Settlement is
            an offline merchant↔agent payment with no member contact step, so none of it applies. */}
        {!isSettlement && <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <SearchSelect label="Country" value={country} onChange={setCountry} options={COUNTRY_OPTIONS} placeholder="Type to search…" />
          <SearchSelect label="State" value={state} onChange={setState} options={STATE_OPTIONS} placeholder="Type to search…" />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <PhoneField code={mobileCode} onCode={setMobileCode} value={mobile} onValue={setMobile}
            codeOptions={DIAL_OPTIONS} style={{ marginBottom: 16 }} />
          {/* Withdrawal: Cash keeps Token Details + Note; Crypto replaces them with a Wallet Address.
              Bank/UPI keep token/note as today. */}
          {isWalletMethod(txnMethod) ? (
            <Input label="Crypto Wallet Address" value={walletAddress} onChange={e => setWalletAddress(e.target.value)}
              required placeholder="The wallet to pay out to"
              hint={walletAddress.trim() && !isValidWallet(walletAddress) ? 'Not a valid wallet address format' : 'Confirmed by the Manager — enter carefully'} />
          ) : (<>
            <Input label="Token Details" value={tokenDetails} onChange={e => setTokenDetails(e.target.value)}
              required placeholder="As provided by the customer" />
            <Input label="Unique Note Number" value={noteNumber} onChange={e => setNoteNumber(e.target.value)}
              required placeholder="As provided by the customer" hint="Must be unique" />
          </>)}
          <Sel label="Instructions" value={instructions} onChange={e => setInstructions(e.target.value)}
            options={[{ value: '', label: '— None —' }, ...fd.instructions.map(i => ({ value: i, label: instrLabel(i) }))]} />
        </div>}

        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{isSettlement ? 'Remarks' : 'Notes'} <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
          <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Up to 100 characters"
            style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
        </div>

        {/* A Settlement has no approver: the Supervisor raises it and drives the whole chain
            (accept → upload proof → settle). */}
        {!isSettlement && <div style={{ marginTop: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          <p style={{ margin: '0 0 4px', fontSize: 13, fontWeight: 700, color: T.textMain }}>Send To Approval</p>
          <p style={{ margin: '0 0 12px', fontSize: 11.5, color: T.textMuted }}>
            Every Agent Withdrawal goes to an approver — choose who reviews this one.
          </p>
          <div style={{ maxWidth: 360 }}>
            <Sel label="Authorized Approver" value={approverId} onChange={e => setApproverId(e.target.value)} required
              options={[{ value: '', label: '— Select approver —' }, ...fd.approvers.map(a => ({ value: String(a.id), label: `${a.name} (${a.role})` }))]} />
          </div>
        </div>}

        <div style={{ marginTop: 18, display: 'flex', gap: 10 }}>
          <Btn onClick={submit} disabled={busy}>{busy ? 'Submitting…' : `Submit Agent ${NOUN}`}</Btn>
          <Btn variant="secondary" onClick={reset} disabled={busy}>Clear</Btn>
        </div>
      </Card>
    </div>
  );
};

// ─── Manage Transaction — correct the amount of a PENDING agent transaction ─────
// A Manager already holds approval authority, so forwarding to another approver is meaningless for
// them — they act on the transaction directly (Update Amount / Approve / Reject / Close). Every
// other role keeps the existing forward-to-approver workflow.
const ManageModal: React.FC<{ row: AgentTxnRow; fd: AgentFormData | null; canApprove: boolean; role: string; onClose: () => void; onRefresh: () => void }> =
  ({ row, fd, canApprove, role, onClose, onRefresh }) => {
    const { showToast } = useToast();
    const canSendToApproval = role !== 'MANAGER';
    const [amount, setAmount] = useState(formatIndianAmountInput(String(row.amount)));
    const [notes, setNotes] = useState('');
    const [sendApproval, setSendApproval] = useState(false);
    const [approverId, setApproverId] = useState('');
    const [busy, setBusy] = useState(false);
    const [current, setCurrent] = useState<AgentTxnRow>(row);
    const [audit, setAudit] = useState<AgentTxnAuditRow[]>([]);

    const loadAudit = useCallback(() => { agentTxnsAPI.audit(row.id).then(setAudit).catch(() => {}); }, [row.id]);
    useEffect(() => { loadAudit(); }, [loadAudit]);

    const immutable: Array<[string, React.ReactNode]> = [
      ['Reference Number', current.referenceNumber], ['Transaction Code', current.transactionCode],
      ['Type', current.type], ['Agent', `${current.agentCode || '—'}${current.agentName ? ` · ${current.agentName}` : ''}`],
      ['Membership', `${current.membershipId}${current.membershipName ? ` · ${current.membershipName}` : ''}`],
      ['Membership Type', current.membershipType], ['Note Number', current.noteNumber],
      ['Token Details', current.tokenDetails], ['Status', current.status],
      ['Created (IST)', `${current.createdDate || ''} ${current.createdTime || ''}`],
    ];

    const saveAmount = async () => {
      const amt = Number(parseIndianAmount(amount));
      if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
      if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
      const forwarding = canSendToApproval && sendApproval;
      if (forwarding && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
      setBusy(true);
      try {
        const updated = await agentTxnsAPI.manage(row.id, {
          amount: amt, notes: notes || undefined, sentForApproval: forwarding,
          approverUserId: forwarding ? Number(approverId) : undefined,
        });
        setCurrent(updated); setNotes('');
        showToast(`Amount updated for ${updated.referenceNumber}.`, 'success');
        loadAudit(); onRefresh();
      } catch (e) { showToast(agentTxnError(e, 'Failed to update the transaction.'), 'error'); }
      finally { setBusy(false); }
    };

    const decide = async (approve: boolean) => {
      setBusy(true);
      try {
        await (approve ? agentTxnsAPI.approve(row.id) : agentTxnsAPI.reject(row.id));
        showToast(approve ? 'Transaction approved.' : 'Transaction rejected.', 'success');
        onRefresh(); onClose();
      } catch (e) { showToast(agentTxnError(e, 'Action failed.'), 'error'); }
      finally { setBusy(false); }
    };

    return (
      <Modal title={`Manage ${current.referenceNumber}`} onClose={onClose} wide>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          {immutable.map(([k, v]) => (
            <div key={k}><div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{k}</div><div style={{ fontSize: 12.5, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{v}</div></div>
          ))}
        </div>
        <p style={{ fontSize: 11.5, color: T.textMuted, margin: '0 0 12px' }}>Only the Transaction Amount can be changed. Agent, membership, reference, code, token and note number are immutable.</p>
        {!AGENT_FINAL_STATUSES.includes(current.status) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', borderRadius: 8, background: T.warningBg, color: T.warning, fontSize: 11.5, fontWeight: 600, marginBottom: 12 }}>
            <span>⚠</span>
            Changing the amount restarts the approval workflow — this {current.type.toLowerCase()} returns to
            {current.type === 'DEPOSIT' ? ' Supervisor approval' : current.type === 'WITHDRAWAL' ? ' Manager approval' : ' Supervisor completion'}
            {' '}and any approval already given is voided.
          </div>
        )}

        <Input label="Transaction Amount" type="text" value={amount} onChange={e => setAmount(formatIndianAmountInput(e.target.value))} inputMode="decimal" />
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
        <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Reason for the correction (optional)"
          style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical', marginBottom: 12 }} />

        {canSendToApproval && (
          <div style={{ padding: 12, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}`, marginBottom: 14 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 13, fontWeight: 700, color: T.textMain }}>
              <input type="checkbox" checked={sendApproval} onChange={e => setSendApproval(e.target.checked)} style={{ width: 16, height: 16, cursor: 'pointer' }} />
              Send To Approval (optional)
            </label>
            {sendApproval && (
              <div style={{ marginTop: 10, maxWidth: 360 }}>
                <Sel label="Authorized Approver" value={approverId} onChange={e => setApproverId(e.target.value)} required
                  options={[{ value: '', label: '— Select approver —' }, ...(fd?.approvers || []).map(a => ({ value: String(a.id), label: `${a.name} (${a.role})` }))]} />
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 18 }}>
          <Btn onClick={saveAmount} disabled={busy}>{busy ? 'Saving…' : 'Update Amount'}</Btn>
          {canApprove && <>
            <Btn variant="success" onClick={() => decide(true)} disabled={busy}>Approve</Btn>
            <Btn variant="danger" onClick={() => decide(false)} disabled={busy}>Reject</Btn>
          </>}
          <Btn variant="secondary" onClick={onClose} disabled={busy}>Close</Btn>
        </div>

      <h3 style={{ margin: '0 0 8px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Audit History</h3>
        <div style={{ overflowX: 'auto', border: `1px solid ${T.border}`, borderRadius: 10 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Date & Time (IST)', 'Action', 'Old', 'New', 'By', 'Note'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {audit.length === 0 && <tr><td colSpan={6} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 18 }}>No history yet.</td></tr>}
              {audit.map(a => (
                <tr key={a.id} style={{ background: T.surface }}>
                  <td style={{ ...tdS, whiteSpace: 'nowrap', color: T.textMuted }}>{a.createdDate} {a.createdTime}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{a.action.replace(/_/g, ' ')}</td>
                  <td style={tdS}>{a.oldAmount == null ? '—' : fmt(a.oldAmount)}</td>
                  <td style={tdS}>{a.newAmount == null ? '—' : fmt(a.newAmount)}</td>
                  <td style={tdS}>{a.actor || '—'}{a.role ? ` (${a.role})` : ''}</td>
                  <td style={{ ...tdS, color: T.textMuted }}>{a.note || (a.approverName ? `→ ${a.approverName}` : '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Modal>
    );
  };

export const AgentManageTransactionPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => {
  const { showToast } = useToast();
  const role = String(user.merchantRole || '').toUpperCase();
  const canApprove = ['SUPERVISOR', 'MANAGER'].includes(role);
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [manageRow, setManageRow] = useState<AgentTxnRow | null>(null);
  const [ref, setRef] = useState('');
  const [agentIdF, setAgentIdF] = useState('');
  const [memberF, setMemberF] = useState('');
  const [dateF, setDateF] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');

  useEffect(() => { agentTxnsAPI.formData().then(setFd).catch(() => {}); }, []);

  const search = useCallback(async () => {
    setLoading(true);
    try {
      const q: AgentTxnQuery = {};
      if (dateF) q.date = dateF; else { if (fromF) q.date_from = fromF; if (toF) q.date_to = toF; }
      let data = await agentTxnsAPI.list(q);
      // Manage is CASH-only, and a finalised transaction can no longer be edited.
      data = data.filter(x => x.txnMethod === 'CASH' && !AGENT_FINAL_STATUSES.includes(x.status));
      const r = ref.trim().toLowerCase(), ag = agentIdF.trim().toLowerCase(), mem = memberF.trim().toLowerCase();
      if (r) data = data.filter(x => (x.referenceNumber || '').toLowerCase().includes(r));
      if (ag) data = data.filter(x => (x.agentCode || '').toLowerCase().includes(ag));
      if (mem) data = data.filter(x => (x.membershipId || '').toLowerCase().includes(mem));
      setRows(data);
    } catch { showToast('Failed to load transactions.', 'error'); }
    finally { setLoading(false); }
  }, [ref, agentIdF, memberF, dateF, fromF, toF, showToast]);

  useEffect(() => { search(); /* initial load */ }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const clearFilters = () => { setRef(''); setAgentIdF(''); setMemberF(''); setDateF(''); setFromF(''); setToF(''); };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Manage Transaction</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Correct the amount of an in-flight <strong>Cash</strong> agent transaction — other methods cannot be edited. Changing the amount restarts approval. Agent transactions only; merchant transactions are never affected.</p>
      </div>

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Reference Number" value={ref} onChange={e => setRef(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="Agent ID" value={agentIdF} onChange={e => setAgentIdF(e.target.value)} placeholder="AGT…" style={{ marginBottom: 0 }} />
          <Input label="Membership ID" value={memberF} onChange={e => setMemberF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="Date" type="date" value={dateF} onChange={e => setDateF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="From Date" type="date" value={fromF} onChange={e => setFromF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="To Date" type="date" value={toF} onChange={e => setToF(e.target.value)} style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
          <Btn size="sm" onClick={search} disabled={loading}>{loading ? 'Searching…' : 'Search'}</Btn>
          <Btn size="sm" variant="ghost" onClick={() => { clearFilters(); }}>Clear</Btn>
          <span style={{ fontSize: 12, color: T.textMuted, alignSelf: 'center' }}>{rows.length} manageable (Cash)</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Type', 'Agent', 'Membership', 'Amount', 'Created (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No manageable Cash transactions match the search.</td></tr>}
              {rows.map(x => (
                <tr key={x.id} style={{ background: T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.type}</td>
                  <td style={tdS}>{x.agentCode || '—'}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={tdS}><Btn size="sm" variant="ghost" onClick={() => setManageRow(x)}>Manage</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {manageRow && <ManageModal row={manageRow} fd={fd} canApprove={canApprove} role={role} onClose={() => setManageRow(null)} onRefresh={search} />}
    </div>
  );
};

// ─── Read-only View Details modal (Agent Deposit / Withdrawal Management) ───────
const DField: React.FC<{ k: string; v: React.ReactNode }> = ({ k, v }) => (
  <div>
    <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{k}</div>
    <div style={{ fontSize: 12.5, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{v == null || v === '' ? '—' : v}</div>
  </div>
);

// ─── Transaction Timeline — the status progression for one transaction ────────────────────────
// Built from the SAME per-method chain the backend enforces, so the steps always match the real
// workflow. Each step is done / current / pending; a rejected transaction ends on a red terminal.
// Audit timestamps enrich the steps that have actually happened.
type TlStep = { key: string; label: string };
const timelineSteps = (row: AgentTxnRow): TlStep[] => {
  const cash = isTokenMethod(row.txnMethod), crypto = isWalletMethod(row.txnMethod);
  if (row.type === 'DEPOSIT') {
    const req = cash ? 'TOKEN_REQUESTED' : crypto ? 'WALLET_REQUESTED' : 'ACCOUNT_REQUESTED';
    const sub: TlStep = cash ? { key: 'TOKEN_SUBMITTED', label: 'Token Submitted' }
      : crypto ? { key: 'WALLET_SUBMITTED', label: 'Wallet Submitted' }
      : { key: 'ACCOUNT_SUBMITTED', label: 'Account Submitted' };
    return [{ key: req, label: 'Deposit Created' }, sub,
      { key: 'SLIP_SUBMITTED', label: 'Supervisor Review' },
      { key: 'SUPERVISOR_APPROVED', label: 'Approved' }, { key: 'DEPOSITED', label: 'Deposited' }];
  }
  if (row.type === 'WITHDRAWAL') {
    if (cash || crypto) return [
      { key: cash ? 'TOKEN_SUBMITTED' : 'WALLET_SUBMITTED', label: 'Withdrawal Created · Manager Review' },
      { key: 'MANAGER_APPROVED', label: 'Manager Approved' }, { key: 'COMPLETED', label: 'Completed' }];
    return [{ key: 'ACCOUNT_SUBMITTED', label: 'Withdrawal Created' },
      { key: 'MANAGER_REVIEW', label: 'Paid · Manager Review' }, { key: 'COMPLETED', label: 'Completed' }];
  }
  return [{ key: 'SLIP_SUBMITTED', label: 'Settlement Created' }, { key: 'COMPLETED', label: 'Completed' }];
};

const TxnTimeline: React.FC<{ row: AgentTxnRow; audit: AgentTxnAuditRow[] }> = ({ row, audit }) => {
  const steps = timelineSteps(row);
  const done = (AGENT_COMPLETED_STATUSES as string[]).includes(row.status);
  const rejected = row.status === 'REJECTED';
  const curIdx = steps.findIndex(s => s.key === row.status);
  const reached = new Set(audit.map(a => a.action));   // statuses that actually happened
  const when = (k: string) => { const a = audit.find(x => x.action === k); return a ? `${a.createdDate || ''} ${a.createdTime || ''}`.trim() : ''; };
  const nodeState = (i: number): 'done' | 'current' | 'pending' => {
    if (done) return 'done';
    if (rejected) return reached.has(steps[i].key) || i === 0 ? 'done' : 'pending';
    if (curIdx < 0) return i === 0 ? 'current' : 'pending';
    return i < curIdx ? 'done' : i === curIdx ? 'current' : 'pending';
  };
  const color = { done: T.success, current: T.blue, pending: T.textMuted } as const;
  // One flat node list — the workflow steps, plus the red terminal when the transaction was
  // rejected. Rendering every node through the same row keeps the rail unbroken all the way to
  // the last entry instead of leaving the terminal floating below a stub of line.
  type TlNode = { label: string; ts: string; dot: string; filled: boolean; line: string; muted: boolean; current: boolean };
  const nodes: TlNode[] = steps.map((s, i) => {
    const st = nodeState(i);
    return { label: s.label, ts: when(s.key), dot: color[st], filled: st !== 'pending',
      line: st === 'done' ? T.success : T.border, muted: st === 'pending', current: st === 'current' };
  });
  if (rejected) nodes.push({ label: 'Rejected', ts: '', dot: T.danger, filled: true, line: T.border, muted: false, current: false });
  // Geometry of the rail, kept in one place so the dot, the connector and the text all line up.
  // ROW is the distance between two dot centres — equal spacing whether or not a step carries a
  // timestamp — and is a minimum, so a wrapped label grows the row instead of clipping. It has to
  // clear the tallest ordinary row (label + timestamp + the 12px tail) or those rows would push
  // past it and the spacing would drift. The connector stretches to fill whatever height the row
  // ends up with, which is what makes the segments meet edge-to-edge as one continuous line.
  // OFF drops the dot onto the centre of the label's first line; since that offset also pushes the
  // NEXT row's dot down, the connector reclaims it with an equal negative bottom margin — without
  // that, every junction shows a 2px break.
  const DOT = 14, LINE = 2, LH = 18, OFF = (LH - DOT) / 2, ROW = 48;
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 10 }}>Timeline</div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {nodes.map((n, i) => {
          const last = i === nodes.length - 1;
          return (
            <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'stretch', minHeight: last ? undefined : ROW }}>
              {/* Rail: dot on top, connector filling the rest of the row. The column stretches to
                  the full row height, so the connector ends exactly where the next dot begins. */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: DOT, flexShrink: 0 }}>
                <div style={{ width: DOT, height: DOT, borderRadius: '50%', background: n.filled ? n.dot : 'transparent', border: `2px solid ${n.dot}`, flexShrink: 0, marginTop: OFF, boxSizing: 'border-box' }} />
                {!last && <div style={{ width: LINE, flex: 1, minHeight: 8, background: n.line, borderRadius: LINE, marginBottom: -OFF }} />}
              </div>
              <div style={{ minWidth: 0, paddingBottom: last ? 0 : 12 }}>
                <p style={{ margin: 0, fontSize: 13, lineHeight: `${LH}px`, fontWeight: n.muted ? 600 : 800, color: n.label === 'Rejected' ? T.danger : n.muted ? T.textMuted : T.textMain, wordBreak: 'break-word' }}>{n.label}{n.current && <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, color: T.blue }}>CURRENT</span>}</p>
                {n.ts && <p style={{ margin: '2px 0 0', fontSize: 11, lineHeight: '15px', color: T.textMuted }}>{n.ts}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const AgentTxnDetailsModal: React.FC<{ row: AgentTxnRow; onClose: () => void }> = ({ row, onClose }) => {
  const [audit, setAudit] = useState<AgentTxnAuditRow[]>([]);
  const [comm, setComm] = useState<AgentTxnCommission | null>(null);
  useEffect(() => { agentTxnsAPI.audit(row.id).then(setAudit).catch(() => {}); }, [row.id]);
  // How this transaction's commission was calculated (item 6) — the exact figures behind it.
  useEffect(() => { agentTxnsAPI.txnCommission(row.id).then(setComm).catch(() => {}); }, [row.id]);

  const fields: Array<[string, React.ReactNode]> = [
    ['Reference Number', row.referenceNumber], ['Transaction Code', row.transactionCode],
    ['Type', row.type], ['Status', <StatusPill status={row.status} type={row.type} method={row.txnMethod} />],
    ['Agent', `${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`],
    ['Agent Country', row.agentCountry], ['Agent State', row.agentState],
    ['Agent Location', row.agentLocation], ['Agent Category', row.agentCategory],
    ['Membership ID', row.membershipId], ['Membership Name', row.membershipName],
    ['Membership Type', row.membershipType], ['Amount', fmt(row.amount)],
    ['Country', row.country], ['State', row.state], ['Location', row.location], ['Mobile', row.mobile],
    ['Token Details', row.tokenDetails], ['Note Number', row.noteNumber],
    ['Crypto Wallet Address', row.walletAddress],
    ['Instructions', row.instructions ? instrLabel(row.instructions) : null], ['Notes', row.notes],
    ['Sent For Approval', row.sentForApproval ? 'Yes' : 'No'], ['Approver', row.approverName],
    ['Approved By', row.approvedBy], ['Approved (IST)', row.approvedDate ? `${row.approvedDate} ${row.approvedTime || ''}` : null],
    // Payment evidence — stored on the transaction and re-read here; never re-uploaded.
    // A cash deposit has no UTR (no rail issues one), so the row is omitted rather than
    // rendered as a dash. Every other method, and every withdrawal, still shows it.
    ...(row.type === 'DEPOSIT' && isTokenMethod(row.txnMethod)
      ? [] : [['UTR Number', row.depositUtr] as [string, React.ReactNode]]),
    ['Slip By', row.slipSubmittedBy],
    ['Slip At (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
    ['Sent To (Agent A/C)', row.agentAccountRef ? `${row.agentAccountRef} · ${row.agentAccountDetail || ''}` : null],
    ['Paid To', [row.payoutAccountHolder, row.payoutAccountNumber || row.payoutUpiId, row.payoutBankName].filter(Boolean).join(' · ') || null],
    ['Supervisor', row.supervisorName], ['Manager', row.managerName], ['Review Remark', row.reviewRemark],
    ['Deposited By', row.depositedBy],
    ['Deposited (IST)', row.depositedDate ? `${row.depositedDate} ${row.depositedTime || ''}` : null],
    ['Created By', row.createdBy], ['Created (IST)', `${row.createdDate || ''} ${row.createdTime || ''}`],
  ];

  // The uploaded slip and the Mark-Deposit proof, shown from storage every time. Each renders
  // through SlipView, which shows the image (or names the PDF) and offers a Download.
  const proofLabel = isWalletMethod(row.txnMethod) ? 'Crypto Payment Slip'
    : isTokenMethod(row.txnMethod) ? 'Token Details Image' : 'Account Proof';
  // A cash withdrawal has no slip — what it carries is the operator's proof that the cash was
  // handed over, captured at Confirm & Complete. Name it for what it is.
  // The stored slip_image is a cash withdrawal's payment proof, a cash deposit's token image, or a
  // real payment slip for every other method — name it for what it actually is.
  const slipLabel = isTokenMethod(row.txnMethod)
    ? (row.type === 'WITHDRAWAL' ? 'Cash Payment Proof' : 'Token Image')
    : 'Uploaded Slip';
  const images: Array<[string, string]> = [
    ...(row.accountProof ? [[proofLabel, row.accountProof] as [string, string]] : []),
    ...(row.slipImage ? [[slipLabel, row.slipImage] as [string, string]] : []),
    ...(row.depositProof ? [['Deposit Proof', row.depositProof] as [string, string]] : []),
  ];

  return (
    <Modal title={`${row.type === 'DEPOSIT' ? 'Agent Deposit' : 'Agent Withdrawal'} — ${row.referenceNumber}`} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {fields.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>

      {/* Commission Breakdown (item 6) — how the commission was calculated + balance movement. */}
      {comm && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Commission Breakdown</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(160px,1fr))', gap: '10px 18px', padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
            <DField k="Agent Name" v={comm.agentName || '—'} />
            <DField k="Membership ID" v={comm.membershipId} />
            <DField k="Transaction Amount" v={fmt(comm.amount)} />
            <DField k="Commission %" v={`${comm.commissionPct}%`} />
            <DField k="Commission Amount" v={fmt(comm.commissionAmount)} />
            <DField k="Net Amount" v={fmt(comm.netAmount)} />
            <DField k="Balance Before" v={fmt(comm.balanceBefore)} />
            <DField k="Balance After" v={fmt(comm.balanceAfter)} />
            <DField k="Date & Time (IST)" v={`${row.createdDate || ''} ${row.createdTime || ''}`.trim() || '—'} />
          </div>
        </div>
      )}

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

      <TxnTimeline row={row} audit={audit} />

      <h3 style={{ margin: '0 0 8px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Audit History</h3>
      <div style={{ overflowX: 'auto', border: `1px solid ${T.border}`, borderRadius: 10, marginBottom: 16 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ background: T.canvas }}>{['Date & Time (IST)', 'Action', 'Old', 'New', 'By', 'Note'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
          <tbody>
            {audit.length === 0 && <tr><td colSpan={6} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 18 }}>No history yet.</td></tr>}
            {audit.map(a => (
              <tr key={a.id} style={{ background: T.surface }}>
                <td style={{ ...tdS, whiteSpace: 'nowrap', color: T.textMuted }}>{a.createdDate} {a.createdTime}</td>
                <td style={{ ...tdS, fontWeight: 700 }}>{a.action.replace(/_/g, ' ')}</td>
                <td style={tdS}>{a.oldAmount == null ? '—' : fmt(a.oldAmount)}</td>
                <td style={tdS}>{a.newAmount == null ? '—' : fmt(a.newAmount)}</td>
                <td style={tdS}>{a.actor || '—'}{a.role ? ` (${a.role})` : ''}</td>
                <td style={{ ...tdS, color: T.textMuted }}>{a.note || (a.approverName ? `→ ${a.approverName}` : '—')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}><Btn variant="secondary" onClick={onClose}>Close</Btn></div>
    </Modal>
  );
};

// ─── Settlement chain modals ───────────────────────────────────────────────────
// Settlement Requested → Settlement Accepted → Proof Uploaded → Settled. The payment itself is
// made OFFLINE (cash / bank transfer / crypto) between the merchant and the agent — nothing here
// initiates, verifies or simulates a payment. Clari5Pay records the workflow and the proof only.

/** Accept or reject a settlement request. Reject stays available until the payment begins. */
const SettlementDecisionModal: React.FC<{ row: AgentTxnRow; action: 'accept' | 'reject'; onClose: () => void; onDone: () => void }> =
  ({ row, action, onClose, onDone }) => {
    const { showToast } = useToast();
    const [remark, setRemark] = useState('');
    const [busy, setBusy] = useState(false);
    const accepting = action === 'accept';
    const go = async () => {
      if (!accepting && !remark.trim()) { showToast('A reason is required to reject.', 'error'); return; }
      setBusy(true);
      try {
        await (accepting ? agentTxnsAPI.settlementAccept(row.id, remark.trim())
          : agentTxnsAPI.settlementReject(row.id, remark.trim()));
        showToast(accepting ? 'Settlement accepted — the agent now settles offline.' : 'Settlement rejected.', 'success');
        onDone(); onClose();
      } catch (e) { showToast(agentTxnError(e, 'Could not update the settlement.'), 'error'); }
      finally { setBusy(false); }
    };
    return (
      <Modal title={`${accepting ? 'Accept' : 'Reject'} Settlement — ${row.referenceNumber}`} onClose={onClose}>
        <div style={{ display: 'grid', gap: 4, marginBottom: 14 }}>
          <ReadField label="Assigned Agent" value={`${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`} />
          <ReadField label="Settlement Method" value={methodLabel(row.txnMethod)} />
          <ReadField label="Settlement Amount" value={fmt(row.amount)} />
        </div>
        {accepting && (
          <p style={{ margin: '0 0 12px', fontSize: 12.5, color: T.textMuted, lineHeight: 1.5 }}>
            Accepting records that the agent will now settle <strong>outside Clari5Pay</strong> by {methodLabel(row.txnMethod)}.
            Upload the payment proof here once that is done.
          </p>
        )}
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          {accepting ? 'Remarks (optional)' : 'Reason for rejection'}
        </label>
        <textarea value={remark} maxLength={200} rows={3} onChange={e => setRemark(e.target.value)}
          placeholder={accepting ? 'Optional note' : 'Why is this being rejected?'}
          style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
        <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
          <Btn variant={accepting ? 'success' : 'danger'} onClick={go} disabled={busy}>
            {busy ? 'Saving…' : accepting ? 'Accept Settlement' : 'Reject Settlement'}</Btn>
          <Btn variant="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
        </div>
      </Modal>
    );
  };

/** Upload proof of the completed offline payment. Mandatory before a settlement can be settled. */
const SettlementProofModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [proof, setProof] = useState('');
  const [utr, setUtr] = useState('');
  const [busy, setBusy] = useState(false);
  const PROOF_HINT: Record<string, string> = {
    CASH: 'a cash receipt or acknowledgement', BANK: 'a bank transfer receipt', CRYPTO: 'a crypto transfer proof',
  };
  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 3 * 1024 * 1024) { showToast('Proof must be under 3 MB.', 'error'); return; }
    try { setProof(await fileToDataUrl(f)); } catch { showToast('Could not read that file.', 'error'); }
  };
  const go = async () => {
    if (!proof) { showToast('Payment proof is required.', 'error'); return; }
    setBusy(true);
    try {
      await agentTxnsAPI.settlementProof(row.id, { slipImage: proof, utr: utr.trim() || undefined });
      showToast('Proof uploaded.', 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Could not upload the proof.'), 'error'); }
    finally { setBusy(false); }
  };
  return (
    <Modal title={`Upload Payment Proof — ${row.referenceNumber}`} onClose={onClose}>
      <div style={{ display: 'grid', gap: 4, marginBottom: 14 }}>
        <ReadField label="Assigned Agent" value={`${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`} />
        <ReadField label="Settlement Method" value={methodLabel(row.txnMethod)} />
        <ReadField label="Settlement Amount" value={fmt(row.amount)} />
      </div>
      <p style={{ margin: '0 0 12px', fontSize: 12.5, color: T.textMuted, lineHeight: 1.5 }}>
        Attach evidence of the payment already made offline — {PROOF_HINT[String(row.txnMethod || '').toUpperCase()] || 'a payment receipt'}.
      </p>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Payment Proof <span style={{ color: T.danger }}>*</span>
      </label>
      <input type="file" accept="image/*,application/pdf" onChange={onFile} style={{ marginBottom: 12, fontSize: 13 }} />
      {proof && !proof.startsWith('data:application/pdf') && (
        <img src={proof} alt="Payment proof" style={{ maxWidth: 220, maxHeight: 200, objectFit: 'contain', borderRadius: 10, border: `1px solid ${T.border}`, display: 'block', marginBottom: 12 }} />)}
      {proof.startsWith('data:application/pdf') && <p style={{ fontSize: 12.5, color: T.textMuted, marginBottom: 12 }}>PDF attached.</p>}
      <Input label="Reference / UTR (optional)" value={utr} onChange={e => setUtr(e.target.value)}
        placeholder="UTR, transaction hash or receipt number" />
      <div style={{ display: 'flex', gap: 10, marginTop: 6 }}>
        <Btn variant="success" onClick={go} disabled={busy}>{busy ? 'Uploading…' : 'Upload Proof'}</Btn>
        <Btn variant="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
      </div>
    </Modal>
  );
};

/** Final step — mark the settlement Settled. Only reachable once the proof is on the record. */
const SettlementSettleModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try {
      await agentTxnsAPI.settlementSettle(row.id);
      showToast('Settlement completed.', 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Could not complete the settlement.'), 'error'); }
    finally { setBusy(false); }
  };
  return (
    <Modal title={`Mark Settled — ${row.referenceNumber}`} onClose={onClose}>
      <div style={{ display: 'grid', gap: 4, marginBottom: 14 }}>
        <ReadField label="Assigned Agent" value={`${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`} />
        <ReadField label="Settlement Method" value={methodLabel(row.txnMethod)} />
        <ReadField label="Settlement Amount" value={fmt(row.amount)} />
      </div>
      {row.slipImage && <SlipView label="Payment Proof" src={row.slipImage} filename={`proof-${row.referenceNumber}`} />}
      <p style={{ margin: '12px 0', fontSize: 12.5, color: T.textMuted, lineHeight: 1.5 }}>
        This records the settlement as complete against the uploaded proof. It does not move any
        money — the payment was made offline.
      </p>
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn variant="success" onClick={go} disabled={busy}>{busy ? 'Saving…' : 'Mark Settled'}</Btn>
        <Btn variant="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// ─── Agent Deposit / Withdrawal Management (operator list + create) ─────────────
// Mirrors the Merchant Deposit/Withdrawal Management pages, but reads and writes ONLY the isolated
// Agent Transaction ledger (filtered to this txn_type) — never any merchant module. The "+ Create"
// button opens the existing Agent Request form (reused, embedded), not a new form.
const PAGE_SIZE = 10;

const AgentTxnManagementPage: React.FC<{
  user: User;
  txnType: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT';
  title: string;
  noun: string;
  requestLabel: string;
  FormComp: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }>;
}> = ({ user, txnType, title, noun, requestLabel, FormComp }) => {
  const { showToast } = useToast();
  const role = String(user.merchantRole || '').toUpperCase();
  const canApprove = ['SUPERVISOR', 'MANAGER'].includes(role);
  // Deposit-chain operator steps — the Data Operator does what the Admin does in the merchant flow.
  const canOperate = ['DEO', 'DEPOSIT_OPERATOR'].includes(role);
  // Withdrawals are paid by the operator; settlements by the Supervisor (no approval in between).
  const canPayout = txnType === 'SETTLEMENT'
    ? role === 'SUPERVISOR'
    : ['DEO', 'WITHDRAWAL_OPERATOR'].includes(role);
  const isDeposit = txnType === 'DEPOSIT';
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [detailRow, setDetailRow] = useState<AgentTxnRow | null>(null);
  // Deposit-chain steps, each driven by the row's current status.
  const [acctRow, setAcctRow] = useState<AgentTxnRow | null>(null);
  const [slipRow, setSlipRow] = useState<AgentTxnRow | null>(null);
  const [depositRow, setDepositRow] = useState<AgentTxnRow | null>(null);
  const [payoutRow, setPayoutRow] = useState<AgentTxnRow | null>(null);
  // Settlement chain steps.
  const [acceptRow, setAcceptRow] = useState<AgentTxnRow | null>(null);
  const [rejectRow, setRejectRow] = useState<AgentTxnRow | null>(null);
  const [proofRow, setProofRow] = useState<AgentTxnRow | null>(null);
  const [settleRow, setSettleRow] = useState<AgentTxnRow | null>(null);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [dateF, setDateF] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);

  useEffect(() => { agentTxnsAPI.formData().then(setFd).catch(() => {}); }, []);

  // `background: true` refreshes the rows without touching `loading`. The Search button reads
  // `loading`, so letting the 20s poll (and every window-focus / tab-visibility refetch) drive it
  // made the button flip to "Searching…" and back on its own — the flicker. Only a real,
  // user-initiated search now moves that state.
  const load = useCallback(async (opts?: { background?: boolean }) => {
    const background = opts?.background === true;
    if (!background) setLoading(true);
    try {
      const q: AgentTxnQuery = { txn_type: txnType };
      if (status) q.status = status;
      if (search.trim()) q.search = search.trim();
      if (dateF) q.date = dateF; else { if (fromF) q.date_from = fromF; if (toF) q.date_to = toF; }
      setRows(await agentTxnsAPI.list(q));
    } catch { if (!background) showToast(`Failed to load Agent ${noun} requests.`, 'error'); }
    finally { if (!background) setLoading(false); }
  }, [txnType, status, search, dateF, fromF, toF, noun, showToast]);

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { if (!showForm && !detailRow) load({ background: true }); });

  const runSearch = () => { setPage(1); load(); };
  const clearFilters = () => { setSearch(''); setStatus(''); setDateF(''); setFromF(''); setToF(''); setPage(1); };

  // Agent Category comes from Agent Master (the /form-data agent list), never from the transaction.
  // The row's own agent_category is the Agent Master value snapshotted at creation — used only as a
  // fallback for agents no longer present in the master list.
  const categoryOf = (x: AgentTxnRow) =>
    fd?.agents.find(a => a.id === x.agentMasterId)?.category || x.agentCategory || '—';

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>{title}</h1>
          <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>All Agent {noun} requests from the isolated Agent ledger.</p>
        </div>
        <Btn onClick={() => setShowForm(true)}>+ Create {requestLabel}</Btn>
      </div>

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent" style={{ marginBottom: 0 }} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, ...STATUS_FILTER_OPTIONS]} />
          <Input label="Date" type="date" value={dateF} onChange={e => setDateF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="From Date" type="date" value={fromF} onChange={e => setFromF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="To Date" type="date" value={toF} onChange={e => setToF(e.target.value)} style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center' }}>
          <Btn size="sm" onClick={runSearch} disabled={loading}>{loading ? 'Searching…' : 'Search'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters}>Clear</Btn>
          <span style={{ fontSize: 12, color: T.textMuted, marginLeft: 'auto' }}>{rows.length} {noun.toLowerCase()}{rows.length === 1 ? '' : 's'}</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Agent', 'Agent Category', 'Membership', 'Amount', 'Status', 'Created (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={8} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && pageRows.length === 0 && <tr><td colSpan={8} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No Agent {noun} requests match the search.</td></tr>}
              {pageRows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{categoryOf(x)}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} type={x.type} method={x.txnMethod} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={{ ...tdS, whiteSpace: 'nowrap', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <Btn size="sm" variant="ghost" onClick={() => setDetailRow(x)}>View Details</Btn>
                    {/* The deposit chain's next step, offered only to the operator roles that may
                        perform it and only at the status that expects it. */}
                    {/* Cash names its two operator steps after the token it handles — Submit Token
                        then Upload Token — since there is no account to submit and no slip to pay. */}
                    {isDeposit && canOperate && x.status === requestedStatus(x.txnMethod) && <Btn size="sm" onClick={() => setAcctRow(x)}>{isTokenMethod(x.txnMethod) ? 'Submit Token' : 'Submit Account'}</Btn>}
                    {isDeposit && canOperate && x.status === submittedStatus(x.txnMethod) && <Btn size="sm" onClick={() => setSlipRow(x)}>{isTokenMethod(x.txnMethod) ? 'Upload Token' : 'Pay / Upload Slip'}</Btn>}
                    {isDeposit && canOperate && x.status === 'SUPERVISOR_APPROVED' && <Btn size="sm" variant="success" onClick={() => setDepositRow(x)}>Mark Deposit</Btn>}
                    {/* Withdrawal — BANK/UPI: created ready to pay (ACCOUNT_SUBMITTED), the Manager
                        reviews the slip afterwards. CASH/CRYPTO: authorised first, so the operator
                        confirms only once the Manager has approved (MANAGER_APPROVED). Settlement:
                        no gate at all, created at SLIP_SUBMITTED. */}
                    {!isDeposit && txnType !== 'SETTLEMENT' && canPayout && x.status === (
                      isSpecialMethod(x.txnMethod) ? 'MANAGER_APPROVED' : 'ACCOUNT_SUBMITTED')
                      && <Btn size="sm" variant="success" onClick={() => setPayoutRow(x)}>
                        {isSpecialMethod(x.txnMethod) ? 'Confirm & Complete' : 'Pay / Upload Slip'}</Btn>}
                    {/* Settlement chain — Requested → Accepted → Proof Uploaded → Settled. The
                        payment happens offline between merchant and agent; these actions only
                        record the workflow and the proof. Reject is available until the payment
                        process begins (i.e. up to and including Accepted). */}
                    {txnType === 'SETTLEMENT' && x.status === 'SETTLEMENT_REQUESTED' && (
                      <Btn size="sm" variant="success" onClick={() => setAcceptRow(x)}>Accept</Btn>)}
                    {txnType === 'SETTLEMENT' && x.status === 'SETTLEMENT_ACCEPTED' && (
                      <Btn size="sm" variant="success" onClick={() => setProofRow(x)}>Upload Proof</Btn>)}
                    {txnType === 'SETTLEMENT' && x.status === 'PROOF_UPLOADED' && (
                      <Btn size="sm" variant="success" onClick={() => setSettleRow(x)}>Mark Settled</Btn>)}
                    {txnType === 'SETTLEMENT' && ['SETTLEMENT_REQUESTED', 'SETTLEMENT_ACCEPTED'].includes(x.status) && (
                      <Btn size="sm" variant="danger" onClick={() => setRejectRow(x)}>Reject</Btn>)}
                    {/* No Manage here — amount corrections are done only in the dedicated
                        Manage Transaction page, so there is a single entry point. */}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {totalPages > 1 && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 10, padding: '12px 16px', borderTop: `1px solid ${T.border}` }}>
            <Btn size="sm" variant="ghost" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={safePage <= 1}>‹ Prev</Btn>
            <span style={{ fontSize: 12, color: T.textMuted }}>Page {safePage} of {totalPages}</span>
            <Btn size="sm" variant="ghost" onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={safePage >= totalPages}>Next ›</Btn>
          </div>
        )}
      </Card>

      {showForm && (
        <Modal title={`Create ${requestLabel}`} onClose={() => setShowForm(false)} xl>
          <FormComp user={user} embedded onSubmitted={() => { setShowForm(false); setPage(1); load(); }} />
        </Modal>
      )}
      {detailRow && <AgentTxnDetailsModal row={detailRow} onClose={() => setDetailRow(null)} />}
      {acctRow && <SubmitAccountModal row={acctRow} onClose={() => setAcctRow(null)} onDone={load} />}
      {slipRow && <UploadSlipModal row={slipRow} onClose={() => setSlipRow(null)} onDone={load} />}
      {depositRow && <MarkDepositModal row={depositRow} onClose={() => setDepositRow(null)} onDone={load} />}
      {payoutRow && <UploadSlipModal row={payoutRow} mode="payout" onClose={() => setPayoutRow(null)} onDone={load} />}
      {acceptRow && <SettlementDecisionModal row={acceptRow} action="accept" onClose={() => setAcceptRow(null)} onDone={load} />}
      {rejectRow && <SettlementDecisionModal row={rejectRow} action="reject" onClose={() => setRejectRow(null)} onDone={load} />}
      {proofRow && <SettlementProofModal row={proofRow} onClose={() => setProofRow(null)} onDone={load} />}
      {settleRow && <SettlementSettleModal row={settleRow} onClose={() => setSettleRow(null)} onDone={load} />}
    </div>
  );
};

export const AgentDepositManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="DEPOSIT" title="Deposit Request" noun="Deposit" requestLabel="Agent Deposit Request" FormComp={AgentDepositRequestPage} />
);

export const AgentWithdrawalManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="WITHDRAWAL" title="Withdrawal Request" noun="Withdrawal" requestLabel="Agent Withdrawal Request" FormComp={AgentWithdrawalRequestPage} />
);

// ─── Agent Settlement Management (Supervisor-only) ─────────────────────────────
// Mirrors Agent Withdrawal Management with the approval gate removed: the Supervisor raises the
// settlement and pays it themselves. Methods are Cash / Bank Transfer / Crypto. Fully isolated
// from Merchant Settlement — reads and writes only the agent ledger.
const AgentSettlementRequestForm: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }> = (props) => (
  <AgentWithdrawalRequestPage {...props} mode="settlement" />
);

export const AgentSettlementManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="SETTLEMENT" title="Settlement Request"
    noun="Settlement" requestLabel="Agent Settlement Request" FormComp={AgentSettlementRequestForm} />
);

// ─── Agent Reports (isolated) ──────────────────────────────────────────────────
// Financial summary comes from the SAME /api/agent-txns/overview endpoint the Agent Overview uses
// (one shared calculation source), and the detailed exportable ledger comes from /api/agent-txns.
// Both read ONLY the isolated agent ledger — never any merchant module. CSV export is client-side.
const csvEscape = (v: string | number) => {
  const s = String(v ?? '');
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const downloadAgentCsv = (filename: string, headers: string[], rows: Array<Array<string | number>>) => {
  const csv = [headers, ...rows].map(r => r.map(csvEscape).join(',')).join('\r\n');
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });   // BOM → Excel-friendly
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename.endsWith('.csv') ? filename : `${filename}.csv`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

// ── Agent report model ────────────────────────────────────────────────────────────────────────
// The three reports share ONE applied filter set, exactly like the Merchant/Admin Reports: editing
// a field only touches `draft`; nothing filters until "Apply Filters" commits it to `f`.
type AgentRTab = 'full' | 'commission' | 'ledger';

interface AgentRFilters {
  ref: string; membershipId: string; memberName: string;
  agentId: string; agentName: string; agentCategory: string;
  type: string; status: string; approvedBy: string;
  minA: string; maxA: string; exactA: string;
  datePreset: string; from: string; to: string;
}
const AGENT_EMPTY_FILTERS: AgentRFilters = {
  ref: '', membershipId: '', memberName: '', agentId: '', agentName: '', agentCategory: '',
  type: '', status: '', approvedBy: '', minA: '', maxA: '', exactA: '', datePreset: 'all', from: '', to: '',
};

const AGENT_CATEGORIES = [
  { value: 'CASH', label: 'Cash' }, { value: 'BANK_TRANSFER', label: 'Bank Transfer' }, { value: 'CRYPTO', label: 'Crypto' },
];
const AGENT_TYPE_OPTIONS = [
  { value: 'DEPOSIT', label: 'Deposit' }, { value: 'WITHDRAWAL', label: 'Withdrawal' }, { value: 'SETTLEMENT', label: 'Settlement' },
];
const typeLabelA = (t: string) => AGENT_TYPE_OPTIONS.find(o => o.value === t)?.label || t;
const statusLabelA = (s: string) => STATUS_STYLE[s]?.label || String(s || '').replace(/_/g, ' ');

/** Only Completed transactions feed reports, cards, ledger, commission and balance. */
const isCompletedA = (r: AgentTxnRow) => AGENT_COMPLETED_STATUSES.includes(r.status);
const commissionA = (r: AgentTxnRow) => Math.max(0, r.commissionAmount ?? 0);
/** This leg's effect on the agent balance — the SAME per-leg formula the backend uses
 *  (`_signed_leg`): a deposit credits net of Pay-In, a withdrawal/settlement debits the amount
 *  plus its own Pay-Out/Settlement commission. No new commission formula is introduced here. */
const signedLegA = (r: AgentTxnRow) =>
  r.type === 'DEPOSIT' ? (r.amount - commissionA(r)) : -(r.amount + commissionA(r));

// ── Timestamps ────────────────────────────────────────────────────────────────────────────────
// The API returns the IST *display* parts (`createdDate` "2026-07-18", `createdTime` "10:34:24 AM")
// alongside the true UTC instant (`createdAt`). The 12-hour time is NOT parseable by `new Date()`
// — `new Date("2026-07-18T10:34:24 AM")` is Invalid Date — so every comparison against it silently
// yields NaN. Always resolve the instant through this helper.
const IST_TZ = 'Asia/Kolkata';
const rowTsA = (r: AgentTxnRow): number => {
  if (r.createdAt) {
    const t = Date.parse(r.createdAt);
    if (!Number.isNaN(t)) return t;
  }
  const d = (r.createdDate || '').trim();
  if (!d) return 0;
  // Fall back to the IST display parts, converting the 12-hour clock to 24-hour and pinning the
  // offset to IST so the viewer's own timezone cannot shift the instant.
  const m = /^(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)?$/i.exec((r.createdTime || '').trim());
  let h = 0, mi = 0, s = 0;
  if (m) {
    h = Number(m[1]); mi = Number(m[2]); s = Number(m[3]);
    const ap = (m[4] || '').toUpperCase();
    if (ap === 'PM' && h < 12) h += 12;
    if (ap === 'AM' && h === 12) h = 0;
  }
  const pad = (n: number) => String(n).padStart(2, '0');
  const t = Date.parse(`${d}T${pad(h)}:${pad(mi)}:${pad(s)}+05:30`);
  return Number.isNaN(t) ? 0 : t;
};

/** An IST calendar date (YYYY-MM-DD), optionally shifted by whole days. */
const istDateStr = (shiftDays = 0) =>
  new Date(Date.now() + shiftDays * 86400000).toLocaleDateString('en-CA', { timeZone: IST_TZ });
/** The instant of 00:00:00 IST on an IST calendar date. */
const istMidnight = (dateStr: string) => Date.parse(`${dateStr}T00:00:00+05:30`);

/**
 * [start, end] instants for a date preset — the SINGLE source of truth for the period. The table,
 * summary cards, footer totals, Opening/Closing Balance and every export all derive from these
 * bounds, so they can never disagree. Calendar presets (Today/Yesterday/7d/30d/Custom) resolve on
 * IST calendar days; rolling presets (30m/1h/24h) are true time windows ending now.
 */
const periodBoundsA = (preset: string, from: string, to: string): [number, number] => {
  const now = Date.now();
  switch (preset) {
    case 'today': return [istMidnight(istDateStr(0)), now];
    case 'yesterday': return [istMidnight(istDateStr(-1)), istMidnight(istDateStr(0)) - 1];
    case '30m': return [now - 30 * 60000, now];
    case '1h': return [now - 3600000, now];
    case '24h': return [now - 86400000, now];
    case '7d': return [istMidnight(istDateStr(-7)), now];
    case '30d': return [istMidnight(istDateStr(-30)), now];
    case 'custom': return [
      from ? istMidnight(from) : -Infinity,
      to ? istMidnight(to) + 86400000 - 1 : Infinity,
    ];
    default: return [-Infinity, Infinity];   // 'all'
  }
};
/** Completed Date & Time — the authoritative moment the money moved, stamped by whichever route
 *  completed the transaction (`completed_at`). Historical rows were backfilled from the audit
 *  trail. The per-route fallback only covers a row the backfill could not resolve at all. */
const completedAtA = (r: AgentTxnRow): string => {
  if (!isCompletedA(r)) return '—';
  if (r.completedDate) return `${r.completedDate} ${r.completedTime || ''}`.trim();
  const pick: Array<[string | null | undefined, string | null | undefined]> = r.type === 'DEPOSIT'
    ? [[r.depositedDate, r.depositedTime], [r.approvedDate, r.approvedTime], [r.updatedDate, r.updatedTime]]
    : [[r.approvedDate, r.approvedTime], [r.depositedDate, r.depositedTime], [r.updatedDate, r.updatedTime]];
  for (const [d, t] of pick) if (d) return `${d} ${t || ''}`.trim();
  return '—';
};
/** Approver actually on the record — the explicit approver, else the gate that cleared it. */
const approverA = (r: AgentTxnRow) => r.approvedBy || r.managerName || r.supervisorName || '—';

const inBoundsA = (r: AgentTxnRow, [start, end]: [number, number]): boolean => {
  const ts = rowTsA(r);
  return ts >= start && ts <= end;
};

/** Every filter EXCEPT the date window. Split out because the Agent Ledger needs to accumulate the
 *  running balance over this scope *before* the period starts, to derive the Opening Balance. */
const matchesNonDateA = (r: AgentTxnRow, f: AgentRFilters): boolean => {
  const inc = (v: string | null | undefined, q: string) => !q || (v || '').toLowerCase().includes(q.toLowerCase());
  return inc(r.referenceNumber, f.ref) && inc(r.membershipId, f.membershipId) && inc(r.membershipName, f.memberName)
    && inc(r.agentCode, f.agentId) && inc(r.agentName, f.agentName)
    && (!f.agentCategory || String(r.agentCategory || '').toUpperCase() === f.agentCategory)
    && (!f.type || r.type === f.type) && (!f.status || r.status === f.status)
    && (!f.approvedBy || approverA(r).toLowerCase().includes(f.approvedBy.toLowerCase()))
    && (!f.minA || r.amount >= Number(parseIndianAmount(f.minA)))
    && (!f.maxA || r.amount <= Number(parseIndianAmount(f.maxA)))
    && (!f.exactA || r.amount === Number(parseIndianAmount(f.exactA)));
};

const matchesA = (r: AgentTxnRow, f: AgentRFilters, bounds: [number, number]): boolean =>
  matchesNonDateA(r, f) && inBoundsA(r, bounds);

/** Completed-only totals for a set of agent rows, per leg and net of commission. */
const totalsA = (rows: AgentTxnRow[]) => {
  const done = rows.filter(isCompletedA);
  const amt = (t: string) => done.filter(r => r.type === t).reduce((a, r) => a + r.amount, 0);
  const com = (t: string) => done.filter(r => r.type === t).reduce((a, r) => a + commissionA(r), 0);
  const deposits = amt('DEPOSIT'), withdrawals = amt('WITHDRAWAL'), settlements = amt('SETTLEMENT');
  const depositCommission = com('DEPOSIT'), withdrawalCommission = com('WITHDRAWAL'), settlementCommission = com('SETTLEMENT');
  return {
    count: done.length, deposits, withdrawals, settlements,
    depositCommission, withdrawalCommission, settlementCommission,
    commission: depositCommission + withdrawalCommission + settlementCommission,
    net: done.reduce((a, r) => a + signedLegA(r), 0),
  };
};

const AGENT_FULL_HEADERS = ['Date', 'Time', 'Transaction Reference', 'Membership ID', 'Member Name', 'Agent ID', 'Agent Name', 'Agent Category', 'Transaction Type', 'Gross Amount', 'Commission %', 'Commission Amount', 'Net Amount', 'Status', 'Created By', 'Approved By', 'Created Date & Time', 'Completed Date & Time'];
const AGENT_FULL_ALIGNS: Array<'l' | 'r'> = ['l', 'l', 'l', 'l', 'l', 'l', 'l', 'l', 'l', 'r', 'r', 'r', 'r', 'l', 'l', 'l', 'l', 'l'];
const AGENT_COMM_HEADERS = ['Agent ID', 'Agent Name', 'Agent Category', 'Total Deposit Amount', 'Deposit Commission', 'Total Withdrawal Amount', 'Withdrawal Commission', 'Total Settlement Amount', 'Settlement Commission', 'Total Commission Earned', 'Total Completed Transactions'];
const AGENT_COMM_ALIGNS: Array<'l' | 'r'> = ['l', 'l', 'l', 'r', 'r', 'r', 'r', 'r', 'r', 'r', 'r'];
const AGENT_LEDGER_HEADERS = ['Date', 'Time', 'Description', 'Transaction Reference', 'Agent ID', 'Agent Name', 'Membership ID', 'Member Name', 'Transaction Type', 'Gross Amount', 'Commission', 'Running Balance', 'Status'];
const AGENT_LEDGER_ALIGNS: Array<'l' | 'r'> = ['l', 'l', 'l', 'l', 'l', 'l', 'l', 'l', 'l', 'r', 'r', 'r', 'l'];

const signedA = (n: number) => `${n >= 0 ? '+' : '−'}${fmt(Math.abs(n))}`;
const commissionTextA = (c: number) => (c > 0 ? `−${fmt(c)}` : fmt(0));

/** Summary card — same shape/colour treatment as the Merchant & Admin report cards. */
const ACard: React.FC<{ label: string; value: React.ReactNode; color: string }> = ({ label, value, color }) => (
  <Card className="c5-hover-lift" style={{ padding: '14px 16px', borderTop: `3px solid ${color}` }}>
    <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
    <p style={{ margin: 0, fontSize: 19, fontWeight: 800, color }}>{value}</p>
  </Card>
);
/** Footer / metadata cell — matches the Merchant report footer summary. */
const AMeta: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div>
    <span style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'block' }}>{label}</span>
    <span style={{ fontSize: 13, fontWeight: 700, color: T.textMain }}>{value}</span>
  </div>
);

// The table body scrolls inside a capped viewport so the totals card sits immediately beneath the
// table however many rows are on the page — no hunting for the footer, and no dead space when the
// page is short. The header stays pinned while the rows scroll.
const A_SCROLL: React.CSSProperties = { overflowX: 'auto', overflowY: 'auto', maxHeight: 420 };
const thStickyA: React.CSSProperties = { ...thR, position: 'sticky', top: 0, zIndex: 1, background: T.canvas, whiteSpace: 'nowrap' };

/** Row count + pager, rendered inside the table card so it never separates from its table. */
const APager: React.FC<{ page: number; totalPages: number; count: number; onPage: (p: number) => void }> =
  ({ page, totalPages, count, onPage }) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '9px 14px', borderTop: `1px solid ${T.border}` }}>
      <span style={{ fontSize: 12, color: T.textMuted }}>{count} row{count === 1 ? '' : 's'}</span>
      {totalPages > 1 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Btn size="sm" variant="ghost" onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1}>‹ Prev</Btn>
          <span style={{ fontSize: 12, color: T.textMuted }}>Page {page} of {totalPages}</span>
          <Btn size="sm" variant="ghost" onClick={() => onPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages}>Next ›</Btn>
        </div>
      )}
    </div>
  );

export const AgentTxnReportsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => {
  const { showToast } = useToast();
  const [ov, setOv] = useState<AgentOverview | null>(null);
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<AgentRTab>('full');
  const [draft, setDraft] = useState<AgentRFilters>(AGENT_EMPTY_FILTERS);
  const [f, setF] = useState<AgentRFilters>(AGENT_EMPTY_FILTERS);
  const [applying, setApplying] = useState(false);
  const [page, setPage] = useState(1);
  const [genAt] = useState(() => new Date());
  const set = (k: keyof AgentRFilters, v: string) => setDraft(p => ({ ...p, [k]: v }));

  const loadSummary = useCallback(() => {
    agentTxnsAPI.overview().then(setOv).catch(() => showToast('Failed to load financial summary.', 'error'));
  }, [showToast]);

  // The whole (business-scoped) agent ledger is pulled once and filtered client-side — the same
  // pattern the Merchant/Admin Reports use, so filters, totals and exports always agree.
  const loadRows = useCallback(async (opts?: { background?: boolean }) => {
    const background = opts?.background === true;
    if (!background) setLoading(true);
    try {
      const q: AgentTxnQuery = {};
      setRows(await agentTxnsAPI.list(q));
    } catch { if (!background) showToast('Failed to load agent transactions.', 'error'); }
    finally { if (!background) setLoading(false); }
  }, [showToast]);

  useEffect(() => { loadSummary(); loadRows(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { loadSummary(); loadRows({ background: true }); });

  // Apply Filters — refresh from the server, then commit the draft so table, cards, footer
  // totals, count and exports all move to the same filter set together.
  const applyFilters = async () => {
    if (applying) return;
    if (draft.datePreset === 'custom' && draft.from && draft.to && draft.to < draft.from) {
      showToast('“To” date cannot be earlier than “From”.', 'error'); return;
    }
    setApplying(true);
    try { await loadRows({ background: true }); setF(draft); setPage(1); }
    finally { setApplying(false); }
  };
  const clearFilters = async () => {
    if (applying) return;
    setApplying(true);
    setDraft(AGENT_EMPTY_FILTERS);
    try { await loadRows({ background: true }); setF(AGENT_EMPTY_FILTERS); setPage(1); }
    finally { setApplying(false); }
  };

  const bounds = periodBoundsA(f.datePreset, f.from, f.to);
  const filtered = rows.filter(r => matchesA(r, f, bounds));
  const tot = totalsA(filtered);
  const rangeLabel = f.datePreset === 'custom'
    ? `${f.from || 'start'} → ${f.to || 'today'}`
    : (DATE_PRESETS.find(d => d[0] === f.datePreset)?.[1] || 'All Time');
  const generatedBy = user?.name || '—';
  // Current Available Balance — the module-wide figure from the shared overview endpoint
  // (completed deposits net of commission, less completed withdrawals/settlements + theirs).
  const availableBalance = ov?.cards.netAmount ?? 0;
  // Agent + status dropdown options, derived from the ledger itself (no extra API call).
  const agentOptions = Array.from(new Map(rows.filter(r => r.agentCode)
    .map(r => [r.agentCode as string, r.agentName || ''])).entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([code, name]) => ({ value: code, label: name ? `${code} · ${name}` : code }));
  const statusOptions = Array.from(new Set(rows.map(r => r.status)))
    .map(s => ({ value: s, label: statusLabelA(s) }));

  // ── Commission Report: completed rows aggregated per agent, using each row's own commission ──
  const commissionRows = (() => {
    const by = new Map<string, {
      agentId: string; agentName: string; category: string;
      depositAmount: number; depositCommission: number;
      withdrawalAmount: number; withdrawalCommission: number;
      settlementAmount: number; settlementCommission: number;
      totalCommission: number; count: number;
    }>();
    for (const r of filtered) {
      if (!isCompletedA(r)) continue;
      const key = r.agentCode || '—';
      let a = by.get(key);
      if (!a) {
        a = { agentId: key, agentName: r.agentName || '—', category: String(r.agentCategory || '').toUpperCase(),
          depositAmount: 0, depositCommission: 0, withdrawalAmount: 0, withdrawalCommission: 0,
          settlementAmount: 0, settlementCommission: 0, totalCommission: 0, count: 0 };
        by.set(key, a);
      }
      const com = commissionA(r);
      if (r.type === 'DEPOSIT') { a.depositAmount += r.amount; a.depositCommission += com; }
      else if (r.type === 'WITHDRAWAL') { a.withdrawalAmount += r.amount; a.withdrawalCommission += com; }
      else { a.settlementAmount += r.amount; a.settlementCommission += com; }
      a.totalCommission += com; a.count += 1;
    }
    return Array.from(by.values()).sort((x, y) => y.totalCommission - x.totalCommission);
  })();

  // ── Agent Ledger: completed rows oldest-first with a running balance net of commission.
  // Computed over the FULL completed ledger and then narrowed to the displayed rows, so the
  // balance carries forward across a filter exactly like a bank statement (and like the
  // Merchant Ledger Report). Opening Balance is the balance immediately BEFORE the first row shown.
  // The ledger scope is every completed transaction matching the NON-date filters, oldest first.
  // The running balance accumulates across that whole scope; the date period then decides which
  // rows are displayed. So Opening Balance is the balance carried INTO the period — the balance
  // after every scoped transaction that happened before it — and stays correct even when the
  // period itself contains no transactions.
  const [periodStart] = bounds;
  const scope = rows.filter(r => isCompletedA(r) && matchesNonDateA(r, f))
    .slice().sort((a, b) => rowTsA(a) - rowTsA(b));
  let running = 0;
  const fullLedger = scope.map(r => {
    running += signedLegA(r);
    return {
      date: r.createdDate || '', time: r.createdTime || '',
      description: `${typeLabelA(r.type)} — ${r.membershipId}${r.membershipName ? ` · ${r.membershipName}` : ''}`,
      ref: r.referenceNumber, agentId: r.agentCode || '—', agentName: r.agentName || '—',
      membershipId: r.membershipId, memberName: r.membershipName || '—', type: r.type,
      amount: r.amount, signedAmount: signedLegA(r) >= 0 ? r.amount : -r.amount,
      commission: commissionA(r), balance: running, status: r.status,
      ts: rowTsA(r), shown: inBoundsA(r, bounds),
    };
  });
  const ledger = fullLedger.filter(l => l.shown);
  // Opening Balance = balance after everything BEFORE the period start. For All Time there is
  // nothing before, so it is the initial ledger balance of 0.
  const before = fullLedger.filter(l => l.ts < periodStart);
  const opening = before.length ? before[before.length - 1].balance : 0;
  // Closing Balance = the last Running Balance in the report; with no entries the period neither
  // credits nor debits, so it closes where it opened.
  const closing = ledger.length ? ledger[ledger.length - 1].balance : opening;

  // ── Pagination (same control as the rest of the Agent module) ──
  const activeRows: unknown[] = tab === 'full' ? filtered : tab === 'commission' ? commissionRows : ledger;
  const totalPages = Math.max(1, Math.ceil(activeRows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const slice = <X,>(xs: X[]) => xs.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  // ── Exports — PDF/Print via the shared print-to-PDF letterhead, Excel via the shared xlsx
  // helper, CSV via the shared CSV helper. Every export honours the applied filters. ──
  const stamp = today();
  const fullCsv = filtered.map(r => [r.createdDate || '', r.createdTime || '', r.referenceNumber, r.membershipId, r.membershipName || '', r.agentCode || '', r.agentName || '', r.agentCategory || '', typeLabelA(r.type), r.amount, r.commissionPct ?? 0, commissionA(r), r.netAmount ?? 0, statusLabelA(r.status), r.createdBy || '', approverA(r) === '—' ? '' : approverA(r), `${r.createdDate || ''} ${r.createdTime || ''}`.trim(), completedAtA(r) === '—' ? '' : completedAtA(r)]);
  const fullPdf = filtered.map(r => [r.createdDate || '—', r.createdTime || '—', r.referenceNumber, r.membershipId, r.membershipName || '—', r.agentCode || '—', r.agentName || '—', r.agentCategory || '—', typeLabelA(r.type), fmt(r.amount), `${r.commissionPct ?? 0}%`, fmt(commissionA(r)), fmt(r.netAmount ?? 0), statusLabelA(r.status), r.createdBy || '—', approverA(r), `${r.createdDate || ''} ${r.createdTime || ''}`.trim(), completedAtA(r)]);
  const commCsv = commissionRows.map(a => [a.agentId, a.agentName, a.category, a.depositAmount, a.depositCommission, a.withdrawalAmount, a.withdrawalCommission, a.settlementAmount, a.settlementCommission, a.totalCommission, a.count]);
  const commPdf = commissionRows.map(a => [a.agentId, a.agentName, a.category || '—', fmt(a.depositAmount), fmt(a.depositCommission), fmt(a.withdrawalAmount), fmt(a.withdrawalCommission), fmt(a.settlementAmount), fmt(a.settlementCommission), fmt(a.totalCommission), a.count]);
  const ledgerCsv: Array<Array<string | number>> = [
    ['', '', 'Opening Balance', '', '', '', '', '', '', '', '', opening, ''],
    ...ledger.map(l => [l.date, l.time, l.description, l.ref, l.agentId, l.agentName, l.membershipId, l.memberName, typeLabelA(l.type), l.signedAmount, l.commission > 0 ? -l.commission : 0, l.balance, statusLabelA(l.status)]),
  ];
  const ledgerPdf: Array<Array<string | number>> = [
    ['—', '—', 'Opening Balance', '—', '—', '—', '—', '—', '—', '—', '—', fmt(opening), '—'],
    ...ledger.map(l => [l.date, l.time, l.description, l.ref, l.agentId, l.agentName, l.membershipId, l.memberName, typeLabelA(l.type), signedA(l.signedAmount), commissionTextA(l.commission), fmt(l.balance), statusLabelA(l.status)]),
  ];

  const onPdf = (autoPrint: boolean) => {
    const cfg = tab === 'full'
      ? { title: 'Agent Full Report', headers: AGENT_FULL_HEADERS, rows: fullPdf, aligns: AGENT_FULL_ALIGNS,
          footerNote: `Completed-only totals — Deposits ${fmt(tot.deposits)}, Withdrawals ${fmt(tot.withdrawals)}, Settlements ${fmt(tot.settlements)}, Commission ${fmt(tot.commission)}. Honours the selected filters.` }
      : tab === 'commission'
        ? { title: 'Agent Commission Report', headers: AGENT_COMM_HEADERS, rows: commPdf, aligns: AGENT_COMM_ALIGNS,
            footerNote: `Overall commission earned ${fmt(tot.commission)} across ${commissionRows.length} agent(s). Completed transactions only. Honours the selected filters.` }
        : { title: 'Agent Ledger Report', headers: AGENT_LEDGER_HEADERS, rows: ledgerPdf, aligns: AGENT_LEDGER_ALIGNS,
            footerNote: `Running Balance = Opening + Σ(Deposit − Commission) − Σ(Withdrawal/Settlement + Commission), the same per-leg calculation as the Agent balance. Opening ${fmt(opening)}, Closing ${fmt(closing)}. Honours the selected filters.` };
    printColumnarReport({ ...cfg, businessName: 'Agent Module', generatedBy, rangeLabel, autoPrint });
  };

  const onExcel = () => {
    if (tab === 'full') {
      downloadXlsx(`clari5pay-agent-full-report-${stamp}.xlsx`, [{
        name: 'Agent Full Report',
        columns: [
          { header: 'Date', get: (r: AgentTxnRow) => r.createdDate || '' },
          { header: 'Time', get: r => r.createdTime || '' },
          { header: 'Transaction Reference', get: r => r.referenceNumber, width: 22 },
          { header: 'Membership ID', get: r => r.membershipId },
          { header: 'Member Name', get: r => r.membershipName || '', width: 22 },
          { header: 'Agent ID', get: r => r.agentCode || '' },
          { header: 'Agent Name', get: r => r.agentName || '', width: 22 },
          { header: 'Agent Category', get: r => r.agentCategory || '' },
          { header: 'Transaction Type', get: r => typeLabelA(r.type) },
          { header: 'Gross Amount', get: r => Number(r.amount), width: 16, z: INR_NUMFMT },
          { header: 'Commission %', get: r => Number(r.commissionPct ?? 0) },
          { header: 'Commission Amount', get: r => Number(commissionA(r)), width: 16, z: INR_NUMFMT },
          { header: 'Net Amount', get: r => Number(r.netAmount ?? 0), width: 16, z: INR_NUMFMT },
          { header: 'Status', get: r => statusLabelA(r.status) },
          { header: 'Created By', get: r => r.createdBy || '' },
          { header: 'Approved By', get: r => (approverA(r) === '—' ? '' : approverA(r)) },
          { header: 'Created Date & Time', get: r => `${r.createdDate || ''} ${r.createdTime || ''}`.trim(), width: 20 },
          { header: 'Completed Date & Time', get: r => (completedAtA(r) === '—' ? '' : completedAtA(r)), width: 20 },
        ],
        rows: filtered,
      }]);
      showToast(`Agent Full Report — ${filtered.length} rows`);
    } else if (tab === 'commission') {
      downloadXlsx(`clari5pay-agent-commission-${stamp}.xlsx`, [{
        name: 'Agent Commission',
        columns: [
          { header: 'Agent ID', get: (a: typeof commissionRows[number]) => a.agentId },
          { header: 'Agent Name', get: a => a.agentName, width: 22 },
          { header: 'Agent Category', get: a => a.category },
          { header: 'Total Deposit Amount', get: a => Number(a.depositAmount), width: 18, z: INR_NUMFMT },
          { header: 'Deposit Commission', get: a => Number(a.depositCommission), width: 18, z: INR_NUMFMT },
          { header: 'Total Withdrawal Amount', get: a => Number(a.withdrawalAmount), width: 20, z: INR_NUMFMT },
          { header: 'Withdrawal Commission', get: a => Number(a.withdrawalCommission), width: 20, z: INR_NUMFMT },
          { header: 'Total Settlement Amount', get: a => Number(a.settlementAmount), width: 20, z: INR_NUMFMT },
          { header: 'Settlement Commission', get: a => Number(a.settlementCommission), width: 20, z: INR_NUMFMT },
          { header: 'Total Commission Earned', get: a => Number(a.totalCommission), width: 20, z: INR_NUMFMT },
          { header: 'Total Completed Transactions', get: a => a.count },
        ],
        rows: commissionRows,
      }]);
      showToast(`Agent Commission Report — ${commissionRows.length} agents`);
    } else {
      downloadXlsx(`clari5pay-agent-ledger-${stamp}.xlsx`, [{
        name: 'Agent Ledger',
        columns: [
          { header: 'Date', get: (l: typeof ledger[number]) => l.date },
          { header: 'Time', get: l => l.time },
          { header: 'Description', get: l => l.description, width: 34 },
          { header: 'Transaction Reference', get: l => l.ref, width: 22 },
          { header: 'Agent ID', get: l => l.agentId },
          { header: 'Agent Name', get: l => l.agentName, width: 20 },
          { header: 'Membership ID', get: l => l.membershipId },
          { header: 'Member Name', get: l => l.memberName, width: 20 },
          { header: 'Transaction Type', get: l => typeLabelA(l.type) },
          { header: 'Gross Amount', get: l => Number(l.signedAmount), width: 16, z: INR_NUMFMT },
          { header: 'Commission', get: l => (l.commission > 0 ? -Number(l.commission) : 0), width: 16, z: INR_NUMFMT },
          { header: 'Running Balance', get: l => Number(l.balance), width: 18, z: INR_NUMFMT },
          { header: 'Status', get: l => statusLabelA(l.status) },
        ],
        rows: ledger,
      }]);
      showToast(`Agent Ledger Report — ${ledger.length} rows`);
    }
  };

  const onCsv = () => {
    if (tab === 'full') downloadCsv(`clari5pay-agent-full-report-${stamp}.csv`, AGENT_FULL_HEADERS, fullCsv);
    else if (tab === 'commission') downloadCsv(`clari5pay-agent-commission-${stamp}.csv`, AGENT_COMM_HEADERS, commCsv);
    else downloadCsv(`clari5pay-agent-ledger-${stamp}.csv`, AGENT_LEDGER_HEADERS, ledgerCsv);
  };

  const exportCount = tab === 'full' ? filtered.length : tab === 'commission' ? commissionRows.length : ledger.length;

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Reports &amp; Analytics</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Financial reporting for the isolated Agent Transaction subsystem — transactions, commission and ledger.</p>
      </div>

      {/* Report-type selector — Full Report · Commission Report · Agent Ledger Report */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        {([['full', 'analytics', 'Full Report'], ['commission', 'treasury', 'Commission Report'], ['ledger', 'ledger', 'Agent Ledger Report']] as const).map(([k, ic, label]) => (
          <button key={k} className="c5-btn" onClick={() => { setTab(k); setPage(1); }}
            style={{ ...pill(tab === k), display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <Icon name={ic} size={14} /> {label}
          </button>
        ))}
      </div>

      {/* Summary cards — completed transactions only */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12, marginBottom: 16 }}>
        {tab === 'full' && <>
          <ACard label="Total Transactions" value={tot.count} color={T.blue} />
          <ACard label="Total Deposit Amount" value={fmt(tot.deposits)} color={T.success} />
          <ACard label="Total Withdrawal Amount" value={fmt(tot.withdrawals)} color={T.danger} />
          <ACard label="Total Settlement Amount" value={fmt(tot.settlements)} color="#7c3aed" />
          <ACard label="Total Commission Earned" value={fmt(tot.commission)} color={T.green} />
          <ACard label="Current Available Balance" value={fmt(availableBalance)} color="#1d4ed8" />
        </>}
        {tab === 'commission' && <>
          <ACard label="Total Deposit Commission" value={fmt(tot.depositCommission)} color={T.success} />
          <ACard label="Total Withdrawal Commission" value={fmt(tot.withdrawalCommission)} color={T.danger} />
          <ACard label="Total Settlement Commission" value={fmt(tot.settlementCommission)} color="#7c3aed" />
          <ACard label="Overall Commission Earned" value={fmt(tot.commission)} color={T.green} />
        </>}
        {tab === 'ledger' && <>
          <ACard label="Opening Balance" value={fmt(opening)} color={T.textMuted} />
          <ACard label="Closing Balance (Current Available)" value={fmt(closing)} color={closing >= 0 ? T.success : T.danger} />
          <ACard label="Total Entries" value={ledger.length} color={T.blue} />
          <ACard label="Total Commission" value={fmt(tot.commission)} color={T.green} />
        </>}
      </div>

      {/* Report metadata */}
      <Card style={{ padding: '14px 18px', marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 14 }}>
          <AMeta label="Module" value="Agent Transactions" />
          <AMeta label="Generated By" value={generatedBy} />
          <AMeta label="Generated Date & Time" value={genAt.toLocaleString('en-IN')} />
          <AMeta label="Selected Date Range" value={rangeLabel} />
        </div>
      </Card>

      {/* Advanced filters — one applied set, the fields relevant to the active report */}
      <RSectionTitle note="Set your filters, then click Apply Filters to update the table, footer totals and exports together."><Icon name="filter" size={15} /> Advanced Filters</RSectionTitle>
      <Card style={{ padding: 16, marginBottom: 18 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
          {DATE_PRESETS.map(([k, label]) => (
            <button key={k} className="c5-btn" onClick={() => set('datePreset', k)} style={pill(draft.datePreset === k)}>{label}</button>
          ))}
        </div>
        {draft.datePreset === 'custom' && (
          <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
            <Input label="From Date" type="date" value={draft.from} onChange={e => set('from', e.target.value)} />
            <Input label="To Date" type="date" value={draft.to} onChange={e => set('to', e.target.value)} />
          </div>
        )}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          {tab === 'full' && <>
            <Input label="Reference Number" value={draft.ref} onChange={e => set('ref', e.target.value)} />
            <Input label="Membership ID" value={draft.membershipId} onChange={e => set('membershipId', normalizeMemberId(e.target.value))} />
            <Input label="Member Name" value={draft.memberName} onChange={e => set('memberName', e.target.value)} />
            <Input label="Agent ID" value={draft.agentId} onChange={e => set('agentId', e.target.value)} />
            <Input label="Agent Name" value={draft.agentName} onChange={e => set('agentName', e.target.value)} />
            <Sel label="Agent Category" value={draft.agentCategory} onChange={e => set('agentCategory', e.target.value)}
              options={[{ value: '', label: 'All Categories' }, ...AGENT_CATEGORIES]} />
            <Sel label="Transaction Type" value={draft.type} onChange={e => set('type', e.target.value)}
              options={[{ value: '', label: 'All Types' }, ...AGENT_TYPE_OPTIONS]} />
            <Sel label="Status" value={draft.status} onChange={e => set('status', e.target.value)}
              options={[{ value: '', label: 'All Statuses' }, ...statusOptions]} />
            <Input label="Approved By" value={draft.approvedBy} onChange={e => set('approvedBy', e.target.value)} placeholder="Supervisor / Manager" />
            <Input label="Minimum Amount" type="text" inputMode="decimal" value={draft.minA} onChange={e => set('minA', formatIndianAmountInput(e.target.value))} />
            <Input label="Maximum Amount" type="text" inputMode="decimal" value={draft.maxA} onChange={e => set('maxA', formatIndianAmountInput(e.target.value))} />
            <Input label="Exact Amount" type="text" inputMode="decimal" value={draft.exactA} onChange={e => set('exactA', formatIndianAmountInput(e.target.value))} />
          </>}
          {tab === 'commission' && <>
            <Sel label="Agent" value={draft.agentId} onChange={e => set('agentId', e.target.value)}
              options={[{ value: '', label: 'All Agents' }, ...agentOptions]} />
            <Sel label="Agent Category" value={draft.agentCategory} onChange={e => set('agentCategory', e.target.value)}
              options={[{ value: '', label: 'All Categories' }, ...AGENT_CATEGORIES]} />
          </>}
          {tab === 'ledger' && <>
            <Sel label="Agent" value={draft.agentId} onChange={e => set('agentId', e.target.value)}
              options={[{ value: '', label: 'All Agents' }, ...agentOptions]} />
            <Input label="Membership ID" value={draft.membershipId} onChange={e => set('membershipId', normalizeMemberId(e.target.value))} />
            <Input label="Member Name" value={draft.memberName} onChange={e => set('memberName', e.target.value)} />
            <Sel label="Transaction Type" value={draft.type} onChange={e => set('type', e.target.value)}
              options={[{ value: '', label: 'All Types' }, ...AGENT_TYPE_OPTIONS]} />
            <Sel label="Status" value={draft.status} onChange={e => set('status', e.target.value)}
              options={[{ value: '', label: 'All Statuses' }, ...statusOptions]} />
          </>}
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 12, flexWrap: 'wrap' }}>
          <Btn size="sm" onClick={applyFilters} disabled={applying}>{applying ? 'Applying…' : 'Apply Filters'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters} disabled={applying}>Clear Filters</Btn>
          <span style={{ fontSize: 12, color: T.textMuted }}>
            {applying ? 'Applying filters…' : `${filtered.length} of ${rows.length} transactions`}
          </span>
        </div>
      </Card>

      {/* ── 1. Full Report ── */}
      {tab === 'full' && <>
        <RSectionTitle note="Every Agent transaction — Deposit, Withdrawal and Settlement — in one place. Summary cards and footer totals count completed transactions only."><Icon name="analytics" size={15} /> Full Report</RSectionTitle>
        <ReportExportBar count={exportCount} onPdf={() => onPdf(true)} onExcel={onExcel} onCsv={onCsv} onPrint={() => onPdf(true)} />
        <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 12 }}>
          <div style={A_SCROLL}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead><tr>{AGENT_FULL_HEADERS.map((h, i) => <th key={h} style={{ ...thStickyA, textAlign: AGENT_FULL_ALIGNS[i] === 'r' ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
              <tbody>
                {loading && rows.length === 0 && <tr><td colSpan={AGENT_FULL_HEADERS.length} style={{ ...tdR, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
                {!loading && filtered.length === 0 && <tr><td colSpan={AGENT_FULL_HEADERS.length} style={{ ...tdR, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions match the selected filters.</td></tr>}
                {slice(filtered).map(r => (
                  <tr key={r.id} className="c5-row-hover">
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{r.createdDate || '—'}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{r.createdTime || '—'}</td>
                    <td style={{ ...tdR, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{r.referenceNumber}</td>
                    <td style={tdR}>{r.membershipId}</td>
                    <td style={{ ...tdR, fontWeight: 600 }}>{r.membershipName || '—'}</td>
                    <td style={tdR}>{r.agentCode || '—'}</td>
                    <td style={tdR}>{r.agentName || '—'}</td>
                    <td style={tdR}>{r.agentCategory || '—'}</td>
                    <td style={tdR}>{typeLabelA(r.type)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700 }}>{fmt(r.amount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', color: T.textMuted }}>{r.commissionPct != null ? `${r.commissionPct}%` : '—'}</td>
                    <td style={{ ...tdR, textAlign: 'right', color: T.green, fontWeight: 700 }}>{fmt(commissionA(r))}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700 }}>{r.netAmount != null ? fmt(r.netAmount) : '—'}</td>
                    <td style={tdR}><StatusPill status={r.status} type={r.type} method={r.txnMethod} /></td>
                    <td style={{ ...tdR, color: T.textMuted }}>{r.createdBy || '—'}</td>
                    <td style={{ ...tdR, color: T.textMuted }}>{approverA(r)}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap', color: T.textMuted }}>{`${r.createdDate || ''} ${r.createdTime || ''}`.trim() || '—'}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap', color: T.textMuted }}>{completedAtA(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <APager page={safePage} totalPages={totalPages} count={filtered.length} onPage={setPage} />
        </Card>
        <Card style={{ padding: '16px 18px', marginBottom: 18, borderTop: `3px solid ${T.blue}` }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 14 }}>
            <AMeta label="Total Deposits" value={<span style={{ color: T.success }}>{fmt(tot.deposits)}</span>} />
            <AMeta label="Total Withdrawals" value={<span style={{ color: T.danger }}>{fmt(tot.withdrawals)}</span>} />
            <AMeta label="Total Settlements" value={<span style={{ color: '#7c3aed' }}>{fmt(tot.settlements)}</span>} />
            <AMeta label="Total Commission" value={<span style={{ color: T.green }}>{fmt(tot.commission)}</span>} />
            <AMeta label="Net Amount" value={<span style={{ color: '#1d4ed8' }}>{fmt(tot.net)}</span>} />
            <AMeta label="Total Transactions" value={tot.count} />
          </div>
        </Card>
      </>}

      {/* ── 2. Commission Report ── */}
      {tab === 'commission' && <>
        <RSectionTitle note="Commission earned per Agent, from each transaction's own Pay-In / Pay-Out / Settlement fee. Completed transactions only."><Icon name="treasury" size={15} /> Commission Report</RSectionTitle>
        <ReportExportBar count={exportCount} onPdf={() => onPdf(true)} onExcel={onExcel} onCsv={onCsv} onPrint={() => onPdf(true)} />
        <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 12 }}>
          <div style={A_SCROLL}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead><tr>{AGENT_COMM_HEADERS.map((h, i) => <th key={h} style={{ ...thStickyA, textAlign: AGENT_COMM_ALIGNS[i] === 'r' ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
              <tbody>
                {commissionRows.length === 0 && <tr><td colSpan={AGENT_COMM_HEADERS.length} style={{ ...tdR, textAlign: 'center', color: T.textMuted, padding: 22 }}>No completed agent transactions match the selected filters.</td></tr>}
                {slice(commissionRows).map(a => (
                  <tr key={a.agentId} className="c5-row-hover">
                    <td style={{ ...tdR, fontWeight: 700, color: T.blue }}>{a.agentId}</td>
                    <td style={{ ...tdR, fontWeight: 600 }}>{a.agentName}</td>
                    <td style={tdR}>{a.category || '—'}</td>
                    <td style={{ ...tdR, textAlign: 'right' }}>{fmt(a.depositAmount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', color: T.green, fontWeight: 700 }}>{fmt(a.depositCommission)}</td>
                    <td style={{ ...tdR, textAlign: 'right' }}>{fmt(a.withdrawalAmount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', color: T.green, fontWeight: 700 }}>{fmt(a.withdrawalCommission)}</td>
                    <td style={{ ...tdR, textAlign: 'right' }}>{fmt(a.settlementAmount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', color: T.green, fontWeight: 700 }}>{fmt(a.settlementCommission)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 800, color: T.green }}>{fmt(a.totalCommission)}</td>
                    <td style={{ ...tdR, textAlign: 'right' }}>{a.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <APager page={safePage} totalPages={totalPages} count={commissionRows.length} onPage={setPage} />
        </Card>
        <Card style={{ padding: '16px 18px', marginBottom: 18, borderTop: `3px solid ${T.blue}` }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))', gap: 14 }}>
            <AMeta label="Overall Deposit Commission" value={<span style={{ color: T.success }}>{fmt(tot.depositCommission)}</span>} />
            <AMeta label="Overall Withdrawal Commission" value={<span style={{ color: T.danger }}>{fmt(tot.withdrawalCommission)}</span>} />
            <AMeta label="Overall Settlement Commission" value={<span style={{ color: '#7c3aed' }}>{fmt(tot.settlementCommission)}</span>} />
            <AMeta label="Overall Commission Earned" value={<span style={{ color: T.green }}>{fmt(tot.commission)}</span>} />
          </div>
        </Card>
      </>}

      {/* ── 3. Agent Ledger Report ── */}
      {tab === 'ledger' && <>
        <RSectionTitle note="Completed transactions in chronological order. Gross Amount is the transaction; Commission is the fee applied by the Agent workflow; Running Balance is net of commission and reconciles to the Current Available Balance."><Icon name="ledger" size={15} /> Agent Ledger Report</RSectionTitle>
        <ReportExportBar count={exportCount} onPdf={() => onPdf(true)} onExcel={onExcel} onCsv={onCsv} onPrint={() => onPdf(true)} />
        <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 12 }}>
          <div style={A_SCROLL}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead><tr>{AGENT_LEDGER_HEADERS.map((h, i) => <th key={h} style={{ ...thStickyA, textAlign: AGENT_LEDGER_ALIGNS[i] === 'r' ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
              <tbody>
                {/* Opening Balance always leads the report */}
                <tr style={{ background: T.canvas }}>
                  <td style={tdR} /><td style={tdR} />
                  <td style={{ ...tdR, fontWeight: 700, color: T.textMuted }}>Opening Balance</td>
                  <td style={tdR} /><td style={tdR} /><td style={tdR} /><td style={tdR} /><td style={tdR} /><td style={tdR} />
                  <td style={{ ...tdR, textAlign: 'right' }}>—</td>
                  <td style={{ ...tdR, textAlign: 'right' }}>—</td>
                  <td style={{ ...tdR, textAlign: 'right', fontWeight: 800 }}>{fmt(opening)}</td>
                  <td style={tdR} />
                </tr>
                {ledger.length === 0 && <tr><td colSpan={AGENT_LEDGER_HEADERS.length} style={{ ...tdR, textAlign: 'center', color: T.textMuted, padding: 22 }}>No completed transactions match the selected filters.</td></tr>}
                {slice(ledger).map((l, i) => (
                  <tr key={`${l.ref}-${i}`} className="c5-row-hover">
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{l.date}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{l.time}</td>
                    <td style={tdR}>{l.description}</td>
                    <td style={{ ...tdR, fontFamily: 'monospace', color: T.blue }}>{l.ref}</td>
                    <td style={tdR}>{l.agentId}</td>
                    <td style={tdR}>{l.agentName}</td>
                    <td style={tdR}>{l.membershipId}</td>
                    <td style={{ ...tdR, fontWeight: 600 }}>{l.memberName}</td>
                    <td style={tdR}>{typeLabelA(l.type)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700, color: l.signedAmount >= 0 ? T.success : T.danger }}>{signedA(l.signedAmount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700, color: l.commission > 0 ? T.danger : T.textMuted }}>{commissionTextA(l.commission)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 800 }}>{fmt(l.balance)}</td>
                    <td style={tdR}><StatusPill status={l.status} type={l.type} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <APager page={safePage} totalPages={totalPages} count={ledger.length} onPage={setPage} />
        </Card>
        <Card style={{ padding: '16px 18px', marginBottom: 18, borderTop: `3px solid ${T.blue}` }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 14 }}>
            <AMeta label="Opening Balance" value={fmt(opening)} />
            <AMeta label="Total Entries" value={ledger.length} />
            <AMeta label="Total Deposits" value={<span style={{ color: T.success }}>{fmt(tot.deposits)}</span>} />
            <AMeta label="Total Withdrawals" value={<span style={{ color: T.danger }}>{fmt(tot.withdrawals)}</span>} />
            <AMeta label="Total Settlements" value={<span style={{ color: '#7c3aed' }}>{fmt(tot.settlements)}</span>} />
            <AMeta label="Total Commission" value={<span style={{ color: T.green }}>{fmt(tot.commission)}</span>} />
            <AMeta label="Closing Balance (Current Available)" value={<span style={{ color: closing >= 0 ? T.success : T.danger }}>{fmt(closing)}</span>} />
          </div>
        </Card>
      </>}

      <div style={{ height: 24 }} />
    </div>
  );
};

// A stored slip/proof: preview when it is an image, always downloadable. The file is read from
// the transaction — never re-uploaded — so every viewer sees the same original.
const SlipView: React.FC<{ label: string; src: string; filename: string }> = ({ label, src, filename }) => {
  const isPdf = src.startsWith('data:application/pdf');
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>{label}</div>
      {isPdf
        ? <div style={{ fontSize: 12.5, color: T.textMuted, marginBottom: 6 }}>PDF document</div>
        : <img src={src} alt={label} style={{ maxWidth: 220, maxHeight: 240, objectFit: 'contain', borderRadius: 10, border: `1px solid ${T.border}`, display: 'block', marginBottom: 6 }} />}
      <Btn size="sm" variant="ghost" onClick={() => downloadDataUrl(src, filename)}>↓ Download</Btn>
    </div>
  );
};

// ─── Deposit chain — operator steps (mirror the merchant deposit workflow) ─────
// The Data Operator performs every step the Admin performs in the merchant flow. Each modal maps
// 1:1 onto a backend endpoint and only ever touches the isolated agent ledger.

/** Submit Account — tell the payer which AGENT account to send to. Agent accounts only. */
const SubmitAccountModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const method = row.txnMethod || '';
  const isCash = isTokenMethod(method);
  const isCrypto = isWalletMethod(method);
  // Bank/UPI pick an Agent Account; Cash enters token+note+image; Crypto enters wallet+slip.
  const [accounts, setAccounts] = useState<AgentAccountOption[] | null>(isCash || isCrypto ? [] : null);
  const [sel, setSel] = useState('');
  const [token, setToken] = useState('');
  const [note, setNote] = useState('');
  const [wallet, setWallet] = useState('');
  const [proof, setProof] = useState('');
  const [proofName, setProofName] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (isCash || isCrypto) return;
    agentTxnsAPI.agentAccounts(row.agentMasterId)
      .then((rows) => {
        setAccounts(rows);
        const preferred = rows.find(a => a.isDefault) || rows[0];
        setSel(preferred ? String(preferred.id) : '');
      })
      .catch(() => { setAccounts([]); showToast('Failed to load agent accounts.', 'error'); });
  }, [row.agentMasterId, isCash, isCrypto, showToast]);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    if (f.size > 8 * 1024 * 1024) { showToast('File too large. Maximum 8 MB.', 'error'); return; }
    try { setProof(await fileToDataUrl(f)); setProofName(f.name); }
    catch { showToast('Could not read the file.', 'error'); }
  };

  const chosen = accounts?.find(a => String(a.id) === sel);

  const submit = async () => {
    let body: { agentAccountId?: number; tokenDetails?: string; noteNumber?: string; walletAddress?: string; accountProof?: string };
    if (isCash) {
      if (!token.trim()) { showToast('Enter the Token Details.', 'error'); return; }
      if (!note.trim()) { showToast('Enter the Unique Note Number.', 'error'); return; }
      // Cash has no token image — the token and note ARE the reference, entered exactly as the
      // customer gave them; the slip uploaded at the next step is the proof.
      body = { tokenDetails: token.trim(), noteNumber: note.trim() };
    } else if (isCrypto) {
      if (!wallet.trim()) { showToast('Enter the Crypto Wallet Address.', 'error'); return; }
      if (!isValidWallet(wallet)) { showToast('Enter a valid crypto wallet address.', 'error'); return; }
      if (!proof) { showToast('Upload the Crypto payment slip.', 'error'); return; }
      body = { walletAddress: wallet.trim(), accountProof: proof };
    } else {
      if (!sel) { showToast('Select an agent account to send.', 'error'); return; }
      body = { agentAccountId: Number(sel) };
    }
    setBusy(true);
    try {
      await agentTxnsAPI.accountSubmit(row.id, body);
      showToast(`Submitted for ${row.referenceNumber}.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Submission failed.'), 'error'); }
    finally { setBusy(false); }
  };

  const title = isCash ? 'Submit Token' : isCrypto ? 'Submit Wallet Details' : 'Submit Account';
  // Cash no longer uploads a token image, so the token + note alone enable Submit. Crypto still
  // requires its payment slip; Bank/UPI still require an account to be chosen.
  const canSubmit = isCash ? Boolean(token.trim() && note.trim())
    : isCrypto ? Boolean(isValidWallet(wallet) && proof)
    : Boolean(sel);

  return (
    <Modal title={`${title} — ${row.referenceNumber}`} onClose={onClose}>
      {isCash ? (
        <>
          <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
            Enter the Token Details and Unique Note Number exactly as the customer provided them.
            The token image is uploaded at the next step (Upload Token).
          </p>
          <Input label="Token Details" value={token} onChange={e => setToken(e.target.value)} required placeholder="As provided by the customer" />
          <Input label="Unique Note Number" value={note} onChange={e => setNote(normalizeNoteNumber(e.target.value))} required placeholder="As provided by the customer" hint="Uppercase letters and numbers only; must be unique" />
        </>
      ) : isCrypto ? (
        <>
          <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
            Enter the wallet the funds were sent to and upload the crypto payment slip. The Supervisor verifies both.
          </p>
          <Input label="Crypto Wallet Address" value={wallet} onChange={e => setWallet(e.target.value)} required placeholder="Destination wallet address"
            hint={wallet.trim() && !isValidWallet(wallet) ? 'Not a valid wallet address format' : 'Enter carefully — crypto transfers are irreversible'} />
          <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Crypto Payment Slip</label>
          <input type="file" accept="image/*,application/pdf" onChange={onFile} style={{ marginBottom: 6, fontSize: 12 }} />
          {proofName && <div style={{ fontSize: 11.5, color: T.textMuted, marginBottom: 12 }}>Attached: {proofName}</div>}
        </>
      ) : (
        <>
          <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
            Choose the agent account the payer should send to. Only this agent's own accounts are listed —
            merchant accounts are never used.
          </p>
          {accounts === null ? <div style={{ padding: 16, color: T.textMuted, fontSize: 13 }}>Loading agent accounts…</div>
            : accounts.length === 0 ? (
              <div style={{ padding: 14, borderRadius: 10, background: T.warningBg, color: T.warning, fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>
                This agent has no active accounts. Add one under Agent Accounts first.
              </div>
            ) : (
              <>
                <Sel label="Agent Account" value={sel} onChange={e => setSel(e.target.value)} required
                  options={accounts.map(a => ({ value: String(a.id), label: `${a.accountRef} · ${a.accountType}${a.label ? ` · ${a.label}` : ''}${a.isDefault ? ' (default)' : ''}` }))} />
                {chosen && (
                  <div style={{ padding: 12, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}`, marginBottom: 14 }}>
                    <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Details sent to the payer</div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{chosen.detail || '—'}</div>
                    {chosen.qrImage && <img src={chosen.qrImage} alt="QR" style={{ marginTop: 10, width: 140, height: 140, objectFit: 'contain', borderRadius: 8, border: `1px solid ${T.border}` }} />}
                  </div>
                )}
              </>
            )}
        </>
      )}
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
        <Btn onClick={submit} disabled={busy || !canSubmit}>{busy ? 'Submitting…' : 'Submit'}</Btn>
      </div>
    </Modal>
  );
};

/** Pay / Upload Slip — evidences the payment and sends the deposit to Supervisor review. */
const UploadSlipModal: React.FC<{ row: AgentTxnRow; mode?: 'deposit' | 'payout'; onClose: () => void; onDone: () => void }> = ({ row, mode = 'deposit', onClose, onDone }) => {
  const { showToast } = useToast();
  // A CASH/CRYPTO withdrawal is confirm-only after Manager approval — no slip, no UTR.
  const confirmOnly = mode === 'payout' && row.type === 'WITHDRAWAL' && isSpecialMethod(row.txnMethod);
  // A CASH deposit has no UTR: the money changes hands in person, so no rail issues a reference and
  // the slip is the only proof. Every other deposit, and every payout, still captures one.
  const isCashDeposit = mode === 'deposit' && isTokenMethod(row.txnMethod);
  // A CASH withdrawal hands physical money over, so the operator must attach proof of it — there is
  // no rail, no slip and no UTR to evidence it otherwise. Crypto confirms on the wallet alone.
  const isCashConfirm = confirmOnly && isTokenMethod(row.txnMethod);
  const [utr, setUtr] = useState('');
  const [slip, setSlip] = useState('');
  const [slipName, setSlipName] = useState('');
  const [busy, setBusy] = useState(false);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    if (f.size > 8 * 1024 * 1024) { showToast('File too large. Maximum 8 MB.', 'error'); return; }
    try { setSlip(await fileToDataUrl(f)); setSlipName(f.name); }
    catch { showToast('Could not read the file.', 'error'); }
  };

  const submit = async () => {
    if (isCashConfirm && !slip) { showToast('Upload the Cash Payment Proof.', 'error'); return; }
    // A confirm-only crypto withdrawal needs neither slip nor UTR.
    if (!confirmOnly) {
      if (!slip) { showToast('Upload the payment slip image.', 'error'); return; }
      if (!isCashDeposit && !utr.trim()) { showToast('Enter the UTR Number.', 'error'); return; }
    }
    setBusy(true);
    try {
      const body = confirmOnly
        ? (isCashConfirm ? { slipImage: slip } : {})
        : { slipImage: slip, ...(isCashDeposit ? {} : { utr: utr.trim() }) };
      // Deposit: slip → Supervisor review. Withdrawal: payout after Manager approval → Completed.
      await (mode === 'payout' ? agentTxnsAPI.payout(row.id, body) : agentTxnsAPI.submitSlip(row.id, body));
      showToast(confirmOnly
        ? `${row.referenceNumber} confirmed and completed.`
        : mode === 'payout'
          ? (row.type === 'WITHDRAWAL'
              ? `${row.referenceNumber} paid — awaiting Manager approval.`
              : `${row.referenceNumber} paid and completed.`)
          : `Slip submitted for ${row.referenceNumber} — awaiting Supervisor approval.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Failed to submit the slip.'), 'error'); }
    finally { setBusy(false); }
  };

  if (confirmOnly) {
    const detail: Array<[string, React.ReactNode]> = isWalletMethod(row.txnMethod)
      ? [['Crypto Wallet Address', row.walletAddress]]
      : [['Token Details', row.tokenDetails], ['Unique Note Number', row.noteNumber]];
    return (
      <Modal title={`Confirm & Complete — ${row.referenceNumber}`} onClose={onClose}>
        <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
          Approved by {row.managerName || 'the Manager'}. Confirm the details below to complete the
          withdrawal.{isCashConfirm
            ? ' Cash is handed over in person, so attach proof of the payment — there is no slip or UTR to evidence it.'
            : ` No payment slip is required for ${methodLabel(row.txnMethod)}.`}
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 14, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          <DField k="Amount" v={fmt(row.amount)} />
          {detail.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
        </div>
        {isCashConfirm && (
          <>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Cash Payment Proof <span style={{ color: T.danger }}>*</span>
            </label>
            <input type="file" accept="image/jpeg,image/jpg,image/png,application/pdf" onChange={onFile} style={{ marginBottom: 6, fontSize: 12 }} />
            {slipName && <div style={{ fontSize: 11.5, color: T.textMuted, marginBottom: 6 }}>Attached: {slipName}</div>}
            <p style={{ fontSize: 11.5, color: T.textMuted, margin: '4px 0 14px' }}>JPG, JPEG, PNG or PDF. Required — it is stored with the transaction and shown wherever it is viewed.</p>
          </>
        )}
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn variant="success" onClick={submit} disabled={busy || (isCashConfirm && !slip)}>{busy ? 'Completing…' : 'Confirm & Complete'}</Btn>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`${isCashDeposit ? 'Upload Token' : 'Pay / Upload Slip'} — ${row.referenceNumber}`} onClose={onClose}>
      {/* Cash has no account to send to — the operator uploads the token image against the Token
          Details and Unique Note Number entered at Submit Token, so summarise those instead. */}
      {isCashDeposit ? (
        <div style={{ padding: 12, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}`, marginBottom: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: '10px 18px' }}>
            <DField k="Amount" v={fmt(row.amount)} />
            <DField k="Token Details" v={row.tokenDetails} />
            <DField k="Unique Note Number" v={row.noteNumber} />
          </div>
        </div>
      ) : (
      <div style={{ padding: 12, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}`, marginBottom: 14 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
          {mode === 'payout'
            ? `Pay the member · ${row.membershipId}`
            : `Send to · ${row.agentAccountRef || '—'} (${row.agentAccountType || '—'})`}
        </div>
        <div style={{ fontSize: 13, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>
          {mode === 'payout'
            ? [row.payoutAccountHolder, row.payoutAccountNumber || row.payoutUpiId, row.payoutIfsc, row.payoutBankName].filter(Boolean).join(' · ') || '—'
            : row.agentAccountDetail || '—'}
        </div>
        <div style={{ marginTop: 8, fontSize: 14, fontWeight: 800, color: T.blue }}>{fmt(row.amount)}</div>
      </div>
      )}
      {/* The UTR is the payment reference for anything paid over a rail — there is no separate
          Reference Number. A cash deposit has none, so only the slip is collected. */}
      {!isCashDeposit && (
        <Input label="UTR Number" value={utr} onChange={e => setUtr(e.target.value)}
          required placeholder="Bank UTR — the payment reference" />
      )}
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{isCashDeposit ? 'Token Image' : 'Slip Image'}</label>
      <input type="file" accept="image/*,application/pdf" onChange={onFile} style={{ marginBottom: 6, fontSize: 12 }} />
      {slipName && <div style={{ fontSize: 11.5, color: T.textMuted, marginBottom: 6 }}>Attached: {slipName}</div>}
      <p style={{ fontSize: 11.5, color: T.textMuted, margin: '4px 0 14px' }}>
        {isCashDeposit ? 'The token image is required.' : 'Both the UTR Number and the slip image are required.'}
      </p>
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
        <Btn onClick={submit} disabled={busy || !slip || (!isCashDeposit && !utr.trim())}>{busy ? 'Submitting…' : (isCashDeposit ? 'Upload Token' : 'Submit Slip')}</Btn>
      </div>
    </Modal>
  );
};

/** Mark Deposit — the merchant workflow's Admin step, performed here by the Data Operator.
 *  A review step, not an upload: the slip and UTR were captured at Pay / Upload Slip and are shown
 *  read-only here, so the original file is never duplicated or overwritten. */
const MarkDepositModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await agentTxnsAPI.markDeposit(row.id, {});
      showToast(`${row.referenceNumber} marked Deposited.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Failed to mark the deposit.'), 'error'); }
    finally { setBusy(false); }
  };

  const facts: Array<[string, React.ReactNode]> = [
    ['Reference', row.referenceNumber],
    ['Amount', fmt(row.amount)],
    // Mark Deposit is a deposit-only step: cash has no UTR, so omit the row entirely.
    ...(isTokenMethod(row.txnMethod)
      ? [] : [['UTR Number', row.depositUtr] as [string, React.ReactNode]]),
    ['Paid By', row.slipSubmittedBy],
    ['Paid At (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
    ['Approved By', row.supervisorName],
  ];

  return (
    <Modal title={`Mark Deposit — ${row.referenceNumber}`} onClose={onClose} wide>
      <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
        Review what was captured at Pay / Upload Slip and confirm. Marking this deposit completes it —
        the status becomes Deposited and the amount counts toward the agent's approved figures.
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 14, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {facts.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>
      {row.slipImage
        ? <div style={{ marginBottom: 14 }}><SlipView label={isTokenMethod(row.txnMethod) ? 'Token Image' : 'Uploaded Slip'} src={row.slipImage} filename={`${row.referenceNumber}-slip`} /></div>
        : <div style={{ marginBottom: 14, fontSize: 12.5, color: T.textMuted }}>No slip image was uploaded for this deposit.</div>}
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
        <Btn variant="success" onClick={submit} disabled={busy}>{busy ? 'Marking…' : 'Mark Deposited'}</Btn>
      </div>
    </Modal>
  );
};

// ─── Agent Approvals (Supervisor) ─────────────────────────────────────────────
// Every Agent Deposit awaiting Supervisor review. Approving moves it to Slip Submitted, where the
// Data Operator marks it Deposited — exactly the merchant deposit workflow's shape.
const ApproveModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [remark, setRemark] = useState('');
  const [busy, setBusy] = useState(false);
  // Deposits are approved by the Supervisor, withdrawals by the Manager — the same split the
  // merchant review gate uses.
  const isDep = row.type === 'DEPOSIT';

  const decide = async (approve: boolean) => {
    if (!remark.trim()) { showToast('Remarks are required for every review action.', 'error'); return; }
    setBusy(true);
    try {
      if (isDep) {
        await (approve ? agentTxnsAPI.supervisorApprove(row.id, remark.trim())
                       : agentTxnsAPI.supervisorReject(row.id, remark.trim()));
      } else {
        await (approve ? agentTxnsAPI.managerApprove(row.id, remark.trim())
                       : agentTxnsAPI.managerReject(row.id, remark.trim()));
      }
      showToast(approve ? `${row.referenceNumber} approved.` : `${row.referenceNumber} rejected.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Action failed.'), 'error'); }
    finally { setBusy(false); }
  };

  const facts: Array<[string, React.ReactNode]> = [
    ['Reference', row.referenceNumber],
    ['Agent', `${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`],
    ['Membership', `${row.membershipId}${row.membershipName ? ` · ${row.membershipName}` : ''}`],
    ['Amount', fmt(row.amount)],
    ['Transaction Type', methodLabel(row.txnMethod)],
    // What this method asks the reviewer to verify.
    ['Token Details', row.tokenDetails],
    ['Unique Note Number', row.noteNumber],
    ['Crypto Wallet Address', row.walletAddress],
    ...(isDep
      ? ([
          ['Sent To', `${row.agentAccountRef || '—'} · ${row.agentAccountDetail || '—'}`],
                ['Slip By', row.slipSubmittedBy],
          ['Slip At (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
        ] as Array<[string, React.ReactNode]>)
      : ([
          ['Pay To', row.payoutAccountHolder],
          ['Account Number', row.payoutAccountNumber],
          ['IFSC', row.payoutIfsc],
          ['Bank', row.payoutBankName],
          ['UPI ID', row.payoutUpiId],
          ['UTR Number', row.depositUtr],
                ['Paid By', row.slipSubmittedBy],
          ['Paid At (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
          ['Requested By', row.createdBy],
        ] as Array<[string, React.ReactNode]>)),
  ];

  return (
    <Modal title={`Review ${row.referenceNumber}`} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 14, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {facts.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>
      {row.accountProof && (
        <div style={{ marginBottom: 14 }}>
          <SlipView label={isWalletMethod(row.txnMethod) ? 'Crypto Payment Slip' : 'Token Details Image'} src={row.accountProof} filename={`${row.referenceNumber}-proof`} />
        </div>
      )}
      {row.slipImage && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>{isTokenMethod(row.txnMethod) ? 'Token Image' : 'Submitted Slip'}</div>
          <SlipView label={isTokenMethod(row.txnMethod) ? 'Token Image' : 'Submitted Slip'} src={row.slipImage} filename={`${row.referenceNumber}-slip`} />
        </div>
      )}
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Remarks <span style={{ color: T.danger }}>*</span>
      </label>
      <textarea value={remark} onChange={e => setRemark(e.target.value)} rows={2} placeholder="Required for both approve and reject"
        style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical', marginBottom: 14 }} />
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Close</Btn>
        <Btn variant="danger" onClick={() => decide(false)} disabled={busy}>Reject</Btn>
        <Btn variant="success" onClick={() => decide(true)} disabled={busy}>Approve</Btn>
      </div>
    </Modal>
  );
};

export const AgentApprovalsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => {
  const { showToast } = useToast();
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [reviewRow, setReviewRow] = useState<AgentTxnRow | null>(null);
  // A Supervisor reviews Deposits, a Manager reviews Withdrawals — the merchant review split.
  const isManager = String(user.merchantRole || '').toUpperCase() === 'MANAGER';
  // A withdrawal waits at its gate under a method-dependent name: cash/crypto are authorised before
  // the operator confirms them (TOKEN_SUBMITTED / WALLET_SUBMITTED), bank/UPI are paid first and
  // arrive at MANAGER_REVIEW. The list endpoint takes a comma-separated status for exactly this.
  const queue = isManager
    ? { status: 'TOKEN_SUBMITTED,WALLET_SUBMITTED,MANAGER_REVIEW', txn_type: 'WITHDRAWAL', noun: 'Withdrawals' }
    : { status: 'SLIP_SUBMITTED', txn_type: 'DEPOSIT', noun: 'Deposits' };

  const load = useCallback(async () => {
    setLoading(true);
    try { setRows(await agentTxnsAPI.list({ status: queue.status, txn_type: queue.txn_type })); }
    catch { showToast('Failed to load approvals.', 'error'); }
    finally { setLoading(false); }
  }, [showToast, queue.status, queue.txn_type]);

  useEffect(() => { load(); }, [load]);
  usePoll(() => { if (!reviewRow) load(); });

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Approvals</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>
          Agent {queue.noun} awaiting your approval. {isManager ? 'The operator has paid and uploaded the slip; review it and' : 'Approving sends them back to the Data Operator to'}
          {isManager ? ' complete — approving finishes the withdrawal.' : ' mark as Deposited.'}
        </p>
      </div>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>Pending Approvals</h2>
          <span style={{ fontSize: 12, color: T.textMuted, marginLeft: 'auto' }}>{rows.length} awaiting review</span>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Agent', 'Membership', 'Amount', 'Type', 'Slip By', 'Submitted (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={8} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && rows.length === 0 && <tr><td colSpan={8} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Nothing awaiting your approval.</td></tr>}
              {rows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}>{methodLabel(x.txnMethod)}</td>
                  <td style={tdS}>{x.slipSubmittedBy || '—'}</td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.slipSubmittedDate} {x.slipSubmittedTime}</td>
                  <td style={tdS}><Btn size="sm" onClick={() => setReviewRow(x)}>Review</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {reviewRow && <ApproveModal row={reviewRow} onClose={() => setReviewRow(null)} onDone={load} />}
    </div>
  );
};

// ─── Agent All Transactions ────────────────────────────────────────────────────
// The agent counterpart of Merchant → All Transactions: same shape (search + date filters, type
// and status selects, export, ledger table, "Showing X of Y"), plus the Agent ID / Agent Name /
// Lists EVERY agent transaction of every type from the isolated ledger — merchant transactions are
// never mixed in, because this only ever calls /api/agent-txns.
export const AgentAllTransactionsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailRow, setDetailRow] = useState<AgentTxnRow | null>(null);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [method, setMethod] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Search / status / dates go server-side; the transaction-type (method) refinement is
      // applied client-side, mirroring how the merchant page refines its server-filtered set.
      const q: AgentTxnQuery = {};
      if (status) q.status = status;
      if (search.trim()) q.search = search.trim();
      if (fromF) q.date_from = fromF;
      if (toF) q.date_to = toF;
      setRows(await agentTxnsAPI.list(q));
    } catch { showToast('Failed to load agent transactions.', 'error'); }
    finally { setLoading(false); }
  }, [status, search, fromF, toF, showToast]);

  useEffect(() => { load(); }, [load]);
  usePoll(() => { if (!detailRow) load(); });

  const filtered = method ? rows.filter(r => r.txnMethod === method) : rows;
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const clearFilters = () => {
    setSearch(''); setStatus(''); setMethod(''); setFromF(''); setToF(''); setPage(1);
  };

  const exportCsv = () => {
    if (filtered.length === 0) { showToast('Nothing to export for the current filters.', 'error'); return; }
    downloadAgentCsv(`agent-all-transactions-${new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })}`,
      ['Reference', 'Agent ID', 'Agent Name', 'Type', 'Transaction Type', 'Membership ID', 'Member Name',
       'Membership Type', 'Amount', 'Status', 'Sending Account', 'Payout Account', 'Instructions',
       'Created By', 'Created Date (IST)', 'Created Time (IST)'],
      filtered.map(r => [
        r.referenceNumber, r.agentCode || '', r.agentName || '', r.type, methodLabel(r.txnMethod),
        r.membershipId, r.membershipName || '', r.membershipType, r.amount,
        STATUS_STYLE[r.status]?.label || r.status,
        [r.senderAccountHolder, r.senderAccountNumber || r.senderUpiId, r.senderBankName].filter(Boolean).join(' · '),
        [r.payoutAccountHolder, r.payoutAccountNumber || r.payoutUpiId, r.payoutBankName].filter(Boolean).join(' · '),
        r.instructions ? instrLabel(r.instructions) : '',
        r.createdBy || '', r.createdDate || '', r.createdTime || '',
      ]));
    showToast(`Exported ${filtered.length} agent transaction${filtered.length === 1 ? '' : 's'}.`, 'success');
  };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>All Transactions</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Every Agent Deposit, Withdrawal and Settlement in the isolated Agent ledger.</p>
      </div>

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(170px,1fr))', gap: 12 }}>
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent" style={{ marginBottom: 0 }} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, ...STATUS_FILTER_OPTIONS]} />
          <Sel label="Transaction Type" value={method} onChange={e => { setMethod(e.target.value); setPage(1); }} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All' }, ...Object.keys(METHOD_LABEL).map(v => ({ value: v, label: METHOD_LABEL[v] }))]} />
          <Input label="From Date" type="date" value={fromF} onChange={e => setFromF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="To Date" type="date" value={toF} onChange={e => setToF(e.target.value)} style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <Btn size="sm" onClick={() => { setPage(1); load(); }} disabled={loading}>{loading ? 'Searching…' : 'Apply Filters'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters}>Clear</Btn>
          <Btn size="sm" variant="secondary" onClick={exportCsv}>Export CSV</Btn>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                {['Reference', 'Agent ID', 'Agent Name', 'Membership', 'Type', 'Transaction Type',
                  'Amount', 'Status', 'Created (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={10} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && pageRows.length === 0 && <tr><td colSpan={10} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions match the filters.</td></tr>}
              {pageRows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{x.agentCode || '—'}</td>
                  <td style={tdS}>{x.agentName || '—'}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={tdS}>{x.type.charAt(0) + x.type.slice(1).toLowerCase()}</td>
                  <td style={tdS}>{methodLabel(x.txnMethod)}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} type={x.type} method={x.txnMethod} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={tdS}><Btn size="sm" variant="ghost" onClick={() => setDetailRow(x)}>View Details</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '10px 16px', borderTop: `1px solid ${T.border}` }}>
          <p style={{ fontSize: 11, color: T.textMuted, margin: 0 }}>Showing {pageRows.length} of {filtered.length}{method ? ` (filtered from ${rows.length})` : ''}</p>
          {totalPages > 1 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Btn size="sm" variant="ghost" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={safePage <= 1}>‹ Prev</Btn>
              <span style={{ fontSize: 12, color: T.textMuted }}>Page {safePage} of {totalPages}</span>
              <Btn size="sm" variant="ghost" onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={safePage >= totalPages}>Next ›</Btn>
            </div>
          )}
        </div>
      </Card>

      {detailRow && <AgentTxnDetailsModal row={detailRow} onClose={() => setDetailRow(null)} />}
    </div>
  );
};
