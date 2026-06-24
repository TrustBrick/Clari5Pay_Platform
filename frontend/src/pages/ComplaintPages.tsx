import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { formatDateTime, memberLabel } from '../utils/helpers';
import { downloadXlsx } from '../utils/xlsx';
import { Card, Btn, Input, Sel, Modal, Skeleton } from '../components/UI';
import { riskAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { exportComplaintPdf, exportComplaintXlsx, exportRiskPdf, exportRiskXlsx } from './RiskPages';
import type { User, Complaint, ComplaintList, ComplaintStatus } from '../types';

const STATUS_META: Record<string, { color: string; bg: string }> = {
  DRAFT: { color: '#6b7280', bg: '#f3f4f6' },
  OPEN: { color: '#0052cc', bg: '#e0ecff' },
  SUBMITTED: { color: '#0052cc', bg: '#e0ecff' },
  UNDER_REVIEW: { color: '#d97706', bg: '#fef3c7' },
  ESCALATED: { color: '#ea580c', bg: '#ffedd5' },
  COMPLAINT_FILED: { color: '#7c3aed', bg: '#ede9fe' },
  CLOSED: { color: '#16a34a', bg: '#dcfce7' },
};
const PRIO_META: Record<string, { color: string; bg: string }> = {
  LOW: { color: '#16a34a', bg: '#dcfce7' }, MEDIUM: { color: '#d97706', bg: '#fef3c7' },
  HIGH: { color: '#ea580c', bg: '#ffedd5' }, CRITICAL: { color: '#dc2626', bg: '#fee2e2' },
};
const WORKFLOW: ComplaintStatus[] = ['OPEN', 'UNDER_REVIEW', 'ESCALATED', 'COMPLAINT_FILED', 'CLOSED'];
const pretty = (s: string) => s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

// SLA: a case should be resolved within this many days; otherwise it is breached.
const SLA_DAYS = 7;
const daysBetween = (a: string, b: string) => Math.max(0, Math.floor((new Date(b).getTime() - new Date(a).getTime()) / 86400000));
const slaInfo = (c: Complaint) => {
  const closed = c.status === 'CLOSED';
  if (!c.createdAt) return { days: 0, breached: false, closed };
  const end = closed && c.closedAt ? c.closedAt : new Date().toISOString();
  const days = daysBetween(c.createdAt, end);
  return { days, breached: !closed && days > SLA_DAYS, closed };
};

const Pill: React.FC<{ text: string; meta: { color: string; bg: string } }> = ({ text, meta }) => (
  <span style={{ padding: '3px 10px', borderRadius: 999, background: meta.bg, color: meta.color, fontWeight: 800, fontSize: 11 }}>{pretty(text)}</span>
);

const th: React.CSSProperties = { textAlign: 'left', padding: '11px 14px', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: `1px solid ${T.border}` };
const td: React.CSSProperties = { padding: '11px 14px', borderBottom: `1px solid ${T.borderLight}`, color: T.textMain, fontSize: 12.5 };

// ── Case detail modal ──
const CaseDetail: React.FC<{ id: number; user: User; onClose: () => void; onChanged: () => void }> = ({ id, user, onClose, onChanged }) => {
  const { showToast } = useToast();
  const [c, setC] = useState<Complaint | null>(null);
  const [note, setNote] = useState('');
  const [resolution, setResolution] = useState('');
  const [assign, setAssign] = useState('');
  const [busy, setBusy] = useState(false);
  const isSA = user.role === 'SUPER_ADMIN';

  const load = () => riskAPI.complaint(id).then(d => { setC(d); setResolution(d.resolutionNotes || ''); setAssign(d.assignedTo || ''); }).catch(() => showToast('Could not load case', 'error'));
  useEffect(() => { load(); }, [id]);

  const patch = async (body: Record<string, unknown>, ok: string) => {
    setBusy(true);
    try { await riskAPI.updateComplaint(id, body); showToast(ok); await load(); onChanged(); }
    catch (e: any) { showToast(e?.response?.data?.detail || 'Update failed', 'error'); }
    finally { setBusy(false); }
  };

  const riskReport = async () => {
    try { const p = await riskAPI.member(c!.memberId); exportRiskPdf(p); }
    catch { showToast('Risk report unavailable', 'error'); }
  };
  const riskReportExcel = async () => {
    try { const p = await riskAPI.member(c!.memberId); exportRiskXlsx(p); }
    catch { showToast('Risk report unavailable', 'error'); }
  };
  const complaintArgs = () => ({
    caseId: c!.caseId, memberId: c!.memberId, memberName: c!.memberName, merchantName: c!.merchantName,
    bank: { accountHolder: c!.accountHolder || null, accountNumber: c!.accountNumber || null, bankName: c!.bankName || null, branch: c!.branch || null, ifsc: c!.ifsc || null, upiId: c!.upiId || null },
    description: c!.description || '', documents: c!.documents || [],
    status: c!.status, priority: c!.priority, riskLevel: c!.riskLevel, timeline: c!.timeline,
  });
  const complaintPdf = () => c && exportComplaintPdf(complaintArgs());
  const complaintExcel = () => c && exportComplaintXlsx(complaintArgs());

  const sec: React.CSSProperties = { margin: '16px 0 8px', fontSize: 12.5, fontWeight: 800, color: T.blue, textTransform: 'uppercase', letterSpacing: '0.04em' };
  const kv = (k: string, v: React.ReactNode) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: `1px solid ${T.borderLight}`, fontSize: 12.5 }}>
      <span style={{ color: T.textMuted }}>{k}</span><span style={{ fontWeight: 700, color: T.textMain, textAlign: 'right' }}>{v}</span>
    </div>
  );

  return (
    <Modal title={`Case ${c?.caseId || ''}`} onClose={onClose} xl>
      {!c ? <div>{[0, 1, 2, 3].map(i => <div key={i} style={{ padding: '8px 0' }}><Skeleton h={20} /></div>)}</div> : (
        <div>
          {/* Always-visible metadata bar */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(140px,1fr))', gap: 10, padding: 14, background: T.canvas, borderRadius: 12, marginBottom: 6 }}>
            {([
              ['Case ID', <b style={{ fontFamily: 'monospace' }}>{c.caseId}</b>],
              ['Created', c.createdAt ? formatDateTime(c.createdAt) : '—'],
              ['Last Updated', c.updatedAt ? formatDateTime(c.updatedAt) : '—'],
              ['Status', <Pill text={c.status} meta={STATUS_META[c.status] || STATUS_META.OPEN} />],
              ['Priority', <Pill text={c.priority} meta={PRIO_META[c.priority] || PRIO_META.MEDIUM} />],
              ['Assigned Investigator', c.assignedTo || '— Unassigned'],
            ] as const).map(([k, v], i) => (
              <div key={i}>
                <p style={{ margin: 0, fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{k}</p>
                <p style={{ margin: '3px 0 0', fontSize: 12.5, color: T.textMain }}>{v}</p>
              </div>
            ))}
          </div>

          {/* Workflow tracker with stage timestamps + actor */}
          {(() => {
            const tl = c.timeline;
            const stamps: Record<string, { at: string | null; by: string | null | undefined }> = {
              OPEN: { at: tl?.openedAt ?? null, by: tl?.openedBy }, UNDER_REVIEW: { at: tl?.underReviewAt ?? null, by: tl?.underReviewBy },
              ESCALATED: { at: tl?.escalatedAt ?? null, by: tl?.escalatedBy }, COMPLAINT_FILED: { at: tl?.complaintFiledAt ?? null, by: tl?.complaintFiledBy },
              CLOSED: { at: tl?.closedAt ?? null, by: tl?.closedBy },
            };
            const curIdx = WORKFLOW.indexOf((c.status === 'SUBMITTED' ? 'OPEN' : c.status) as ComplaintStatus);
            return (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '12px 0' }}>
                {WORKFLOW.map((s, i) => {
                  const active = curIdx >= i;
                  const { at, by } = stamps[s];
                  return (
                    <div key={s} style={{ flex: 1, minWidth: 110, textAlign: 'center', padding: '8px 4px', borderRadius: 8, background: active ? (STATUS_META[s]?.bg) : T.canvas }}>
                      <p style={{ margin: 0, fontSize: 10.5, fontWeight: 700, color: active ? STATUS_META[s]?.color : T.textMuted }}>{pretty(s)}</p>
                      <p style={{ margin: '3px 0 0', fontSize: 9.5, color: T.textMuted }}>{at ? formatDateTime(at) : '—'}</p>
                      {by && <p style={{ margin: '1px 0 0', fontSize: 9, color: T.textMuted }}>By: {by}</p>}
                    </div>
                  );
                })}
              </div>
            );
          })()}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
            <div>
              <div style={sec}>Membership Information</div>
              {kv('Membership - Member', memberLabel(c.memberId, c.memberName))}{kv('Merchant', c.merchantName)}{kv('Risk Level', c.riskLevel)}
              <div style={sec}>Bank Information</div>
              {kv('Account Holder', c.accountHolder || '—')}{kv('Account Number', c.accountNumber || '—')}{kv('Bank', c.bankName || '—')}{kv('Branch', c.branch || '—')}{kv('IFSC', c.ifsc || '—')}{kv('UPI ID', c.upiId || '—')}
            </div>
            <div>
              <div style={sec}>Uploaded Documents ({(c.documents || []).length})</div>
              {(c.documents || []).length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>None.</p>}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {(c.documents || []).map((d, i) => (
                  <a key={i} href={d.dataUrl} target="_blank" rel="noreferrer" download={d.name}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, padding: '5px 9px', borderRadius: 8, background: T.canvas, border: `1px solid ${T.border}`, color: T.textMain, textDecoration: 'none' }}>
                    {d.kind === 'aadhaar' ? '🪪' : d.kind === 'pan' ? '💳' : '📎'} {d.name}
                  </a>
                ))}
              </div>
              <div style={sec}>Complaint Description</div>
              <p style={{ fontSize: 12.5, color: T.textMain, whiteSpace: 'pre-wrap', margin: 0 }}>{c.description || '—'}</p>
              <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
                <Btn size="sm" variant="secondary" onClick={complaintPdf}>📄 Complaint PDF</Btn>
                <Btn size="sm" variant="secondary" onClick={complaintExcel}>📊 Complaint Excel</Btn>
                <Btn size="sm" variant="secondary" onClick={riskReport}>🛡️ Risk Report PDF</Btn>
                <Btn size="sm" variant="secondary" onClick={riskReportExcel}>📊 Risk Report Excel</Btn>
              </div>
            </div>
          </div>

          {/* Case management controls */}
          <div style={sec}>Case Management</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12, alignItems: 'end' }}>
            <Sel label="Status" value={c.status} style={{ marginBottom: 0 }}
              onChange={e => patch({ status: e.target.value }, 'Status updated')}
              options={WORKFLOW.map(s => ({ value: s, label: pretty(s) }))} />
            <Sel label="Priority" value={c.priority} style={{ marginBottom: 0 }}
              onChange={e => patch({ priority: e.target.value }, 'Priority updated')}
              options={['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map(s => ({ value: s, label: pretty(s) }))} />
            {isSA && (
              <div style={{ display: 'flex', gap: 6, alignItems: 'end' }}>
                <Input label="Assigned To" value={assign} onChange={e => setAssign(e.target.value)} placeholder="Investigator" style={{ marginBottom: 0, flex: 1 }} />
                <Btn size="sm" disabled={busy} onClick={() => patch({ assignedTo: assign }, 'Assigned')}>Save</Btn>
              </div>
            )}
            {!isSA && <div><span style={{ fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase' }}>Assigned To</span><p style={{ margin: '6px 0 0', fontSize: 13 }}>{c.assignedTo || '— (Super Admin assigns)'}</p></div>}
          </div>

          <div style={sec}>Internal Notes ({(c.notes || []).length})</div>
          <div style={{ maxHeight: 160, overflowY: 'auto', marginBottom: 8 }}>
            {(c.notes || []).length === 0 && <p style={{ fontSize: 12, color: T.textMuted }}>No notes yet.</p>}
            {(c.notes || []).map((n, i) => (
              <div key={i} style={{ padding: '8px 10px', background: T.canvas, borderRadius: 8, marginBottom: 6 }}>
                <p style={{ margin: 0, fontSize: 12.5, color: T.textMain }}>{n.text}</p>
                <p style={{ margin: '2px 0 0', fontSize: 10.5, color: T.textMuted }}>{n.author} ({n.role}) · {formatDateTime(n.at)}</p>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Input label="" value={note} onChange={e => setNote(e.target.value)} placeholder="Add an investigation note…" style={{ marginBottom: 0, flex: 1 }} />
            <Btn disabled={busy || !note.trim()} onClick={() => { patch({ note }, 'Note added'); setNote(''); }}>Add Note</Btn>
          </div>

          <div style={sec}>Resolution Notes</div>
          <textarea value={resolution} onChange={e => setResolution(e.target.value)} placeholder="Resolution / outcome…"
            style={{ width: '100%', minHeight: 70, padding: '10px 12px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 13, fontFamily: 'inherit', color: T.textMain, background: T.surface, outline: 'none', resize: 'vertical', boxSizing: 'border-box' }} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 10, flexWrap: 'wrap' }}>
            <Btn variant="secondary" disabled={busy} onClick={() => patch({ resolutionNotes: resolution }, 'Resolution saved')}>Save Resolution</Btn>
            {c.status !== 'CLOSED' && <Btn variant="success" disabled={busy} onClick={() => patch({ status: 'CLOSED', resolutionNotes: resolution }, 'Case closed')}>✓ Close Case</Btn>}
          </div>
        </div>
      )}
    </Modal>
  );
};

// ── Main page ──
export const ComplaintManagementPage: React.FC<{ user: User }> = ({ user }) => {
  const [data, setData] = useState<ComplaintList | null>(null);
  const [statusF, setStatusF] = useState('');
  const [prioF, setPrioF] = useState('');
  const [q, setQ] = useState('');
  const [sel, setSel] = useState<number | null>(null);

  const reload = () => riskAPI.complaints({ status: statusF || undefined, priority: prioF || undefined, q: q || undefined }).then(setData).catch(() => {});
  useEffect(() => { reload(); }, [statusF, prioF, q]);
  usePoll(reload, 30000);

  const counts: Record<string, number> = {};
  (data?.complaints || []).forEach(c => { counts[c.status] = (counts[c.status] || 0) + 1; });
  const total = data?.complaints.length || 0;
  const criticalCount = (data?.complaints || []).filter(c => c.priority === 'CRITICAL').length;

  const exportComplaintsExcel = () => {
    const rows = data?.complaints || [];
    downloadXlsx(`clari5pay-complaints-${new Date().toISOString().slice(0, 10)}.xlsx`, [{
      name: 'Complaints',
      columns: [
        { header: 'Case ID', get: (c: any) => c.caseId },
        { header: 'Membership Number', get: (c: any) => c.memberId || '' },
        { header: 'Member Name', get: (c: any) => c.memberName || '' },
        { header: 'Merchant Name', get: (c: any) => c.merchantName || '' },
        { header: 'Risk Level', get: (c: any) => c.riskLevel || '' },
        { header: 'Priority', get: (c: any) => c.priority || '' },
        { header: 'Status', get: (c: any) => pretty(c.status || '') },
        { header: 'Complaint Date', get: (c: any) => c.createdAt ? formatDateTime(c.createdAt) : '', width: 20 },
        { header: 'Days Open', get: (c: any) => slaInfo(c).days },
        { header: 'Assigned To', get: (c: any) => c.assignedTo || '' },
      ],
      rows,
    }]);
  };
  const metrics: Array<[string, number, string]> = [
    ['Total Complaints', total, T.blue],
    ['Open', counts.OPEN || 0, STATUS_META.OPEN.color],
    ['Escalated', counts.ESCALATED || 0, STATUS_META.ESCALATED.color],
    ['Closed', counts.CLOSED || 0, STATUS_META.CLOSED.color],
    ['Critical Priority', criticalCount, PRIO_META.CRITICAL.color],
  ];

  return (
    <div>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: T.textMuted }}>
        Track and action cyber crime complaints — {user.role === 'SUPER_ADMIN' ? 'all merchants platform-wide' : 'merchants you created'}.
      </p>

      {/* Quick metrics */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: 12, marginBottom: 14 }}>
        {metrics.map(([label, value, color]) => (
          <Card key={label} className="c5-hover-lift" style={{ padding: 14, borderLeft: `3px solid ${color}` }}>
            <p style={{ margin: 0, fontSize: 24, fontWeight: 800, color }}>{value}</p>
            <p style={{ margin: '2px 0 0', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase' }}>{label}</p>
          </Card>
        ))}
      </div>

      {/* Status filter cards */}
      <p style={{ margin: '0 0 8px', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Filter by status</p>
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: 12, marginBottom: 16 }}>
        {['OPEN', 'UNDER_REVIEW', 'ESCALATED', 'COMPLAINT_FILED', 'CLOSED'].map(s => (
          <Card key={s} className="c5-hover-lift" style={{ padding: 14, cursor: 'pointer', borderTop: `3px solid ${STATUS_META[s].color}` }} onClick={() => setStatusF(statusF === s ? '' : s)}>
            <p style={{ margin: 0, fontSize: 22, fontWeight: 800, color: STATUS_META[s].color }}>{counts[s] || 0}</p>
            <p style={{ margin: '2px 0 0', fontSize: 11, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase' }}>{pretty(s)}</p>
          </Card>
        ))}
      </div>

      <Card style={{ padding: 14, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Search" value={q} onChange={e => setQ(e.target.value)} placeholder="Case ID / membership / member" icon="🔍" style={{ marginBottom: 0 }} />
          <Sel label="Status" value={statusF} onChange={e => setStatusF(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, ...(data?.statuses || []).map(s => ({ value: s, label: pretty(s) }))]} />
          <Sel label="Priority" value={prioF} onChange={e => setPrioF(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Priorities' }, ...(data?.priorities || []).map(s => ({ value: s, label: pretty(s) }))]} />
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <Btn size="sm" variant="secondary" disabled={!total} onClick={exportComplaintsExcel}>📊 Download Excel</Btn>
          <span style={{ fontSize: 12, color: T.textMuted }}>{total} complaint(s)</span>
        </div>
      </Card>

      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: T.canvas }}>
                {['Case ID', 'Membership - Member', 'Merchant', 'Risk', 'Priority', 'Complaint Date', 'Days Open / SLA', 'Status', 'Assigned To', 'Action'].map(h => <th key={h} style={th}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {!data && [0, 1, 2].map(i => <tr key={i}><td style={td} colSpan={10}><Skeleton h={18} /></td></tr>)}
              {data && data.complaints.length === 0 && <tr><td style={{ ...td, textAlign: 'center', color: T.textMuted }} colSpan={10}>No complaints found.</td></tr>}
              {data?.complaints.map(c => (
                <tr key={c.id} className="c5-row-hover">
                  <td style={{ ...td, fontFamily: 'monospace', fontWeight: 700 }}>{c.caseId}</td>
                  <td style={{ ...td, fontWeight: 600 }}>{memberLabel(c.memberId, c.memberName)}</td>
                  <td style={{ ...td, color: T.textMuted }}>{c.merchantName}</td>
                  <td style={td}>{c.riskLevel}</td>
                  <td style={td}><Pill text={c.priority} meta={PRIO_META[c.priority] || PRIO_META.MEDIUM} /></td>
                  <td style={{ ...td, whiteSpace: 'nowrap' }}>{c.createdAt ? formatDateTime(c.createdAt) : '—'}</td>
                  <td style={{ ...td, whiteSpace: 'nowrap' }}>{(() => { const s = slaInfo(c); return <span>{s.days}d {s.breached ? <span style={{ color: '#dc2626', fontWeight: 800, fontSize: 10.5 }}>· SLA BREACHED</span> : s.closed ? <span style={{ color: '#16a34a', fontSize: 10.5 }}>· closed</span> : <span style={{ color: '#16a34a', fontSize: 10.5 }}>· on track</span>}</span>; })()}</td>
                  <td style={td}><Pill text={c.status} meta={STATUS_META[c.status] || STATUS_META.OPEN} /></td>
                  <td style={{ ...td, color: T.textMuted }}>{c.assignedTo || '—'}</td>
                  <td style={td}><Btn size="sm" variant="secondary" onClick={() => setSel(c.id)}>View</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {sel !== null && <CaseDetail id={sel} user={user} onClose={() => setSel(null)} onChanged={reload} />}
    </div>
  );
};
