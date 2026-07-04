import React, { useEffect, useMemo, useRef, useState } from 'react';
import { T } from '../utils/theme';
import { Card, StatCard, Sel, Input, Btn, Modal, Skeleton, CountUp, ReasonModal } from '../components/UI';
import { supportManagementAPI } from '../services/api';
import { usePoll, PRESENCE_POLL_MS } from '../utils/usePoll';
import { openSSE } from '../utils/sse';
import type { User, SupportMembersData, SupportMemberRow, SupportStatus, AssignableMerchant } from '../types';

const DEPARTMENTS = ['Technical Support', 'Payments', 'Merchant Support', 'Finance', 'Compliance'];
const SHIFTS = ['Morning', 'Afternoon', 'Night'];
// Common country dial codes for the phone selector (value = dial code with '+').
const COUNTRY_CODES = [
  { value: '+91', label: '🇮🇳 +91' }, { value: '+1', label: '🇺🇸 +1' }, { value: '+44', label: '🇬🇧 +44' },
  { value: '+971', label: '🇦🇪 +971' }, { value: '+65', label: '🇸🇬 +65' }, { value: '+61', label: '🇦🇺 +61' },
  { value: '+49', label: '🇩🇪 +49' }, { value: '+33', label: '🇫🇷 +33' }, { value: '+234', label: '🇳🇬 +234' },
  { value: '+92', label: '🇵🇰 +92' }, { value: '+880', label: '🇧🇩 +880' }, { value: '+94', label: '🇱🇰 +94' },
];

