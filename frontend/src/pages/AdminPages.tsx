import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, CHART_DATA } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, Badge, MiniBar, Modal } from '../components/UI';
import TxTable from '../components/TxTable';
import { transactionAPI, userAPI } from '../services/api';
import { useToast } from '../context/ToastContext';
import type { Transaction, User } from '../types';

// ─── Admin Dashboard ──────────────────────────────────────────────────────────
export const AdminDashboard: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [merchants, setMerchants] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([transactionAPI.getAll(), userAPI.getMerchants()])
      .then(([t,m]) => { setTxns(t); setMerchants(m); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const pending = txns.filter(t => t.status === 'PENDING');

  const handleAction = async (t: Transaction, action: string) => {
    try {
      if(action === 'approve') await transactionAPI.approve(t.id);
      else await transactionAPI.reject(t.id);
      const updated = await transactionAPI.getAll();
      setTxns(updated);
      showToast(`${t.ref} ${action === 'approve' ? 'approved — sent to Super Admin' : 'rejected'}`);
    } catch {
      showToast('Action failed','error');
    }
  };

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:20 }}>
        <StatCard icon="🏪" label="My Merchants" value={merchants.length} color={T.blue}/>
        <StatCard icon="⧗" label="Pending Approvals" value={pending.length} color={T.warning}/>
        <StatCard icon="✓" label="Approved Today" value="7" color={T.success}/>
        <StatCard icon="💹" label="Platform Volume" value={fmt(2450000)} color={T.info}/>
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Pending Approvals ({pending.length})</h3>
          <Badge status="PENDING"/>
        </div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={txns} onAction={handleAction} userRole="ADMIN"/>}
      </Card>
    </div>
  );
};

