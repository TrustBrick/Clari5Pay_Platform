import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, Modal, Skeleton, CountUp } from '../components/UI';
import { riskAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import type { User, RiskOverview, RiskMember, RiskProfile, RiskLevelStr } from '../types';

const RISK_META: Record<RiskLevelStr, { dot: string; color: string; bg: string }> = {
  LOW: { dot: '🟢', color: '#16a34a', bg: '#dcfce7' },
  MEDIUM: { dot: '🟡', color: '#d97706', bg: '#fef3c7' },
  HIGH: { dot: '🟠', color: '#ea580c', bg: '#ffedd5' },
  CRITICAL: { dot: '🔴', color: '#dc2626', bg: '#fee2e2' },
};

const RiskBadge: React.FC<{ level: RiskLevelStr }> = ({ level }) => {
  const m = RISK_META[level];
  return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 10px', borderRadius: 999, background: m.bg, color: m.color, fontWeight: 800, fontSize: 11.5 }}>{m.dot} {level}</span>;
};

// 8 business-grouped intelligence categories. Phase-1 status reflects whether we have
// real data to analyse the checks ("analyzed") or it awaits KYC/location data ("pending").
const RISK_CATEGORIES: Array<{ title: string; icon: string; purpose: string; checks: string[]; status: 'analyzed' | 'pending' }> = [
  { title: 'Identity & KYC Intelligence', icon: '🪪', purpose: 'Verifies the member is genuine and not using fake or duplicate identities.', status: 'pending',
    checks: ['Aadhaar Verification', 'PAN Verification', 'Mobile Verification', 'Address Verification', 'Occupation Verification', 'Income Verification', 'Document Verification', 'Duplicate Identity Detection'] },
  { title: 'Customer Profile Intelligence', icon: '👤', purpose: "Determines whether the member's activity matches their profile.", status: 'pending',
    checks: ['Profession Analysis', 'Age Analysis', 'Income Analysis', 'Customer Category', 'Onboarding Information'] },
  { title: 'Transaction Behaviour Intelligence', icon: '📈', purpose: 'Identifies abnormal transaction behaviour.', status: 'analyzed',
    checks: ['Deposit Patterns', 'Withdrawal Patterns', 'Settlement Patterns', 'Transaction Timing', 'Transaction Frequency', 'Transaction Growth'] },
  { title: 'Source of Funds Intelligence', icon: '💸', purpose: 'Identifies suspicious incoming money.', status: 'analyzed',
    checks: ['Source Account Analysis', 'Third-Party Deposits', 'Funding Patterns', 'Sender Behaviour', 'High-Risk Source Detection'] },
  { title: 'Location Intelligence', icon: '📍', purpose: 'Detects geographical anomalies.', status: 'pending',
    checks: ['Residence Location', 'Transaction Locations', 'Address Changes', 'Foreign Activity', 'High-Risk Regions'] },
  { title: 'Membership Intelligence', icon: '🧩', purpose: 'Detects account farming and duplicate identities.', status: 'analyzed',
    checks: ['Multiple Memberships', 'Membership Growth', 'Duplicate Memberships', 'Deposit Distribution'] },
  { title: 'Relationship Intelligence', icon: '🕸️', purpose: 'Detects connected accounts and fraud networks.', status: 'analyzed',
    checks: ['Self Accounts', 'Family Relationships', 'Business Relationships', 'Employee Relationships', 'Friend Relationships', 'Hidden Connections'] },
  { title: 'Historical Intelligence', icon: '🗂️', purpose: 'Uses historical behaviour to identify long-term risk.', status: 'pending',
    checks: ['Transaction History', 'Complaint History', 'Address History', 'Income History', 'Risk History'] },
];

const th: React.CSSProperties = { textAlign: 'left', padding: '11px 14px', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: `1px solid ${T.border}` };
const td: React.CSSProperties = { padding: '11px 14px', borderBottom: `1px solid ${T.borderLight}`, color: T.textMain };

// ── Investigation-report PDF (print-to-PDF, no deps) ──
function exportRiskPdf(p: RiskProfile) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups to download the report.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const pr = p.profile;
  const m = RISK_META[pr.riskLevel];
  const li = (a: string[]) => a.map(x => `<li>${x}</li>`).join('') || '<li>None noted.</li>';
  const stat = (t: string, s: RiskProfile['txnIntel']['deposits']) =>
    `<tr><td>${t}</td><td class="amt">${fmt(s.total)}</td><td class="amt">${fmt(s.largest)}</td><td class="amt">${fmt(s.average)}</td><td>${s.count}</td></tr>`;
  const rel = p.relationships;
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Risk Assessment — ${pr.memberId}</title>
  <style>@page{size:A4;margin:14mm}*{box-sizing:border-box}body{font-family:Arial,'Segoe UI',sans-serif;color:#0a2540;margin:0}
  .head{display:flex;align-items:center;gap:12px;border-bottom:3px solid #0052cc;padding-bottom:10px}
  .brand{font-size:22px;font-weight:800}.brand .b{color:#0052cc}.brand .g{color:#26d00c}
  .meta{margin-left:auto;text-align:right;font-size:11px;color:#4a5568;line-height:1.6}
  h1{font-size:17px;margin:14px 0 2px}.sub{font-size:12px;color:#4a5568;margin:0 0 12px}
  h2{font-size:13px;margin:16px 0 8px;color:#0052cc;border-bottom:1px solid #e2e8f0;padding-bottom:4px}
  .lvl{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:800;background:${m.bg};color:${m.color}}
  table{width:100%;border-collapse:collapse;font-size:11px;margin-top:4px}th{background:#0a2540;color:#fff;text-align:left;padding:6px 8px;font-size:9px;text-transform:uppercase}
  td{padding:5px 8px;border-bottom:1px solid #e2e8f0}.amt{text-align:right}.grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px;font-size:12px}
  .grid div{padding:4px 0;border-bottom:1px solid #f1f5f9}ul{font-size:11.5px;line-height:1.7;margin:4px 0}
  footer{margin-top:16px;font-size:9.5px;color:#9ca3af;text-align:center}</style></head><body>
  <div class="head"><span class="brand"><span class="b">clari</span><span class="g">5</span>pay</span>
    <div class="meta">Risk Assessment Report — CONFIDENTIAL<br>Generated: ${now}</div></div>
  <h1>Member Risk Assessment</h1>
  <p class="sub">Investigation reference: RA-${pr.memberId} · Prepared for ${pr.merchantName}</p>
  <h2>Member Information</h2>
  <div class="grid">
    <div><b>Membership Number:</b> ${pr.memberId}</div><div><b>Member Name:</b> ${pr.memberName}</div>
    <div><b>Merchant:</b> ${pr.merchantName}</div><div><b>Current Risk Level:</b> <span class="lvl">${pr.riskLevel}</span></div>
    <div><b>Registration:</b> ${pr.registrationDate || '—'}</div><div><b>First Transaction:</b> ${pr.firstTransactionDate || '—'}</div>
    <div><b>Last Transaction:</b> ${pr.lastTransactionDate || '—'}</div><div><b>Total Volume:</b> ${fmt(pr.totalVolume)}</div>
  </div>
  <h2>Transaction Summary</h2>
  <table><thead><tr><th>Type</th><th>Total</th><th>Largest</th><th>Average</th><th>Count</th></tr></thead><tbody>
    ${stat('Deposits', p.txnIntel.deposits)}${stat('Withdrawals', p.txnIntel.withdrawals)}${stat('Settlements', p.txnIntel.settlements)}
  </tbody></table>
  <h2>Relationship Analysis</h2>
  <div class="grid">
    <div><b>Linked Bank Accounts:</b> ${rel.linkedAccounts.length}</div><div><b>Linked UPI IDs:</b> ${rel.linkedUpis.length}</div>
    <div><b>Repeated Senders:</b> ${rel.repeatedSenders.length}</div><div><b>Related Memberships:</b> ${rel.relatedMemberships.length}</div>
  </div>
  <h2>Risk Factors / Indicators</h2><ul>${li(p.summary.indicators)}</ul>
  <h2>Strengths</h2><ul>${li(p.summary.strengths)}</ul>
  <h2>Recommendations</h2>
  <ul>
    <li>${pr.riskLevel === 'LOW' ? 'No immediate action required. Continue routine monitoring.' : 'Escalate for manual review and enhanced due diligence.'}</li>
    <li>Complete KYC verification (Aadhaar, PAN, address) to close identity intelligence gaps.</li>
    ${rel.relatedMemberships.length ? '<li>Investigate linked memberships for potential account farming.</li>' : ''}
  </ul>
  <footer>Clari5Pay Risk Intelligence — confidential. Generated from live platform data. Phase 1: identity/location checks pending KYC data.</footer>
  </body></html>`);
  w.document.close(); w.focus();
  setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

function shareText(p: RiskProfile): string {
  const pr = p.profile;
  return `Clari5Pay Risk Assessment\nMember: ${pr.memberName} (${pr.memberId})\nMerchant: ${pr.merchantName}\nRisk Level: ${pr.riskLevel}\nTotal Volume: ${fmt(pr.totalVolume)}\nIndicators: ${p.summary.indicators.join(', ') || 'none'}`;
}

const ShareMenu: React.FC<{ p: RiskProfile; onClose: () => void }> = ({ p, onClose }) => {
  const txt = encodeURIComponent(shareText(p));
  const native = async () => {
    try {
      if (navigator.share) await navigator.share({ title: 'Clari5Pay Risk Assessment', text: shareText(p) });
      else exportRiskPdf(p);
    } catch { /* cancelled */ }
    onClose();
  };
  const item: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', fontSize: 13, color: T.textMain, textDecoration: 'none', cursor: 'pointer', borderRadius: 8 };
  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 60 }} />
      <div style={{ position: 'absolute', right: 0, bottom: 'calc(100% + 8px)', zIndex: 61, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 12, boxShadow: '0 12px 40px rgba(0,0,0,0.16)', padding: 6, width: 230 }}>
        <div style={item} onClick={() => { exportRiskPdf(p); onClose(); }}>⬇ <span>Download PDF</span></div>
        <a style={item} href={`mailto:?subject=Clari5Pay Risk Assessment&body=${txt}`} onClick={onClose}>✉ <span>Email</span></a>
        <a style={item} href={`https://wa.me/?text=${txt}`} target="_blank" rel="noreferrer" onClick={onClose}>🟢 <span>WhatsApp</span></a>
        <a style={item} href={`https://t.me/share/url?url=clari5pay&text=${txt}`} target="_blank" rel="noreferrer" onClick={onClose}>✈ <span>Telegram</span></a>
        <div style={item} onClick={native}>📱 <span>Device Share…</span></div>
      </div>
    </>
  );
};

// ── Risk Profile modal ──
const RiskProfileModal: React.FC<{ memberId: string; onClose: () => void }> = ({ memberId, onClose }) => {
  const [p, setP] = useState<RiskProfile | null>(null);
  const [share, setShare] = useState(false);
  const { showToast } = useToast();
  useEffect(() => { riskAPI.member(memberId).then(setP).catch(() => showToast('Could not load risk report', 'error')); }, [memberId]);

  const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
    <div style={{ marginTop: 18 }}>
      <h4 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.blue, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{title}</h4>
      {children}
    </div>
  );
  const row = (k: string, v: React.ReactNode) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: `1px solid ${T.borderLight}`, fontSize: 13 }}>
      <span style={{ color: T.textMuted }}>{k}</span><span style={{ fontWeight: 700, color: T.textMain }}>{v}</span>
    </div>
  );

  return (
    <Modal title={`Risk Report — ${memberId}`} onClose={onClose} xl>
      {!p ? (
        <div>{[0, 1, 2, 3, 4].map(i => <div key={i} style={{ padding: '8px 0' }}><Skeleton h={20} /></div>)}</div>
      ) : (
        <div>
          {/* Header band */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: 16, borderRadius: 12, background: RISK_META[p.profile.riskLevel].bg, flexWrap: 'wrap' }}>
            <div>
              <p style={{ margin: 0, fontSize: 18, fontWeight: 800, color: T.textMain }}>{p.profile.memberName}</p>
              <p style={{ margin: '2px 0 0', fontSize: 12.5, color: T.textMuted }}>{p.profile.memberId} · {p.profile.merchantName}</p>
            </div>
            <RiskBadge level={p.profile.riskLevel} />
          </div>

          <Section title="Member Profile">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
              {row('Membership Number', p.profile.memberId)}
              {row('Member Name', p.profile.memberName)}
              {row('Merchant Name', p.profile.merchantName)}
              {row('Registration Date', p.profile.registrationDate || '—')}
              {row('First Transaction', p.profile.firstTransactionDate || '—')}
              {row('Last Transaction', p.profile.lastTransactionDate || '—')}
              {row('Total Deposits', fmt(p.profile.totalDeposits))}
              {row('Total Withdrawals', fmt(p.profile.totalWithdrawals))}
              {row('Total Settlements', fmt(p.profile.totalSettlements))}
              {row('Total Volume', fmt(p.profile.totalVolume))}
            </div>
          </Section>

          <Section title="Risk Intelligence Categories">
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(300px,1fr))', gap: 12 }}>
              {RISK_CATEGORIES.map(c => (
                <Card key={c.title} style={{ padding: 14 }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                    <span style={{ fontSize: 13, fontWeight: 800, color: T.textMain }}>{c.icon} {c.title}</span>
                    <span style={{ fontSize: 10, fontWeight: 800, padding: '2px 8px', borderRadius: 999, background: c.status === 'analyzed' ? '#dcfce7' : '#fef3c7', color: c.status === 'analyzed' ? '#16a34a' : '#d97706' }}>
                      {c.status === 'analyzed' ? 'ANALYZED' : 'PENDING DATA'}
                    </span>
                  </div>
                  <p style={{ margin: '0 0 8px', fontSize: 11, color: T.textMuted }}>{c.purpose}</p>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                    {c.checks.map(ch => (
                      <span key={ch} style={{ fontSize: 10.5, padding: '2px 7px', borderRadius: 6, background: T.canvas, color: T.textMuted }}>
                        {c.status === 'analyzed' ? '✓' : '○'} {ch}
                      </span>
                    ))}
                  </div>
                </Card>
              ))}
            </div>
          </Section>

          <Section title="Risk Summary">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
              <Card style={{ padding: 14 }}>
                <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 800, color: '#16a34a' }}>STRENGTHS</p>
                {p.summary.strengths.map((s, i) => <p key={i} style={{ margin: '4px 0', fontSize: 12.5, color: T.textMain }}>✓ {s}</p>)}
              </Card>
              <Card style={{ padding: 14 }}>
                <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 800, color: '#dc2626' }}>RISK INDICATORS</p>
                {p.summary.indicators.length === 0 && <p style={{ margin: '4px 0', fontSize: 12.5, color: T.textMuted }}>None detected.</p>}
                {p.summary.indicators.map((s, i) => <p key={i} style={{ margin: '4px 0', fontSize: 12.5, color: T.textMain }}>⚠ {s}</p>)}
              </Card>
            </div>
          </Section>

          <Section title="Transaction Intelligence">
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 12 }}>
              {([['Deposit', p.txnIntel.deposits, T.success], ['Withdrawal', p.txnIntel.withdrawals, T.danger], ['Settlement', p.txnIntel.settlements, T.warning]] as const).map(([label, s, color]) => (
                <Card key={label} style={{ padding: 14 }}>
                  <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 800, color }}>{label} Statistics</p>
                  {row('Total', fmt(s.total))}
                  {row('Largest', fmt(s.largest))}
                  {row('Average', fmt(s.average))}
                  {row('Count', String(s.count))}
                </Card>
              ))}
            </div>
          </Section>

          <Section title="Relationship Analysis">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
              <Card style={{ padding: 14 }}>
                <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 800, color: T.textMain }}>Linked Bank Accounts ({p.relationships.linkedAccounts.length})</p>
                {p.relationships.linkedAccounts.length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>None.</p>}
                {p.relationships.linkedAccounts.map((a, i) => (
                  <p key={i} style={{ margin: '4px 0', fontSize: 12, color: T.textMain }}>{a.accountHolder || '—'} · {a.bankName || '—'} · A/C {a.accountNumber}</p>
                ))}
                <p style={{ margin: '10px 0 6px', fontSize: 12, fontWeight: 800, color: T.textMain }}>Linked UPI IDs ({p.relationships.linkedUpis.length})</p>
                {p.relationships.linkedUpis.length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>None.</p>}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                  {p.relationships.linkedUpis.map((u, i) => <span key={i} style={{ fontSize: 11, fontFamily: 'monospace', padding: '2px 7px', borderRadius: 6, background: T.canvas }}>{u.upiId}</span>)}
                </div>
              </Card>
              <Card style={{ padding: 14 }}>
                <p style={{ margin: '0 0 8px', fontSize: 12, fontWeight: 800, color: T.textMain }}>Repeated Senders ({p.relationships.repeatedSenders.length})</p>
                {p.relationships.repeatedSenders.length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>None.</p>}
                {p.relationships.repeatedSenders.map((s, i) => <p key={i} style={{ margin: '4px 0', fontSize: 12, color: T.textMain }}>{s.upiId} · {s.count}×</p>)}
                <p style={{ margin: '10px 0 6px', fontSize: 12, fontWeight: 800, color: T.textMain }}>Related Memberships ({p.relationships.relatedMemberships.length})</p>
                {p.relationships.relatedMemberships.length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>None detected.</p>}
                {p.relationships.relatedMemberships.map((r, i) => <p key={i} style={{ margin: '4px 0', fontSize: 12, color: T.danger }}>⚠ {r.memberId} <span style={{ color: T.textMuted }}>(via {r.via})</span></p>)}
              </Card>
            </div>
          </Section>

          {/* Footer actions */}
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 22, flexWrap: 'wrap', position: 'relative' }}>
            <Btn variant="secondary" onClick={() => exportRiskPdf(p)}>⬇ Download PDF</Btn>
            <div style={{ position: 'relative' }}>
              <Btn variant="secondary" onClick={() => setShare(s => !s)}>📤 Share</Btn>
              {share && <ShareMenu p={p} onClose={() => setShare(false)} />}
            </div>
            <span title="Cyber Crime Complaint module — coming in the next phase">
              <Btn variant="danger" disabled>🚨 Request Cyber Crime Complaint</Btn>
            </span>
          </div>
        </div>
      )}
    </Modal>
  );
};

