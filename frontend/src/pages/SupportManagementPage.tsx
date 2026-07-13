import React, { useEffect, useMemo, useRef, useState } from 'react';
import { T } from '../utils/theme';
import { Card, StatCard, Sel, Input, Btn, Modal, Skeleton, CountUp, ReasonModal } from '../components/UI';
import { Icon } from '../components/Icon';
import { supportManagementAPI } from '../services/api';
import { usePoll, PRESENCE_POLL_MS } from '../utils/usePoll';
import { openSSE } from '../utils/sse';
import type { User, SupportMembersData, SupportMemberRow, SupportStatus, SupportConversationRow } from '../types';

const DEPARTMENTS = ['Technical Support', 'Payments', 'Merchant Support', 'Finance', 'Compliance'];
const SHIFTS = ['Morning', 'Afternoon', 'Night'];
const COUNTRY_CODES = [
  { value: '+91', label: '🇮🇳 +91' }, { value: '+1', label: '🇺🇸 +1' }, { value: '+44', label: '🇬🇧 +44' },
  { value: '+971', label: '🇦🇪 +971' }, { value: '+65', label: '🇸🇬 +65' }, { value: '+61', label: '🇦🇺 +61' },
  { value: '+49', label: '🇩🇪 +49' }, { value: '+33', label: '🇫🇷 +33' }, { value: '+234', label: '🇳🇬 +234' },
  { value: '+92', label: '🇵🇰 +92' }, { value: '+880', label: '🇧🇩 +880' }, { value: '+94', label: '🇱🇰 +94' },
];

// ── formatting helpers ──
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
  available: { label: 'Available', color: T.success },
  online: { label: 'Available', color: T.success },
  busy: { label: 'Busy', color: T.warning },
  break: { label: 'On Break', color: T.danger },
  offline: { label: 'Offline', color: T.textMuted },
};
const AVAILABILITY_LABEL: Record<string, string> = { AVAILABLE: 'Available', BUSY: 'Busy', ON_BREAK: 'On Break' };
const availColor = (a?: string) => a === 'BUSY' ? T.warning : a === 'ON_BREAK' ? T.danger : T.success;

