import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, fileToDataUrl, COUNTRY_CODES, formatDateTime } from '../utils/helpers';
import { accountToPng } from '../utils/image';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, Badge, MiniBar, StatusChart, Modal } from '../components/UI';
import TxTable from '../components/TxTable';
import { usePoll } from '../utils/usePoll';
import { transactionAPI, userAPI, accountAPI, systemLogAPI } from '../services/api';
import type { SystemLogEntry } from '../types';
import { useToast } from '../context/ToastContext';
import type { Transaction, User, Account } from '../types';

const REQUEST_TYPES = ['DEPOSIT', 'WITHDRAWAL', 'SETTLEMENT', 'DEPOSIT_REQUEST', 'WITHDRAWAL_REQUEST', 'SETTLEMENT_REQUEST'];
const REQUEST_STATUSES = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SLIP_SUBMITTED', 'COMPLETED', 'CANCELLED'];

// Active (pending) workflow statuses — anything not yet completed/cancelled.
const ACTIVE_STATUSES = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SLIP_SUBMITTED', 'PENDING', 'ADMIN_APPROVED'];
const isActive = (s: string) => ACTIVE_STATUSES.includes(s);

const ROLE_LABELS: Record<string, string> = { DEO: 'Data Operator', SUPERVISOR: 'Supervisor', MANAGER: 'Manager' };
const roleLabel = (r?: string | null) => (r ? ROLE_LABELS[String(r).toUpperCase()] || r : '—');