// ─── Admin Merchants Page ─────────────────────────────────────────────────────
export const AdminMerchantsPage: React.FC = () => {
  const { showToast } = useToast();
  const [merchants, setMerchants] = useState<User[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name:'',username:'',email:'',password:'',payIn:'',payOut:'',settlement:'',payInFee:'1.5',payOutFee:'1.2',profile:'Maker' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { userAPI.getMerchants().then(setMerchants).catch(()=>{}); }, []);

  const createMerchant = async () => {
    if(!form.name||!form.username||!form.email||!form.password||!form.payIn||!form.payOut||!form.settlement){ showToast('Fill all required fields','error'); return; }
    try {
      await userAPI.createMerchant({...form, payInFee:parseFloat(form.payInFee), payOutFee:parseFloat(form.payOutFee), role:'MERCHANT'});
      const updated = await userAPI.getMerchants();
      setMerchants(updated);
      setShowCreate(false);
      setForm({ name:'',username:'',email:'',password:'',payIn:'',payOut:'',settlement:'',payInFee:'1.5',payOutFee:'1.2',profile:'Maker' });
      showToast(`Merchant "${form.name}" created`);
    } catch {
      showToast('Failed to create merchant','error');
    }
  };

  const toggleMerchant = async (m: User) => {
    try {
      await userAPI.toggleStatus(m.id);
      const updated = await userAPI.getMerchants();
      setMerchants(updated);
      showToast(`${m.name} ${m.active ? 'deactivated' : 'activated'}`);
    } catch {
      showToast('Failed to update status','error');
    }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Merchant Management</h2>
        <Btn onClick={()=>setShowCreate(true)}>+ Create Merchant</Btn>
      </div>
      {showCreate && (
        <Modal title="Create Merchant Account" onClose={()=>setShowCreate(false)} wide>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            <Input label="Business Name" value={form.name} onChange={e=>set('name',e.target.value)} placeholder="e.g. Nexus Fintech Ltd." required/>
            <Input label="Username" value={form.username} onChange={e=>set('username',e.target.value)} placeholder="Login username" required hint="Used to login"/>
            <Input label="Email" type="email" value={form.email} onChange={e=>set('email',e.target.value)} placeholder="business@domain.com" required/>
            <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} placeholder="Set login password" required hint="Merchant logs in with this"/>
            <Input label="Pay-In Code" value={form.payIn} onChange={e=>set('payIn',e.target.value)} placeholder="Max 3 chars e.g. DEP" required hint="Max 3 characters"/>
            <Input label="Pay-Out Code" value={form.payOut} onChange={e=>set('payOut',e.target.value)} placeholder="e.g. WIT" required/>
            <Input label="Settlement Code" value={form.settlement} onChange={e=>set('settlement',e.target.value)} placeholder="e.g. SET" required/>
            <Sel label="Profile Type" value={form.profile} onChange={e=>set('profile',e.target.value)} options={['Admin','User','Maker','Checker'].map(v=>({value:v,label:v}))}/>
            <Input label="Pay-In Fee (%)" type="number" value={form.payInFee} onChange={e=>set('payInFee',e.target.value)} required/>
            <Input label="Pay-Out Fee (%)" type="number" value={form.payOutFee} onChange={e=>set('payOutFee',e.target.value)} required/>
          </div>
          <div style={{ background:T.infoBg,border:`1px solid ${T.blue}20`,borderRadius:10,padding:12,marginBottom:16,fontSize:12,color:T.blue }}>
            ℹ Merchant will login with: <strong>{form.username||'[username]'}</strong> / <strong>{form.password||'[password]'}</strong>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={createMerchant}>Create Account</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <Card>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Merchant','Username','Email','Pay-In','Balance','Risk','Status','Action'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merchants.map((m,i)=>(
                <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px' }}><div style={{ display:'flex',gap:8,alignItems:'center' }}><div style={{ width:28,height:28,borderRadius:8,background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontWeight:700,fontSize:12,color:'#fff' }}>{m.name.charAt(0)}</div><span style={{ fontWeight:700 }}>{m.name}</span></div></td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11 }}>{m.username}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{m.email}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.infoBg,color:T.blue,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{m.payIn}</code></td>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{fmt(m.balance||0)}</td>
                  <td style={{ padding:'11px 14px' }}><RiskBadge risk={m.risk||'LOW'}/></td>
                  <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:m.active?T.successBg:T.dangerBg,color:m.active?T.success:T.danger }}>{m.active?'Active':'Inactive'}</span></td>
                  <td style={{ padding:'11px 14px' }}><Btn size="sm" variant="ghost" onClick={()=>toggleMerchant(m)}>Toggle</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};

