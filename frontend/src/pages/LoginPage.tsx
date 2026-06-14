import React, { useState } from 'react';
import { T } from '../utils/theme';
import { Logo, Btn, Input } from '../components/UI';
import { useAuth } from '../context/AuthContext';
import { useToast } from '../context/ToastContext';

const LoginPage: React.FC = () => {
  const { login, isLoading } = useAuth();
  const { showToast } = useToast();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [forgot, setForgot] = useState(false);
  const [forgotEmail, setForgotEmail] = useState('');
  const [forgotSent, setForgotSent] = useState(false);
  const [show, setShow] = useState(false);

  const handleLogin = async () => {
    setError('');
    try {
      await login(username, password);
    } catch {
      setError('Invalid credentials. Check demo credentials below.');
    }
  };

  const DEMOS = [
    ['superadmin', 'Super Admin', '👑'],
    ['admin1', 'Admin', '🛡'],
    ['merchant1', 'Merchant', '🏪'],
  ];

  return (
    <div style={{ minHeight:'100vh',display:'flex',background:T.dark,fontFamily:'inherit',position:'relative',overflow:'hidden' }}>
      <style>{`
        *{margin:0;padding:0;box-sizing:border-box;}
        body{font-family:'Inter','Segoe UI',system-ui,sans-serif;}
        @keyframes pulse{from{opacity:0.6;transform:scale(1);}to{opacity:1;transform:scale(1.05);}}
        .login-left{display:flex;}
        .login-logo-mobile{display:none;}
      `}</style>

      {/* Animated BG */}
      <div style={{ position:'absolute',inset:0,overflow:'hidden',pointerEvents:'none' }}>
        {[{top:'8%',left:'6%',w:500,h:500,c:'rgba(0,82,204,0.12)'},{top:'55%',right:'4%',w:600,h:600,c:'rgba(38,208,12,0.08)'},{top:'35%',left:'40%',w:400,h:400,c:'rgba(0,163,255,0.07)'}].map((orb,i)=>(
          <div key={i} style={{ position:'absolute',top:orb.top,left:orb.left,right:(orb as {right?:string}).right,width:orb.w,height:orb.h,borderRadius:'50%',background:`radial-gradient(circle,${orb.c} 0%,transparent 70%)`,animation:`pulse ${3+i}s ease-in-out infinite alternate` }}/>
        ))}
        <div style={{ position:'absolute',inset:0,backgroundImage:`radial-gradient(rgba(0,82,204,0.06) 1px,transparent 1px)`,backgroundSize:'40px 40px' }}/>
      </div>

      {/* Left Panel */}
      <div className="login-left" style={{ flex:1,display:'flex',flexDirection:'column',justifyContent:'center',alignItems:'center',padding:48,position:'relative' }}>
        <div style={{ maxWidth:480,width:'100%' }}>
          <div style={{ marginBottom:48 }}><Logo size="lg"/></div>
          <h2 style={{ color:'#fff',fontSize:28,fontWeight:800,margin:'0 0 12px',lineHeight:1.3 }}>Enterprise Payment<br/>Infrastructure</h2>
          <p style={{ color:'rgba(255,255,255,0.55)',fontSize:15,lineHeight:1.7,marginBottom:40 }}>A unified platform for merchants, admins, and platform teams to manage payments with full audit trails and real-time risk intelligence.</p>
          {[{icon:'🛡',t:'Bank-grade security',d:'End-to-end encrypted transactions'},{icon:'⚡',t:'Real-time processing',d:'Instant settlement and approvals'},{icon:'📊',t:'Full audit trail',d:'Every action logged and traceable'},{icon:'🤖',t:'AI-Powered Assistant',d:'Claude AI for smart payment insights'}].map(f=>(
            <div key={f.t} style={{ display:'flex',gap:14,alignItems:'flex-start',marginBottom:20 }}>
              <div style={{ width:40,height:40,borderRadius:12,background:'rgba(0,82,204,0.3)',display:'flex',alignItems:'center',justifyContent:'center',fontSize:18,flexShrink:0 }}>{f.icon}</div>
              <div><p style={{ color:'#fff',fontWeight:700,margin:0,fontSize:14 }}>{f.t}</p><p style={{ color:'rgba(255,255,255,0.45)',fontSize:13,margin:0 }}>{f.d}</p></div>
            </div>
          ))}
        </div>
      </div>

      {/* Right Panel */}
      <div style={{ width:'100%',maxWidth:480,background:'rgba(255,255,255,0.97)',backdropFilter:'blur(20px)',display:'flex',flexDirection:'column',justifyContent:'center',padding:'40px 40px',boxShadow:'-20px 0 80px rgba(0,0,0,0.3)',position:'relative',zIndex:1 }} className="login-right">
        {!forgot ? (
          <>
            <div style={{ marginBottom:32 }}>
              <div style={{ display:'flex',justifyContent:'center',marginBottom:20 }} className="login-logo-mobile"><Logo size="sm"/></div>
              <h2 style={{ fontSize:22,fontWeight:800,color:T.textMain,margin:'0 0 6px' }}>Welcome back</h2>
              <p style={{ color:T.textMuted,fontSize:13,margin:0 }}>Sign in to your clari5pay account</p>
            </div>

            {error && <div style={{ background:T.dangerBg,border:`1px solid ${T.danger}30`,borderRadius:10,padding:'10px 14px',marginBottom:16,fontSize:12,color:T.danger,fontWeight:600 }}>⚠ {error}</div>}

            <Input label="Username" value={username} onChange={e=>setUsername(e.target.value)} placeholder="Your username" icon="👤" required/>
            <div style={{ position:'relative' }}>
              <Input label="Password" type={show?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder="Your password" icon="🔒" required/>
              <span onClick={()=>setShow(!show)} style={{ position:'absolute',right:12,bottom:22,cursor:'pointer',fontSize:16,color:T.textMuted }}>{show?'🙈':'👁'}</span>
            </div>

            <div style={{ display:'flex',justifyContent:'flex-end',marginBottom:20,marginTop:-8 }}>
              <span onClick={()=>setForgot(true)} style={{ fontSize:12,color:T.blue,cursor:'pointer',fontWeight:700 }}>Forgot password?</span>
            </div>

            <Btn size="lg" full onClick={handleLogin} disabled={isLoading||!username||!password}>
              {isLoading?'Authenticating...':'Sign In →'}
            </Btn>

            <div style={{ marginTop:28,padding:16,background:T.canvas,borderRadius:12,border:`1px solid ${T.border}` }}>
              <p style={{ fontWeight:800,color:T.textMain,marginBottom:10,fontSize:12,textTransform:'uppercase',letterSpacing:'0.05em' }}>Demo Accounts (password: pass123)</p>
              {DEMOS.map(([u,l,ic])=>(
                <div key={u} onClick={()=>{setUsername(u);setPassword('pass123');}}
                  style={{ cursor:'pointer',padding:'7px 10px',borderRadius:8,display:'flex',alignItems:'center',gap:8,marginBottom:4,transition:'background 0.15s' }}
                  onMouseEnter={e=>(e.currentTarget as HTMLDivElement).style.background=T.infoBg}
                  onMouseLeave={e=>(e.currentTarget as HTMLDivElement).style.background='transparent'}>
                  <span>{ic}</span>
                  <code style={{ color:T.blue,fontWeight:700,fontSize:12 }}>{u}</code>
                  <span style={{ color:T.textMuted,fontSize:12,marginLeft:'auto' }}>{l}</span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <>
            <div style={{ textAlign:'center',marginBottom:28 }}>
              <div style={{ fontSize:40,marginBottom:12 }}>🔐</div>
              <h2 style={{ fontSize:20,fontWeight:800,color:T.textMain,margin:'0 0 6px' }}>Reset Password</h2>
              <p style={{ color:T.textMuted,fontSize:13,margin:0 }}>Enter your email for a one-time password</p>
            </div>
            {!forgotSent ? (
              <>
                <Input label="Email Address" type="email" value={forgotEmail} onChange={e=>setForgotEmail(e.target.value)} placeholder="you@company.com" icon="✉" required/>
                <Btn size="lg" full onClick={()=>setForgotSent(true)} disabled={!forgotEmail}>Send OTP</Btn>
              </>
            ) : (
              <div style={{ textAlign:'center',padding:20 }}>
                <div style={{ width:56,height:56,borderRadius:'50%',background:T.successBg,display:'flex',alignItems:'center',justifyContent:'center',fontSize:24,margin:'0 auto 12px' }}>✓</div>
                <p style={{ color:T.success,fontWeight:700 }}>OTP sent to {forgotEmail}</p>
                <p style={{ fontSize:12,color:T.textMuted }}>Valid for 5 minutes</p>
              </div>
            )}
            <div style={{ textAlign:'center',marginTop:20 }}>
              <span onClick={()=>{setForgot(false);setForgotSent(false);}} style={{ fontSize:13,color:T.blue,cursor:'pointer',fontWeight:700 }}>← Back to Sign In</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
};

export default LoginPage;
