import React, { useState } from 'react';
import { T } from '../utils/theme';
import type { User } from '../types';

interface HeaderProps {
  user: User;
  title: string;
  onMenuClick: () => void;
}

const NOTIFS = [
  { icon: '✓', color: '#059669', msg: 'DEP0000001 completed successfully', time: '2m ago' },
  { icon: '⚠', color: '#d97706', msg: 'WIT0000001 awaiting review', time: '15m ago' },
  { icon: '↓', color: '#0052cc', msg: 'New deposit from Nexus Fintech', time: '2h ago' },
];

const Header: React.FC<HeaderProps> = ({ user, title, onMenuClick }) => {
  const [notif, setNotif] = useState(false);

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
        <div style={{ position:'relative' }}>
          <div
            onClick={() => setNotif(!notif)}
            style={{ cursor:'pointer',width:36,height:36,display:'flex',alignItems:'center',justifyContent:'center',borderRadius:10,background:notif?T.infoBg:'transparent',transition:'background 0.2s',position:'relative' }}
          >
            <span style={{ fontSize:17 }}>🔔</span>
            <span style={{ position:'absolute',top:4,right:4,background:T.danger,color:'#fff',borderRadius:'50%',width:15,height:15,display:'flex',alignItems:'center',justifyContent:'center',fontSize:8,fontWeight:800 }}>3</span>
          </div>

          {notif && (
            <div style={{ position:'absolute',right:0,top:44,width:300,background:T.surface,borderRadius:14,boxShadow:'0 16px 48px rgba(0,0,0,0.14)',border:`1px solid ${T.border}`,zIndex:200,overflow:'hidden' }}>
              <div style={{ padding:'12px 16px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
                <span style={{ fontWeight:800,fontSize:13 }}>Notifications</span>
                <span onClick={() => setNotif(false)} style={{ fontSize:11,color:T.blue,cursor:'pointer',fontWeight:700 }}>Clear all</span>
              </div>
              {NOTIFS.map((n,i) => (
                <div key={i} style={{ padding:'11px 16px',display:'flex',gap:10,borderBottom:`1px solid ${T.borderLight}`,cursor:'pointer' }}
                  onMouseEnter={e => (e.currentTarget as HTMLDivElement).style.background=T.canvas}
                  onMouseLeave={e => (e.currentTarget as HTMLDivElement).style.background='transparent'}>
                  <div style={{ width:28,height:28,borderRadius:8,background:`${n.color}15`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:12,color:n.color,flexShrink:0 }}>{n.icon}</div>
                  <div>
                    <p style={{ fontSize:12,color:T.textMain,margin:0,fontWeight:500 }}>{n.msg}</p>
                    <p style={{ fontSize:10,color:T.textMuted,margin:0 }}>{n.time}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ display:'flex',alignItems:'center',gap:8,padding:'4px 8px',borderRadius:10,background:T.canvas }}>
          <div style={{ width:30,height:30,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:12,fontWeight:700,color:'#fff' }}>{user.name.charAt(0)}</div>
          <div style={{ display:'flex',flexDirection:'column' }}>
            <p style={{ fontSize:11,fontWeight:700,color:T.textMain,margin:0 }}>{user.name.split(' ')[0]}</p>
            <p style={{ fontSize:9,color:T.textMuted,margin:0 }}>{user.role.replace('_',' ')}</p>
          </div>
        </div>
      </div>
    </header>
  );
};

export default Header;