// ─── SA Dashboard ─────────────────────────────────────────────────────────────
export const SaDashboard: React.FC = () => {
  const { showToast } = useToast();
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [merchants, setMerchants] = useState<User[]>([]);
  const [admins, setAdmins] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([transactionAPI.getAll(), userAPI.getMerchants(), userAPI.getAdmins()])
      .then(([t,m,a]) => { setTxns(t); setMerchants(m); setAdmins(a); })
      .catch(()=>{})
      .finally(()=>setLoading(false));
  }, []);

  const queue = txns.filter(t => t.status === 'ADMIN_APPROVED');

  const handleAction = async (t: Transaction, action: string) => {
    try {
      if(action === 'complete') await transactionAPI.complete(t.id);
      else await transactionAPI.saReject(t.id);
      const updated = await transactionAPI.getAll();
      setTxns(updated);
      showToast(`${t.ref} ${action === 'complete' ? 'completed' : 'rejected'}`);
    } catch {
      showToast('Action failed','error');
    }
  };

  const riskCounts = merchants.reduce((acc,m) => { const r=m.risk||'LOW'; acc[r]=(acc[r]||0)+1; return acc; },{} as Record<string,number>);

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:20 }}>
        <StatCard icon="👑" label="Platform Admins" value={admins.length} color={T.blue}/>
        <StatCard icon="🏪" label="Total Merchants" value={merchants.length} color={T.success} trend={16.7}/>
        <StatCard icon="⧗" label="Awaiting Sign-off" value={queue.length} color={T.warning}/>
        <StatCard icon="💹" label="Monthly Volume" value={fmt(12450000)} color={T.info} trend={23.4}/>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'2fr 1fr',gap:16,marginBottom:20 }}>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800 }}>Platform Volume</h3>
          <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>All merchants — last 7 days</p>
          <MiniBar data={CHART_DATA.map(d=>({...d,deposit:d.deposit*3.2,withdrawal:d.withdrawal*2.8}))}/>
        </Card>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 14px',fontSize:14,fontWeight:800 }}>Risk Distribution</h3>
          {[['Low Risk',T.success,'LOW'],['Medium Risk',T.warning,'MEDIUM'],['High Risk',T.danger,'HIGH']].map(([label,color,key])=>(
            <div key={key} style={{ display:'flex',justifyContent:'space-between',alignItems:'center',padding:'9px 0',borderBottom:`1px solid ${T.borderLight}` }}>
              <div style={{ display:'flex',gap:8,alignItems:'center' }}><div style={{ width:10,height:10,borderRadius:'50%',background:color }}/><span style={{ fontSize:12,color:T.textMain }}>{label}</span></div>
              <span style={{ fontWeight:800,color }}>{riskCounts[key]||0}</span>
            </div>
          ))}
        </Card>
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Admin-Approved Queue — Final Sign-Off</h3>
          <Badge status="ADMIN_APPROVED"/>
        </div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={queue} onAction={handleAction} userRole="SUPER_ADMIN"/>}
      </Card>
    </div>
  );
};

