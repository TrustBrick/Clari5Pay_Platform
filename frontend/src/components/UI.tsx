import React, { CSSProperties, useState } from 'react';
import { T } from '../utils/theme';
import { statusStyle, statusLabel } from '../utils/helpers';
import type { TxStatus, ChartDataPoint } from '../types';

// ─── Logo ────────────────────────────────────────────────────────────────────
export const Logo: React.FC<{ size?: 'sm' | 'md' | 'lg'; dark?: boolean }> = ({ size = 'md', dark = false }) => {
  const scale = size === 'sm' ? 0.38 : size === 'lg' ? 0.7 : 0.5;
  // The shield icon is the brand image (public/logo-mark.png, transparent); the wordmark +
  // tagline stay as text so their colours adapt on dark backgrounds (navy "pay"/tagline → light).
  // On dark backgrounds the navy "pay" + grey tagline are invisible — switch them to light.
  const clari = dark ? '#4d9fff' : '#0052cc';
  const pay = dark ? '#ffffff' : '#0a2540';
  const tag = dark ? 'rgba(255,255,255,0.6)' : '#4a5568';
  // Inline styles (not shared CSS classes) so multiple logos on one page don't clash.
  const F = "'Montserrat','Segoe UI',Arial,sans-serif";
  const sClari = { fontFamily: F, fontWeight: 700, fill: clari, fontSize: '56px', letterSpacing: '-1px' } as const;
  const s5 = { fontFamily: F, fontWeight: 700, fill: '#26d00c', fontSize: '62px' } as const;
  const sPay = { fontFamily: F, fontWeight: 700, fill: pay, fontSize: '56px', letterSpacing: '-1px' } as const;
  const sTag = { fontFamily: "'Segoe UI',Arial,sans-serif", fontWeight: 500, fill: tag, fontSize: '13.5px', letterSpacing: '0.5px' } as const;
  return (
    <svg viewBox="0 0 650 220" style={{ width: 650 * scale, height: 220 * scale, maxWidth: '100%' }}>
      <image href="/logo-mark.png" x="15" y="32" width="160" height="160" preserveAspectRatio="xMidYMid meet" />
      <g transform="translate(195,122)">
        <text x="0" y="0">
          <tspan style={sClari}>clari</tspan>
          <tspan style={s5}>5</tspan>
          <tspan style={sPay}>pay</tspan>
        </text>
      </g>
      <g transform="translate(195,155)">
        <line x1="0" y1="-5" x2="25" y2="-5" stroke="#0052cc" strokeWidth="2" strokeLinecap="round"/>
        <text x="35" y="0" style={sTag}>Secure Payments. Trusted Always.</text>
        <line x1="285" y1="-5" x2="310" y2="-5" stroke="#26d00c" strokeWidth="2" strokeLinecap="round"/>
      </g>
    </svg>
  );
};

// ─── Badge ───────────────────────────────────────────────────────────────────
// Statuses that are still "in flight" → their dot gently pulses to signal processing.
const INFLIGHT_STATUSES = new Set(['PENDING','ADMIN_APPROVED','ACCOUNT_REQUESTED','ACCOUNT_SUBMITTED','SLIP_SUBMITTED']);
export const Badge: React.FC<{ status: TxStatus; type?: string; viewerRole?: string }> = ({ status, type, viewerRole }) => {
  const s = statusStyle(status);
  return (
    <span style={{ display:'inline-flex',alignItems:'center',gap:4,padding:'3px 10px',borderRadius:20,fontSize:11,fontWeight:600,color:s.color,background:s.bg,whiteSpace:'nowrap' }}>
      <span className={INFLIGHT_STATUSES.has(status) ? 'c5-dot-pulse' : undefined}
        style={{ width:6,height:6,borderRadius:'50%',background:s.color,display:'inline-block' }}/>
      {statusLabel(status, type, viewerRole)}
    </span>
  );
};

// ─── RiskBadge ───────────────────────────────────────────────────────────────
export const RiskBadge: React.FC<{ risk: string }> = ({ risk }) => {
  const c = ({ HIGH:T.danger, MEDIUM:T.warning, LOW:T.success } as Record<string,string>)[risk] || T.textMuted;
  const bg = ({ HIGH:T.dangerBg, MEDIUM:T.warningBg, LOW:T.successBg } as Record<string,string>)[risk] || T.borderLight;
  return <span style={{padding:'2px 8px',borderRadius:6,fontSize:11,fontWeight:700,color:c,background:bg}}>{risk}</span>;
};

