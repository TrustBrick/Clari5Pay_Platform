import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, fileToDataUrl } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, Modal, Skeleton, CountUp } from '../components/UI';
import { riskAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import type { User, RiskOverview, RiskMember, RiskProfile, RiskLevelStr, BankDetail } from '../types';

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

// Risk recommendations — shared by the on-screen Risk Profile and the PDF.
export const RISK_RECS: Record<string, string[]> = {
  LOW: ['Continue normal monitoring.', 'Maintain current verification standards.'],
  MEDIUM: ['Increase transaction monitoring.', 'Review funding sources periodically.'],
  HIGH: ['Perform enhanced due diligence.', 'Verify source of funds.', 'Monitor withdrawals closely.'],
  CRITICAL: ['Immediate review required.', 'Consider account restrictions.', 'Escalate to Compliance Team.', 'Evaluate Cyber Crime Complaint initiation.'],
};
export function riskRecs(level: string, hasRelated: boolean): string[] {
  return (RISK_RECS[level] || RISK_RECS.LOW).concat(hasRelated ? ['Investigate linked memberships for potential account farming.'] : []);
}

// ── Investigation-report PDF (print-to-PDF, no deps) ──
export function exportRiskPdf(p: RiskProfile, generatedBy?: string) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups to download the report.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const pr = p.profile;
  const m = RISK_META[pr.riskLevel];
  const li = (a: string[]) => a.map(x => `<li>${x}</li>`).join('') || '<li>None noted.</li>';
  const stat = (t: string, s: RiskProfile['txnIntel']['deposits']) =>
    `<tr><td>${t}</td><td class="amt">${fmt(s.total)}</td><td class="amt">${fmt(s.largest)}</td><td class="amt">${fmt(s.average)}</td><td>${s.count}</td></tr>`;
  const rel = p.relationships;
  const recs = riskRecs(pr.riskLevel, rel.relatedMemberships.length > 0);
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
  footer{margin-top:16px;font-size:9.5px;color:#9ca3af;text-align:center}
  .runfoot{position:fixed;bottom:5mm;left:0;right:0;text-align:center;font-size:8px;color:#9ca3af}</style></head><body>
  <div class="runfoot">Clari5Pay · Risk Assessment Report · CONFIDENTIAL · enable "Headers and footers" in the print dialog for page numbers</div>
  <div class="head"><span class="brand"><span class="b">clari</span><span class="g">5</span>pay</span>
    <div class="meta">Risk Assessment Report — CONFIDENTIAL<br>Generated: ${now}<br>Generated By: ${generatedBy || '—'}</div></div>
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
  <h2>Recommendations <span style="font-weight:400;color:#777">(risk level: ${pr.riskLevel})</span></h2>
  <ul>${recs.map(r => `<li>${r}</li>`).join('')}</ul>
  <footer>Clari5Pay Risk Intelligence — confidential. Generated from live platform data. Phase 1: identity/location checks pending KYC data.</footer>
  </body></html>`);
  w.document.close(); w.focus();
  setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

function shareText(p: RiskProfile): string {
  const pr = p.profile;
  return `Clari5Pay Risk Assessment\nMember: ${pr.memberName} (${pr.memberId})\nMerchant: ${pr.merchantName}\nRisk Level: ${pr.riskLevel}\nTotal Volume: ${fmt(pr.totalVolume)}\nIndicators: ${p.summary.indicators.join(', ') || 'none'}`;
}

const ShareMenu: React.FC<{ p: RiskProfile; generatedBy?: string; onClose: () => void }> = ({ p, generatedBy, onClose }) => {
  const txt = encodeURIComponent(shareText(p));
  const native = async () => {
    try {
      if (navigator.share) await navigator.share({ title: 'Clari5Pay Risk Assessment', text: shareText(p) });
      else exportRiskPdf(p, generatedBy);
    } catch { /* cancelled */ }
    onClose();
  };
  const item: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', fontSize: 13, color: T.textMain, textDecoration: 'none', cursor: 'pointer', borderRadius: 8 };
  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 60 }} />
      <div style={{ position: 'absolute', right: 0, bottom: 'calc(100% + 8px)', zIndex: 61, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 12, boxShadow: '0 12px 40px rgba(0,0,0,0.16)', padding: 6, width: 230 }}>
        <div style={item} onClick={() => { exportRiskPdf(p, generatedBy); onClose(); }}>⬇ <span>Download PDF</span></div>
        <a style={item} href={`mailto:?subject=Clari5Pay Risk Assessment&body=${txt}`} onClick={onClose}>✉ <span>Email</span></a>
        <a style={item} href={`https://wa.me/?text=${txt}`} target="_blank" rel="noreferrer" onClick={onClose}>🟢 <span>WhatsApp</span></a>
        <a style={item} href={`https://t.me/share/url?url=clari5pay&text=${txt}`} target="_blank" rel="noreferrer" onClick={onClose}>✈ <span>Telegram</span></a>
        <div style={item} onClick={native}>📱 <span>Device Share…</span></div>
      </div>
    </>
  );
};

// ── Complaint PDF (human-style complaint letter) ──
type LocalDoc = { name: string; type: string; dataUrl: string; kind: 'aadhaar' | 'pan' | 'evidence' };
type PdfDoc = { name: string; type?: string; dataUrl?: string; kind?: string };
type PdfTimeline = { openedAt?: string | null; openedBy?: string | null; underReviewAt?: string | null; underReviewBy?: string | null; escalatedAt?: string | null; escalatedBy?: string | null; complaintFiledAt?: string | null; complaintFiledBy?: string | null; closedAt?: string | null; closedBy?: string | null };
export function exportComplaintPdf(args: { caseId?: string; memberId: string; memberName: string; merchantName: string; bank: BankDetail; description: string; documents: PdfDoc[]; status?: string; priority?: string; riskLevel?: string; timeline?: PdfTimeline }) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups to download the complaint.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const ref = args.caseId || 'DRAFT (unsaved)';
  const b = args.bank;
  const docList = args.documents.map(d => `<li>${d.kind === 'aadhaar' ? 'Aadhaar Card' : d.kind === 'pan' ? 'PAN Card' : 'Evidence'} — ${d.name}</li>`).join('') || '<li>None attached.</li>';
  const fdt = (s?: string | null) => s ? new Date(s).toLocaleString('en-IN') : '—';
  const tl = args.timeline || {};
  const tlRows = ([['Opened', tl.openedAt, tl.openedBy], ['Under Review', tl.underReviewAt, tl.underReviewBy], ['Escalated', tl.escalatedAt, tl.escalatedBy], ['Complaint Filed', tl.complaintFiledAt, tl.complaintFiledBy], ['Closed', tl.closedAt, tl.closedBy]] as const)
    .map(([s, at, by]) => `<tr><td>${s}</td><td>${fdt(at)}</td><td>${by || '—'}</td></tr>`).join('');
  const invSummary = (args.status || args.priority || args.riskLevel)
    ? `<h2>Investigation Summary</h2><div class="row"><b>Risk Level:</b> ${args.riskLevel || '—'}</div><div class="row"><b>Complaint Status:</b> ${(args.status || '—').replace(/_/g, ' ')}</div><div class="row"><b>Priority:</b> ${args.priority || '—'}</div><div class="row"><b>Complaint Reason:</b> ${(args.description || '—').slice(0, 120).replace(/[<>]/g, '')}</div>`
    : '';
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Cyber Crime Complaint — ${args.memberId}</title>
  <style>@page{size:A4;margin:18mm}*{box-sizing:border-box}body{font-family:'Times New Roman',Georgia,serif;color:#1a1a1a;margin:0;line-height:1.6;font-size:13px}
  .head{text-align:center;border-bottom:2px solid #b91c1c;padding-bottom:10px;margin-bottom:14px}
  .head h1{font-size:18px;margin:0;color:#b91c1c;letter-spacing:1px}.head p{margin:2px 0;font-size:11px;color:#555}
  h2{font-size:13px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #ddd;padding-bottom:3px}
  .row{display:flex;margin:3px 0}.row b{width:200px;display:inline-block}
  .body{white-space:pre-wrap;text-align:justify;margin:6px 0}ul{margin:4px 0}
  .sign{margin-top:40px;display:flex;justify-content:space-between}.muted{color:#666;font-size:11px}
  footer{margin-top:24px;border-top:1px solid #ddd;padding-top:8px;font-size:10px;color:#888;text-align:center}
  .runfoot{position:fixed;bottom:6mm;left:0;right:0;text-align:center;font-size:8px;color:#9ca3af}</style></head><body>
  <div class="runfoot">Clari5Pay · Cyber Crime Complaint · CONFIDENTIAL · enable "Headers and footers" in the print dialog for page numbers</div>
  <div class="head"><h1>CYBER CRIME COMPLAINT</h1><p>Filed via Clari5Pay Risk Management · Confidential</p>
    <p><b>Complaint Reference:</b> ${ref}　|　<b>Generated:</b> ${now}</p></div>
  <p>To,<br><b>The Investigating Officer,</b><br>Cyber Crime Cell.</p>
  <p><b>Subject:</b> Complaint regarding suspicious financial activity associated with membership ${args.memberId}.</p>
  <h2>Membership Information</h2>
  <div class="row"><b>Membership Number:</b> ${args.memberId}</div>
  <div class="row"><b>Member Name:</b> ${args.memberName}</div>
  <div class="row"><b>Merchant:</b> ${args.merchantName}</div>
  ${invSummary}
  <h2>Bank / Payment Details</h2>
  <div class="row"><b>Account Holder:</b> ${b.accountHolder || '—'}</div>
  <div class="row"><b>Account Number:</b> ${b.accountNumber || '—'}</div>
  <div class="row"><b>Bank Name:</b> ${b.bankName || '—'}</div>
  <div class="row"><b>Branch:</b> ${b.branch || '—'}</div>
  <div class="row"><b>IFSC Code:</b> ${b.ifsc || '—'}</div>
  <div class="row"><b>UPI ID:</b> ${b.upiId || '—'}</div>
  <h2>Complaint Details</h2>
  <p class="body">${(args.description || 'No description provided.').replace(/[<>]/g, '')}</p>
  <h2>Documents Enclosed</h2>
  <ul>${docList}</ul>
  <h2>Case Timeline</h2>
  <table><thead><tr><th>Stage</th><th>Timestamp</th><th>By</th></tr></thead><tbody>${tlRows}</tbody></table>
  <p style="margin-top:14px">I request you to kindly investigate the above matter and take appropriate action as per law. The information provided above is true to the best of my knowledge.</p>
  <div class="sign"><div class="muted">Place: ______________<br>Date: ${new Date().toLocaleDateString('en-IN')}</div><div class="muted" style="text-align:right">Yours faithfully,<br><br>______________________<br>Complainant Signature</div></div>
  <footer>Generated by Clari5Pay Risk Management. This document is a complaint draft for submission to the relevant authorities.</footer>
  </body></html>`);
  w.document.close(); w.focus();
  setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
}

const EMPTY_BANK: BankDetail = { accountHolder: '', accountNumber: '', bankName: '', branch: '', ifsc: '', upiId: '' };

// ── Cyber Crime Complaint modal ──
const ComplaintModal: React.FC<{ memberId: string; memberName: string; merchantName: string; onClose: () => void }> =
  ({ memberId, memberName, merchantName, onClose }) => {
    const { showToast } = useToast();
    const [accounts, setAccounts] = useState<BankDetail[]>([]);
    const [loadingBanks, setLoadingBanks] = useState(true);
    const [selIdx, setSelIdx] = useState(-1);            // -1 = manual entry
    const [manual, setManual] = useState<BankDetail>(EMPTY_BANK);
    const [saveBank, setSaveBank] = useState(true);
    const [desc, setDesc] = useState('');
    const [docs, setDocs] = useState<LocalDoc[]>([]);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
      riskAPI.memberBanks(memberId)
        .then(r => { setAccounts(r.accounts); if (r.accounts.length) setSelIdx(0); })
        .catch(() => {}).finally(() => setLoadingBanks(false));
    }, [memberId]);

    const chosen: BankDetail = selIdx >= 0 && accounts[selIdx] ? accounts[selIdx] : manual;
    const hasAadhaar = docs.some(d => d.kind === 'aadhaar');
    const hasPan = docs.some(d => d.kind === 'pan');

    const addDoc = async (files: FileList | null, kind: LocalDoc['kind']) => {
      if (!files || !files.length) return;
      const incoming: LocalDoc[] = [];
      for (const f of Array.from(files)) incoming.push({ name: f.name, type: f.type, dataUrl: await fileToDataUrl(f), kind });
      setDocs(prev => {
        // Aadhaar/PAN are single — replace any existing of that kind.
        const base = kind === 'evidence' ? prev : prev.filter(d => d.kind !== kind);
        const next = [...base, ...incoming];
        if (next.length > 10) { showToast('You can attach at most 10 documents', 'error'); return next.slice(0, 10); }
        return next;
      });
    };
    const removeDoc = (i: number) => setDocs(d => d.filter((_, idx) => idx !== i));

    const buildPayload = (submit: boolean) => ({
      memberId, memberName, merchantName, submit, description: desc,
      accountHolder: chosen.accountHolder, accountNumber: chosen.accountNumber, bankName: chosen.bankName,
      branch: chosen.branch, ifsc: chosen.ifsc, upiId: chosen.upiId,
      saveBank: selIdx < 0 && saveBank,
      documents: docs.map(({ name, type, dataUrl }) => ({ name, type, dataUrl })),
    });

    const save = async (submit: boolean) => {
      if (submit) {
        if (!chosen.accountNumber && !chosen.upiId) { showToast('Add a bank account or UPI ID', 'error'); return; }
        if (!hasAadhaar || !hasPan) { showToast('Aadhaar and PAN are required to submit', 'error'); return; }
        if (!desc.trim()) { showToast('Enter a complaint description', 'error'); return; }
      }
      setBusy(true);
      try {
        const c = await riskAPI.createComplaint(buildPayload(submit));
        showToast(submit ? `Complaint ${c.ref} submitted` : `Draft ${c.ref} saved`);
        onClose();
      } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to save complaint', 'error'); }
      finally { setBusy(false); }
    };

    const lbl: React.CSSProperties = { display: 'block', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', margin: '0 0 6px' };
    const fileBtn: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 12px', border: `1.5px dashed ${T.border}`, borderRadius: 10, fontSize: 12.5, cursor: 'pointer', color: T.textMain, background: T.canvas };

    return (
      <Modal title="🚨 Cyber Crime Complaint" onClose={onClose} xl>
        <div style={{ fontSize: 12.5, color: T.textMuted, marginBottom: 6 }}>
          Capture the details, attach documents, and either save a draft or submit. Submission requires bank/UPI details, Aadhaar + PAN, and a description.
        </div>

        <h4 style={{ margin: '14px 0 8px', fontSize: 13, color: T.blue }}>Membership Information</h4>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12 }}>
          <div><span style={lbl}>Membership No.</span><b>{memberId}</b></div>
          <div><span style={lbl}>Member Name</span><b>{memberName}</b></div>
          <div><span style={lbl}>Merchant</span><b>{merchantName}</b></div>
        </div>

        <h4 style={{ margin: '18px 0 8px', fontSize: 13, color: T.blue }}>Bank Details</h4>
        {loadingBanks ? <Skeleton h={40} /> : (
          <div>
            {accounts.map((a, i) => (
              <label key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 12px', border: `1.5px solid ${selIdx === i ? T.blue : T.border}`, borderRadius: 10, marginBottom: 8, cursor: 'pointer' }}>
                <input type="radio" checked={selIdx === i} onChange={() => setSelIdx(i)} />
                <span style={{ fontSize: 13 }}>
                  <b>{a.accountHolder || '—'}</b> · {a.bankName || '—'} · A/C {a.accountNumber}
                  {a.ifsc ? ` · ${a.ifsc}` : ''}{a.upiId ? ` · ${a.upiId}` : ''}
                </span>
              </label>
            ))}
            <label style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 12px', border: `1.5px solid ${selIdx < 0 ? T.blue : T.border}`, borderRadius: 10, marginBottom: 8, cursor: 'pointer' }}>
              <input type="radio" checked={selIdx < 0} onChange={() => setSelIdx(-1)} />
              <span style={{ fontSize: 13, fontWeight: 700 }}>{accounts.length ? 'Add a different account' : 'No account on file — enter details'}</span>
            </label>
            {selIdx < 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 12, padding: '4px 2px 6px' }}>
                <Input label="Account Holder" value={manual.accountHolder || ''} onChange={e => setManual(m => ({ ...m, accountHolder: e.target.value }))} />
                <Input label="Account Number" value={manual.accountNumber || ''} onChange={e => setManual(m => ({ ...m, accountNumber: e.target.value }))} />
                <Input label="Bank Name" value={manual.bankName || ''} onChange={e => setManual(m => ({ ...m, bankName: e.target.value }))} />
                <Input label="Branch" value={manual.branch || ''} onChange={e => setManual(m => ({ ...m, branch: e.target.value }))} />
                <Input label="IFSC Code" value={manual.ifsc || ''} onChange={e => setManual(m => ({ ...m, ifsc: e.target.value }))} />
                <Input label="UPI ID" value={manual.upiId || ''} onChange={e => setManual(m => ({ ...m, upiId: e.target.value }))} />
                <label style={{ gridColumn: '1 / -1', display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5, color: T.textMuted }}>
                  <input type="checkbox" checked={saveBank} onChange={e => setSaveBank(e.target.checked)} /> Save this account against the membership for future use
                </label>
              </div>
            )}
          </div>
        )}

        <h4 style={{ margin: '18px 0 8px', fontSize: 13, color: T.blue }}>Documents <span style={{ fontWeight: 400, color: T.textMuted, fontSize: 11 }}>(max 10 · Aadhaar + PAN required to submit)</span></h4>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
          <label style={{ ...fileBtn, borderColor: hasAadhaar ? T.success : T.border }}>{hasAadhaar ? '✓' : '＋'} Aadhaar Card *<input type="file" accept="image/*,application/pdf" hidden onChange={e => addDoc(e.target.files, 'aadhaar')} /></label>
          <label style={{ ...fileBtn, borderColor: hasPan ? T.success : T.border }}>{hasPan ? '✓' : '＋'} PAN Card *<input type="file" accept="image/*,application/pdf" hidden onChange={e => addDoc(e.target.files, 'pan')} /></label>
          <label style={fileBtn}>＋ Evidence files<input type="file" accept="image/*,application/pdf" hidden multiple onChange={e => addDoc(e.target.files, 'evidence')} /></label>
        </div>
        {docs.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 6 }}>
            {docs.map((d, i) => (
              <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, padding: '4px 8px', borderRadius: 8, background: T.canvas, border: `1px solid ${T.border}` }}>
                {d.kind === 'aadhaar' ? '🪪' : d.kind === 'pan' ? '💳' : '📎'} {d.name}
                <span onClick={() => removeDoc(i)} style={{ cursor: 'pointer', color: T.danger, fontWeight: 800 }}>✕</span>
              </span>
            ))}
          </div>
        )}
        <p style={{ fontSize: 11, color: T.textMuted, margin: 0 }}>{docs.length}/10 files attached.</p>

        <h4 style={{ margin: '18px 0 8px', fontSize: 13, color: T.blue }}>Complaint Description</h4>
        <textarea value={desc} onChange={e => setDesc(e.target.value)} placeholder="Describe the suspicious activity, amounts, dates and any context…"
          style={{ width: '100%', minHeight: 120, padding: '12px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 13.5, fontFamily: 'inherit', color: T.textMain, background: T.surface, outline: 'none', resize: 'vertical', boxSizing: 'border-box' }} />

        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 18, flexWrap: 'wrap' }}>
          <Btn variant="secondary" onClick={() => exportComplaintPdf({ memberId, memberName, merchantName, bank: chosen, description: desc, documents: docs })}>⬇ Download Complaint PDF</Btn>
          <Btn variant="secondary" disabled={busy} onClick={() => save(false)}>{busy ? 'Saving…' : '💾 Save Draft'}</Btn>
          <Btn variant="danger" disabled={busy} onClick={() => save(true)}>{busy ? 'Submitting…' : '🚨 Submit Complaint'}</Btn>
        </div>
      </Modal>
    );
  };

// ── Risk Profile modal ──
const RiskProfileModal: React.FC<{ memberId: string; user: User; onClose: () => void }> = ({ memberId, user, onClose }) => {
  const genBy = `${user.name} (${user.role})`;
  const [p, setP] = useState<RiskProfile | null>(null);
  const [share, setShare] = useState(false);
  const [complaint, setComplaint] = useState(false);
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
          {/* Report header band */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: 16, borderRadius: 12, background: RISK_META[p.profile.riskLevel].bg, flexWrap: 'wrap' }}>
            <div>
              <p style={{ margin: 0, fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Risk Assessment Report</p>
              <p style={{ margin: '4px 0 0', fontSize: 18, fontWeight: 800, color: T.textMain }}>{p.profile.memberName}</p>
              <p style={{ margin: '2px 0 0', fontSize: 12.5, color: T.textMuted }}>{p.profile.memberId} · {p.profile.merchantName}</p>
              <p style={{ margin: '4px 0 0', fontSize: 11, color: T.textMuted }}>Generated {new Date().toLocaleString('en-IN')} · By: {genBy}</p>
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

          <Section title="Recommendations">
            <Card style={{ padding: 14, borderLeft: `4px solid ${RISK_META[p.profile.riskLevel].color}` }}>
              <p style={{ margin: '0 0 8px', fontSize: 11.5, fontWeight: 800, color: RISK_META[p.profile.riskLevel].color }}>FOR {p.profile.riskLevel} RISK</p>
              {riskRecs(p.profile.riskLevel, p.relationships.relatedMemberships.length > 0).map((r, i) => (
                <p key={i} style={{ margin: '5px 0', fontSize: 12.5, color: T.textMain }}>› {r}</p>
              ))}
            </Card>
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
            <Btn variant="secondary" onClick={() => exportRiskPdf(p, genBy)}>⬇ Download PDF</Btn>
            <div style={{ position: 'relative' }}>
              <Btn variant="secondary" onClick={() => setShare(s => !s)}>📤 Share</Btn>
              {share && <ShareMenu p={p} generatedBy={genBy} onClose={() => setShare(false)} />}
            </div>
            <Btn variant="danger" onClick={() => setComplaint(true)}>🚨 Request Cyber Crime Complaint</Btn>
          </div>

          {complaint && (
            <ComplaintModal
              memberId={p.profile.memberId}
              memberName={p.profile.memberName}
              merchantName={p.profile.merchantName}
              onClose={() => setComplaint(false)}
            />
          )}
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

      {sel && <RiskProfileModal memberId={sel} user={user} onClose={() => setSel(null)} />}
    </div>
  );
};
