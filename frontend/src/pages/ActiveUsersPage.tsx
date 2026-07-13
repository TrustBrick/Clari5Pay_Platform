import React, { useEffect, useMemo, useState } from 'react';
import { T } from '../utils/theme';
import { Card, StatCard, Sel, Input, Modal, Skeleton, CountUp } from '../components/UI';
import { Icon, isIconName } from '../components/Icon';
import { activeUsersAPI } from '../services/api';
import { usePoll, PRESENCE_POLL_MS } from '../utils/usePoll';
import { usePresenceStream } from '../utils/sse';
import type { User, ActiveUsersData, ActiveUserRow } from '../types';

// ── formatting helpers ──
const prettyRole = (r?: string | null) => (r || '—').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
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
const fmtDuration = (secs?: number | null): string => {
  if (secs == null) return '—';
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
};

const StatusDot: React.FC<{ online: boolean }> = ({ online }) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 700, color: online ? T.success : T.textMuted }}>
    <span style={{ width: 9, height: 9, borderRadius: '50%', background: online ? T.success : T.textMuted, boxShadow: online ? `0 0 0 3px ${T.success}22` : 'none' }} />
    {online ? 'Online' : 'Offline'}
  </span>
);

// Multi-state dot for user rows: Support members can be Busy (yellow) / On Break (red) while logged in.
const UserStatusDot: React.FC<{ status: string }> = ({ status }) => {
  const color = status === 'online' ? T.success : status === 'busy' ? T.warning : status === 'break' ? T.danger : T.textMuted;
  const label = status === 'online' ? 'Online' : status === 'busy' ? 'Busy' : status === 'break' ? 'On Break' : 'Offline';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 700, color }}>
      <span style={{ width: 9, height: 9, borderRadius: '50%', background: color, boxShadow: status !== 'offline' ? `0 0 0 3px ${color}22` : 'none' }} />
      {label}
    </span>
  );
};