// ── Main page ──
export const RiskManagementPage: React.FC<{ user: User }> = ({ user }) => {
  const [data, setData] = useState<RiskOverview | null>(null);
  const [q, setQ] = useState('');
  const [riskF, setRiskF] = useState('');
  const [merchantF, setMerchantF] = useState('');
  const [sel, setSel] = useState<string | null>(null);

  const reload = () => riskAPI.members().then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  usePoll(reload, 30000);

  if (!data) return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 14, marginBottom: 16 }}>
        {[0, 1, 2, 3].map(i => <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={70} h={26} /></Card>)}
      </div>
      <Card style={{ padding: 18 }}>{[0, 1, 2, 3, 4].map(i => <div key={i} style={{ padding: '8px 0' }}><Skeleton h={20} /></div>)}</Card>
    </div>
  );

  const isSA = data.scope === 'SUPER_ADMIN';
  const isStaff = data.scope !== 'MERCHANT';
  const merchants = Array.from(new Set(data.members.map(m => m.merchantName))).sort();
  const rows = data.members.filter(m =>
    (!q || m.memberId.toLowerCase().includes(q.toLowerCase()) || m.memberName.toLowerCase().includes(q.toLowerCase())) &&
    (!riskF || m.riskLevel === riskF) &&
    (!merchantF || m.merchantName === merchantF)
  );

  return (
    <div>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: T.textMuted }}>
        Centralized member screening & risk intelligence — scoped to {isSA ? 'the whole platform' : isStaff ? 'your merchants' : 'your memberships'}.
        <span style={{ color: T.warning }}> Phase 1: all memberships are LOW risk until the scoring engine is enabled.</span>
      </p>

      {/* Risk dashboard cards */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 14, marginBottom: 18 }}>
        <StatCard icon="🟢" label="Low Risk Members" value={<CountUp value={data.stats.low} />} color="#16a34a" />
        <StatCard icon="🟡" label="Medium Risk" value={<CountUp value={data.stats.medium} />} color="#d97706" />
        <StatCard icon="🟠" label="High Risk" value={<CountUp value={data.stats.high} />} color="#ea580c" />
        <StatCard icon="🔴" label="Critical Risk" value={<CountUp value={data.stats.critical} />} color="#dc2626" />
      </div>

      {/* Filters */}
      <Card style={{ padding: 14, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Search" value={q} onChange={e => setQ(e.target.value)} placeholder="Membership / name" icon="🔍" style={{ marginBottom: 0 }} />
          <Sel label="Risk Level" value={riskF} onChange={e => setRiskF(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Levels' }, ...(['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const).map(v => ({ value: v, label: v }))]} />
          {isStaff && <Sel label="Merchant" value={merchantF} onChange={e => setMerchantF(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Merchants' }, ...merchants.map(m => ({ value: m, label: m }))]} />}
        </div>
      </Card>

      {/* Members table */}
      <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 18 }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                <th style={th}>Risk Level</th>
                <th style={th}>Membership No.</th>
                <th style={th}>Member Name</th>
                {isStaff && <th style={th}>Merchant</th>}
                <th style={th}>Total Transactions</th>
                <th style={th}>Last Activity</th>
                <th style={{ ...th, textAlign: 'right' }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={isStaff ? 7 : 6} style={{ ...td, textAlign: 'center', color: T.textMuted }}>No memberships found.</td></tr>}
              {rows.map(m => (
                <tr key={m.memberId} className="c5-row-hover">
                  <td style={td}><RiskBadge level={m.riskLevel} /></td>
                  <td style={{ ...td, fontFamily: 'monospace', fontWeight: 700 }}>{m.memberId}</td>
                  <td style={td}>{m.memberName}</td>
                  {isStaff && <td style={{ ...td, color: T.textMuted }}>{m.merchantName}</td>}
                  <td style={{ ...td, fontWeight: 700 }}>{m.totalTransactions}</td>
                  <td style={{ ...td, color: T.textMuted, whiteSpace: 'nowrap' }}>{m.lastActivity || '—'}</td>
                  <td style={{ ...td, textAlign: 'right' }}>
                    <Btn size="sm" variant="secondary" onClick={() => setSel(m.memberId)}>View Risk Report</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Super Admin: top risk merchants */}
      {isSA && data.topMerchants && data.topMerchants.length > 0 && (
        <Card style={{ padding: 18 }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 800 }}>Top Risk Merchants</h3>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead><tr style={{ background: T.canvas }}><th style={th}>Merchant</th><th style={th}>Members</th><th style={{ ...th, textAlign: 'right' }}>Volume</th></tr></thead>
              <tbody>
                {data.topMerchants.map(m => (
                  <tr key={m.merchantName}><td style={td}>{m.merchantName}</td><td style={td}>{m.members}</td><td style={{ ...td, textAlign: 'right', fontWeight: 700 }}>{fmt(m.volume)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {sel && <RiskProfileModal memberId={sel} onClose={() => setSel(null)} />}
    </div>
  );
};