const StatusDot: React.FC<{ status: SupportStatus }> = ({ status }) => {
  const m = STATUS_META[status] || STATUS_META.offline;
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

// Active-load pill (active / max conversations), coloured by how full the member is.
const LoadPill: React.FC<{ active?: number; max?: number }> = ({ active = 0, max = 0 }) => {
  const full = max > 0 && active >= max;
  const color = full ? T.warning : active > 0 ? T.blue : T.textMuted;
  return <span style={{ fontWeight: 700, color }}>{active}/{max || '—'}</span>;
};

// ── Create modal ──
const CreateModal: React.FC<{ onClose: () => void; onCreated: () => void }> = ({ onClose, onCreated }) => {
  const [f, setF] = useState({ fullName: '', username: '', email: '', dial: '+91', phone: '', password: '', confirm: '', department: '', shift: SHIFTS[0], status: 'Active' });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const set = (k: keyof typeof f, v: string) => setF(p => ({ ...p, [k]: v }));

  const phoneDigits = f.phone.replace(/\D/g, '');
  const emailOk = EMAIL_RE.test(f.email);
  const phoneOk = phoneDigits.length >= 6 && phoneDigits.length <= 14;
  const pwOk = f.password.length >= 8 && f.password === f.confirm;
  const valid = f.fullName.trim() && f.username.trim() && emailOk && phoneOk && pwOk;
  const disabledReason =
    !f.fullName.trim() ? 'Enter the full name'
    : !f.username.trim() ? 'Enter a username'
    : !emailOk ? 'Enter a valid email address'
    : !phoneOk ? 'Enter a valid phone number'
    : f.password.length < 8 ? 'Password must be at least 8 characters'
    : f.password !== f.confirm ? 'Passwords do not match'
    : '';

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true); setErr('');
    try {
      await supportManagementAPI.create({
        username: f.username.trim(), password: f.password, email: f.email.trim(), fullName: f.fullName.trim(),
        phone: `${f.dial}${phoneDigits}`, department: f.department, shift: f.shift, status: f.status,
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
      <p style={{ fontSize: 12, color: T.textMuted, margin: '8px 0 4px' }}>
        Customers are assigned to members automatically by availability — members are no longer tied to specific merchants.
      </p>
      {err && <p style={{ color: T.danger, fontSize: 12.5, fontWeight: 600, margin: '4px 0 10px' }}>{err}</p>}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <Btn onClick={submit} disabled={!valid || busy}>{busy ? 'Creating…' : 'Create Support Member'}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
        {!valid && !busy && disabledReason && <span style={{ fontSize: 12, color: T.warning, fontWeight: 600 }}><Icon name="warning" size={13} /> {disabledReason}</span>}
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
  member: SupportMemberRow; isSuperAdmin: boolean; activeConversations: number | null;
  onClose: () => void; onChanged: () => void;
}> = ({ member, isSuperAdmin, activeConversations, onClose, onChanged }) => {
  const [sub, setSub] = useState<'' | 'edit' | 'reset' | 'toggle' | 'delete'>('');
  const done = () => { onChanged(); setSub(''); };
  const force = async (a: 'AVAILABLE' | 'BUSY' | 'ON_BREAK') => { await supportManagementAPI.forceAvailability(member.id, a); onChanged(); };
  const rows: [string, React.ReactNode][] = [
    ['Support ID', member.supportCode || '—'],
    ['Username', `@${member.username}`],
    ['Email', member.email || '—'],
    ['Phone', member.phone || '—'],
    ['Department', member.department || '—'],
    ['Shift', member.shift || '—'],
    ['Status', member.active ? 'Active' : 'Inactive'],
    ['Availability', <StatusDot status={member.status} />],
    ['Active Conversations', `${activeConversations == null ? '…' : activeConversations} / ${member.maxConversations ?? '—'}`],
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
      {/* Admin force-status */}
      <div style={{ marginTop: 14 }}>
        <p style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', margin: '0 0 6px' }}>Force Status</p>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Btn size="sm" variant="secondary" onClick={() => force('AVAILABLE')}>Set Available</Btn>
          <Btn size="sm" variant="secondary" onClick={() => force('BUSY')}>Set Busy</Btn>
          <Btn size="sm" variant="secondary" onClick={() => force('ON_BREAK')}>Set On Break</Btn>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 16 }}>
        <Btn size="sm" onClick={() => setSub('edit')}>Edit</Btn>
        <Btn size="sm" variant="secondary" onClick={() => setSub('reset')}>Reset Password</Btn>
        <Btn size="sm" variant={member.active ? 'danger' : 'success'} onClick={() => setSub('toggle')}>{member.active ? 'Deactivate' : 'Activate'}</Btn>
        {isSuperAdmin && <Btn size="sm" variant="danger" onClick={() => setSub('delete')}>Delete</Btn>}
      </div>

      {sub === 'edit' && <EditModal member={member} onClose={() => setSub('')} onSaved={done} />}
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
            This removes the support member from all lists and revokes their access. Their open conversations are returned to the queue and reassigned. The record is archived (preserved for audit).
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

// ── Assignment config panel ──
const ConfigPanel: React.FC<{ max: number; strategy: string; onSaved: () => void }> = ({ max, strategy, onSaved }) => {
  const [m, setM] = useState(String(max));
  const [s, setS] = useState(strategy);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  useEffect(() => { setM(String(max)); setS(strategy); }, [max, strategy]);
  const dirty = m !== String(max) || s !== strategy;
  const save = async () => {
    const n = parseInt(m, 10);
    if (!n || n < 1) { setMsg('Enter a valid limit'); return; }
    setBusy(true); setMsg('');
    try { await supportManagementAPI.updateConfig({ maxActiveConversations: n, strategy: s }); setMsg('Saved'); onSaved(); }
    catch { setMsg('Could not save'); }
    finally { setBusy(false); }
  };
  return (
    <Card style={{ padding: 14, marginBottom: 12 }}>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div style={{ width: 200 }}>
          <Input label="Max Active Conversations / Member" value={m} onChange={e => setM(e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
        </div>
        <Sel label="Assignment Strategy" value={s} onChange={e => setS(e.target.value)} options={[
          { value: 'LEAST_ACTIVE', label: 'Least Active Conversations' },
          { value: 'ROUND_ROBIN', label: 'Round Robin' },
        ]} />
        <Btn onClick={save} disabled={!dirty || busy}>{busy ? 'Saving…' : 'Save Config'}</Btn>
        {msg && <span style={{ fontSize: 12, color: msg === 'Saved' ? T.success : T.danger, fontWeight: 600 }}>{msg}</span>}
      </div>
    </Card>
  );
};

// ── Conversations & queue panel ──
const ConversationsPanel: React.FC<{ members: SupportMemberRow[]; refreshKey: number; onChanged: () => void }> = ({ members, refreshKey, onChanged }) => {
  const [convs, setConvs] = useState<SupportConversationRow[] | null>(null);
  const [reassignId, setReassignId] = useState<number | null>(null);
  const load = () => supportManagementAPI.conversations().then(setConvs).catch(() => setConvs([]));
  useEffect(() => { load(); }, [refreshKey]);
  usePoll(() => load(), PRESENCE_POLL_MS);

  const rows = convs || [];
  const target = reassignId != null ? rows.find(c => c.id === reassignId) : null;

  return (
    <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 18 }}>
      <div style={{ padding: '12px 14px', borderBottom: `1px solid ${T.border}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <p style={{ margin: 0, fontSize: 13, fontWeight: 800, color: T.textMain }}>Conversations & Queue</p>
        <span style={{ fontSize: 11, color: T.textMuted }}>{rows.filter(c => c.queued).length} waiting · {rows.filter(c => !c.queued && c.status === 'OPEN').length} active</span>
      </div>
      <div style={{ overflowX: 'auto', maxHeight: 360 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ background: T.canvas }}>
            {['Customer', 'Assigned To', 'Status', 'Last Message', 'Started', 'Actions'].map(h => <th key={h} style={th}>{h}</th>)}
          </tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={6} style={{ ...td, textAlign: 'center', color: T.textMuted, padding: 24 }}>No open conversations.</td></tr>}
            {rows.map(c => (
              <tr key={c.id} className="c5-row-hover">
                <td style={{ ...td, fontWeight: 700 }}>{c.customerName || `#${c.customerId}`}{c.customerCode && <span style={{ marginLeft: 6, fontSize: 11, color: T.textMuted }}>{c.customerCode}</span>}</td>
                <td style={{ ...td, color: c.queued ? T.warning : T.textMain, fontWeight: 700 }}>{c.queued ? '— Queued —' : (c.supportName || '—')}</td>
                <td style={td}>
                  <span style={{ fontSize: 11, fontWeight: 800, color: c.queued ? T.warning : c.status === 'CLOSED' ? T.textMuted : T.success }}>
                    {c.queued ? 'QUEUED' : c.status}
                  </span>
                </td>
                <td style={{ ...td, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', color: T.textMuted }} title={c.lastMessage || ''}>{c.lastMessage || '—'}</td>
                <td style={{ ...td, color: T.textMuted }}>{relTime(c.createdAt)}</td>
                <td style={td}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <Btn size="sm" variant="ghost" onClick={() => setReassignId(c.id)}>Reassign</Btn>
                    <Btn size="sm" variant="ghost" onClick={async () => { await supportManagementAPI.closeConversation(c.id); load(); onChanged(); }}>Close</Btn>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {target && (
        <Modal title={`Reassign — ${target.customerName || 'Customer'}`} onClose={() => setReassignId(null)}>
          <p style={{ fontSize: 12.5, color: T.textMuted, marginTop: 0 }}>Pick a support member to own this conversation.</p>
          <div style={{ display: 'grid', gap: 8 }}>
            {members.filter(m => m.active).map(m => (
              <button key={m.id} className="c5-row-hover"
                onClick={async () => { await supportManagementAPI.reassignConversation(target.id, m.id); setReassignId(null); load(); onChanged(); }}
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 12px', borderRadius: 10, border: `1px solid ${T.border}`, background: T.surface, cursor: 'pointer', textAlign: 'left' }}>
                <span style={{ fontWeight: 700, color: T.textMain, fontSize: 13 }}>{m.fullName} <span style={{ color: T.textMuted, fontWeight: 500 }}>· {m.supportCode}</span></span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <LoadPill active={m.activeConversations} max={m.maxConversations} />
                  <StatusDot status={m.status} />
                </span>
              </button>
            ))}
          </div>
          <div style={{ marginTop: 12 }}><Btn variant="secondary" onClick={() => setReassignId(null)}>Cancel</Btn></div>
        </Modal>
      )}
    </Card>
  );
};

// ── Main page ──
export const SupportManagementPage: React.FC<{ user: User }> = ({ user }) => {
  const isSuperAdmin = user.role === 'SUPER_ADMIN';
  const [data, setData] = useState<SupportMembersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selId, setSelId] = useState<number | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [convKey, setConvKey] = useState(0);

  const [status, setStatus] = useState('ALL');
  const [availability, setAvailability] = useState('ALL');
  const [department, setDepartment] = useState('ALL');
  const [shift, setShift] = useState('ALL');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<'name' | 'loginTime' | 'lastSeen' | 'department'>('name');

  const reload = () => supportManagementAPI.list().then(setData).catch(() => {});
  useEffect(() => { reload().finally(() => setLoading(false)); }, []);

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

  const [convos, setConvos] = useState<number | null>(null);
  useEffect(() => {
    if (selId == null) { setConvos(null); return; }
    setConvos(null);
    supportManagementAPI.profile(selId).then(p => setConvos(p.activeConversations ?? 0)).catch(() => setConvos(null));
  }, [selId]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = rows.filter(r =>
      (status === 'ALL' || r.status === status) &&
      (availability === 'ALL' || r.availability === availability) &&
      (department === 'ALL' || (r.department || '') === department) &&
      (shift === 'ALL' || (r.shift || '') === shift) &&
      (!q || [r.supportCode, r.fullName, r.username, r.email, r.phone].some(v => (v || '').toLowerCase().includes(q)))
    );
    const cmp: Record<string, (a: SupportMemberRow, b: SupportMemberRow) => number> = {
      name: (a, b) => (a.fullName || '').localeCompare(b.fullName || ''),
      loginTime: (a, b) => (b.loginTime || '').localeCompare(a.loginTime || ''),
      lastSeen: (a, b) => (b.lastSeen || '').localeCompare(a.lastSeen || ''),
      department: (a, b) => (a.department || '~').localeCompare(b.department || '~'),
    };
    return [...list].sort(cmp[sortKey]);
  }, [rows, status, availability, department, shift, search, sortKey]);

  const s = data?.summary;
  const bumpConv = () => setConvKey(k => k + 1);

  if (loading) return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 14 }}>
      {[0, 1, 2, 3].map(i => <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={70} h={26} /></Card>)}
    </div>
  );

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <p style={{ fontSize: 13, color: T.textMuted, margin: 0 }}>
          Manage your support team and track real-time availability. Customers are auto-assigned to one available member.
        </p>
        <Btn onClick={() => setShowCreate(true)}>+ Create Support Member</Btn>
      </div>

      {/* Dashboard cards */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 14, marginBottom: 14 }}>
        <StatCard icon="support" label="Support Members" value={<CountUp value={s?.members ?? 0} />} color={T.blue} />
        <StatCard icon="available" label="Available" value={<CountUp value={s?.available ?? 0} />} color={T.success} />
        <StatCard icon="busy" label="Busy" value={<CountUp value={s?.busy ?? 0} />} color={T.warning} />
        <StatCard icon="on-break" label="On Break" value={<CountUp value={s?.onBreak ?? 0} />} color={T.danger} />
        <StatCard icon="offline" label="Offline" value={<CountUp value={s?.offline ?? 0} />} color={T.textMuted} />
        <StatCard icon="chat" label="Active Conversations" value={<CountUp value={s?.activeConversations ?? 0} />} color={T.blue} />
        <StatCard icon="queue" label="Waiting in Queue" value={<CountUp value={s?.waitingCustomers ?? 0} />} color={(s?.waitingCustomers ?? 0) > 0 ? T.warning : T.textMuted} />
        <StatCard icon="response-time" label="Avg Response" value={fmtDuration(s?.avgResponseSeconds ?? null)} color={T.textMuted} />
      </div>

      {/* Assignment config */}
      <ConfigPanel max={s?.maxActiveConversations ?? 10} strategy={s?.strategy ?? 'LEAST_ACTIVE'} onSaved={reload} />

      {/* Conversations & queue */}
      <ConversationsPanel members={rows} refreshKey={convKey} onChanged={reload} />

      {/* Filters */}
      <Card style={{ padding: 14, marginBottom: 12 }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ flex: '1 1 220px', minWidth: 180 }}>
            <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Support ID, name, username, email or phone" />
          </div>
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} options={[{ value: 'ALL', label: 'All' }, { value: 'available', label: 'Available' }, { value: 'busy', label: 'Busy' }, { value: 'break', label: 'On Break' }, { value: 'offline', label: 'Offline' }]} />
          <Sel label="Availability" value={availability} onChange={e => setAvailability(e.target.value)} options={[{ value: 'ALL', label: 'All' }, { value: 'AVAILABLE', label: 'Available' }, { value: 'BUSY', label: 'Busy' }, { value: 'ON_BREAK', label: 'On Break' }]} />
          <Sel label="Department" value={department} onChange={e => setDepartment(e.target.value)} options={[{ value: 'ALL', label: 'All' }, ...DEPARTMENTS.map(d => ({ value: d, label: d }))]} />
          <Sel label="Shift" value={shift} onChange={e => setShift(e.target.value)} options={[{ value: 'ALL', label: 'All' }, ...SHIFTS.map(x => ({ value: x, label: x }))]} />
          <Sel label="Sort By" value={sortKey} onChange={e => setSortKey(e.target.value as typeof sortKey)} options={[
            { value: 'name', label: 'Name' }, { value: 'loginTime', label: 'Login Time' }, { value: 'lastSeen', label: 'Last Seen' }, { value: 'department', label: 'Department' },
          ]} />
        </div>
      </Card>

      {/* Members table */}
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto', maxHeight: 600 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>
              {['Support ID', 'Full Name', 'Username', 'Department', 'Shift', 'Active / Max', 'Availability', 'Status', 'Login Time', 'Last Activity', 'Last Seen', 'Current Session', 'Actions'].map(h => <th key={h} style={th}>{h}</th>)}
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
                  <td style={td}><LoadPill active={m.activeConversations} max={m.maxConversations} /></td>
                  <td style={{ ...td, color: availColor(m.availability), fontWeight: 700 }}>{AVAILABILITY_LABEL[m.availability] || 'Available'}</td>
                  <td style={td}><StatusDot status={m.status} /></td>
                  <td style={{ ...td, color: T.textMuted }}>{fmtTime(m.loginTime)}</td>
                  <td style={{ ...td, color: T.textMuted }}>{relTime(m.lastActivity)}</td>
                  <td style={{ ...td, color: m.status !== 'offline' ? T.success : T.textMuted }}>{relTime(m.lastSeen)}</td>
                  <td style={{ ...td, color: m.currentSession ? T.success : T.textMuted, fontWeight: 700 }}>{m.currentSession ? 'Active' : '—'}</td>
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

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={() => { reload(); bumpConv(); }} />}
      {sel && <ProfileDrawer member={sel} isSuperAdmin={isSuperAdmin} activeConversations={convos} onClose={() => setSelId(null)} onChanged={() => { reload(); bumpConv(); }} />}
    </div>
  );
};

export default SupportManagementPage;
