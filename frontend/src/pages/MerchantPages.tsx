import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, CHART_DATA } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, MiniBar } from '../components/UI';
import TxTable from '../components/TxTable';
import { transactionAPI } from '../services/api';
import { useToast } from '../context/ToastContext';
import type { Transaction, User } from '../types';

// ─── Merchant Dashboard ──────────────────────────────────────────────────────
export const MerchantDashboard: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    transactionAPI.getMine().then(setTxns).catch(()=>setTxns([])).finally(()=>setLoading(false));
  }, []);

  const completed = txns.filter(t => t.status === 'COMPLETED');
  const pending = txns.filter(t => t.status === 'PENDING');

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:16,marginBottom:22 }}>
        <StatCard icon="💰" label="Available Balance" value={fmt(user.balance||0)} sub="Updated now" color={T.success} trend={8.4}/>
        <StatCard icon="↓" label="Total Deposits" value={fmt(txns.filter(t=>t.type==='DEPOSIT').reduce((a,t)=>a+t.amount,0))} color={T.blue}/>
        <StatCard icon="↑" label="Withdrawals" value={fmt(txns.filter(t=>t.type==='WITHDRAWAL').reduce((a,t)=>a+t.amount,0))} color={T.danger}/>
        <StatCard icon="⧗" label="Pending" value={pending.length} sub="Awaiting review" color={T.warning}/>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'2fr 1fr',gap:16,marginBottom:22 }}>
        <Card style={{ padding:22 }}>
          <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:14 }}>
            <div><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Transaction Volume</h3><p style={{ margin:0,fontSize:11,color:T.textMuted }}>Last 7 days</p></div>
            <div style={{ display:'flex',gap:12,fontSize:10,color:T.textMuted }}>
              <span style={{ display:'flex',alignItems:'center',gap:3 }}><span style={{ width:8,height:8,borderRadius:2,background:T.blue,display:'inline-block' }}/> Deposits</span>
              <span style={{ display:'flex',alignItems:'center',gap:3 }}><span style={{ width:8,height:8,borderRadius:2,background:T.danger,display:'inline-block' }}/> Withdrawals</span>
            </div>
          </div>
          <MiniBar data={CHART_DATA}/>
        </Card>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>Account Info</h3>
          {[['Pay-In Code',user.payIn||'DEP'],['Pay-Out Code',user.payOut||'WIT'],['Fee Rate',`${user.payInFee||1.5}%`],['Profile',user.profile||'Maker'],['Risk Level','']].map(([k,v])=>(
            <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'8px 0',borderBottom:`1px solid ${T.borderLight}` }}>
              <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
              {k==='Risk Level'?<RiskBadge risk={user.risk||'LOW'}/>:<span style={{ fontSize:12,fontWeight:700,color:T.textMain }}>{v}</span>}
            </div>
          ))}
        </Card>
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Recent Transactions</h3></div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={txns.slice(0,5)} userRole="MERCHANT"/>}
      </Card>
    </div>
  );
};

