import React, { useState, useEffect, useRef } from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, fileToDataUrl, downloadDataUrl, downloadText, merchantRoleLabel, formatDateTime } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, StatusChart, LoadingScreen, Modal, Badge, BankNamesDatalist } from '../components/UI';
import TxTable from '../components/TxTable';
import { TxExportButton } from '../components/TxExport';
import { transactionAPI, supportAPI, supportWsUrl, userAPI, bankAccountAPI, newsAPI } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { useAuth } from '../context/AuthContext';
import { lookupIfsc, isValidIfsc, bankBadge, BANK_NAMES } from '../utils/ifsc';
import type { Transaction, User, SupportMessage, BalanceSummary, MerchantBankAccount, NewsPost } from '../types';

// ─── Reusable merchant bank-account picker (select saved or add new) ───────────
type BankForm = { accountHolder: string; accountNumber: string; ifsc: string; branch: string; bankName: string };
const emptyBank: BankForm = { accountHolder: '', accountNumber: '', ifsc: '', branch: '', bankName: '' };

const BankAccountFields: React.FC<{
  memberId: string;
  bank: BankForm; onBank: (b: BankForm) => void; saveNew: boolean; onSaveNew: (v: boolean) => void;
}> = ({ memberId, bank, onBank, saveNew, onSaveNew }) => {
  const [saved, setSaved] = useState<MerchantBankAccount[]>([]);
  const [sel, setSel] = useState<string>('NEW');

  // Saved accounts are specific to the entered Membership ID — refetch whenever it changes.
  useEffect(() => {
    if (!memberId.trim()) { setSaved([]); setSel('NEW'); onBank(emptyBank); onSaveNew(true); return; }
    bankAccountAPI.listMine(memberId.trim()).then(all => {
      // Only real bank accounts here — UPI-only saved records would render as "null — null".
      const list = all.filter(a => a.accountNumber);
      setSaved(list);
      if (list.length) {
        const a = list[0];
        setSel(String(a.id));
        onBank({ accountHolder:a.accountHolder, accountNumber:a.accountNumber, ifsc:a.ifsc, branch:a.branch, bankName:a.bankName || '' });
        onSaveNew(false);
      } else {
        setSel('NEW'); onBank(emptyBank); onSaveNew(true);
      }
    }).catch(()=>{});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memberId]);

  const choose = (v: string) => {
    setSel(v);
    if (v === 'NEW') { onBank(emptyBank); onSaveNew(true); }
    else {
      const a = saved.find(x => String(x.id) === v);
      if (a) { onBank({ accountHolder:a.accountHolder, accountNumber:a.accountNumber, ifsc:a.ifsc, branch:a.branch, bankName:a.bankName || '' }); onSaveNew(false); }
    }
  };
  const set = (k: keyof BankForm, v: string) => onBank({ ...bank, [k]: v });

  // Typing an IFSC auto-fills the bank name + branch (Razorpay API; falls back to manual on failure).
  const onIfsc = async (raw: string) => {
    const up = raw.toUpperCase();
    onBank({ ...bank, ifsc: up });
    if (isValidIfsc(up)) {
      const info = await lookupIfsc(up);
      if (info) onBank({ ...bank, ifsc: up, bankName: info.bank, branch: info.branch });
    }
  };

  if (!memberId.trim()) {
    return (
      <div style={{ background:T.warningBg,borderRadius:10,padding:'10px 14px',margin:'4px 0 14px',fontSize:12,color:T.warning,fontWeight:600 }}>
        Enter a Membership ID above to add or select its bank account.
      </div>
    );
  }

  return (
    <div>
      <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'4px 0 8px' }}>Bank Account for {memberId}</p>
      {saved.length > 0 && (
        <Sel label="Select Bank Account" value={sel} onChange={e=>choose(e.target.value)}
          options={[...saved.map(a => ({ value:String(a.id), label:`${a.accountHolder} — ${a.accountNumber}` })), { value:'NEW', label:'➕ Add new account' }]} />
      )}
      {sel === 'NEW' && (
        <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
          <BankNamesDatalist names={BANK_NAMES}/>
          <Input label="Account Holder Name" value={bank.accountHolder} onChange={e=>set('accountHolder',e.target.value)} required/>
          <Input label="Account Number" value={bank.accountNumber} onChange={e=>set('accountNumber',e.target.value)} required/>
          <Input label="IFSC Code" value={bank.ifsc} onChange={e=>onIfsc(e.target.value)} required hint="Auto-fills bank & branch"/>
          <Input label="Branch Name" value={bank.branch} onChange={e=>set('branch',e.target.value)} required/>
          <div style={{ marginBottom:16 }}>
            <Input label="Bank Name" value={bank.bankName} onChange={e=>set('bankName',e.target.value)} list="bank-names" style={{ marginBottom:6 }}/>
            {bank.bankName && (() => { const b = bankBadge(bank.bankName); return (
              <span style={{ display:'inline-flex',alignItems:'center',gap:6,fontSize:11,color:T.textMuted }}>
                <span style={{ width:18,height:18,borderRadius:5,background:b.color,color:'#fff',display:'inline-flex',alignItems:'center',justifyContent:'center',fontSize:9,fontWeight:800 }}>{b.initials}</span>
                {bank.bankName}
              </span>); })()}
          </div>
        </div>
      )}
      {sel !== 'NEW' && (
        <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12,marginBottom:14 }}>
          <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Account Holder</span><b>{bank.accountHolder}</b></div>
          <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Account Number</span><b>{bank.accountNumber}</b></div>
          <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>IFSC</span><b>{bank.ifsc}</b></div>
          <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Branch</span><b>{bank.branch}</b></div>
        </div>
      )}
      {saveNew && <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 12px' }}>This account will be saved for future requests.</p>}
    </div>
  );
};

// ─── Proof/slip upload (up to 3 files; JPG/JPEG/PNG/PDF) ─────────────────────────
const PROOF_MAX = 3;
const PROOF_ACCEPT = 'image/jpeg,image/jpg,image/png,application/pdf,.jpg,.jpeg,.png,.pdf';
const PROOF_LIMIT_MSG = 'You can upload a maximum of 3 proof/slip files per request.';
const PROOF_TYPE_MSG = 'Unsupported file type. Allowed: JPG, JPEG, PNG, PDF.';
const isAllowedProof = (f: File) => {
  const t = (f.type || '').toLowerCase();
  if (['image/jpeg', 'image/jpg', 'image/png', 'application/pdf'].includes(t)) return true;
  return /\.(jpe?g|png|pdf)$/i.test(f.name);
};

