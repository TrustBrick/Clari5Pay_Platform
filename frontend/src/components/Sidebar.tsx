import React from 'react';
import { T } from '../utils/theme';
import { NAV } from '../utils/nav';
import { Logo } from './UI';
import type { User } from '../types';

interface SidebarProps {
  user: User;
  active: string;
  onNav: (key: string) => void;
  onLogout: () => void;
  open: boolean;
  onClose: () => void;
}

const Sidebar: React.FC<SidebarProps> = ({ user, active, onNav, onLogout, open, onClose }) => {
  const nav = NAV[user.role] || [];

  return (
    <>
      {open && (
        <div
          onClick={onClose}
          className="mob-overlay"
          style={{ position:'fixed',inset:0,background:'rgba(0,0,0,0.5)',zIndex:99,display:'none' }}
        />
      )}
      <aside
        className={`sidebar ${open ? 'open' : ''}`}
        style={{ width:248,height:'100vh',background:T.sidebar,display:'flex',flexDirection:'column',position:'fixed',left:0,top:0,zIndex:100,boxShadow:'4px 0 24px rgba(0,0,0,0.2)',transition:'transform 0.3s ease' }}
      >
        {/* Brand */}
        <div style={{ padding:'20px 18px 14px',borderBottom:'1px solid rgba(255,255,255,0.08)' }}>
          <div style={{ display:'flex',justifyContent:'center',marginBottom:8 }}>
            <div style={{ transform:'scale(0.6)',transformOrigin:'center',marginTop:-28,marginBottom:-28 }}>
              <Logo size="sm"/>
            </div>
          </div>
          <div style={{ textAlign:'center',marginTop:4 }}>
            <span style={{ background:'rgba(0,82,204,0.25)',border:'1px solid rgba(0,82,204,0.4)',color:'#7eb8ff',fontSize:10,fontWeight:700,padding:'2px 10px',borderRadius:20,letterSpacing:'0.08em' }}>
              {user.role.replace('_',' ')}
            </span>
          </div>
        </div>

        {/* User */}
        <div style={{ padding:'12px 16px',borderBottom:'1px solid rgba(255,255,255,0.06)',display:'flex',alignItems:'center',gap:10 }}>
          <div style={{ width:36,height:36,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:15,fontWeight:700,color:'#fff',flexShrink:0 }}>
            {user.name.charAt(0)}
          </div>
          <div style={{ overflow:'hidden',flex:1 }}>
            <p style={{ color:'#fff',fontSize:12,fontWeight:700,whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',margin:0 }}>{user.name}</p>
            <p style={{ color:'rgba(255,255,255,0.4)',fontSize:10,margin:0 }}>{user.email}</p>
          </div>
          <div
            onClick={onClose}
            className="mob-close"
            style={{ cursor:'pointer',color:'rgba(255,255,255,0.3)',fontSize:18,display:'none' }}
          >✕</div>
        </div>

        {/* Nav */}
        <nav style={{ flex:1,padding:'10px',overflowY:'auto' }}>
          {nav.map(item => (
            <div
              key={item.key}
              onClick={() => { onNav(item.key); onClose(); }}
              style={{
                display:'flex',alignItems:'center',gap:10,padding:'9px 12px',borderRadius:10,cursor:'pointer',marginBottom:2,
                background:active===item.key ? T.sidebarActive : 'transparent',
                color:active===item.key ? '#7eb8ff' : 'rgba(255,255,255,0.6)',
                fontWeight:active===item.key ? 700 : 500,
                fontSize:13,transition:'all 0.15s ease',
                borderLeft:active===item.key ? `3px solid ${T.blue}` : '3px solid transparent',
              }}
              onMouseEnter={e => { if(active!==item.key){ (e.currentTarget as HTMLDivElement).style.background=T.sidebarHover; (e.currentTarget as HTMLDivElement).style.color='rgba(255,255,255,0.9)'; } }}
              onMouseLeave={e => { if(active!==item.key){ (e.currentTarget as HTMLDivElement).style.background='transparent'; (e.currentTarget as HTMLDivElement).style.color='rgba(255,255,255,0.6)'; } }}
            >
              <span style={{ fontSize:15,width:20,textAlign:'center',flexShrink:0 }}>{item.icon}</span>
              <span style={{ flex:1 }}>{item.label}</span>
              {item.badge && (
                <span style={{ background:T.danger,color:'#fff',borderRadius:10,fontSize:10,padding:'1px 7px',fontWeight:800 }}>{item.badge}</span>
              )}
            </div>
          ))}
        </nav>

        <div style={{ padding:'10px',borderTop:'1px solid rgba(255,255,255,0.08)' }}>
          <div
            onClick={onLogout}
            style={{ display:'flex',alignItems:'center',gap:10,padding:'9px 12px',borderRadius:10,cursor:'pointer',color:'rgba(255,255,255,0.45)',fontSize:13,fontWeight:500,transition:'all 0.15s ease' }}
            onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.background='rgba(220,38,38,0.12)'; (e.currentTarget as HTMLDivElement).style.color=T.danger; }}
            onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background='transparent'; (e.currentTarget as HTMLDivElement).style.color='rgba(255,255,255,0.45)'; }}
          >
            <span>⎋</span> Sign Out
          </div>
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
