import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { T } from '../utils/theme';
import { Card, Btn, Input, Sel, Modal, TableSkeleton, LoadingScreen, StatCard } from '../components/UI';
import { agentAPI, agentAccountAPI, agentAssignmentAPI, agentDashboardAPI, agentTransactionAPI } from '../services/api';
import { formatDateTimeIST, COUNTRY_CODES, fileToDataUrl, fmt, downloadText } from '../utils/helpers';
import { downloadXlsx } from '../utils/xlsx';
import type { Col } from '../utils/xlsx';
import type { Agent, AgentAccount, AgentAccountType, AgentAssignmentResult, AgentAuditRow, AgentCategory, AgentDashboard, AgentStatus, AgentTxRow, User } from '../types';

// ─── Agent Management (Merchant Portal — Supervisor & Manager only) ──────────────
//
// Phase 2 implements the Agent Master (Agents page). Agents are Non-EPS operational
// entities — they never log in, have no username/password/portal. This page only stores
// agent information; Phase 4 links agents to Deposit/Withdrawal/Settlement transactions.
// The whole module is demo-gated (see nav.ts / App.tsx / backend main.py) until complete.

interface AgentPageProps {
  user: User;
  onNavigate?: (page: string) => void;
}

// Country name options, reused from the shared phone-code list (same source as onboarding).
const COUNTRY_OPTIONS = COUNTRY_CODES
  .map((c) => c.label.split(' ').slice(2).join(' '))
  .filter((n, i, a) => !!n && a.indexOf(n) === i)
  .sort()
  .map((n) => ({ value: n, label: n }));

const CURRENCY_OPTIONS = [
  'INR', 'USD', 'EUR', 'GBP', 'AED', 'SGD', 'AUD', 'CAD', 'JPY', 'CNY',
  'USDT', 'BTC', 'ETH',
].map((c) => ({ value: c, label: c }));

const CATEGORY_OPTIONS: Array<{ value: AgentCategory; label: string }> = [
  { value: 'CASH', label: 'Cash' },
  { value: 'BANK_TRANSFER', label: 'Bank Transfer' },
  { value: 'CRYPTO', label: 'Crypto' },
];
const CATEGORY_LABEL: Record<string, string> = {
  CASH: 'Cash', BANK_TRANSFER: 'Bank Transfer', CRYPTO: 'Crypto',
};
const REFERENCE_OPTIONS = ['', 'Internal Staff', 'Existing Agent', 'Other']
  .map((v) => ({ value: v, label: v || '— None —' }));

const PAGE_SIZE = 10;
const todayISO = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });

// Cross-page hand-off: "Manage Accounts" on the Agents page stashes the chosen agent id here,
// then navigates to the Agent Accounts page (page key `agent-accounts`), which reads & clears it
// on mount to preselect that agent. Both pages live in this module so a simple holder suffices.
let _pendingAgentId: number | null = null;
export const openAgentAccounts = (agentId: number) => { _pendingAgentId = agentId; };

// Account types (Bank / UPI / QR / Crypto). Type drives which fields the form shows.
const ACCOUNT_TYPE_OPTIONS: Array<{ value: AgentAccountType; label: string }> = [
  { value: 'BANK', label: 'Bank Account' },
  { value: 'UPI', label: 'UPI ID' },
  { value: 'QR', label: 'QR Code' },
  { value: 'CRYPTO', label: 'Crypto Wallet' },
];
const ACCOUNT_TYPE_LABEL: Record<string, string> = { BANK: 'Bank Account', UPI: 'UPI ID', QR: 'QR Code', CRYPTO: 'Crypto Wallet' };
const ACCOUNT_TYPE_ICON: Record<string, string> = { BANK: '🏦', UPI: '📱', QR: '▦', CRYPTO: '₿' };
const errText = (e: unknown): string => {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || 'Something went wrong. Please try again.';
};

// ── Small presentational helpers ────────────────────────────────────────────────
const StatusPill: React.FC<{ status: AgentStatus }> = ({ status }) => {
  const active = status === 'ACTIVE';
  return (
    <span style={{
      display: 'inline-block', background: active ? T.successBg : T.dangerBg,
      color: active ? T.success : T.danger, fontSize: 11, fontWeight: 800,
      padding: '3px 10px', borderRadius: 20, letterSpacing: '0.03em',
    }}>{active ? 'Active' : 'Inactive'}</span>
  );
};

