import React, { CSSProperties, useState, useEffect, useRef } from 'react';
import { T } from '../utils/theme';
import { statusStyle, statusLabel, displayStatus } from '../utils/helpers';
import { Icon, isIconName } from './Icon';
import type { TxStatus, ChartDataPoint } from '../types';

// ─── CountUp — animate a number from 0 → value on mount / when value changes ────
export const CountUp: React.FC<{
  value: number; duration?: number; format?: (n: number) => string;
}> = ({ value, duration = 900, format = (n) => Math.round(n).toLocaleString('en-IN') }) => {
  const [display, setDisplay] = useState(0);
  const from = useRef(0);
  const raf = useRef<number>();
  useEffect(() => {
    const start = performance.now();
    const begin = from.current;
    const animate = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);          // easeOutCubic
      setDisplay(begin + (value - begin) * eased);
      if (t < 1) raf.current = requestAnimationFrame(animate);
      else from.current = value;
    };
    raf.current = requestAnimationFrame(animate);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [value, duration]);
  return <>{format(display)}</>;
};

// ─── Logo (vertical lockup: shield above the wordmark + tagline) ───────────────
export const Logo: React.FC<{ size?: 'sm' | 'md' | 'lg'; dark?: boolean }> = ({ size = 'md', dark = false }) => {
  // Rendered width (~25% larger than the old horizontal mark). maxWidth keeps it responsive.
  const W = size === 'sm' ? 150 : size === 'lg' ? 290 : 200;
  // The shield is the brand image (public/logo-mark.png, transparent → works on any bg);
  // the wordmark + tagline stay as SVG text so their colours adapt in dark mode.
  // `dark` = rendered on a dark brand splash (login/chooser/sidebar) → force light ink.
  const clari = dark ? '#4d9fff' : '#0052cc';
  const pay = dark ? '#ffffff' : 'var(--c5-text-main)';
  const tag = dark ? 'rgba(255,255,255,0.65)' : 'var(--c5-text-muted)';
  const F = "'Montserrat','Segoe UI',Arial,sans-serif";
  const sClari = { fontFamily: F, fontWeight: 800, fill: clari, fontSize: '52px', letterSpacing: '-1px' } as const;
  const s5 = { fontFamily: F, fontWeight: 800, fill: '#26d00c', fontSize: '58px' } as const;
  const sPay = { fontFamily: F, fontWeight: 800, fill: pay, fontSize: '52px', letterSpacing: '-1px' } as const;
  const sTag = { fontFamily: "'Segoe UI',Arial,sans-serif", fontWeight: 500, fill: tag, fontSize: '15px', letterSpacing: '0.3px' } as const;
  return (
    <svg viewBox="0 0 320 262" width={W} height={W * 262 / 320}
      style={{ maxWidth: '100%', height: 'auto', display: 'block' }}
      role="img" aria-label="Clari5Pay — Secure Payments. Prevent Fraud.">
      {/* Shield, centred on top */}
      <image href="/logo-mark.png" x="97" y="2" width="126" height="126" preserveAspectRatio="xMidYMid meet" />
      {/* Wordmark, centred just below the shield (tightened gap — same size, less empty space) */}
      <text x="160" y="188" textAnchor="middle">
        <tspan style={sClari}>clari</tspan>
        <tspan style={s5}>5</tspan>
        <tspan style={sPay}>pay</tspan>
      </text>
      {/* Tagline with flanking accent dashes */}
      <g transform="translate(160,224)">
        <line x1="-150" y1="-5" x2="-126" y2="-5" stroke="#0052cc" strokeWidth="2" strokeLinecap="round" />
        <text x="0" y="0" textAnchor="middle" style={sTag}>Secure Payments. Prevent Fraud.</text>
        <line x1="126" y1="-5" x2="150" y2="-5" stroke="#26d00c" strokeWidth="2" strokeLinecap="round" />
      </g>
    </svg>
  );
};