// ─── SA Admins Page ───────────────────────────────────────────────────────────
export const SaAdminsPage: React.FC = () => {
  const { showToast } = useToast();
  const [admins, setAdmins] = useState<User[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name:'',username:'',email:'',password:'' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { userAPI.getAdmins().then(setAdmins).catch(()=>{}); }, []);

  const create = async () => {
    if(!form.name||!form.username||!form.email||!form.password){ showToast('Fill all fields','error'); return; }
    try {
      await userAPI.createAdmin({...form, role:'ADMIN'});
      const updated = await userAPI.getAdmins();
      setAdmins(updated);
      setShowCreate(false);
      setForm({ name:'',username:'',email:'',password:'' });
      showToast(`Admin "${form.name}" created. Login: ${form.username} / ${form.password}`);
    } catch {
      showToast('Failed to create admin','error');
    }
  };

  const toggleAdmin = async (a: User) => {
    try {
      await userAPI.toggleStatus(a.id);
      const updated = await userAPI.getAdmins();
      setAdmins(updated);
      showToast(`${a.name} ${a.active ? 'suspended' : 'activated'}`);
    } catch {
      showToast('Failed to update status','error');
    }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Admin Management</h2>
        <Btn onClick={()=>setShowCreate(true)}>+ Create Admin</Btn>
      </div>
      {showCreate && (
        <Modal title="Create Admin Account" onClose={()=>setShowCreate(false)}>
          <Input label="Full Name" value={form.name} onChange={e=>set('name',e.target.value)} required placeholder="Admin's full name"/>
          <Input label="Username" value={form.username} onChange={e=>set('username',e.target.value)} required placeholder="Login username"/>
          <Input label="Email" type="email" value={form.email} onChange={e=>set('email',e.target.value)} required placeholder="admin@company.com"/>
          <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} required placeholder="Set login password" hint="Admin will use this to login"/>
          <div style={{ background:T.infoBg,border:`1px solid ${T.blue}20`,borderRadius:10,padding:12,marginBottom:16,fontSize:12,color:T.blue }}>
            ℹ Admin will login with: <strong>{form.username||'[username]'}</strong> / <strong>{form.password||'[password]'}</strong>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create}>Create Admin</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(280px,1fr))',gap:14 }}>
        {admins.map(a=>(
          <Card key={a.id} style={{ padding:20 }}>
            <div style={{ display:'flex',gap:12,alignItems:'center',marginBottom:14 }}>
              <div style={{ width:44,height:44,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:18,fontWeight:800,color:'#fff' }}>{a.name.charAt(0)}</div>
              <div style={{ flex:1,overflow:'hidden' }}>
                <p style={{ margin:0,fontWeight:800,fontSize:14,color:T.textMain }}>{a.name}</p>
                <p style={{ margin:0,fontSize:11,color:T.textMuted }}>{a.email}</p>
                <code style={{ fontSize:10,color:T.blue,background:T.infoBg,padding:'1px 5px',borderRadius:4 }}>{a.username}</code>
              </div>
              <span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:a.active?T.successBg:T.dangerBg,color:a.active?T.success:T.danger,flexShrink:0 }}>{a.active?'Active':'Inactive'}</span>
            </div>
            <div style={{ display:'flex',gap:8 }}>
              <Btn size="sm" variant={a.active?'danger':'success'} style={{ flex:1,justifyContent:'center' }} onClick={()=>toggleAdmin(a)}>
                {a.active?'Suspend':'Activate'}
              </Btn>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
};

// ─── SA Merchants Page ────────────────────────────────────────────────────────
export const SaMerchantsPage: React.FC = () => {
  const { showToast } = useToast();
  const [merchants, setMerchants] = useState<User[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name:'',username:'',email:'',password:'',payIn:'',payOut:'',settlement:'',payInFee:'1.5',payOutFee:'1.2',profile:'Maker',risk:'LOW' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { userAPI.getMerchants().then(setMerchants).catch(()=>{}); }, []);

  const create = async () => {
    if(!form.name||!form.username||!form.email||!form.password||!form.payIn||!form.payOut||!form.settlement){ showToast('Fill all required fields','error'); return; }
    try {
      await userAPI.createMerchant({...form, role:'MERCHANT', payInFee:parseFloat(form.payInFee), payOutFee:parseFloat(form.payOutFee)});
      const updated = await userAPI.getMerchants();
      setMerchants(updated);
      setShowCreate(false);
      setForm({ name:'',username:'',email:'',password:'',payIn:'',payOut:'',settlement:'',payInFee:'1.5',payOutFee:'1.2',profile:'Maker',risk:'LOW' });
      showToast(`Merchant "${form.name}" created. Login: ${form.username} / ${form.password}`);
    } catch {
      showToast('Failed to create merchant','error');
    }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Merchant Management</h2>
        <Btn onClick={()=>setShowCreate(true)}>+ Create Merchant</Btn>
      </div>
      {showCreate && (
        <Modal title="Create Merchant Account" onClose={()=>setShowCreate(false)} wide>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            <Input label="Business Name" value={form.name} onChange={e=>set('name',e.target.value)} placeholder="e.g. Nexus Fintech Ltd." required/>
            <Input label="Username" value={form.username} onChange={e=>set('username',e.target.value)} placeholder="Login username" required hint="Merchant uses this to login"/>
            <Input label="Email" type="email" value={form.email} onChange={e=>set('email',e.target.value)} placeholder="biz@company.com" required/>
            <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} placeholder="Set login password" required hint="Merchant login password"/>
            <Input label="Pay-In Code" value={form.payIn} onChange={e=>set('payIn',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. DEP (max 3 chars)" required/>
            <Input label="Pay-Out Code" value={form.payOut} onChange={e=>set('payOut',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. WIT" required/>
            <Input label="Settlement Code" value={form.settlement} onChange={e=>set('settlement',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. SET" required/>
            <Sel label="Profile Type" value={form.profile} onChange={e=>set('profile',e.target.value)} options={['Admin','User','Maker','Checker'].map(v=>({value:v,label:v}))}/>
            <Input label="Pay-In Fee (%)" type="number" value={form.payInFee} onChange={e=>set('payInFee',e.target.value)} required/>
            <Input label="Pay-Out Fee (%)" type="number" value={form.payOutFee} onChange={e=>set('payOutFee',e.target.value)} required/>
            <Sel label="Risk Level" value={form.risk} onChange={e=>set('risk',e.target.value)} options={['LOW','MEDIUM','HIGH'].map(v=>({value:v,label:v}))}/>
          </div>
          <div style={{ background:T.infoBg,border:`1px solid ${T.blue}20`,borderRadius:10,padding:12,marginBottom:16,fontSize:12,color:T.blue }}>
            ℹ Merchant will login with: <strong>{form.username||'[username]'}</strong> / <strong>{form.password||'[password]'}</strong>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create}>Create Merchant</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <Card>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Business','Username','Email','Codes','Fees','Balance','Risk','Status','Action'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merchants.map((m,i)=>(
                <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',fontWeight:800 }}>{m.name}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11 }}>{m.username}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{m.email}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',gap:3,flexWrap:'wrap' }}>
                      {[m.payIn,m.payOut,m.settlement].filter(Boolean).map(c=><code key={c} style={{ background:T.infoBg,color:T.blue,padding:'1px 5px',borderRadius:4,fontSize:10,fontWeight:700 }}>{c}</code>)}
                    </div>
                  </td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{m.payInFee}% / {m.payOutFee}%</td>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{fmt(m.balance||0)}</td>
                  <td style={{ padding:'11px 14px' }}><RiskBadge risk={m.risk||'LOW'}/></td>
                  <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:m.active?T.successBg:T.dangerBg,color:m.active?T.success:T.danger }}>{m.active?'Active':'Inactive'}</span></td>
                  <td style={{ padding:'11px 14px' }}><Btn size="sm" variant={m.active?'danger':'success'} onClick={async()=>{ try{ await userAPI.toggleStatus(m.id); const u=await userAPI.getMerchants(); setMerchants(u); showToast(`${m.name} ${m.active?'deactivated':'activated'}`); }catch{} }}>Toggle</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};

// ─── SA Risk Page ─────────────────────────────────────────────────────────────
export const SaRiskPage: React.FC = () => {
  const [merchants, setMerchants] = useState<User[]>([]);
  useEffect(() => { userAPI.getMerchants().then(setMerchants).catch(()=>{}); }, []);

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:20 }}>
        {[['🔴','High Risk',`${merchants.filter(m=>m.risk==='HIGH').length} merchants`,T.danger],['🟡','Medium Risk',`${merchants.filter(m=>m.risk==='MEDIUM').length} merchants`,T.warning],['🟢','Low Risk',`${merchants.filter(m=>(m.risk||'LOW')==='LOW').length} merchants`,T.success],['⚡','Alerts','2 today',T.info]].map(([ic,lbl,sub,c])=>(
          <StatCard key={lbl as string} icon={ic as string} label={lbl as string} value={sub as string} color={c as string}/>
        ))}
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Merchant Risk Intelligence</h3></div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Merchant','Risk','Score','Status'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merchants.map((m,i)=>{
                const score = m.risk==='HIGH'?80:m.risk==='MEDIUM'?45:20;
                const color = m.risk==='HIGH'?T.danger:m.risk==='MEDIUM'?T.warning:T.success;
                return (
                  <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px',fontWeight:700 }}>{m.name}</td>
                    <td style={{ padding:'11px 14px' }}><RiskBadge risk={m.risk||'LOW'}/></td>
                    <td style={{ padding:'11px 14px',width:200 }}>
                      <div style={{ display:'flex',alignItems:'center',gap:8 }}>
                        <div style={{ flex:1,height:6,background:T.borderLight,borderRadius:3 }}><div style={{ height:'100%',width:`${score}%`,background:color,borderRadius:3 }}/></div>
                        <span style={{ fontSize:11,color:T.textMuted,minWidth:28 }}>{score}</span>
                      </div>
                    </td>
                    <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:m.active?T.successBg:T.dangerBg,color:m.active?T.success:T.danger }}>{m.active?'Active':'Inactive'}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};