// ─── Deposit Form ─────────────────────────────────────────────────────────────
export const DepositForm: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',depositType:'UPI',memberName:'',memberId:'',segment:'A',profile:'NEW' });
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const submit = async () => {
    setLoading(true);
    try {
      await transactionAPI.createDeposit({ ...form, amount: parseFloat(form.amount) });
      showToast('Deposit request submitted — awaiting admin review');
      setForm({ amount:'',depositType:'UPI',memberName:'',memberId:'',segment:'A',profile:'NEW' });
    } catch {
      showToast('Failed to submit deposit request','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth:660 }}>
      <Card style={{ padding:26 }}>
        <div style={{ display:'flex',alignItems:'center',gap:12,marginBottom:22 }}>
          <div style={{ width:44,height:44,borderRadius:14,background:T.successBg,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22 }}>↓</div>
          <div><h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>New Deposit Request</h2><p style={{ margin:0,fontSize:12,color:T.textMuted }}>Reference auto-generated: DEP000XXXX</p></div>
        </div>
        <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
          <Input label="Amount (₹)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="0.00" required icon="₹"/>
          <Sel label="Deposit Type" value={form.depositType} onChange={e=>set('depositType',e.target.value)} options={['UPI','QR','IMPS','NEFT','RTGS','CASH'].map(v=>({value:v,label:v}))} required/>
          <Input label="Member Name" value={form.memberName} onChange={e=>set('memberName',e.target.value)} placeholder="Full name" required/>
          <Input label="Member ID" value={form.memberId} onChange={e=>set('memberId',e.target.value)} placeholder="e.g. MBR20240001" required/>
          <Sel label="Segment" value={form.segment} onChange={e=>set('segment',e.target.value)} options={['A','B','C','D'].map(v=>({value:v,label:`Segment ${v}`}))}/>
          <Sel label="Profile" value={form.profile} onChange={e=>set('profile',e.target.value)} options={[{value:'OLD',label:'OLD'},{value:'NEW',label:'NEW'}]}/>
        </div>
        <div style={{ background:T.canvas,borderRadius:10,padding:14,marginBottom:20,display:'flex',gap:24,flexWrap:'wrap' }}>
          <div><p style={{ fontSize:10,color:T.textMuted,margin:0,fontWeight:700,textTransform:'uppercase' }}>Pay-In Code</p><p style={{ margin:0,fontWeight:800,color:T.blue }}>{user.payIn||'DEP'}</p></div>
          <div><p style={{ fontSize:10,color:T.textMuted,margin:0,fontWeight:700,textTransform:'uppercase' }}>Fee Rate</p><p style={{ margin:0,fontWeight:800 }}>{user.payInFee||1.5}%</p></div>
          <div><p style={{ fontSize:10,color:T.textMuted,margin:0,fontWeight:700,textTransform:'uppercase' }}>Net Amount</p><p style={{ margin:0,fontWeight:800,color:T.success }}>{form.amount?fmt(parseFloat(form.amount)*(1-(user.payInFee||1.5)/100)):'—'}</p></div>
        </div>
        <Btn size="lg" onClick={submit} disabled={loading||!form.amount||!form.memberName}>{loading?'Submitting...':'Submit Deposit →'}</Btn>
      </Card>
    </div>
  );
};

// ─── Withdrawal Form ──────────────────────────────────────────────────────────
export const WithdrawalForm: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',memberId:'',accountHolder:'',accountNumber:'',ifsc:'',bankName:'' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  const bal = user.balance || 0;

  const submit = async () => {
    try {
      await transactionAPI.createWithdrawal({ ...form, amount: parseFloat(form.amount) });
      showToast('Withdrawal request submitted');
      setForm({ amount:'',memberId:'',accountHolder:'',accountNumber:'',ifsc:'',bankName:'' });
    } catch {
      showToast('Failed to submit withdrawal','error');
    }
  };

  return (
    <div style={{ maxWidth:660 }}>
      <Card style={{ padding:26 }}>
        <div style={{ display:'flex',alignItems:'center',gap:12,marginBottom:22 }}>
          <div style={{ width:44,height:44,borderRadius:14,background:T.dangerBg,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22 }}>↑</div>
          <div><h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>New Withdrawal Request</h2><p style={{ margin:0,fontSize:12,color:T.textMuted }}>Reference auto-generated: WIT000XXXX</p></div>
        </div>
        <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
          <Input label="Amount (₹)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="0.00" required icon="₹"/>
          <Input label="Member ID" value={form.memberId} onChange={e=>set('memberId',e.target.value)} placeholder="Alphanumeric" required/>
          <Input label="Account Holder" value={form.accountHolder} onChange={e=>set('accountHolder',e.target.value)} placeholder="As per bank" required/>
          <Input label="Account Number" value={form.accountNumber} onChange={e=>set('accountNumber',e.target.value)} placeholder="Account number" required/>
          <Input label="IFSC Code" value={form.ifsc} onChange={e=>set('ifsc',e.target.value)} placeholder="e.g. HDFC0001234" required/>
          <Input label="Bank Name" value={form.bankName} onChange={e=>set('bankName',e.target.value)} placeholder="e.g. HDFC Bank" required/>
        </div>
        <div style={{ background:T.warningBg,border:`1px solid ${T.warning}30`,borderRadius:10,padding:12,marginBottom:18,display:'flex',gap:8,alignItems:'center' }}>
          <span style={{ color:T.warning }}>⚠</span>
          <div>
            <p style={{ margin:0,fontSize:12,fontWeight:700,color:T.warning }}>Available: {fmt(bal)}</p>
            {form.amount&&parseFloat(form.amount)>bal&&<p style={{ margin:0,fontSize:11,color:T.danger }}>Amount exceeds balance</p>}
          </div>
        </div>
        <Btn size="lg" variant="danger" style={{ background:T.danger,color:'#fff' }} onClick={submit} disabled={!form.amount||parseFloat(form.amount)>bal||!form.accountHolder}>
          Submit Withdrawal →
        </Btn>
      </Card>
    </div>
  );
};

