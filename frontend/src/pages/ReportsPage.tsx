import React, { useState, useEffect, useCallback } from 'react';
import { T } from '../utils/theme';
import { fmt, today, depositTypeLabel, memberLabel } from '../utils/helpers';
import { downloadXlsx, INR_NUMFMT } from '../utils/xlsx';
import { Card, StatCard, Btn, Input, Sel, Modal, CountUp, Skeleton } from '../components/UI';
import { transactionAPI, userAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { useTheme } from '../context/ThemeContext';
import {
  ResponsiveContainer, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip as RcTooltip,
} from 'recharts';
import type { User, ReportData, ReportRow, ReportMemberRow } from '../types';

const RTYPE_LABEL: Record<string, string> = { deposit: 'Deposit', withdrawal: 'Withdrawal', settlement: 'Settlement' };
const prettyStatusR = (s: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
// Type label, with the deposit method appended for deposits, e.g. "Deposit (Crypto (USDT))".
const rtypeLabel = (r: ReportRow) => {
  const base = RTYPE_LABEL[r.type || ''] || '';
  return r.type === 'deposit' && r.depositType ? `${base} (${depositTypeLabel(r.depositType)})` : base;
};

const exportRowsXlsx = (rows: ReportRow[], filename: string) => {
  downloadXlsx(filename, [{
    name: 'Transactions',
    columns: [
      { header: 'Reference Number', get: r => r.ref },
      { header: 'Membership - Member', get: r => memberLabel(r.memberId, r.member), width: 28 },
      { header: 'Transaction Type', get: r => rtypeLabel(r) },
      { header: 'Amount (INR)', get: r => Number(r.amount), width: 14, z: INR_NUMFMT },
      { header: 'Status', get: r => prettyStatusR(r.status) },
      { header: 'Date & Time', get: r => `${r.date || ''} ${r.time || ''}`.trim(), width: 20 },
      { header: 'Cancellation Reason', get: r => r.cancelReason || '' },
    ],
    rows,
  }]);
};

// Management-style PDF (print-to-PDF, no extra deps) of the report summary.
function exportReportPdf(data: ReportData, businessName: string) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups for this site to export the PDF.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const c = data.cards;
  const card = (l: string, v: string) => `<div class="kpi"><div class="kl">${l}</div><div class="kv">${v}</div></div>`;
  const memRows = (rows: ReportMemberRow[], key: keyof ReportMemberRow, money: boolean) => rows.map((m, i) =>
    `<tr class="${i % 2 ? 'alt' : ''}"><td>${i + 1}</td><td>${memberLabel(m.memberId, m.memberName)}</td><td class="amt">${money ? fmt(Number(m[key] || 0)) : (m[key] ?? 0)}</td></tr>`).join('');
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Clari5Pay Report</title>
  <style>
    @page { size: A4; margin: 14mm; } * { box-sizing: border-box; }
    body { font-family: Arial,'Segoe UI',sans-serif; color:#0a2540; margin:0; }
    .head { display:flex; align-items:center; gap:14px; border-bottom:3px solid #0052cc; padding-bottom:12px; }
    .brand { font-size:24px; font-weight:800; } .brand .b{color:#0052cc}.brand .g{color:#26d00c}.brand .n{color:#0a2540}
    .meta { margin-left:auto; text-align:right; font-size:11px; color:#4a5568; line-height:1.6; }
    h1 { font-size:16px; margin:16px 0 2px; } .sub { font-size:12px; color:#4a5568; margin:0 0 14px; }
    h2 { font-size:13px; margin:18px 0 8px; color:#0052cc; }
    .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
    .kpi { border:1px solid #e2e8f0; border-radius:8px; padding:8px 10px; }
    .kl { font-size:9px; text-transform:uppercase; letter-spacing:.04em; color:#64748b; }
    .kv { font-size:15px; font-weight:800; margin-top:2px; }
    table { width:100%; border-collapse:collapse; font-size:10.5px; }
    th { background:#0a2540; color:#fff; text-align:left; padding:6px 8px; font-size:9px; text-transform:uppercase; }
    td { padding:5px 8px; border-bottom:1px solid #e2e8f0; } tr.alt td { background:#f5f8ff; }
    .amt { text-align:right; font-weight:700; } .mono { font-family:'Courier New',monospace; }
    ul { font-size:11px; color:#334155; line-height:1.7; } footer { margin-top:16px; font-size:9.5px; color:#9ca3af; text-align:center; }
  </style></head><body>
    <div class="head"><span class="brand"><span class="b">clari</span><span class="g">5</span><span class="n">pay</span></span>
      <div class="meta">Merchant Management Report<br>${businessName}<br>Generated: ${now}</div></div>
    <h1>Business Performance Report</h1>
    <p class="sub">Summary of transactions, memberships and value across the business.</p>
    <h2>Key Figures</h2>
    <div class="kpis">
      ${card('Total Transactions', String(c.totalTransactions))}
      ${card('Total Amount', fmt(c.totalTransactionAmount))}
      ${card('Deposits', `${c.totalDeposits} &middot; ${fmt(c.totalDepositAmount)}`)}
      ${card('Withdrawals', `${c.totalWithdrawals} &middot; ${fmt(c.totalWithdrawalAmount)}`)}
      ${card('Settlements', `${c.totalSettlements} &middot; ${fmt(c.totalSettlementAmount)}`)}
      ${card('Active Memberships', String(c.activeMemberships))}
      ${card('Most Active Member', c.mostActiveMember ? `${c.mostActiveMember.memberName} (${c.mostActiveMember.count})` : '-')}
      ${card('Largest Today', c.largestTransactionToday ? fmt(c.largestTransactionToday.amount) : '-')}
    </div>
    <h2>Top Members by Transactions</h2>
    <table><thead><tr><th>Rank</th><th>Membership - Member</th><th style="text-align:right">Transactions</th></tr></thead>
      <tbody>${memRows(data.memberAnalytics.mostActive, 'count', false) || '<tr><td colspan=4>No data</td></tr>'}</tbody></table>
    <h2>Top Members by Overall Volume</h2>
    <table><thead><tr><th>Rank</th><th>Membership - Member</th><th style="text-align:right">Total Volume</th></tr></thead>
      <tbody>${memRows(data.memberAnalytics.highestValue, 'total', true) || '<tr><td colspan=4>No data</td></tr>'}</tbody></table>
    <h2>Business Insights</h2>
    <ul>${data.insights.map(i => `<li>${i}</li>`).join('') || '<li>No insights available yet.</li>'}</ul>
    <footer>Clari5Pay - confidential. Generated from live platform data.</footer>
  </body></html>`);
  w.document.close(); w.focus();
  setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

const thR: React.CSSProperties = { textAlign: 'left', padding: '11px 14px', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: `1px solid ${T.border}` };
const tdR: React.CSSProperties = { padding: '11px 14px', borderBottom: `1px solid ${T.borderLight}`, color: T.textMain };

const pill = (active: boolean): React.CSSProperties => ({
  padding: '8px 14px', borderRadius: 999, border: `1.5px solid ${active ? T.blue : T.border}`,
  background: active ? T.blue : T.surface, color: active ? '#fff' : T.textMuted,
  cursor: 'pointer', fontSize: 12.5, fontWeight: 700, fontFamily: 'inherit',
});

const RSectionTitle: React.FC<{ children: React.ReactNode; note?: string }> = ({ children, note }) => (
  <div style={{ margin: '26px 0 12px' }}>
    <h3 style={{ margin: 0, fontSize: 16, fontWeight: 800, color: T.textMain }}>{children}</h3>
    {note && <p style={{ margin: '3px 0 0', fontSize: 12, color: T.textMuted }}>{note}</p>}
  </div>
);

const MemberTable: React.FC<{
  rows: ReportMemberRow[]; valueKey: keyof ReportMemberRow; valueLabel: string; money?: boolean;
  showRank?: boolean; onPick: (memberId: string) => void;
}> = ({ rows, valueKey, valueLabel, money, showRank, onPick }) => (
  <Card style={{ padding: 0, overflow: 'hidden' }}>
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ background: T.canvas }}>
            {showRank && <th style={thR}>Rank</th>}
            <th style={thR}>Membership - Member</th>
            <th style={{ ...thR, textAlign: 'right' }}>{valueLabel}</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={showRank ? 3 : 2} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>No data yet.</td></tr>}
          {rows.map((m, i) => (
            <tr key={m.memberId} style={{ cursor: 'pointer' }} onClick={() => onPick(m.memberId)}>
              {showRank && <td style={{ ...tdR, fontWeight: 800, color: T.blue }}>{m.rank ?? i + 1}</td>}
              <td style={{ ...tdR, fontWeight: 600 }}>{memberLabel(m.memberId, m.memberName)}</td>
              <td style={{ ...tdR, textAlign: 'right', fontWeight: 800 }}>{money ? fmt(Number(m[valueKey] || 0)) : (m[valueKey] ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </Card>
);

const ReportRowsTable: React.FC<{ rows: ReportRow[]; onPick?: (id: string) => void; empty: string }> = ({ rows, onPick, empty }) => (
  <Card style={{ padding: 0, overflow: 'hidden' }}>
    <div style={{ overflowX: 'auto', maxHeight: 460 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          <tr style={{ background: T.canvas }}>
            {['Reference', 'Membership - Member', 'Type', 'Amount', 'Status', 'Date & Time'].map(h => <th key={h} style={thR}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={6} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>{empty}</td></tr>}
          {rows.slice(0, 500).map(r => (
            <tr key={r.ref} style={{ cursor: onPick ? 'pointer' : 'default' }} onClick={() => onPick && r.memberId && onPick(r.memberId)}>
              <td style={{ ...tdR, fontFamily: 'monospace' }}>{r.ref}</td>
              <td style={{ ...tdR, fontWeight: 600 }}>{memberLabel(r.memberId, r.member)}</td>
              <td style={tdR}>{rtypeLabel(r) || '-'}</td>
              <td style={{ ...tdR, textAlign: 'right', fontWeight: 700 }}>{fmt(r.amount)}</td>
              <td style={tdR}>{prettyStatusR(r.status)}</td>
              <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{r.date} {r.time}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </Card>
);

const WIN_LABELS: Array<[string, string]> = [
  ['10m', 'Last 10 Minutes'], ['20m', 'Last 20 Minutes'], ['30m', 'Last 30 Minutes'], ['1h', 'Last 1 Hour'],
  ['today', 'Today'], ['yesterday', 'Yesterday'], ['7d', 'Last 7 Days'], ['30d', 'Last 30 Days'],
];

type RTab = 'overview' | 'quick' | 'members' | 'intel' | 'trends' | 'search';

// ── Overview tab ──
const OverviewTab: React.FC<{ data: ReportData }> = ({ data }) => {
  const c = data.cards;
  return (
    <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 14 }}>
      <StatCard icon="≡" label="Total Transactions" value={<CountUp value={c.totalTransactions} />} color={T.blue} />
      <StatCard icon="↓" label="Total Deposits" value={<CountUp value={c.totalDeposits} />} sub={fmt(c.totalDepositAmount)} color={T.success} />
      <StatCard icon="↑" label="Total Withdrawals" value={<CountUp value={c.totalWithdrawals} />} sub={fmt(c.totalWithdrawalAmount)} color={T.danger} />
      <StatCard icon="⇄" label="Total Settlements" value={<CountUp value={c.totalSettlements} />} sub={fmt(c.totalSettlementAmount)} color={T.warning} />
      <StatCard icon="₹" label="Total Transaction Amount" value={<CountUp value={c.totalTransactionAmount} format={fmt} />} valueLen={fmt(c.totalTransactionAmount).length} color={T.blue} />
      <StatCard icon="👥" label="Active Memberships" value={<CountUp value={c.activeMemberships} />} sub="transacted in last 30 days" color={T.cyan || T.blue} />
      <StatCard icon="⭐" label="Most Active Member" value={c.mostActiveMember ? memberLabel(c.mostActiveMember.memberId, c.mostActiveMember.memberName) : '-'} sub={c.mostActiveMember ? `${c.mostActiveMember.count} txns` : undefined} color={T.success} />
      <StatCard icon="🔝" label="Largest Transaction Today" value={c.largestTransactionToday ? fmt(c.largestTransactionToday.amount) : '-'} sub={c.largestTransactionToday ? memberLabel(c.largestTransactionToday.memberId, c.largestTransactionToday.memberName) : undefined} color={T.warning} />
    </div>
  );
};

// ── Quick reports tab ──
const QuickReportsTab: React.FC<{ data: ReportData }> = ({ data }) => {
  const [sel, setSel] = useState<string>('today');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');

  const custom = sel === 'custom';
  const win = custom
    ? (() => {
        const rows = data.transactions.filter(r => (!from || (r.date || '') >= from) && (!to || (r.date || '') <= to));
        const sum = (k: string) => rows.filter(r => r.type === k && r.completed).reduce((a, r) => a + r.amount, 0);
        const cnt = (k: string) => rows.filter(r => r.type === k).length;
        return {
          count: rows.length, totalAmount: rows.filter(r => r.completed).reduce((a, r) => a + r.amount, 0),
          deposits: sum('deposit'), withdrawals: sum('withdrawal'), settlements: sum('settlement'),
          depositCount: cnt('deposit'), withdrawalCount: cnt('withdrawal'), settlementCount: cnt('settlement'),
        };
      })()
    : (data.windows as Record<string, ReportData['windows']['10m']>)[sel];

  const metric = (label: string, value: string, color: string) => (
    <Card className="c5-hover-lift" style={{ padding: '16px 18px' }}>
      <p style={{ margin: '0 0 6px', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
      <p style={{ margin: 0, fontSize: 22, fontWeight: 800, color }}>{value}</p>
    </Card>
  );

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
        {WIN_LABELS.map(([k, label]) => <button key={k} className="c5-btn" onClick={() => setSel(k)} style={pill(sel === k)}>{label}</button>)}
        <button className="c5-btn" onClick={() => setSel('custom')} style={pill(custom)}>Custom Date Range</button>
      </div>
      {custom && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          <Input label="From" type="date" value={from} onChange={e => setFrom(e.target.value)} />
          <Input label="To" type="date" value={to} onChange={e => setTo(e.target.value)} />
        </div>
      )}
      <div key={sel} className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
        {metric('Number of Transactions', String(win?.count ?? 0), T.blue)}
        {metric('Total Amount', fmt(win?.totalAmount ?? 0), T.textMain)}
        {metric('Deposits', fmt(win?.deposits ?? 0), T.success)}
        {metric('Withdrawals', fmt(win?.withdrawals ?? 0), T.danger)}
        {metric('Settlements', fmt(win?.settlements ?? 0), T.warning)}
      </div>
      <p style={{ fontSize: 11, color: T.textMuted, marginTop: 12 }}>Amounts are summed over completed transactions; counts include all transactions in the selected window.</p>
    </div>
  );
};

// ── Members tab ──
const MembersTab: React.FC<{ data: ReportData; onPick: (id: string) => void }> = ({ data, onPick }) => {
  const m = data.memberAnalytics;
  return (
    <div>
      <RSectionTitle note="Ranked by number of transactions. Click a row to open the member profile.">Most Active Members</RSectionTitle>
      <MemberTable rows={m.mostActive} valueKey="count" valueLabel="Total Transactions" showRank onPick={onPick} />
      <RSectionTitle>Largest Deposit Members</RSectionTitle>
      <MemberTable rows={m.largestDeposit} valueKey="deposit" valueLabel="Largest Deposit" money onPick={onPick} />
      <RSectionTitle>Largest Withdrawal Members</RSectionTitle>
      <MemberTable rows={m.largestWithdrawal} valueKey="withdrawal" valueLabel="Largest Withdrawal" money onPick={onPick} />
      <RSectionTitle>Largest Settlement Members</RSectionTitle>
      <MemberTable rows={m.largestSettlement} valueKey="settlement" valueLabel="Largest Settlement" money onPick={onPick} />
      <RSectionTitle note="Total Deposits + Withdrawals + Settlements, ranked highest to lowest.">Highest Value Members (Leaderboard)</RSectionTitle>
      <MemberTable rows={m.highestValue} valueKey="total" valueLabel="Total Volume" money showRank onPick={onPick} />
    </div>
  );
};

// ── Transaction intelligence tab ──
const IntelTab: React.FC<{ data: ReportData }> = ({ data }) => {
  const i = data.intelligence;
  const [thr, setThr] = useState('50000');
  const [win, setWin] = useState('30m');
  const minutes: Record<string, number> = { '10m': 10, '20m': 20, '30m': 30, '1h': 60 };
  const cutoff = Date.now() - minutes[win] * 60000;
  const recent = data.transactions.filter(r => r.createdAt && new Date(r.createdAt).getTime() >= cutoff && r.amount >= Number(thr));

  const bigCard = (title: string, x: ReportData['intelligence']['largestDepositEver'], color: string) => (
    <Card className="c5-hover-lift" style={{ padding: 18 }}>
      <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{title}</p>
      {x ? (
        <>
          <p style={{ margin: '0 0 4px', fontSize: 26, fontWeight: 800, color }}>{fmt(x.amount)}</p>
          <p style={{ margin: 0, fontSize: 13, color: T.textMain }}>{memberLabel(x.memberId, x.memberName)}</p>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>{x.date} {x.time}</p>
        </>
      ) : <p style={{ margin: 0, color: T.textMuted }}>No data yet.</p>}
    </Card>
  );

  return (
    <div>
      <RSectionTitle>Largest Transactions Ever</RSectionTitle>
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(240px,1fr))', gap: 14 }}>
        {bigCard('Largest Deposit Ever', i.largestDepositEver, T.success)}
        {bigCard('Largest Withdrawal Ever', i.largestWithdrawalEver, T.danger)}
        {bigCard('Largest Settlement Ever', i.largestSettlementEver, T.warning)}
      </div>
      <RSectionTitle note="High-value transactions in a recent window, above a chosen threshold.">Recent High-Value Transactions</RSectionTitle>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
        <Sel label="Window" value={win} onChange={e => setWin(e.target.value)} options={[['10m', 'Last 10 Minutes'], ['20m', 'Last 20 Minutes'], ['30m', 'Last 30 Minutes'], ['1h', 'Last 1 Hour']].map(([v, l]) => ({ value: v, label: l }))} />
        <Sel label="Minimum Amount" value={thr} onChange={e => setThr(e.target.value)} options={[['50000', 'INR 50,000+'], ['100000', 'INR 1,00,000+'], ['500000', 'INR 5,00,000+']].map(([v, l]) => ({ value: v, label: l }))} />
      </div>
      <ReportRowsTable rows={recent} empty="No high-value transactions in this window." />
    </div>
  );
};

// Concrete chart series colors — recharts renders these as SVG attributes (var() won't
// resolve there), and status colors stay the same in light/dark (req 6).
const SERIES = { success: '#059669', danger: '#dc2626', warning: '#d97706', blue: '#0052cc' };

// ── Trends tab ──
const TrendsTab: React.FC<{ data: ReportData }> = ({ data }) => {
  const { chart } = useTheme();
  const t = data.trends;
  const [gran, setGran] = useState<'daily' | 'weekly' | 'monthly'>('daily');
  const fullDaily = t.deposits.map((d, idx) => ({
    date: d.date, Deposits: d.amount,
    Withdrawals: t.withdrawals[idx]?.amount ?? 0,
    Settlements: t.settlements[idx]?.amount ?? 0,
  }));
  // Aggregate the daily series into the chosen granularity (sum per week/month).
  const merged = (() => {
    if (gran === 'daily') return fullDaily.map(d => ({ ...d, date: d.date.slice(5) }));
    const b: Record<string, { date: string; Deposits: number; Withdrawals: number; Settlements: number }> = {};
    for (const d of fullDaily) {
      const dt = new Date(d.date + 'T00:00:00Z');
      let key: string, label: string;
      if (gran === 'monthly') { key = d.date.slice(0, 7); label = dt.toLocaleDateString('en-IN', { month: 'short', year: '2-digit', timeZone: 'UTC' }); }
      else { const mon = new Date(dt); mon.setUTCDate(dt.getUTCDate() - ((dt.getUTCDay() + 6) % 7)); key = mon.toISOString().slice(0, 10); label = key.slice(5); }
      if (!b[key]) b[key] = { date: label, Deposits: 0, Withdrawals: 0, Settlements: 0 };
      b[key].Deposits += d.Deposits; b[key].Withdrawals += d.Withdrawals; b[key].Settlements += d.Settlements;
    }
    return Object.keys(b).sort().map(k => b[k]);
  })();
  const tick = gran === 'daily' ? 4 : 0;
  const granLabel = gran === 'daily' ? 'Daily' : gran === 'weekly' ? 'Weekly' : 'Monthly';
  const growth = t.membershipGrowth.map(g => ({ date: g.date.slice(5), New: g.count }));
  const axis = { fontSize: 11, fill: chart.axis };
  const tip = { background: chart.tooltipBg, border: `1px solid ${chart.tooltipBorder}`, borderRadius: 8, color: chart.tooltipText };

  const chartCard = (title: string, node: React.ReactNode) => (
    <Card className="c5-hover-lift" style={{ padding: 18 }}>
      <p style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 800, color: T.textMain }}>{title}</p>
      <div style={{ width: '100%', height: 240 }}>{node}</div>
    </Card>
  );

  const area = (key: string, color: string) => (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={merged} margin={{ top: 6, right: 10, left: -10, bottom: 0 }}>
        <defs><linearGradient id={`g-${key}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="5%" stopColor={color} stopOpacity={0.4} /><stop offset="95%" stopColor={color} stopOpacity={0} />
        </linearGradient></defs>
        <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
        <XAxis dataKey="date" tick={axis} interval={tick} /><YAxis tick={axis} width={54} />
        <RcTooltip formatter={(v) => fmt(Number(v))} contentStyle={tip} labelStyle={{ color: chart.tooltipText }} itemStyle={{ color: chart.tooltipText }} cursor={{ fill: chart.grid, opacity: 0.3 }} />
        <Area type="monotone" dataKey={key} stroke={color} strokeWidth={2} fill={`url(#g-${key})`} />
      </AreaChart>
    </ResponsiveContainer>
  );

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        {(['daily', 'weekly', 'monthly'] as const).map(g => (
          <button key={g} className="c5-btn" onClick={() => setGran(g)} style={pill(gran === g)}>{g[0].toUpperCase() + g.slice(1)}</button>
        ))}
      </div>
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(320px,1fr))', gap: 14 }}>
      {chartCard(`Deposit Trend (${granLabel})`, area('Deposits', SERIES.success))}
      {chartCard(`Withdrawal Trend (${granLabel})`, area('Withdrawals', SERIES.danger))}
      {chartCard(`Settlement Trend (${granLabel})`, area('Settlements', SERIES.warning))}
      {chartCard('Membership Growth (new active members/day)', (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={growth} margin={{ top: 6, right: 10, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
            <XAxis dataKey="date" tick={axis} interval={4} /><YAxis tick={axis} width={32} allowDecimals={false} />
            <RcTooltip contentStyle={tip} labelStyle={{ color: chart.tooltipText }} itemStyle={{ color: chart.tooltipText }} cursor={{ fill: chart.grid, opacity: 0.3 }} /><Bar dataKey="New" fill={SERIES.blue} radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ))}
      </div>
    </div>
  );
};

// ── Search tab ──
const SearchTab: React.FC<{ data: ReportData; onPick: (id: string) => void }> = ({ data, onPick }) => {
  const [member, setMember] = useState('');
  const [name, setName] = useState('');
  const [ref, setRef] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');
  const [minA, setMinA] = useState('');
  const [maxA, setMaxA] = useState('');

  const rows = data.transactions.filter(r =>
    (!member || (r.memberId || '').toLowerCase().includes(member.toLowerCase())) &&
    (!name || r.member.toLowerCase().includes(name.toLowerCase())) &&
    (!ref || r.ref.toLowerCase().includes(ref.toLowerCase())) &&
    (!from || (r.date || '') >= from) && (!to || (r.date || '') <= to) &&
    (!type || r.type === type) &&
    (!status || r.status === status) &&
    (!minA || r.amount >= Number(minA)) && (!maxA || r.amount <= Number(maxA))
  );

  const statuses = Array.from(new Set(data.transactions.map(r => r.status)));

  return (
    <div>
      <Card style={{ padding: 18, marginBottom: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Membership Number" value={member} onChange={e => setMember(e.target.value)} placeholder="e.g. MBR10001" />
          <Input label="Member Name" value={name} onChange={e => setName(e.target.value)} />
          <Input label="Reference Number" value={ref} onChange={e => setRef(e.target.value)} />
          <Sel label="Transaction Type" value={type} onChange={e => setType(e.target.value)} options={[{ value: '', label: 'All' }, { value: 'deposit', label: 'Deposit' }, { value: 'withdrawal', label: 'Withdrawal' }, { value: 'settlement', label: 'Settlement' }]} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} options={[{ value: '', label: 'All' }, ...statuses.map(s => ({ value: s, label: prettyStatusR(s) }))]} />
          <Input label="From Date" type="date" value={from} onChange={e => setFrom(e.target.value)} />
          <Input label="To Date" type="date" value={to} onChange={e => setTo(e.target.value)} />
          <Input label="Min Amount" type="number" value={minA} onChange={e => setMinA(e.target.value)} />
          <Input label="Max Amount" type="number" value={maxA} onChange={e => setMaxA(e.target.value)} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center' }}>
          <Btn size="sm" variant="secondary" onClick={() => exportRowsXlsx(rows, `clari5pay-search-${today()}.xlsx`)}>📊 Download Excel</Btn>
          <span style={{ fontSize: 12, color: T.textMuted }}>{rows.length} result(s)</span>
        </div>
      </Card>
      <ReportRowsTable rows={rows} onPick={onPick} empty="No transactions match your filters." />
    </div>
  );
};

// ── Member profile drill-down modal ──
const MemberProfileModal: React.FC<{ data: ReportData; memberId: string; onClose: () => void }> = ({ data, memberId, onClose }) => {
  const rows = data.transactions.filter(r => r.memberId === memberId);
  const name = rows.find(r => r.member && r.member !== '-')?.member || '-';
  const completed = rows.filter(r => r.completed);
  const sum = (k: string) => completed.filter(r => r.type === k).reduce((a, r) => a + r.amount, 0);
  const largest = (k: string) => completed.filter(r => r.type === k).reduce((a, r) => Math.max(a, r.amount), 0);
  const dates = rows.map(r => r.date).filter(Boolean).sort();
  const stat = (label: string, value: string, color: string = T.textMain) => (
    <div style={{ padding: '10px 0', borderBottom: `1px solid ${T.borderLight}`, display: 'flex', justifyContent: 'space-between' }}>
      <span style={{ fontSize: 12.5, color: T.textMuted }}>{label}</span>
      <span style={{ fontSize: 13, fontWeight: 800, color }}>{value}</span>
    </div>
  );

  return (
    <Modal title={`Member Profile - ${memberId}`} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
        <div>
          <h4 style={{ margin: '0 0 8px', fontSize: 13, color: T.blue }}>Member Information</h4>
          {stat('Membership Number', memberId)}
          {stat('Member Name', name)}
          {stat('First Transaction Date', dates[0] || '-')}
          {stat('Last Transaction Date', dates[dates.length - 1] || '-')}
        </div>
        <div>
          <h4 style={{ margin: '0 0 8px', fontSize: 13, color: T.blue }}>Statistics</h4>
          {stat('Total Deposits', fmt(sum('deposit')), T.success)}
          {stat('Total Withdrawals', fmt(sum('withdrawal')), T.danger)}
          {stat('Total Settlements', fmt(sum('settlement')), T.warning)}
          {stat('Largest Deposit', fmt(largest('deposit')))}
          {stat('Largest Withdrawal', fmt(largest('withdrawal')))}
          {stat('Largest Settlement', fmt(largest('settlement')))}
          {stat('Total Transaction Count', String(rows.length))}
          {stat('Total Transaction Amount', fmt(completed.reduce((a, r) => a + r.amount, 0)), T.blue)}
        </div>
      </div>
      <h4 style={{ margin: '18px 0 8px', fontSize: 13, color: T.blue }}>Transaction History</h4>
      <ReportRowsTable rows={rows} empty="No transactions for this member." />
    </Modal>
  );
};

// ── Main page ──
// ── Advanced-filter model + matching ───────────────────────────────────────────
const DATE_PRESETS: [string, string][] = [
  ['all', 'All Time'], ['today', 'Today'], ['yesterday', 'Yesterday'],
  ['30m', 'Last 30 Minutes'], ['1h', 'Last 1 Hour'], ['24h', 'Last 24 Hours'],
  ['7d', 'Last 7 Days'], ['30d', 'Last 30 Days'], ['custom', 'Custom Range'],
];
const PAYMENT_METHODS = ['UPI', 'BANK', 'IMPS', 'NEFT', 'RTGS', 'CASH', 'CRYPTO'];

interface RFilters {
  ref: string; memberId: string; memberName: string; combined: string; agentCode: string;
  approvedBy: string; processedBy: string; type: string; status: string; method: string;
  riskLevel: string; minA: string; maxA: string; exactA: string; datePreset: string; from: string; to: string;
  fromTime: string; toTime: string;   // Custom Range time-of-day bounds (IST, "HH:MM")
  business: string;   // admin Reports only — client-side business-name filter (all-merchants view)
}
const EMPTY_FILTERS: RFilters = {
  ref: '', memberId: '', memberName: '', combined: '', agentCode: '', approvedBy: '', processedBy: '',
  type: '', status: '', method: '', riskLevel: '', minA: '', maxA: '', exactA: '', datePreset: 'all', from: '', to: '',
  fromTime: '', toTime: '',
  business: '',
};

const rowTs = (r: ReportRow) => r.createdAt ? new Date(r.createdAt).getTime() : new Date(`${r.date}T${r.time || '00:00:00'}Z`).getTime();
// "HH:MM" (24h) → "h:MM AM/PM" for the Selected Date Range label. '' when no time chosen.
const fmtTime12 = (t: string): string => {
  if (!t) return '';
  const [hs, m] = t.split(':');
  let h = Number(hs); const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
};
const inDateWindow = (r: ReportRow, preset: string, from: string, to: string, fromTime = '', toTime = ''): boolean => {
  if (preset === 'all') return true;
  if (preset === 'custom') {
    // Compare on the transaction's own IST date+time (tx_date / tx_time are already IST) — no UTC.
    // Missing time defaults to start-of-day (From) / end-of-day (To), preserving date-only behaviour.
    const rDT = `${r.date || ''}T${r.time || '00:00:00'}`;
    const fromDT = from ? `${from}T${(fromTime || '00:00')}:00` : '';
    const toDT = to ? `${to}T${(toTime || '23:59')}:59` : '';
    return (!fromDT || rDT >= fromDT) && (!toDT || rDT <= toDT);
  }
  const ts = rowTs(r); const now = Date.now(); const day = 86400000;
  if (preset === '30m') return ts >= now - 30 * 60000;
  if (preset === '1h') return ts >= now - 3600000;
  if (preset === '24h') return ts >= now - day;
  if (preset === '7d') return ts >= now - 7 * day;
  if (preset === '30d') return ts >= now - 30 * day;
  const start = new Date(); start.setHours(0, 0, 0, 0);
  if (preset === 'today') return ts >= start.getTime();
  if (preset === 'yesterday') return ts >= start.getTime() - day && ts < start.getTime();
  return true;
};
const matchesFilters = (r: ReportRow, f: RFilters): boolean => {
  const inc = (v: string | null | undefined, q: string) => !q || (v || '').toLowerCase().includes(q.toLowerCase());
  return inc(r.ref, f.ref) && inc(r.memberId, f.memberId) && inc(r.member, f.memberName)
    && inc(r.business, f.business)
    && (!f.combined || memberLabel(r.memberId, r.member).toLowerCase().includes(f.combined.toLowerCase()))
    && inc(r.agentCode, f.agentCode) && inc(r.approvedBy, f.approvedBy) && inc(r.processedBy, f.processedBy)
    && (!f.type || r.type === f.type) && (!f.status || r.status === f.status)
    && (!f.method || (r.paymentMethod || '').toUpperCase() === f.method) && (!f.riskLevel || (r.riskLevel || '') === f.riskLevel)
    && (!f.minA || r.amount >= Number(f.minA)) && (!f.maxA || r.amount <= Number(f.maxA))
    && (!f.exactA || r.amount === Number(f.exactA))
    && inDateWindow(r, f.datePreset, f.from, f.to, f.fromTime, f.toTime);
};
const totalsOf = (rows: ReportRow[]) => {
  const sum = (k: string) => rows.filter(r => r.type === k && r.completed).reduce((a, r) => a + r.amount, 0);
  return { deposits: sum('deposit'), withdrawals: sum('withdrawal'), settlements: sum('settlement') };
};

// ── Filter-aware report export (Download PDF / Print) — includes the filtered table ──
function exportFilteredReport(data: ReportData, rows: ReportRow[], businessName: string, generatedBy: string, rangeLabel: string, autoPrint = true) {
  const w = window.open('', '_blank', 'width=1180,height=820');
  if (!w) { alert('Please allow pop-ups to export the report.'); return; }
  const c = data.cards; const now = new Date().toLocaleString('en-IN'); const tot = totalsOf(rows);
  const esc = (s: unknown) => String(s ?? '—').replace(/[&<>]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch] as string));
  const kpi = (l: string, v: string) => `<div class="kpi"><div class="kl">${l}</div><div class="kv">${v}</div></div>`;
  const body = rows.map((r, i) => `<tr class="${i % 2 ? 'alt' : ''}"><td class="mono">${esc(r.ref)}</td><td>${esc(memberLabel(r.memberId, r.member))}</td><td>${esc(rtypeLabel(r))}</td><td class="amt">${esc(fmt(r.amount))}</td><td>${esc(prettyStatusR(r.status))}</td><td class="nw">${esc(r.date)} ${esc(r.time)}</td><td>${esc(r.paymentMethod ? depositTypeLabel(r.paymentMethod) : '—')}</td><td class="amt">${r.availableBalance != null ? esc(fmt(r.availableBalance)) : '—'}</td><td>${esc(r.approvedBy)}</td><td>${esc(r.processedBy)}</td></tr>`).join('');
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Clari5Pay Report</title><style>
    @page{size:A4 landscape;margin:12mm}*{box-sizing:border-box}body{font-family:Arial,'Segoe UI',sans-serif;color:#0a2540;margin:0}
    .head{display:flex;align-items:center;gap:14px;border-bottom:3px solid #0052cc;padding-bottom:10px}
    .brand{font-size:22px;font-weight:800}.brand .b{color:#0052cc}.brand .g{color:#26d00c}.meta{margin-left:auto;text-align:right;font-size:11px;color:#4a5568;line-height:1.6}
    h2{font-size:13px;margin:16px 0 8px;color:#0052cc}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
    .kpi{border:1px solid #e2e8f0;border-radius:8px;padding:7px 9px}.kl{font-size:8.5px;text-transform:uppercase;letter-spacing:.04em;color:#64748b}.kv{font-size:13px;font-weight:800;margin-top:2px}
    table{width:100%;border-collapse:collapse;font-size:9.5px}th{background:#0a2540;color:#fff;text-align:left;padding:6px 7px;font-size:8.5px;text-transform:uppercase}
    td{padding:5px 7px;border-bottom:1px solid #e2e8f0}tr.alt td{background:#f5f8ff}.amt{text-align:right;font-weight:700}.mono{font-family:'Courier New',monospace}.nw{white-space:nowrap}
    tfoot td{font-weight:800;background:#eef4ff;border-top:2px solid #0052cc}footer{margin-top:14px;font-size:9px;color:#9ca3af;text-align:center}
  </style></head><body>
    <div class="head"><span class="brand"><span class="b">clari</span><span class="g">5</span>pay</span>
      <div class="meta">Transaction Report — CONFIDENTIAL<br>${esc(businessName)}<br>Generated: ${esc(now)} · By ${esc(generatedBy)}<br>Range: ${esc(rangeLabel)} · ${rows.length} transaction(s)</div></div>
    <h2>Summary</h2><div class="kpis">
      ${kpi('Total Deposits', fmt(c.totalDepositAmount))}${kpi('Total Withdrawals', fmt(c.totalWithdrawalAmount))}${kpi('Total Settlements', fmt(c.totalSettlementAmount))}
      ${kpi('Available Balance', fmt(c.availableBalance))}</div>
    <h2>Transactions (filtered)</h2>
    <table><thead><tr><th>Reference</th><th>Membership - Member</th><th>Type</th><th style="text-align:right">Amount</th><th>Status</th><th>Date &amp; Time</th><th>Payment Method</th><th style="text-align:right">Avail. Balance</th><th>Approved By</th><th>Processed By</th></tr></thead>
      <tbody>${body || '<tr><td colspan="10" style="text-align:center;padding:24px;color:#9ca3af">No transactions match the selected filters.</td></tr>'}</tbody>
      <tfoot><tr><td colspan="3">Footer Totals (filtered)</td><td class="amt">Dep ${esc(fmt(tot.deposits))}</td><td colspan="2">Wd ${esc(fmt(tot.withdrawals))}</td><td colspan="2">Set ${esc(fmt(tot.settlements))}</td><td colspan="2">Available ${esc(fmt(c.availableBalance))}</td></tr></tfoot>
    </table>
    <footer>Clari5Pay — confidential. Generated from live platform data, honouring the selected filters.</footer>
  </body></html>`);
  w.document.close(); w.focus();
  if (autoPrint) setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

// ─── Treasury Report & Agent Ledger Report ──────────────────────────────────────
// Two focused report types layered on top of the existing Reports module. They reuse the
// same advanced-filter set (the already-`filtered` rows), the same xlsx helper and the same
// print-to-PDF mechanism — only the columns/derivation differ.

// Transaction Method label for a report row (UPI, Bank Transfer, QR, Cash, IMPS, …).
const methodLabel = (r: ReportRow) => (r.paymentMethod ? depositTypeLabel(r.paymentMethod) : '—');

// Generic CSV download (Excel-friendly: UTF-8 BOM + CRLF, quotes escaped).
function downloadCsv(filename: string, headers: string[], rows: Array<Array<string | number>>) {
  const esc = (v: string | number) => {
    const s = String(v ?? '');
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers, ...rows].map(r => r.map(esc).join(',')).join('\r\n');
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename.endsWith('.csv') ? filename : `${filename}.csv`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Print-to-PDF for a simple columnar report (shared by Treasury & Agent Ledger).
// `aligns[i] === 'r'` right-aligns that column (amounts). Same brand/letterhead as the
// existing filtered-report PDF so all exports look consistent.
function printColumnarReport(opts: {
  title: string; businessName: string; generatedBy: string; rangeLabel: string;
  headers: string[]; rows: Array<Array<string | number>>; aligns?: Array<'l' | 'r'>;
  footerNote?: string; autoPrint?: boolean;
}) {
  const { title, businessName, generatedBy, rangeLabel, headers, rows, aligns, footerNote, autoPrint = true } = opts;
  const w = window.open('', '_blank', 'width=1180,height=820');
  if (!w) { alert('Please allow pop-ups to export the report.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const esc = (s: unknown) => String(s ?? '—').replace(/[&<>]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch] as string));
  const thead = headers.map((h, i) => `<th${aligns?.[i] === 'r' ? ' style="text-align:right"' : ''}>${esc(h)}</th>`).join('');
  const body = rows.map((r, ri) => `<tr class="${ri % 2 ? 'alt' : ''}">${r.map((c, i) => `<td class="${aligns?.[i] === 'r' ? 'amt' : ''}">${esc(c)}</td>`).join('')}</tr>`).join('');
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Clari5Pay ${esc(title)}</title><style>
    @page{size:A4 landscape;margin:12mm}*{box-sizing:border-box}body{font-family:Arial,'Segoe UI',sans-serif;color:#0a2540;margin:0}
    .head{display:flex;align-items:center;gap:14px;border-bottom:3px solid #0052cc;padding-bottom:10px}
    .brand{font-size:22px;font-weight:800}.brand .b{color:#0052cc}.brand .g{color:#26d00c}.meta{margin-left:auto;text-align:right;font-size:11px;color:#4a5568;line-height:1.6}
    h2{font-size:14px;margin:16px 0 8px;color:#0052cc}
    table{width:100%;border-collapse:collapse;font-size:9.5px}th{background:#0a2540;color:#fff;text-align:left;padding:6px 7px;font-size:8.5px;text-transform:uppercase}
    td{padding:5px 7px;border-bottom:1px solid #e2e8f0}tr.alt td{background:#f5f8ff}.amt{text-align:right;font-weight:700}
    footer{margin-top:14px;font-size:9px;color:#9ca3af;text-align:center}
  </style></head><body>
    <div class="head"><span class="brand"><span class="b">clari</span><span class="g">5</span>pay</span>
      <div class="meta">${esc(title)} — CONFIDENTIAL<br>${esc(businessName)}<br>Generated: ${esc(now)} · By ${esc(generatedBy)}<br>Range: ${esc(rangeLabel)} · ${rows.length} row(s)</div></div>
    <h2>${esc(title)}</h2>
    <table><thead><tr>${thead}</tr></thead>
      <tbody>${body || `<tr><td colspan="${headers.length}" style="text-align:center;padding:24px;color:#9ca3af">No rows match the selected filters.</td></tr>`}</tbody>
    </table>
    <footer>Clari5Pay — confidential. ${esc(footerNote || 'Generated from live platform data, honouring the selected filters.')}</footer>
  </body></html>`);
  w.document.close(); w.focus();
  if (autoPrint) setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

// Shared export toolbar (PDF / Excel / CSV / Print) for the focused reports.
const ReportExportBar: React.FC<{ count: number; onPdf: () => void; onExcel: () => void; onCsv: () => void; onPrint: () => void }> =
  ({ count, onPdf, onExcel, onCsv, onPrint }) => (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
      <Btn size="sm" variant="secondary" onClick={onPdf}>📄 Download PDF</Btn>
      <Btn size="sm" variant="secondary" onClick={onExcel}>📊 Download Excel</Btn>
      <Btn size="sm" variant="secondary" onClick={onCsv}>🧾 Download CSV</Btn>
      <Btn size="sm" variant="secondary" onClick={onPrint}>🖨 Print Report</Btn>
      <span style={{ fontSize: 12, color: T.textMuted }}>{count} row(s)</span>
    </div>
  );

// ── Treasury Report: all transactions in their current status (narrow with the Status
// filter). Nine columns — the spec's eight plus a Status column for treasury visibility. ──
const TREASURY_HEADERS = ['Unique Transaction Reference', 'Member Name', 'Membership ID', 'Date & Time', 'Status', 'Approver', 'Operator', 'Transaction Amount', 'Transaction Method'];
const TREASURY_ALIGNS: Array<'l' | 'r'> = ['l', 'l', 'l', 'l', 'l', 'l', 'l', 'r', 'l'];
const TreasuryReport: React.FC<{ rows: ReportRow[]; businessName: string; generatedBy: string; rangeLabel: string }> =
  ({ rows, businessName, generatedBy, rangeLabel }) => {
    const toast = useToast();
    const data = rows;   // all transactions honouring the advanced filters (incl. Status)
    const csvRows = data.map(r => [r.ref, r.member || '', r.memberId || '', `${r.date || ''} ${r.time || ''}`.trim(), prettyStatusR(r.status), r.approvedBy || '', r.processedBy || '', r.amount, methodLabel(r)]);
    const pdfRows = data.map(r => [r.ref, r.member || '—', r.memberId || '—', `${r.date || ''} ${r.time || ''}`.trim(), prettyStatusR(r.status), r.approvedBy || '—', r.processedBy || '—', fmt(r.amount), methodLabel(r)]);
    const onExcel = () => {
      downloadXlsx(`clari5pay-treasury-${today()}.xlsx`, [{
        name: 'Treasury Report',
        columns: [
          { header: 'Unique Transaction Reference', get: r => r.ref, width: 22 },
          { header: 'Member Name', get: r => r.member || '', width: 22 },
          { header: 'Membership ID', get: r => r.memberId || '' },
          { header: 'Date & Time', get: r => `${r.date || ''} ${r.time || ''}`.trim(), width: 20 },
          { header: 'Status', get: r => prettyStatusR(r.status) },
          { header: 'Approver', get: r => r.approvedBy || '' },
          { header: 'Operator', get: r => r.processedBy || '' },
          { header: 'Transaction Amount', get: r => Number(r.amount), width: 16, z: INR_NUMFMT },
          { header: 'Transaction Method', get: r => methodLabel(r) },
        ],
        rows: data,
      }]);
      toast.showToast(`Treasury — ${data.length} rows`);
    };
    const onPdf = (autoPrint: boolean) => printColumnarReport({
      title: 'Treasury Report', businessName, generatedBy, rangeLabel,
      headers: TREASURY_HEADERS, rows: pdfRows, aligns: TREASURY_ALIGNS,
      footerNote: 'All transactions in their current status. Honours the selected filters.', autoPrint,
    });
    return (
      <div>
        <RSectionTitle note="All transactions in their current status (use the Status filter to narrow) — status, approver, operator, amount and method per transaction.">🏦 Treasury Report</RSectionTitle>
        <ReportExportBar count={data.length} onPdf={() => onPdf(true)} onExcel={onExcel} onCsv={() => downloadCsv(`clari5pay-treasury-${today()}.csv`, TREASURY_HEADERS, csvRows)} onPrint={() => onPdf(true)} />
        <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 14 }}>
          <div style={{ overflowX: 'auto', maxHeight: 560 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead><tr style={{ background: T.canvas }}>{TREASURY_HEADERS.map((h, i) => <th key={h} style={{ ...thR, textAlign: TREASURY_ALIGNS[i] === 'r' ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
              <tbody>
                {data.length === 0 && <tr><td colSpan={TREASURY_HEADERS.length} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>No transactions match the selected filters.</td></tr>}
                {data.slice(0, 500).map(r => (
                  <tr key={r.ref} className="c5-row-hover">
                    <td style={{ ...tdR, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{r.ref}</td>
                    <td style={{ ...tdR, fontWeight: 600 }}>{r.member || '—'}</td>
                    <td style={tdR}>{r.memberId || '—'}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{r.date} {r.time}</td>
                    <td style={tdR}>{prettyStatusR(r.status)}</td>
                    <td style={tdR}>{r.approvedBy || '—'}</td>
                    <td style={tdR}>{r.processedBy || '—'}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700 }}>{fmt(r.amount)}</td>
                    <td style={tdR}>{methodLabel(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    );
  };

// ── Agent Ledger Report: completed transactions in chronological order, with a
// sequential running balance (Opening + Deposits − Withdrawals − Settlements). ──
const LEDGER_HEADERS = ['Date', 'Time', 'Description', 'Transaction Reference', 'Amount', 'Running Balance'];
const signed = (n: number) => `${n >= 0 ? '+' : '−'}${fmt(Math.abs(n))}`;
export const AgentLedgerReport: React.FC<{ rows: ReportRow[]; allRows?: ReportRow[]; businessName: string; generatedBy: string; rangeLabel: string }> =
  ({ rows, allRows, businessName, generatedBy, rangeLabel }) => {
    const toast = useToast();
    // Deposits add; withdrawals & settlements subtract (shared Total Available Balance rule).
    const delta = (r: ReportRow) => (r.type === 'deposit' ? r.amount : -r.amount);
    // Running Balance is computed over the FULL completed ledger (oldest first), seeded from
    // zero at the first all-time transaction — the formula is unchanged. Filtering only hides
    // rows; the balance always carries forward from the transactions before the filter, exactly
    // like a bank statement. `rows` are the filtered rows to display; `allRows` is the full,
    // unfiltered set they came from (falls back to `rows` when a caller doesn't supply it).
    const shown = new Set(rows);
    const fullLedger = (() => {
      const completed = (allRows || rows).filter(r => r.completed).slice().sort((a, b) => rowTs(a) - rowTs(b));
      let bal = 0;
      return completed.map(r => {
        const d = delta(r);
        bal += d;
        return { date: r.date, time: r.time, description: `${RTYPE_LABEL[r.type || ''] || 'Transaction'} — ${memberLabel(r.memberId, r.member)}`, ref: r.ref, amount: d, balance: bal, shown: shown.has(r) };
      });
    })();
    // Rows actually displayed — those passing the active filters — in chronological order.
    const ledger = fullLedger.filter(l => l.shown);
    // Opening Balance = the Running Balance immediately BEFORE the first displayed transaction
    // (bank-statement style). When the first displayed transaction is also the first of all
    // time there is no prior balance, so we fall back to its own Running Balance. 0 only when
    // the filtered report is empty.
    const firstIdx = fullLedger.findIndex(l => l.shown);
    const opening = firstIdx < 0 ? 0 : (firstIdx > 0 ? fullLedger[firstIdx - 1].balance : fullLedger[0].balance);
    const closing = ledger.length ? ledger[ledger.length - 1].balance : 0;
    const csvRows: Array<Array<string | number>> = [['', '', 'Opening Balance', '', '', opening], ...ledger.map(l => [l.date, l.time, l.description, l.ref, l.amount, l.balance])];
    const pdfRows: Array<Array<string | number>> = [['—', '—', 'Opening Balance', '—', '—', fmt(opening)], ...ledger.map(l => [l.date, l.time, l.description, l.ref, signed(l.amount), fmt(l.balance)])];
    const onExcel = () => {
      downloadXlsx(`clari5pay-agent-ledger-${today()}.xlsx`, [{
        name: 'Agent Ledger',
        columns: [
          { header: 'Date', get: (l: typeof ledger[number]) => l.date },
          { header: 'Time', get: l => l.time },
          { header: 'Description', get: l => l.description, width: 32 },
          { header: 'Transaction Reference', get: l => l.ref, width: 22 },
          { header: 'Amount', get: l => Number(l.amount), width: 16, z: INR_NUMFMT },
          { header: 'Running Balance', get: l => Number(l.balance), width: 18, z: INR_NUMFMT },
        ],
        rows: ledger,
      }]);
      toast.showToast(`Agent Ledger — ${ledger.length} rows`);
    };
    const onPdf = (autoPrint: boolean) => printColumnarReport({
      title: 'Agent Ledger Report', businessName, generatedBy, rangeLabel,
      headers: LEDGER_HEADERS, rows: pdfRows, aligns: ['l', 'l', 'l', 'l', 'r', 'r'],
      footerNote: `Running Balance = Opening + Deposits − Withdrawals − Settlements (shared Total Available Balance, no commission). Closing ${fmt(closing)}. Honours the selected filters.`, autoPrint,
    });
    return (
      <div>
        <RSectionTitle note="Completed transactions in chronological order. Running Balance = Opening + Deposits − Withdrawals − Settlements — the shared Total Available Balance formula (no commission deducted).">📒 Agent Ledger Report</RSectionTitle>
        <ReportExportBar count={ledger.length} onPdf={() => onPdf(true)} onExcel={onExcel} onCsv={() => downloadCsv(`clari5pay-agent-ledger-${today()}.csv`, LEDGER_HEADERS, csvRows)} onPrint={() => onPdf(true)} />
        <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 14 }}>
          <div style={{ overflowX: 'auto', maxHeight: 560 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead><tr style={{ background: T.canvas }}>{LEDGER_HEADERS.map((h, i) => <th key={h} style={{ ...thR, textAlign: i >= 4 ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
              <tbody>
                <tr style={{ background: T.canvas }}>
                  <td style={tdR} /><td style={tdR} />
                  <td style={{ ...tdR, fontWeight: 700, color: T.textMuted }}>Opening Balance</td>
                  <td style={tdR} /><td style={{ ...tdR, textAlign: 'right' }}>—</td>
                  <td style={{ ...tdR, textAlign: 'right', fontWeight: 800 }}>{fmt(opening)}</td>
                </tr>
                {ledger.length === 0 && <tr><td colSpan={6} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>No completed transactions match the selected filters.</td></tr>}
                {ledger.slice(0, 500).map((l, idx) => (
                  <tr key={`${l.ref}-${idx}`} className="c5-row-hover">
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{l.date}</td>
                    <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{l.time}</td>
                    <td style={tdR}>{l.description}</td>
                    <td style={{ ...tdR, fontFamily: 'monospace', color: T.blue }}>{l.ref}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 700, color: l.amount >= 0 ? T.success : T.danger }}>{signed(l.amount)}</td>
                    <td style={{ ...tdR, textAlign: 'right', fontWeight: 800 }}>{fmt(l.balance)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card style={{ padding: '14px 18px', marginBottom: 18, borderTop: `3px solid ${T.blue}` }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))', gap: 14 }}>
            <div><span style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'block' }}>Opening Balance</span><span style={{ fontSize: 15, fontWeight: 800, color: T.textMain }}>{fmt(opening)}</span></div>
            <div><span style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'block' }}>Entries</span><span style={{ fontSize: 15, fontWeight: 800, color: T.textMain }}>{ledger.length}</span></div>
            <div><span style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'block' }}>Closing Balance (Total Available)</span><span style={{ fontSize: 15, fontWeight: 800, color: closing >= 0 ? T.success : T.danger }}>{fmt(closing)}</span></div>
          </div>
        </Card>
      </div>
    );
  };

// ── Reusable Reports view ──
// Shared by the Merchant Reports (own business) and the Admin Reports (all merchants, or one
// selected merchant). The financial/analytics payload always comes from the backend
// (single source of truth) — this component only renders + applies client-side filters.
interface ReportsViewProps {
  data: ReportData | null;
  reload: () => Promise<void> | void;
  businessName: string;     // shown as "Merchant Name" + used in PDF/Excel titles
  generatedBy: string;
  subtitle?: string;
  merchantSelector?: React.ReactNode;   // admin: scope dropdown rendered in the header
  showBusinessFilter?: boolean;         // admin all-merchants: enables the Business Name filter
}

const ReportsView: React.FC<ReportsViewProps> = ({
  data, reload, businessName, generatedBy, subtitle, merchantSelector, showBusinessFilter,
}) => {
  const [profileId, setProfileId] = useState<string | null>(null);
  // Which report the page is showing: the full analytics dashboard (default), or one of the
  // two focused report types. All three share the same advanced filters + applied filter set.
  const [reportType, setReportType] = useState<'full' | 'treasury' | 'ledger'>('full');
  // `draft` is bound to the filter inputs; `f` is the *applied* filter set that
  // actually drives the table, summary/footer totals, count and exports. Editing a
  // field only updates the draft — nothing filters until "Apply Filters" is pressed.
  const [draft, setDraft] = useState<RFilters>(EMPTY_FILTERS);
  const [f, setF] = useState<RFilters>(EMPTY_FILTERS);
  const [applying, setApplying] = useState(false);
  const [genAt] = useState(() => new Date());
  const toast = useToast();
  const set = (k: keyof RFilters, v: string) => setDraft(p => ({ ...p, [k]: v }));

  // Apply Filters — pull a fresh server snapshot (the existing reports API), then
  // commit the draft so the table, summary cards, footer totals, charts, count and
  // exports all reflect the same filter set together. Disabled while in flight so a
  // second click can't fire a duplicate request.
  const applyFilters = async () => {
    if (applying) return;
    // Custom Range validation: "To" date & time cannot be earlier than "From".
    if (draft.datePreset === 'custom' && draft.from && draft.to) {
      const fromDT = `${draft.from}T${(draft.fromTime || '00:00')}:00`;
      const toDT = `${draft.to}T${(draft.toTime || '23:59')}:59`;
      if (toDT < fromDT) { toast.showToast('“To” date & time cannot be earlier than “From”.', 'error'); return; }
    }
    setApplying(true);
    try { await reload(); setF(draft); }
    finally { setApplying(false); }
  };
  // Clear Filters — reset every field to its default, refresh from the server and
  // return the table / cards / footer / count to the full record set.
  const clearFilters = async () => {
    if (applying) return;
    setApplying(true);
    setDraft(EMPTY_FILTERS);
    try { await reload(); setF(EMPTY_FILTERS); }
    finally { setApplying(false); }
  };

  if (!data) return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 14 }}>
        {[0, 1, 2, 3, 4, 5, 6].map(i => (
          <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={120} h={24} /></Card>
        ))}
      </div>
    </div>
  );

  const c = data.cards;
  const statuses = Array.from(new Set(data.transactions.map(r => r.status)));
  const filtered = data.transactions.filter(r => matchesFilters(r, f));
  const tot = totalsOf(filtered);
  const rangeLabel = f.datePreset === 'custom'
    ? `${f.from || 'start'}${f.fromTime ? ' ' + fmtTime12(f.fromTime) : ''} → ${f.to || 'today'}${f.toTime ? ' ' + fmtTime12(f.toTime) : ''}`
    : (DATE_PRESETS.find(d => d[0] === f.datePreset)?.[1] || 'All Time');
  // 1 — Summary cards (colour-coded per spec)
  const card = (label: string, value: number, color: string) => (
    <Card className="c5-hover-lift" style={{ padding: '14px 16px', borderTop: `3px solid ${color}` }}>
      <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
      <p style={{ margin: 0, fontSize: 19, fontWeight: 800, color }}>{fmt(value)}</p>
    </Card>
  );
  const meta = (label: string, value: React.ReactNode) => (
    <div><span style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'block' }}>{label}</span><span style={{ fontSize: 13, fontWeight: 700, color: T.textMain }}>{value}</span></div>
  );

  const downloadExcel = () => { exportRowsXlsx(filtered, `clari5pay-report-${today()}.xlsx`); toast.showToast(`Excel — ${filtered.length} rows`); };

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', marginBottom: 16 }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800 }}>Reports &amp; Analytics</h2>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>{subtitle || 'Financial intelligence dashboard — your memberships, transactions and reports.'}</p>
        </div>
        {merchantSelector}
        {reportType === 'full' && <>
          <Btn size="sm" variant="secondary" onClick={() => exportFilteredReport(data, filtered, businessName, generatedBy, rangeLabel, true)}>📄 Download PDF</Btn>
          <Btn size="sm" variant="secondary" onClick={downloadExcel}>📊 Download Excel</Btn>
          <Btn size="sm" variant="secondary" onClick={() => exportFilteredReport(data, filtered, businessName, generatedBy, rangeLabel, true)}>🖨 Print Report</Btn>
        </>}
      </div>

      {/* Report-type selector — Full dashboard · Treasury Report · Agent Ledger Report */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        {([['full', '📊 Full Report'], ['treasury', '🏦 Treasury Report'], ['ledger', '📒 Agent Ledger Report']] as const).map(([k, label]) => (
          <button key={k} className="c5-btn" onClick={() => setReportType(k)} style={pill(reportType === k)}>{label}</button>
        ))}
      </div>

      {/* 1 — Summary cards */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12, marginBottom: 16 }}>
        {card('Total Deposits', c.totalDepositAmount, T.success)}
        {card('Total Withdrawals', c.totalWithdrawalAmount, T.danger)}
        {card('Total Settlements', c.totalSettlementAmount, T.blue)}
        {card('Available Balance', c.availableBalance, '#1d4ed8')}
      </div>

      {/* 2 — Report metadata */}
      <Card style={{ padding: '14px 18px', marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 14 }}>
          {meta('Merchant Name', businessName)}
          {meta('Generated By', generatedBy)}
          {meta('Generated Date & Time', genAt.toLocaleString('en-IN'))}
          {meta('Selected Date Range', rangeLabel)}
        </div>
      </Card>

      {/* 3 — Advanced filters */}
      <RSectionTitle note="Set your filters, then click Apply Filters to update the table, footer totals and exports together.">🔎 Advanced Filters</RSectionTitle>
      <Card style={{ padding: 16, marginBottom: 18 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
          {DATE_PRESETS.map(([k, label]) => <button key={k} className="c5-btn" onClick={() => set('datePreset', k)} style={pill(draft.datePreset === k)}>{label}</button>)}
        </div>
        {draft.datePreset === 'custom' && (
          <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
            <Input label="From Date" type="date" value={draft.from} onChange={e => set('from', e.target.value)} />
            <Input label="From Time" type="time" value={draft.fromTime} onChange={e => set('fromTime', e.target.value)} hint="IST · optional (start of day)" />
            <Input label="To Date" type="date" value={draft.to} onChange={e => set('to', e.target.value)} />
            <Input label="To Time" type="time" value={draft.toTime} onChange={e => set('toTime', e.target.value)} hint="IST · optional (end of day)" />
          </div>
        )}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          {showBusinessFilter && <Input label="Business Name" value={draft.business} onChange={e => set('business', e.target.value)} placeholder="Merchant business" />}
          <Input label="Reference Number" value={draft.ref} onChange={e => set('ref', e.target.value)} />
          <Input label="Membership Number" value={draft.memberId} onChange={e => set('memberId', e.target.value)} />
          <Input label="Member Name" value={draft.memberName} onChange={e => set('memberName', e.target.value)} />
          <Input label="Membership - Member" value={draft.combined} onChange={e => set('combined', e.target.value)} placeholder="MBR… - Name" />
          <Input label="Agent Code" value={draft.agentCode} onChange={e => set('agentCode', e.target.value)} />
          <Sel label="Transaction Type" value={draft.type} onChange={e => set('type', e.target.value)} options={[{ value: '', label: 'All' }, { value: 'deposit', label: 'Deposit' }, { value: 'withdrawal', label: 'Withdrawal' }, { value: 'settlement', label: 'Settlement' }]} />
          <Sel label="Status" value={draft.status} onChange={e => set('status', e.target.value)} options={[{ value: '', label: 'All' }, ...statuses.map(s => ({ value: s, label: prettyStatusR(s) }))]} />
          <Sel label="Payment Method" value={draft.method} onChange={e => set('method', e.target.value)} options={[{ value: '', label: 'All' }, ...PAYMENT_METHODS.map(m => ({ value: m, label: depositTypeLabel(m) }))]} />
          <Input label="Approved By" value={draft.approvedBy} onChange={e => set('approvedBy', e.target.value)} />
          <Input label="Processed By" value={draft.processedBy} onChange={e => set('processedBy', e.target.value)} />
          <Sel label="Risk Level" value={draft.riskLevel} onChange={e => set('riskLevel', e.target.value)} options={[{ value: '', label: 'All' }, { value: 'LOW', label: 'Low' }, { value: 'HIGH', label: 'High' }]} />
          <Input label="Minimum Amount" type="number" value={draft.minA} onChange={e => set('minA', e.target.value)} />
          <Input label="Maximum Amount" type="number" value={draft.maxA} onChange={e => set('maxA', e.target.value)} />
          <Input label="Exact Amount" type="number" value={draft.exactA} onChange={e => set('exactA', e.target.value)} />
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 12 }}>
          <Btn size="sm" onClick={applyFilters} disabled={applying}>{applying ? '⏳ Applying…' : '🔍 Apply Filters'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters} disabled={applying}>Clear Filters</Btn>
          <span style={{ fontSize: 12, color: T.textMuted }}>
            {applying ? 'Applying filters…' : `${filtered.length} of ${data.transactions.length} transactions`}
          </span>
        </div>
      </Card>

      {/* Focused report types (Treasury / Agent Ledger) — same filters, same exports */}
      {reportType === 'treasury' && <TreasuryReport rows={filtered} businessName={businessName} generatedBy={generatedBy} rangeLabel={rangeLabel} />}
      {reportType === 'ledger' && <AgentLedgerReport rows={filtered} allRows={data.transactions} businessName={businessName} generatedBy={generatedBy} rangeLabel={rangeLabel} />}

      {reportType === 'full' && <>
      {/* 4 — Membership analytics */}
      <RSectionTitle>👥 Membership Analytics</RSectionTitle>
      <MembersTab data={data} onPick={setProfileId} />

      {/* 5 — Transaction intelligence */}
      <RSectionTitle>🧠 Transaction Intelligence</RSectionTitle>
      <IntelTab data={data} />

      {/* 6 — Charts */}
      <RSectionTitle>📈 Trend Charts</RSectionTitle>
      <TrendsTab data={data} />

      {/* 7 — Transaction table (filtered) */}
      <RSectionTitle note="Reflects the advanced filters above.">📋 Transactions</RSectionTitle>
      <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 14 }}>
        <div style={{ overflowX: 'auto', maxHeight: 520 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                {['Reference Number', 'Membership - Member', 'Transaction Type', 'Amount', 'Status', 'Date & Time', 'Payment Method', 'Available Balance', 'Approved By', 'Processed By'].map(h => <th key={h} style={thR}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && <tr><td colSpan={10} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>No transactions match the selected filters.</td></tr>}
              {filtered.slice(0, 500).map(r => (
                <tr key={r.ref} className="c5-row-hover" style={{ cursor: r.memberId ? 'pointer' : 'default' }} onClick={() => r.memberId && setProfileId(r.memberId)}>
                  <td style={{ ...tdR, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{r.ref}</td>
                  <td style={{ ...tdR, fontWeight: 600 }}>{memberLabel(r.memberId, r.member)}</td>
                  <td style={tdR}>{rtypeLabel(r) || '-'}</td>
                  <td style={{ ...tdR, textAlign: 'right', fontWeight: 700 }}>{fmt(r.amount)}</td>
                  <td style={tdR}>{prettyStatusR(r.status)}</td>
                  <td style={{ ...tdR, whiteSpace: 'nowrap' }}>{r.date} {r.time}</td>
                  <td style={tdR}>{r.paymentMethod ? depositTypeLabel(r.paymentMethod) : '—'}</td>
                  <td style={{ ...tdR, textAlign: 'right', color: T.textMuted }}>{r.availableBalance != null ? fmt(r.availableBalance) : '—'}</td>
                  <td style={{ ...tdR, color: T.textMuted }}>{r.approvedBy || '—'}</td>
                  <td style={{ ...tdR, color: T.textMuted }}>{r.processedBy || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 8 — Footer summary */}
      <Card style={{ padding: '16px 18px', marginBottom: 18, borderTop: `3px solid ${T.blue}` }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 14 }}>
          {meta('Total Deposits', <span style={{ color: T.success }}>{fmt(tot.deposits)}</span>)}
          {meta('Total Withdrawals', <span style={{ color: T.danger }}>{fmt(tot.withdrawals)}</span>)}
          {meta('Total Settlements', <span style={{ color: T.blue }}>{fmt(tot.settlements)}</span>)}
          {meta('Available Balance', <span style={{ color: '#1d4ed8' }}>{fmt(c.availableBalance)}</span>)}
        </div>
      </Card>

      {profileId && <MemberProfileModal data={data} memberId={profileId} onClose={() => setProfileId(null)} />}

      <RSectionTitle note="Generated automatically from your live transaction data.">💡 Business Insights</RSectionTitle>
      <Card style={{ padding: 18 }}>
        {data.insights.length === 0 && <p style={{ margin: 0, color: T.textMuted, fontSize: 13 }}>Not enough activity yet to generate insights.</p>}
        {data.insights.map((i, idx) => (
          <div key={idx} style={{ display: 'flex', gap: 10, padding: '8px 0', borderBottom: idx < data.insights.length - 1 ? `1px solid ${T.borderLight}` : 'none' }}>
            <span style={{ color: T.blue, fontWeight: 800 }}>›</span>
            <span style={{ fontSize: 13, color: T.textMain, lineHeight: 1.5 }}>{i}</span>
          </div>
        ))}
      </Card>
      </>}
      <div style={{ height: 24 }} />
    </div>
  );
};

// ── Merchant Reports page (own business) ──
export const ReportsPage: React.FC<{ user: User }> = ({ user }) => {
  const [data, setData] = useState<ReportData | null>(null);
  const reload = () => transactionAPI.reports().then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  usePoll(reload, 30000);
  return <ReportsView data={data} reload={reload} businessName={user.name} generatedBy={user.name} />;
};

// ── Admin Reports page (all merchants, or one selected merchant) ──
// Same module/UI as the merchant Reports, but system-wide. The Merchant selector re-scopes
// the whole report server-side (cards, charts, analytics, table) using the SAME backend
// calculation, so a single merchant's figures here match exactly what that merchant sees.
export const AdminReportsPage: React.FC<{ user: User }> = ({ user }) => {
  const [data, setData] = useState<ReportData | null>(null);
  const [merchant, setMerchant] = useState('');         // '' = all merchants
  const [businesses, setBusinesses] = useState<string[]>([]);
  const reload = useCallback(
    () => transactionAPI.adminReports(merchant || undefined).then(setData).catch(() => {}),
    [merchant],
  );
  // Distinct merchant business names for the scope dropdown (system-wide).
  useEffect(() => {
    userAPI.getMerchants()
      .then(ms => setBusinesses(Array.from(new Set(ms.map(m => m.name))).sort((a, b) => a.localeCompare(b))))
      .catch(() => {});
  }, []);
  useEffect(() => { setData(null); reload(); }, [reload]);
  usePoll(reload, 30000);

  const selector = (
    <Sel label="Merchant" value={merchant} onChange={e => setMerchant(e.target.value)} style={{ marginBottom: 0, minWidth: 220 }}
      options={[{ value: '', label: 'All Merchants' }, ...businesses.map(b => ({ value: b, label: b }))]} />
  );
  return (
    <ReportsView
      data={data} reload={reload}
      businessName={merchant || 'All Merchants'} generatedBy={user.name}
      subtitle="System-wide financial intelligence — all merchants, transactions and reports."
      merchantSelector={selector} showBusinessFilter={!merchant}
    />
  );
};
