import React, { useState, useEffect, useRef } from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, fileToDataUrl, downloadDataUrl, downloadText } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, StatusChart, Modal, Badge } from '../components/UI';
import TxTable from '../components/TxTable';
import { transactionAPI, supportAPI, supportWsUrl, userAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { useAuth } from '../context/AuthContext';
import type { Transaction, User, SupportMessage, BalanceSummary } from '../types';

// ─── Proof upload field (shared by request forms) ──────────────────────────────
const ProofUpload: React.FC<{ value: string | null; onChange: (v: string | null) => void }> = ({ value, onChange }) => {
  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) onChange(await fileToDataUrl(f));
  };
  return (
    <div style={{ marginBottom:16 }}>
      <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Proof Document / Image</label>
      <input type="file" accept="image/*,.pdf" onChange={onFile} style={{ fontSize:12 }} />
      {value && <img src={value} alt="proof" style={{ display:'block',marginTop:8,maxHeight:140,maxWidth:'100%',objectFit:'contain',borderRadius:8,border:`1px solid ${T.border}` }} />}
    </div>
  );
};

// ─── Merchant payment-slip modal (pay using admin details, submit proof) ────────
const SlipRow = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <div style={{ display:'flex',justifyContent:'space-between',padding:'8px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
    <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
    <span style={{ fontSize:12,fontWeight:700,color:T.textMain,textAlign:'right' }}>{v}</span>
  </div>
);

export const MerchantSlipModal: React.FC<{
  tx: Transaction;
  onClose: () => void;
  onSubmitted?: () => void;
}> = ({ tx, onClose, onSubmitted }) => {
  const { showToast } = useToast();
  const [proof, setProof] = useState<string | null>(null);
  const [ref, setRef] = useState('');
  const [loading, setLoading] = useState(false);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) setProof(await fileToDataUrl(f));
  };

  const canSubmit = !!proof || !!ref.trim();
  // Slip submission only applies to deposits awaiting the merchant's payment proof.
  const canSubmitSlip = tx.type.startsWith('DEPOSIT') && tx.status === 'ACCOUNT_SUBMITTED';
  const adminLabel = tx.type.startsWith('DEPOSIT') ? 'Payment Details from Admin' : 'Payment Receipt from Admin';

  const submit = async () => {
    if (!canSubmit) { showToast('Upload an image or enter a reference number', 'error'); return; }
    setLoading(true);
    try {
      await transactionAPI.submitSlip(tx.id, { merchantProof: proof || undefined, merchantRef: ref.trim() || undefined });
      showToast('Payment proof submitted');
      onSubmitted?.();
      onClose();
    } catch {
      showToast('Failed to submit proof', 'error');
    } finally {
      setLoading(false);
    }
  };

  const helper = proof
    ? 'Image attached — reference number is now optional.'
    : ref.trim()
      ? 'Reference entered — image upload is now optional.'
      : 'Upload an image or enter a reference number (at least one is required).';

  const hasAdminDetails = !!(tx.adminProof || tx.adminBankDetails);
  const downloadDetails = () => {
    if (tx.adminProof) {
      downloadDataUrl(tx.adminProof, `account-details-${tx.ref}.png`);
    } else if (tx.adminBankDetails) {
      const lines = [
        `Clari5Pay — Payment Details for ${tx.ref}`,
        `Amount: ${fmt(tx.amount)}`,
        tx.adminBankDetails,
        tx.adminUpiId ? `UPI ID: ${tx.adminUpiId}` : '',
        tx.adminRef ? `Reference: ${tx.adminRef}` : '',
      ].filter(Boolean).join('\n');
      downloadText(lines, `account-details-${tx.ref}.txt`);
    }
  };

  return (
    <Modal title={`${canSubmitSlip ? 'Pay & Submit Proof' : 'Request Details'} — ${tx.ref}`} onClose={onClose}>
      <div style={{ marginBottom:16 }}>
        <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>{adminLabel}</p>
        <div style={{ background:T.canvas,borderRadius:10,padding:12 }}>
          <SlipRow k="Amount" v={fmt(tx.amount)} />
          <SlipRow k="Status" v={<Badge status={tx.status} type={tx.type} viewerRole="MERCHANT" />} />
          {tx.adminUpiId && <SlipRow k="UPI ID" v={tx.adminUpiId} />}
          {tx.adminBankDetails && <SlipRow k="Bank Details" v={tx.adminBankDetails} />}
          {tx.adminRef && <SlipRow k="Reference" v={tx.adminRef} />}
          {!tx.adminUpiId && !tx.adminBankDetails && !tx.adminRef && !tx.adminProof &&
            <p style={{ fontSize:12,color:T.textMuted,margin:0 }}>Awaiting details from admin.</p>}
        </div>
        {tx.adminProof && <img src={tx.adminProof} alt="Admin details" style={{ width:'100%',maxHeight:200,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
        {hasAdminDetails && (
          <Btn size="sm" variant="ghost" style={{ marginTop:10 }} onClick={downloadDetails}>
            ⬇ Download {tx.type.startsWith('DEPOSIT') ? 'Account Details' : 'Receipt'}
          </Btn>
        )}
      </div>

      {/* Already-submitted slip (read-only) */}
      {!canSubmitSlip && (tx.merchantProof || tx.merchantRef) && (
        <div style={{ borderTop:`1px solid ${T.border}`,paddingTop:14,marginBottom:4 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Your Submitted Proof</p>
          {tx.merchantRef && <SlipRow k="Reference" v={tx.merchantRef} />}
          {tx.merchantProof && <img src={tx.merchantProof} alt="slip" style={{ display:'block',marginTop:8,maxHeight:180,maxWidth:'100%',objectFit:'contain',borderRadius:8,border:`1px solid ${T.border}` }} />}
        </div>
      )}

      {canSubmitSlip ? (
        <div style={{ borderTop:`1px solid ${T.border}`,paddingTop:14 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Submit Your Payment Proof</p>
          <Input label="Reference Number" value={ref} onChange={e=>setRef(e.target.value)} placeholder="Payment / UTR reference" />
          <div style={{ marginBottom:10 }}>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Upload Slip Image</label>
            <input type="file" accept="image/*,.pdf" onChange={onFile} style={{ fontSize:12 }} />
            {proof && <img src={proof} alt="slip" style={{ display:'block',marginTop:8,maxHeight:160,maxWidth:'100%',objectFit:'contain',borderRadius:8,border:`1px solid ${T.border}` }} />}
          </div>
          <p style={{ fontSize:11,color:canSubmit?T.success:T.textMuted,margin:'0 0 14px',fontWeight:600 }}>{helper}</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={submit} disabled={loading||!canSubmit}>{loading?'Submitting...':'Submit Proof'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
        </div>
      ) : (
        <div style={{ display:'flex',justifyContent:'flex-end' }}>
          <Btn variant="secondary" onClick={onClose}>Close</Btn>
        </div>
      )}
    </Modal>
  );
};

// ─── Merchant Dashboard ──────────────────────────────────────────────────────
export const MerchantDashboard: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [summary, setSummary] = useState<BalanceSummary | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = () => Promise.all([transactionAPI.getMine(), transactionAPI.summary()])
    .then(([t, s]) => { setTxns(t); setSummary(s); })
    .catch(()=>{});
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  // Dashboard shows only in-flight requests (Account Requested / Submitted / Pending).
  const inFlight = txns.filter(t => t.status === 'ACCOUNT_REQUESTED' || t.status === 'ACCOUNT_SUBMITTED' || t.status === 'SLIP_SUBMITTED');

  // Real-time status breakdown from this merchant's own records.
  const cnt = (s: string) => txns.filter(t => t.status === s).length;
  const statusData = [
    { label: 'Account Requested', value: cnt('ACCOUNT_REQUESTED'), color: T.warning },
    { label: 'Account Submitted', value: cnt('ACCOUNT_SUBMITTED'), color: T.info },
    { label: 'Slip Submitted', value: cnt('SLIP_SUBMITTED'), color: T.blue },
    { label: 'Completed', value: cnt('COMPLETED'), color: T.success },
  ];

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:16,marginBottom:22 }}>
        <StatCard icon="💰" label="Available Balance" value={fmt(summary?.available ?? 0)} sub="Updated now" color={T.success}/>
        <StatCard icon="↓" label="No. of Deposits" value={summary?.depositCount ?? 0} color={T.blue}/>
        <StatCard icon="↑" label="No. of Withdrawals" value={summary?.withdrawalCount ?? 0} color={T.danger}/>
        <StatCard icon="⧗" label="Pending Requests" value={inFlight.length} sub="In progress" color={T.warning}/>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'2fr 1fr',gap:16,marginBottom:22 }}>
        <Card style={{ padding:22 }}>
          <div style={{ marginBottom:14 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Requests by Status</h3>
            <p style={{ margin:0,fontSize:11,color:T.textMuted }}>Live counts from your transactions</p>
          </div>
          <StatusChart data={statusData}/>
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
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Pending Requests</h3></div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={inFlight.slice(0,6)} viewerRole="MERCHANT"/>}
      </Card>
    </div>
  );
};

// ─── Deposit form (used inside the Request modal) ──────────────────────────────
export const DepositForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',depositType:'UPI',memberName:'',memberId:'',segment:'A',profile:'NEW' });
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const submit = async () => {
    if(!form.amount||!form.memberName||!form.memberId){ showToast('Fill all required fields','error'); return; }
    setLoading(true);
    try {
      await transactionAPI.createDeposit({ ...form, amount: parseFloat(form.amount) });
      showToast('Deposit request submitted — awaiting admin review');
      onSubmitted?.();
    } catch {
      showToast('Failed to submit deposit request','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Sel label="Deposit Type" value={form.depositType} onChange={e=>set('depositType',e.target.value)} options={['UPI','QR','IMPS','NEFT','RTGS','CASH'].map(v=>({value:v,label:v}))} required/>
        <Input label="Amount (INR ₹)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="0.00" required icon="₹"/>
        <Input label="Member Name" value={form.memberName} onChange={e=>set('memberName',e.target.value)} placeholder="Full name" required/>
        <Input label="Member ID" value={form.memberId} onChange={e=>set('memberId',e.target.value)} placeholder="e.g. MBR20240001" required/>
        <Sel label="Segment" value={form.segment} onChange={e=>set('segment',e.target.value)} options={['A','B','C','D'].map(v=>({value:v,label:`Segment ${v}`}))}/>
        <Sel label="Profile" value={form.profile} onChange={e=>set('profile',e.target.value)} options={[{value:'OLD',label:'OLD'},{value:'NEW',label:'NEW'}]}/>
      </div>
      <p style={{ fontSize:12,color:T.textMuted,margin:'4px 0 16px' }}>After you submit, the admin will send payment details. You then pay and upload your slip from the Action button.</p>
      <Btn size="lg" full onClick={submit} disabled={loading||!form.amount||!form.memberName}>{loading?'Submitting...':'Submit Deposit Request →'}</Btn>
    </div>
  );
};

// ─── Withdrawal form ───────────────────────────────────────────────────────────
export const WithdrawalForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',memberId:'',accountHolder:'',accountNumber:'',ifsc:'',bankName:'' });
  const [available, setAvailable] = useState(0);
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { transactionAPI.summary().then(s => setAvailable(s.available)).catch(()=>{}); }, []);

  const submit = async () => {
    if(!form.amount||!form.accountHolder||!form.accountNumber){ showToast('Fill all required fields','error'); return; }
    if(parseFloat(form.amount) > available){ showToast('Total available balance insufficient — try again','error'); return; }
    setLoading(true);
    try {
      await transactionAPI.createWithdrawal({ ...form, amount: parseFloat(form.amount) });
      showToast('Withdrawal request submitted');
      onSubmitted?.();
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to submit withdrawal','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div style={{ background:T.infoBg,borderRadius:10,padding:'10px 14px',marginBottom:14,fontSize:12,color:T.blue,fontWeight:700 }}>
        Available balance: {fmt(available)}
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Amount (INR ₹)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="0.00" required icon="₹"/>
        <Input label="Member ID" value={form.memberId} onChange={e=>set('memberId',e.target.value)} placeholder="Alphanumeric" required/>
        <Input label="Account Holder" value={form.accountHolder} onChange={e=>set('accountHolder',e.target.value)} placeholder="As per bank" required/>
        <Input label="Account Number" value={form.accountNumber} onChange={e=>set('accountNumber',e.target.value)} placeholder="Account number" required/>
        <Input label="IFSC Code" value={form.ifsc} onChange={e=>set('ifsc',e.target.value)} placeholder="e.g. HDFC0001234" required/>
        <Input label="Bank Name" value={form.bankName} onChange={e=>set('bankName',e.target.value)} placeholder="e.g. HDFC Bank" required/>
      </div>
      <p style={{ fontSize:12,color:T.textMuted,margin:'4px 0 16px' }}>After you submit, the admin will send payment details. You then pay and upload your slip from the Action button.</p>
      <Btn size="lg" full variant="danger" style={{ background:T.danger,color:'#fff' }} onClick={submit} disabled={loading||!form.amount||!form.accountHolder}>
        {loading?'Submitting...':'Submit Withdrawal Request →'}
      </Btn>
    </div>
  );
};

// ─── Settlement form ───────────────────────────────────────────────────────────
export const SettlementForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ user, onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'', memberId:'' });
  const [proof, setProof] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const submit = async () => {
    if(!form.amount){ showToast('Enter an amount','error'); return; }
    setLoading(true);
    try {
      await transactionAPI.createSettlement({ amount: parseFloat(form.amount), memberId: form.memberId || undefined, proof: proof || undefined });
      showToast('Settlement request submitted');
      onSubmitted?.();
    } catch {
      showToast('Failed to submit settlement','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div style={{ background:T.grad3,borderRadius:14,padding:20,marginBottom:18,textAlign:'center' }}>
        <p style={{ fontSize:11,color:'rgba(255,255,255,0.6)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Available Balance</p>
        <p style={{ fontSize:30,fontWeight:800,color:'#fff',margin:0 }}>{fmt(user.balance||0)}</p>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Settlement Amount (INR ₹)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="Enter amount" icon="₹" required/>
        <Input label="Member ID" value={form.memberId} onChange={e=>set('memberId',e.target.value)} placeholder="e.g. MBR20240001"/>
      </div>
      <ProofUpload value={proof} onChange={setProof}/>
      <Btn size="lg" full onClick={submit} disabled={loading||!form.amount}>{loading?'Submitting...':'Submit Settlement Request →'}</Btn>
    </div>
  );
};

// ─── Generic management page (history grouped by Member ID) ─────────────────────
const ManagementPage: React.FC<{
  user: User;
  title: string;
  prefix: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT';
  requestLabel: string;
  noun: string;
  FormComp: React.FC<{ user: User; onSubmitted?: () => void }>;
}> = ({ user, title, prefix, requestLabel, noun, FormComp }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [openMember, setOpenMember] = useState<string | null>(null);
  const [slipTx, setSlipTx] = useState<Transaction | null>(null);

  const reload = () => transactionAPI.getMine().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(() => { if (!showForm && !slipTx) reload(); });

  const mine = txns.filter(t => t.type.startsWith(prefix));

  // Group by Member ID, most requests first.
  const groups = Object.values(
    mine.reduce((acc, t) => {
      const key = t.memberId || t.member || 'Unassigned';
      (acc[key] ||= { key, items: [] as Transaction[] }).items.push(t);
      return acc;
    }, {} as Record<string, { key: string; items: Transaction[] }>)
  ).sort((a, b) => b.items.length - a.items.length);

  const active = openMember ? groups.find(g => g.key === openMember) : null;

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18,gap:8,flexWrap:'wrap' }}>
        <div>
          <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>{title}</h2>
          <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{noun} history grouped by Member ID</p>
        </div>
        <Btn onClick={()=>setShowForm(true)}>+ {requestLabel}</Btn>
      </div>

      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Members ({groups.length})</h3>
        </div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : (
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['Member ID',`Total ${noun} Requests`,'Total Amount','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.length === 0 && <tr><td colSpan={4} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No {noun.toLowerCase()} requests yet</td></tr>}
                {groups.map((g,i)=>(
                  <tr key={g.key} style={{ background:i%2===0?T.surface:'#f8faff',cursor:'pointer' }} onClick={()=>setOpenMember(g.key)}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{g.key}</td>
                    <td style={{ padding:'11px 14px',fontWeight:800,color:T.blue }}>{g.items.length}</td>
                    <td style={{ padding:'11px 14px',fontWeight:700 }}>{fmt(g.items.reduce((a,t)=>a+t.amount,0))}</td>
                    <td style={{ padding:'11px 14px' }}><Btn size="sm" variant="ghost" onClick={(e?:any)=>{ e?.stopPropagation?.(); setOpenMember(g.key); }}>View History</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {showForm && (
        <Modal title={requestLabel} onClose={()=>setShowForm(false)} wide>
          <FormComp user={user} onSubmitted={()=>{ setShowForm(false); reload(); }}/>
        </Modal>
      )}

      {active && (
        <Modal title={`Member ${active.key} — ${noun} History`} onClose={()=>setOpenMember(null)} wide>
          <div style={{ display:'flex',gap:14,marginBottom:14,flexWrap:'wrap' }}>
            <div style={{ background:T.infoBg,borderRadius:10,padding:'10px 16px' }}>
              <p style={{ margin:0,fontSize:10,color:T.textMuted,fontWeight:700,textTransform:'uppercase' }}>Total {noun} Requests</p>
              <p style={{ margin:0,fontSize:22,fontWeight:800,color:T.blue }}>{active.items.length}</p>
            </div>
            <div style={{ background:T.successBg,borderRadius:10,padding:'10px 16px' }}>
              <p style={{ margin:0,fontSize:10,color:T.textMuted,fontWeight:700,textTransform:'uppercase' }}>Total Amount</p>
              <p style={{ margin:0,fontSize:22,fontWeight:800,color:T.success }}>{fmt(active.items.reduce((a,t)=>a+t.amount,0))}</p>
            </div>
          </div>
          <TxTable txns={active.items} actionMode="merchant" viewerRole="MERCHANT" onAction={(t)=>setSlipTx(t)}/>
        </Modal>
      )}

      {slipTx && (
        <MerchantSlipModal tx={slipTx} onClose={()=>setSlipTx(null)} onSubmitted={()=>{ setSlipTx(null); reload(); }}/>
      )}
    </div>
  );
};

export const DepositManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Deposit Management" prefix="DEPOSIT" requestLabel="Request Deposit" noun="Deposit" FormComp={DepositForm}/>;

export const WithdrawalManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Withdrawal Management" prefix="WITHDRAWAL" requestLabel="Request Withdrawal" noun="Withdrawal" FormComp={WithdrawalForm}/>;

export const SettlementManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Settlement Management" prefix="SETTLEMENT" requestLabel="Request Settlement" noun="Settlement" FormComp={SettlementForm}/>;

// ─── Balance Page ─────────────────────────────────────────────────────────────
export const BalancePage: React.FC<{ user: User }> = ({ user }) => {
  const [s, setS] = useState<BalanceSummary | null>(null);
  const reload = () => transactionAPI.summary().then(setS).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(reload);

  const totalDeposit = s?.totalDeposit ?? 0;
  const payInFees = s?.payInFees ?? 0;
  const totalSettled = s?.totalSettled ?? 0;
  const totalWithdrawn = s?.totalWithdrawn ?? 0;
  const payOutFees = s?.payOutFees ?? 0;
  const netAvailableBalance = totalDeposit - payInFees - totalSettled; // before withdrawals
  const available = s?.available ?? 0;                                 // spendable (after withdrawals)

  const rows: Array<[string, number, string, boolean]> = [
    ['Total Deposit Amount', totalDeposit, T.success, false],
    ['Pay-In Fees', payInFees, T.danger, false],
    ['Total Settled Amount', totalSettled, T.warning, false],
    ['Total Net Available Balance', netAvailableBalance, T.textMain, true],
    ['Total Withdrawn', totalWithdrawn, T.danger, false],
    ['Pay-Out Fees', payOutFees, T.danger, false],
    ['Net Available Withdrawal Amount', available, T.blue, true],
    ['Net Available Settlement Amount', available, T.success, true],
  ];

  return (
    <div style={{ maxWidth:600 }}>
      <Card style={{ padding:26 }}>
        <div style={{ background:T.grad3,borderRadius:16,padding:28,marginBottom:22,color:'#fff' }}>
          <p style={{ fontSize:11,color:'rgba(255,255,255,0.55)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Net Available Balance</p>
          <p style={{ fontSize:40,fontWeight:800,margin:'0 0 16px' }}>{fmt(available)}</p>
          <div style={{ display:'flex',gap:24,flexWrap:'wrap' }}>
            {[['Pay-In Fee',`${user.payInFee ?? 0}%`],['Pay-Out Fee',`${user.payOutFee ?? 0}%`],['Currency','INR']].map(([k,v])=>(
              <div key={k}><p style={{ fontSize:10,color:'rgba(255,255,255,0.45)',margin:0 }}>{k}</p><p style={{ fontWeight:700,margin:0,fontSize:14 }}>{v}</p></div>
            ))}
          </div>
        </div>
        {rows.map(([k,v,c,strong])=>(
          <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'11px 0',borderBottom:`1px solid ${T.borderLight}` }}>
            <span style={{ fontSize:13,color:strong?T.textMain:T.textMuted,fontWeight:strong?800:400 }}>{k}</span>
            <span style={{ fontSize:14,fontWeight:800,color:c }}>{fmt(v)}</span>
          </div>
        ))}
        <p style={{ fontSize:11,color:T.textMuted,margin:'14px 0 0' }}>Computed from completed transactions. Fees are applied at your configured rates.</p>
      </Card>
    </div>
  );
};

// ─── Cancel Request Page (DEO / Supervisor) ────────────────────────────────────
export const CancelRequestPage: React.FC<{ user: User }> = () => {
  const { showToast } = useToast();
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const reload = () => transactionAPI.getMine().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(() => { if (!busy) reload(); });

  // Only requests still in flight can be cancelled.
  const cancellable = txns.filter(t => t.status === 'ACCOUNT_REQUESTED' || t.status === 'ACCOUNT_SUBMITTED');

  const cancel = async (t: Transaction) => {
    if (!window.confirm(`Cancel request ${t.ref}?`)) return;
    setBusy(t.id);
    try { await transactionAPI.cancel(t.id); await reload(); showToast(`${t.ref} cancelled`); }
    catch { showToast('Failed to cancel request','error'); }
    finally { setBusy(null); }
  };

  return (
    <div>
      <div style={{ marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Cancel Request</h2>
        <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Cancel pending deposit, withdrawal or settlement requests</p>
      </div>
      <Card>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : (
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['Reference Number','Type','Amount','Member ID','Status','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cancellable.length === 0 && <tr><td colSpan={6} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No cancellable requests</td></tr>}
                {cancellable.map((t,i)=>(
                  <tr key={t.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px' }}><code style={{ fontSize:11,background:T.canvas,padding:'2px 6px',borderRadius:4,fontWeight:700 }}>{t.ref}</code></td>
                    <td style={{ padding:'11px 14px' }}>{typeLabel(t.type)}</td>
                    <td style={{ padding:'11px 14px',fontWeight:800 }}>{fmt(t.amount)}</td>
                    <td style={{ padding:'11px 14px',color:T.textMain }}>{t.memberId||'—'}</td>
                    <td style={{ padding:'11px 14px' }}><Badge status={t.status} type={t.type} viewerRole="MERCHANT"/></td>
                    <td style={{ padding:'11px 14px' }}>
                      <Btn size="sm" variant="danger" disabled={busy===t.id} onClick={()=>cancel(t)}>{busy===t.id?'Cancelling...':'⊘ Cancel'}</Btn>
                    </td>
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

// ─── All Templates View (Manager — read-only consolidated list) ─────────────────
export const TemplatesPage: React.FC<{ user: User }> = () => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = () => transactionAPI.getMine().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  return (
    <div>
      <div style={{ marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>All Templates View</h2>
        <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Read-only overview of all deposit, withdrawal and settlement requests</p>
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>All Requests ({txns.length})</h3>
        </div>
        {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={txns} viewerRole="MERCHANT"/>}
      </Card>
    </div>
  );
};

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

// ─── Transaction History ──────────────────────────────────────────────────────
const MERCHANT_TYPES = ['DEPOSIT_REQUEST','WITHDRAWAL_REQUEST','SETTLEMENT_REQUEST','DEPOSIT','WITHDRAWAL','SETTLEMENT'];
const MERCHANT_STATUSES = ['ACCOUNT_REQUESTED','ACCOUNT_SUBMITTED','SLIP_SUBMITTED','COMPLETED','CANCELLED'];

export const TransactionHistory: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [search, setSearch] = useState('');
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [loading, setLoading] = useState(true);

  const reload = () => {
    const fn = user.role === 'MERCHANT' ? transactionAPI.getMine : transactionAPI.getAll;
    return fn().then(setTxns).catch(()=>setTxns([]));
  };
  useEffect(() => { reload().finally(()=>setLoading(false)); }, [user.role]);
  usePoll(reload);

  const filtered = txns.filter(t => {
    const ms = !search || t.ref.toLowerCase().includes(search.toLowerCase()) || t.merchant.toLowerCase().includes(search.toLowerCase());
    return ms && (type === 'ALL' || t.type === type) && (status === 'ALL' || t.status === status);
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
            {['ALL',...MERCHANT_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
          </select>
          <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...MERCHANT_STATUSES].map(v=><option key={v} value={v}>{v==='ALL'?'All Statuses':typeLabel(v)}</option>)}
          </select>
        </div>
      </div>
      {loading ? <div style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</div> : <TxTable txns={filtered} viewerRole={user.role}/>}
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
    </Card>
  );
};

// ─── Customer Support chat (merchant side, WebSocket) ──────────────────────────
export const MerchantSupportChat: React.FC<{ user: User }> = ({ user }) => {
  const [messages, setMessages] = useState<SupportMessage[]>([]);
  const [input, setInput] = useState('');
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  useEffect(() => {
    supportAPI.myMessages().then(setMessages).catch(()=>{});
    const ws = new WebSocket(supportWsUrl());
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data) as SupportMessage;
        if (m.merchantId === user.id) setMessages(prev => prev.some(x=>x.id===m.id) ? prev : [...prev, m]);
      } catch { /* ignore */ }
    };
    return () => ws.close();
  }, [user.id]);

  const send = async () => {
    const content = input.trim();
    if (!content) return;
    setInput('');
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ content }));
    } else {
      try { const m = await supportAPI.send(content); setMessages(prev => [...prev, m]); } catch { /* ignore */ }
    }
  };

  return (
    <div style={{ maxWidth:800,height:'calc(100vh - 120px)',display:'flex',flexDirection:'column',gap:16 }}>
      <Card style={{ padding:'16px 20px' }}>
        <div style={{ display:'flex',alignItems:'center',gap:12 }}>
          <div style={{ width:44,height:44,borderRadius:14,background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22 }}>💬</div>
          <div>
            <h2 style={{ margin:0,fontSize:15,fontWeight:800 }}>Customer Support</h2>
            <p style={{ margin:0,fontSize:12,color:T.textMuted }}>Chat with our support team in real time</p>
          </div>
          <div style={{ marginLeft:'auto',display:'flex',alignItems:'center',gap:6 }}>
            <div style={{ width:8,height:8,borderRadius:'50%',background:connected?T.success:T.textLight }}/>
            <span style={{ fontSize:11,color:connected?T.success:T.textMuted,fontWeight:700 }}>{connected?'Connected':'Connecting...'}</span>
          </div>
        </div>
      </Card>

      <Card style={{ flex:1,padding:0,display:'flex',flexDirection:'column',overflow:'hidden' }}>
        <div style={{ flex:1,overflowY:'auto',padding:20,display:'flex',flexDirection:'column',gap:12 }}>
          {messages.length === 0 && <div style={{ margin:'auto',color:T.textMuted,fontSize:13 }}>No messages yet. Say hello 👋</div>}
          {messages.map((m)=>{
            const mine = m.sender === 'MERCHANT';
            return (
              <div key={m.id} style={{ display:'flex',justifyContent:mine?'flex-end':'flex-start' }}>
                <div style={{ maxWidth:'75%',padding:'10px 14px',borderRadius:mine?'16px 16px 4px 16px':'16px 16px 16px 4px',background:mine?T.grad1:T.canvas,color:mine?'#fff':T.textMain,fontSize:13,lineHeight:1.5 }}>
                  {!mine && <p style={{ margin:'0 0 2px',fontSize:10,fontWeight:800,color:T.blue }}>{m.senderName}</p>}
                  {m.content}
                  <p style={{ margin:'3px 0 0',fontSize:9,opacity:0.6,textAlign:'right' }}>{new Date(m.createdAt).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</p>
                </div>
              </div>
            );
          })}
          <div ref={bottomRef}/>
        </div>
        <div style={{ padding:'12px 16px',borderTop:`1px solid ${T.border}`,display:'flex',gap:10 }}>
          <input value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>e.key==='Enter'&&send()}
            placeholder="Type a message..."
            style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:12,fontSize:13,outline:'none',fontFamily:'inherit',color:T.textMain,background:T.canvas }}/>
          <Btn onClick={send} disabled={!input.trim()} style={{ borderRadius:12 }}>→ Send</Btn>
        </div>
      </Card>
    </div>
  );
};

// ─── Profile Page (centered details + Edit → email/password) ───────────────────
export const ProfilePage: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const { updateUser } = useAuth();
  const [edit, setEdit] = useState(false);
  const [form, setForm] = useState({ email:user.email, current:'', next:'', confirm:'' });
  const [saving, setSaving] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const save = async () => {
    if(form.next && form.next !== form.confirm){ showToast('Passwords do not match','error'); return; }
    setSaving(true);
    try {
      const updated = await userAPI.updateProfile({
        email: form.email !== user.email ? form.email : undefined,
        new_password: form.next || undefined,
        current_password: form.current || undefined,
      });
      updateUser({ email: updated.email });
      showToast('Profile updated successfully');
      setEdit(false);
      setForm(f => ({ ...f, current:'', next:'', confirm:'' }));
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to update profile','error');
    } finally {
      setSaving(false);
    }
  };

  const details: Array<[string,string]> = [
    ['Username', user.username],
    ['Email ID', user.email],
    ['Phone', user.phone || '—'],
    ['Role', user.role.replace('_',' ')],
    ['Member Since', user.created],
  ];
  if (user.role === 'MERCHANT') {
    details.push(['Pay-In Code', user.payIn||'—'],['Pay-Out Code', user.payOut||'—'],['Settlement Code', user.settlement||'—'],['Profile Type', user.profile||'—']);
  }

  return (
    <div style={{ maxWidth:560,margin:'0 auto',position:'relative' }}>
      <Card style={{ padding:'30px 28px' }}>
        <div style={{ position:'absolute',top:18,right:18 }}>
          <Btn size="sm" variant="ghost" onClick={()=>setEdit(true)}>✎ Edit</Btn>
        </div>
        <div style={{ display:'flex',flexDirection:'column',alignItems:'center',textAlign:'center',marginBottom:24 }}>
          <div style={{ width:78,height:78,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:32,fontWeight:800,color:'#fff',marginBottom:14 }}>{user.name.charAt(0)}</div>
          <p style={{ margin:0,fontWeight:800,fontSize:20,color:T.textMain }}>{user.name}</p>
          <p style={{ margin:'2px 0 0',fontSize:13,color:T.textMuted }}>{user.email}</p>
          <span style={{ marginTop:8,padding:'3px 12px',borderRadius:20,fontSize:11,fontWeight:700,background:T.infoBg,color:T.blue }}>{user.role.replace('_',' ')}</span>
        </div>
        <div>
          {details.map(([k,v])=>(
            <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'11px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
              <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
              <span style={{ fontSize:13,fontWeight:700,color:T.textMain,textAlign:'right' }}>{v}</span>
            </div>
          ))}
        </div>
      </Card>

      {edit && (
        <Modal title="Edit Profile" onClose={()=>setEdit(false)}>
          <Input label="Email ID" type="email" value={form.email} onChange={e=>set('email',e.target.value)} placeholder="you@company.com"/>
          <div style={{ borderTop:`1px solid ${T.border}`,margin:'4px 0 14px',paddingTop:14 }}>
            <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Change Password</p>
            <Input label="Current Password" type="password" value={form.current} onChange={e=>set('current',e.target.value)} placeholder="Required to change password"/>
            <Input label="New Password" type="password" value={form.next} onChange={e=>set('next',e.target.value)} placeholder="Leave blank to keep current"/>
            <Input label="Confirm New Password" type="password" value={form.confirm} onChange={e=>set('confirm',e.target.value)} placeholder="Re-enter new password"/>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={save} disabled={saving}>{saving?'Saving...':'Save Changes'}</Btn>
            <Btn variant="secondary" onClick={()=>setEdit(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
    </div>
  );
};
