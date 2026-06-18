import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { Logo, Btn, Input } from '../components/UI';
import { useAuth } from '../context/AuthContext';
import { useToast } from '../context/ToastContext';
import { authAPI } from '../services/api';
import { passwordPolicyError, PASSWORD_POLICY_TEXT } from '../utils/helpers';
import type { OtpChallenge } from '../types';

const LoginPage: React.FC = () => {
  const { login, verifyOtp, resendOtp, isLoading } = useAuth();
  const { showToast } = useToast();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  // Forgot-password flow: email → otp → newpw → done.
  const [forgot, setForgot] = useState(false);
  const [forgotStep, setForgotStep] = useState<'username' | 'otp' | 'newpw' | 'done'>('username');
  const [forgotUsername, setForgotUsername] = useState('');
  const [resetToken, setResetToken] = useState('');
  const [confirmedToken, setConfirmedToken] = useState('');
  const [resetCode, setResetCode] = useState('');
  const [resetMasked, setResetMasked] = useState('');
  const [resetDevOtp, setResetDevOtp] = useState<string | undefined>(undefined);
  const [newPw, setNewPw] = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [forgotBusy, setForgotBusy] = useState(false);
  const [show, setShow] = useState(false);
  // OTP step
  const [otp, setOtp] = useState<OtpChallenge | null>(null);
  const [code, setCode] = useState('');
  const [verifying, setVerifying] = useState(false);
  // OTP on/off toggle (testing aid)
  const [otpEnabled, setOtpEnabled] = useState(true);
  // Resend OTP becomes available only after a 60-second cooldown.
  const [resendIn, setResendIn] = useState(0);
  const armResend = () => setResendIn(60);

  useEffect(() => { authAPI.otpStatus().then(s => setOtpEnabled(s.enabled)).catch(()=>{}); }, []);

  useEffect(() => {
    if (resendIn <= 0) return;
    const id = setInterval(() => setResendIn(s => (s <= 1 ? 0 : s - 1)), 1000);
    return () => clearInterval(id);
  }, [resendIn]);

  const toggleOtp = async () => {
    const next = !otpEnabled;
    setOtpEnabled(next); // optimistic
    try {
      await authAPI.setOtpEnabled(next);
      showToast(`Login OTP ${next ? 'enabled' : 'disabled'}`);
    } catch {
      setOtpEnabled(!next);
      showToast('Could not change OTP setting', 'error');
    }
  };

  const handleLogin = async () => {
    setError('');
    try {
      const challenge = await login(username, password);
      if (challenge.otpRequired) {
        setOtp(challenge);
        setCode('');
        armResend();
      }
      // otpRequired === false → session already established; App redirects to dashboard.
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Login failed. Please try again.');
    }
  };

  const handleVerify = async () => {
    if (!otp) return;
    setError('');
    setVerifying(true);
    try {
      await verifyOtp(otp.otpToken, code.trim());
      // success → AuthContext sets the user; App redirects to the dashboard.
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Invalid OTP. Please try again.');
    } finally {
      setVerifying(false);
    }
  };

  const handleResend = async () => {
    if (!otp || resendIn > 0) return;
    setError('');
    try {
      const challenge = await resendOtp(otp.otpToken);
      setOtp(challenge);
      setCode('');
      armResend();
      showToast('A new OTP has been sent');
    } catch {
      showToast('Could not resend OTP', 'error');
    }
  };

  const backToLogin = () => { setOtp(null); setCode(''); setError(''); setPassword(''); };

  const openForgot = () => {
    setForgot(true); setForgotStep('username'); setError('');
    setForgotUsername(''); setResetToken(''); setConfirmedToken(''); setResetCode('');
    setResetMasked(''); setResetDevOtp(undefined); setNewPw(''); setConfirmPw('');
  };
  const closeForgot = () => { setForgot(false); setError(''); };

  const sendResetOtp = async () => {
    setError(''); setForgotBusy(true);
    try {
      const r = await authAPI.forgotPassword(forgotUsername.trim());
      setResetToken(r.resetToken);
      setResetMasked(r.email);
      setResetDevOtp(r.devOtp);
      setResetCode('');
      setForgotStep('otp');
      armResend();
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not send the reset code. Please try again.');
    } finally { setForgotBusy(false); }
  };

  const verifyResetCode = async () => {
    setError(''); setForgotBusy(true);
    try {
      const r = await authAPI.verifyResetOtp(resetToken, resetCode.trim());
      setConfirmedToken(r.confirmedToken);
      setNewPw(''); setConfirmPw('');
      setForgotStep('newpw');
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Invalid OTP. Please try again.');
    } finally { setForgotBusy(false); }
  };

  const resendResetOtp = async () => {
    if (resendIn > 0) return;
    setError('');
    try {
      const r = await authAPI.forgotPassword(forgotUsername.trim());
      setResetToken(r.resetToken);
      setResetDevOtp(r.devOtp);
      setResetCode('');
      armResend();
      showToast('A new OTP has been sent');
    } catch { showToast('Could not resend OTP', 'error'); }
  };

  const submitNewPassword = async () => {
    setError('');
    if (newPw !== confirmPw) { setError('Passwords do not match.'); return; }
    const policy = passwordPolicyError(newPw);
    if (policy) { setError(policy); return; }
    setForgotBusy(true);
    try {
      await authAPI.resetPassword(confirmedToken, newPw);
      setForgotStep('done');
      showToast('Password updated — please sign in');
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not update the password. Please try again.');
    } finally { setForgotBusy(false); }
  };

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
        {otp ? (
          <>
            <div style={{ marginBottom:28 }}>
              <div style={{ display:'flex',justifyContent:'center',marginBottom:20 }} className="login-logo-mobile"><Logo size="sm"/></div>
              <h2 style={{ fontSize:22,fontWeight:800,color:T.textMain,margin:'0 0 6px' }}>Verify it's you</h2>
              <p style={{ color:T.textMuted,fontSize:13,margin:0 }}>Enter the 6-digit code sent to <b style={{ color:T.textMain }}>{otp.email}</b></p>
            </div>

            {error && <div style={{ background:T.dangerBg,border:`1px solid ${T.danger}30`,borderRadius:10,padding:'10px 14px',marginBottom:16,fontSize:12,color:T.danger,fontWeight:600 }}>⚠ {error}</div>}

            {otp.devOtp && (
              <div style={{ background:T.infoBg,border:`1px solid ${T.blue}30`,borderRadius:10,padding:'10px 14px',marginBottom:16,fontSize:12,color:T.blue }}>
                🧪 Dev mode (no email server): your code is <b style={{ letterSpacing:'0.1em' }}>{otp.devOtp}</b>
              </div>
            )}

            <Input label="One-Time Password" value={code}
              onChange={e=>setCode(e.target.value.replace(/[^\d]/g,'').slice(0,6))}
              placeholder="6-digit code" icon="🔑" required/>

            <Btn size="lg" full onClick={handleVerify} disabled={verifying||code.length<6}>
              {verifying?'Verifying...':'Verify & Sign In →'}
            </Btn>

            <div style={{ display:'flex',justifyContent:'space-between',marginTop:18 }}>
              <span onClick={backToLogin} style={{ fontSize:13,color:T.textMuted,cursor:'pointer',fontWeight:700 }}>← Back</span>
              <span onClick={handleResend} style={{ fontSize:13,color:resendIn>0?T.textLight:T.blue,cursor:resendIn>0?'default':'pointer',fontWeight:700 }}>{resendIn>0?`Resend in ${resendIn}s`:'Resend OTP'}</span>
            </div>
            <p style={{ fontSize:11,color:T.textMuted,marginTop:16,textAlign:'center' }}>The code expires in 15 minutes.</p>
          </>
        ) : !forgot ? (
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
              <span onClick={openForgot} style={{ fontSize:12,color:T.blue,cursor:'pointer',fontWeight:700 }}>Forgot password?</span>
            </div>

            <Btn size="lg" full onClick={handleLogin} disabled={isLoading||!username||!password}>
              {isLoading?'Authenticating...':'Sign In →'}
            </Btn>

            {/* OTP on/off toggle (testing aid) */}
            <div style={{ marginTop:18,display:'flex',alignItems:'center',justifyContent:'space-between',padding:'10px 14px',background:T.canvas,borderRadius:10,border:`1px solid ${T.border}` }}>
              <div>
                <p style={{ margin:0,fontSize:12,fontWeight:700,color:T.textMain }}>Login OTP</p>
                <p style={{ margin:0,fontSize:11,color:T.textMuted }}>{otpEnabled ? 'On — verify with email code' : 'Off — password only'}</p>
              </div>
              <div onClick={toggleOtp} role="switch" aria-checked={otpEnabled}
                style={{ width:46,height:26,borderRadius:13,background:otpEnabled?T.success:T.border,position:'relative',cursor:'pointer',transition:'background 0.2s',flexShrink:0 }}>
                <div style={{ position:'absolute',top:3,left:otpEnabled?23:3,width:20,height:20,borderRadius:'50%',background:'#fff',boxShadow:'0 1px 3px rgba(0,0,0,0.3)',transition:'left 0.2s' }}/>
              </div>
            </div>

          </>
        ) : (
          <>
            <div style={{ textAlign:'center',marginBottom:24 }}>
              <div style={{ fontSize:40,marginBottom:12 }}>🔐</div>
              <h2 style={{ fontSize:20,fontWeight:800,color:T.textMain,margin:'0 0 6px' }}>Reset Password</h2>
              <p style={{ color:T.textMuted,fontSize:13,margin:0 }}>
                {forgotStep==='username' && 'Enter your username to receive a verification code'}
                {forgotStep==='otp' && <>Enter the 6-digit code sent to <b style={{ color:T.textMain }}>{resetMasked}</b> for <b style={{ color:T.textMain }}>{forgotUsername}</b></>}
                {forgotStep==='newpw' && <>Choose a new password for <b style={{ color:T.textMain }}>{forgotUsername}</b></>}
                {forgotStep==='done' && <>Password updated for <b style={{ color:T.textMain }}>{forgotUsername}</b></>}
              </p>
            </div>

            {error && <div style={{ background:T.dangerBg,border:`1px solid ${T.danger}30`,borderRadius:10,padding:'10px 14px',marginBottom:16,fontSize:12,color:T.danger,fontWeight:600 }}>⚠ {error}</div>}

            {forgotStep==='username' && (
              <>
                <Input label="Username" value={forgotUsername} onChange={e=>setForgotUsername(e.target.value)} placeholder="Your username" icon="👤" required/>
                <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 14px' }}>A one-time code will be sent to the email registered for this account.</p>
                <Btn size="lg" full onClick={sendResetOtp} disabled={forgotBusy||!forgotUsername}>{forgotBusy?'Sending...':'Send OTP'}</Btn>
              </>
            )}

            {forgotStep==='otp' && (
              <>
                {resetDevOtp && (
                  <div style={{ background:T.infoBg,border:`1px solid ${T.blue}30`,borderRadius:10,padding:'10px 14px',marginBottom:16,fontSize:12,color:T.blue }}>
                    🧪 Dev mode: your code is <b style={{ letterSpacing:'0.1em' }}>{resetDevOtp}</b>
                  </div>
                )}
                <Input label="One-Time Password" value={resetCode}
                  onChange={e=>setResetCode(e.target.value.replace(/[^\d]/g,'').slice(0,6))}
                  placeholder="6-digit code" icon="🔑" required/>
                <Btn size="lg" full onClick={verifyResetCode} disabled={forgotBusy||resetCode.length<6}>{forgotBusy?'Verifying...':'Verify Code →'}</Btn>
                <div style={{ display:'flex',justifyContent:'flex-end',marginTop:16 }}>
                  <span onClick={resendResetOtp} style={{ fontSize:13,color:resendIn>0?T.textLight:T.blue,cursor:resendIn>0?'default':'pointer',fontWeight:700 }}>{resendIn>0?`Resend in ${resendIn}s`:'Resend OTP'}</span>
                </div>
                <p style={{ fontSize:11,color:T.textMuted,marginTop:14,textAlign:'center' }}>The code expires in 15 minutes.</p>
              </>
            )}

            {forgotStep==='newpw' && (
              <>
                <div style={{ position:'relative' }}>
                  <Input label="New Password" type={show?'text':'password'} value={newPw} onChange={e=>setNewPw(e.target.value)} placeholder="Enter new password" icon="🔒" required/>
                  <span onClick={()=>setShow(!show)} style={{ position:'absolute',right:12,bottom:22,cursor:'pointer',fontSize:16,color:T.textMuted }}>{show?'🙈':'👁'}</span>
                </div>
                <Input label="Confirm Password" type={show?'text':'password'} value={confirmPw} onChange={e=>setConfirmPw(e.target.value)} placeholder="Re-enter new password" icon="🔒" required/>
                {confirmPw && newPw !== confirmPw && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
                <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 14px' }}>{PASSWORD_POLICY_TEXT}</p>
                <Btn size="lg" full onClick={submitNewPassword} disabled={forgotBusy||!newPw||!confirmPw}>{forgotBusy?'Updating...':'Update Password'}</Btn>
              </>
            )}

            {forgotStep==='done' && (
              <div style={{ textAlign:'center',padding:'8px 0 4px' }}>
                <div style={{ width:56,height:56,borderRadius:'50%',background:T.successBg,display:'flex',alignItems:'center',justifyContent:'center',fontSize:24,margin:'0 auto 12px' }}>✓</div>
                <p style={{ color:T.success,fontWeight:700,margin:'0 0 4px' }}>Password updated successfully</p>
                <p style={{ fontSize:12,color:T.textMuted,margin:0 }}>Sign in with your new password.</p>
                <Btn size="lg" full style={{ marginTop:18 }} onClick={closeForgot}>← Back to Sign In</Btn>
              </div>
            )}

            {forgotStep!=='done' && (
              <div style={{ textAlign:'center',marginTop:20 }}>
                <span onClick={closeForgot} style={{ fontSize:13,color:T.blue,cursor:'pointer',fontWeight:700 }}>← Back to Sign In</span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default LoginPage;
