import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, downloadText, today } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, Modal, CountUp, Skeleton } from '../components/UI';
import { transactionAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import {
  ResponsiveContainer, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip as RcTooltip,
} from 'recharts';
import type { User, ReportData, ReportRow, ReportMemberRow } from '../types';

const RTYPE_LABEL: Record<string, string> = { deposit: 'Deposit', withdrawal: 'Withdrawal', settlement: 'Settlement' };
const prettyStatusR = (s: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

const csvEscape = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
const exportRowsCsv = (rows: ReportRow[], filename: string) => {
  const head = ['Reference No.', 'Membership Number', 'Member Name', 'Type', 'Amount', 'Status', 'Date', 'Time'];
  const lines = [head.map(csvEscape).join(',')];
  for (const r of rows) {
    lines.push([r.ref, r.memberId || '', r.member, RTYPE_LABEL[r.type || ''] || '', r.amount, prettyStatusR(r.status), r.date, r.time].map(csvEscape).join(','));
  }
  downloadText(lines.join('\r\n'), filename);
};

// Management-style PDF (print-to-PDF, no extra deps) of the report summary.
function exportReportPdf(data: ReportData, businessName: string) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups for this site to export the PDF.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const c = data.cards;
  const card = (l: string, v: string) => `<div class="kpi"><div class="kl">${l}</div><div class="kv">${v}</div></div>`;
  const memRows = (rows: ReportMemberRow[], key: keyof ReportMemberRow, money: boolean) => rows.map((m, i) =>
    `<tr class="${i % 2 ? 'alt' : ''}"><td>${i + 1}</td><td class="mono">${m.memberId}</td><td>${m.memberName}</td><td class="amt">${money ? fmt(Number(m[key] || 0)) : (m[key] ?? 0)}</td></tr>`).join('');
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
    <table><thead><tr><th>Rank</th><th>Membership</th><th>Member</th><th style="text-align:right">Transactions</th></tr></thead>
      <tbody>${memRows(data.memberAnalytics.mostActive, 'count', false) || '<tr><td colspan=4>No data</td></tr>'}</tbody></table>
    <h2>Top Members by Overall Volume</h2>
    <table><thead><tr><th>Rank</th><th>Membership</th><th>Member</th><th style="text-align:right">Total Volume</th></tr></thead>
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
            <th style={thR}>Membership No.</th>
            <th style={thR}>Member Name</th>
            <th style={{ ...thR, textAlign: 'right' }}>{valueLabel}</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={showRank ? 4 : 3} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>No data yet.</td></tr>}
          {rows.map((m, i) => (
            <tr key={m.memberId} style={{ cursor: 'pointer' }} onClick={() => onPick(m.memberId)}>
              {showRank && <td style={{ ...tdR, fontWeight: 800, color: T.blue }}>{m.rank ?? i + 1}</td>}
              <td style={{ ...tdR, fontFamily: 'monospace', fontWeight: 700 }}>{m.memberId}</td>
              <td style={tdR}>{m.memberName}</td>
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
            {['Reference', 'Membership', 'Member', 'Type', 'Amount', 'Status', 'Date & Time'].map(h => <th key={h} style={thR}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={7} style={{ ...tdR, textAlign: 'center', color: T.textMuted }}>{empty}</td></tr>}
          {rows.slice(0, 500).map(r => (
            <tr key={r.ref} style={{ cursor: onPick ? 'pointer' : 'default' }} onClick={() => onPick && r.memberId && onPick(r.memberId)}>
              <td style={{ ...tdR, fontFamily: 'monospace' }}>{r.ref}</td>
              <td style={{ ...tdR, fontFamily: 'monospace' }}>{r.memberId || '-'}</td>
              <td style={tdR}>{r.member}</td>
              <td style={tdR}>{RTYPE_LABEL[r.type || ''] || '-'}</td>
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
      <StatCard icon="⭐" label="Most Active Member" value={c.mostActiveMember ? c.mostActiveMember.memberName : '-'} sub={c.mostActiveMember ? `${c.mostActiveMember.memberId} · ${c.mostActiveMember.count} txns` : undefined} color={T.success} />
      <StatCard icon="🔝" label="Largest Transaction Today" value={c.largestTransactionToday ? fmt(c.largestTransactionToday.amount) : '-'} sub={c.largestTransactionToday ? `${c.largestTransactionToday.memberName} (${c.largestTransactionToday.memberId})` : undefined} color={T.warning} />
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
          <p style={{ margin: 0, fontSize: 13, color: T.textMain }}>{x.memberName} <span style={{ color: T.textMuted }}>({x.memberId})</span></p>
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

// ── Trends tab ──
const TrendsTab: React.FC<{ data: ReportData }> = ({ data }) => {
  const t = data.trends;
  const merged = t.deposits.map((d, idx) => ({
    date: d.date.slice(5),
    Deposits: d.amount,
    Withdrawals: t.withdrawals[idx]?.amount ?? 0,
    Settlements: t.settlements[idx]?.amount ?? 0,
  }));
  const growth = t.membershipGrowth.map(g => ({ date: g.date.slice(5), New: g.count }));
  const axis = { fontSize: 11, fill: T.textMuted };

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
        <CartesianGrid strokeDasharray="3 3" stroke={T.borderLight} />
        <XAxis dataKey="date" tick={axis} interval={4} /><YAxis tick={axis} width={54} />
        <RcTooltip formatter={(v) => fmt(Number(v))} />
        <Area type="monotone" dataKey={key} stroke={color} strokeWidth={2} fill={`url(#g-${key})`} />
      </AreaChart>
    </ResponsiveContainer>
  );

  return (
    <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(320px,1fr))', gap: 14 }}>
      {chartCard('Deposits Trend (30 days)', area('Deposits', T.success))}
      {chartCard('Withdrawals Trend (30 days)', area('Withdrawals', T.danger))}
      {chartCard('Settlements Trend (30 days)', area('Settlements', T.warning))}
      {chartCard('Membership Growth (new active members/day)', (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={growth} margin={{ top: 6, right: 10, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={T.borderLight} />
            <XAxis dataKey="date" tick={axis} interval={4} /><YAxis tick={axis} width={32} allowDecimals={false} />
            <RcTooltip /><Bar dataKey="New" fill={T.blue} radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ))}
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
          <Btn size="sm" variant="secondary" onClick={() => exportRowsCsv(rows, `clari5pay-search-${today()}.csv`)}>⬇ Export results (CSV)</Btn>
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
export const ReportsPage: React.FC<{ user: User }> = ({ user }) => {
  const [data, setData] = useState<ReportData | null>(null);
  const [tab, setTab] = useState<RTab>('overview');
  const [profileId, setProfileId] = useState<string | null>(null);
  const toast = useToast();

  const reload = () => transactionAPI.reports().then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  usePoll(reload, 30000);

  if (!data) return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
        {[0, 1, 2, 3, 4, 5].map(i => <Skeleton key={i} w={120} h={36} style={{ borderRadius: 10 }} />)}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 14 }}>
        {[0, 1, 2, 3, 4, 5, 6, 7].map(i => (
          <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={120} h={24} /></Card>
        ))}
      </div>
    </div>
  );

  const tabBtn = (k: RTab, label: string) => (
    <button key={k} className="c5-btn" onClick={() => setTab(k)} style={{
      padding: '9px 16px', borderRadius: 10, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 700,
      background: tab === k ? T.blue : T.canvas, color: tab === k ? '#fff' : T.textMuted, fontFamily: 'inherit',
    }}>{label}</button>
  );

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', marginBottom: 16 }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Analytics for <b style={{ color: T.textMain }}>{user.name}</b> — your memberships, transactions and reports only.</p>
        </div>
        <Btn size="sm" variant="secondary" onClick={() => { exportRowsCsv(data.transactions, `clari5pay-report-${today()}.csv`); toast.showToast('CSV downloaded'); }}>⬇ Export CSV</Btn>
        <Btn size="sm" variant="secondary" onClick={() => exportReportPdf(data, user.name)}>⬇ Export PDF</Btn>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 18 }}>
        {tabBtn('overview', '📊 Overview')}
        {tabBtn('quick', '⚡ Quick Reports')}
        {tabBtn('members', '👥 Membership Analytics')}
        {tabBtn('intel', '🧠 Transaction Intelligence')}
        {tabBtn('trends', '📈 Trends')}
        {tabBtn('search', '🔍 Search')}
      </div>

      <div key={tab} className="c5-panel-in">
        {tab === 'overview' && <OverviewTab data={data} />}
        {tab === 'quick' && <QuickReportsTab data={data} />}
        {tab === 'members' && <MembersTab data={data} onPick={setProfileId} />}
        {tab === 'intel' && <IntelTab data={data} />}
        {tab === 'trends' && <TrendsTab data={data} />}
        {tab === 'search' && <SearchTab data={data} onPick={setProfileId} />}
      </div>

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
      <div style={{ height: 24 }} />
    </div>
  );
};