const ProofThumb: React.FC<{ src: string }> = ({ src }) => {
  if (src.startsWith('data:application/pdf')) {
    return <div style={{ width:64,height:64,borderRadius:8,border:`1px solid ${T.border}`,display:'flex',alignItems:'center',justifyContent:'center',fontSize:11,fontWeight:800,color:T.danger,background:T.canvas }}>PDF</div>;
  }
  return <img src={src} alt="proof" style={{ width:64,height:64,objectFit:'cover',borderRadius:8,border:`1px solid ${T.border}` }} />;
};

// Read-only viewer for submitted proofs (images shown inline; PDFs as a download chip).
export const ProofGallery: React.FC<{ srcs: string[] }> = ({ srcs }) => (
  <div style={{ display:'flex',gap:10,flexWrap:'wrap',marginTop:8 }}>
    {srcs.map((src, i) => src.startsWith('data:application/pdf')
      ? <a key={i} href={src} download={`proof-${i + 1}.pdf`} style={{ display:'flex',width:90,height:110,borderRadius:8,border:`1px solid ${T.border}`,alignItems:'center',justifyContent:'center',flexDirection:'column',gap:4,fontSize:12,fontWeight:800,color:T.danger,background:T.canvas,textDecoration:'none' }}>PDF<span style={{ fontSize:9,color:T.textMuted }}>#{i + 1} ⬇</span></a>
      : <img key={i} src={src} alt={`proof ${i + 1}`} style={{ maxHeight:160,maxWidth:'48%',objectFit:'contain',borderRadius:8,border:`1px solid ${T.border}` }} />
    )}
  </div>
);