// ─── Card ─────────────────────────────────────────────────────────────────────
export const Card: React.FC<{ children: React.ReactNode; style?: CSSProperties; glow?: boolean }> = ({ children, style={}, glow }) => (
  <div style={{ background:T.surface,borderRadius:16,boxShadow:glow?`0 0 0 1px ${T.blue}30,0 8px 32px rgba(0,82,204,0.1)`:'0 4px 6px -1px rgba(0,0,0,0.07),0 2px 4px -1px rgba(0,0,0,0.04)',border:`1px solid ${T.border}`,overflow:'hidden',...style }}>
    {children}
  </div>
);

// ─── StatCard ─────────────────────────────────────────────────────────────────
export const StatCard: React.FC<{
  icon: string; label: string; value: string | number; sub?: string;
  color?: string; trend?: number; gradient?: string;
}> = ({ icon, label, value, sub, color=T.blue, trend, gradient }) => {
  // Shrink the value font for long strings (e.g. "INR 1,79,000.00") so it stays on one line.
  const len = String(value).length;
  const valueSize = len > 13 ? 17 : len > 10 ? 20 : 24;
  return (
  <Card style={{ padding:'18px 18px',position:'relative',overflow:'hidden' }}>
    <div style={{ position:'absolute',top:-20,right:-20,width:100,height:100,borderRadius:'50%',background:`${color}10`,pointerEvents:'none' }}/>
    <div style={{ display:'flex',alignItems:'flex-start',justifyContent:'space-between',position:'relative',gap:10 }}>
      <div style={{ minWidth:0,flex:1 }}>
        <p style={{ fontSize:11,color:T.textMuted,fontWeight:700,textTransform:'uppercase',letterSpacing:'0.06em',marginBottom:6,lineHeight:1.3 }}>{label}</p>
        <p style={{ fontSize:valueSize,fontWeight:800,color:T.textMain,lineHeight:1.2,whiteSpace:'nowrap' }}>{value}</p>
        {sub && <p style={{ fontSize:11,color:T.textMuted,marginTop:4 }}>{sub}</p>}
        {trend!==undefined && <p style={{ fontSize:11,marginTop:6,color:trend>=0?T.success:T.danger,fontWeight:700 }}>{trend>=0?'▲':'▼'} {Math.abs(trend)}% vs last week</p>}
      </div>
      <div style={{ width:40,height:40,borderRadius:12,background:gradient||`${color}18`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:19,flexShrink:0 }}>{icon}</div>
    </div>
  </Card>
  );
};

// ─── Btn ──────────────────────────────────────────────────────────────────────
type BtnVariant = 'primary'|'secondary'|'danger'|'success'|'ghost'|'green'|'dark';
export const Btn: React.FC<{
  children: React.ReactNode; variant?: BtnVariant; size?: 'sm'|'md'|'lg';
  onClick?: () => void; type?: 'button'|'submit'; style?: CSSProperties;
  disabled?: boolean; full?: boolean;
}> = ({ children, variant='primary', size='md', onClick, type='button', style={}, disabled, full }) => {
  const base: CSSProperties = { display:'inline-flex',alignItems:'center',gap:6,borderRadius:10,fontWeight:700,cursor:disabled?'not-allowed':'pointer',border:'none',transition:'all 0.2s ease',opacity:disabled?0.6:1,fontSize:size==='sm'?12:size==='lg'?15:13,padding:size==='sm'?'5px 12px':size==='lg'?'13px 28px':'9px 18px',width:full?'100%':'auto',justifyContent:full?'center':'flex-start',fontFamily:'inherit' };
  const vars: Record<BtnVariant,CSSProperties> = {
    primary:{ background:T.grad1,color:'#fff',boxShadow:`0 4px 14px ${T.blue}40` },
    secondary:{ background:T.borderLight,color:T.textMain },
    danger:{ background:T.dangerBg,color:T.danger },
    success:{ background:T.successBg,color:T.success },
    ghost:{ background:'transparent',color:T.blue,border:`1.5px solid ${T.blue}` },
    green:{ background:T.grad2,color:'#fff',boxShadow:`0 4px 14px ${T.green}40` },
    dark:{ background:T.dark,color:'#fff' },
  };
  return <button type={type} onClick={onClick} disabled={disabled} className="c5-btn" style={{ ...base,...vars[variant],...style }}>{children}</button>;
};

