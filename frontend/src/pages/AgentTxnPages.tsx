import React, { useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { fmt, formatIndianAmountInput, parseIndianAmount, fileToDataUrl } from '../utils/helpers';
import { Card, Btn, Input, Sel, Modal, LoadingScreen } from '../components/UI';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import {
  agentTxnsAPI, agentTxnError, AGENT_FINAL_STATUSES, AGENT_SETTLEMENT_METHODS,
  type AgentOverview, type AgentFormData, type AgentFormAgent, type AgentDepositBody,
  type AgentWithdrawalBody, type AgentMemberLookup, type AgentTxnRow,
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

const IsolationNote: React.FC = () => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 8, background: T.infoBg, color: T.info, fontSize: 11.5, fontWeight: 600, marginBottom: 16 }}>
    <span>ⓘ</span> Isolated module — these figures reflect only Agent Transactions and never affect merchant balances, settlements, treasury, risk or reports.
  </div>
);

// Workflow statuses — same labels as the merchant deposit workflow. PENDING/APPROVED are legacy
// rows created before the chain existed.
const STATUS_STYLE: Record<string, { c: string; bg: string; label: string }> = {
  ACCOUNT_REQUESTED: { c: T.warning, bg: T.warningBg, label: 'Account Requested' },
  ACCOUNT_SUBMITTED: { c: T.info, bg: T.infoBg, label: 'Account Submitted' },
  SUPERVISOR_REVIEW: { c: '#7c3aed', bg: '#7c3aed18', label: 'Supervisor Review' },
  MANAGER_REVIEW: { c: '#7c3aed', bg: '#7c3aed18', label: 'Manager Review' },
  SLIP_SUBMITTED: { c: T.blue, bg: `${T.blue}18`, label: 'Slip Submitted' },
  DEPOSITED: { c: T.success, bg: T.successBg, label: 'Deposited' },
  COMPLETED: { c: T.success, bg: T.successBg, label: 'Completed' },
  APPROVED: { c: T.success, bg: T.successBg, label: 'Approved' },
  PENDING: { c: T.warning, bg: T.warningBg, label: 'Pending' },
  REJECTED: { c: T.danger, bg: T.dangerBg, label: 'Rejected' },
};

const StatusPill: React.FC<{ status: string }> = ({ status }) => {
  const s = STATUS_STYLE[status] || { c: T.textMuted, bg: T.borderLight, label: status };
  return <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, color: s.c, background: s.bg, whiteSpace: 'nowrap' }}>{s.label}</span>;
};

// Status filter — the chain in workflow order, then the legacy values.
const STATUS_FILTER_OPTIONS = [
  'ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SUPERVISOR_REVIEW', 'SLIP_SUBMITTED',
  'DEPOSITED', 'COMPLETED', 'REJECTED', 'PENDING', 'APPROVED',
].map(v => ({ value: v, label: STATUS_STYLE[v]?.label || v }));