const MultiProofUpload: React.FC<{
  values: string[]; onChange: (v: string[]) => void; label?: string; required?: boolean;
}> = ({ values, onChange, label = 'Proof Document / Image', required }) => {
  const { showToast } = useToast();
  const onFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = '';
    if (!files.length) return;
    if (values.length + files.length > PROOF_MAX) { showToast(PROOF_LIMIT_MSG, 'error'); return; }
    if (files.some(f => !isAllowedProof(f))) { showToast(PROOF_TYPE_MSG, 'error'); return; }
    const urls = await Promise.all(files.map(fileToDataUrl));
    onChange([...values, ...urls]);
  };
  return (
    <div style={{ marginBottom:16 }}>
      <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}{required && <span style={{ color:T.danger }}> *</span>}</label>
      {values.length < PROOF_MAX && <input type="file" multiple accept={PROOF_ACCEPT} onChange={onFiles} style={{ fontSize:12 }} />}
      <p style={{ fontSize:11,color:T.textMuted,margin:'6px 0 0' }}>Up to {PROOF_MAX} files · JPG, JPEG, PNG, PDF ({values.length}/{PROOF_MAX})</p>
      {values.length > 0 && (
        <div style={{ display:'flex',gap:8,flexWrap:'wrap',marginTop:8 }}>
          {values.map((v, i) => (
            <div key={i} style={{ position:'relative' }}>
              <ProofThumb src={v} />
              <button onClick={()=>onChange(values.filter((_, j) => j !== i))} title="Remove" aria-label="Remove file"
                style={{ position:'absolute',top:-7,right:-7,width:20,height:20,borderRadius:'50%',border:'none',background:T.danger,color:'#fff',fontSize:13,lineHeight:1,cursor:'pointer',display:'flex',alignItems:'center',justifyContent:'center' }}>×</button>
            </div>
          ))}
        </div>
      )}
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
  const [proofs, setProofs] = useState<string[]>([]);
  const [ref, setRef] = useState('');
  const [loading, setLoading] = useState(false);
  // Proof/receipt images are omitted from list payloads; fetch them when the modal opens.
  const [imgs, setImgs] = useState<{ adminProof?: string | null; merchantProof?: string | null; merchantProofs?: string[] | null }>({ adminProof: tx.adminProof, merchantProof: tx.merchantProof, merchantProofs: tx.merchantProofs });
  useEffect(() => {
    transactionAPI.getDetail(tx.id).then(d => setImgs({ adminProof: d.adminProof, merchantProof: d.merchantProof, merchantProofs: d.merchantProofs })).catch(()=>{});
  }, [tx.id]);

  // Full account/payment details as shareable text (copy-all / share).
  const detailsText = [
    `Clari5Pay — Payment Details (${tx.ref})`,
    `Amount: ${fmt(tx.amount)}`,
    tx.adminBankDetails || '',
    tx.adminUpiId ? `UPI ID: ${tx.adminUpiId}` : '',
  ].filter(Boolean).join('\n');

  const copy = async (text: string, what: string) => {
    try { await navigator.clipboard.writeText(text); showToast(`${what} copied`); }
    catch { showToast('Copy failed', 'error'); }
  };
  // Share ALL account details as text.
  const shareAll = async () => {
    const nav = navigator as Navigator & { share?: (d: { title?: string; text?: string }) => Promise<void> };
    if (nav.share) { try { await nav.share({ title: 'Clari5Pay payment details', text: detailsText }); return; } catch { /* cancelled */ } }
    await copy(detailsText, 'All details');
  };

  // Both the UTR number and at least one payment proof are mandatory when submitting a slip.
  const canSubmit = proofs.length > 0 && !!ref.trim();
  // Slip submission only applies to deposits awaiting the merchant's payment proof.
  const canSubmitSlip = tx.type.startsWith('DEPOSIT') && tx.status === 'ACCOUNT_SUBMITTED';
  const adminLabel = tx.type.startsWith('DEPOSIT') ? 'Payment Details from Agent' : 'Payment Receipt from Agent';

  const submit = async () => {
    if (!ref.trim()) { showToast('Enter the UTR number', 'error'); return; }
    if (!proofs.length) { showToast('Upload the payment proof', 'error'); return; }
    setLoading(true);
    try {
      await transactionAPI.submitSlip(tx.id, { merchantProofs: proofs, merchantRef: ref.trim() || undefined });
      showToast('Payment proof submitted');
      onSubmitted?.();
      onClose();
    } catch {
      showToast('Failed to submit proof', 'error');
    } finally {
      setLoading(false);
    }
  };

  const helper = canSubmit
    ? 'UTR number and proof attached — ready to submit.'
    : 'Both the UTR number and a payment proof image are required.';

  const hasAdminDetails = !!(imgs.adminProof || tx.adminBankDetails);
  const downloadDetails = () => {
    if (imgs.adminProof) {
      downloadDataUrl(imgs.adminProof, `account-details-${tx.ref}.png`);
    } else if (tx.adminBankDetails) {
      const lines = [
        `Clari5Pay — Payment Details for ${tx.ref}`,
        `Amount: ${fmt(tx.amount)}`,
        tx.adminBankDetails,
        tx.adminUpiId ? `UPI ID: ${tx.adminUpiId}` : '',
      ].filter(Boolean).join('\n');
      downloadText(lines, `account-details-${tx.ref}.txt`);
    }
  };

  return (
    <Modal title={`${canSubmitSlip ? 'Pay & Submit Proof' : 'Request Details'} — ${tx.ref}`} onClose={onClose}>
      {tx.highRisk && (
        <div style={{ display:'flex',gap:10,alignItems:'flex-start',background:'#fdecea',border:'1px solid #f5b5ae',borderRadius:10,padding:'12px 14px',marginBottom:16 }}>
          <span style={{ fontSize:20,lineHeight:1 }}>⚠</span>
          <div>
            <p style={{ margin:0,fontSize:13,fontWeight:800,color:'#b71c1c' }}>High Risk — Member {tx.memberId || tx.ref}</p>
            <p style={{ margin:'2px 0 0',fontSize:12,color:'#7f1d1d' }}>{tx.rejectReason || 'Payment was not received in our bank for this member. Please contact support.'}</p>
          </div>
        </div>
      )}
      <div style={{ marginBottom:16 }}>
        <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>{adminLabel}</p>
        <div style={{ background:T.canvas,borderRadius:10,padding:12 }}>
          <SlipRow k="Amount" v={fmt(tx.amount)} />
          <SlipRow k="Status" v={<Badge status={tx.status} type={tx.type} viewerRole="MERCHANT" />} />
          {tx.adminUtr && <SlipRow k="UTR Number" v={tx.adminUtr} />}
          {/* Receiving account the merchant pays into — UPI ID (copyable) and/or bank details. */}
          {tx.adminUpiId && (
            <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',padding:'8px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
              <span style={{ fontSize:12,color:T.textMuted }}>UPI ID</span>
              <span style={{ display:'flex',alignItems:'center',gap:8 }}>
                <b style={{ fontSize:12,color:T.textMain }}>{tx.adminUpiId}</b>
                <Btn size="sm" variant="ghost" onClick={()=>copy(tx.adminUpiId || '', 'UPI ID')}>⧉ Copy</Btn>
              </span>
            </div>
          )}
          {tx.adminBankDetails && (
            <div style={{ marginTop:8 }}>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 4px' }}>Bank Details</p>
              <p style={{ fontSize:13,color:T.textMain,margin:0,whiteSpace:'pre-line',lineHeight:1.6 }}>{tx.adminBankDetails}</p>
            </div>
          )}
          {!tx.adminUpiId && !tx.adminBankDetails && !imgs.adminProof &&
            <p style={{ fontSize:12,color:T.textMuted,margin:0 }}>Awaiting updates from Agent.</p>}
        </div>

        {imgs.adminProof && <img src={imgs.adminProof} alt="Admin details" style={{ display:'block',width:'100%',height:'auto',objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
        {hasAdminDetails && (
          <div style={{ marginTop:10 }}>
            <div style={{ display:'flex',gap:8,flexWrap:'wrap' }}>
              {(tx.adminBankDetails || tx.adminUpiId) && (
                <>
                  <Btn size="sm" variant="ghost" onClick={()=>copy(detailsText, 'All details')}>⧉ Copy All Details</Btn>
                  <Btn size="sm" variant="ghost" onClick={shareAll}>↗ Share</Btn>
                </>
              )}
              <Btn size="sm" variant="ghost" onClick={downloadDetails}>
                ⬇ Download {tx.type.startsWith('DEPOSIT') ? 'Account Details' : 'Receipt'}
              </Btn>
            </div>
          </div>
        )}
      </div>

      {/* Already-submitted slip (read-only) */}
      {!canSubmitSlip && (imgs.merchantProof || tx.merchantRef) && (
        <div style={{ borderTop:`1px solid ${T.border}`,paddingTop:14,marginBottom:4 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Your Submitted Proof</p>
          {tx.merchantRef && <SlipRow k="Reference" v={tx.merchantRef} />}
          {(() => {
            const list = (imgs.merchantProofs && imgs.merchantProofs.length) ? imgs.merchantProofs : (imgs.merchantProof ? [imgs.merchantProof] : []);
            return list.length ? <ProofGallery srcs={list} /> : null;
          })()}
        </div>
      )}

      {canSubmitSlip ? (
        <div style={{ borderTop:`1px solid ${T.border}`,paddingTop:14 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Submit Your Payment Proof</p>
          <Input label="UTR Number" value={ref} onChange={e=>setRef(e.target.value)} placeholder="Bank UTR / payment reference" required />
          <MultiProofUpload values={proofs} onChange={setProofs} label="Upload Slip (up to 3)" required />
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

  // Real-time graphs from this merchant's own records.
  const settlementCount = txns.filter(t => t.type.startsWith('SETTLEMENT')).length;
  const depTx = txns.filter(t => t.type.startsWith('DEPOSIT'));
  const wdTx = txns.filter(t => t.type.startsWith('WITHDRAWAL'));
  const byStatus = (arr: Transaction[], s: string) => arr.filter(t => t.status === s).length;
  const depositGraph = [
    { label: 'Requested', value: byStatus(depTx, 'ACCOUNT_REQUESTED'), color: T.warning },
    { label: 'Submitted', value: byStatus(depTx, 'ACCOUNT_SUBMITTED'), color: T.info },
    { label: 'Slip', value: byStatus(depTx, 'SLIP_SUBMITTED'), color: T.blue },
    { label: 'Deposited', value: byStatus(depTx, 'COMPLETED'), color: T.success },
  ];
  const withdrawalGraph = [
    { label: 'Submitted', value: byStatus(wdTx, 'ACCOUNT_REQUESTED'), color: T.warning },
    { label: 'Completed', value: byStatus(wdTx, 'COMPLETED'), color: T.success },
  ];

  // Role-scoped dashboard cards.
  const role = String(user.merchantRole || '').toUpperCase();
  const pendingCard = <StatCard icon="⧗" label="Pending Requests" value={inFlight.length} sub="In progress" color={T.warning}/>;
  const balanceCard = <StatCard icon="💰" label="Available Balance" value={fmt(summary?.available ?? 0)} sub="Updated now" color={T.success}/>;
  let cards: React.ReactNode;
  if (role === 'DEPOSIT_OPERATOR') {
    cards = <><StatCard icon="↓" label="No. of Deposits" value={summary?.depositCount ?? 0} color={T.blue}/>{pendingCard}</>;
  } else if (role === 'WITHDRAWAL_OPERATOR') {
    cards = <>{balanceCard}<StatCard icon="↑" label="No. of Withdrawals" value={summary?.withdrawalCount ?? 0} color={T.danger}/>{pendingCard}</>;
  } else if (role === 'SUPERVISOR') {
    cards = <>{balanceCard}<StatCard icon="⇄" label="No. of Settlements" value={settlementCount} color={T.info}/>{pendingCard}</>;
  } else {
    cards = <>
      <StatCard icon="💰" label="Available Balance" value={fmt(summary?.available ?? 0)} sub="Updated now" color={T.success}/>
      <StatCard icon="↓" label="No. of Deposits" value={summary?.depositCount ?? 0} color={T.blue}/>
      <StatCard icon="↑" label="No. of Withdrawals" value={summary?.withdrawalCount ?? 0} color={T.danger}/>
      {pendingCard}
    </>;
  }

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:16,marginBottom:22 }}>
        {cards}
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:16,marginBottom:22 }}>
        <Card style={{ padding:22 }}>
          <div style={{ marginBottom:14 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Deposits by Status</h3>
            <p style={{ margin:0,fontSize:11,color:T.textMuted }}>Live counts from your deposits</p>
          </div>
          <StatusChart data={depositGraph}/>
        </Card>
        <Card style={{ padding:22 }}>
          <div style={{ marginBottom:14 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Withdrawals</h3>
            <p style={{ margin:0,fontSize:11,color:T.textMuted }}>Submitted vs completed</p>
          </div>
          <StatusChart data={withdrawalGraph}/>
        </Card>
      </div>
      <div style={{ marginBottom:22 }}>
        <Card style={{ padding:22 }}>
          <div style={{ padding:'2px 0 14px' }}><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Pending Requests</h3></div>
          {loading ? <LoadingScreen label="Loading requests…"/> : <TxTable txns={inFlight.slice(0,6)} viewerRole="MERCHANT"/>}
        </Card>
      </div>
    </div>
  );
};

// ─── Deposit form (used inside the Request modal) ──────────────────────────────
export const DepositForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',depositType:'UPI',memberName:'',memberId:'',segment:'A',profile:'NEW',notes:'' });
  const [bank, setBank] = useState<BankForm>(emptyBank);
  const [saveNew, setSaveNew] = useState(true);
  const [riskAnalysis, setRiskAnalysis] = useState(false);
  const [loading, setLoading] = useState(false);
  const [senderUpi, setSenderUpi] = useState('');
  const isUpi = form.depositType === 'UPI';
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  // Membership IDs are uppercase letters + digits only (auto-converted, lowercase blocked).
  const setMemberId = (raw: string) => setForm(f => ({ ...f, memberId: raw.toUpperCase().replace(/[^A-Z0-9]/g, '') }));

  // Auto-fill from the latest record for this Membership ID (member name, sender UPI; bank is
  // handled by BankAccountFields). A known ID's name is authoritative (one name per ID).
  useEffect(() => {
    const mid = form.memberId.trim();
    if (mid.length < 3) return;
    let alive = true;
    const t = setTimeout(() => {
      transactionAPI.memberProfile(mid).then(p => {
        if (!alive) return;
        if (p.memberName) setForm(f => ({ ...f, memberName: p.memberName as string }));
        if (p.upiId) setSenderUpi(p.upiId);
      }).catch(()=>{});
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [form.memberId]);

  const submit = async () => {
    if(!form.amount||!form.memberName||!form.memberId){ showToast('Fill all required fields','error'); return; }
    if(parseFloat(form.amount) < 1){ showToast('Amount must be greater than 0.','error'); return; }
    if(isUpi && !senderUpi.includes('@')){ showToast('Enter a valid Sender UPI ID (name@bank)','error'); return; }
    if(!bank.accountHolder||!bank.accountNumber){ showToast('Select or add a bank account','error'); return; }
    setLoading(true);
    try {
      await transactionAPI.createDeposit({
        ...form, amount: parseFloat(form.amount), riskAnalysis,
        accountHolder:bank.accountHolder, accountNumber:bank.accountNumber, ifsc:bank.ifsc, branch:bank.branch, bankName:bank.bankName,
        saveBankAccount: saveNew,
        ...(isUpi ? { senderUpiId: senderUpi.trim() } : {}),
      });
      showToast('Deposit request submitted — awaiting agent review');
      onSubmitted?.();
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to submit deposit request','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Sel label="Deposit Type" value={form.depositType} onChange={e=>set('depositType',e.target.value)} options={['UPI','IMPS','NEFT','RTGS','CASH'].map(v=>({value:v,label:v}))} required/>
        <Input label="Amount (INR)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="Min 1" required/>
        <Input label="Member Name" value={form.memberName} onChange={e=>set('memberName',e.target.value)} placeholder="Full name" required/>
        <Input label="Membership ID" value={form.memberId} onChange={e=>setMemberId(e.target.value)} placeholder="e.g. MBR20240001" required/>
        <Sel label="Segment" value={form.segment} onChange={e=>set('segment',e.target.value)} options={['A','B','C','D'].map(v=>({value:v,label:`Segment ${v}`}))}/>
        <Sel label="Profile" value={form.profile} onChange={e=>set('profile',e.target.value)} options={[{value:'OLD',label:'OLD'},{value:'NEW',label:'NEW'}]}/>
      </div>
      {isUpi
        ? <div style={{ background:T.canvas,borderRadius:12,padding:'12px 14px',margin:'4px 0 14px' }}>
            <p style={{ fontSize:11,fontWeight:800,color:T.textMain,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 8px' }}>Sender Account Details</p>
            <Input label="UPI ID" value={senderUpi} onChange={e=>setSenderUpi(e.target.value)} placeholder="e.g. satish@ybl" required
              hint="The UPI the payment is sent from — saved to this Membership ID for future withdrawals" />
            <BankAccountFields memberId={form.memberId} bank={bank} onBank={setBank} saveNew={saveNew} onSaveNew={setSaveNew}/>
          </div>
        : <BankAccountFields memberId={form.memberId} bank={bank} onBank={setBank} saveNew={saveNew} onSaveNew={setSaveNew}/>}
      <div style={{ marginBottom:14 }}>
        <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Note to Agent (optional)</label>
        <textarea value={form.notes} onChange={e=>set('notes',e.target.value)} placeholder="Any message for the agent reviewing this request"
          style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:60 }}/>
      </div>
      <label style={{ display:'flex',alignItems:'center',gap:8,fontSize:13,color:T.textMain,marginBottom:16,cursor:'pointer' }}>
        <input type="checkbox" checked={riskAnalysis} onChange={e=>setRiskAnalysis(e.target.checked)}/> Perform Risk Analysis
      </label>
      <Btn size="lg" full onClick={submit} disabled={loading||!form.amount||!form.memberName}>{loading?'Submitting...':'Submit Deposit Request →'}</Btn>
    </div>
  );
};

// ─── Withdrawal form (payout-mode driven) ──────────────────────────────────────
const PAYOUT_MODES = [
  { value:'BANK', label:'Bank Transfer' },
  { value:'UPI', label:'UPI' },
  { value:'CASH', label:'Cash' },
  { value:'CRYPTO', label:'Crypto (USDT)' },
];
// Mode-specific input fields the merchant fills. The agent uploads the proof/UTR/Hash afterward.
const MODE_FIELDS: Record<string, { key:string; label:string; digits?: boolean; upper?: boolean; max?: number }[]> = {
  BANK: [{key:'accountHolder',label:'Account Holder'},{key:'accountNumber',label:'Account Number'},{key:'ifsc',label:'IFSC Code',upper:true}],
  UPI: [{key:'upiId',label:'UPI ID'}],
  CASH: [{key:'village',label:'Village'},{key:'city',label:'City'},{key:'mobile',label:'Mobile Number',digits:true},{key:'pinCode',label:'PIN Code',digits:true,max:6}],
  CRYPTO: [{key:'walletAddress',label:'Wallet Address'},{key:'network',label:'Network (e.g. TRC20)'}],
};

export const WithdrawalForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ onSubmitted }) => {
  const { showToast } = useToast();
  const [amount, setAmount] = useState('');
  const [memberId, setMemberId] = useState('');
  const [mode, setMode] = useState('BANK');
  const [details, setDetails] = useState<Record<string,string>>({});
  const [available, setAvailable] = useState(0);
  const [maxWithdrawable, setMaxWithdrawable] = useState(0);
  const [rb, setRb] = useState(0);
  const [loading, setLoading] = useState(false);
  const [savedBanks, setSavedBanks] = useState<MerchantBankAccount[]>([]);
  const [savedUpis, setSavedUpis] = useState<MerchantBankAccount[]>([]);
  const [destId, setDestId] = useState('');   // '' = none chosen, 'OTHER' = manual entry

  // Pick a saved destination (UPI or bank) → drives payout mode + details.
  const applyDest = (kind: 'UPI' | 'BANK', row: MerchantBankAccount) => {
    if (kind === 'UPI') { setDestId(`upi-${row.id}`); setMode('UPI'); setDetails({ upiId: row.upiId || '' }); }
    else { setDestId(`bank-${row.id}`); setMode('BANK'); setDetails({ accountHolder: row.accountHolder || '', accountNumber: row.accountNumber || '', ifsc: row.ifsc || '', bank: row.bankName || '', branch: row.branch || '' }); }
  };

  useEffect(() => { transactionAPI.summary().then(s => { setAvailable(s.available); setRb(s.runningBalance || 0); setMaxWithdrawable(s.maxWithdrawable ?? s.available); }).catch(()=>{}); }, []);

  // Load this member's saved withdrawal destinations (bank accounts + UPIs), member-scoped.
  useEffect(() => {
    const mid = memberId.trim();
    if (!mid) { setSavedBanks([]); setSavedUpis([]); setDestId(''); setDetails({}); return; }
    let alive = true;
    bankAccountAPI.listMine(mid).then(rows => {
      if (!alive) return;
      const banks = rows.filter(r => r.accountNumber);
      const upis = rows.filter(r => r.upiId);
      setSavedBanks(banks);
      setSavedUpis(upis);
      // If exactly one saved destination exists, auto-select it; otherwise let the user choose.
      if (banks.length + upis.length === 1) {
        if (upis.length === 1) applyDest('UPI', upis[0]); else applyDest('BANK', banks[0]);
      } else {
        setDestId(''); setDetails({});
      }
    }).catch(()=>{});
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memberId]);

  // Saved destinations the member can withdraw to (UPI IDs + bank accounts), built into radio options.
  const savedDests = [
    ...savedUpis.map(u => ({ id:`upi-${u.id}`, kind:'UPI' as const, label:`UPI · ${u.upiId}${u.isDefault?'  ★ default':''}`, row:u })),
    ...savedBanks.map(b => ({ id:`bank-${b.id}`, kind:'BANK' as const, label:`Bank · ${b.bankName || 'Account'} ····${(b.accountNumber || '').slice(-4)}`, row:b })),
  ];
  const hasSaved = savedDests.length > 0;
  const usingOther = destId === 'OTHER' || !hasSaved;
  const chosenSaved = savedDests.find(d => d.id === destId);
  const fields = MODE_FIELDS[mode];
  const submit = async () => {
    if(!amount||!memberId){ showToast('Enter amount and Membership ID','error'); return; }
    if(parseFloat(amount) < 1){ showToast('Amount must be greater than 0.','error'); return; }
    if(parseFloat(amount) > maxWithdrawable + 0.01){ showToast('Insufficient Balance','error'); return; }
    if(hasSaved && !destId){ showToast('Select a withdrawal destination','error'); return; }
    const missing = fields.filter(f => !(details[f.key]||'').trim());
    if(missing.length){ showToast(`Fill: ${missing.map(m=>m.label).join(', ')}`,'error'); return; }
    setLoading(true);
    try {
      const payload: Record<string, unknown> = { amount: parseFloat(amount), memberId, payoutMode: mode, payoutDetails: details };
      if (mode === 'BANK') { payload.accountHolder = details.accountHolder; payload.accountNumber = details.accountNumber; payload.ifsc = details.ifsc; }
      await transactionAPI.createWithdrawal(payload);
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
      <div style={{ background:T.infoBg,borderRadius:12,padding:'14px 16px',marginBottom:16 }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 2px',textTransform:'uppercase',letterSpacing:'0.06em',fontWeight:800 }}>Available Withdrawal Balance</p>
        <p style={{ fontSize:26,fontWeight:800,color:T.blue,margin:0 }}>{fmt(available)}</p>
        <p style={{ fontSize:11,color:T.textMuted,margin:'6px 0 0' }}>
          Max you can withdraw after fees: <b>{fmt(maxWithdrawable)}</b>{rb > 0 ? ` · Reserved (pending): ${fmt(rb)}` : ''}
        </p>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Amount (INR)" type="number" value={amount} onChange={e=>setAmount(e.target.value)} placeholder="Min 1" required/>
        <Input label="Membership ID" value={memberId} onChange={e=>setMemberId(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,''))} placeholder="Alphanumeric (A-Z, 0-9)" required/>
      </div>

      {hasSaved && (
        <div style={{ marginBottom:12 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'4px 0 8px' }}>Withdrawal Destination</p>
          {savedDests.map(d => (
            <label key={d.id} style={{ display:'flex',alignItems:'center',gap:10,padding:'9px 12px',border:`1.5px solid ${destId===d.id?T.blue:T.border}`,borderRadius:10,marginBottom:8,cursor:'pointer',fontSize:13,color:T.textMain,fontWeight:600 }}>
              <input type="radio" name="wd-dest" checked={destId===d.id} onChange={()=>applyDest(d.kind, d.row)} />
              {d.label}
            </label>
          ))}
          <label style={{ display:'flex',alignItems:'center',gap:10,padding:'9px 12px',border:`1.5px solid ${destId==='OTHER'?T.blue:T.border}`,borderRadius:10,cursor:'pointer',fontSize:13,color:T.textMain,fontWeight:600 }}>
            <input type="radio" name="wd-dest" checked={destId==='OTHER'} onChange={()=>{ setDestId('OTHER'); setMode('BANK'); setDetails({}); }} />
            Other / new method
          </label>
        </div>
      )}

      {chosenSaved && (
        <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12,marginBottom:12 }}>
          {chosenSaved.kind === 'UPI'
            ? <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>UPI ID</span><b>{details.upiId}</b></div>
            : <>
                <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Account Holder</span><b>{details.accountHolder}</b></div>
                <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Account Number</span><b>{details.accountNumber}</b></div>
                <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>IFSC</span><b>{details.ifsc}</b></div>
                {details.bank && <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Bank</span><b>{details.bank}</b></div>}
                {details.branch && <div style={{ display:'flex',justifyContent:'space-between',padding:'2px 0' }}><span style={{ color:T.textMuted }}>Branch</span><b>{details.branch}</b></div>}
              </>}
        </div>
      )}

      {usingOther && (
        <>
          <Sel label="Payout Mode" value={mode} onChange={e=>{ setMode(e.target.value); setDetails({}); }} options={PAYOUT_MODES} required/>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'4px 0 8px' }}>{PAYOUT_MODES.find(m=>m.value===mode)?.label} Details</p>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            {fields.map(f => (
              <Input key={f.key} label={f.label} value={details[f.key]||''} required
                hint={f.key==='ifsc' ? 'Auto-fills bank & branch' : undefined}
                onChange={async e=>{
                  let v = e.target.value;
                  if (f.upper) v = v.toUpperCase();
                  if (f.digits) v = v.replace(/[^\d]/g,'').slice(0, f.max || 10);
                  setDetails(d => ({...d,[f.key]:v}));
                  if (f.key==='ifsc' && isValidIfsc(v)) {
                    const info = await lookupIfsc(v);
                    if (info) setDetails(d => ({...d, ifsc:v, bank:info.bank, branch:info.branch}));
                  }
                }}/>
            ))}
          </div>
          {mode==='BANK' && details.bank && (() => { const b = bankBadge(details.bank); return (
            <div style={{ display:'flex',alignItems:'center',gap:8,margin:'0 0 12px',fontSize:12,color:T.textMain }}>
              <span style={{ width:20,height:20,borderRadius:5,background:b.color,color:'#fff',display:'inline-flex',alignItems:'center',justifyContent:'center',fontSize:9,fontWeight:800 }}>{b.initials}</span>
              <b>{details.bank}</b>{details.branch ? <span style={{ color:T.textMuted }}>· {details.branch}</span> : null}
            </div>); })()}
        </>
      )}
      <div style={{ background:T.canvas,borderRadius:10,padding:'8px 12px',margin:'2px 0 16px',fontSize:11,color:T.textMuted }}>
        No proof needed now — after payment, the agent uploads the proof ({mode==='CRYPTO' ? 'Transaction Hash' : mode==='CASH' ? 'a proof image' : 'UTR number + transaction slip'}), which you can then view.
      </div>
      <Btn size="lg" full variant="danger" style={{ background:T.danger,color:'#fff' }} onClick={submit} disabled={loading||!amount||!memberId}>
        {loading?'Submitting...':'Submit Withdrawal Request →'}
      </Btn>
    </div>
  );
};

// ─── Settlement form ───────────────────────────────────────────────────────────
export const SettlementForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'', memberId:'' });
  const [proofs, setProofs] = useState<string[]>([]);
  const [available, setAvailable] = useState(0);
  const [maxSettleable, setMaxSettleable] = useState(0);
  const [rb, setRb] = useState(0);
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { transactionAPI.summary().then(s => { setAvailable(s.available); setRb(s.runningBalance || 0); setMaxSettleable(s.maxSettleable ?? s.available); }).catch(()=>{}); }, []);

  const submit = async () => {
    if(!form.amount){ showToast('Enter an amount','error'); return; }
    if(parseFloat(form.amount) > maxSettleable + 0.01){ showToast('We cannot process this request. The requested amount exceeds your available balance.','error'); return; }
    setLoading(true);
    try {
      await transactionAPI.createSettlement({ amount: parseFloat(form.amount), memberId: form.memberId || undefined, proofs });
      showToast('Settlement request submitted');
      onSubmitted?.();
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to submit settlement','error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div style={{ background:T.grad3,borderRadius:14,padding:20,marginBottom:18,textAlign:'center' }}>
        <p style={{ fontSize:11,color:'rgba(255,255,255,0.6)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Available Balance</p>
        <p style={{ fontSize:30,fontWeight:800,color:'#fff',margin:0 }}>{fmt(available)}</p>
        <p style={{ fontSize:11,color:'rgba(255,255,255,0.75)',margin:'8px 0 0' }}>
          Max you can settle after fees: <b>{fmt(maxSettleable)}</b>{rb > 0 ? ` · Reserved (pending): ${fmt(rb)}` : ''}
        </p>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Settlement Amount (INR)" type="number" value={form.amount} onChange={e=>set('amount',e.target.value)} placeholder="Enter amount" required/>
        <Input label="Membership ID" value={form.memberId} onChange={e=>set('memberId',e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,''))} placeholder="e.g. MBR20240001"/>
      </div>
      <MultiProofUpload values={proofs} onChange={setProofs}/>
      <Btn size="lg" full onClick={submit} disabled={loading||!form.amount}>{loading?'Submitting...':'Submit Settlement Request →'}</Btn>
    </div>
  );
};

// ─── Generic management page (history grouped by Membership ID) ─────────────────────
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

  // Group by Membership ID, most requests first.
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
          <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{noun} history grouped by Membership ID</p>
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
                  {['Membership ID',`Total ${noun} Requests`,'Status','Total Amount','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.length === 0 && <tr><td colSpan={5} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No {noun.toLowerCase()} requests yet</td></tr>}
                {groups.map((g,i)=>(
                  <tr key={g.key} style={{ background:i%2===0?T.surface:'#f8faff',cursor:'pointer' }} onClick={()=>setOpenMember(g.key)}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{g.key}</td>
                    <td style={{ padding:'11px 14px',fontWeight:800,color:T.blue }}>{g.items.length}</td>
                    <td style={{ padding:'11px 14px' }}><Badge status={g.items[0].status} type={g.items[0].type} viewerRole="MERCHANT"/></td>
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
        <Modal title={`Member ${active.key} — ${noun} History`} onClose={()=>setOpenMember(null)} xl>
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
  <ManagementPage user={user} title="Deposit Management" prefix="DEPOSIT" requestLabel="Deposit Request" noun="Deposit" FormComp={DepositForm}/>;

export const WithdrawalManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Withdrawal Management" prefix="WITHDRAWAL" requestLabel="Withdrawal Request" noun="Withdrawal" FormComp={WithdrawalForm}/>;

export const SettlementManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Settlement Management" prefix="SETTLEMENT" requestLabel="Settlement Request" noun="Settlement" FormComp={SettlementForm}/>;

// ─── Balance Page ─────────────────────────────────────────────────────────────
export const BalancePage: React.FC<{ user: User }> = ({ user }) => {
  const [s, setS] = useState<BalanceSummary | null>(null);
  const reload = () => transactionAPI.summary().then(setS).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(reload);

  const totalDeposit = s?.totalDeposit ?? 0;
  const payInFees = s?.payInFees ?? 0;
  const totalSettled = s?.totalSettled ?? 0;
  const settlementFees = s?.settlementFees ?? 0;
  const totalWithdrawn = s?.totalWithdrawn ?? 0;
  const payOutFees = s?.payOutFees ?? 0;
  const netAvailableBalance = totalDeposit - payInFees - totalSettled - settlementFees; // before withdrawals
  const available = s?.available ?? 0;                                 // spendable (after withdrawals)

  const rows: Array<[string, number, string, boolean]> = [
    ['Total Deposit Amount', totalDeposit, T.success, false],
    ['Pay-In Fees', payInFees, T.danger, false],
    ['Total Settled Amount', totalSettled, T.warning, false],
    ['Settlement Fees', settlementFees, T.danger, false],
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
                  {['Reference Number','Type','Amount','Membership ID','Status','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cancellable.length === 0 && <tr><td colSpan={6} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No cancellable requests</td></tr>}
                {cancellable.map((t,i)=>(
                  <tr key={t.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{t.ref}</td>
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
  const [slipTx, setSlipTx] = useState<Transaction | null>(null);

  const reload = () => {
    const fn = user.role === 'MERCHANT' ? transactionAPI.getMine : transactionAPI.getAll;
    return fn().then(setTxns).catch(()=>setTxns([]));
  };
  useEffect(() => { reload().finally(()=>setLoading(false)); }, [user.role]);
  usePoll(() => { if (!slipTx) reload(); });

  const filtered = txns.filter(t => {
    const ms = !search || t.ref.toLowerCase().includes(search.toLowerCase()) || t.merchant.toLowerCase().includes(search.toLowerCase());
    return ms && (type === 'ALL' || t.type === type) && (status === 'ALL' || t.status === status);
  });

  return (
    <>
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
          <TxExportButton txns={filtered} title="Transaction Ledger" />
        </div>
      </div>
      <TxTable loading={loading} txns={filtered} viewerRole={user.role} actionMode={user.role==='MERCHANT'?'merchant':'view'} onAction={(t)=>setSlipTx(t)}/>
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
    </Card>
    {slipTx && (
      <MerchantSlipModal tx={slipTx} onClose={()=>setSlipTx(null)} onSubmitted={()=>{ setSlipTx(null); reload(); }}/>
    )}
    </>
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
  const [avatar, setAvatar] = useState<string | null>(user.avatar || null);
  const [saving, setSaving] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const onAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { showToast('Image must be under 2 MB', 'error'); return; }
    setAvatar(await fileToDataUrl(f));
  };

  const openEdit = () => { setForm({ email:user.email, current:'', next:'', confirm:'' }); setAvatar(user.avatar || null); setEdit(true); };

  const save = async () => {
    if(form.next && form.next !== form.confirm){ showToast('Passwords do not match','error'); return; }
    setSaving(true);
    try {
      const avatarChanged = avatar !== (user.avatar || null);
      const updated = await userAPI.updateProfile({
        email: form.email !== user.email ? form.email : undefined,
        new_password: form.next || undefined,
        current_password: form.current || undefined,
        avatar: avatarChanged ? (avatar || '') : undefined,
      });
      updateUser({ email: updated.email, avatar: updated.avatar });
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
    details.splice(1, 0, ['Merchant ID', user.merchantCode || '—']);
    details.push(['Access Role', merchantRoleLabel(user.merchantRole) || '—'],['Pay-In Code', user.payIn||'—'],['Pay-Out Code', user.payOut||'—'],['Settlement Code', user.settlement||'—'],['Profile Type', user.profile||'—']);
  }

  return (
    <div style={{ maxWidth:560,margin:'0 auto',position:'relative' }}>
      <Card style={{ padding:'30px 28px' }}>
        <div style={{ position:'absolute',top:18,right:18 }}>
          <Btn size="sm" variant="ghost" onClick={openEdit}>✎ Edit</Btn>
        </div>
        <div style={{ display:'flex',flexDirection:'column',alignItems:'center',textAlign:'center',marginBottom:24 }}>
          {user.avatar
            ? <img src={user.avatar} alt={user.name} style={{ width:78,height:78,borderRadius:'50%',objectFit:'cover',border:`2px solid ${T.border}`,marginBottom:14 }}/>
            : <div style={{ width:78,height:78,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:32,fontWeight:800,color:'#fff',marginBottom:14 }}>{user.name.charAt(0)}</div>}
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
          {/* Profile picture */}
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Profile Picture</p>
          <div style={{ display:'flex',alignItems:'center',gap:14,marginBottom:16 }}>
            {avatar
              ? <img src={avatar} alt="avatar" style={{ width:64,height:64,borderRadius:'50%',objectFit:'cover',border:`1px solid ${T.border}` }}/>
              : <div style={{ width:64,height:64,borderRadius:'50%',background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:26,fontWeight:800,color:'#fff' }}>{user.name.charAt(0)}</div>}
            <div style={{ display:'flex',flexDirection:'column',gap:6 }}>
              <label style={{ cursor:'pointer' }}>
                <span style={{ display:'inline-block',padding:'6px 12px',borderRadius:8,border:`1.5px solid ${T.blue}`,color:T.blue,fontSize:12,fontWeight:700 }}>Upload Office Image</span>
                <input type="file" accept="image/*" onChange={onAvatar} style={{ display:'none' }}/>
              </label>
              {avatar && <span onClick={()=>setAvatar(null)} style={{ fontSize:11,color:T.danger,cursor:'pointer',fontWeight:700 }}>Remove</span>}
            </div>
          </div>
          <Input label="Email ID" type="email" value={form.email} onChange={e=>set('email',e.target.value)} placeholder="you@company.com"/>
          <div style={{ borderTop:`1px solid ${T.border}`,margin:'4px 0 14px',paddingTop:14 }}>
            <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Change Password</p>
            <Input label="Current Password" type="password" value={form.current} onChange={e=>set('current',e.target.value)} placeholder="Required to change password"/>
            <Input label="New Password" type="password" value={form.next} onChange={e=>set('next',e.target.value)} placeholder="Leave blank to keep current"/>
            <Input label="Confirm New Password" type="password" value={form.confirm} onChange={e=>set('confirm',e.target.value)} placeholder="Re-enter new password"/>
            {form.next && <p style={{ fontSize:11,color:T.textMuted,margin:'-6px 0 0' }}>At least 8 characters with an uppercase letter, a lowercase letter, a number and a special character.</p>}
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

// ─── News & Updates (merchant feed — live from the News API) ────────────────────
export const SECTION_COLOR: Record<string, string> = {
  'Announcements': T.blue, 'Product Updates': T.success, 'Offers': T.warning, 'Alerts': T.danger,
};

export const NewsPage: React.FC<{ user: User }> = () => {
  const [posts, setPosts] = useState<NewsPost[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => { newsAPI.list().then(setPosts).catch(()=>setPosts([])).finally(()=>setLoading(false)); }, []);
  usePoll(() => { newsAPI.list().then(setPosts).catch(()=>{}); });

  if (loading) return <LoadingScreen label="Loading news…"/>;

  return (
    <div style={{ maxWidth:820 }}>
      <div style={{ marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>News &amp; Updates</h2>
        <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Platform announcements and product updates</p>
      </div>

      {posts.length === 0 ? (
        <Card style={{ padding:40,textAlign:'center' }}>
          <div style={{ fontSize:40,marginBottom:10 }}>📰</div>
          <p style={{ fontWeight:800,color:T.textMain,margin:'0 0 4px' }}>No news yet</p>
          <p style={{ fontSize:12,color:T.textMuted,margin:0 }}>Announcements and updates will appear here.</p>
        </Card>
      ) : (
        <div style={{ display:'flex',flexDirection:'column',gap:14 }}>
          {posts.map(p => { const c = SECTION_COLOR[p.section] || T.blue; return (
            <Card key={p.id} style={{ padding:20 }}>
              <div style={{ display:'flex',alignItems:'center',gap:10,marginBottom:8 }}>
                <span style={{ padding:'2px 10px',borderRadius:20,fontSize:11,fontWeight:700,background:`${c}18`,color:c }}>{p.section}</span>
                <span style={{ fontSize:11,color:T.textMuted }}>{formatDateTime(p.createdAt)} · {p.author}</span>
              </div>
              <h3 style={{ margin:'0 0 6px',fontSize:15,fontWeight:800,color:T.textMain }}>{p.title}</h3>
              {p.image && <img src={p.image} alt="" style={{ display:'block',maxWidth:'100%',maxHeight:260,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,margin:'0 0 10px',background:T.canvas }} />}
              <p style={{ margin:0,fontSize:13,color:T.textMuted,lineHeight:1.6,whiteSpace:'pre-line' }}>{p.body}</p>
            </Card>
          ); })}
          <p style={{ textAlign:'center',fontSize:11,color:T.textMuted,margin:'4px 0' }}>You're all caught up.</p>
        </div>
      )}
    </div>
  );
};