const Field: React.FC<{ label: string; value?: React.ReactNode }> = ({ label, value }) => (
  <div>
    <p style={{ margin: 0, fontSize: 11, fontWeight: 700, color: T.textLight, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</p>
    <p style={{ margin: '3px 0 0', fontSize: 14, color: T.textMain, fontWeight: 600, wordBreak: 'break-word' }}>{value ?? '—'}</p>
  </div>
);

const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={{ marginBottom: 20 }}>
    <p style={{ margin: '0 0 12px', fontSize: 12, fontWeight: 800, color: T.blue, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{title}</p>
    {children}
  </div>
);

const Checkbox: React.FC<{ label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }> = ({ label, hint, checked, onChange }) => (
  <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer', marginBottom: 14 }}>
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} style={{ width: 18, height: 18, marginTop: 1, accentColor: T.blue, cursor: 'pointer' }} />
    <span>
      <span style={{ fontSize: 13.5, fontWeight: 600, color: T.textMain }}>{label}</span>
      {hint && <span style={{ display: 'block', fontSize: 11.5, color: T.textMuted, marginTop: 2 }}>{hint}</span>}
    </span>
  </label>
);

const grid2: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '0 20px' };

// ── Form (Create / Edit) ────────────────────────────────────────────────────────
type FormState = {
  fullName: string; country: string; state: string; location: string;
  mobile: string; email: string; currency: string; dateOfCreation: string;
  reference: string; feesPct: string; transactionCode: string; category: AgentCategory;
  notes: string; riskAnalysis: boolean; sendForApproval: boolean; status: AgentStatus;
};

const blankForm = (): FormState => ({
  fullName: '', country: '', state: '', location: '', mobile: '', email: '',
  currency: 'INR', dateOfCreation: todayISO(), reference: '', feesPct: '',
  transactionCode: '', category: 'CASH', notes: '', riskAnalysis: false, sendForApproval: false,
  status: 'ACTIVE',
});

const AgentForm: React.FC<{
  mode: 'create' | 'edit';
  initial?: Agent;
  onCancel: () => void;
  onSaved: (a: Agent) => void;
}> = ({ mode, initial, onCancel, onSaved }) => {
  const [form, setForm] = useState<FormState>(() => {
    if (mode === 'edit' && initial) {
      return {
        fullName: initial.fullName, country: initial.country, state: initial.state,
        location: initial.location, mobile: initial.mobile || '', email: initial.email || '',
        currency: initial.currency, dateOfCreation: initial.dateOfCreation || todayISO(),
        reference: initial.reference || '', feesPct: String(initial.feesPct ?? ''),
        transactionCode: initial.transactionCode, category: initial.category,
        notes: initial.notes || '', riskAnalysis: initial.riskAnalysis, sendForApproval: false,
        status: initial.status,
      };
    }
    return blankForm();
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  const validate = (): string | null => {
    if (!form.fullName.trim()) return 'Full Name is required.';
    if (!form.country.trim()) return 'Country is required.';
    if (!form.state.trim()) return 'State is required.';
    if (!form.location.trim()) return 'Location is required.';
    if (!form.currency.trim()) return 'Currency is required.';
    if (form.feesPct === '' || isNaN(Number(form.feesPct))) return 'Fees % is required.';
    if (Number(form.feesPct) < 0) return 'Fees % cannot be negative.';
    if (mode === 'create' && !/^[A-Za-z0-9]{3}$/.test(form.transactionCode)) return 'Transaction Code must be exactly 3 alphanumeric characters.';
    return null;
  };

  const submit = async () => {
    const v = validate();
    if (v) { setError(v); return; }
    setError(''); setSaving(true);
    try {
      const base = {
        fullName: form.fullName.trim(), country: form.country.trim(), state: form.state.trim(),
        location: form.location.trim(), mobile: form.mobile.trim() || undefined,
        email: form.email.trim() || undefined, currency: form.currency,
        reference: form.reference || undefined, feesPct: Number(form.feesPct),
        category: form.category, notes: form.notes.trim() || undefined,
        riskAnalysis: form.riskAnalysis,
      };
      const saved = mode === 'create'
        ? await agentAPI.create({
            ...base, transactionCode: form.transactionCode.toUpperCase(),
            dateOfCreation: form.dateOfCreation, sendForApproval: form.sendForApproval,
          })
        : await agentAPI.update(initial!.id, { ...base, status: form.status });
      onSaved(saved);
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card style={{ maxWidth: 860, margin: '0 auto' }}>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>
          {mode === 'create' ? 'Create Agent' : `Edit Agent · ${initial?.agentId}`}
        </h2>
        <Btn variant="secondary" size="sm" onClick={onCancel}>← Back</Btn>
      </div>
      <div style={{ padding: '22px 24px' }}>
        {error && (
          <div style={{ background: T.dangerBg, color: T.danger, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 18 }}>
            {error}
          </div>
        )}

        <Section title="Basic Information">
          <Input label="Agent ID" value={mode === 'edit' ? initial!.agentId : 'Auto-generated on save'} onChange={() => {}} readOnly hint="System generated — cannot be edited." />
          <div style={grid2}>
            <Input label="Full Name" value={form.fullName} onChange={(e) => set('fullName', e.target.value)} required />
            <Sel label="Country" value={form.country} onChange={(e) => set('country', e.target.value)} required options={[{ value: '', label: 'Select country' }, ...COUNTRY_OPTIONS]} />
            <Input label="State" value={form.state} onChange={(e) => set('state', e.target.value)} required />
            <Input label="Location" value={form.location} onChange={(e) => set('location', e.target.value)} required />
          </div>
        </Section>

        <Section title="Contact Information">
          <div style={grid2}>
            <Input label="Mobile Number" value={form.mobile} onChange={(e) => set('mobile', e.target.value)} inputMode="tel" hint="Optional" />
            <Input label="Email Address" type="email" value={form.email} onChange={(e) => set('email', e.target.value)} hint="Optional" />
          </div>
        </Section>

        <Section title="Business Information">
          <div style={grid2}>
            <Sel label="Currency" value={form.currency} onChange={(e) => set('currency', e.target.value)} required options={CURRENCY_OPTIONS} />
            <Input label="Date of Creation" type="date" value={form.dateOfCreation} onChange={(e) => set('dateOfCreation', e.target.value)} readOnly={mode === 'edit'} />
            <Sel label="Reference" value={form.reference} onChange={(e) => set('reference', e.target.value)} options={REFERENCE_OPTIONS} />
            <Input label="Fees %" type="number" inputMode="decimal" value={form.feesPct} onChange={(e) => set('feesPct', e.target.value)} required />
            <Input
              label="Unique Transaction Code"
              value={form.transactionCode}
              onChange={(e) => set('transactionCode', e.target.value.replace(/[^A-Za-z0-9]/g, '').slice(0, 3).toUpperCase())}
              required={mode === 'create'}
              readOnly={mode === 'edit'}
              hint={mode === 'edit' ? 'Cannot be edited.' : 'Exactly 3 alphanumeric characters.'}
            />
            <Sel label="Category" value={form.category} onChange={(e) => set('category', e.target.value as AgentCategory)} required options={CATEGORY_OPTIONS} />
          </div>
        </Section>

        <Section title="Additional Information">
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes</label>
            <textarea value={form.notes} onChange={(e) => set('notes', e.target.value)} rows={3}
              style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
          </div>
          <Checkbox label="Perform Risk Analysis" checked={form.riskAnalysis} onChange={(v) => set('riskAnalysis', v)} />
          {mode === 'create' && (
            <Checkbox label="Send for Approval" hint="Route this agent through the approval workflow (Phase 6)." checked={form.sendForApproval} onChange={(v) => set('sendForApproval', v)} />
          )}
          {mode === 'edit' && (
            <Sel label="Status" value={form.status} onChange={(e) => set('status', e.target.value as AgentStatus)} style={{ maxWidth: 260 }}
              options={[{ value: 'ACTIVE', label: 'Active' }, { value: 'INACTIVE', label: 'Inactive' }]} />
          )}
        </Section>

        <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end', borderTop: `1px solid ${T.border}`, paddingTop: 18 }}>
          <Btn variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Btn>
          <Btn variant="primary" onClick={submit} disabled={saving}>{saving ? 'Saving…' : mode === 'create' ? 'Create Agent' : 'Save Changes'}</Btn>
        </div>
      </div>
    </Card>
  );
};

// ── View (read-only) ──────────────────────────────────────────────────────────
const AgentView: React.FC<{ agent: Agent; onBack: () => void; onEdit: () => void }> = ({ agent, onBack, onEdit }) => (
  <Card style={{ maxWidth: 860, margin: '0 auto' }}>
    <div style={{ padding: '20px 24px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>{agent.fullName}</h2>
        <StatusPill status={agent.status} />
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn variant="ghost" size="sm" onClick={onEdit}>Edit</Btn>
        <Btn variant="secondary" size="sm" onClick={onBack}>← Back</Btn>
      </div>
    </div>
    <div style={{ padding: '22px 24px' }}>
      <Section title="Basic Information">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Agent ID" value={agent.agentId} />
          <Field label="Full Name" value={agent.fullName} />
          <Field label="Category" value={CATEGORY_LABEL[agent.category]} />
          <Field label="Country" value={agent.country} />
          <Field label="State" value={agent.state} />
          <Field label="Location" value={agent.location} />
        </div>
      </Section>
      <Section title="Contact Information">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Mobile Number" value={agent.mobile} />
          <Field label="Email Address" value={agent.email} />
        </div>
      </Section>
      <Section title="Business Information">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Currency" value={agent.currency} />
          <Field label="Fees %" value={`${agent.feesPct}%`} />
          <Field label="Transaction Code" value={agent.transactionCode} />
          <Field label="Reference" value={agent.reference} />
          <Field label="Date of Creation" value={agent.dateOfCreation} />
        </div>
      </Section>
      <Section title="Additional Information">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Notes" value={agent.notes} />
          <Field label="Risk Analysis" value={agent.riskAnalysis ? 'Yes' : 'No'} />
          <Field label="Status" value={agent.status === 'ACTIVE' ? 'Active' : 'Inactive'} />
          <Field label="Approval" value={agent.approvalStatus.replace('_', ' ')} />
          <Field label="Created By" value={agent.createdBy} />
          <Field label="Created Date & Time" value={agent.createdAt ? formatDateTimeIST(agent.createdAt) : '—'} />
          <Field label="Last Updated By" value={agent.updatedBy} />
          <Field label="Last Updated Date & Time" value={agent.updatedAt ? formatDateTimeIST(agent.updatedAt) : '—'} />
        </div>
      </Section>
    </div>
  </Card>
);

// ── Main page (list + search + filters + pagination) ────────────────────────────
type Mode = { screen: 'list' } | { screen: 'create' } | { screen: 'edit'; agent: Agent } | { screen: 'view'; agent: Agent };

export const AgentsPage: React.FC<AgentPageProps> = ({ onNavigate }) => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<Mode>({ screen: 'list' });
  const [search, setSearch] = useState('');
  const [fCat, setFCat] = useState('');
  const [fCountry, setFCountry] = useState('');
  const [fState, setFState] = useState('');
  const [fStatus, setFStatus] = useState('');
  const [page, setPage] = useState(1);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [toDelete, setToDelete] = useState<Agent | null>(null);
  const [deleteErr, setDeleteErr] = useState('');
  const [banner, setBanner] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try { setAgents(await agentAPI.list()); }
    catch { /* surfaced by the empty state */ }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // Filter option lists are derived from the loaded data.
  const countryOpts = useMemo(() => Array.from(new Set(agents.map((a) => a.country))).sort(), [agents]);
  const stateOpts = useMemo(() => Array.from(new Set(agents.map((a) => a.state))).sort(), [agents]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return agents.filter((a) => {
      if (q && !`${a.agentId} ${a.fullName} ${a.mobile || ''} ${a.email || ''}`.toLowerCase().includes(q)) return false;
      if (fCat && a.category !== fCat) return false;
      if (fCountry && a.country !== fCountry) return false;
      if (fState && a.state !== fState) return false;
      if (fStatus && a.status !== fStatus) return false;
      return true;
    });
  }, [agents, search, fCat, fCountry, fState, fStatus]);

  useEffect(() => { setPage(1); }, [search, fCat, fCountry, fState, fStatus]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const flash = (msg: string) => { setBanner(msg); window.setTimeout(() => setBanner(''), 3500); };

  const toggleStatus = async (a: Agent) => {
    setBusyId(a.id);
    try {
      const next = a.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE';
      const updated = await agentAPI.setStatus(a.id, next);
      setAgents((list) => list.map((x) => (x.id === a.id ? updated : x)));
      flash(`${updated.fullName} is now ${next === 'ACTIVE' ? 'Active' : 'Inactive'}.`);
    } catch (e) { flash(errText(e)); }
    finally { setBusyId(null); }
  };

  const confirmDelete = async () => {
    if (!toDelete) return;
    setBusyId(toDelete.id); setDeleteErr('');
    try {
      await agentAPI.remove(toDelete.id);
      setAgents((list) => list.filter((x) => x.id !== toDelete.id));
      flash(`${toDelete.fullName} deleted.`);
      setToDelete(null);
    } catch (e) { setDeleteErr(errText(e)); }
    finally { setBusyId(null); }
  };

  if (mode.screen === 'create')
    return <AgentForm mode="create" onCancel={() => setMode({ screen: 'list' })} onSaved={(a) => { setAgents((l) => [a, ...l]); setMode({ screen: 'list' }); flash(`Agent ${a.agentId} created.`); }} />;
  if (mode.screen === 'edit')
    return <AgentForm mode="edit" initial={mode.agent} onCancel={() => setMode({ screen: 'list' })} onSaved={(a) => { setAgents((l) => l.map((x) => (x.id === a.id ? a : x))); setMode({ screen: 'list' }); flash(`Agent ${a.agentId} updated.`); }} />;
  if (mode.screen === 'view')
    return <AgentView agent={mode.agent} onBack={() => setMode({ screen: 'list' })} onEdit={() => setMode({ screen: 'edit', agent: mode.agent })} />;

  const th: React.CSSProperties = { textAlign: 'left', padding: '11px 12px', fontSize: 11, fontWeight: 800, color: T.textLight, textTransform: 'uppercase', letterSpacing: '0.04em', whiteSpace: 'nowrap' };
  const td: React.CSSProperties = { padding: '12px', fontSize: 13, color: T.textMain, borderTop: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };

  return (
    <div>
      {banner && (
        <div style={{ background: T.successBg, color: T.success, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 14 }}>{banner}</div>
      )}

      {/* Toolbar */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
        <Input label="Search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Agent ID, name, mobile or email" icon="🔍" style={{ marginBottom: 0, flex: '1 1 260px' }} />
        <Sel label="Category" value={fCat} onChange={(e) => setFCat(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...CATEGORY_OPTIONS]} />
        <Sel label="Country" value={fCountry} onChange={(e) => setFCountry(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...countryOpts.map((c) => ({ value: c, label: c }))]} />
        <Sel label="State" value={fState} onChange={(e) => setFState(e.target.value)} style={{ marginBottom: 0, minWidth: 140 }} options={[{ value: '', label: 'All' }, ...stateOpts.map((s) => ({ value: s, label: s }))]} />
        <Sel label="Status" value={fStatus} onChange={(e) => setFStatus(e.target.value)} style={{ marginBottom: 0, minWidth: 130 }} options={[{ value: '', label: 'All' }, { value: 'ACTIVE', label: 'Active' }, { value: 'INACTIVE', label: 'Inactive' }]} />
        <Btn variant="primary" onClick={() => setMode({ screen: 'create' })} style={{ marginLeft: 'auto' }}>＋ Create Agent</Btn>
      </div>

      <Card>
        {loading ? (
          <div style={{ padding: 16 }}><TableSkeleton rows={6} cols={7} /></div>
        ) : filtered.length === 0 ? (
          <div style={{ padding: '56px 24px', textAlign: 'center' }}>
            <div style={{ fontSize: 40, marginBottom: 10 }}>🧑‍💼</div>
            <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: T.textMain }}>{agents.length === 0 ? 'No agents yet' : 'No agents match your filters'}</p>
            <p style={{ margin: '6px 0 16px', fontSize: 13, color: T.textMuted }}>{agents.length === 0 ? 'Create your first Non-EPS agent to get started.' : 'Try clearing the search or filters.'}</p>
            {agents.length === 0 && <Btn variant="primary" onClick={() => setMode({ screen: 'create' })}>＋ Create Agent</Btn>}
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 1050 }}>
              <thead>
                <tr>
                  {['Agent ID', 'Full Name', 'Country', 'State', 'Location', 'Category', 'Currency', 'Fees %', 'Txn Code', 'Status', 'Created By', 'Created (IST)', 'Actions'].map((h) => (
                    <th key={h} style={th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pageRows.map((a) => (
                  <tr key={a.id}>
                    <td style={{ ...td, fontWeight: 700, color: T.blue }}>{a.agentId}</td>
                    <td style={td}>{a.fullName}</td>
                    <td style={td}>{a.country}</td>
                    <td style={td}>{a.state}</td>
                    <td style={td}>{a.location}</td>
                    <td style={td}>{CATEGORY_LABEL[a.category]}</td>
                    <td style={td}>{a.currency}</td>
                    <td style={td}>{a.feesPct}%</td>
                    <td style={{ ...td, fontWeight: 700 }}>{a.transactionCode}</td>
                    <td style={td}><StatusPill status={a.status} /></td>
                    <td style={td}>{a.createdBy || '—'}</td>
                    <td style={{ ...td, color: T.textMuted, fontSize: 12 }}>{a.createdAt ? formatDateTimeIST(a.createdAt) : '—'}</td>
                    <td style={td}>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <Btn variant="ghost" size="sm" onClick={() => setMode({ screen: 'view', agent: a })}>View</Btn>
                        <Btn variant="secondary" size="sm" onClick={() => setMode({ screen: 'edit', agent: a })}>Edit</Btn>
                        <Btn variant="ghost" size="sm" onClick={() => { openAgentAccounts(a.id); onNavigate?.('agent-accounts'); }}>Accounts</Btn>
                        <Btn variant={a.status === 'ACTIVE' ? 'secondary' : 'success'} size="sm" disabled={busyId === a.id} onClick={() => toggleStatus(a)}>
                          {a.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}
                        </Btn>
                        <Btn variant="danger" size="sm" disabled={busyId === a.id} onClick={() => { setDeleteErr(''); setToDelete(a); }}>Delete</Btn>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!loading && filtered.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderTop: `1px solid ${T.border}`, flexWrap: 'wrap', gap: 10 }}>
            <span style={{ fontSize: 12.5, color: T.textMuted }}>
              Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}
            </span>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <Btn variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>← Prev</Btn>
              <span style={{ fontSize: 12.5, color: T.textMuted }}>Page {page} / {pageCount}</span>
              <Btn variant="secondary" size="sm" disabled={page >= pageCount} onClick={() => setPage((p) => Math.min(pageCount, p + 1))}>Next →</Btn>
            </div>
          </div>
        )}
      </Card>

      {toDelete && (
        <Modal title="Delete Agent" onClose={() => setToDelete(null)}>
          {deleteErr ? (
            <div style={{ background: T.warningBg, color: T.warning, borderRadius: 10, padding: '12px 14px', fontSize: 13, fontWeight: 600, marginBottom: 16, lineHeight: 1.5 }}>{deleteErr}</div>
          ) : (
            <p style={{ margin: '0 0 18px', fontSize: 14, color: T.textMain, lineHeight: 1.55 }}>
              Delete agent <b>{toDelete.fullName}</b> ({toDelete.agentId})? This cannot be undone.
            </p>
          )}
          <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end' }}>
            <Btn variant="secondary" onClick={() => setToDelete(null)}>{deleteErr ? 'Close' : 'Cancel'}</Btn>
            {!deleteErr && <Btn variant="danger" disabled={busyId === toDelete.id} onClick={confirmDelete}>{busyId === toDelete.id ? 'Deleting…' : 'Delete Agent'}</Btn>}
          </div>
        </Modal>
      )}
    </div>
  );
};

// ═══ Agent Assignment panel (Phase 4) ════════════════════════════════════════════
// Embedded (demo-gated) in the shared TransactionDetailsModal. Lets the allowed operator role
// assign / reassign a Non-EPS agent + account to a Deposit / Withdrawal / Settlement, following
// Payment-Method (account type) → Active Agent → that agent's Active accounts of that type.
const ASSIGNER_ROLES: Record<string, string[]> = {
  DEPOSIT: ['DEO', 'DEPOSIT_OPERATOR'],
  WITHDRAWAL: ['DEO', 'WITHDRAWAL_OPERATOR'],
  SETTLEMENT: ['SUPERVISOR'],
};

// Selection-only assignment picker used INSIDE the create workflows (Deposit / Withdrawal /
// Settlement). It only chooses Payment Method (account type) → Active Agent → that agent's Active
// accounts of that type; the parent form performs the assign call after the transaction is created.
export interface AgentAssignSelection { accountType: AgentAccountType; agentId: string; accountId: string }
export const emptyAgentAssignSelection: AgentAssignSelection = { accountType: 'BANK', agentId: '', accountId: '' };

export const AgentAssignmentSelect: React.FC<{
  value: AgentAssignSelection;
  onChange: (v: AgentAssignSelection) => void;
}> = ({ value, onChange }) => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [accts, setAccts] = useState<AgentAccount[]>([]);
  useEffect(() => { agentAPI.list({ status: 'ACTIVE' }).then(setAgents).catch(() => setAgents([])); }, []);
  useEffect(() => {
    if (!value.agentId) { setAccts([]); return; }
    agentAccountAPI.list(Number(value.agentId), { status: 'ACTIVE', accountType: value.accountType })
      .then(setAccts).catch(() => setAccts([]));
  }, [value.agentId, value.accountType]);
  return (
    <div style={{ background: T.canvas, borderRadius: 12, padding: '12px 14px', margin: '4px 0 14px' }}>
      <p style={{ fontSize: 11, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em', margin: '0 0 4px' }}>👥 Assign Non-EPS Agent (optional)</p>
      <p style={{ fontSize: 11.5, color: T.textMuted, margin: '0 0 10px' }}>Only active agents and their active accounts of the chosen payment method are shown.</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: '0 14px' }}>
        <Sel label="Payment Method" value={value.accountType} onChange={(e) => onChange({ ...value, accountType: e.target.value as AgentAccountType, accountId: '' })} options={ACCOUNT_TYPE_OPTIONS} />
        <Sel label="Agent (Active)" value={value.agentId} onChange={(e) => onChange({ ...value, agentId: e.target.value, accountId: '' })} options={[{ value: '', label: agents.length ? 'Select agent…' : 'No active agents' }, ...agents.map((a) => ({ value: String(a.id), label: `${a.agentId} · ${a.fullName}` }))]} />
        <Sel label="Account (Active)" value={value.accountId} onChange={(e) => onChange({ ...value, accountId: e.target.value })} options={[{ value: '', label: value.agentId ? (accts.length ? 'Select account…' : 'No active accounts of this type') : 'Select an agent first' }, ...accts.map((a) => ({ value: String(a.id), label: `${a.accountRef} · ${a.label || a.keyDetail}` }))]} />
      </div>
    </div>
  );
};

export const AgentAssignmentPanel: React.FC<{ txRef: string; txType: string; assignerRole?: string | null; readOnly?: boolean }> = ({ txRef, txType, assignerRole, readOnly }) => {
  const base = (txType || '').split('_')[0].toUpperCase();   // DEPOSIT / WITHDRAWAL / SETTLEMENT
  const canAssign = !readOnly && !!assignerRole && (ASSIGNER_ROLES[base] || []).includes(String(assignerRole).toUpperCase());
  const [data, setData] = useState<AgentAssignmentResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [accts, setAccts] = useState<AgentAccount[]>([]);
  const [acctType, setAcctType] = useState<AgentAccountType>('BANK');
  const [agentId, setAgentId] = useState('');
  const [acctId, setAcctId] = useState('');
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try { setData(await agentAssignmentAPI.get(txRef)); } catch { setData(null); }
    finally { setLoading(false); }
  }, [txRef]);
  useEffect(() => { load(); }, [load]);

  useEffect(() => { if (open) agentAPI.list({ status: 'ACTIVE' }).then(setAgents).catch(() => setAgents([])); }, [open]);
  useEffect(() => {
    if (!open || !agentId) { setAccts([]); setAcctId(''); return; }
    agentAccountAPI.list(Number(agentId), { status: 'ACTIVE', accountType: acctType }).then(setAccts).catch(() => setAccts([]));
    setAcctId('');
  }, [open, agentId, acctType]);

  const submit = async () => {
    if (!agentId || !acctId) { setErr('Select an agent and an account.'); return; }
    setErr(''); setSaving(true);
    try {
      await agentAssignmentAPI.assign(txRef, { agentId: Number(agentId), agentAccountId: Number(acctId), paymentMethod: acctType });
      setOpen(false); setAgentId(''); setAcctId('');
      await load();
    } catch (e) { setErr(errText(e)); }
    finally { setSaving(false); }
  };

  const cur = data?.current;
  return (
    <div style={{ marginTop: 10, border: `1px solid ${T.border}`, borderRadius: 12, overflow: 'hidden' }}>
      <div style={{ padding: '10px 16px', background: T.canvas, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 800, color: T.textMain }}>👥 Agent Assignment</span>
        {canAssign && !open && <Btn size="sm" variant="ghost" onClick={() => { setErr(''); setOpen(true); }}>{cur?.assigned ? 'Reassign' : 'Assign Agent'}</Btn>}
      </div>
      <div style={{ padding: '14px 16px' }}>
        {loading ? <span style={{ fontSize: 13, color: T.textMuted }}>Loading…</span> : (
          <>
            {cur?.assigned ? (
              <div style={{ fontSize: 13, color: T.textMain, lineHeight: 1.7 }}>
                <div><b>{cur.agentName}</b> <span style={{ color: T.textMuted }}>({cur.agentId})</span></div>
                <div>{ACCOUNT_TYPE_LABEL[cur.accountType || ''] || cur.accountType} · {cur.accountRef}{cur.accountDetail ? ` — ${cur.accountDetail}` : ''}</div>
                <div style={{ fontSize: 11.5, color: T.textMuted }}>Assigned by {cur.assignedBy}{cur.assignedAt ? ` · ${formatDateTimeIST(cur.assignedAt)}` : ''}</div>
              </div>
            ) : <span style={{ fontSize: 13, color: T.textMuted }}>No agent assigned yet.</span>}

            {open && (
              <div style={{ marginTop: 14, borderTop: `1px dashed ${T.border}`, paddingTop: 14 }}>
                {err && <div style={{ background: T.dangerBg, color: T.danger, borderRadius: 8, padding: '8px 12px', fontSize: 12.5, fontWeight: 600, marginBottom: 12 }}>{err}</div>}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: '0 14px' }}>
                  <Sel label="Payment Method (Account Type)" value={acctType} onChange={(e) => setAcctType(e.target.value as AgentAccountType)} options={ACCOUNT_TYPE_OPTIONS} />
                  <Sel label="Agent (Active)" value={agentId} onChange={(e) => setAgentId(e.target.value)} options={[{ value: '', label: 'Select agent…' }, ...agents.map((a) => ({ value: String(a.id), label: `${a.agentId} · ${a.fullName}` }))]} />
                  <Sel label="Account (Active)" value={acctId} onChange={(e) => setAcctId(e.target.value)} options={[{ value: '', label: agentId ? (accts.length ? 'Select account…' : 'No active accounts of this type') : 'Select an agent first' }, ...accts.map((a) => ({ value: String(a.id), label: `${a.accountRef} · ${a.label || a.keyDetail}` }))]} />
                </div>
                <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 6 }}>
                  <Btn size="sm" variant="secondary" onClick={() => { setOpen(false); setErr(''); }} disabled={saving}>Cancel</Btn>
                  <Btn size="sm" variant="primary" onClick={submit} disabled={saving || !agentId || !acctId}>{saving ? 'Assigning…' : 'Confirm Assignment'}</Btn>
                </div>
              </div>
            )}

            {data && data.history.length > 0 && (
              <details style={{ marginTop: 12 }}>
                <summary style={{ fontSize: 12, color: T.textMuted, cursor: 'pointer', fontWeight: 700 }}>History ({data.history.length})</summary>
                <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {data.history.map((h) => (
                    <div key={h.id} style={{ fontSize: 12, color: T.textMuted, borderLeft: `2px solid ${T.border}`, paddingLeft: 10 }}>
                      <b style={{ color: T.textMain }}>{h.action}</b> → {h.agentId} · {h.accountRef} ({ACCOUNT_TYPE_LABEL[h.accountType] || h.accountType}) · by {h.assignedBy}{h.createdAt ? ` · ${formatDateTimeIST(h.createdAt)}` : ''}
                    </div>
                  ))}
                </div>
              </details>
            )}
          </>
        )}
      </div>
    </div>
  );
};

