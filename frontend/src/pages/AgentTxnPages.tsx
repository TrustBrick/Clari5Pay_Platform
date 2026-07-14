import React, { useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { fmt } from '../utils/helpers';
import { Card, Btn, Input, Sel, Modal, LoadingScreen } from '../components/UI';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import {
  agentTxnsAPI, agentTxnError,
  type AgentOverview, type AgentFormData, type AgentFormAgent, type AgentDepositBody,
  type AgentWithdrawalBody, type AgentMemberLookup, type AgentTxnRow,
  type AgentTxnAuditRow, type AgentTxnQuery,
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

const IsolationNote: React.FC = () => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 8, background: T.infoBg, color: T.info, fontSize: 11.5, fontWeight: 600, marginBottom: 16 }}>
    <span>ⓘ</span> Isolated module — these figures reflect only Agent Transactions and never affect merchant balances, settlements, treasury, risk or reports.
  </div>
);

const StatusPill: React.FC<{ status: string }> = ({ status }) => {
  const m: Record<string, { c: string; bg: string }> = {
    APPROVED: { c: T.success, bg: T.successBg }, PENDING: { c: T.warning, bg: T.warningBg }, REJECTED: { c: T.danger, bg: T.dangerBg },
  };
  const s = m[status] || { c: T.textMuted, bg: T.borderLight };
  return <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, color: s.c, background: s.bg }}>{status}</span>;
};

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
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Overview</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Summary of the isolated Agent Transaction subsystem.</p>
      </div>
      <IsolationNote />

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
                  <td style={tdS}><StatusPill status={r.status} /></td>
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