// ── formatting helpers (kept local to mirror ActiveUsersPage) ──
const relTime = (iso?: string | null): string => {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 45) return 'Just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hr ago`;
  const d = new Date(iso);
  if (s < 172800) return `Yesterday ${d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
};
const fmtTime = (iso?: string | null) => iso ? new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
const fmtDate = (iso?: string | null) => iso ? new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) : '—';
const fmtDuration = (secs?: number | null): string => {
  if (secs == null) return '—';
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
};

const STATUS_META: Record<SupportStatus, { label: string; color: string }> = {
  online: { label: 'Online', color: T.success },
  busy: { label: 'Busy', color: T.warning },
  break: { label: 'On Break', color: T.danger },
  offline: { label: 'Offline', color: T.textMuted },
};
const AVAILABILITY_LABEL: Record<string, string> = { AVAILABLE: 'Available', BUSY: 'Busy', ON_BREAK: 'On Break' };
const availColor = (a?: string) => a === 'BUSY' ? T.warning : a === 'ON_BREAK' ? T.danger : T.success;

const StatusDot: React.FC<{ status: SupportStatus }> = ({ status }) => {
  const m = STATUS_META[status];
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 700, color: m.color }}>
      <span style={{ width: 9, height: 9, borderRadius: '50%', background: m.color, boxShadow: status !== 'offline' ? `0 0 0 3px ${m.color}22` : 'none' }} />
      {m.label}
    </span>
  );
};

const th: React.CSSProperties = { textAlign: 'left', padding: '10px 12px', fontSize: 10.5, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', whiteSpace: 'nowrap', borderBottom: `1px solid ${T.border}` };
const td: React.CSSProperties = { padding: '10px 12px', fontSize: 12.5, color: T.textMain, borderBottom: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };
const rowLine: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', gap: 16, padding: '8px 0', borderBottom: `1px solid ${T.borderLight}` };
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ── Merchant multiselect (checkbox list) ──
const MerchantMultiSelect: React.FC<{
  options: AssignableMerchant[]; selected: number[]; onChange: (ids: number[]) => void;
}> = ({ options, selected, onChange }) => {
  const [q, setQ] = useState('');
  const filtered = options.filter(o => o.name.toLowerCase().includes(q.trim().toLowerCase()));
  const toggle = (id: number) => onChange(selected.includes(id) ? selected.filter(x => x !== id) : [...selected, id]);
  return (
    <div>
      <Input label="Assigned Merchant(s)" value={q} onChange={e => setQ(e.target.value)} placeholder="Search merchants…" style={{ marginBottom: 8 }} />
      <div style={{ maxHeight: 180, overflowY: 'auto', border: `1.5px solid ${T.border}`, borderRadius: 10, padding: 6 }}>
        {options.length === 0 && <p style={{ fontSize: 12, color: T.textMuted, padding: 8, margin: 0 }}>No merchants available to assign.</p>}
        {filtered.map(o => (
          <label key={o.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderRadius: 8, cursor: 'pointer', fontSize: 13, color: T.textMain }} className="c5-row-hover">
            <input type="checkbox" checked={selected.includes(o.id)} onChange={() => toggle(o.id)} />
            <span style={{ fontWeight: 600 }}>{o.name}</span>
            {o.merchantCode && <span style={{ fontSize: 11, color: T.textMuted }}>· {o.merchantCode}</span>}
          </label>
        ))}
      </div>
      <p style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>{selected.length} merchant(s) selected</p>
    </div>
  );
};

// ── Create modal ──
const CreateModal: React.FC<{ merchants: AssignableMerchant[]; onClose: () => void; onCreated: () => void }> = ({ merchants, onClose, onCreated }) => {
  const [f, setF] = useState({ fullName: '', username: '', email: '', dial: '+91', phone: '', password: '', confirm: '', department: '', shift: SHIFTS[0], status: 'Active' });
  const [ids, setIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const set = (k: keyof typeof f, v: string) => setF(p => ({ ...p, [k]: v }));

  const phoneDigits = f.phone.replace(/\D/g, '');
  const emailOk = EMAIL_RE.test(f.email);
  const phoneOk = phoneDigits.length >= 6 && phoneDigits.length <= 14;
  const pwOk = f.password.length >= 8 && f.password === f.confirm;
  const valid = f.fullName.trim() && f.username.trim() && emailOk && phoneOk && pwOk;

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true); setErr('');
    try {
      await supportManagementAPI.create({
        username: f.username.trim(), password: f.password, email: f.email.trim(), fullName: f.fullName.trim(),
        phone: `${f.dial}${phoneDigits}`, department: f.department, shift: f.shift, status: f.status, merchantIds: ids,
      });
      onCreated(); onClose();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setErr(ax.response?.data?.detail || 'Could not create support member.');
    } finally { setBusy(false); }
  };

  return (
    <Modal title="Create Support Member" onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: '0 18px' }}>
        <Input label="Support ID" value="Auto-generated (SUP…)" onChange={() => {}} readOnly />
        <Input label="Full Name" value={f.fullName} onChange={e => set('fullName', e.target.value)} required />
        <Input label="Username" value={f.username} onChange={e => set('username', e.target.value)} required />
        <Input label="Email ID" value={f.email} onChange={e => set('email', e.target.value)} required hint={f.email && !emailOk ? 'Enter a valid email' : undefined} />
        <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', gap: 8 }}>
          <Sel label="Code" value={f.dial} onChange={e => set('dial', e.target.value)} options={COUNTRY_CODES} />
          <Input label="Phone Number" value={f.phone} onChange={e => set('phone', e.target.value)} required inputMode="tel" hint={f.phone && !phoneOk ? 'Enter a valid number' : undefined} />
        </div>
        <Sel label="Department" value={f.department} onChange={e => set('department', e.target.value)} options={[{ value: '', label: 'Select department' }, ...DEPARTMENTS.map(d => ({ value: d, label: d }))]} />
        <Input label="Password" type="password" value={f.password} onChange={e => set('password', e.target.value)} required hint={f.password && f.password.length < 8 ? 'Min 8 characters' : undefined} />
        <Input label="Confirm Password" type="password" value={f.confirm} onChange={e => set('confirm', e.target.value)} required hint={f.confirm && f.password !== f.confirm ? 'Passwords do not match' : undefined} />
        <Sel label="Shift" value={f.shift} onChange={e => set('shift', e.target.value)} options={SHIFTS.map(s => ({ value: s, label: s }))} />
        <Sel label="Status" value={f.status} onChange={e => set('status', e.target.value)} options={[{ value: 'Active', label: 'Active' }, { value: 'Inactive', label: 'Inactive' }]} />
      </div>
      <div style={{ marginTop: 4, marginBottom: 8 }}>
        <MerchantMultiSelect options={merchants} selected={ids} onChange={setIds} />
      </div>
      {err && <p style={{ color: T.danger, fontSize: 12.5, fontWeight: 600, margin: '4px 0 10px' }}>{err}</p>}
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn onClick={submit} disabled={!valid || busy}>{busy ? 'Creating…' : 'Create Support Member'}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// ── Edit modal ──
const EditModal: React.FC<{ member: SupportMemberRow; onClose: () => void; onSaved: () => void }> = ({ member, onClose, onSaved }) => {
  const [f, setF] = useState({ fullName: member.fullName, email: member.email, phone: member.phone || '', department: member.department || '', shift: member.shift || '' });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const set = (k: keyof typeof f, v: string) => setF(p => ({ ...p, [k]: v }));
  const emailOk = EMAIL_RE.test(f.email);
  const submit = async () => {
    if (!emailOk || busy) return;
    setBusy(true); setErr('');
    try {
      await supportManagementAPI.update(member.id, { fullName: f.fullName.trim(), email: f.email.trim(), phone: f.phone, department: f.department, shift: f.shift });
      onSaved(); onClose();
    } catch (e: unknown) {
      const ax = e as { response?: { data?: { detail?: string } } };
      setErr(ax.response?.data?.detail || 'Could not save changes.');
    } finally { setBusy(false); }
  };
  return (
    <Modal title={`Edit — ${member.fullName}`} onClose={onClose}>
      <Input label="Full Name" value={f.fullName} onChange={e => set('fullName', e.target.value)} required />
      <Input label="Email ID" value={f.email} onChange={e => set('email', e.target.value)} required hint={f.email && !emailOk ? 'Enter a valid email' : undefined} />
      <Input label="Phone Number" value={f.phone} onChange={e => set('phone', e.target.value)} placeholder="+919812345678" inputMode="tel" />
      <Sel label="Department" value={f.department} onChange={e => set('department', e.target.value)} options={[{ value: '', label: '—' }, ...DEPARTMENTS.map(d => ({ value: d, label: d }))]} />
      <Sel label="Shift" value={f.shift} onChange={e => set('shift', e.target.value)} options={[{ value: '', label: '—' }, ...SHIFTS.map(s => ({ value: s, label: s }))]} />
      {err && <p style={{ color: T.danger, fontSize: 12.5, fontWeight: 600, margin: '4px 0 10px' }}>{err}</p>}
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn onClick={submit} disabled={!emailOk || busy}>{busy ? 'Saving…' : 'Save Changes'}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// ── Assign merchants modal ──
const AssignModal: React.FC<{ member: SupportMemberRow; merchants: AssignableMerchant[]; onClose: () => void; onSaved: () => void }> = ({ member, merchants, onClose, onSaved }) => {
  const [ids, setIds] = useState<number[]>(member.assignedMerchants.map(m => m.id));
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try { await supportManagementAPI.assignMerchants(member.id, ids); onSaved(); onClose(); }
    finally { setBusy(false); }
  };
  return (
    <Modal title={`Assign Merchants — ${member.fullName}`} onClose={onClose}>
      <MerchantMultiSelect options={merchants} selected={ids} onChange={setIds} />
      <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
        <Btn onClick={submit} disabled={busy}>{busy ? 'Saving…' : 'Save Assignments'}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// ── Reset password modal ──
const ResetModal: React.FC<{ member: SupportMemberRow; onClose: () => void; onDone: () => void }> = ({ member, onClose, onDone }) => {
  const [pw, setPw] = useState(''); const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false); const [err, setErr] = useState('');
  const ok = pw.length >= 8 && pw === confirm;
  const submit = async () => {
    if (!ok || busy) return;
    setBusy(true); setErr('');
    try { await supportManagementAPI.resetPassword(member.id, pw); onDone(); onClose(); }
    catch (e: unknown) { const ax = e as { response?: { data?: { detail?: string } } }; setErr(ax.response?.data?.detail || 'Could not reset password.'); }
    finally { setBusy(false); }
  };
  return (
    <Modal title={`Reset Password — ${member.fullName}`} onClose={onClose}>
      <Input label="New Password" type="password" value={pw} onChange={e => setPw(e.target.value)} required hint={pw && pw.length < 8 ? 'Min 8 characters' : undefined} />
      <Input label="Confirm Password" type="password" value={confirm} onChange={e => setConfirm(e.target.value)} required hint={confirm && pw !== confirm ? 'Passwords do not match' : undefined} />
      {err && <p style={{ color: T.danger, fontSize: 12.5, fontWeight: 600, margin: '4px 0 10px' }}>{err}</p>}
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn onClick={submit} disabled={!ok || busy}>{busy ? 'Resetting…' : 'Reset Password'}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// ── Profile drawer ──
const ProfileDrawer: React.FC<{
  member: SupportMemberRow; isSuperAdmin: boolean; merchants: AssignableMerchant[]; activeConversations: number | null;
  onClose: () => void; onChanged: () => void;
}> = ({ member, isSuperAdmin, merchants, activeConversations, onClose, onChanged }) => {
  const [sub, setSub] = useState<'' | 'edit' | 'assign' | 'reset' | 'toggle' | 'delete'>('');
  const done = () => { onChanged(); setSub(''); };
  const rows: [string, React.ReactNode][] = [
    ['Support ID', member.supportCode || '—'],
    ['Username', `@${member.username}`],
    ['Email', member.email || '—'],
    ['Phone', member.phone || '—'],
    ['Department', member.department || '—'],
    ['Shift', member.shift || '—'],
    ['Status', member.active ? 'Active' : 'Inactive'],
    ['Availability', <StatusDot status={member.status} />],
    ['Assigned Merchants', member.assignedMerchants.length ? member.assignedMerchants.map(m => m.name).join(', ') : '—'],
    ['Total Assigned Merchants', member.assignedMerchantCount],
    ['Active Conversations', activeConversations == null ? '…' : activeConversations],
    ['Last Login', fmtTime(member.loginTime)],
    ['Last Activity', relTime(member.lastActivity)],
    ['Last Seen', relTime(member.lastSeen)],
    ['Current Session', member.currentSession ? 'Active now' : '—'],
    ['Session Duration', fmtDuration(member.sessionDuration)],
    ['Browser', member.browser || '—'],
    ['Device', member.device || '—'],
    ['IP Address', member.ip || '—'],
    ['Created', fmtDate(member.createdAt || member.created)],
  ];
  return (
    <Modal title={`${member.fullName} — Support Profile`} onClose={onClose}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
        <div style={{ width: 54, height: 54, borderRadius: '50%', background: T.canvas, border: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', fontSize: 20, fontWeight: 800, color: T.textMuted }}>
          {member.avatar ? <img src={member.avatar} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : (member.fullName[0] || '?').toUpperCase()}
        </div>
        <div>
          <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>{member.fullName}</p>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>{member.supportCode || ''}</p>
          <div style={{ marginTop: 4 }}><StatusDot status={member.status} /></div>
        </div>
      </div>
      {rows.map(([k, v], i) => (
        <div key={i} style={rowLine}>
          <span style={{ fontSize: 12.5, color: T.textMuted }}>{k}</span>
          <span style={{ fontSize: 12.5, fontWeight: 700, color: T.textMain, textAlign: 'right', wordBreak: 'break-word' }}>{v}</span>
        </div>
      ))}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 16 }}>
        <Btn size="sm" onClick={() => setSub('edit')}>Edit</Btn>
        <Btn size="sm" variant="secondary" onClick={() => setSub('assign')}>Assign Merchants</Btn>
        <Btn size="sm" variant="secondary" onClick={() => setSub('reset')}>Reset Password</Btn>
        <Btn size="sm" variant={member.active ? 'danger' : 'success'} onClick={() => setSub('toggle')}>{member.active ? 'Deactivate' : 'Activate'}</Btn>
        {isSuperAdmin && <Btn size="sm" variant="danger" onClick={() => setSub('delete')}>Delete</Btn>}
      </div>

      {sub === 'edit' && <EditModal member={member} onClose={() => setSub('')} onSaved={done} />}
      {sub === 'assign' && <AssignModal member={member} merchants={merchants} onClose={() => setSub('')} onSaved={done} />}
      {sub === 'reset' && <ResetModal member={member} onClose={() => setSub('')} onDone={() => setSub('')} />}
      {sub === 'toggle' && (
        <ReasonModal
          title={`${member.active ? 'Deactivate' : 'Activate'} ${member.fullName}`}
          message={`Provide a reason to ${member.active ? 'deactivate' : 'activate'} this support member.`}
          confirmLabel={member.active ? 'Deactivate' : 'Activate'}
          onClose={() => setSub('')}
          onSubmit={async (reason) => { await supportManagementAPI.toggle(member.id, reason); done(); }}
        />
      )}
      {sub === 'delete' && (
        <Modal title={`Delete ${member.fullName}?`} onClose={() => setSub('')}>
          <p style={{ fontSize: 13, color: T.textMuted, marginBottom: 16 }}>
            This removes the support member from all lists and revokes their access. The record is archived (preserved for audit), not permanently erased.
          </p>
          <div style={{ display: 'flex', gap: 10 }}>
            <Btn variant="danger" onClick={async () => { await supportManagementAPI.archive(member.id); onChanged(); onClose(); }}>Delete</Btn>
            <Btn variant="secondary" onClick={() => setSub('')}>Cancel</Btn>
          </div>
        </Modal>
      )}
    </Modal>
  );
};

// ── Main page ──
export const SupportManagementPage: React.FC<{ user: User }> = ({ user }) => {
  const isSuperAdmin = user.role === 'SUPER_ADMIN';
  const [data, setData] = useState<SupportMembersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [merchants, setMerchants] = useState<AssignableMerchant[]>([]);
  const [selId, setSelId] = useState<number | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  // Filters / search / sort
  const [status, setStatus] = useState('ALL');
  const [availability, setAvailability] = useState('ALL');
  const [department, setDepartment] = useState('ALL');
  const [shift, setShift] = useState('ALL');
  const [merchant, setMerchant] = useState('ALL');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<'name' | 'loginTime' | 'lastSeen' | 'department'>('name');

  const reload = () => supportManagementAPI.list().then(setData).catch(() => {});
  const loadMerchants = () => supportManagementAPI.assignableMerchants().then(setMerchants).catch(() => {});
  useEffect(() => { reload().finally(() => setLoading(false)); loadMerchants(); }, []);

  // Live SSE stream; polling is the fallback only while the stream is down.
  const [live, setLive] = useState(false);
  const cb = useRef<(d: SupportMembersData) => void>(() => {});
  cb.current = (d) => { setData(d); setLoading(false); };
  useEffect(() => {
    const conn = openSSE('/api/support-management/agents/stream', {
      onMessage: d => cb.current(d as SupportMembersData),
      onOpen: () => setLive(true),
      onError: () => setLive(false),
    });
    return () => conn.close();
  }, []);
  usePoll(() => { if (!live) reload(); }, PRESENCE_POLL_MS);

  const rows = data?.members || [];
  const sel = useMemo(() => rows.find(r => r.id === selId) || null, [rows, selId]);

  // Fetch the richer profile (Active Conversations) when a member drawer opens.
  const [convos, setConvos] = useState<number | null>(null);
  useEffect(() => {
    if (selId == null) { setConvos(null); return; }
    setConvos(null);
    supportManagementAPI.profile(selId).then(p => setConvos(p.activeConversations ?? 0)).catch(() => setConvos(null));
  }, [selId]);

  const merchantOpts = useMemo(() => {
    const names = new Set<string>();
    rows.forEach(r => r.assignedMerchants.forEach(m => names.add(m.name)));
    return Array.from(names).sort();
  }, [rows]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = rows.filter(r =>
      (status === 'ALL' || r.status === status) &&
      (availability === 'ALL' || r.availability === availability) &&
      (department === 'ALL' || (r.department || '') === department) &&
      (shift === 'ALL' || (r.shift || '') === shift) &&
      (merchant === 'ALL' || r.assignedMerchants.some(m => m.name === merchant)) &&
      (!q || [r.supportCode, r.fullName, r.username, r.email, r.phone].some(v => (v || '').toLowerCase().includes(q)))
    );
    const cmp: Record<string, (a: SupportMemberRow, b: SupportMemberRow) => number> = {
      name: (a, b) => (a.fullName || '').localeCompare(b.fullName || ''),
      loginTime: (a, b) => (b.loginTime || '').localeCompare(a.loginTime || ''),
      lastSeen: (a, b) => (b.lastSeen || '').localeCompare(a.lastSeen || ''),
      department: (a, b) => (a.department || '~').localeCompare(b.department || '~'),
    };
    return [...list].sort(cmp[sortKey]);
  }, [rows, status, availability, department, shift, merchant, search, sortKey]);

  const s = data?.summary;

  if (loading) return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 14 }}>
      {[0, 1, 2, 3].map(i => <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={70} h={26} /></Card>)}
    </div>
  );

  return (
    <div>
      {/* Header + create */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <p style={{ fontSize: 13, color: T.textMuted, margin: 0 }}>
          Manage your support team, assign merchants, and track real-time availability.
        </p>
        <Btn onClick={() => { loadMerchants(); setShowCreate(true); }}>+ Create Support Member</Btn>
      </div>

      {/* Cards */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 14, marginBottom: 18 }}>
        <StatCard icon="🎧" label="Support Members" value={<CountUp value={s?.members ?? 0} />} color={T.blue} />
        <StatCard icon="🟢" label="Online" value={<CountUp value={s?.online ?? 0} />} color={T.success} />
        <StatCard icon="🟡" label="Busy" value={<CountUp value={s?.busy ?? 0} />} color={T.warning} />
        <StatCard icon="🔴" label="On Break" value={<CountUp value={s?.onBreak ?? 0} />} color={T.danger} />
        <StatCard icon="⚪" label="Offline" value={<CountUp value={s?.offline ?? 0} />} color={T.textMuted} />
        <StatCard icon="🏢" label="Assigned Merchants" value={<CountUp value={s?.assignedMerchants ?? 0} />} color={T.blue} />
        <StatCard icon="🎫" label="Open Tickets" value={s?.openTickets ?? 0} sub="Coming soon" color={T.textMuted} />
      </div>

      {/* Filters */}
      <Card style={{ padding: 14, marginBottom: 12 }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ flex: '1 1 220px', minWidth: 180 }}>
            <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Support ID, name, username, email or phone" />
          </div>
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} options={[{ value: 'ALL', label: 'All' }, { value: 'online', label: 'Online' }, { value: 'busy', label: 'Busy' }, { value: 'break', label: 'On Break' }, { value: 'offline', label: 'Offline' }]} />
          <Sel label="Availability" value={availability} onChange={e => setAvailability(e.target.value)} options={[{ value: 'ALL', label: 'All' }, { value: 'AVAILABLE', label: 'Available' }, { value: 'BUSY', label: 'Busy' }, { value: 'ON_BREAK', label: 'On Break' }]} />
          <Sel label="Department" value={department} onChange={e => setDepartment(e.target.value)} options={[{ value: 'ALL', label: 'All' }, ...DEPARTMENTS.map(d => ({ value: d, label: d }))]} />
          <Sel label="Shift" value={shift} onChange={e => setShift(e.target.value)} options={[{ value: 'ALL', label: 'All' }, ...SHIFTS.map(x => ({ value: x, label: x }))]} />
          <Sel label="Merchant" value={merchant} onChange={e => setMerchant(e.target.value)} options={[{ value: 'ALL', label: 'All Merchants' }, ...merchantOpts.map(m => ({ value: m, label: m }))]} />
          <Sel label="Sort By" value={sortKey} onChange={e => setSortKey(e.target.value as typeof sortKey)} options={[
            { value: 'name', label: 'Name' }, { value: 'loginTime', label: 'Login Time' }, { value: 'lastSeen', label: 'Last Seen' }, { value: 'department', label: 'Department' },
          ]} />
        </div>
      </Card>

      {/* Table */}
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto', maxHeight: 600 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>
              {['Support ID', 'Full Name', 'Username', 'Department', 'Shift', 'Assigned Merchants', 'Availability', 'Status', 'Login Time', 'Last Activity', 'Last Seen', 'Current Session', 'Actions'].map(h => <th key={h} style={th}>{h}</th>)}
            </tr></thead>
            <tbody>
              {filtered.length === 0 && <tr><td colSpan={13} style={{ ...td, textAlign: 'center', color: T.textMuted, padding: 28 }}>No support members match the filters.</td></tr>}
              {filtered.map(m => (
                <tr key={m.id} className="c5-row-hover" style={{ cursor: 'pointer' }} onClick={() => setSelId(m.id)}>
                  <td style={{ ...td, fontFamily: 'monospace', color: T.textMuted }}>{m.supportCode || '—'}</td>
                  <td style={{ ...td, fontWeight: 700 }}>{m.fullName}{!m.active && <span style={{ marginLeft: 6, fontSize: 10, color: T.danger, fontWeight: 700 }}>· Inactive</span>}</td>
                  <td style={{ ...td, color: T.textMuted }}>{m.username}</td>
                  <td style={{ ...td, color: T.textMuted }}>{m.department || '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{m.shift || '—'}</td>
                  <td style={{ ...td, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }} title={m.assignedMerchants.map(x => x.name).join(', ')}>
                    {m.assignedMerchantCount ? `${m.assignedMerchantCount} · ${m.assignedMerchants.map(x => x.name).join(', ')}` : '—'}
                  </td>
                  <td style={{ ...td, color: availColor(m.availability), fontWeight: 700 }}>{AVAILABILITY_LABEL[m.availability] || 'Available'}</td>
                  <td style={td}><StatusDot status={m.status} /></td>
                  <td style={{ ...td, color: T.textMuted }}>{fmtTime(m.loginTime)}</td>
                  <td style={{ ...td, color: T.textMuted }}>{relTime(m.lastActivity)}</td>
                  <td style={{ ...td, color: m.status !== 'offline' ? T.success : T.textMuted }}>{relTime(m.lastSeen)}</td>
                  <td style={{ ...td, color: m.currentSession ? T.success : T.textMuted, fontWeight: 700 }}>{m.currentSession ? 'Active' : '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{fmtDate(m.createdAt || m.created)}</td>
                  <td style={td}><Btn size="sm" variant="ghost" onClick={() => setSelId(m.id)}>View</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ padding: '10px 14px', borderTop: `1px solid ${T.border}` }}>
          <p style={{ fontSize: 11, color: T.textMuted, margin: 0 }}>Showing {filtered.length} of {rows.length} support members · updates automatically</p>
        </div>
      </Card>

      {showCreate && <CreateModal merchants={merchants} onClose={() => setShowCreate(false)} onCreated={reload} />}
      {sel && <ProfileDrawer member={sel} isSuperAdmin={isSuperAdmin} merchants={merchants} activeConversations={convos} onClose={() => setSelId(null)} onChanged={() => { reload(); loadMerchants(); }} />}
    </div>
  );
};

export default SupportManagementPage;
