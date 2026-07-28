import React, { useState, useEffect, useCallback, useRef, type CSSProperties } from 'react';
import { T } from '../utils/theme';
import { timeAgo, merchantRoleLabel, formatDateTime } from '../utils/helpers';
import ThemeToggle from './ThemeToggle';
import { Icon } from './Icon';
import { notificationAPI } from '../services/api';
import type { Notification, User } from '../types';

interface HeaderProps {
  user: User;
  title: string;
  onMenuClick: () => void;
  /** Span the full width — set on the pages rendered without the sidebar, so the header does not
   *  leave a 248px gap where the sidebar would have been. Also hides the menu button, since there
   *  is no sidebar for it to open. */
  fullWidth?: boolean;
  /** The active page key. On a dashboard page the title is replaced by a time-based greeting. */
  page?: string;
  /** Navigate to a page key (used by the profile popup's Profile / Change Password links). */
  onNavigate?: (page: string) => void;
  /** Sign the user out (profile popup Logout). */
  onLogout?: () => void;
}

// Theme-aware glassmorphism panel for the floating popups: a frosted translucent surface that
// reads correctly in both light and dark themes (var(--c5-*) tokens re-resolve on theme flip).
const glass: CSSProperties = {
  background: `color-mix(in srgb, ${T.surface} 78%, transparent)`,
  backdropFilter: 'blur(20px) saturate(1.4)',
  WebkitBackdropFilter: 'blur(20px) saturate(1.4)',
  border: `1px solid color-mix(in srgb, ${T.textMain} 12%, transparent)`,
  borderRadius: 18,
  boxShadow: '0 24px 60px rgba(0,0,0,0.28)',
  animation: 'c5menuin 0.2s ease',
};