const Row = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <div style={{ display:'flex',justifyContent:'space-between',padding:'8px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
    <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
    <span style={{ fontSize:12,fontWeight:700,color:T.textMain,textAlign:'right' }}>{v}</span>
  </div>
);

// ─── Request detail / action popup (type- and status-driven) ───────────────────
const RequestModal: React.FC<{
  tx: Transaction;
  canAct: boolean;
  onClose: () => void;
  onDone?: () => void;
}> = ({ tx, canAct, onClose, onDone }) => {
  const { showToast } = useToast();
  const isDeposit = tx.type.startsWith('DEPOSIT');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountRef, setAccountRef] = useState('');
  const [receipt, setReceipt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Admin steps:
  const chooseStep = canAct && isDeposit && tx.status === 'ACCOUNT_REQUESTED'; // pick account → auto PNG
  const depositDoneStep = canAct && isDeposit && tx.status === 'SLIP_SUBMITTED'; // review slip → Deposited
  const payStep = canAct && !isDeposit && tx.status === 'ACCOUNT_REQUESTED'; // pay merchant → upload receipt → Completed
  const title = chooseStep ? 'Choose Account' : depositDoneStep ? 'Review Payment Slip' : payStep ? 'Pay & Complete' : 'Request Details';

  useEffect(() => {
    if (chooseStep) accountAPI.list().then(a => setAccounts(a.filter(x => (x.status || '').toUpperCase() === 'ACTIVE'))).catch(()=>{});
  }, [chooseStep]);

  const onReceipt = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) setReceipt(await fileToDataUrl(f));
  };

  const sendAccount = async () => {
    const acc = accounts.find(a => a.referenceNumber === accountRef);
    if (!acc) { showToast('Select an account', 'error'); return; }
    setSaving(true);
    try {
      const png = accountToPng(acc);
      const summary = `${acc.accountName} | ${acc.bankName} | A/C ${acc.accountNumber} | IFSC ${acc.ifscCode}`;
      await transactionAPI.submitAccount(tx.id, { adminProof: png, adminBankDetails: summary, adminRef: acc.referenceNumber });
      showToast(`${tx.ref} — account details sent`);
      onDone?.(); onClose();
    } catch { showToast('Failed to send account', 'error'); }
    finally { setSaving(false); }
  };

  const complete = async (withReceipt: boolean) => {
    if (withReceipt && !receipt) { showToast('Upload the payment receipt', 'error'); return; }
    setSaving(true);
    try {
      await transactionAPI.markDone(tx.id, withReceipt ? { adminProof: receipt || undefined } : undefined);
      showToast(`${tx.ref} ${isDeposit ? 'deposited' : 'completed'}`);
      onDone?.(); onClose();
    } catch { showToast('Failed to complete', 'error'); }
    finally { setSaving(false); }
  };

  return (
    <Modal title={`${title} — ${tx.ref}`} onClose={onClose} wide>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 24px' }}>
        <div>
          <Row k="Merchant" v={tx.merchant} />
          <Row k="Type" v={typeLabel(tx.type)} />
          <Row k="Amount" v={fmt(tx.amount)} />
          <Row k="Status" v={<Badge status={tx.status} type={tx.type} viewerRole="ADMIN" />} />
          {tx.memberId && <Row k="Member ID" v={tx.memberId} />}
          {tx.member && <Row k="Member Name" v={tx.member} />}
          {tx.depositType && <Row k="Deposit Type" v={tx.depositType} />}
          <Row k="Date" v={`${tx.date} ${tx.time}`} />
        </div>
        <div>
          {isDeposit ? (
            <>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Account Sent</p>
              {(tx.adminBankDetails || tx.adminRef) ? (
                <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12 }}>
                  {tx.adminBankDetails && <Row k="Account" v={tx.adminBankDetails} />}
                  {tx.adminRef && <Row k="Reference" v={tx.adminRef} />}
                </div>
              ) : <div style={{ padding:24,textAlign:'center',color:T.textMuted,background:T.canvas,borderRadius:10,fontSize:12 }}>Not sent yet</div>}
              {tx.adminProof && <img src={tx.adminProof} alt="Account details" style={{ width:'100%',maxHeight:160,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
            </>
          ) : (
            <>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Merchant Bank Details (pay here)</p>
              <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12 }}>
                {tx.accountHolder && <Row k="Account Holder" v={tx.accountHolder} />}
                {tx.accountNumber && <Row k="Account Number" v={tx.accountNumber} />}
                {tx.ifsc && <Row k="IFSC" v={tx.ifsc} />}
                {tx.bank && <Row k="Bank" v={tx.bank} />}
                {!tx.accountHolder && !tx.accountNumber && !tx.bank && <p style={{ margin:0,color:T.textMuted }}>No bank details provided.</p>}
              </div>
              {tx.adminProof && <><p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'12px 0 8px' }}>Payment Receipt</p>
                <img src={tx.adminProof} alt="Receipt" style={{ width:'100%',maxHeight:160,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,background:T.canvas }} /></>}
            </>
          )}
        </div>
      </div>

      {/* Merchant payment slip (deposit) */}
      {isDeposit && (tx.merchantProof || tx.merchantRef) && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Merchant Payment Slip</p>
          {tx.merchantRef && <Row k="Reference Number" v={tx.merchantRef} />}
          {tx.merchantProof && <img src={tx.merchantProof} alt="Payment slip" style={{ width:'100%',maxHeight:220,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
        </div>
      )}

      {chooseStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Select an account to send ({accounts.length} active)</p>
          <Sel label="Account" value={accountRef} onChange={e=>setAccountRef(e.target.value)}
            options={[{ value:'', label:'— Select an account —' }, ...accounts.map(a => ({ value:a.referenceNumber, label:`${a.accountName} — ${a.bankName} (A/C ${a.accountNumber})` }))]} />
          <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 12px' }}>A PNG of the selected account is auto-generated and sent to the merchant.</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={sendAccount} disabled={saving||!accountRef}>{saving ? 'Sending...' : '🏦 Send Account'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
        </div>
      )}

      {depositDoneStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 12px' }}>Review the merchant's payment slip above, then mark this deposit complete.</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={()=>complete(false)} disabled={saving}>{saving ? 'Saving...' : '✓ Mark Deposited'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
        </div>
      )}

      {payStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Upload Payment Receipt</p>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 8px' }}>Pay the merchant using the bank details above, then upload your payment receipt to complete.</p>
          <input type="file" accept="image/*,.pdf" onChange={onReceipt} style={{ fontSize:12 }} />
          {receipt && <img src={receipt} alt="Receipt" style={{ width:'100%',maxHeight:180,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,margin:'12px 0',background:T.canvas }} />}
          <div style={{ display:'flex',gap:10,marginTop:12 }}>
            <Btn onClick={()=>complete(true)} disabled={saving||!receipt}>{saving ? 'Saving...' : '✓ Complete'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
        </div>
      )}
    </Modal>
  );
};