const th: React.CSSProperties = { textAlign: 'left', padding: '10px 12px', fontSize: 10.5, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', whiteSpace: 'nowrap', borderBottom: `1px solid ${T.border}` };
const td: React.CSSProperties = { padding: '10px 12px', fontSize: 12.5, color: T.textMain, borderBottom: `1px solid ${T.borderLight}`, whiteSpace: 'nowrap' };

// One presence container (Admins / Support / Users) — same table, its own heading + counts.
const UserGroup: React.FC<{
  title: string; icon: string; rows: ActiveUserRow[]; flash: Set<number>; onSelect: (u: ActiveUserRow) => void;
}> = ({ title, icon, rows, flash, onSelect }) => {
  const online = rows.filter(r => r.status !== 'offline').length;
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, margin: '0 0 10px' }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: T.textMain, display: 'inline-flex', alignItems: 'center', gap: 6 }}>{isIconName(icon) ? <Icon name={icon} size={16} /> : icon} {title}</h3>
        <span style={{ fontSize: 12, color: T.textMuted, fontWeight: 700 }}>
          <span style={{ color: T.success }}>{online} online</span> · {rows.length} total
        </span>
      </div>
      <Card style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ overflowX: 'auto', maxHeight: 460 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ background: T.canvas }}>
              {['Status', 'User Name', 'Username', 'Merchant', 'Role', 'Member Role', 'Phone', 'Login Time', 'Last Activity', 'Last Seen', 'IP', 'Device', 'Browser', 'Logout', 'Session'].map(h => <th key={h} style={th}>{h}</th>)}
            </tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={15} style={{ ...td, textAlign: 'center', color: T.textMuted, padding: 24 }}>No {title.toLowerCase()} match the filters.</td></tr>}
              {rows.map(u => (
                <tr key={u.id} className="c5-row-hover" style={{ cursor: 'pointer', background: flash.has(u.id) ? `${T.success}22` : undefined, transition: 'background 600ms ease' }} onClick={() => onSelect(u)}>
                  <td style={td}><UserStatusDot status={u.status} /></td>
                  <td style={{ ...td, fontWeight: 700 }}>{u.name}</td>
                  <td style={{ ...td, color: T.textMuted }}>{u.username}</td>
                  <td style={td}>{u.merchant || '—'}</td>
                  <td style={td}>{prettyRole(u.role)}</td>
                  <td style={td}>{prettyRole(u.merchantRole)}</td>
                  <td style={{ ...td, color: T.textMuted }}>{u.phone || '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{fmtTime(u.loginTime)}</td>
                  <td style={{ ...td, color: T.textMuted }}>{relTime(u.lastActivity)}</td>
                  <td style={{ ...td, color: u.status !== 'offline' ? T.success : T.textMuted }}>{relTime(u.lastSeen)}</td>
                  <td style={{ ...td, color: T.textMuted, fontFamily: 'monospace' }}>{u.ip || '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{u.device || '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{u.browser || '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{u.logoutTime ? fmtTime(u.logoutTime) : '—'}</td>
                  <td style={{ ...td, color: T.textMuted }}>{fmtDuration(u.sessionDuration)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ padding: '10px 14px', borderTop: `1px solid ${T.border}` }}>
          <p style={{ fontSize: 11, color: T.textMuted, margin: 0 }}>Showing {rows.length} {title.toLowerCase()} · updates automatically</p>
        </div>
      </Card>
    </div>
  );
};

export const ActiveUsersPage: React.FC<{ user: User }> = () => {
  const [data, setData] = useState<ActiveUsersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<ActiveUserRow | null>(null);
  const [status, setStatus] = useState('ALL');
  const [merchant, setMerchant] = useState('ALL');
  const [role, setRole] = useState('ALL');
  const [memberRole, setMemberRole] = useState('ALL');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState<'status' | 'lastSeen' | 'loginTime' | 'merchant' | 'role'>('status');

  const reload = () => activeUsersAPI.list().then(setData).catch(() => {});
  useEffect(() => { reload().finally(() => setLoading(false)); }, []);
  // Live push via SSE (<1s). Polling stays as the fallback and only runs while the stream is down.
  const live = usePresenceStream(d => { setData(d); setLoading(false); });
  usePoll(() => { if (!live) reload(); }, PRESENCE_POLL_MS);

  // Briefly highlight rows that just went offline → online, as a live "someone came online" cue.
  const prevOnline = React.useRef<Set<number>>(new Set());
  const initedRef = React.useRef(false);
  const [flash, setFlash] = useState<Set<number>>(() => new Set());
  useEffect(() => {
    const online = new Set((data?.users || []).filter(u => u.status === 'online').map(u => u.id));
    if (!initedRef.current) { prevOnline.current = online; initedRef.current = true; return; }  // don't flash the whole list on first load
    const fresh = [...online].filter(id => !prevOnline.current.has(id));
    prevOnline.current = online;
    if (!fresh.length) return;
    setFlash(prev => { const n = new Set(prev); fresh.forEach(id => n.add(id)); return n; });
    const t = setTimeout(() => setFlash(prev => { const n = new Set(prev); fresh.forEach(id => n.delete(id)); return n; }), 2600);
    return () => clearTimeout(t);
  }, [data]);

  const rows = data?.users || [];
  const merchantOpts = useMemo(() => Array.from(new Set(rows.map(r => r.merchant).filter(Boolean))) as string[], [rows]);
  const roleOpts = useMemo(() => Array.from(new Set(rows.map(r => r.role).filter(Boolean))), [rows]);
  const memberRoleOpts = useMemo(() => Array.from(new Set(rows.map(r => r.merchantRole).filter(Boolean))) as string[], [rows]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = rows.filter(r =>
      (status === 'ALL' || r.status === status) &&
      (merchant === 'ALL' || r.merchant === merchant) &&
      (role === 'ALL' || r.role === role) &&
      (memberRole === 'ALL' || r.merchantRole === memberRole) &&
      (!q || [r.username, r.name, r.merchant, r.phone].some(v => (v || '').toLowerCase().includes(q)))
    );
    const cmp: Record<string, (a: ActiveUserRow, b: ActiveUserRow) => number> = {
      status: (a, b) => (a.status === b.status ? 0 : a.status === 'online' ? -1 : 1),
      lastSeen: (a, b) => (b.lastSeen || '').localeCompare(a.lastSeen || ''),
      loginTime: (a, b) => (b.loginTime || '').localeCompare(a.loginTime || ''),
      merchant: (a, b) => (a.merchant || '~').localeCompare(b.merchant || '~'),
      role: (a, b) => (a.role || '').localeCompare(b.role || ''),
    };
    return [...list].sort(cmp[sortKey]);
  }, [rows, status, merchant, role, memberRole, search, sortKey]);

  // Split the filtered list into three presence containers.
  const admins = useMemo(() => filtered.filter(u => u.role === 'SUPER_ADMIN' || u.role === 'ADMIN'), [filtered]);
  const support = useMemo(() => filtered.filter(u => u.role === 'SUPPORT_AGENT'), [filtered]);
  const users = useMemo(() => filtered.filter(u => u.role === 'MERCHANT'), [filtered]);

  const s = data?.summary;

  if (loading) return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 14 }}>
      {[0, 1, 2, 3].map(i => <Card key={i} style={{ padding: 18 }}><Skeleton w={90} h={11} /><div style={{ height: 12 }} /><Skeleton w={70} h={26} /></Card>)}
    </div>
  );

  return (
    <div>
      {/* 1 — Summary cards */}
      <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(210px,1fr))', gap: 14, marginBottom: 18 }}>
        <StatCard icon="online" label="Online Users" value={<CountUp value={s?.online ?? 0} />} color={T.success} />
        <StatCard icon="offline" label="Offline Users" value={<CountUp value={s?.offline ?? 0} />} color={T.textMuted} />
        <StatCard icon="logged-in" label="Total Logged In" value={<CountUp value={s?.totalLoggedIn ?? 0} />} color={T.blue} />
        <StatCard icon="registered-users" label="Total Registered Users" value={<CountUp value={s?.totalRegistered ?? 0} />} color={T.info || T.blue} />
      </div>

      {/* 2 — Merchant status */}
      {(data?.merchants?.length ?? 0) > 0 && (
        <>
          <h3 style={{ margin: '0 0 10px', fontSize: 14, fontWeight: 800, color: T.textMain }}>Merchant Status</h3>
          <Card style={{ padding: 0, overflow: 'hidden', marginBottom: 20 }}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr style={{ background: T.canvas }}>
                  {['Merchant Name', 'Online', 'Offline', 'Total Users', 'Status'].map(h => <th key={h} style={th}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {data!.merchants.map(m => (
                    <tr key={m.name} className="c5-row-hover">
                      <td style={{ ...td, fontWeight: 700 }}>{m.name}</td>
                      <td style={{ ...td, color: T.success, fontWeight: 700 }}>{m.online}</td>
                      <td style={{ ...td, color: T.textMuted }}>{m.offline}</td>
                      <td style={td}>{m.total}</td>
                      <td style={td}><StatusDot online={m.status === 'Online'} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      {/* 3 — Active users table */}
      <h3 style={{ margin: '0 0 10px', fontSize: 14, fontWeight: 800, color: T.textMain }}>Active Users</h3>
      <Card style={{ padding: 14, marginBottom: 12 }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ flex: '1 1 220px', minWidth: 180 }}>
            <Input label="Search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Username, name, merchant or phone" />
          </div>
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)} options={[{ value: 'ALL', label: 'All' }, { value: 'online', label: 'Online' }, { value: 'offline', label: 'Offline' }]} />
          <Sel label="Merchant" value={merchant} onChange={e => setMerchant(e.target.value)} options={[{ value: 'ALL', label: 'All Merchants' }, ...merchantOpts.map(m => ({ value: m, label: m }))]} />
          <Sel label="Role" value={role} onChange={e => setRole(e.target.value)} options={[{ value: 'ALL', label: 'All Roles' }, ...roleOpts.map(r => ({ value: r, label: prettyRole(r) }))]} />
          <Sel label="Member Role" value={memberRole} onChange={e => setMemberRole(e.target.value)} options={[{ value: 'ALL', label: 'All' }, ...memberRoleOpts.map(r => ({ value: r, label: prettyRole(r) }))]} />
          <Sel label="Sort By" value={sortKey} onChange={e => setSortKey(e.target.value as typeof sortKey)} options={[
            { value: 'status', label: 'Status' }, { value: 'lastSeen', label: 'Last Seen' }, { value: 'loginTime', label: 'Login Time' },
            { value: 'merchant', label: 'Merchant' }, { value: 'role', label: 'Role' },
          ]} />
        </div>
      </Card>

      {/* Three separate presence containers */}
      <UserGroup title="Admins" icon="admin-management" rows={admins} flash={flash} onSelect={setSel} />
      <UserGroup title="Support" icon="support" rows={support} flash={flash} onSelect={setSel} />
      <UserGroup title="Users" icon="users" rows={users} flash={flash} onSelect={setSel} />

      {/* User details drawer */}
      {sel && (
        <Modal title={`${sel.name} — Session Details`} onClose={() => setSel(null)}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
            <div style={{ width: 54, height: 54, borderRadius: '50%', background: T.canvas, border: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', fontSize: 20, fontWeight: 800, color: T.textMuted }}>
              {sel.avatar ? <img src={sel.avatar} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : (sel.name[0] || '?').toUpperCase()}
            </div>
            <div>
              <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>{sel.name}</p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>@{sel.username}</p>
              <div style={{ marginTop: 4 }}><UserStatusDot status={sel.status} /></div>
            </div>
          </div>
          {([
            ['Merchant', sel.merchant || '—'], ['Role', prettyRole(sel.role)], ['Member Role', prettyRole(sel.merchantRole)],
            ['Email', sel.email || '—'], ['Phone', sel.phone || '—'],
            ['Login Time', fmtTime(sel.loginTime)], ['Logout Time', sel.logoutTime ? fmtTime(sel.logoutTime) : '—'],
            ['Session Duration', fmtDuration(sel.sessionDuration)], ['Last Activity', relTime(sel.lastActivity)], ['Last Seen', relTime(sel.lastSeen)],
            ['IP Address', sel.ip || '—'], ['Device', sel.device || '—'], ['Browser', sel.browser || '—'], ['Operating System', sel.os || '—'],
            ['Country', sel.country || '—'],
          ] as [string, string][]).map(([k, v]) => (
            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '8px 0', borderBottom: `1px solid ${T.borderLight}` }}>
              <span style={{ fontSize: 12.5, color: T.textMuted }}>{k}</span>
              <span style={{ fontSize: 12.5, fontWeight: 700, color: T.textMain, textAlign: 'right', wordBreak: 'break-word' }}>{v}</span>
            </div>
          ))}
        </Modal>
      )}
    </div>
  );
};

export default ActiveUsersPage;