// ─── Input ───────────────────────────────────────────────────────────────────
export const Input: React.FC<{
  label?: string; type?: string; value: string; onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string; required?: boolean; hint?: string; icon?: string; style?: CSSProperties; list?: string;
}> = ({ label, type='text', value, onChange, placeholder, required, hint, icon, style={}, list }) => (
  <div style={{ marginBottom:16,...style }}>
    {label && <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}{required&&<span style={{color:T.danger}}> *</span>}</label>}
    <div style={{ position:'relative' }}>
      {icon && <span style={{ position:'absolute',left:12,top:'50%',transform:'translateY(-50%)',fontSize:16,color:T.textMuted }}>{icon}</span>}
      <input type={type} value={value} onChange={onChange} placeholder={placeholder} required={required} list={list}
        style={{ width:'100%',padding:icon?'10px 12px 10px 38px':'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',transition:'border-color 0.2s,box-shadow 0.2s',fontFamily:'inherit' }}
        onFocus={e=>{e.target.style.borderColor=T.blue;e.target.style.boxShadow=`0 0 0 3px ${T.blue}18`;}}
        onBlur={e=>{e.target.style.borderColor=T.border;e.target.style.boxShadow='none';}}/>
    </div>
    {hint && <p style={{ fontSize:11,color:T.textMuted,marginTop:4 }}>{hint}</p>}
  </div>
);

// Shared <datalist> of Indian bank names — render once, reference via Input list="bank-names".
export const BankNamesDatalist: React.FC<{ names: string[] }> = ({ names }) => (
  <datalist id="bank-names">{names.map(n => <option key={n} value={n} />)}</datalist>
);

// ─── Sel ─────────────────────────────────────────────────────────────────────
export const Sel: React.FC<{
  label?: string; value: string; onChange: (e: React.ChangeEvent<HTMLSelectElement>) => void;
  options: Array<{value:string;label:string}>; required?: boolean;
}> = ({ label, value, onChange, options, required }) => (
  <div style={{ marginBottom:16 }}>
    {label && <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}{required&&<span style={{color:T.danger}}> *</span>}</label>}
    <select value={value} onChange={onChange}
      style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',appearance:'none',backgroundImage:`url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24'%3E%3Cpath fill='%236b7280' d='M7 10l5 5 5-5z'/%3E%3C/svg%3E")`,backgroundRepeat:'no-repeat',backgroundPosition:'right 12px center',cursor:'pointer',fontFamily:'inherit' }}
      onFocus={e=>{e.target.style.borderColor=T.blue;}}
      onBlur={e=>{e.target.style.borderColor=T.border;}}>
      {options.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  </div>
);

// ─── MiniBar Chart ───────────────────────────────────────────────────────────
export const MiniBar: React.FC<{ data: ChartDataPoint[] }> = ({ data }) => {
  const max = Math.max(1, ...data.flatMap(d=>[d.deposit,d.withdrawal]));
  return (
    <div style={{ display:'flex',alignItems:'flex-end',gap:6,height:90,padding:'0 4px' }}>
      {data.map((d,i)=>(
        <div key={i} style={{ flex:1,display:'flex',flexDirection:'column',alignItems:'center',gap:2 }}>
          <div style={{ width:'100%',display:'flex',gap:2,alignItems:'flex-end',height:72 }}>
            <div style={{ flex:1,background:T.grad1,borderRadius:'4px 4px 0 0',height:`${(d.deposit/max)*100}%`,opacity:0.85 }}/>
            <div style={{ flex:1,background:T.danger,borderRadius:'4px 4px 0 0',height:`${(d.withdrawal/max)*100}%`,opacity:0.6 }}/>
          </div>
          <span style={{ fontSize:9,color:T.textMuted,fontWeight:600 }}>{d.day}</span>
        </div>
      ))}
    </div>
  );
};