const METHOD_LABEL: Record<string, string> = {
  CASH: 'Cash', UPI: 'UPI', BANK: 'Bank Transfer', IMPS: 'IMPS', NEFT: 'NEFT', RTGS: 'RTGS', CRYPTO: 'Crypto (USDT)',
};
const methodLabel = (v?: string | null) => (v ? METHOD_LABEL[v] || v : '—');
const BANK_LIKE = ['BANK', 'IMPS', 'NEFT', 'RTGS'];

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
  const [tokenDetails, setTokenDetails] = useState('');
  const [noteNumber, setNoteNumber] = useState('');
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
    setTokenDetails(''); setNoteNumber('');
    setTxnMethod(''); setSenderUpiId(''); setSenderAccountHolder(''); setSenderAccountNumber('');
    setSenderIfsc(''); setSenderBankName(''); setSenderBranch('');
    setCountry(''); setState(''); setLocation(''); setMobile(''); setNotes(''); setInstructions('');
    setSendApproval(false); setApproverId('');
  };

  const submit = async () => {
    if (!agent) { showToast('Select an Agent ID.', 'error'); return; }
    if (!membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    const amt = Number(parseIndianAmount(amount));
    if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
    if (!tokenDetails.trim()) { showToast('Enter the Token Details.', 'error'); return; }
    if (!noteNumber.trim()) { showToast('Enter the Unique Note Number.', 'error'); return; }
    if (!txnMethod) { showToast('Select a Transaction Type.', 'error'); return; }
    if (txnMethod === 'UPI' && !senderUpiId.includes('@')) { showToast('Enter a valid Sender UPI ID (name@bank).', 'error'); return; }
    if (BANK_LIKE.includes(txnMethod) && (!senderAccountHolder.trim() || !senderAccountNumber.trim())) {
      showToast('Enter the Sending Account holder and number.', 'error'); return;
    }
    if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
    if (sendApproval && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    setBusy(true); setResult(null);
    const body: AgentDepositBody = {
      agentMasterId: agent.id, membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: sendApproval,
      tokenDetails: tokenDetails.trim(), noteNumber: noteNumber.trim(),
      txnMethod,
      senderUpiId: senderUpiId.trim() || undefined,
      senderAccountHolder: senderAccountHolder.trim() || undefined,
      senderAccountNumber: senderAccountNumber.trim() || undefined,
      senderIfsc: senderIfsc.trim() || undefined,
      senderBankName: senderBankName.trim() || undefined,
      senderBranch: senderBranch.trim() || undefined,
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
          <Input label="Transaction Amount" type="text" value={amount} onChange={e => setAmount(formatIndianAmountInput(e.target.value))} required inputMode="decimal" />

          <Sel label="Transaction Type" value={txnMethod} onChange={e => setTxnMethod(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...(fd.txnMethods || []).map(v => ({ value: v, label: methodLabel(v) }))]} />
          <div />
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
                <Input label="IFSC Code" value={senderIfsc} onChange={e => setSenderIfsc(e.target.value.toUpperCase())} />
                <Input label="Bank Name" value={senderBankName} onChange={e => setSenderBankName(e.target.value)} />
                <Input label="Branch" value={senderBranch} onChange={e => setSenderBranch(e.target.value)} />
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <Input label="Country" value={country} onChange={e => setCountry(e.target.value)} />
          <Input label="State" value={state} onChange={e => setState(e.target.value)} />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <Input label="Mobile Number" value={mobile} onChange={e => setMobile(e.target.value.replace(/[^\d]/g, ''))} placeholder="Optional" inputMode="numeric" />

          {/* Provided by the customer/agent — the operator enters them; nothing is generated. */}
          <Input label="Token Details" value={tokenDetails} onChange={e => setTokenDetails(e.target.value)}
            required placeholder="As provided by the customer" />
          <Input label="Unique Note Number" value={noteNumber} onChange={e => setNoteNumber(e.target.value)}
            required placeholder="As provided by the customer" hint="Must be unique" />
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
  const [agentId, setAgentId] = useState('');
  const [autoAgent, setAutoAgent] = useState<AgentMemberLookup['latestDeposit']>(null);
  const [txnMethod, setTxnMethod] = useState('');
  // Supplied by the customer/agent and typed in by the operator — never generated.
  const [tokenDetails, setTokenDetails] = useState('');
  const [noteNumber, setNoteNumber] = useState('');
  // Payout account — saved accounts auto-fetch with the membership; a single one auto-selects.
  const [savedAccounts, setSavedAccounts] = useState<AgentMemberAccount[]>([]);
  const [payoutAccountId, setPayoutAccountId] = useState('');
  const [addingAccount, setAddingAccount] = useState(false);
  const [payHolder, setPayHolder] = useState('');
  const [payNumber, setPayNumber] = useState('');
  const [payIfsc, setPayIfsc] = useState('');
  const [payBank, setPayBank] = useState('');
  const [payBranch, setPayBranch] = useState('');
  const [payUpi, setPayUpi] = useState('');
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
      // Saved payout accounts for this membership: exactly one → auto-select it; none → the
      // operator enters the details, which are then saved and re-used next time.
      const accts = r.savedAccounts || [];
      setSavedAccounts(accts);
      const preferred = accts.find(a => a.isDefault) || (accts.length === 1 ? accts[0] : undefined);
      setPayoutAccountId(preferred ? String(preferred.id) : '');
      setAddingAccount(accts.length === 0);
    } catch {
      setManualOverride(true);
      setSavedAccounts([]); setPayoutAccountId(''); setAddingAccount(true);
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
    setSavedAccounts([]); setPayoutAccountId(''); setAddingAccount(false); setTxnMethod('');
    setTokenDetails(''); setNoteNumber('');
    setPayHolder(''); setPayNumber(''); setPayIfsc(''); setPayBank(''); setPayBranch(''); setPayUpi('');
  };

  const submit = async () => {
    if (!membershipId.trim()) { showToast('Membership ID is required.', 'error'); return; }
    if (!membershipType) { showToast('Select a Membership Type.', 'error'); return; }
    if (!agentId) { showToast('Select an Agent ID.', 'error'); return; }
    const amt = Number(parseIndianAmount(amount));
    if (!amt || amt <= 0) { showToast('Enter a valid Transaction Amount.', 'error'); return; }
    if (notes.length > 100) { showToast('Notes must be 100 characters or fewer.', 'error'); return; }
    if (sendApproval && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    if (!txnMethod) { showToast('Select a Transaction Type.', 'error'); return; }
    if (!tokenDetails.trim()) { showToast('Enter the Token Details.', 'error'); return; }
    if (!noteNumber.trim()) { showToast('Enter the Unique Note Number.', 'error'); return; }
    // The payout account: an existing saved one, or new details to be saved for re-use.
    if (!addingAccount && !payoutAccountId) { showToast('Select the payout account.', 'error'); return; }
    if (addingAccount && !payNumber.trim() && !payUpi.trim()) {
      showToast('Enter the payout Account Number or UPI ID.', 'error'); return;
    }
    if (addingAccount && payNumber.trim() && !payHolder.trim()) {
      showToast('Enter the payout Account Holder.', 'error'); return;
    }
    setBusy(true); setResult(null);
    const body: AgentWithdrawalBody = {
      agentMasterId: Number(agentId), membershipId: membershipId.trim(),
      membershipName: membershipName.trim() || undefined, membershipType,
      amount: amt, country: country || undefined, state: state || undefined,
      location: location || undefined, mobile: mobile || undefined, notes: notes || undefined,
      instructions: instructions || undefined, sentForApproval: sendApproval,
      approverUserId: sendApproval ? Number(approverId) : undefined,
      txnMethod,
      tokenDetails: tokenDetails.trim(), noteNumber: noteNumber.trim(),
      linkedDepositId: usingAuto ? autoAgent!.depositId : undefined,
      ...(addingAccount ? {
        payoutAccountHolder: payHolder.trim() || undefined,
        payoutAccountNumber: payNumber.trim() || undefined,
        payoutIfsc: payIfsc.trim() || undefined,
        payoutBankName: payBank.trim() || undefined,
        payoutBranch: payBranch.trim() || undefined,
        payoutUpiId: payUpi.trim() || undefined,
        savePayoutAccount: true,
      } : { payoutAccountId: Number(payoutAccountId) }),
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
      <IsolationNote />
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
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <Input label="Membership ID" value={membershipId} onChange={e => setMembershipId(e.target.value)} onBlur={lookupMember} required placeholder="Enter Membership ID" hint={looking ? 'Looking up…' : undefined} />
          <Input label="Membership Name" value={membershipName} onChange={e => setMembershipName(e.target.value)} placeholder="Manual or auto-fetched" />
          <Sel label="Membership Type" value={membershipType} onChange={e => setMembershipType(e.target.value)} required
            options={[{ value: '', label: '— Select —' }, ...fd.membershipTypes.map(t => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))]} />
          <Input label="Transaction Amount" type="text" value={amount} onChange={e => setAmount(formatIndianAmountInput(e.target.value))} required inputMode="decimal" />
          <Sel label="Transaction Type" value={txnMethod} onChange={e => setTxnMethod(e.target.value)} required
            options={[{ value: '', label: '— Select —' },
              ...(isSettlement ? AGENT_SETTLEMENT_METHODS : (fd.txnMethods || [])).map(v => ({ value: v, label: methodLabel(v) }))]} />
          <div />
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

        {/* Payout account — where the member is paid. Saved accounts auto-fetch with the
            membership and a single one auto-selects; new details are saved for re-use. */}
        <div style={{ margin: '4px 0 14px', padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
            <p style={{ margin: 0, fontSize: 11, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Payout Account</p>
            {savedAccounts.length > 0 && (
              <Btn size="sm" variant="ghost" style={{ marginLeft: 'auto' }} onClick={() => setAddingAccount(a => !a)}>
                {addingAccount ? '↩ Use a saved account' : '+ Add Account'}
              </Btn>
            )}
          </div>
          {!addingAccount ? (
            <>
              {savedAccounts.length > 0 && (
                <span style={{ fontSize: 11, fontWeight: 700, color: T.success, background: T.successBg, padding: '2px 10px', borderRadius: 20 }}>
                  ✓ {savedAccounts.length === 1 ? 'Auto-selected the account on file' : `${savedAccounts.length} accounts on file`}
                </span>
              )}
              <Sel label="Saved Account" value={payoutAccountId} onChange={e => setPayoutAccountId(e.target.value)} required
                style={{ marginTop: 10, marginBottom: 0 }}
                options={[{ value: '', label: '— Select an account —' },
                  ...savedAccounts.map(a => ({ value: String(a.id), label: `${a.label || a.accountNumber || a.upiId}${a.isDefault ? ' (default)' : ''}` }))]} />
            </>
          ) : (
            <>
              <p style={{ margin: '0 0 10px', fontSize: 11.5, color: T.textMuted }}>
                {savedAccounts.length === 0
                  ? 'No account on file for this membership — enter it once and it is saved for future withdrawals.'
                  : 'New account details. Saved against this membership; a repeat of an existing account is reused, not duplicated.'}
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
                <Input label="Account Holder" value={payHolder} onChange={e => setPayHolder(e.target.value)} />
                <Input label="Account Number" value={payNumber} onChange={e => setPayNumber(e.target.value)} />
                <Input label="IFSC Code" value={payIfsc} onChange={e => setPayIfsc(e.target.value.toUpperCase())} />
                <Input label="Bank Name" value={payBank} onChange={e => setPayBank(e.target.value)} />
                <Input label="Branch" value={payBranch} onChange={e => setPayBranch(e.target.value)} />
                <Input label="UPI ID" value={payUpi} onChange={e => setPayUpi(e.target.value)} placeholder="Instead of a bank account" />
              </div>
            </>
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
          <Input label="Country" value={country} onChange={e => setCountry(e.target.value)} />
          <Input label="State" value={state} onChange={e => setState(e.target.value)} />
          <Input label="Location" value={location} onChange={e => setLocation(e.target.value)} />
          <Input label="Mobile Number" value={mobile} onChange={e => setMobile(e.target.value.replace(/[^\d]/g, ''))} placeholder="Optional" inputMode="numeric" />
          {/* Provided by the customer/agent — the operator enters them; nothing is generated. */}
          <Input label="Token Details" value={tokenDetails} onChange={e => setTokenDetails(e.target.value)}
            required placeholder="As provided by the customer" />
          <Input label="Unique Note Number" value={noteNumber} onChange={e => setNoteNumber(e.target.value)}
            required placeholder="As provided by the customer" hint="Must be unique" />
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
    // Payment evidence — stored on the transaction and re-read here; never re-uploaded.
    ['UTR Number', row.depositUtr],
    ['Slip Reference', row.slipRef],
    ['Slip By', row.slipSubmittedBy],
    ['Slip At (IST)', row.slipSubmittedDate ? `${row.slipSubmittedDate} ${row.slipSubmittedTime || ''}` : null],
    ['Sent To (Agent A/C)', row.agentAccountRef ? `${row.agentAccountRef} · ${row.agentAccountDetail || ''}` : null],
    ['Paid To', [row.payoutAccountHolder, row.payoutAccountNumber || row.payoutUpiId, row.payoutBankName].filter(Boolean).join(' · ') || null],
    ['Supervisor', row.supervisorName], ['Manager', row.managerName], ['Review Remark', row.reviewRemark],
    ['Deposited By', row.depositedBy],
    ['Deposited (IST)', row.depositedDate ? `${row.depositedDate} ${row.depositedTime || ''}` : null],
    ['Created By', row.createdBy], ['Created (IST)', `${row.createdDate || ''} ${row.createdTime || ''}`],
  ];

  // The uploaded slip and the Mark-Deposit proof, shown from storage every time.
  const images: Array<[string, string]> = [
    ...(row.slipImage ? [['Uploaded Slip', row.slipImage] as [string, string]] : []),
    ...(row.depositProof ? [['Deposit Proof', row.depositProof] as [string, string]] : []),
  ];

  return (
    <Modal title={`${row.type === 'DEPOSIT' ? 'Agent Deposit' : 'Agent Withdrawal'} — ${row.referenceNumber}`} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '10px 18px', marginBottom: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
        {fields.map(([k, v]) => <DField key={k as string} k={k as string} v={v} />)}
      </div>

        {images.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Payment Evidence</div>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {images.map(([label, src]) => (
              <div key={label}>
                <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>{label}</div>
                {src.startsWith('data:application/pdf')
                  ? <a href={src} target="_blank" rel="noreferrer" style={{ fontSize: 12.5, fontWeight: 700, color: T.blue }}>Open {label} (PDF)</a>
                  : <img src={src} alt={label} style={{ maxWidth: 220, maxHeight: 240, objectFit: 'contain', borderRadius: 10, border: `1px solid ${T.border}` }} />}
              </div>
            ))}
          </div>
        </div>
      )}

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
  txnType: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT';
  title: string;
  noun: string;
  requestLabel: string;
  FormComp: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }>;
}> = ({ user, txnType, title, noun, requestLabel, FormComp }) => {
  const { showToast } = useToast();
  const role = String(user.merchantRole || '').toUpperCase();
  const canManage = ['SUPERVISOR', 'MANAGER', 'DEO'].includes(role);   // amount correction (backend MANAGE_ROLES)
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
  const [manageRow, setManageRow] = useState<AgentTxnRow | null>(null);
  // Deposit-chain steps, each driven by the row's current status.
  const [acctRow, setAcctRow] = useState<AgentTxnRow | null>(null);
  const [slipRow, setSlipRow] = useState<AgentTxnRow | null>(null);
  const [depositRow, setDepositRow] = useState<AgentTxnRow | null>(null);
  const [payoutRow, setPayoutRow] = useState<AgentTxnRow | null>(null);
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
  usePoll(() => { if (!showForm && !detailRow && !manageRow) load({ background: true }); });

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
      <IsolationNote />

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
                  <td style={tdS}><StatusPill status={x.status} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
                  <td style={{ ...tdS, whiteSpace: 'nowrap', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <Btn size="sm" variant="ghost" onClick={() => setDetailRow(x)}>View Details</Btn>
                    {/* The deposit chain's next step, offered only to the operator roles that may
                        perform it and only at the status that expects it. */}
                    {isDeposit && canOperate && x.status === 'ACCOUNT_REQUESTED' && <Btn size="sm" onClick={() => setAcctRow(x)}>Submit Account</Btn>}
                    {isDeposit && canOperate && x.status === 'ACCOUNT_SUBMITTED' && <Btn size="sm" onClick={() => setSlipRow(x)}>Pay / Upload Slip</Btn>}
                    {isDeposit && canOperate && x.status === 'SLIP_SUBMITTED' && <Btn size="sm" variant="success" onClick={() => setDepositRow(x)}>Mark Deposit</Btn>}
                    {/* Withdrawal: created ready to pay (ACCOUNT_SUBMITTED) → the Manager reviews the
                        slip afterwards. Settlement: no gate at all, created at SLIP_SUBMITTED. */}
                    {!isDeposit && canPayout && x.status === (txnType === 'SETTLEMENT' ? 'SLIP_SUBMITTED' : 'ACCOUNT_SUBMITTED')
                      && <Btn size="sm" variant="success" onClick={() => setPayoutRow(x)}>Pay / Upload Slip</Btn>}
                    {/* Manage is CASH-only — hidden entirely for every other method. */}
                    {canManage && x.txnMethod === 'CASH' && !AGENT_FINAL_STATUSES.includes(x.status)
                      && <Btn size="sm" variant="ghost" onClick={() => setManageRow(x)}>Manage</Btn>}
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
      {manageRow && <ManageModal row={manageRow} fd={fd} canApprove={canApprove} role={role} onClose={() => setManageRow(null)} onRefresh={load} />}
      {acctRow && <SubmitAccountModal row={acctRow} onClose={() => setAcctRow(null)} onDone={load} />}
      {slipRow && <UploadSlipModal row={slipRow} onClose={() => setSlipRow(null)} onDone={load} />}
      {depositRow && <MarkDepositModal row={depositRow} onClose={() => setDepositRow(null)} onDone={load} />}
      {payoutRow && <UploadSlipModal row={payoutRow} mode="payout" onClose={() => setPayoutRow(null)} onDone={load} />}
    </div>
  );
};

export const AgentDepositManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="DEPOSIT" title="Agent Deposit Management" noun="Deposit" requestLabel="Agent Deposit Request" FormComp={AgentDepositRequestPage} />
);

export const AgentWithdrawalManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="WITHDRAWAL" title="Agent Withdrawal Management" noun="Withdrawal" requestLabel="Agent Withdrawal Request" FormComp={AgentWithdrawalRequestPage} />
);

// ─── Agent Settlement Management (Supervisor-only) ─────────────────────────────
// Mirrors Agent Withdrawal Management with the approval gate removed: the Supervisor raises the
// settlement and pays it themselves. Methods are Cash / Bank Transfer / Crypto. Fully isolated
// from Merchant Settlement — reads and writes only the agent ledger.
const AgentSettlementRequestForm: React.FC<{ user: User; onNavigate?: (p: string) => void; embedded?: boolean; onSubmitted?: () => void }> = (props) => (
  <AgentWithdrawalRequestPage {...props} mode="settlement" />
);

export const AgentSettlementManagementPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = ({ user }) => (
  <AgentTxnManagementPage user={user} txnType="SETTLEMENT" title="Agent Settlement Management"
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

export const AgentTxnReportsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const [ov, setOv] = useState<AgentOverview | null>(null);
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [type, setType] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);

  const loadSummary = useCallback(() => {
    agentTxnsAPI.overview().then(setOv).catch(() => showToast('Failed to load financial summary.', 'error'));
  }, [showToast]);

  // See AgentTxnManagementPage.load — background refreshes must not drive the Search button.
  const loadRows = useCallback(async (opts?: { background?: boolean }) => {
    const background = opts?.background === true;
    if (!background) setLoading(true);
    try {
      const q: AgentTxnQuery = {};
      if (status) q.status = status;
      if (type) q.txn_type = type;
      if (search.trim()) q.search = search.trim();
      if (fromF) q.date_from = fromF;
      if (toF) q.date_to = toF;
      setRows(await agentTxnsAPI.list(q));
    } catch { if (!background) showToast('Failed to load agent transactions.', 'error'); }
    finally { if (!background) setLoading(false); }
  }, [status, type, search, fromF, toF, showToast]);

  useEffect(() => { loadSummary(); loadRows(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { loadSummary(); loadRows({ background: true }); });

  const runSearch = () => { setPage(1); loadRows(); };
  const clearFilters = () => { setSearch(''); setStatus(''); setType(''); setFromF(''); setToF(''); setPage(1); };

  const c = ov?.cards;
  // Financial summary — every figure from the shared overview endpoint (isolated agent ledger).
  const fin: Array<[string, React.ReactNode, string]> = c ? [
    ['Gross Amount (Approved)', fmt(c.grossAmount), T.blue],
    ['Deposit Commission', fmt(c.depositCommission), T.green],
    ['Total Withdrawals (Approved)', fmt(c.approvedWithdrawals), T.danger],
    ['Withdrawal Commission', fmt(c.withdrawalCommission), T.green],
    ['Total Settlements (Approved)', fmt(c.approvedSettlements), '#7c3aed'],
    ['Settlement Commission', fmt(c.settlementCommission), T.green],
    ['Total Commission', fmt(c.totalCommission), T.green],
    ['Net (Approved)', fmt(c.netAmount), '#1d4ed8'],
  ] : [];

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const exportCsv = () => {
    if (rows.length === 0) { showToast('Nothing to export for the current filters.', 'error'); return; }
    downloadAgentCsv(`agent-transactions-${new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })}`,
      ['Reference', 'Type', 'Agent Code', 'Agent Name', 'Membership ID', 'Membership Name', 'Membership Type', 'Amount', 'Status', 'Instructions', 'Created Date (IST)', 'Created Time (IST)'],
      rows.map(r => [r.referenceNumber, r.type, r.agentCode || '', r.agentName || '', r.membershipId, r.membershipName || '', r.membershipType, r.amount, r.status, r.instructions ? instrLabel(r.instructions) : '', r.createdDate || '', r.createdTime || '']));
    showToast(`Exported ${rows.length} agent transaction${rows.length === 1 ? '' : 's'}.`, 'success');
  };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Reports</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Financial summary and detailed ledger for the isolated Agent Transaction subsystem.</p>
      </div>
      <IsolationNote />

      {/* Financial Summary — shared /overview calculation (same as Agent Overview) */}
      <Card style={{ padding: 18, marginBottom: 16 }}>
        <h2 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 800, color: T.textMain }}>Financial Summary (Approved)</h2>
        {!c ? <div style={{ padding: 16, color: T.textMuted, fontSize: 13 }}>Loading…</div> : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 12 }}>
              {fin.map(([label, value, color]) => (
                <div key={label} style={{ padding: '12px 14px', borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}`, borderTop: `3px solid ${color}` }}>
                  <p style={{ margin: '0 0 6px', fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
                  <p style={{ margin: 0, fontSize: 17, fontWeight: 800, color }}>{value}</p>
                </div>
              ))}
            </div>
            <p style={{ margin: '12px 0 0', fontSize: 11.5, color: T.textMuted }}>
              Net (Approved) = Gross Amount − Deposit Commission − Total Withdrawals − Withdrawal Commission − Total Settlements − Settlement Commission. Total Commission = Deposit + Withdrawal + Settlement Commission. Commission uses each agent's Fees %. Mirrors the Merchant available-balance formula.
            </p>
          </>
        )}
      </Card>

      {/* Per-agent breakdown — from the same overview payload */}
      {ov && ov.byAgent.length > 0 && (
        <Card style={{ marginBottom: 16, overflow: 'hidden' }}>
          <div style={{ padding: '12px 16px', borderBottom: `1px solid ${T.border}` }}><h2 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>By Agent</h2></div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead><tr style={{ background: T.canvas }}>{['Agent', 'Deposits', 'Withdrawals', 'Transactions'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
              <tbody>
                {ov.byAgent.map((a, i) => (
                  <tr key={i} style={{ background: i % 2 ? T.canvas : T.surface }}>
                    <td style={{ ...tdS, fontWeight: 700 }}>{a.agentCode || '—'}{a.agentName ? ` · ${a.agentName}` : ''}</td>
                    <td style={{ ...tdS, color: T.success, fontWeight: 700 }}>{fmt(a.deposits)}</td>
                    <td style={{ ...tdS, color: T.danger, fontWeight: 700 }}>{fmt(a.withdrawals)}</td>
                    <td style={tdS}>{a.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Detailed ledger — isolated list endpoint, filterable + exportable */}
      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(170px,1fr))', gap: 12 }}>
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent" style={{ marginBottom: 0 }} />
          <Sel label="Type" value={type} onChange={e => setType(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Types' }, { value: 'DEPOSIT', label: 'Deposit' }, { value: 'WITHDRAWAL', label: 'Withdrawal' }]} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} style={{ marginBottom: 0 }}
            options={[{ value: '', label: 'All Statuses' }, { value: 'PENDING', label: 'Pending' }, { value: 'APPROVED', label: 'Approved' }, { value: 'REJECTED', label: 'Rejected' }]} />
          <Input label="From Date" type="date" value={fromF} onChange={e => setFromF(e.target.value)} style={{ marginBottom: 0 }} />
          <Input label="To Date" type="date" value={toF} onChange={e => setToF(e.target.value)} style={{ marginBottom: 0 }} />
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' }}>
          <Btn size="sm" onClick={runSearch} disabled={loading}>{loading ? 'Searching…' : 'Search'}</Btn>
          <Btn size="sm" variant="ghost" onClick={clearFilters}>Clear</Btn>
          <Btn size="sm" variant="secondary" onClick={exportCsv}>Export CSV</Btn>
          <span style={{ fontSize: 12, color: T.textMuted, marginLeft: 'auto' }}>{rows.length} transaction{rows.length === 1 ? '' : 's'}</span>
        </div>
      </Card>

      <Card style={{ overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>{['Reference', 'Type', 'Agent', 'Membership', 'Amount', 'Status', 'Created (IST)'].map(h => <th key={h} style={thS}>{h}</th>)}</tr></thead>
            <tbody>
              {loading && rows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>Loading…</td></tr>}
              {!loading && pageRows.length === 0 && <tr><td colSpan={7} style={{ ...tdS, textAlign: 'center', color: T.textMuted, padding: 22 }}>No agent transactions match the filters.</td></tr>}
              {pageRows.map((x, i) => (
                <tr key={x.id} style={{ background: i % 2 ? T.canvas : T.surface }}>
                  <td style={{ ...tdS, fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{x.referenceNumber}</td>
                  <td style={tdS}>{x.type}</td>
                  <td style={tdS}>{x.agentCode || '—'}{x.agentName ? ` · ${x.agentName}` : ''}</td>
                  <td style={tdS}>{x.membershipId}{x.membershipName ? ` · ${x.membershipName}` : ''}</td>
                  <td style={{ ...tdS, fontWeight: 700 }}>{fmt(x.amount)}</td>
                  <td style={tdS}><StatusPill status={x.status} /></td>
                  <td style={{ ...tdS, color: T.textMuted, whiteSpace: 'nowrap' }}>{x.createdDate} {x.createdTime}</td>
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
    </div>
  );
};

// ─── Deposit chain — operator steps (mirror the merchant deposit workflow) ─────
// The Data Operator performs every step the Admin performs in the merchant flow. Each modal maps
// 1:1 onto a backend endpoint and only ever touches the isolated agent ledger.

/** Submit Account — tell the payer which AGENT account to send to. Agent accounts only. */
const SubmitAccountModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [accounts, setAccounts] = useState<AgentAccountOption[] | null>(null);
  const [sel, setSel] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    agentTxnsAPI.agentAccounts(row.agentMasterId)
      .then((rows) => {
        setAccounts(rows);
        const preferred = rows.find(a => a.isDefault) || rows[0];
        setSel(preferred ? String(preferred.id) : '');
      })
      .catch(() => { setAccounts([]); showToast('Failed to load agent accounts.', 'error'); });
  }, [row.agentMasterId, showToast]);

  const chosen = accounts?.find(a => String(a.id) === sel);

  const submit = async () => {
    if (!sel) { showToast('Select an agent account to send.', 'error'); return; }
    setBusy(true);
    try {
      await agentTxnsAPI.accountSubmit(row.id, Number(sel));
      showToast(`Account submitted for ${row.referenceNumber}.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Failed to submit the account.'), 'error'); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={`Submit Account — ${row.referenceNumber}`} onClose={onClose}>
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
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
        <Btn onClick={submit} disabled={busy || !sel}>{busy ? 'Submitting…' : 'Submit Account'}</Btn>
      </div>
    </Modal>
  );
};

/** Pay / Upload Slip — evidences the payment and sends the deposit to Supervisor review. */
const UploadSlipModal: React.FC<{ row: AgentTxnRow; mode?: 'deposit' | 'payout'; onClose: () => void; onDone: () => void }> = ({ row, mode = 'deposit', onClose, onDone }) => {
  const { showToast } = useToast();
  const [utr, setUtr] = useState('');
  const [slipRef, setSlipRef] = useState('');
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
    if (!slip && !slipRef.trim()) { showToast('Upload the slip image or enter a reference number.', 'error'); return; }
    setBusy(true);
    try {
      const body = { slipImage: slip || undefined, slipRef: slipRef.trim() || undefined, utr: utr.trim() || undefined };
      // Deposit: slip → Supervisor review. Withdrawal: payout after Manager approval → Completed.
      await (mode === 'payout' ? agentTxnsAPI.payout(row.id, body) : agentTxnsAPI.submitSlip(row.id, body));
      showToast(mode === 'payout'
        ? (row.type === 'WITHDRAWAL'
            ? `${row.referenceNumber} paid — awaiting Manager approval.`
            : `${row.referenceNumber} paid and completed.`)
        : `Slip submitted for ${row.referenceNumber} — awaiting Supervisor approval.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Failed to submit the slip.'), 'error'); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={`Pay / Upload Slip — ${row.referenceNumber}`} onClose={onClose}>
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
      <Input label="Reference Number" value={slipRef} onChange={e => setSlipRef(e.target.value)} placeholder="Payment reference" />
      {mode === 'payout' && (
        <Input label="UTR Number" value={utr} onChange={e => setUtr(e.target.value)} placeholder="Bank UTR (if applicable)" />
      )}
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Slip Image</label>
      <input type="file" accept="image/*,application/pdf" onChange={onFile} style={{ marginBottom: 6, fontSize: 12 }} />
      {slipName && <div style={{ fontSize: 11.5, color: T.textMuted, marginBottom: 6 }}>Attached: {slipName}</div>}
      <p style={{ fontSize: 11.5, color: T.textMuted, margin: '4px 0 14px' }}>Provide the slip image, a reference number, or both.</p>
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Btn variant="secondary" onClick={onClose} disabled={busy}>Cancel</Btn>
        <Btn onClick={submit} disabled={busy}>{busy ? 'Submitting…' : 'Submit Slip'}</Btn>
      </div>
    </Modal>
  );
};

/** Mark Deposit — the merchant workflow's Admin step, performed here by the Data Operator. */
const MarkDepositModal: React.FC<{ row: AgentTxnRow; onClose: () => void; onDone: () => void }> = ({ row, onClose, onDone }) => {
  const { showToast } = useToast();
  const [utr, setUtr] = useState('');
  const [proof, setProof] = useState('');
  const [proofName, setProofName] = useState('');
  const [busy, setBusy] = useState(false);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    if (f.size > 8 * 1024 * 1024) { showToast('File too large. Maximum 8 MB.', 'error'); return; }
    try { setProof(await fileToDataUrl(f)); setProofName(f.name); }
    catch { showToast('Could not read the file.', 'error'); }
  };

  const submit = async () => {
    setBusy(true);
    try {
      await agentTxnsAPI.markDeposit(row.id, { utr: utr.trim() || undefined, proof: proof || undefined });
      showToast(`${row.referenceNumber} marked Deposited.`, 'success');
      onDone(); onClose();
    } catch (e) { showToast(agentTxnError(e, 'Failed to mark the deposit.'), 'error'); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={`Mark Deposit — ${row.referenceNumber}`} onClose={onClose}>
      <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted }}>
        Approved by {row.supervisorName || 'the Supervisor'}. Marking this deposit completes it — the
        status becomes Deposited and the amount counts toward the agent's approved figures.
      </p>
      <Input label="UTR Number (optional)" value={utr} onChange={e => setUtr(e.target.value)} placeholder="Bank UTR / reference" />
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Proof (optional)</label>
      <input type="file" accept="image/*,application/pdf" onChange={onFile} style={{ marginBottom: 6, fontSize: 12 }} />
      {proofName && <div style={{ fontSize: 11.5, color: T.textMuted, marginBottom: 6 }}>Attached: {proofName}</div>}
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 14 }}>
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
    ...(isDep
      ? ([
          ['Sent To', `${row.agentAccountRef || '—'} · ${row.agentAccountDetail || '—'}`],
          ['Slip Reference', row.slipRef],
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
          ['Slip Reference', row.slipRef],
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
      {row.slipImage && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>Submitted Slip</div>
          {row.slipImage.startsWith('data:application/pdf')
            ? <a href={row.slipImage} target="_blank" rel="noreferrer" style={{ fontSize: 12.5, fontWeight: 700, color: T.blue }}>Open slip (PDF)</a>
            : <img src={row.slipImage} alt="Slip" style={{ maxWidth: '100%', maxHeight: 320, borderRadius: 10, border: `1px solid ${T.border}` }} />}
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
  const queue = isManager
    ? { status: 'MANAGER_REVIEW', txn_type: 'WITHDRAWAL', noun: 'Withdrawals' }   // paid, awaiting slip review
    : { status: 'SUPERVISOR_REVIEW', txn_type: 'DEPOSIT', noun: 'Deposits' };

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
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent Approvals</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>
          Agent {queue.noun} awaiting your approval. {isManager ? 'The operator has paid and uploaded the slip; review it and' : 'Approving sends them back to the Data Operator to'}
          {isManager ? ' complete — approving finishes the withdrawal.' : ' mark as Deposited.'}
        </p>
      </div>
      <IsolationNote />

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
// Transaction Type columns. Lists EVERY agent transaction of every type from the isolated ledger —
// merchant transactions are never mixed in, because this only ever calls /api/agent-txns.
const AGENT_TYPE_OPTIONS = [
  { value: '', label: 'All Types' },
  { value: 'DEPOSIT', label: 'Deposit' },
  { value: 'WITHDRAWAL', label: 'Withdrawal' },
  { value: 'SETTLEMENT', label: 'Settlement' },
];

export const AgentAllTransactionsPage: React.FC<{ user: User; onNavigate?: (p: string) => void }> = () => {
  const { showToast } = useToast();
  const [rows, setRows] = useState<AgentTxnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailRow, setDetailRow] = useState<AgentTxnRow | null>(null);
  const [search, setSearch] = useState('');
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');
  const [method, setMethod] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Search / type / status / dates go server-side; the transaction-type (method) refinement is
      // applied client-side, mirroring how the merchant page refines its server-filtered set.
      const q: AgentTxnQuery = {};
      if (type) q.txn_type = type;
      if (status) q.status = status;
      if (search.trim()) q.search = search.trim();
      if (fromF) q.date_from = fromF;
      if (toF) q.date_to = toF;
      setRows(await agentTxnsAPI.list(q));
    } catch { showToast('Failed to load agent transactions.', 'error'); }
    finally { setLoading(false); }
  }, [type, status, search, fromF, toF, showToast]);

  useEffect(() => { load(); }, [load]);
  usePoll(() => { if (!detailRow) load(); });

  const filtered = method ? rows.filter(r => r.txnMethod === method) : rows;
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const clearFilters = () => {
    setSearch(''); setType(''); setStatus(''); setMethod(''); setFromF(''); setToF(''); setPage(1);
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
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>Agent All Transactions</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Every Agent Deposit, Withdrawal and Settlement in the isolated Agent ledger.</p>
      </div>
      <IsolationNote />

      <Card style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(170px,1fr))', gap: 12 }}>
          <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Reference / Membership / Agent" style={{ marginBottom: 0 }} />
          <Sel label="Type" value={type} onChange={e => setType(e.target.value)} style={{ marginBottom: 0 }} options={AGENT_TYPE_OPTIONS} />
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
                  <td style={tdS}><StatusPill status={x.status} /></td>
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