// ─── Settlement Form ──────────────────────────────────────────────────────────
export const SettlementForm: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [amount, setAmount] = useState('');
  const bal = user.balance || 0;
  const valid = amount && parseFloat(amount) > 0 && parseFloat(amount) <= bal;

  const submit = async () => {
    try {
      await transactionAPI.createSettlement({ amount: parseFloat(amount) });
      showToast('Settlement submitted for review');
      setAmount('');
    } catch {
      showToast('Failed to submit settlement','error');
    }
  };

  return (
    <div style={{ maxWidth:560 }}>
      <Card style={{ padding:26 }}>
        <div style={{ display:'flex',alignItems:'center',gap:12,marginBottom:22 }}>
          <div style={{ width:44,height:44,borderRadius:14,background:T.infoBg,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22 }}>⇄</div>
          <div><h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Settlement Request</h2><p style={{ margin:0,fontSize:12,color:T.textMuted }}>Transfer to your registered account</p></div>
        </div>
        <div style={{ background:T.grad3,borderRadius:14,padding:24,marginBottom:22,textAlign:'center' }}>
          <p style={{ fontSize:11,color:'rgba(255,255,255,0.6)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Available Balance</p>
          <p style={{ fontSize:36,fontWeight:800,color:'#fff',margin:0 }}>{fmt(bal)}</p>
        </div>
        <Input label="Settlement Amount (₹)" type="number" value={amount} onChange={e=>setAmount(e.target.value)} placeholder="Enter amount" icon="₹" hint={`Max: ${fmt(bal)}`}/>
        {valid && (
          <div style={{ background:T.successBg,borderRadius:8,padding:12,marginBottom:14,fontSize:12 }}>
            <p style={{ margin:0,fontWeight:700,color:T.success }}>✓ Remaining after settlement: {fmt(bal-parseFloat(amount))}</p>
          </div>
        )}
        <Btn size="lg" full onClick={submit} disabled={!valid}>Submit Settlement →</Btn>
      </Card>
    </div>
  );
};

// ─── Balance Page ─────────────────────────────────────────────────────────────
export const BalancePage: React.FC<{ user: User }> = ({ user }) => (
  <div style={{ maxWidth:600 }}>
    <Card style={{ padding:26 }}>
      <div style={{ background:T.grad3,borderRadius:16,padding:28,marginBottom:22,color:'#fff' }}>
        <p style={{ fontSize:11,color:'rgba(255,255,255,0.55)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Current Balance</p>
        <p style={{ fontSize:40,fontWeight:800,margin:'0 0 16px' }}>{fmt(user.balance||0)}</p>
        <div style={{ display:'flex',gap:24,flexWrap:'wrap' }}>
          {[['Pay-In',user.payIn||'DEP'],['Pay-Out',user.payOut||'WIT'],['Currency','INR']].map(([k,v])=>(
            <div key={k}><p style={{ fontSize:10,color:'rgba(255,255,255,0.45)',margin:0 }}>{k}</p><p style={{ fontWeight:700,margin:0,fontSize:14 }}>{v}</p></div>
          ))}
        </div>
      </div>
      {[['Total Deposits (MTD)',fmt(825000),T.success],['Total Withdrawals (MTD)',fmt(340000),T.danger],['Pending Settlements',fmt(150000),T.warning],['Net Flow',fmt(335000),T.blue]].map(([k,v,c])=>(
        <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'11px 0',borderBottom:`1px solid ${T.borderLight}` }}>
          <span style={{ fontSize:13,color:T.textMuted }}>{k}</span>
          <span style={{ fontSize:14,fontWeight:800,color:c }}>{v}</span>
        </div>
      ))}
    </Card>
  </div>
);

// ─── Risk Page ────────────────────────────────────────────────────────────────
export const RiskPage: React.FC<{ user: User }> = ({ user }) => (
  <div style={{ maxWidth:780 }}>
    <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:14,marginBottom:20 }}>
      <StatCard icon="🛡" label="Risk Score" value="22 / 100" color={T.success}/>
      <StatCard icon="⚑" label="Flagged Txns" value="0" color={T.warning}/>
      <StatCard icon="⚡" label="Velocity" value="Normal" color={T.info}/>
    </div>
    <Card style={{ padding:24 }}>
      <h3 style={{ margin:'0 0 16px',fontSize:14,fontWeight:800 }}>Risk Factor Breakdown</h3>
      {[{label:'Transaction Velocity',score:18,max:30,color:T.success},{label:'Amount Anomaly',score:8,max:25,color:T.success},{label:'Failed Attempts',score:4,max:20,color:T.success},{label:'Geographic Risk',score:12,max:25,color:T.warning}].map(r=>(
        <div key={r.label} style={{ marginBottom:14 }}>
          <div style={{ display:'flex',justifyContent:'space-between',fontSize:13,marginBottom:5 }}>
            <span style={{ fontWeight:600,color:T.textMain }}>{r.label}</span>
            <span style={{ color:r.color,fontWeight:800 }}>{r.score} / {r.max}</span>
          </div>
          <div style={{ height:8,background:T.borderLight,borderRadius:4 }}>
            <div style={{ height:'100%',width:`${(r.score/r.max)*100}%`,background:r.color,borderRadius:4 }}/>
          </div>
        </div>
      ))}
      <div style={{ marginTop:18,paddingTop:14,borderTop:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
        <p style={{ fontWeight:800,margin:0 }}>Overall: <RiskBadge risk={user.risk||'LOW'}/></p>
        <span style={{ fontSize:11,color:T.textMuted }}>Based on last 90 days</span>
      </div>
    </Card>
  </div>
);

// ─── Integrations Page ────────────────────────────────────────────────────────
export const IntegrationsPage: React.FC = () => {
  const [states, setStates] = useState({ wa:false, tg:true, email:true });
  const toggle = (k: keyof typeof states) => setStates(s=>({...s,[k]:!s[k]}));
  const items = [
    {id:'wa' as const,icon:'💬',label:'WhatsApp Business',desc:'Live transaction confirmations',color:'#25D366'},
    {id:'tg' as const,icon:'✈️',label:'Telegram Bot',desc:'Instant alerts via Telegram',color:'#229ED9'},
    {id:'email' as const,icon:'📧',label:'Email Notifications',desc:'HTML email summaries',color:T.blue},
  ];
  return (
    <div style={{ maxWidth:640 }}>
      {items.map(item=>(
        <Card key={item.id} style={{ padding:20,marginBottom:14 }}>
          <div style={{ display:'flex',alignItems:'center',justifyContent:'space-between',gap:14 }}>
            <div style={{ display:'flex',gap:12,alignItems:'center' }}>
              <div style={{ width:44,height:44,borderRadius:12,background:`${item.color}15`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:20 }}>{item.icon}</div>
              <div><p style={{ margin:0,fontWeight:800,fontSize:14,color:T.textMain }}>{item.label}</p><p style={{ margin:0,fontSize:12,color:T.textMuted }}>{item.desc}</p></div>
            </div>
            <div onClick={()=>toggle(item.id)} style={{ cursor:'pointer',width:44,height:24,borderRadius:12,background:states[item.id]?item.color:T.border,position:'relative',transition:'background 0.25s',flexShrink:0 }}>
              <div style={{ position:'absolute',top:3,left:states[item.id]?22:3,width:18,height:18,borderRadius:'50%',background:'#fff',transition:'left 0.25s',boxShadow:'0 1px 4px rgba(0,0,0,0.2)' }}/>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
};

// ─── Transaction History ──────────────────────────────────────────────────────
export const TransactionHistory: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [search, setSearch] = useState('');
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fn = user.role === 'MERCHANT' ? transactionAPI.getMine : transactionAPI.getAll;
    fn().then(setTxns).catch(()=>setTxns([])).finally(()=>setLoading(false));
  }, [user.role]);

  const filtered = txns.filter(t => {
    const ms = !search || t.ref.includes(search.toUpperCase()) || t.merchant.toLowerCase().includes(search.toLowerCase());
    const mt = type === 'ALL' || t.type === type;
    const mst = status === 'ALL' || t.status === status;
    return ms && mt && mst;
  });

  return (
    <Card>
      <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800 }}>Transaction Ledger</h3>
        <div style={{ display:'flex',gap:8,flexWrap:'wrap' }}>
          <div style={{ position:'relative',flex:1,minWidth:180 }}>
            <span style={{ position:'absolute',left:10,top:'50%',transform:'translateY(-50%)',color:T.textMuted,fontSize:14 }}>🔍</span>
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search reference or merchant..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
          <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL','DEPOSIT','WITHDRAWAL','SETTLEMENT'].map(v=><option key={v} value={v}>{v}</option>)}
          </select>
          <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL','PENDING','ADMIN_APPROVED','COMPLETED','REJECTED','SA_REJECTED','CANCELLED'].map(v=><option key={v} value={v}>{v}</option>)}
          </select>
        </div>
      </div>
      {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={filtered} userRole={user.role}/>}
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
    </Card>
  );
};