// ─── StatusChart (real-time status breakdown) ─────────────────────────────────
export const StatusChart: React.FC<{ data: Array<{ label: string; value: number; color: string }> }> = ({ data }) => {
  const max = Math.max(1, ...data.map(d => d.value));
  return (
    <div style={{ display:'flex',alignItems:'flex-end',gap:14,height:140,padding:'4px 4px 0' }}>
      {data.map(d => (
        <div key={d.label} style={{ flex:1,display:'flex',flexDirection:'column',alignItems:'center',gap:6 }}>
          <span style={{ fontSize:14,fontWeight:800,color:d.color }}>{d.value}</span>
          <div style={{ width:'100%',height:88,display:'flex',alignItems:'flex-end' }}>
            <div style={{ width:'100%',background:d.color,borderRadius:'6px 6px 0 0',height:`${(d.value/max)*100}%`,minHeight:d.value>0?6:2,opacity:d.value>0?0.9:0.25,transition:'height 0.4s ease' }}/>
          </div>
          <span style={{ fontSize:9,color:T.textMuted,fontWeight:600,textAlign:'center',lineHeight:1.2 }}>{d.label}</span>
        </div>
      ))}
    </div>
  );
};

// ─── Modal ───────────────────────────────────────────────────────────────────
export const Modal: React.FC<{ title:string; children:React.ReactNode; onClose:()=>void; wide?:boolean; xl?:boolean }> = ({ title, children, onClose, wide, xl }) => (
  <div style={{ position:'fixed',inset:0,background:'rgba(10,37,64,0.6)',zIndex:500,display:'flex',alignItems:'center',justifyContent:'center',padding:16,backdropFilter:'blur(4px)' }}>
    <div className="c5-pop" style={{ background:T.surface,borderRadius:20,width:'100%',maxWidth:xl?1040:wide?740:520,maxHeight:'90vh',overflowY:'auto',boxShadow:'0 24px 80px rgba(0,0,0,0.25)' }}>
      <div style={{ padding:'20px 24px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center',position:'sticky',top:0,background:T.surface,zIndex:1 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800,color:T.textMain }}>{title}</h2>
        <button onClick={onClose} style={{ background:'none',border:'none',fontSize:20,cursor:'pointer',color:T.textMuted,borderRadius:8,width:32,height:32,display:'flex',alignItems:'center',justifyContent:'center' }}>✕</button>
      </div>
      <div style={{ padding:'20px 24px' }}>{children}</div>
    </div>
  </div>
);

// ─── LoadingScreen (logo + spinner, shown while a page fetches its data) ───────
export const LoadingScreen: React.FC<{ label?: string }> = ({ label = 'Loading…' }) => (
  <div style={{ display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',minHeight:'60vh',gap:18 }}>
    <Logo size="md"/>
    <div style={{ width:34,height:34,border:`3px solid ${T.border}`,borderTopColor:T.blue,borderRadius:'50%',animation:'c5spin 0.8s linear infinite' }}/>
    <p style={{ color:T.textMuted,fontSize:13,fontWeight:600 }}>{label}</p>
    <style>{`@keyframes c5spin{to{transform:rotate(360deg)}}`}</style>
  </div>
);

// ─── Skeletons (shimmer placeholders for perceived speed while data loads) ─────
export const Skeleton: React.FC<{ w?: number | string; h?: number; style?: CSSProperties }> = ({ w='100%', h=14, style={} }) => (
  <div className="c5-skel" style={{ width:w, height:h, ...style }} />
);

export const TableSkeleton: React.FC<{ rows?: number; cols?: number }> = ({ rows=6, cols=6 }) => (
  <div>
    {Array.from({ length: rows }).map((_, r) => (
      <div key={r} style={{ display:'flex',gap:14,padding:'13px 14px',borderBottom:`1px solid ${T.borderLight}` }}>
        {Array.from({ length: cols }).map((_, c) => (
          <div key={c} className="c5-skel" style={{ flex:1, height:13 }} />
        ))}
      </div>
    ))}
  </div>
);

// ─── ReasonModal (prompts for a required free-text reason) ─────────────────────
export const ReasonModal: React.FC<{
  title: string; label?: string; confirmLabel?: string; busy?: boolean;
  onSubmit: (reason: string) => void; onClose: () => void;
}> = ({ title, label = 'Reason', confirmLabel = 'Confirm', busy, onSubmit, onClose }) => {
  const [reason, setReason] = useState('');
  return (
    <Modal title={title} onClose={onClose}>
      <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}<span style={{ color:T.danger }}> *</span></label>
      <textarea value={reason} onChange={e=>setReason(e.target.value)} placeholder="Enter a reason..." autoFocus
        style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:80,marginBottom:14 }}/>
      <div style={{ display:'flex',gap:10 }}>
        <Btn onClick={()=>onSubmit(reason.trim())} disabled={busy||!reason.trim()}>{busy?'Saving...':confirmLabel}</Btn>
        <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};