// ─── Admin Dashboard ──────────────────────────────────────────────────────────
export const AdminDashboard: React.FC<{ user: User }> = () => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [merchants, setMerchants] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [type, setType] = useState('ALL');
  const [active, setActive] = useState<Transaction | null>(null);

  const reload = () => Promise.all([transactionAPI.getAll(), userAPI.getMerchants()])
    .then(([t,m]) => { setTxns(t); setMerchants(m); })
    .catch(() => {});

  useEffect(() => { reload().finally(() => setLoading(false)); }, []);
  usePoll(() => { if (!active) reload(); });

  // Pending = every active (in-flight) request: Account Requested, Account Submitted,
  // Slip Submitted, Pending. Only completed/cancelled are excluded.
  const pending = txns.filter(t => isActive(t.status));
  const completed = txns.filter(t => t.status === 'COMPLETED');
  const filtered = pending.filter(t => type === 'ALL' || t.type === type);

  // Real-time status breakdown (counts straight from the DB records).
  const statusData = [
    { label: 'Account Requested', value: txns.filter(t => t.status === 'ACCOUNT_REQUESTED').length, color: T.warning },
    { label: 'Account Submitted', value: txns.filter(t => t.status === 'ACCOUNT_SUBMITTED').length, color: T.info },
    { label: 'Slip Submitted', value: txns.filter(t => t.status === 'SLIP_SUBMITTED').length, color: T.blue },
    { label: 'Completed', value: completed.length, color: T.success },
  ];

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:20 }}>
        <StatCard icon="🏪" label="My Merchants" value={merchants.length} color={T.blue}/>
        <StatCard icon="≡" label="Total Requests" value={txns.length} color={T.info}/>
        <StatCard icon="✓" label="Completed" value={completed.length} color={T.success}/>
        <StatCard icon="⧗" label="Pending" value={pending.length} color={T.warning}/>
      </div>
      <Card style={{ padding:22,marginBottom:20 }}>
        <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800 }}>Requests by Status</h3>
        <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>Live counts from current transactions</p>
        <StatusChart data={statusData}/>
      </Card>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center',gap:8,flexWrap:'wrap' }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Pending Requests ({pending.length})</h3>
          <div style={{ display:'flex',gap:8,flexWrap:'wrap' }}>
            <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'7px 10px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
              {['ALL',...REQUEST_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
            </select>
          </div>
        </div>
        {loading
          ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div>
          : <TxTable txns={filtered} actionMode="admin" viewerRole="ADMIN" onAction={(t)=>setActive(t)}/>}
      </Card>
      {active && <RequestModal tx={active} canAct onClose={()=>setActive(null)} onDone={reload}/>}
    </div>
  );
};

