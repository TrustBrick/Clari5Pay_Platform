import React, { useState, useEffect, useCallback, useRef } from 'react';
import { T } from '../utils/theme';
import { timeAgo, merchantRoleLabel } from '../utils/helpers';
import ThemeToggle from './ThemeToggle';
import { notificationAPI } from '../services/api';
import type { Notification, User } from '../types';

interface HeaderProps {
  user: User;
  title: string;
  onMenuClick: () => void;
}

const Header: React.FC<HeaderProps> = ({ user, title, onMenuClick }) => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Notification[]>([]);
  const boxRef = useRef<HTMLDivElement>(null);

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

  // Close the dropdown when clicking outside it.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const unread = items.filter((n) => !n.read).length;

  const toggleOpen = () => {
    const next = !open;
    setOpen(next);
    if (next) load();
  };

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

  return (
    <header
      className="main-header"
      style={{ height:60,background:T.surface,borderBottom:`1px solid ${T.border}`,display:'flex',alignItems:'center',justifyContent:'space-between',padding:'0 20px',position:'fixed',top:0,left:248,right:0,zIndex:90,boxShadow:'0 1px 4px rgba(0,0,0,0.06)' }}
    >
      <div style={{ display:'flex',alignItems:'center',gap:12 }}>
        <button
          onClick={onMenuClick}
          className="hamburger"
          style={{ display:'none',background:'none',border:'none',fontSize:22,cursor:'pointer',color:T.textMuted,padding:4 }}
        >☰</button>
        <div>
          <h1 style={{ fontSize:16,fontWeight:800,color:T.textMain,margin:0 }}>{title}</h1>
          <p style={{ fontSize:10,color:T.textMuted,margin:0 }}>
            {new Date().toLocaleDateString('en-IN',{weekday:'long',year:'numeric',month:'long',day:'numeric'})}
          </p>
        </div>
      </div>

      <div style={{ display:'flex',alignItems:'center',gap:12 }}>
        <ThemeToggle compact />
        <div style={{ position:'relative' }} ref={boxRef}>
          <div
            onClick={toggleOpen}
            style={{ cursor:'pointer',width:36,height:36,display:'flex',alignItems:'center',justifyContent:'center',borderRadius:10,background:open?T.infoBg:'transparent',transition:'background 0.2s',position:'relative' }}
          >
            <span style={{ fontSize:17 }}>🔔</span>
            {unread > 0 && (
              <span style={{ position:'absolute',top:2,right:2,background:T.danger,color:'#fff',borderRadius:'50%',minWidth:15,height:15,padding:'0 3px',display:'flex',alignItems:'center',justifyContent:'center',fontSize:8,fontWeight:800,boxSizing:'border-box' }}>{unread > 9 ? '9+' : unread}</span>
            )}
          </div>

          {open && (
            <div style={{ position:'absolute',right:0,top:44,width:320,maxHeight:420,display:'flex',flexDirection:'column',background:T.surface,borderRadius:14,boxShadow:'0 16px 48px rgba(0,0,0,0.14)',border:`1px solid ${T.border}`,zIndex:200,overflow:'hidden' }}>
              <div style={{ padding:'12px 16px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center',gap:8 }}>
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
                    <div key={n.id} style={{ padding:'11px 16px',display:'flex',gap:10,borderBottom:`1px solid ${T.borderLight}`,background:n.read?'transparent':T.infoBg }}>
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
            </div>
          )}
        </div>

        <div style={{ display:'flex',alignItems:'center',gap:8,padding:'4px 8px',borderRadius:10,background:T.canvas }}>
          {user.avatar
            ? <img src={user.avatar} alt={user.name} style={{ width:30,height:30,borderRadius:'50%',objectFit:'cover',border:`1px solid ${T.border}` }}/>
            : <div style={{ width:30,height:30,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:12,fontWeight:700,color:'#fff' }}>{user.name.charAt(0)}</div>}
          <div style={{ display:'flex',flexDirection:'column' }}>
            <p style={{ fontSize:11,fontWeight:700,color:T.textMain,margin:0 }}>{user.name.split(' ')[0]}</p>
            <p style={{ fontSize:9,color:T.textMuted,margin:0 }}>
              {user.role === 'MERCHANT' && user.merchantRole ? merchantRoleLabel(user.merchantRole) : user.role.replace('_',' ')}
            </p>
          </div>
        </div>
      </div>
    </header>
  );
};

export default Header;