const Header: React.FC<HeaderProps> = ({ user, title, onMenuClick, fullWidth, page, onNavigate, onLogout }) => {
  // A single menu is open at a time (opening one closes the other).
  const [menu, setMenu] = useState<'notif' | 'profile' | null>(null);
  const [items, setItems] = useState<Notification[]>([]);
  const notifRef = useRef<HTMLDivElement>(null);
  const profileRef = useRef<HTMLDivElement>(null);

  // Live clock for the dashboard greeting — a 30s tick keeps the minute display fresh without
  // re-rendering the header every second.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(id);
  }, []);

  // ── New-notification chime ────────────────────────────────────────────────
  // One preloaded <audio> shared across the app; we chime once per poll cycle when a
  // notification id we haven't seen before appears. IDs already seen never replay, so the
  // sound never repeats while a notification stays unread.
  const chimeRef = useRef<HTMLAudioElement | null>(null);
  const chimePrimed = useRef(false);        // true once the user has interacted (autoplay unlock)
  const seenIds = useRef<Set<Notification['id']>>(new Set());
  const baselineDone = useRef(false);       // skip the chime for whatever already exists on first load

  const playChime = useCallback(() => {
    const a = chimeRef.current;
    if (!a || !chimePrimed.current) return; // respect browser autoplay policy
    try { a.currentTime = 0; void a.play().catch(() => {}); } catch { /* ignore */ }
  }, []);

  const load = useCallback(() => {
    notificationAPI.list().then((list) => {
      setItems(list);
      const fresh = list.some((n) => !seenIds.current.has(n.id));
      list.forEach((n) => seenIds.current.add(n.id));
      if (!baselineDone.current) { baselineDone.current = true; return; } // don't chime for pre-existing
      if (fresh) playChime();
    }).catch(() => {});
  }, [playChime]);

  // Preload the sound once and unlock playback on the user's first interaction (browsers
  // block audio until then). Priming during the gesture keeps later chimes instant.
  useEffect(() => {
    const a = new Audio('/notification.mp3');
    a.preload = 'auto';
    chimeRef.current = a;
    const unlock = () => {
      chimePrimed.current = true;
      a.muted = true;
      a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; }).catch(() => { a.muted = false; });
      window.removeEventListener('pointerdown', unlock);
      window.removeEventListener('keydown', unlock);
    };
    window.addEventListener('pointerdown', unlock);
    window.addEventListener('keydown', unlock);
    return () => {
      window.removeEventListener('pointerdown', unlock);
      window.removeEventListener('keydown', unlock);
    };
  }, []);

  // Initial load + light polling so actions across the app show up.
  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, [load]);

  // Close the open popup on an outside click or Escape.
  useEffect(() => {
    if (!menu) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (notifRef.current?.contains(t) || profileRef.current?.contains(t)) return;
      setMenu(null);
    };
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onEsc);
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onEsc); };
  }, [menu]);

  const unread = items.filter((n) => !n.read).length;

  const toggleNotif = () => setMenu((m) => { const next = m === 'notif' ? null : 'notif'; if (next === 'notif') load(); return next; });
  const toggleProfile = () => setMenu((m) => (m === 'profile' ? null : 'profile'));

  const markAllRead = async () => {
    try {
      await notificationAPI.markAllRead();
      setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    } catch { /* ignore */ }
  };

  const clearAll = async () => {
    try {
      await notificationAPI.clear();
      setItems([]);
    } catch { /* ignore */ }
  };

  const go = (pageKey: string) => { setMenu(null); onNavigate?.(pageKey); };

  // Dashboard pages swap the static title for a personalised, time-aware greeting.
  const isDashboard = (page || '').toLowerCase().includes('dashboard');
  const hr = now.getHours();
  const greeting = hr >= 5 && hr < 12 ? 'Good Morning' : hr >= 12 && hr < 17 ? 'Good Afternoon' : 'Good Evening';
  // Greet the signed-in person — never the business name. For merchant logins `user.name` holds the
  // BUSINESS name, so prefer the personal Full Name and fall back to the username (never the business).
  // Other portals keep `user.name` (the person's real name) with the same Full Name preference.
  const greetName = user.role === 'MERCHANT'
    ? ((user.fullName || '').trim() || user.username)
    : user.name;
  const dateStr = now.toLocaleDateString('en-IN', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  const timeStr = now.toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit', hour12: true });

  const roleLabel = user.role === 'MERCHANT' && user.merchantRole ? merchantRoleLabel(user.merchantRole) : user.role.replace('_', ' ');

  return (
    <header
      className="main-header"
      style={{ height:60,background:T.surface,borderBottom:`1px solid ${T.border}`,display:'flex',alignItems:'center',justifyContent:'space-between',padding:'0 20px',position:'fixed',top:'var(--demo-banner-h, 0px)',left:fullWidth?0:248,right:0,zIndex:90,boxShadow:'0 1px 4px rgba(0,0,0,0.06)' }}
    >
      <style>{`@keyframes c5menuin{from{opacity:0;transform:translateY(-6px) scale(0.98);}to{opacity:1;transform:translateY(0) scale(1);}}`}</style>
      <div style={{ display:'flex',alignItems:'center',gap:12 }}>
        {!fullWidth && <button
          onClick={onMenuClick}
          className="hamburger"
          style={{ display:'none',background:'none',border:'none',cursor:'pointer',color:T.textMuted,padding:4,alignItems:'center' }}
        ><Icon name="menu" size={22} /></button>}
        <div>
          {isDashboard ? (
            <>
              <h1 style={{ fontSize:16,fontWeight:800,color:T.textMain,margin:0 }}>{greeting}, {greetName} <span aria-hidden style={{ fontSize:15 }}>👋</span></h1>
              <p style={{ fontSize:10,color:T.textMuted,margin:0 }}>{dateStr} • {timeStr}</p>
            </>
          ) : (
            <>
              <h1 style={{ fontSize:16,fontWeight:800,color:T.textMain,margin:0 }}>{title}</h1>
              <p style={{ fontSize:10,color:T.textMuted,margin:0 }}>{dateStr}</p>
            </>
          )}
        </div>
      </div>

      <div style={{ display:'flex',alignItems:'center',gap:12 }}>
        <ThemeToggle compact />
        <div style={{ position:'relative' }} ref={notifRef}>
          <div
            onClick={toggleNotif}
            style={{ cursor:'pointer',width:36,height:36,display:'flex',alignItems:'center',justifyContent:'center',borderRadius:10,background:menu==='notif'?T.infoBg:'transparent',transition:'background 0.2s',position:'relative' }}
          >
            <Icon name="bell" size={19} color={T.textMuted} weight={menu==='notif' ? 'fill' : 'regular'} />
            {unread > 0 && (
              <span style={{ position:'absolute',top:2,right:2,background:T.danger,color:'#fff',borderRadius:'50%',minWidth:15,height:15,padding:'0 3px',display:'flex',alignItems:'center',justifyContent:'center',fontSize:8,fontWeight:800,boxSizing:'border-box' }}>{unread > 9 ? '9+' : unread}</span>
            )}
          </div>

          {menu==='notif' && (
            <div style={{ ...glass,position:'absolute',right:0,top:44,width:320,maxHeight:420,display:'flex',flexDirection:'column',zIndex:200,overflow:'hidden' }}>
              <div style={{ padding:'12px 16px',borderBottom:`1px solid ${T.borderLight}`,display:'flex',justifyContent:'space-between',alignItems:'center',gap:8 }}>
                <span style={{ fontWeight:800,fontSize:13 }}>Notifications{unread > 0 ? ` (${unread})` : ''}</span>
                <div style={{ display:'flex',gap:12 }}>
                  <span
                    onClick={items.some(n=>!n.read) ? markAllRead : undefined}
                    style={{ fontSize:11,color:items.some(n=>!n.read)?T.blue:T.textLight,cursor:items.some(n=>!n.read)?'pointer':'default',fontWeight:700 }}
                  >Mark all read</span>
                  <span
                    onClick={items.length ? clearAll : undefined}
                    style={{ fontSize:11,color:items.length?T.danger:T.textLight,cursor:items.length?'pointer':'default',fontWeight:700 }}
                  >Clear</span>
                </div>
              </div>

              <div style={{ overflowY:'auto' }}>
                {items.length === 0 ? (
                  <div style={{ padding:'28px 16px',textAlign:'center',color:T.textMuted,fontSize:12 }}>No notifications</div>
                ) : (
                  items.map((n) => (
                    <div key={n.id} style={{ padding:'11px 16px',display:'flex',gap:10,borderBottom:`1px solid ${T.borderLight}`,background:n.read?'transparent':`color-mix(in srgb, ${T.blue} 8%, transparent)` }}>
                      <div style={{ width:28,height:28,borderRadius:8,background:T.surface,border:`1px solid ${T.border}`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:13,flexShrink:0 }}>{n.icon}</div>
                      <div style={{ flex:1 }}>
                        <p style={{ fontSize:12,color:T.textMain,margin:0,fontWeight:n.read?500:700 }}>{n.message}</p>
                        <p style={{ fontSize:10,color:T.textMuted,margin:0 }}>{timeAgo(n.createdAt)}</p>
                      </div>
                      {!n.read && <span style={{ width:7,height:7,borderRadius:'50%',background:T.blue,flexShrink:0,marginTop:5 }}/>}
                    </div>
                  ))
                )}
              </div>

              {/* Quick preview only — the full list (search / filter / pagination) lives on its
                  own page; existing popup behaviour above is unchanged. */}
              {onNavigate && (
                <div
                  onClick={() => go('notifications')}
                  style={{ padding:'10px 16px',borderTop:`1px solid ${T.borderLight}`,textAlign:'center',fontSize:11.5,fontWeight:700,color:T.blue,cursor:'pointer',flexShrink:0 }}
                >View All Notifications</div>
              )}
            </div>
          )}
        </div>

        <div style={{ position:'relative' }} ref={profileRef}>
          <div
            onClick={toggleProfile}
            style={{ display:'flex',alignItems:'center',gap:8,padding:'4px 8px',borderRadius:10,background:menu==='profile'?T.infoBg:T.canvas,cursor:'pointer',transition:'background 0.2s' }}
          >
            {user.avatar
              ? <img src={user.avatar} alt={user.name} style={{ width:30,height:30,borderRadius:'50%',objectFit:'cover',border:`1px solid ${T.border}` }}/>
              : <div style={{ width:30,height:30,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:12,fontWeight:700,color:'#fff' }}>{user.name.charAt(0)}</div>}
            <div style={{ display:'flex',flexDirection:'column' }}>
              <p style={{ fontSize:11,fontWeight:700,color:T.textMain,margin:0 }}>{user.name.split(' ')[0]}</p>
              <p style={{ fontSize:9,color:T.textMuted,margin:0 }}>{roleLabel}</p>
            </div>
          </div>

          {menu==='profile' && (
            <div style={{ ...glass,position:'absolute',right:0,top:48,width:300,zIndex:200,overflow:'hidden' }}>
              {/* Identity block */}
              <div style={{ padding:'16px',display:'flex',alignItems:'center',gap:12,borderBottom:`1px solid ${T.borderLight}` }}>
                {user.avatar
                  ? <img src={user.avatar} alt={user.name} style={{ width:44,height:44,borderRadius:'50%',objectFit:'cover',border:`1px solid ${T.border}` }}/>
                  : <div style={{ width:44,height:44,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:18,fontWeight:800,color:'#fff' }}>{user.name.charAt(0)}</div>}
                <div style={{ minWidth:0 }}>
                  <p style={{ fontSize:14,fontWeight:800,color:T.textMain,margin:0,whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis' }}>{user.name}</p>
                  <p style={{ fontSize:11,color:T.textMuted,margin:'1px 0 0' }}>{roleLabel}</p>
                  <span style={{ display:'inline-flex',alignItems:'center',gap:5,marginTop:4,fontSize:10,fontWeight:700,color:T.success }}>
                    <span style={{ width:7,height:7,borderRadius:'50%',background:T.success }}/> Online
                  </span>
                </div>
              </div>
              {/* Details — each row hidden when its value is absent */}
              <div style={{ padding:'10px 16px',borderBottom:`1px solid ${T.borderLight}`,display:'flex',flexDirection:'column',gap:7 }}>
                {user.merchantCode && <ProfileRow k="Merchant ID" v={user.merchantCode} />}
                {user.email && <ProfileRow k="Email" v={user.email} />}
                {user.phone && <ProfileRow k="Phone" v={user.phone} />}
                {user.lastLogin && <ProfileRow k="Last Login" v={formatDateTime(user.lastLogin)} />}
              </div>
              {/* Actions */}
              <div style={{ padding:6 }}>
                <MenuItem icon="user" label="Profile" onClick={() => go('profile')} />
                <MenuItem icon="password" label="Change Password" onClick={() => go('profile')} />
                <MenuItem icon="logout" label="Logout" danger onClick={() => { setMenu(null); onLogout?.(); }} />
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
};

const ProfileRow: React.FC<{ k: string; v: string }> = ({ k, v }) => (
  <div style={{ display:'flex',justifyContent:'space-between',gap:12,fontSize:11 }}>
    <span style={{ color:T.textMuted,flexShrink:0 }}>{k}</span>
    <span style={{ color:T.textMain,fontWeight:700,whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis' }}>{v}</span>
  </div>
);

const MenuItem: React.FC<{ icon: string; label: string; onClick: () => void; danger?: boolean }> = ({ icon, label, onClick, danger }) => (
  <button
    type="button" onClick={onClick}
    style={{ width:'100%',display:'flex',alignItems:'center',gap:10,padding:'9px 10px',border:'none',background:'transparent',borderRadius:9,cursor:'pointer',fontSize:12.5,fontWeight:700,color:danger?T.danger:T.textMain,fontFamily:'inherit',textAlign:'left' }}
    onMouseEnter={(e)=>{ e.currentTarget.style.background = danger ? T.dangerBg : T.canvas; }}
    onMouseLeave={(e)=>{ e.currentTarget.style.background = 'transparent'; }}
  >
    <Icon name={icon as never} size={16} color={danger?T.danger:T.textMuted} /> {label}
  </button>
);

export default Header;