// ═══ Agent Dashboard (Phase 5 — statistics, charts, recent activity) ══════════════
const CATEGORY_LABEL_FULL: Record<string, string> = { CASH: 'Cash', BANK_TRANSFER: 'Bank Transfer', CRYPTO: 'Crypto' };
const TXTYPE_LABEL: Record<string, string> = { DEPOSIT: 'Deposit', WITHDRAWAL: 'Withdrawal', SETTLEMENT: 'Settlement' };

const BarList: React.FC<{ title: string; icon?: string; color?: string; data: Array<{ label: string; value: number }> }> = ({ title, icon, color = T.blue, data }) => {
  const max = Math.max(1, ...data.map((d) => d.value));
  const total = data.reduce((a, d) => a + d.value, 0);
  return (
    <Card style={{ padding: '16px 18px' }}>
      <p style={{ margin: '0 0 14px', fontSize: 13, fontWeight: 800, color: T.textMain }}>{icon ? `${icon} ` : ''}{title}</p>
      {total === 0 ? (
        <p style={{ fontSize: 12.5, color: T.textMuted, margin: 0 }}>No data yet.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
          {data.map((d) => (
            <div key={d.label}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                <span style={{ color: T.textMain, fontWeight: 600 }}>{d.label}</span>
                <span style={{ color: T.textMuted, fontWeight: 700 }}>{d.value}</span>
              </div>
              <div style={{ height: 8, background: T.borderLight, borderRadius: 6, overflow: 'hidden' }}>
                <div style={{ width: `${(d.value / max) * 100}%`, height: '100%', background: color, borderRadius: 6, transition: 'width 0.4s ease' }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
};

const toBars = (rec: Record<string, number>, labels: Record<string, string>): Array<{ label: string; value: number }> =>
  Object.entries(rec).map(([k, v]) => ({ label: labels[k] || k, value: v }));

export const AgentDashboardPage: React.FC<AgentPageProps> = ({ onNavigate }) => {
  const [d, setD] = useState<AgentDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try { setD(await agentDashboardAPI.get()); } catch { setD(null); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  if (loading) return <LoadingScreen label="Loading agent analytics…" />;
  if (!d) return <Card><div style={{ padding: 40, textAlign: 'center', color: T.textMuted }}>Could not load the dashboard. <Btn variant="ghost" size="sm" onClick={load}>Retry</Btn></div></Card>;

  const gridCards: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 14, marginBottom: 16 };
  const gridCharts: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14, marginBottom: 16 };
  const th: React.CSSProperties = { textAlign: 'left', padding: '10px 12px', fontSize: 11, fontWeight: 800, color: T.textLight, textTransform: 'uppercase', letterSpacing: '0.04em', whiteSpace: 'nowrap' };
  const td: React.CSSProperties = { padding: '11px 12px', fontSize: 13, color: T.textMain, borderTop: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 10, flexWrap: 'wrap' }}>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Overview of your Non-EPS agents and the transactions they are handling.</p>
        <div style={{ display: 'flex', gap: 8 }}>
          <Btn variant="secondary" size="sm" onClick={load}>↻ Refresh</Btn>
          <Btn variant="ghost" size="sm" onClick={() => onNavigate?.('agents')}>Manage Agents →</Btn>
        </div>
      </div>

      <div style={gridCards}>
        <StatCard icon="🧑‍💼" label="Total Agents" value={d.agents.total} sub={`${d.agents.active} active · ${d.agents.inactive} inactive`} color={T.blue} />
        <StatCard icon="🏦" label="Agent Accounts" value={d.accounts.total} sub={`${d.accounts.active} active · ${d.accounts.inactive} inactive`} color={T.green} />
        <StatCard icon="🔗" label="Transactions Assigned" value={d.assignments.totalTransactions} sub="currently assigned to an agent" color={T.info} />
        <StatCard icon="⚠️" label="Unassigned Transactions" value={d.assignments.unassignedTransactions} sub="click to assign an agent" color={T.danger} onClick={() => onNavigate?.('agent-unassigned')} />
        <StatCard icon="🔄" label="Reassignments" value={d.assignments.reassignments} sub="total reassign actions" color={T.warning} />
      </div>

      <div style={gridCharts}>
        <BarList title="Assignments by Transaction Type" icon="≡" color={T.blue} data={toBars(d.assignments.byTxType, TXTYPE_LABEL)} />
        <BarList title="Assignments by Channel" icon="💳" color={T.green} data={toBars(d.assignments.byChannel, ACCOUNT_TYPE_LABEL)} />
        <BarList title="Agents by Category" icon="🏷️" color={T.info} data={toBars(d.agentsByCategory, CATEGORY_LABEL_FULL)} />
        <BarList title="Accounts by Type" icon="🗂️" color={T.blue} data={toBars(d.accounts.byType, ACCOUNT_TYPE_LABEL)} />
        <BarList title="Agents by Country" icon="🌐" color={T.green} data={d.agentsByCountry.map((c) => ({ label: c.label, value: c.count }))} />
        <BarList title="Top Agents (by transactions handled)" icon="🏆" color={T.warning} data={d.topAgents.map((a) => ({ label: `${a.agentId} · ${a.name}`, value: a.count }))} />
      </div>

      <Card>
        <div style={{ padding: '14px 18px', borderBottom: `1px solid ${T.border}` }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain }}>Recent Agent Assignments</h3>
        </div>
        {d.recent.length === 0 ? (
          <div style={{ padding: '40px 24px', textAlign: 'center', color: T.textMuted, fontSize: 13 }}>No agent assignments yet.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 820 }}>
              <thead><tr>{['Action', 'Transaction', 'Type', 'Agent', 'Account', 'Assigned By', 'When (IST)'].map((h) => <th key={h} style={th}>{h}</th>)}</tr></thead>
              <tbody>
                {d.recent.map((r) => (
                  <tr key={r.id}>
                    <td style={td}><span style={{ background: r.action === 'REASSIGN' ? T.warningBg : T.successBg, color: r.action === 'REASSIGN' ? T.warning : T.success, fontSize: 10.5, fontWeight: 800, padding: '2px 8px', borderRadius: 20 }}>{r.action}</span></td>
                    <td style={{ ...td, fontWeight: 700, color: T.blue }}>{r.txRef}</td>
                    <td style={td}>{TXTYPE_LABEL[r.txType] || r.txType}</td>
                    <td style={td}>{r.agentId} · {r.agentName}</td>
                    <td style={td}>{r.accountRef} ({ACCOUNT_TYPE_LABEL[r.accountType] || r.accountType})</td>
                    <td style={td}>{r.assignedBy || '—'}</td>
                    <td style={{ ...td, color: T.textMuted, fontSize: 12 }}>{r.createdAt ? formatDateTimeIST(r.createdAt) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
};

// ═══ Agent Accounts (Bank / UPI / QR / Crypto per agent) ═════════════════════════
const NETWORK_OPTIONS = ['TRC20', 'ERC20', 'BEP20', 'BTC', 'SOL', 'POLYGON', 'OTHER'].map((v) => ({ value: v, label: v }));
const ASSET_OPTIONS = ['USDT', 'USDC', 'BTC', 'ETH', 'BNB', 'SOL', 'OTHER'].map((v) => ({ value: v, label: v }));

type AcctForm = {
  accountType: AgentAccountType; label: string; currency: string; notes: string; isDefault: boolean;
  accountHolder: string; accountNumber: string; ifsc: string; bankName: string; branch: string;
  upiId: string; upiHolder: string; qrImage: string; qrLinkedRef: string;
  walletAddress: string; cryptoNetwork: string; cryptoAsset: string;
};
const blankAcct = (currency: string): AcctForm => ({
  accountType: 'BANK', label: '', currency: currency || 'INR', notes: '', isDefault: false,
  accountHolder: '', accountNumber: '', ifsc: '', bankName: '', branch: '',
  upiId: '', upiHolder: '', qrImage: '', qrLinkedRef: '',
  walletAddress: '', cryptoNetwork: 'TRC20', cryptoAsset: 'USDT',
});

const AccountForm: React.FC<{
  mode: 'create' | 'edit'; agent: Agent; initial?: AgentAccount;
  onCancel: () => void; onSaved: (a: AgentAccount) => void;
}> = ({ mode, agent, initial, onCancel, onSaved }) => {
  const [form, setForm] = useState<AcctForm>(() => {
    if (mode === 'edit' && initial) {
      return {
        accountType: initial.accountType, label: initial.label || '', currency: initial.currency,
        notes: initial.notes || '', isDefault: initial.isDefault,
        accountHolder: initial.accountHolder || '', accountNumber: initial.accountNumber || '',
        ifsc: initial.ifsc || '', bankName: initial.bankName || '', branch: initial.branch || '',
        upiId: initial.upiId || '', upiHolder: initial.upiHolder || '',
        qrImage: initial.qrImage || '', qrLinkedRef: initial.qrLinkedRef || '',
        walletAddress: initial.walletAddress || '', cryptoNetwork: initial.cryptoNetwork || 'TRC20',
        cryptoAsset: initial.cryptoAsset || 'USDT',
      };
    }
    return blankAcct(agent.currency);
  });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof AcctForm>(k: K, v: AcctForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  const onQr = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 3 * 1024 * 1024) { setError('QR image must be under 3 MB.'); return; }
    try { set('qrImage', await fileToDataUrl(f)); setError(''); }
    catch { setError('Could not read that image.'); }
  };

  const validate = (): string | null => {
    const t = form.accountType;
    if (t === 'BANK') {
      if (!form.accountHolder.trim()) return 'Account Holder is required.';
      if (!form.accountNumber.trim()) return 'Account Number is required.';
      if (!form.ifsc.trim()) return 'IFSC / Routing is required.';
      if (!form.bankName.trim()) return 'Bank Name is required.';
    } else if (t === 'UPI') {
      if (!form.upiId.trim()) return 'UPI ID is required.';
      if (!form.upiId.includes('@')) return 'Enter a valid UPI ID (name@bank).';
    } else if (t === 'QR') {
      if (!form.qrImage) return 'A QR image is required.';
    } else if (t === 'CRYPTO') {
      if (!form.walletAddress.trim()) return 'Wallet Address is required.';
      if (!form.cryptoNetwork.trim()) return 'Network is required.';
      if (!form.cryptoAsset.trim()) return 'Asset / Coin is required.';
    }
    return null;
  };

  const submit = async () => {
    const v = validate();
    if (v) { setError(v); return; }
    setError(''); setSaving(true);
    try {
      const payload = {
        label: form.label.trim() || undefined, currency: form.currency, notes: form.notes.trim() || undefined,
        accountHolder: form.accountHolder.trim() || undefined, accountNumber: form.accountNumber.trim() || undefined,
        ifsc: form.ifsc.trim() || undefined, bankName: form.bankName.trim() || undefined, branch: form.branch.trim() || undefined,
        upiId: form.upiId.trim() || undefined, upiHolder: form.upiHolder.trim() || undefined,
        qrImage: form.qrImage || undefined, qrLinkedRef: form.qrLinkedRef.trim() || undefined,
        walletAddress: form.walletAddress.trim() || undefined, cryptoNetwork: form.cryptoNetwork || undefined,
        cryptoAsset: form.cryptoAsset || undefined,
      };
      const saved = mode === 'create'
        ? await agentAccountAPI.create(agent.id, { ...payload, accountType: form.accountType, isDefault: form.isDefault })
        : await agentAccountAPI.update(agent.id, initial!.id, payload);
      onSaved(saved);
    } catch (e) { setError(errText(e)); }
    finally { setSaving(false); }
  };

  const t = form.accountType;
  return (
    <Card style={{ maxWidth: 760, margin: '0 auto' }}>
      <div style={{ padding: '20px 24px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>
          {mode === 'create' ? 'Add Account' : `Edit Account · ${initial?.accountRef}`}
          <span style={{ fontSize: 13, fontWeight: 600, color: T.textMuted, marginLeft: 8 }}>· {agent.agentId} {agent.fullName}</span>
        </h2>
        <Btn variant="secondary" size="sm" onClick={onCancel}>← Back</Btn>
      </div>
      <div style={{ padding: '22px 24px' }}>
        {error && <div style={{ background: T.dangerBg, color: T.danger, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 18 }}>{error}</div>}

        <Section title="Account Type">
          <Sel label="Type" value={form.accountType} onChange={(e) => set('accountType', e.target.value as AgentAccountType)}
            options={ACCOUNT_TYPE_OPTIONS} required style={{ maxWidth: 300, ...(mode === 'edit' ? { pointerEvents: 'none', opacity: 0.7 } : {}) }} />
          {mode === 'edit' && <p style={{ margin: '-8px 0 0', fontSize: 11.5, color: T.textMuted }}>Account type cannot be changed after creation.</p>}
        </Section>

        {t === 'BANK' && (
          <Section title="Bank Details">
            <div style={grid2}>
              <Input label="Account Holder" value={form.accountHolder} onChange={(e) => set('accountHolder', e.target.value)} required />
              <Input label="Account Number" value={form.accountNumber} onChange={(e) => set('accountNumber', e.target.value)} required />
              <Input label="IFSC / Routing" value={form.ifsc} onChange={(e) => set('ifsc', e.target.value.toUpperCase())} required />
              <Input label="Bank Name" value={form.bankName} onChange={(e) => set('bankName', e.target.value)} required />
              <Input label="Branch" value={form.branch} onChange={(e) => set('branch', e.target.value)} />
            </div>
          </Section>
        )}
        {t === 'UPI' && (
          <Section title="UPI Details">
            <div style={grid2}>
              <Input label="UPI ID" value={form.upiId} onChange={(e) => set('upiId', e.target.value)} required hint="e.g. name@bank" />
              <Input label="Holder Name" value={form.upiHolder} onChange={(e) => set('upiHolder', e.target.value)} />
            </div>
          </Section>
        )}
        {t === 'QR' && (
          <Section title="QR Details">
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>QR Image <span style={{ color: T.danger }}>*</span></label>
              <input type="file" accept="image/*" onChange={onQr} style={{ fontSize: 13, color: T.textMain }} />
              {form.qrImage && <div style={{ marginTop: 12 }}><img src={form.qrImage} alt="QR" style={{ width: 150, height: 150, objectFit: 'contain', border: `1px solid ${T.border}`, borderRadius: 10, background: '#fff' }} /></div>}
            </div>
            <Input label="Linked UPI / Bank (optional)" value={form.qrLinkedRef} onChange={(e) => set('qrLinkedRef', e.target.value)} />
          </Section>
        )}
        {t === 'CRYPTO' && (
          <Section title="Crypto Details">
            <div style={grid2}>
              <Input label="Wallet Address" value={form.walletAddress} onChange={(e) => set('walletAddress', e.target.value)} required />
              <Sel label="Network / Chain" value={form.cryptoNetwork} onChange={(e) => set('cryptoNetwork', e.target.value)} required options={NETWORK_OPTIONS} />
              <Sel label="Asset / Coin" value={form.cryptoAsset} onChange={(e) => set('cryptoAsset', e.target.value)} required options={ASSET_OPTIONS} />
            </div>
          </Section>
        )}

        <Section title="Common">
          <div style={grid2}>
            <Input label="Label / Nickname" value={form.label} onChange={(e) => set('label', e.target.value)} hint="Optional" />
            <Sel label="Currency" value={form.currency} onChange={(e) => set('currency', e.target.value)} options={CURRENCY_OPTIONS} />
          </div>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Notes</label>
            <textarea value={form.notes} onChange={(e) => set('notes', e.target.value)} rows={2}
              style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical' }} />
          </div>
          {mode === 'create' && (
            <Checkbox label="Set as default for this account type" hint="Used first when this agent is assigned (Phase 4). One default per type." checked={form.isDefault} onChange={(v) => set('isDefault', v)} />
          )}
        </Section>

        <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end', borderTop: `1px solid ${T.border}`, paddingTop: 18 }}>
          <Btn variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Btn>
          <Btn variant="primary" onClick={submit} disabled={saving}>{saving ? 'Saving…' : mode === 'create' ? 'Add Account' : 'Save Changes'}</Btn>
        </div>
      </div>
    </Card>
  );
};

const AccountView: React.FC<{ account: AgentAccount; agent: Agent; onBack: () => void; onEdit: () => void }> = ({ account: a, agent, onBack, onEdit }) => (
  <Card style={{ maxWidth: 760, margin: '0 auto' }}>
    <div style={{ padding: '20px 24px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>{ACCOUNT_TYPE_ICON[a.accountType]} {a.label || ACCOUNT_TYPE_LABEL[a.accountType]}</h2>
        <StatusPill status={a.status} />
        {a.isDefault && <span style={{ background: T.infoBg, color: T.info, fontSize: 11, fontWeight: 800, padding: '3px 10px', borderRadius: 20 }}>DEFAULT</span>}
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn variant="ghost" size="sm" onClick={onEdit}>Edit</Btn>
        <Btn variant="secondary" size="sm" onClick={onBack}>← Back</Btn>
      </div>
    </div>
    <div style={{ padding: '22px 24px' }}>
      <Section title="Account">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Account Ref" value={a.accountRef} />
          <Field label="Type" value={ACCOUNT_TYPE_LABEL[a.accountType]} />
          <Field label="Agent" value={`${agent.agentId} · ${agent.fullName}`} />
          <Field label="Currency" value={a.currency} />
        </div>
      </Section>
      {a.accountType === 'BANK' && (
        <Section title="Bank Details">
          <div style={{ ...grid2, rowGap: 16 }}>
            <Field label="Account Holder" value={a.accountHolder} />
            <Field label="Account Number" value={a.accountNumber} />
            <Field label="IFSC / Routing" value={a.ifsc} />
            <Field label="Bank Name" value={a.bankName} />
            <Field label="Branch" value={a.branch} />
          </div>
        </Section>
      )}
      {a.accountType === 'UPI' && (
        <Section title="UPI Details">
          <div style={{ ...grid2, rowGap: 16 }}>
            <Field label="UPI ID" value={a.upiId} />
            <Field label="Holder Name" value={a.upiHolder} />
          </div>
        </Section>
      )}
      {a.accountType === 'QR' && (
        <Section title="QR Details">
          {a.qrImage && <img src={a.qrImage} alt="QR" style={{ width: 170, height: 170, objectFit: 'contain', border: `1px solid ${T.border}`, borderRadius: 10, background: '#fff', marginBottom: 12 }} />}
          <Field label="Linked UPI / Bank" value={a.qrLinkedRef} />
        </Section>
      )}
      {a.accountType === 'CRYPTO' && (
        <Section title="Crypto Details">
          <div style={{ ...grid2, rowGap: 16 }}>
            <Field label="Wallet Address" value={a.walletAddress} />
            <Field label="Network / Chain" value={a.cryptoNetwork} />
            <Field label="Asset / Coin" value={a.cryptoAsset} />
          </div>
        </Section>
      )}
      <Section title="Additional Information">
        <div style={{ ...grid2, rowGap: 16 }}>
          <Field label="Notes" value={a.notes} />
          <Field label="Default" value={a.isDefault ? 'Yes' : 'No'} />
          <Field label="Status" value={a.status === 'ACTIVE' ? 'Active' : 'Inactive'} />
          <Field label="Created By" value={a.createdBy} />
          <Field label="Created Date & Time" value={a.createdAt ? formatDateTimeIST(a.createdAt) : '—'} />
          <Field label="Last Updated By" value={a.updatedBy} />
          <Field label="Last Updated Date & Time" value={a.updatedAt ? formatDateTimeIST(a.updatedAt) : '—'} />
        </div>
      </Section>
    </div>
  </Card>
);

type AcctMode = { screen: 'list' } | { screen: 'create' } | { screen: 'edit'; account: AgentAccount } | { screen: 'view'; account: AgentAccount };

export const AgentAccountsPage: React.FC<AgentPageProps> = () => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [selAgentId, setSelAgentId] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<AgentAccount[]>([]);
  const [accLoading, setAccLoading] = useState(false);
  const [mode, setMode] = useState<AcctMode>({ screen: 'list' });
  const [search, setSearch] = useState('');
  const [fType, setFType] = useState('');
  const [fStatus, setFStatus] = useState('');
  const [page, setPage] = useState(1);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [toDelete, setToDelete] = useState<AgentAccount | null>(null);
  const [deleteErr, setDeleteErr] = useState('');
  const [banner, setBanner] = useState('');
  const flash = (m: string) => { setBanner(m); window.setTimeout(() => setBanner(''), 3500); };

  // Load agents once; honour a pending "Manage Accounts" hand-off from the Agents page.
  useEffect(() => {
    (async () => {
      setAgentsLoading(true);
      try {
        const list = await agentAPI.list();
        setAgents(list);
        const pending = _pendingAgentId;
        _pendingAgentId = null;
        if (pending && list.some((a) => a.id === pending)) setSelAgentId(pending);
      } catch { /* empty state */ }
      finally { setAgentsLoading(false); }
    })();
  }, []);

  const loadAccounts = useCallback(async (agentId: number) => {
    setAccLoading(true);
    try { setAccounts(await agentAccountAPI.list(agentId)); }
    catch { setAccounts([]); }
    finally { setAccLoading(false); }
  }, []);
  useEffect(() => { if (selAgentId != null) loadAccounts(selAgentId); else setAccounts([]); }, [selAgentId, loadAccounts]);

  const selAgent = useMemo(() => agents.find((a) => a.id === selAgentId) || null, [agents, selAgentId]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return accounts.filter((a) => {
      if (q && !`${a.accountRef} ${a.label || ''} ${a.accountNumber || ''} ${a.upiId || ''} ${a.walletAddress || ''}`.toLowerCase().includes(q)) return false;
      if (fType && a.accountType !== fType) return false;
      if (fStatus && a.status !== fStatus) return false;
      return true;
    });
  }, [accounts, search, fType, fStatus]);
  useEffect(() => { setPage(1); }, [search, fType, fStatus, selAgentId]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const toggleStatus = async (a: AgentAccount) => {
    if (selAgentId == null) return;
    setBusyId(a.id);
    try {
      const next = a.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE';
      const updated = await agentAccountAPI.setStatus(selAgentId, a.id, next);
      setAccounts((l) => l.map((x) => (x.id === a.id ? updated : x)));
      flash(`${updated.accountRef} is now ${next === 'ACTIVE' ? 'Active' : 'Inactive'}.`);
    } catch (e) { flash(errText(e)); }
    finally { setBusyId(null); }
  };

  const makeDefault = async (a: AgentAccount) => {
    if (selAgentId == null) return;
    setBusyId(a.id);
    try {
      await agentAccountAPI.setDefault(selAgentId, a.id);
      await loadAccounts(selAgentId);   // reload so the cleared previous default reflects
      flash(`${a.accountRef} set as default ${ACCOUNT_TYPE_LABEL[a.accountType]}.`);
    } catch (e) { flash(errText(e)); }
    finally { setBusyId(null); }
  };

  const confirmDelete = async () => {
    if (!toDelete || selAgentId == null) return;
    setBusyId(toDelete.id); setDeleteErr('');
    try {
      await agentAccountAPI.remove(selAgentId, toDelete.id);
      setAccounts((l) => l.filter((x) => x.id !== toDelete.id));
      flash(`${toDelete.accountRef} deleted.`);
      setToDelete(null);
    } catch (e) { setDeleteErr(errText(e)); }
    finally { setBusyId(null); }
  };

  if (agentsLoading) return <LoadingScreen label="Loading agents…" />;

  // Sub-screens (scoped to the selected agent).
  if (selAgent && mode.screen === 'create')
    return <AccountForm mode="create" agent={selAgent} onCancel={() => setMode({ screen: 'list' })} onSaved={(a) => { setAccounts((l) => [a, ...l]); setMode({ screen: 'list' }); flash(`Account ${a.accountRef} added.`); if (a.isDefault && selAgentId != null) loadAccounts(selAgentId); }} />;
  if (selAgent && mode.screen === 'edit')
    return <AccountForm mode="edit" agent={selAgent} initial={mode.account} onCancel={() => setMode({ screen: 'list' })} onSaved={(a) => { setAccounts((l) => l.map((x) => (x.id === a.id ? a : x))); setMode({ screen: 'list' }); flash(`Account ${a.accountRef} updated.`); }} />;
  if (selAgent && mode.screen === 'view')
    return <AccountView account={mode.account} agent={selAgent} onBack={() => setMode({ screen: 'list' })} onEdit={() => setMode({ screen: 'edit', account: mode.account })} />;

  const th: React.CSSProperties = { textAlign: 'left', padding: '11px 12px', fontSize: 11, fontWeight: 800, color: T.textLight, textTransform: 'uppercase', letterSpacing: '0.04em', whiteSpace: 'nowrap' };
  const td: React.CSSProperties = { padding: '12px', fontSize: 13, color: T.textMain, borderTop: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };

  return (
    <div>
      {banner && <div style={{ background: T.successBg, color: T.success, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 14 }}>{banner}</div>}

      {/* Step 1: pick an agent */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ padding: '16px 20px', display: 'flex', gap: 16, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <Sel label="Select Agent" value={selAgentId == null ? '' : String(selAgentId)} onChange={(e) => { setSelAgentId(e.target.value ? Number(e.target.value) : null); setMode({ screen: 'list' }); }}
            style={{ marginBottom: 0, minWidth: 320, flex: '1 1 320px' }}
            options={[{ value: '', label: agents.length ? 'Choose an agent…' : 'No agents — create one first' }, ...agents.map((a) => ({ value: String(a.id), label: `${a.agentId} · ${a.fullName} (${a.country})` }))]} />
          {selAgent && (
            <div style={{ display: 'flex', gap: 18, alignItems: 'center', fontSize: 12.5, color: T.textMuted }}>
              <span>Category: <b style={{ color: T.textMain }}>{{ CASH: 'Cash', BANK_TRANSFER: 'Bank Transfer', CRYPTO: 'Crypto' }[selAgent.category]}</b></span>
              <span>Currency: <b style={{ color: T.textMain }}>{selAgent.currency}</b></span>
              <StatusPill status={selAgent.status} />
            </div>
          )}
        </div>
      </Card>

      {!selAgent ? (
        <Card><div style={{ padding: '56px 24px', textAlign: 'center' }}>
          <div style={{ fontSize: 40, marginBottom: 10 }}>🏦</div>
          <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: T.textMain }}>Select an agent to manage their accounts</p>
          <p style={{ margin: '6px 0 0', fontSize: 13, color: T.textMuted }}>Each agent can hold multiple Bank, UPI, QR and Crypto accounts.</p>
        </div></Card>
      ) : (
        <>
          {/* Toolbar */}
          <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
            <Input label="Search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Ref, label, account no, UPI or wallet" icon="🔍" style={{ marginBottom: 0, flex: '1 1 240px' }} />
            <Sel label="Type" value={fType} onChange={(e) => setFType(e.target.value)} style={{ marginBottom: 0, minWidth: 160 }} options={[{ value: '', label: 'All' }, ...ACCOUNT_TYPE_OPTIONS]} />
            <Sel label="Status" value={fStatus} onChange={(e) => setFStatus(e.target.value)} style={{ marginBottom: 0, minWidth: 130 }} options={[{ value: '', label: 'All' }, { value: 'ACTIVE', label: 'Active' }, { value: 'INACTIVE', label: 'Inactive' }]} />
            <Btn variant="primary" onClick={() => setMode({ screen: 'create' })} style={{ marginLeft: 'auto' }}>＋ Add Account</Btn>
          </div>

          <Card>
            {accLoading ? (
              <div style={{ padding: 16 }}><TableSkeleton rows={4} cols={7} /></div>
            ) : filtered.length === 0 ? (
              <div style={{ padding: '48px 24px', textAlign: 'center' }}>
                <div style={{ fontSize: 34, marginBottom: 10 }}>🗂️</div>
                <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: T.textMain }}>{accounts.length === 0 ? 'No accounts yet' : 'No accounts match your filters'}</p>
                <p style={{ margin: '6px 0 16px', fontSize: 13, color: T.textMuted }}>{accounts.length === 0 ? 'Add this agent’s first settlement account.' : 'Try clearing the search or filters.'}</p>
                {accounts.length === 0 && <Btn variant="primary" onClick={() => setMode({ screen: 'create' })}>＋ Add Account</Btn>}
              </div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 980 }}>
                  <thead><tr>{['Account Ref', 'Type', 'Label / Holder', 'Detail', 'Currency', 'Default', 'Status', 'Created (IST)', 'Actions'].map((h) => <th key={h} style={th}>{h}</th>)}</tr></thead>
                  <tbody>
                    {pageRows.map((a) => (
                      <tr key={a.id}>
                        <td style={{ ...td, fontWeight: 700, color: T.blue }}>{a.accountRef}</td>
                        <td style={td}>{ACCOUNT_TYPE_ICON[a.accountType]} {ACCOUNT_TYPE_LABEL[a.accountType]}</td>
                        <td style={td}>{a.label || a.accountHolder || a.upiHolder || '—'}</td>
                        <td style={{ ...td, fontFamily: 'monospace', fontSize: 12 }}>{a.keyDetail || '—'}</td>
                        <td style={td}>{a.currency}</td>
                        <td style={td}>{a.isDefault ? <span style={{ background: T.infoBg, color: T.info, fontSize: 10.5, fontWeight: 800, padding: '2px 8px', borderRadius: 20 }}>DEFAULT</span> : '—'}</td>
                        <td style={td}><StatusPill status={a.status} /></td>
                        <td style={{ ...td, color: T.textMuted, fontSize: 12 }}>{a.createdAt ? formatDateTimeIST(a.createdAt) : '—'}</td>
                        <td style={td}>
                          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            <Btn variant="ghost" size="sm" onClick={() => setMode({ screen: 'view', account: a })}>View</Btn>
                            <Btn variant="secondary" size="sm" onClick={() => setMode({ screen: 'edit', account: a })}>Edit</Btn>
                            {a.status === 'ACTIVE' && !a.isDefault && <Btn variant="ghost" size="sm" disabled={busyId === a.id} onClick={() => makeDefault(a)}>Set Default</Btn>}
                            <Btn variant={a.status === 'ACTIVE' ? 'secondary' : 'success'} size="sm" disabled={busyId === a.id} onClick={() => toggleStatus(a)}>{a.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}</Btn>
                            <Btn variant="danger" size="sm" disabled={busyId === a.id} onClick={() => { setDeleteErr(''); setToDelete(a); }}>Delete</Btn>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {!accLoading && filtered.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderTop: `1px solid ${T.border}`, flexWrap: 'wrap', gap: 10 }}>
                <span style={{ fontSize: 12.5, color: T.textMuted }}>Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}</span>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <Btn variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>← Prev</Btn>
                  <span style={{ fontSize: 12.5, color: T.textMuted }}>Page {page} / {pageCount}</span>
                  <Btn variant="secondary" size="sm" disabled={page >= pageCount} onClick={() => setPage((p) => Math.min(pageCount, p + 1))}>Next →</Btn>
                </div>
              </div>
            )}
          </Card>
        </>
      )}

      {toDelete && (
        <Modal title="Delete Account" onClose={() => setToDelete(null)}>
          {deleteErr ? (
            <div style={{ background: T.warningBg, color: T.warning, borderRadius: 10, padding: '12px 14px', fontSize: 13, fontWeight: 600, marginBottom: 16, lineHeight: 1.5 }}>{deleteErr}</div>
          ) : (
            <p style={{ margin: '0 0 18px', fontSize: 14, color: T.textMain, lineHeight: 1.55 }}>Delete account <b>{toDelete.accountRef}</b> ({ACCOUNT_TYPE_LABEL[toDelete.accountType]})? This cannot be undone.</p>
          )}
          <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end' }}>
            <Btn variant="secondary" onClick={() => setToDelete(null)}>{deleteErr ? 'Close' : 'Cancel'}</Btn>
            {!deleteErr && <Btn variant="danger" disabled={busyId === toDelete.id} onClick={confirmDelete}>{busyId === toDelete.id ? 'Deleting…' : 'Delete Account'}</Btn>}
          </div>
        </Modal>
      )}
    </div>
  );
};

// ═══ Phase 6: Transactions · Unassigned · Audit · Reports ═════════════════════════
const prettyStatus = (s: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const thc: React.CSSProperties = { textAlign: 'left', padding: '11px 12px', fontSize: 11, fontWeight: 800, color: T.textLight, textTransform: 'uppercase', letterSpacing: '0.04em', whiteSpace: 'nowrap' };
const tdc: React.CSSProperties = { padding: '11px 12px', fontSize: 13, color: T.textMain, borderTop: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };
const TXTYPE_FILTER = [{ value: 'DEPOSIT', label: 'Deposit' }, { value: 'WITHDRAWAL', label: 'Withdrawal' }, { value: 'SETTLEMENT', label: 'Settlement' }];

const TxStatusPill: React.FC<{ status: string }> = ({ status }) => {
  const up = status.toUpperCase();
  const good = ['COMPLETED', 'DEPOSITED', 'SUCCESSFUL', 'APPROVED'].some((s) => up.includes(s));
  const bad = ['REJECTED', 'CANCELLED', 'FAILED'].some((s) => up.includes(s));
  return <span style={{ background: good ? T.successBg : bad ? T.dangerBg : T.infoBg, color: good ? T.success : bad ? T.danger : T.info, fontSize: 10.5, fontWeight: 800, padding: '2px 8px', borderRadius: 20, whiteSpace: 'nowrap' }}>{prettyStatus(status)}</span>;
};

const Pager: React.FC<{ page: number; pageCount: number; total: number; onPage: (p: number) => void }> = ({ page, pageCount, total, onPage }) => (
  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderTop: `1px solid ${T.border}`, flexWrap: 'wrap', gap: 10 }}>
    <span style={{ fontSize: 12.5, color: T.textMuted }}>Showing {total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}</span>
    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
      <Btn variant="secondary" size="sm" disabled={page <= 1} onClick={() => onPage(Math.max(1, page - 1))}>← Prev</Btn>
      <span style={{ fontSize: 12.5, color: T.textMuted }}>Page {page} / {pageCount}</span>
      <Btn variant="secondary" size="sm" disabled={page >= pageCount} onClick={() => onPage(Math.min(pageCount, page + 1))}>Next →</Btn>
    </div>
  </div>
);

const AuditTable: React.FC<{ rows: AgentAuditRow[] }> = ({ rows }) => (
  rows.length === 0 ? <div style={{ padding: '32px', textAlign: 'center', color: T.textMuted, fontSize: 13 }}>No audit entries.</div> : (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 760 }}>
        <thead><tr>{['User', 'Role', 'Action', 'Reference', 'Note', 'When (IST)'].map((h) => <th key={h} style={thc}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.id}>
            <td style={tdc}>{r.user || '—'}</td>
            <td style={tdc}>{r.role || '—'}</td>
            <td style={{ ...tdc, fontWeight: 700 }}>{prettyStatus(r.action)}</td>
            <td style={{ ...tdc, color: T.blue, fontWeight: 600 }}>{r.reference || '—'}</td>
            <td style={{ ...tdc, whiteSpace: 'normal', maxWidth: 280, color: T.textMuted }}>{r.note || '—'}</td>
            <td style={{ ...tdc, color: T.textMuted, fontSize: 12 }}>{r.createdAt ? formatDateTimeIST(r.createdAt) : '—'}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
);

export const AgentTransactionsPage: React.FC<AgentPageProps> = () => {
  const [rows, setRows] = useState<AgentTxRow[]>([]);
  const [audit, setAudit] = useState<AgentAuditRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState(''); const [fType, setFType] = useState(''); const [fPay, setFPay] = useState(''); const [fStatus, setFStatus] = useState('');
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<AgentTxRow | null>(null);
  const [history, setHistory] = useState<AgentTxRow | null>(null);
  const [auditRow, setAuditRow] = useState<AgentTxRow | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try { const [a, au] = await Promise.all([agentTransactionAPI.assigned(), agentTransactionAPI.audit()]); setRows(a); setAudit(au); }
    catch { setRows([]); } finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const statuses = useMemo(() => Array.from(new Set(rows.map((r) => r.status))).sort(), [rows]);
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (q && !`${r.ref} ${r.memberId || ''} ${r.memberName || ''} ${r.agentCode || ''} ${r.agentName || ''}`.toLowerCase().includes(q)) return false;
      if (fType && r.type !== fType) return false;
      if (fPay && r.paymentMethod !== fPay) return false;
      if (fStatus && r.status !== fStatus) return false;
      return true;
    });
  }, [rows, search, fType, fPay, fStatus]);
  useEffect(() => { setPage(1); }, [search, fType, fPay, fStatus]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  if (loading) return <LoadingScreen label="Loading transactions…" />;
  return (
    <div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
        <Input label="Search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Ref, membership, player, agent" icon="🔍" style={{ marginBottom: 0, flex: '1 1 260px' }} />
        <Sel label="Type" value={fType} onChange={(e) => setFType(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...TXTYPE_FILTER]} />
        <Sel label="Payment Method" value={fPay} onChange={(e) => setFPay(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...ACCOUNT_TYPE_OPTIONS]} />
        <Sel label="Status" value={fStatus} onChange={(e) => setFStatus(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...statuses.map((s) => ({ value: s, label: prettyStatus(s) }))]} />
        <Btn variant="secondary" size="sm" onClick={load} style={{ marginLeft: 'auto' }}>↻ Refresh</Btn>
      </div>
      <Card>
        {filtered.length === 0 ? <div style={{ padding: '48px', textAlign: 'center', color: T.textMuted }}>No assigned transactions{rows.length ? ' match your filters' : ' yet'}.</div> : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 1200 }}>
              <thead><tr>{['Reference', 'Type', 'Membership', 'Player', 'Agent', 'Account', 'Payment', 'Amount', 'Status', 'Assigned By', 'Assigned (IST)', 'Actions'].map((h) => <th key={h} style={thc}>{h}</th>)}</tr></thead>
              <tbody>{pageRows.map((r) => (
                <tr key={r.id}>
                  <td style={{ ...tdc, fontWeight: 700, color: T.blue }}>{r.ref}</td>
                  <td style={tdc}>{TXTYPE_LABEL[r.type] || r.type}</td>
                  <td style={tdc}>{r.memberId || '—'}</td>
                  <td style={tdc}>{r.memberName || '—'}</td>
                  <td style={tdc}>{r.agentCode} · {r.agentName}</td>
                  <td style={tdc}>{r.accountRef}</td>
                  <td style={tdc}>{ACCOUNT_TYPE_LABEL[r.paymentMethod || ''] || r.paymentMethod || '—'}</td>
                  <td style={{ ...tdc, fontWeight: 700 }}>{fmt(r.amount)}</td>
                  <td style={tdc}><TxStatusPill status={r.status} /></td>
                  <td style={tdc}>{r.assignedBy || '—'}</td>
                  <td style={{ ...tdc, color: T.textMuted, fontSize: 12 }}>{r.assignedAt ? formatDateTimeIST(r.assignedAt) : '—'}</td>
                  <td style={tdc}>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      <Btn variant="ghost" size="sm" onClick={() => setDetail(r)}>Details</Btn>
                      <Btn variant="secondary" size="sm" onClick={() => setHistory(r)}>History</Btn>
                      <Btn variant="ghost" size="sm" onClick={() => setAuditRow(r)}>Audit</Btn>
                    </div>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {filtered.length > 0 && <Pager page={page} pageCount={pageCount} total={filtered.length} onPage={setPage} />}
      </Card>

      {detail && (
        <Modal title={`Transaction ${detail.ref}`} onClose={() => setDetail(null)}>
          <div style={{ ...grid2, rowGap: 16 }}>
            <Field label="Reference" value={detail.ref} />
            <Field label="Type" value={TXTYPE_LABEL[detail.type] || detail.type} />
            <Field label="Membership ID" value={detail.memberId} />
            <Field label="Player Name" value={detail.memberName} />
            <Field label="Assigned Agent" value={`${detail.agentCode} · ${detail.agentName}`} />
            <Field label="Assigned Account" value={detail.accountRef} />
            <Field label="Payment Method" value={ACCOUNT_TYPE_LABEL[detail.paymentMethod || ''] || detail.paymentMethod} />
            <Field label="Amount" value={fmt(detail.amount)} />
            <Field label="Status" value={prettyStatus(detail.status)} />
            <Field label="Assigned By" value={detail.assignedBy} />
            <Field label="Assigned Date & Time" value={detail.assignedAt ? formatDateTimeIST(detail.assignedAt) : '—'} />
            <Field label="Created By" value={detail.createdBy} />
          </div>
        </Modal>
      )}
      {history && (
        <Modal title={`Assignment History — ${history.ref}`} onClose={() => setHistory(null)}>
          <AgentAssignmentPanel txRef={history.ref} txType={history.typeFull} readOnly />
        </Modal>
      )}
      {auditRow && (
        <Modal title={`Audit Trail — ${auditRow.ref}`} onClose={() => setAuditRow(null)} wide>
          <AuditTable rows={audit.filter((a) => a.reference === auditRow.ref)} />
        </Modal>
      )}
    </div>
  );
};

export const UnassignedTransactionsPage: React.FC<AgentPageProps> = () => {
  const [rows, setRows] = useState<AgentTxRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState(''); const [fType, setFType] = useState(''); const [fStatus, setFStatus] = useState('');
  const [page, setPage] = useState(1);
  const [assignRow, setAssignRow] = useState<AgentTxRow | null>(null);
  const [sel, setSel] = useState<AgentAssignSelection>(emptyAgentAssignSelection);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(''); const [banner, setBanner] = useState('');
  const flash = (m: string) => { setBanner(m); window.setTimeout(() => setBanner(''), 3500); };

  const load = useCallback(async () => { setLoading(true); try { setRows(await agentTransactionAPI.unassigned()); } catch { setRows([]); } finally { setLoading(false); } }, []);
  useEffect(() => { load(); }, [load]);

  const statuses = useMemo(() => Array.from(new Set(rows.map((r) => r.status))).sort(), [rows]);
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (q && !`${r.ref} ${r.memberId || ''} ${r.memberName || ''}`.toLowerCase().includes(q)) return false;
      if (fType && r.type !== fType) return false;
      if (fStatus && r.status !== fStatus) return false;
      return true;
    });
  }, [rows, search, fType, fStatus]);
  useEffect(() => { setPage(1); }, [search, fType, fStatus]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const confirmAssign = async () => {
    if (!assignRow) return;
    if (!sel.agentId || !sel.accountId) { setErr('Select an Agent and an Agent Account.'); return; }
    setSaving(true); setErr('');
    try {
      await agentAssignmentAPI.assign(assignRow.ref, { agentId: Number(sel.agentId), agentAccountId: Number(sel.accountId), paymentMethod: sel.accountType });
      setRows((l) => l.filter((x) => x.id !== assignRow.id));
      flash(`Agent assigned to ${assignRow.ref}.`);
      setAssignRow(null);
    } catch (e) { setErr(errText(e)); } finally { setSaving(false); }
  };

  if (loading) return <LoadingScreen label="Loading unassigned transactions…" />;
  return (
    <div>
      {banner && <div style={{ background: T.successBg, color: T.success, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 14 }}>{banner}</div>}
      <div style={{ background: T.warningBg, color: T.warning, borderRadius: 10, padding: '10px 14px', fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>
        These Deposit / Withdrawal / Settlement requests have no assigned agent. Assign one to complete the record.
      </div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
        <Input label="Search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Ref, membership, player" icon="🔍" style={{ marginBottom: 0, flex: '1 1 260px' }} />
        <Sel label="Type" value={fType} onChange={(e) => setFType(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...TXTYPE_FILTER]} />
        <Sel label="Status" value={fStatus} onChange={(e) => setFStatus(e.target.value)} style={{ marginBottom: 0, minWidth: 150 }} options={[{ value: '', label: 'All' }, ...statuses.map((s) => ({ value: s, label: prettyStatus(s) }))]} />
        <Btn variant="secondary" size="sm" onClick={load} style={{ marginLeft: 'auto' }}>↻ Refresh</Btn>
      </div>
      <Card>
        {filtered.length === 0 ? (
          <div style={{ padding: '48px', textAlign: 'center', color: T.textMuted }}>
            <div style={{ fontSize: 34, marginBottom: 8 }}>✅</div>
            <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: T.textMain }}>{rows.length ? 'No transactions match your filters' : 'Every transaction is assigned'}</p>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 980 }}>
              <thead><tr>{['Reference', 'Type', 'Membership', 'Player', 'Amount', 'Status', 'Created By', 'Created (IST)', 'Action'].map((h) => <th key={h} style={thc}>{h}</th>)}</tr></thead>
              <tbody>{pageRows.map((r) => (
                <tr key={r.id}>
                  <td style={{ ...tdc, fontWeight: 700, color: T.blue }}>{r.ref}</td>
                  <td style={tdc}>{TXTYPE_LABEL[r.type] || r.type}</td>
                  <td style={tdc}>{r.memberId || '—'}</td>
                  <td style={tdc}>{r.memberName || '—'}</td>
                  <td style={{ ...tdc, fontWeight: 700 }}>{fmt(r.amount)}</td>
                  <td style={tdc}><TxStatusPill status={r.status} /></td>
                  <td style={tdc}>{r.createdBy || '—'}</td>
                  <td style={{ ...tdc, color: T.textMuted, fontSize: 12 }}>{r.createdAt ? formatDateTimeIST(r.createdAt) : `${r.txDate || ''} ${r.txTime || ''}`}</td>
                  <td style={tdc}><Btn variant="primary" size="sm" onClick={() => { setErr(''); setSel(emptyAgentAssignSelection); setAssignRow(r); }}>Assign Agent</Btn></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {filtered.length > 0 && <Pager page={page} pageCount={pageCount} total={filtered.length} onPage={setPage} />}
      </Card>

      {assignRow && (
        <Modal title={`Assign Agent — ${assignRow.ref}`} onClose={() => setAssignRow(null)} wide>
          <p style={{ margin: '0 0 6px', fontSize: 13, color: T.textMuted }}>{TXTYPE_LABEL[assignRow.type] || assignRow.type} · {fmt(assignRow.amount)} · {assignRow.memberName || '—'} ({assignRow.memberId || '—'})</p>
          {err && <div style={{ background: T.dangerBg, color: T.danger, borderRadius: 8, padding: '8px 12px', fontSize: 12.5, fontWeight: 600, margin: '10px 0' }}>{err}</div>}
          <AgentAssignmentSelect value={sel} onChange={setSel} />
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <Btn variant="secondary" onClick={() => setAssignRow(null)} disabled={saving}>Cancel</Btn>
            <Btn variant="primary" onClick={confirmAssign} disabled={saving || !sel.agentId || !sel.accountId}>{saving ? 'Assigning…' : 'Confirm Assignment'}</Btn>
          </div>
        </Modal>
      )}
    </div>
  );
};

export const AgentAuditPage: React.FC<AgentPageProps> = () => {
  const [rows, setRows] = useState<AgentAuditRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState(''); const [fAction, setFAction] = useState('');
  const [page, setPage] = useState(1);
  const load = useCallback(async () => { setLoading(true); try { setRows(await agentTransactionAPI.audit()); } catch { setRows([]); } finally { setLoading(false); } }, []);
  useEffect(() => { load(); }, [load]);
  const actions = useMemo(() => Array.from(new Set(rows.map((r) => r.action))).sort(), [rows]);
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (q && !`${r.action} ${r.reference || ''} ${r.note || ''} ${r.role || ''}`.toLowerCase().includes(q)) return false;
      if (fAction && r.action !== fAction) return false;
      return true;
    });
  }, [rows, search, fAction]);
  useEffect(() => { setPage(1); }, [search, fAction]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  if (loading) return <LoadingScreen label="Loading audit trail…" />;
  return (
    <div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
        <Input label="Search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Action, reference, note" icon="🔍" style={{ marginBottom: 0, flex: '1 1 260px' }} />
        <Sel label="Action" value={fAction} onChange={(e) => setFAction(e.target.value)} style={{ marginBottom: 0, minWidth: 220 }} options={[{ value: '', label: 'All' }, ...actions.map((a) => ({ value: a, label: prettyStatus(a) }))]} />
        <Btn variant="secondary" size="sm" onClick={load} style={{ marginLeft: 'auto' }}>↻ Refresh</Btn>
      </div>
      <Card>
        <AuditTable rows={pageRows} />
        {filtered.length > 0 && <Pager page={page} pageCount={pageCount} total={filtered.length} onPage={setPage} />}
      </Card>
    </div>
  );
};

// Reports — CSV/Excel export reusing the project's xlsx utility + downloadText.
const csvOf = <T,>(cols: Col<T>[], rows: T[]): string => {
  const esc = (v: unknown) => { const s = String(v ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  return [cols.map((c) => esc(c.header)).join(','), ...rows.map((r) => cols.map((c) => esc(c.get(r))).join(','))].join('\n');
};
const emitReport = <T,>(name: string, cols: Col<T>[], rows: T[], format: 'csv' | 'xlsx', stamp: string) => {
  const file = `${name}_${stamp}`;
  if (format === 'csv') downloadText(csvOf(cols, rows), `${file}.csv`);
  else downloadXlsx(`${file}.xlsx`, [{ name: name.replace(/_/g, ' ').slice(0, 31), columns: cols, rows }]);
};

export const AgentReportsPage: React.FC<AgentPageProps> = () => {
  const [busy, setBusy] = useState('');
  const [banner, setBanner] = useState('');
  const flash = (m: string) => { setBanner(m); window.setTimeout(() => setBanner(''), 3500); };

  const run = async (kind: 'agents' | 'accounts' | 'history' | 'transactions', format: 'csv' | 'xlsx') => {
    setBusy(`${kind}-${format}`);
    const stamp = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
    try {
      if (kind === 'agents') {
        const data = await agentAPI.list();
        emitReport<Agent>('Agent_Master', [
          { header: 'Agent ID', get: (a) => a.agentId }, { header: 'Full Name', get: (a) => a.fullName },
          { header: 'Country', get: (a) => a.country }, { header: 'State', get: (a) => a.state }, { header: 'Location', get: (a) => a.location },
          { header: 'Category', get: (a) => CATEGORY_LABEL[a.category] || a.category }, { header: 'Currency', get: (a) => a.currency },
          { header: 'Fees %', get: (a) => a.feesPct }, { header: 'Txn Code', get: (a) => a.transactionCode }, { header: 'Status', get: (a) => a.status },
          { header: 'Created By', get: (a) => a.createdBy || '' }, { header: 'Created At (IST)', get: (a) => a.createdAt ? formatDateTimeIST(a.createdAt) : '' },
        ], data, format, stamp);
      } else if (kind === 'accounts') {
        const data = await agentTransactionAPI.allAccounts();
        emitReport<Record<string, unknown>>('Agent_Accounts', [
          { header: 'Account Ref', get: (r) => r.accountRef }, { header: 'Agent', get: (r) => `${r.agentCode || ''} ${r.agentName || ''}`.trim() },
          { header: 'Type', get: (r) => ACCOUNT_TYPE_LABEL[String(r.accountType)] || r.accountType }, { header: 'Label', get: (r) => r.label || '' },
          { header: 'Currency', get: (r) => r.currency }, { header: 'Default', get: (r) => r.isDefault ? 'Yes' : 'No' }, { header: 'Status', get: (r) => r.status },
          { header: 'Detail', get: (r) => r.detail || '' }, { header: 'Created By', get: (r) => r.createdBy || '' },
          { header: 'Created At (IST)', get: (r) => r.createdAt ? formatDateTimeIST(String(r.createdAt)) : '' },
        ], data, format, stamp);
      } else if (kind === 'history') {
        const data = await agentTransactionAPI.assignmentHistory();
        emitReport('Assignment_History', [
          { header: 'Action', get: (r) => r.action }, { header: 'Txn Ref', get: (r) => r.txRef }, { header: 'Type', get: (r) => r.txType },
          { header: 'Prev Agent', get: (r) => r.prevAgentId || '' }, { header: 'New Agent', get: (r) => r.newAgentId },
          { header: 'Prev Account', get: (r) => r.prevAccountRef || '' }, { header: 'New Account', get: (r) => r.newAccountRef },
          { header: 'Channel', get: (r) => r.newAccountType }, { header: 'Assigned By', get: (r) => r.assignedBy || '' },
          { header: 'When (IST)', get: (r) => r.createdAt ? formatDateTimeIST(r.createdAt) : '' }, { header: 'Note', get: (r) => r.note || '' },
        ], data, format, stamp);
      } else {
        const data = await agentTransactionAPI.assigned();
        emitReport<AgentTxRow>('Transaction_History', [
          { header: 'Reference', get: (r) => r.ref }, { header: 'Type', get: (r) => r.type }, { header: 'Membership ID', get: (r) => r.memberId || '' },
          { header: 'Player', get: (r) => r.memberName || '' }, { header: 'Agent', get: (r) => `${r.agentCode || ''} ${r.agentName || ''}`.trim() },
          { header: 'Account', get: (r) => r.accountRef || '' }, { header: 'Payment Method', get: (r) => r.paymentMethod || '' },
          { header: 'Amount', get: (r) => r.amount }, { header: 'Status', get: (r) => r.status }, { header: 'Assigned By', get: (r) => r.assignedBy || '' },
          { header: 'Assigned At (IST)', get: (r) => r.assignedAt ? formatDateTimeIST(r.assignedAt) : '' },
        ], data, format, stamp);
      }
      flash('Export ready — check your downloads.');
    } catch { flash('Export failed. Please try again.'); }
    finally { setBusy(''); }
  };

  const items: Array<{ kind: 'agents' | 'accounts' | 'history' | 'transactions'; icon: string; title: string; desc: string }> = [
    { kind: 'agents', icon: '🧑‍💼', title: 'Agent Master', desc: 'All agents with their details and status.' },
    { kind: 'accounts', icon: '🏦', title: 'Agent Accounts', desc: 'All Bank / UPI / QR / Crypto accounts across agents.' },
    { kind: 'history', icon: '🔁', title: 'Assignment History', desc: 'Every assignment and reassignment.' },
    { kind: 'transactions', icon: '≡', title: 'Transaction History', desc: 'All transactions assigned to an agent.' },
  ];
  return (
    <div>
      {banner && <div style={{ background: T.successBg, color: T.success, borderRadius: 10, padding: '10px 14px', fontSize: 13, fontWeight: 600, marginBottom: 14 }}>{banner}</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 14 }}>
        {items.map((it) => (
          <Card key={it.kind} style={{ padding: '18px 18px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <div style={{ width: 42, height: 42, borderRadius: 12, background: T.grad1, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20 }}>{it.icon}</div>
              <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>{it.title}</p>
            </div>
            <p style={{ margin: '0 0 14px', fontSize: 12.5, color: T.textMuted, lineHeight: 1.5 }}>{it.desc}</p>
            <div style={{ display: 'flex', gap: 10 }}>
              <Btn variant="secondary" size="sm" disabled={busy === `${it.kind}-csv`} onClick={() => run(it.kind, 'csv')}>{busy === `${it.kind}-csv` ? '…' : '⬇ CSV'}</Btn>
              <Btn variant="primary" size="sm" disabled={busy === `${it.kind}-xlsx`} onClick={() => run(it.kind, 'xlsx')}>{busy === `${it.kind}-xlsx` ? '…' : '⬇ Excel'}</Btn>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
};