// ─── Badge ───────────────────────────────────────────────────────────────────
// Statuses that are still "in flight" → their dot gently pulses to signal processing.
const INFLIGHT_STATUSES = new Set(['PENDING','ADMIN_APPROVED','ACCOUNT_REQUESTED','ACCOUNT_SUBMITTED','SLIP_SUBMITTED','PENDING_APPROVAL','SUPERVISOR_REVIEW','MANAGER_REVIEW','RESUBMITTED']);
export const Badge: React.FC<{ status: TxStatus; type?: string; viewerRole?: string; approverRole?: string | null }> = ({ status, type, viewerRole, approverRole }) => {
  // The colour, the pulse and the label all follow the status this viewer is shown, so a
  // withdrawal the Merchant Portal renders as "Pending" also carries the Pending styling. A request
  // sent to a specific approver reads as that person's role (see displayStatus).
  const shown = displayStatus(status, type, viewerRole, approverRole) as TxStatus;
  const s = statusStyle(shown);
  return (
    <span style={{ display:'inline-flex',alignItems:'center',gap:4,padding:'3px 10px',borderRadius:20,fontSize:11,fontWeight:600,color:s.color,background:s.bg,whiteSpace:'nowrap' }}>
      <span className={INFLIGHT_STATUSES.has(shown) ? 'c5-dot-pulse' : undefined}
        style={{ width:6,height:6,borderRadius:'50%',background:s.color,display:'inline-block' }}/>
      {statusLabel(shown, type, viewerRole)}
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
export const Card: React.FC<{ children: React.ReactNode; style?: CSSProperties; glow?: boolean; className?: string; onClick?: () => void }> = ({ children, style={}, glow, className, onClick }) => (
  <div className={className} onClick={onClick} style={{ background:T.surface,borderRadius:16,boxShadow:glow?`0 0 0 1px ${T.blue}30,0 8px 32px rgba(0,82,204,0.1)`:'0 4px 6px -1px rgba(0,0,0,0.07),0 2px 4px -1px rgba(0,0,0,0.04)',border:`1px solid ${T.border}`,overflow:'hidden',...style }}>
    {children}
  </div>
);

// ─── StatCard ─────────────────────────────────────────────────────────────────
export const StatCard: React.FC<{
  icon: string; label: string; value: React.ReactNode; sub?: string;
  color?: string; trend?: number; gradient?: string; onClick?: () => void; valueLen?: number;
}> = ({ icon, label, value, sub, color=T.blue, trend, gradient, onClick, valueLen }) => {
  // Shrink the value font for long strings (e.g. "INR 1,79,000.00") so it stays on one line.
  const len = valueLen ?? ((typeof value === 'string' || typeof value === 'number') ? String(value).length : 10);
  const valueSize = len > 13 ? 17 : len > 10 ? 20 : 24;
  return (
  <Card className="c5-hover-lift" onClick={onClick} style={{ padding:'18px 18px',position:'relative',overflow:'hidden',cursor:onClick?'pointer':'default' }}>
    <div style={{ position:'absolute',top:-20,right:-20,width:100,height:100,borderRadius:'50%',background:`${color}10`,pointerEvents:'none' }}/>
    <div style={{ display:'flex',alignItems:'flex-start',justifyContent:'space-between',position:'relative',gap:10 }}>
      <div style={{ minWidth:0,flex:1 }}>
        <p style={{ fontSize:11,color:T.textMuted,fontWeight:700,textTransform:'uppercase',letterSpacing:'0.06em',marginBottom:6,lineHeight:1.3 }}>{label}</p>
        <p style={{ fontSize:valueSize,fontWeight:800,color:T.textMain,lineHeight:1.2,whiteSpace:'nowrap' }}>{value}</p>
        {sub && <p style={{ fontSize:11,color:T.textMuted,marginTop:4 }}>{sub}</p>}
        {trend!==undefined && <p style={{ fontSize:11,marginTop:6,color:trend>=0?T.success:T.danger,fontWeight:700 }}>{trend>=0?'▲':'▼'} {Math.abs(trend)}% vs last week</p>}
      </div>
      <div style={{ width:40,height:40,borderRadius:12,background:gradient||`${color}18`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:19,flexShrink:0,color:gradient?'#fff':color }}>
        {isIconName(icon) ? <Icon name={icon} size={22} color={gradient?'#fff':color} /> : icon}
      </div>
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
  readOnly?: boolean; onBlur?: (e: React.FocusEvent<HTMLInputElement>) => void; error?: string;
  inputMode?: React.HTMLAttributes<HTMLInputElement>['inputMode'];
}> = ({ label, type='text', value, onChange, placeholder, required, hint, icon, style={}, list, inputMode, readOnly, onBlur, error }) => (
  <div style={{ marginBottom:16,...style }}>
    {label && <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}{required&&<span style={{color:T.danger}}> *</span>}</label>}
    <div style={{ position:'relative' }}>
      {icon && <span style={{ position:'absolute',left:12,top:'50%',transform:'translateY(-50%)',display:'flex',alignItems:'center',fontSize:16,color:T.textMuted }}>{isIconName(icon) ? <Icon name={icon} size={16} color={T.textMuted} /> : icon}</span>}
      <input type={type} value={value} onChange={onChange} placeholder={placeholder} required={required} list={list} inputMode={inputMode} readOnly={readOnly} aria-invalid={error?true:undefined}
        style={{ width:'100%',padding:icon?'10px 12px 10px 38px':'10px 14px',borderWidth:1.5,borderStyle:'solid',borderColor:error?T.danger:T.border,borderRadius:10,fontSize:14,color:T.textMain,background:readOnly?T.canvas:T.surface,cursor:readOnly?'not-allowed':'text',outline:'none',boxSizing:'border-box',transition:'border-color 0.2s,box-shadow 0.2s',fontFamily:'inherit' }}
        onFocus={e=>{ if(readOnly) return; e.target.style.borderColor=error?T.danger:T.blue;e.target.style.boxShadow=`0 0 0 3px ${error?T.danger:T.blue}18`;}}
        onBlur={e=>{e.target.style.borderColor=error?T.danger:T.border;e.target.style.boxShadow='none';onBlur?.(e);}}/>
    </div>
    {error ? <p style={{ fontSize:11,color:T.danger,marginTop:4,fontWeight:600 }}>{error}</p>
     : hint && <p style={{ fontSize:11,color:T.textMuted,marginTop:4 }}>{hint}</p>}
  </div>
);

// Shared <datalist> of Indian bank names — render once, reference via Input list="bank-names".
export const BankNamesDatalist: React.FC<{ names: string[] }> = ({ names }) => (
  <datalist id="bank-names">{names.map(n => <option key={n} value={n} />)}</datalist>
);

// ─── Sel ─────────────────────────────────────────────────────────────────────
export const Sel: React.FC<{
  label?: string; value: string; onChange: (e: React.ChangeEvent<HTMLSelectElement>) => void;
  options: Array<{value:string;label:string}>; required?: boolean; style?: CSSProperties;
}> = ({ label, value, onChange, options, required, style={} }) => (
  <div style={{ marginBottom:16,...style }}>
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

// ─── PhoneField ──────────────────────────────────────────────────────────────
// Country-code selector + national number, in one place. Same layout and rules the platform
// already uses for phone entry (Sel of dial codes + digits-only field); previously inlined per
// page, so this is the shared version rather than a fourth copy.
//   • digits only — letters, spaces and punctuation are stripped as you type
//   • max 10 digits (national part; the dial code is separate)
export const PHONE_MAX = 10;
export const digitsOnly = (v: string, max = PHONE_MAX) => (v || '').replace(/\D/g, '').slice(0, max);

export const PhoneField: React.FC<{
  label?: string;
  code: string;
  onCode: (v: string) => void;
  value: string;
  onValue: (v: string) => void;
  codeOptions: Array<{ value: string; label: string }>;
  required?: boolean;
  hint?: string;
  style?: CSSProperties;
}> = ({ label = 'Mobile Number', code, onCode, value, onValue, codeOptions, required, hint, style = {} }) => (
  <div style={{ display: 'grid', gridTemplateColumns: '118px 1fr', gap: 8, ...style }}>
    <Sel label="Code" value={code} onChange={(e) => onCode(e.target.value)} options={codeOptions} style={{ marginBottom: 0 }} />
    <Input
      label={label}
      value={value}
      onChange={(e) => onValue(digitsOnly(e.target.value))}
      required={required}
      inputMode="numeric"
      placeholder="10 digits"
      hint={hint ?? (value && value.length !== PHONE_MAX ? `${value.length}/10 digits` : undefined)}
      style={{ marginBottom: 0 }}
    />
  </div>
);

// ─── SearchSelect ────────────────────────────────────────────────────────────
// Type-to-filter dropdown for long option lists (countries, states). The platform's only
// existing typeahead is the native <datalist> (see BankNamesDatalist), which cannot cap how many
// rows are visible — so this renders the same idea explicitly: type to filter, ~5 rows visible,
// the rest reachable by scrolling. Falls back to accepting free text, like the datalist does.
export const SEARCH_VISIBLE_ROWS = 5;

export const SearchSelect: React.FC<{
  label?: string;
  value: string;
  onChange: (v: string) => void;
  options: Array<{ value: string; label: string }>;
  placeholder?: string;
  required?: boolean;
  hint?: string;
  style?: CSSProperties;
}> = ({ label, value, onChange, options, placeholder = 'Type to search…', required, hint, style = {} }) => {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, []);

  // While open the field shows what you typed; closed, it shows the chosen value.
  const shown = open ? q : value;
  const needle = (open ? q : '').trim().toLowerCase();
  const list = needle ? options.filter((o) => o.label.toLowerCase().includes(needle)) : options;
  const ROW = 34;

  return (
    <div style={{ marginBottom: 16, ...style }} ref={box}>
      {label && (
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          {label}{required && <span style={{ color: T.danger }}> *</span>}
        </label>
      )}
      <div style={{ position: 'relative' }}>
        <input
          value={shown}
          placeholder={placeholder}
          onFocus={() => { setOpen(true); setQ(''); }}
          onChange={(e) => { setQ(e.target.value); setOpen(true); onChange(e.target.value); }}
          style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${open ? T.blue : T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', transition: 'border-color 0.2s' }}
        />
        {open && (
          <div style={{ position: 'absolute', zIndex: 30, top: '100%', left: 0, right: 0, marginTop: 4, background: T.surface, border: `1.5px solid ${T.border}`, borderRadius: 10, boxShadow: '0 12px 32px rgba(0,0,0,0.14)', maxHeight: ROW * SEARCH_VISIBLE_ROWS, overflowY: 'auto' }}>
            {list.length === 0 && (
              <div style={{ padding: '9px 14px', fontSize: 12.5, color: T.textMuted }}>No matches</div>
            )}
            {list.map((o) => (
              <div
                key={o.value}
                onMouseDown={(e) => { e.preventDefault(); onChange(o.value); setOpen(false); setQ(''); }}
                style={{ padding: '8px 14px', fontSize: 13.5, cursor: 'pointer', height: ROW, boxSizing: 'border-box', color: o.value === value ? T.blue : T.textMain, fontWeight: o.value === value ? 700 : 500, background: o.value === value ? `${T.blue}0e` : 'transparent' }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = T.canvas; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = o.value === value ? `${T.blue}0e` : 'transparent'; }}
              >
                {o.label}
              </div>
            ))}
          </div>
        )}
      </div>
      {hint && <p style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>{hint}</p>}
    </div>
  );
};

// ─── Modal ───────────────────────────────────────────────────────────────────
export const Modal: React.FC<{ title:string; children:React.ReactNode; onClose:()=>void; wide?:boolean; xl?:boolean; xxl?:boolean; icon?:string }> = ({ title, children, onClose, wide, xl, xxl, icon }) => (
  <div className="c5-overlay" style={{ position:'fixed',inset:0,background:'rgba(10,37,64,0.6)',zIndex:500,display:'flex',alignItems:'center',justifyContent:'center',padding:xxl?'24px 2.5vw':16,backdropFilter:'blur(4px)' }}>
    <div className="c5-pop" style={{ background:T.surface,borderRadius:20,width:'100%',maxWidth:xxl?1760:xl?1040:wide?740:520,maxHeight:'90vh',overflowY:'auto',boxShadow:'0 24px 80px rgba(0,0,0,0.25)' }}>
      <div style={{ padding:'20px 24px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center',position:'sticky',top:0,background:T.surface,zIndex:1 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800,color:T.textMain,display:'flex',alignItems:'center',gap:9 }}>{icon && isIconName(icon) && <Icon name={icon} size={19} color={T.blue} />}{title}</h2>
        <button onClick={onClose} aria-label="Close" style={{ background:'none',border:'none',cursor:'pointer',color:T.textMuted,borderRadius:8,width:32,height:32,display:'flex',alignItems:'center',justifyContent:'center' }}><Icon name="close" size={20} /></button>
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
  message?: string; maxLength?: number; closeLabel?: string; requiredHint?: string; placeholder?: string;
  onSubmit: (reason: string) => void; onClose: () => void;
}> = ({ title, label = 'Reason', confirmLabel = 'Confirm', busy, message, maxLength,
       closeLabel = 'Cancel', requiredHint, placeholder = 'Enter a reason...', onSubmit, onClose }) => {
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);
  const empty = !reason.trim();
  return (
    <Modal title={title} onClose={onClose}>
      {message && <p style={{ margin:'0 0 14px',fontSize:13,color:T.textMuted }}>{message}</p>}
      <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}<span style={{ color:T.danger }}> *</span></label>
      <textarea value={reason} maxLength={maxLength} onChange={e=>setReason(e.target.value)} onBlur={()=>setTouched(true)} placeholder={placeholder} autoFocus
        style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${touched && empty ? T.danger : T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:80,marginBottom:(maxLength||requiredHint)?4:14 }}/>
      {(maxLength || requiredHint) && (
        <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',gap:8,marginBottom:14,fontSize:11,minHeight:16 }}>
          <span style={{ color:T.danger,fontWeight:600 }}>{touched && empty && requiredHint ? requiredHint : ''}</span>
          {maxLength && <span style={{ color:T.textMuted,flexShrink:0 }}>{reason.length} / {maxLength}</span>}
        </div>
      )}
      <div style={{ display:'flex',gap:10 }}>
        <Btn onClick={()=>{ if (empty) { setTouched(true); return; } onSubmit(reason.trim()); }} disabled={busy||empty}>{busy?'Saving...':confirmLabel}</Btn>
        <Btn variant="secondary" onClick={onClose}>{closeLabel}</Btn>
      </div>
    </Modal>
  );
};