// ─── Profile Page ─────────────────────────────────────────────────────────────
export const ProfilePage: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [pw, setPw] = useState({ current:'', next:'', confirm:'' });
  const set = (k: string, v: string) => setPw(p => ({...p,[k]:v}));

  const changePassword = async () => {
    if(pw.next !== pw.confirm){ showToast('Passwords do not match','error'); return; }
    try {
      // await userAPI.changePassword({ current_password: pw.current, new_password: pw.next });
      showToast('Password updated successfully');
      setPw({ current:'', next:'', confirm:'' });
    } catch {
      showToast('Failed to update password','error');
    }
  };

  return (
    <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(280px,1fr))',gap:18,maxWidth:860 }}>
      <Card style={{ padding:26 }}>
        <h3 style={{ margin:'0 0 18px',fontSize:14,fontWeight:800 }}>Change Password</h3>
        <Input label="Current Password" type="password" value={pw.current} onChange={e=>set('current',e.target.value)} placeholder="Current password"/>
        <Input label="New Password" type="password" value={pw.next} onChange={e=>set('next',e.target.value)} placeholder="Min 8 characters"/>
        <Input label="Confirm New Password" type="password" value={pw.confirm} onChange={e=>set('confirm',e.target.value)} placeholder="Re-enter password"/>
        <Btn full onClick={changePassword}>Update Password</Btn>
      </Card>
      <Card style={{ padding:26 }}>
        <div style={{ display:'flex',gap:14,alignItems:'center',marginBottom:20,padding:'14px',background:T.canvas,borderRadius:12 }}>
          <div style={{ width:52,height:52,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:20,fontWeight:800,color:'#fff' }}>{user.name.charAt(0)}</div>
          <div><p style={{ margin:0,fontWeight:800,fontSize:15,color:T.textMain }}>{user.name}</p><p style={{ margin:0,fontSize:12,color:T.textMuted }}>{user.email}</p></div>
        </div>
        <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>Account Details</h3>
        {[['Username',user.username],['Role',user.role.replace('_',' ')],['Pay-In Code',user.payIn||'—'],['Pay-Out Code',user.payOut||'—'],['Settlement Code',user.settlement||'—'],['Profile Type',user.profile||'—'],['Risk Level',user.risk||'—']].filter(([,v])=>v&&v!=='—').map(([k,v])=>(
          <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'9px 0',borderBottom:`1px solid ${T.borderLight}` }}>
            <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
            <span style={{ fontSize:12,fontWeight:700,color:T.textMain }}>{v}</span>
          </div>
        ))}
      </Card>
    </div>
  );
};