// ─── Admin All Transactions ─────────────────────────────────────────────────────
export const AdminTransactionsPage: React.FC = () => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [search, setSearch] = useState('');
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<Transaction | null>(null);

  const reload = () => transactionAPI.getAll().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(() => { if (!active) reload(); });

  const filtered = txns.filter(t => {
    const ms = !search || t.ref.toLowerCase().includes(search.toLowerCase()) || t.merchant.toLowerCase().includes(search.toLowerCase());
    return ms && (type === 'ALL' || t.type === type) && (status === 'ALL' || t.status === status);
  });

  return (
    <Card>
      <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800 }}>All Transactions</h3>
        <div style={{ display:'flex',gap:8,flexWrap:'wrap' }}>
          <div style={{ position:'relative',flex:1,minWidth:180 }}>
            <span style={{ position:'absolute',left:10,top:'50%',transform:'translateY(-50%)',color:T.textMuted,fontSize:14 }}>🔍</span>
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search reference or merchant..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
          <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...REQUEST_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
          </select>
          <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...REQUEST_STATUSES].map(v=><option key={v} value={v}>{v==='ALL'?'All Statuses':typeLabel(v)}</option>)}
          </select>
        </div>
      </div>
      {loading
        ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div>
        : <TxTable txns={filtered} actionMode="admin" viewerRole="ADMIN" onAction={(t)=>setActive(t)}/>}
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}` }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
      {active && <RequestModal tx={active} canAct onClose={()=>setActive(null)} onDone={reload}/>}
    </Card>
  );
};

// ─── Admin Merchants Page ─────────────────────────────────────────────────────
export const AdminMerchantsPage: React.FC = () => {
  const { showToast } = useToast();
  const [merchants, setMerchants] = useState<User[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const empty = { name:'',username:'',email:'',countryCode:'+91',phone:'',password:'',confirmPassword:'',payIn:'',payOut:'',settlement:'',payInFee:'1.5',payOutFee:'1.2',profile:'Maker',merchantRole:'DEO',risk:'LOW' };
  const [form, setForm] = useState(empty);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  const passwordMismatch = !!form.confirmPassword && form.password !== form.confirmPassword;

  const reload = () => userAPI.getMerchants().then(setMerchants).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!showCreate) reload(); });

  const createMerchant = async () => {
    if(!form.name||!form.username||!form.email||!form.phone||!form.password||!form.payIn||!form.payOut||!form.settlement){ showToast('Fill all required fields','error'); return; }
    if(form.password !== form.confirmPassword){ showToast('Passwords do not match','error'); return; }
    try {
      await userAPI.createMerchant({
        name:form.name, username:form.username, email:form.email,
        phone:`${form.countryCode} ${form.phone}`, password:form.password,
        payIn:form.payIn, payOut:form.payOut, settlement:form.settlement,
        payInFee:parseFloat(form.payInFee), payOutFee:parseFloat(form.payOutFee),
        profile:form.profile, merchantRole:form.merchantRole, risk:form.risk, role:'MERCHANT',
      });
      await reload();
      setShowCreate(false);
      setForm(empty);
      showToast(`Merchant "${form.name}" created`);
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
            <Input label="Email ID" type="email" value={form.email} onChange={e=>set('email',e.target.value)} placeholder="biz@company.com" required/>
            <div style={{ marginBottom:16 }}>
              <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Phone Number<span style={{ color:T.danger }}> *</span></label>
              <div style={{ display:'flex',gap:8 }}>
                <select value={form.countryCode} onChange={e=>set('countryCode',e.target.value)}
                  style={{ width:130,padding:'10px 8px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:13,outline:'none',fontFamily:'inherit',background:T.surface }}>
                  {COUNTRY_CODES.map(c=><option key={c.code} value={c.code}>{c.label}</option>)}
                </select>
                <input value={form.phone} onChange={e=>set('phone',e.target.value.replace(/[^\d]/g,''))} placeholder="Phone number"
                  style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,outline:'none',fontFamily:'inherit',boxSizing:'border-box' }}/>
              </div>
            </div>
            <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} placeholder="Set login password" required hint="Merchant login password"/>
            <div>
              <Input label="Confirm Password" type="password" value={form.confirmPassword} onChange={e=>set('confirmPassword',e.target.value)} placeholder="Re-enter password" required/>
              {passwordMismatch && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
            </div>
            <Sel label="Role Selection" value={form.merchantRole} onChange={e=>set('merchantRole',e.target.value)} required options={[{value:'DEO',label:'Data Entry Operator'},{value:'SUPERVISOR',label:'Supervisor'},{value:'MANAGER',label:'Manager'}]}/>
            <Sel label="Profile Type" value={form.profile} onChange={e=>set('profile',e.target.value)} options={['Admin','User','Maker','Checker'].map(v=>({value:v,label:v}))}/>
            <Input label="Pay-In Code" value={form.payIn} onChange={e=>set('payIn',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. DEP (max 3 chars)" required/>
            <Input label="Pay-Out Code" value={form.payOut} onChange={e=>set('payOut',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. WIT" required/>
            <Input label="Settlement Code" value={form.settlement} onChange={e=>set('settlement',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. SET" required/>
            <Input label="Pay-In Fee (%)" type="number" value={form.payInFee} onChange={e=>set('payInFee',e.target.value)} required/>
            <Input label="Pay-Out Fee (%)" type="number" value={form.payOutFee} onChange={e=>set('payOutFee',e.target.value)} required/>
            <Sel label="Risk Level" value={form.risk} onChange={e=>set('risk',e.target.value)} options={['LOW','MEDIUM','HIGH'].map(v=>({value:v,label:v}))}/>
          </div>
          <div style={{ background:T.infoBg,border:`1px solid ${T.blue}20`,borderRadius:10,padding:12,margin:'4px 0 16px',fontSize:12,color:T.blue }}>
            ℹ Integration settings are configured and managed by Admins — merchants do not have access.
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={createMerchant} disabled={passwordMismatch}>Create Account</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <Card>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Business','Username','Role','Email','Phone','Codes','Balance','Status','Action'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merchants.map((m,i)=>(
                <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',fontWeight:800 }}>{m.name}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11 }}>{m.username}</code></td>
                  <td style={{ padding:'11px 14px' }}><span style={{ background:T.infoBg,color:T.blue,padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,whiteSpace:'nowrap' }}>{roleLabel(m.merchantRole)}</span></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{m.email}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11,whiteSpace:'nowrap' }}>{m.phone||'—'}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',gap:3,flexWrap:'wrap' }}>
                      {[m.payIn,m.payOut,m.settlement].filter(Boolean).map(c=><code key={c} style={{ background:T.infoBg,color:T.blue,padding:'1px 5px',borderRadius:4,fontSize:10,fontWeight:700 }}>{c}</code>)}
                    </div>
                  </td>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{fmt(m.balance||0)}</td>
                  <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:m.active?T.successBg:T.dangerBg,color:m.active?T.success:T.danger }}>{m.active?'Active':'Inactive'}</span></td>
                  <td style={{ padding:'11px 14px' }}>
                    <Btn size="sm" variant={m.active?'danger':'success'} onClick={async()=>{ try{ await userAPI.toggleStatus(m.id); await reload(); showToast(`${m.name} ${m.active?'deactivated':'activated'}`); }catch{} }}>{m.active?'Deactivate':'Activate'}</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
};

// ─── Admin Account Management ───────────────────────────────────────────────────
export const AdminAccountsPage: React.FC = () => {
  const { showToast } = useToast();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [search, setSearch] = useState('');
  const [detail, setDetail] = useState<Account | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const empty = { account_name:'',account_number:'',ifsc_code:'',bank_name:'',branch:'',account_type:'Savings Account',status:'ACTIVE' };
  const [form, setForm] = useState(empty);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const reload = () => accountAPI.list().then(setAccounts).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!detail && !showCreate) reload(); });

  const toggleStatus = async (a: Account) => {
    try { await accountAPI.toggle(a.referenceNumber); await reload(); showToast(`${a.referenceNumber} set ${a.status==='ACTIVE'?'INACTIVE':'ACTIVE'}`); }
    catch { showToast('Failed to update account','error'); }
  };

  const filtered = accounts.filter(a => !search || a.merchantName.toLowerCase().includes(search.toLowerCase()));

  const create = async () => {
    if(!form.account_name||!form.account_number||!form.ifsc_code||!form.bank_name||!form.branch){ showToast('Fill all fields','error'); return; }
    try {
      await accountAPI.create(form);
      await reload();
      setShowCreate(false);
      setForm(empty);
      showToast('Account created');
    } catch {
      showToast('Failed to create account','error');
    }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18,gap:8,flexWrap:'wrap' }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Account Management</h2>
        <Btn onClick={()=>setShowCreate(true)}>+ Add Account</Btn>
      </div>
      <Card>
        <div style={{ padding:'14px 20px',borderBottom:`1px solid ${T.border}` }}>
          <div style={{ position:'relative',maxWidth:320 }}>
            <span style={{ position:'absolute',left:10,top:'50%',transform:'translateY(-50%)',color:T.textMuted,fontSize:14 }}>🔍</span>
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search by Merchant Name..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
        </div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Merchant Name','Status','Account Details'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && <tr><td colSpan={3} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No accounts found</td></tr>}
              {filtered.map((a,i)=>(
                <tr key={a.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{a.merchantName}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <Btn size="sm" variant={a.status==='ACTIVE'?'success':'danger'} onClick={()=>toggleStatus(a)}>{a.status==='ACTIVE'?'● ACTIVE':'○ INACTIVE'}</Btn>
                  </td>
                  <td style={{ padding:'11px 14px' }}><Btn size="sm" variant="ghost" onClick={()=>setDetail(a)}>View Details</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {detail && (
        <Modal title="Account Details" onClose={()=>setDetail(null)}>
          {[['Account Name',detail.accountName],['Account Number',detail.accountNumber],['IFSC Code',detail.ifscCode],['Bank Name',detail.bankName],['Branch',detail.branch],['Account Type',detail.accountType],['Reference Number',detail.referenceNumber],['Status',detail.status]].map(([k,v])=>(
            <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'10px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
              <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
              <span style={{ fontSize:13,fontWeight:700,color:T.textMain,textAlign:'right' }}>{v}</span>
            </div>
          ))}
        </Modal>
      )}

      {showCreate && (
        <Modal title="Add Bank Account" onClose={()=>setShowCreate(false)} wide>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            <Input label="Account Name" value={form.account_name} onChange={e=>set('account_name',e.target.value)} required/>
            <Input label="Account Number" value={form.account_number} onChange={e=>set('account_number',e.target.value)} required/>
            <Input label="IFSC Code" value={form.ifsc_code} onChange={e=>set('ifsc_code',e.target.value.toUpperCase())} required/>
            <Input label="Bank Name" value={form.bank_name} onChange={e=>set('bank_name',e.target.value)} required/>
            <Input label="Branch" value={form.branch} onChange={e=>set('branch',e.target.value)} required/>
            <Sel label="Account Type" value={form.account_type} onChange={e=>set('account_type',e.target.value)} options={['Savings Account','Current Account'].map(v=>({value:v,label:v}))}/>
            <Sel label="Status" value={form.status} onChange={e=>set('status',e.target.value)} options={['ACTIVE','INACTIVE'].map(v=>({value:v,label:v}))}/>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create}>Create Account</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
    </div>
  );
};

// ─── SA Dashboard ─────────────────────────────────────────────────────────────
export const SaDashboard: React.FC = () => {
  const [merchants, setMerchants] = useState<User[]>([]);
  const [admins, setAdmins] = useState<User[]>([]);
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = () => Promise.all([userAPI.getMerchants(), userAPI.getAdmins(), transactionAPI.getAll()])
    .then(([m,a,t]) => { setMerchants(m); setAdmins(a); setTxns(t); })
    .catch(()=>{});
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  // Volume from real completed deposit + withdrawal amounts.
  const completed = txns.filter(t => t.status === 'COMPLETED' && (t.type.startsWith('DEPOSIT') || t.type.startsWith('WITHDRAWAL')));
  const ym = new Date().toISOString().slice(0, 7);
  const monthlyVolume = completed.filter(t => (t.date || '').startsWith(ym)).reduce((a, t) => a + t.amount, 0);
  const chartData = [...Array(7)].map((_, i) => {
    const d = new Date(); d.setDate(d.getDate() - (6 - i));
    const key = d.toISOString().slice(0, 10);
    const onDay = (pfx: string) => completed.filter(t => t.type.startsWith(pfx) && t.date === key).reduce((a, t) => a + t.amount, 0);
    return { day: d.toLocaleDateString('en-IN', { weekday: 'short' }), deposit: onDay('DEPOSIT'), withdrawal: onDay('WITHDRAWAL') };
  });

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:14,marginBottom:20 }}>
        <StatCard icon="🛡" label="Total Admins" value={admins.length} color={T.blue}/>
        <StatCard icon="🏪" label="Total Merchants" value={merchants.length} color={T.success}/>
        <StatCard icon="✅" label="Active Admins" value={admins.filter(a=>a.active).length} color={T.info}/>
        <StatCard icon="💹" label="Monthly Volume" value={fmt(monthlyVolume)} color={T.warning}/>
      </div>
      <Card style={{ padding:22,marginBottom:20 }}>
        <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800 }}>Platform Volume</h3>
        <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>All merchants — completed deposits & withdrawals, last 7 days</p>
        <MiniBar data={chartData}/>
      </Card>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Admins Overview</h3>
        </div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : (
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['Admin','Email','Merchants Created','Status'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {admins.map((a,i)=>(
                  <tr key={a.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px',fontWeight:700 }}>{a.name}</td>
                    <td style={{ padding:'11px 14px',color:T.textMuted }}>{a.email}</td>
                    <td style={{ padding:'11px 14px',fontWeight:800,color:T.blue }}>{a.merchantCount ?? 0}</td>
                    <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:a.active?T.successBg:T.dangerBg,color:a.active?T.success:T.danger }}>{a.active?'Active':'Inactive'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
};

// ─── SA Admin Management (with monitoring + merchant drill-down) ─────────────────
export const SaAdminsPage: React.FC = () => {
  const { showToast } = useToast();
  const [admins, setAdmins] = useState<User[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name:'',username:'',email:'',countryCode:'+91',phone:'',password:'',confirmPassword:'' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  const passwordMismatch = !!form.confirmPassword && form.password !== form.confirmPassword;
  const [drill, setDrill] = useState<User | null>(null);
  const [drillMerchants, setDrillMerchants] = useState<User[]>([]);

  const reload = () => userAPI.getAdmins().then(setAdmins).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!drill && !showCreate) reload(); });

  const openDrill = async (a: User) => {
    setDrill(a);
    try { setDrillMerchants(await userAPI.getAdminMerchants(a.id)); }
    catch { setDrillMerchants([]); }
  };

  const create = async () => {
    if(!form.name||!form.username||!form.email||!form.password){ showToast('Fill all fields','error'); return; }
    if(form.password !== form.confirmPassword){ showToast('Passwords do not match','error'); return; }
    try {
      await userAPI.createAdmin({ name:form.name, username:form.username, email:form.email, phone:form.phone?`${form.countryCode} ${form.phone}`:undefined, password:form.password, role:'ADMIN' });
      await reload();
      setShowCreate(false);
      setForm({ name:'',username:'',email:'',countryCode:'+91',phone:'',password:'',confirmPassword:'' });
      showToast(`Admin "${form.name}" created`);
    } catch {
      showToast('Failed to create admin','error');
    }
  };

  const toggleAdmin = async (a: User) => {
    try { await userAPI.toggleStatus(a.id); await reload(); showToast(`${a.name} ${a.active?'deactivated':'activated'}`); }
    catch { showToast('Failed to update admin','error'); }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <div>
          <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Admin Management</h2>
          <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{admins.length} admin{admins.length===1?'':'s'} · Click an admin to view their merchants</p>
        </div>
        <Btn onClick={()=>setShowCreate(true)}>+ Create Admin</Btn>
      </div>
      {showCreate && (
        <Modal title="Create Admin Account" onClose={()=>setShowCreate(false)}>
          <Input label="Full Name" value={form.name} onChange={e=>set('name',e.target.value)} required placeholder="Admin's full name"/>
          <Input label="Username" value={form.username} onChange={e=>set('username',e.target.value)} required placeholder="Login username"/>
          <Input label="Email ID" type="email" value={form.email} onChange={e=>set('email',e.target.value)} required placeholder="admin@company.com"/>
          <div style={{ marginBottom:16 }}>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Phone Number</label>
            <div style={{ display:'flex',gap:8 }}>
              <select value={form.countryCode} onChange={e=>set('countryCode',e.target.value)}
                style={{ width:130,padding:'10px 8px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:13,outline:'none',fontFamily:'inherit',background:T.surface }}>
                {COUNTRY_CODES.map(c=><option key={c.code} value={c.code}>{c.label}</option>)}
              </select>
              <input value={form.phone} onChange={e=>set('phone',e.target.value.replace(/[^\d]/g,''))} placeholder="Phone number"
                style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,outline:'none',fontFamily:'inherit',boxSizing:'border-box' }}/>
            </div>
          </div>
          <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} required placeholder="Set login password" hint="Admin will use this to login"/>
          <Input label="Confirm Password" type="password" value={form.confirmPassword} onChange={e=>set('confirmPassword',e.target.value)} required placeholder="Re-enter password"/>
          {passwordMismatch && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create} disabled={passwordMismatch}>Create Admin</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))',gap:14 }}>
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
            <div style={{ display:'flex',justifyContent:'space-between',padding:'8px 0',borderTop:`1px solid ${T.borderLight}`,borderBottom:`1px solid ${T.borderLight}`,marginBottom:12 }}>
              <span style={{ fontSize:12,color:T.textMuted }}>Phone</span>
              <span style={{ fontSize:12,fontWeight:700 }}>{a.phone||'—'}</span>
            </div>
            <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:14 }}>
              <span style={{ fontSize:12,color:T.textMuted }}>Merchants Created</span>
              <span style={{ fontSize:20,fontWeight:800,color:T.blue }}>{a.merchantCount ?? 0}</span>
            </div>
            <div style={{ display:'flex',gap:8 }}>
              <Btn size="sm" style={{ flex:1,justifyContent:'center' }} onClick={()=>openDrill(a)}>View Merchants</Btn>
              <Btn size="sm" variant={a.active?'danger':'success'} onClick={()=>toggleAdmin(a)}>{a.active?'Deactivate':'Activate'}</Btn>
            </div>
          </Card>
        ))}
      </div>

      {drill && (
        <Modal title={`Merchants created by ${drill.name}`} onClose={()=>{setDrill(null);setDrillMerchants([]);}} wide>
          {drillMerchants.length === 0
            ? <div style={{ padding:24,textAlign:'center',color:T.textMuted }}>No merchants created by this admin.</div>
            : (
              <div style={{ overflowX:'auto' }}>
                <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
                  <thead>
                    <tr style={{ background:T.canvas }}>
                      {['Business Name','User ID','Phone Number','Email ID','Created On'].map(h=>(
                        <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {drillMerchants.map((m,i)=>(
                      <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                        <td style={{ padding:'11px 14px',fontWeight:700 }}>{m.name}</td>
                        <td style={{ padding:'11px 14px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11 }}>{m.username}</code></td>
                        <td style={{ padding:'11px 14px',color:T.textMuted }}>{m.phone||'—'}</td>
                        <td style={{ padding:'11px 14px',color:T.textMuted }}>{m.email}</td>
                        <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{formatDateTime(m.createdAt || m.created)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </Modal>
      )}
    </div>
  );
};

// ─── System Logs (Super Admin) ─────────────────────────────────────────────────
export const SystemLogsPage: React.FC = () => {
  const [logs, setLogs] = useState<SystemLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');

  const reload = () => systemLogAPI.list().then(setLogs).catch(()=>setLogs([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  const filtered = logs.filter(l => !q ||
    l.actor.toLowerCase().includes(q.toLowerCase()) ||
    l.action.toLowerCase().includes(q.toLowerCase()) ||
    l.detail.toLowerCase().includes(q.toLowerCase()));

  return (
    <div>
      <div style={{ marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>System Logs</h2>
        <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Audit trail of logins, user management and transaction activity</p>
      </div>
      <Card>
        <div style={{ padding:'14px 20px',borderBottom:`1px solid ${T.border}` }}>
          <div style={{ position:'relative',maxWidth:340 }}>
            <span style={{ position:'absolute',left:10,top:'50%',transform:'translateY(-50%)',color:T.textMuted,fontSize:14 }}>🔍</span>
            <input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search actor, action or detail..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
        </div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Time','Actor','Action','Detail'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && <tr><td colSpan={4} style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</td></tr>}
              {!loading && filtered.length === 0 && <tr><td colSpan={4} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No logs found</td></tr>}
              {filtered.map((l,i)=>(
                <tr key={l.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{formatDateTime(l.createdAt)}</td>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{l.actor}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.infoBg,color:T.blue,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{l.action}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMain }}>{l.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {logs.length}</p>
        </div>
      </Card>
    </div>
  );
};