// ─── Reusable read-only field (auto-fetched agent details) ─────────────────────
const ReadField: React.FC<{ label: string; value?: string | null }> = ({ label, value }) => (
  <Input label={label} value={value || ''} onChange={() => {}} placeholder="—" readOnly />
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
  const [membershipType, setMembershipType] = useState('');
  const [amount, setAmount] = useState('');
  const [country, setCountry] = useState('');
  const [state, setState] = useState('');
  const [location, setLocation] = useState('');
  const [mobile, setMobile] = useState('');
  const [notes, setNotes] = useState('');
  const [instructions, setInstructions] = useState('');
  const [sendApproval, setSendApproval] = useState(false);
  const [approverId, setApproverId] = useState('');
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
    setCountry(''); setState(''); setLocation(''); setMobile(''); setNotes(''); setInstructions('');
    setSendApproval(false); setApproverId('');
  };

  const submit = async () => {
    if (!agent) { showToast('Select an Agent ID.', 'error'); return; }
    if (!membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    const amt = Number(amount);
    if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
    if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
    if (sendApproval && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    setBusy(true); setResult(null);
    const body: AgentDepositBody = {
      agentMasterId: agent.id, membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: sendApproval,
      approverUserId: sendApproval ? Number(approverId) : undefined,
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
      <IsolationNote />
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
          <Sel label="Select Agent ID" value={agentId} onChange={e => setAgentId(e.target.value)} required
            options={[{ value: '', label: '— Select an agent —' }, ...fd.agents.map(a => ({ value: String(a.id), label: `${a.agentId} — ${a.name}` }))]} />
          <ReadField label="Agent Name" value={agent?.name} />
          <ReadField label="Agent Country" value={agent?.country} />
          <ReadField label="Agent State" value={agent?.state} />
          <ReadField label="Agent Location" value={agent?.location} />
          <ReadField label="Agent Category" value={agent?.category} />

          <Input label="Membership ID" value={membershipId} onChange={e => setMembershipId(e.target.value)} required placeholder="Enter Membership ID" />
          <Input label="Membership Name" value={membershipName} onChange={e => setMembershipName(e.target.value)} placeholder="Manual or auto-fetched" readOnly={memberLocked} hint={memberLocked ? 'Auto-filled from existing membership' : undefined} />
          <Sel label="Membership Type" value={membershipType} onChange={e => setMembershipType(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...fd.membershipTypes.map(t => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))]} />
          <Input label="Transaction Amount" type="number" value={amount} onChange={e => setAmount(e.target.value)} required inputMode="decimal" />

          <Input label="Country" value={country} onChange={e => setCountry(e.target.value)} />
          <Input label="State" value={state} onChange={e => setState(e.target.value)} />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <Input label="Mobile Number" value={mobile} onChange={e => setMobile(e.target.value.replace(/[^\d]/g, ''))} placeholder="Optional" inputMode="numeric" />

          <ReadField label="Token Details" value="Auto-generated on submit" />
          <ReadField label="Unique Note Number" value="System-generated on submit" />
          <Sel label="Instructions" value={instructions} onChange={e => setInstructions(e.target.value)}
            options={[{ value: '', label: '— None —' }, ...fd.instructions.map(i => ({ value: i, label: instrLabel(i) }))]} />
        </div>

        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
          <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Up to 100 characters"
            style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
        </div>

        <div style={{ marginTop: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 13, fontWeight: 700, color: T.textMain }}>
            <input type="checkbox" checked={sendApproval} onChange={e => setSendApproval(e.target.checked)} style={{ width: 16, height: 16, cursor: 'pointer' }} />
            Send To Approval (optional)
          </label>
          {sendApproval && (
            <div style={{ marginTop: 12, maxWidth: 360 }}>
              <Sel label="Authorized Approver" value={approverId} onChange={e => setApproverId(e.target.value)} required
                options={[{ value: '', label: '— Select approver —' }, ...fd.approvers.map(a => ({ value: String(a.id), label: `${a.name} (${a.role})` }))]} />
            </div>
          )}
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
export const AgentWithdrawalRequestPage: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }> = ({ embedded, onSubmitted }) => {
  const { showToast } = useToast();
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [membershipId, setMembershipId] = useState('');
  const [membershipName, setMembershipName] = useState('');
  const [membershipType, setMembershipType] = useState('');
  const [agentId, setAgentId] = useState('');
  const [autoAgent, setAutoAgent] = useState<AgentMemberLookup['latestDeposit']>(null);
  const [manualOverride, setManualOverride] = useState(false);
  const [looking, setLooking] = useState(false);
  const [amount, setAmount] = useState('');
  const [country, setCountry] = useState('');
  const [state, setState] = useState('');
  const [location, setLocation] = useState('');
  const [mobile, setMobile] = useState('');
  const [notes, setNotes] = useState('');
  const [instructions, setInstructions] = useState('');
  const [sendApproval, setSendApproval] = useState(false);
  const [approverId, setApproverId] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AgentTxnRow | null>(null);

  useEffect(() => {
    agentTxnsAPI.formData().then(setFd).catch(() => showToast('Failed to load form data', 'error'));
  }, [showToast]);

  // Membership lookup → auto-fetch the agent from the latest agent DEPOSIT for this membership.
  const lookupMember = async () => {
    const id = membershipId.trim();
    setAutoAgent(null); setManualOverride(false);
    if (!id) return;
    setLooking(true);
    try {
      const r = await agentTxnsAPI.member(id);
      if (r.membershipName) setMembershipName(r.membershipName);
      if (r.latestDeposit) { setAutoAgent(r.latestDeposit); setAgentId(String(r.latestDeposit.agentMasterId)); }
      else { setManualOverride(true); }   // no prior agent deposit → manual selection
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
    setManualOverride(false); setAmount(''); setCountry(''); setState(''); setLocation(''); setMobile('');
    setNotes(''); setInstructions(''); setSendApproval(false); setApproverId('');
  };

  const submit = async () => {
    if (!membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    if (!agentId) { showToast('Select an Agent ID.', 'error'); return; }
    const amt = Number(amount);
    if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
    if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
    if (sendApproval && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    setBusy(true); setResult(null);
    const body: AgentWithdrawalBody = {
      agentMasterId: Number(agentId), membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: sendApproval,
      approverUserId: sendApproval ? Number(approverId) : undefined,
      linkedDepositId: usingAuto ? autoAgent!.depositId : undefined,
    };
    try {
      const row = await agentTxnsAPI.createWithdrawal(body);
      setResult(row);
      showToast(`Agent withdrawal ${row.referenceNumber} created.`, 'success');
      reset();
      onSubmitted?.();
    } catch (e) {
      showToast(agentTxnError(e, 'Failed to create Agent Withdrawal Request.'), 'error');
    } finally { setBusy(false); }
  };

  if (!fd) return <LoadingScreen label="Loading…" />;

  return (
    <div style={embedded ? undefined : { maxWidth: 860 }}>
      {!embedded && (<>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Withdrawal Request</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Record a third-party agent withdrawal in the isolated Agent ledger.</p>
      </div>
      <IsolationNote />
      </>)}

      {result && !embedded && (
        <Card style={{ padding: 16, marginBottom: 18, borderLeft: `4px solid ${T.success}` }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: T.success, marginBottom: 10 }}>✓ Agent Withdrawal Request created</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: '10px 18px' }}>
            {[['Reference Number', result.referenceNumber], ['Transaction Code', result.transactionCode], ['Note Number', result.noteNumber], ['Token Details', result.tokenDetails], ['Status', result.status], ['Created (IST)', `${result.createdDate} ${result.createdTime}`]].map(([k, v]) => (
              <div key={k}><div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{k}</div><div style={{ fontSize: 13, fontWeight: 700, color: T.textMain, wordBreak: 'break-word' }}>{v}</div></div>
            ))}
          </div>
        </Card>
      )}

      <Card style={{ padding: 22 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <Input label="Membership ID" value={membershipId} onChange={e => setMembershipId(e.target.value)} onBlur={lookupMember} required placeholder="Enter Membership ID" hint={looking ? 'Looking up…' : undefined} />
          <Input label="Membership Name" value={membershipName} onChange={e => setMembershipName(e.target.value)} placeholder="Manual or auto-fetched" />
          <Sel label="Membership Type" value={membershipType} onChange={e => setMembershipType(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...fd.membershipTypes.map(t => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))]} />
          <Input label="Transaction Amount" type="number" value={amount} onChange={e => setAmount(e.target.value)} required inputMode="decimal" />
        </div>

        {/* Agent — auto-fetched from the latest deposit for this membership, else manual selection */}
        <div style={{ margin: '4px 0 14px', padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          {usingAuto ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
                <span style={{ fontSize: 11, fontWeight: 700, color: T.success, background: T.successBg, padding: '2px 10px', borderRadius: 20 }}>✓ Auto-fetched</span>
                <span style={{ fontSize: 12, color: T.textMuted }}>Agent from the latest agent deposit {autoAgent!.reference} for this membership (view only).</span>
                <Btn size="sm" variant="ghost" onClick={() => setManualOverride(true)} style={{ marginLeft: 'auto' }}>Select a different agent</Btn>
              </div>
              <ReadField label="Agent ID" value={disp?.code} />
            </>
          ) : (
            <>
              <Sel label="Select Agent ID" value={agentId} onChange={e => setAgentId(e.target.value)} required
                options={[{ value: '', label: '— Select an agent —' }, ...fd.agents.map(a => ({ value: String(a.id), label: `${a.agentId} — ${a.name}` }))]} />
              {autoAgent && (
                <Btn size="sm" variant="ghost" onClick={() => { setManualOverride(false); setAgentId(String(autoAgent.agentMasterId)); }}>↩ Use auto-fetched agent ({autoAgent.agentCode})</Btn>
              )}
            </>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px', marginTop: 12 }}>
            <ReadField label="Agent Name" value={disp?.name} />
            <ReadField label="Agent Country" value={disp?.country} />
            <ReadField label="Agent State" value={disp?.state} />
            <ReadField label="Agent Location" value={disp?.location} />
            <ReadField label="Agent Category" value={disp?.category} />
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <Input label="Country" value={country} onChange={e => setCountry(e.target.value)} />
          <Input label="State" value={state} onChange={e => setState(e.target.value)} />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <Input label="Mobile Number" value={mobile} onChange={e => setMobile(e.target.value.replace(/[^\d]/g, ''))} placeholder="Optional" inputMode="numeric" />
          <ReadField label="Token Details" value="Auto-generated on submit" />
          <ReadField label="Unique Note Number" value="System-generated on submit" />
          <Sel label="Instructions" value={instructions} onChange={e => setInstructions(e.target.value)}
            options={[{ value: '', label: '— None —' }, ...fd.instructions.map(i => ({ value: i, label: instrLabel(i) }))]} />
        </div>

        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
          <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Up to 100 characters"
            style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
        </div>

        <div style={{ marginTop: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 13, fontWeight: 700, color: T.textMain }}>
            <input type="checkbox" checked={sendApproval} onChange={e => setSendApproval(e.target.checked)} style={{ width: 16, height: 16, cursor: 'pointer' }} />
            Send To Approval (optional)
          </label>
          {sendApproval && (
            <div style={{ marginTop: 12, maxWidth: 360 }}>
              <Sel label="Authorized Approver" value={approverId} onChange={e => setApproverId(e.target.value)} required
                options={[{ value: '', label: '— Select approver —' }, ...fd.approvers.map(a => ({ value: String(a.id), label: `${a.name} (${a.role})` }))]} />
            </div>
          )}
        </div>

        <div style={{ marginTop: 18, display: 'flex', gap: 10 }}>
          <Btn onClick={submit} disabled={busy}>{busy ? 'Submitting…' : 'Submit Agent Withdrawal'}</Btn>
          <Btn variant="secondary" onClick={reset} disabled={busy}>Clear</Btn>
        </div>
      </Card>
    </div>
  );
};

// ─── Manage Transaction — correct the amount of a PENDING agent transaction ─────
const ManageModal: React.FC<{ row: AgentTxnRow; fd: AgentFormData | null; canApprove: boolean; onClose: () => void; onRefresh: () => void }> =
  ({ row, fd, canApprove, onClose, onRefresh }) => {
    const { showToast } = useToast();
    const [amount, setAmount] = useState(String(row.amount));
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
      const amt = Number(amount);
      if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
      if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
      if (sendApproval && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
      setBusy(true);
      try {
        const updated = await agentTxnsAPI.manage(row.id, {
          amount: amt, notes: notes || undefined, sentForApproval: sendApproval,
          approverUserId: sendApproval ? Number(approverId) : undefined,
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

        <Input label="Transaction Amount" type="number" value={amount} onChange={e => setAmount(e.target.value)} inputMode="decimal" />
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes <span style={{ color: T.textLight, fontWeight: 600 }}>({notes.length}/100)</span></label>
        <textarea value={notes} maxLength={100} onChange={e => setNotes(e.target.value)} rows={2} placeholder="Reason for the correction (optional)"
          style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical', marginBottom: 12 }} />

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
  const canApprove = ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
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
      const q: AgentTxnQuery = { status: 'PENDING' };
      if (dateF) q.date = dateF; else { if (fromF) q.date_from = fromF; if (toF) q.date_to = toF; }
      let data = await agentTxnsAPI.list(q);
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
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Correct the amount of a pending agent transaction. Agent transactions only — merchant transactions are never affected.</p>
      </div>
      <IsolationNote />

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
          <span style={{ fontSize: 12, color: T.textMuted, alignSelf: 'center' }}>{rows.length} pending</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Type', 'Agent', 'Membership', 'Amount', 'Created (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No pending agent transactions match the search.</td></tr>}
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

      {manageRow && <ManageModal row={manageRow} fd={fd} canApprove={canApprove} onClose={() => setManageRow(null)} onRefresh={search} />}
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

const AgentTxnDetailsModal: React.FC<{ row: AgentTxnRow; onClose: () => void }> = ({ row, onClose }) => {
  const [audit, setAudit] = useState<AgentTxnAuditRow[]>([]);
  useEffect(() => { agentTxnsAPI.audit(row.id).then(setAudit).catch(() => {}); }, [row.id]);

  const fields: Array<[string, React.ReactNode]> = [
    ['Reference Number', row.referenceNumber], ['Transaction Code', row.transactionCode],
    ['Type', row.type], ['Status', <StatusPill status={row.status} />],
    ['Agent', `${row.agentCode || '—'}${row.agentName ? ` · ${row.agentName}` : ''}`],
    ['Agent Country', row.agentCountry], ['Agent State', row.agentState],
    ['Agent Location', row.agentLocation], ['Agent Category', row.agentCategory],
    ['Membership ID', row.membershipId], ['Membership Name', row.membershipName],
    ['Membership Type', row.membershipType], ['Amount', fmt(row.amount)],
    ['Country', row.country], ['State', row.state], ['Location', row.location], ['Mobile', row.mobile],
    ['Token Details', row.tokenDetails], ['Note Number', row.noteNumber],
    ['Instructions', row.instructions ? instrLabel(row.instructions) : null], ['Notes', row.notes],
    ['Sent For Approval', row.sentForApproval ? 'Yes' : 'No'], ['Approver', row.approverName],
    ['Approved By', row.approvedBy], ['Approved (IST)', row.approvedDate ? `${row.approvedDate} ${row.approvedTime || ''}` : null],
    ['Created By', row.createdBy], ['Created (IST)', `${row.createdDate || ''} ${row.createdTime || ''}`],
  ];

  return (
    <Modal title={`${row.type === 'DEPOSIT' ? 'Agent Deposit' : 'Agent Withdrawal'} — ${row.referenceNumber}`} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {fields.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>

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

// ─── Agent Deposit / Withdrawal Management (operator list + create) ─────────────
// Mirrors the Merchant Deposit/Withdrawal Management pages, but reads and writes ONLY the isolated
// Agent Transaction ledger (filtered to this txn_type) — never any merchant module. The "+ Create"
// button opens the existing Agent Request form (reused, embedded), not a new form.
const PAGE_SIZE = 10;

const AgentTxnManagementPage: React.FC<{
  user: User;
  txnType: 'DEPOSIT' | 'WITHDRAWAL';
  title: string;
  noun: string;
  requestLabel: string;
  FormComp: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }>;
}> = ({ user, txnType, title, noun, requestLabel, FormComp }) => {
  const { showToast } = useToast();
  const role = String(user.merchantRole || '').toUpperCase();
  const canManage = ['SUPERVISOR', 'MANAGER', 'DEO'].includes(role);   // amount correction (backend MANAGE_ROLES)
  const canApprove = ['SUPERVISOR', 'MANAGER'].includes(role);
  const [fd, setFd] = useState<AgentFormData | null>(null);
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [detailRow, setDetailRow] = useState<AgentTxnRow | null>(null);
  const [manageRow, setManageRow] = useState<AgentTxnRow | null>(null);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [dateF, setDateF] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);

  useEffect(() => { agentTxnsAPI.formData().then(setFd).catch(() => {}); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q: AgentTxnQuery = { txn_type: txnType };
      if (status) q.status = status;
      if (search.trim()) q.search = search.trim();
      if (dateF) q.date = dateF; else { if (fromF) q.date_from = fromF; if (toF) q.date_to = toF; }
      setRows(await agentTxnsAPI.list(q));
    } catch { showToast(`Failed to load Agent ${noun} requests.`, 'error'); }
    finally { setLoading(false); }
  }, [txnType, status, search, dateF, fromF, toF, noun, showToast]);

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { if (!showForm && !detailRow && !manageRow) load(); });

  const runSearch = () => { setPage(1); load(); };
  const clearFilters = () => { setSearch(''); setStatus(''); setDateF(''); setFromF(''); setToF(''); setPage(1); };

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
      <IsolationNote />

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 12 }}>
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent" style={{ marginBottom: 0 }} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, { value: 'PENDING', label: 'Pending' }, { value: 'APPROVED', label: 'Approved' }, { value: 'REJECTED', label: 'Rejected' }]} />
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
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Agent', 'Membership', 'Amount', 'Status', 'Created (IST)', 'Action'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && pageRows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No Agent {noun} requests match the search.</td></tr>}
              {pageRows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={{ ...tdS, whiteSpace: 'nowrap' }}>
                    <Btn size="sm" variant="ghost" onClick={() => setDetailRow(x)}>View Details</Btn>
                    {canManage && x.status === 'PENDING' && <Btn size="sm" variant="ghost" onClick={() => setManageRow(x)}>Manage</Btn>}
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
      {manageRow && <ManageModal row={manageRow} fd={fd} canApprove={canApprove} onClose={() => setManageRow(null)} onRefresh={load} />}
    </div>
  );
};

export const AgentDepositManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="DEPOSIT" title="Agent Deposit Management" noun="Deposit" requestLabel="Agent Deposit Request" FormComp={AgentDepositRequestPage} />
);

export const AgentWithdrawalManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="WITHDRAWAL" title="Agent Withdrawal Management" noun="Withdrawal" requestLabel="Agent Withdrawal Request" FormComp={AgentWithdrawalRequestPage} />
);

// ─── Agent Settlement Management (Supervisor-only placeholder) ──────────────────
// Phase-1 scaffold: page, routing, permissions and navigation only. Settlement business logic is
// deferred to a future phase. Remains fully isolated from Merchant Settlement.
export const AgentSettlementManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => (
  <div>
    <div style={{ marginBottom: 16 }}>
      <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Settlement Management</h1>
      <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Settle agent balances in the isolated Agent ledger.</p>
    </div>
    <IsolationNote />
    <Card style={{ padding: '48px 24px', textAlign: 'center' }}>
      <div style={{ fontSize: 40, marginBottom: 12 }}>🏦</div>
      <h2 style={{ margin: '0 0 6px', fontSize: 16, fontWeight: 800, color: T.textMain }}>Coming soon</h2>
      <p style={{ margin: '0 auto', maxWidth: 460, fontSize: 13, color: T.textMuted, lineHeight: 1.5 }}>
        Agent Settlement operations will be enabled in an upcoming phase. This module is reserved for
        Supervisors and stays fully isolated from Merchant Settlement.
      </p>
    </Card>
  </div>
);
