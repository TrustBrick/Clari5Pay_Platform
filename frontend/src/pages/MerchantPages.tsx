import React, { useState, useEffect, useRef } from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, depositTypeLabel, depositDetailLabel, memberLabel, DEPOSIT_TYPE_OPTIONS, fileToDataUrl, downloadDataUrl, downloadText, merchantRoleLabel, nameWithRole, clientApproverLabel, isInternalRole, clientRemarkActor, clientAuditActor, formatDate, formatDateTime, formatIndianAmountInput, parseIndianAmount, chatTime, chatDateLabel, formatBytes, isChatImage, chatAttachmentError, readChatAttachment, openDataUrl, CHAT_ACCEPT } from '../utils/helpers';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, StatusChart, LoadingScreen, Modal, Badge, BankNamesDatalist, CountUp, Skeleton, ReasonModal } from '../components/UI';
import { Icon } from '../components/Icon';
import { fireConfetti } from '../utils/confetti';
import TxTable from '../components/TxTable';
import { TxExportButton } from '../components/TxExport';
import TxSearchFilters from '../components/TxSearchFilters';
import { IS_DEMO, SEND_TO_APPROVAL_ENABLED } from '../utils/portal';
import { AgentAssignmentPanel } from './AgentPages';
import { transactionAPI, supportAPI, supportWsUrl, userAPI, bankAccountAPI, newsAPI } from '../services/api';
import type { TxQuery } from '../services/api';
import { usePoll } from '../utils/usePoll';
import { useToast } from '../context/ToastContext';
import { useAuth } from '../context/AuthContext';
import { lookupIfsc, isValidIfsc, bankBadge, BANK_NAMES } from '../utils/ifsc';
import type { Transaction, User, SupportMessage, BalanceSummary, MerchantBankAccount, NewsPost, AuditLogEntry } from '../types';

// The Reports module lives in its own file; re-exported here so App.tsx imports stay grouped.
export { ReportsPage } from './ReportsPage';

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

// ─── Standardized receipt / slip / proof image container ────────────────────────
// ONE shared container for every payment receipt / slip / proof image in every Request
// Details / review modal across all portals. The layout — size, padding, radius, margin,
// background, border, centering and object-fit: contain scaling — is identical everywhere;
// only the theme colours change between Light and Dark (they come from T.*, which adapts).
// The image is always centered and shown in full (never cropped or stretched); a PDF shows
// a download tile in the same box; with no source it shows a placeholder in the same box.
const RECEIPT_BOX: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  width: '100%', height: 240, padding: 12, marginTop: 8, boxSizing: 'border-box',
  borderRadius: 12, border: `1px solid ${T.border}`, background: T.canvas, overflow: 'hidden',
};
export const ReceiptImage: React.FC<{ src?: string | null; alt?: string }> = ({ src, alt = 'Receipt' }) => {
  const isPdf = !!src && src.startsWith('data:application/pdf');
  return (
    <div style={RECEIPT_BOX}>
      {!src
        ? <span style={{ fontSize: 12, color: T.textMuted }}>No image uploaded</span>
        : isPdf
          ? <a href={src} download={`${alt}.pdf`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, fontSize: 15, fontWeight: 800, color: T.danger, textDecoration: 'none' }}>PDF<span style={{ fontSize: 10, color: T.textMuted }}>Open / Download <Icon name="download" size={10} /></span></a>
          : <img src={src} alt={alt} style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', display: 'block', margin: '0 auto' }} />}
    </div>
  );
};

// Read-only viewer for one or more submitted proofs/slips/receipts — each rendered in the
// shared ReceiptImage container, so every image looks identical across the application.
export const ProofGallery: React.FC<{ srcs: string[] }> = ({ srcs }) => (
  <div>
    {srcs.map((src, i) => <ReceiptImage key={i} src={src} alt={`proof-${i + 1}`} />)}
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
  // "Send To Approval" (demo): once the slip proof uploads, the merchant chooses the Authorized
  // Approver who should review this deposit — revealed here rather than on the create form.
  const [approverId, setApproverId] = useState('');
  const [approvers, setApprovers] = useState<ApproverOption[]>([]);
  useEffect(() => { if (SEND_TO_APPROVAL_ENABLED) transactionAPI.approvers().then(setApprovers).catch(()=>{}); }, []);
  // Proof/receipt images are omitted from list payloads; fetch them when the modal opens.
  const [imgs, setImgs] = useState<{ adminProof?: string | null; adminBankImage?: string | null; merchantProof?: string | null; merchantProofs?: string[] | null }>({ adminProof: tx.adminProof, adminBankImage: tx.adminBankImage, merchantProof: tx.merchantProof, merchantProofs: tx.merchantProofs });
  useEffect(() => {
    transactionAPI.getDetail(tx.id).then(d => setImgs({ adminProof: d.adminProof, adminBankImage: d.adminBankImage, merchantProof: d.merchantProof, merchantProofs: d.merchantProofs })).catch(()=>{});
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

  // Both the UTR number and at least one payment proof are mandatory when submitting a slip; in demo
  // the Authorized Approver (revealed after the proof uploads) is required too.
  const canSubmit = proofs.length > 0 && !!ref.trim() && (!SEND_TO_APPROVAL_ENABLED || !!approverId);
  // Slip submission applies to deposits awaiting the merchant's payment proof, or those a
  // Supervisor returned for resubmission (the Data Operator re-uploads the correct slip).
  const canSubmitSlip = tx.type.startsWith('DEPOSIT') && (tx.status === 'ACCOUNT_SUBMITTED' || tx.status === 'RESUBMITTED');
  const adminLabel = tx.type.startsWith('DEPOSIT') ? 'Payment Details from Agent' : 'Payment Receipt from Agent';

  const submit = async () => {
    if (!ref.trim()) { showToast('Enter the UTR number', 'error'); return; }
    if (!proofs.length) { showToast('Upload the payment proof', 'error'); return; }
    if (SEND_TO_APPROVAL_ENABLED && !approverId) { showToast('Select an Authorized Approver.', 'error'); return; }
    setLoading(true);
    try {
      await transactionAPI.submitSlip(tx.id, { merchantProofs: proofs, merchantRef: ref.trim() || undefined,
        ...(SEND_TO_APPROVAL_ENABLED && approverId ? { approverUserId: Number(approverId) } : {}) });
      fireConfetti();
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

  // A custom uploaded image overrides the auto-generated card for this transaction.
  const bankImageSrc = imgs.adminBankImage || null;
  const hasImage = !!(bankImageSrc || imgs.adminProof);
  const hasAdminDetails = !!(bankImageSrc || imgs.adminProof || tx.adminBankDetails || tx.hasAdminBankImage);
  // Security: bank-detail fields are NOT copyable/exportable as text — only the image (if any) can be saved.
  const downloadDetails = () => {
    const img = bankImageSrc || imgs.adminProof;
    if (img) downloadDataUrl(img, `bank-details-${tx.ref}.png`);
  };

  return (
    <Modal title={`${canSubmitSlip ? 'Pay & Submit Proof' : 'Request Details'} — ${tx.ref}`} onClose={onClose}>
      {tx.highRisk && (
        <div style={{ display:'flex',gap:10,alignItems:'flex-start',background:'#fdecea',border:'1px solid #f5b5ae',borderRadius:10,padding:'12px 14px',marginBottom:16 }}>
          <span style={{ fontSize:20,lineHeight:1 }}><Icon name="warning" size={20} /></span>
          <div>
            <p style={{ margin:0,fontSize:13,fontWeight:800,color:'#b71c1c' }}>High Risk — Member {tx.memberId || tx.ref}</p>
            <p style={{ margin:'2px 0 0',fontSize:12,color:'#7f1d1d' }}>{tx.rejectReason || 'Payment was not received in our bank for this member. Please contact support.'}</p>
          </div>
        </div>
      )}
      {tx.type.startsWith('DEPOSIT') && (tx.depositType || tx.depositDetails) && (
        <div style={{ marginBottom:16 }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Request Details</p>
          <div style={{ background:T.canvas,borderRadius:10,padding:12 }}>
            {tx.depositType && <SlipRow k="Deposit Type" v={depositTypeLabel(tx.depositType)} />}
            {tx.depositDetails && Object.entries(tx.depositDetails).map(([k,v]) => v ? <SlipRow key={k} k={depositDetailLabel(k)} v={String(v)} /> : null)}
          </div>
        </div>
      )}
      <div style={{ marginBottom:16 }}>
        <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>{adminLabel}</p>
        <div style={{ background:T.canvas,borderRadius:10,padding:12 }}>
          <SlipRow k="Amount" v={fmt(tx.amount)} />
          <SlipRow k="Status" v={<Badge status={tx.status} type={tx.type} viewerRole="MERCHANT" approverRole={tx.approverRole} />} />
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
              {/* Security: bank-detail fields cannot be copied/selected (UPI ID stays copyable above). */}
              <p onCopy={e=>e.preventDefault()} style={{ fontSize:13,color:T.textMain,margin:0,whiteSpace:'pre-line',lineHeight:1.6,userSelect:'none',WebkitUserSelect:'none',MozUserSelect:'none' }}>{tx.adminBankDetails}</p>
            </div>
          )}
          {!tx.adminUpiId && !tx.adminBankDetails && !imgs.adminProof && !bankImageSrc && !tx.hasAdminBankImage &&
            <p style={{ fontSize:12,color:T.textMuted,margin:0 }}>Awaiting updates from Agent.</p>}
        </div>

        {/* A custom uploaded image overrides the auto-generated card; otherwise show the auto card PNG. */}
        {bankImageSrc
          ? <ReceiptImage src={bankImageSrc} alt="Bank details" />
          : imgs.adminProof && <ReceiptImage src={imgs.adminProof} alt="Admin details" />}
        {hasImage && (
          <div style={{ marginTop:10 }}>
            <Btn size="sm" variant="ghost" onClick={downloadDetails}><Icon name="download" size={14} /> Download Bank Details Image</Btn>
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
          {/* Send To Approval — revealed only after the slip proof uploads; the merchant chooses who
              reviews this deposit, then it routes to that approver (Supervisor review). Demo only. */}
          {SEND_TO_APPROVAL_ENABLED && proofs.length > 0 && (
            <SendToApprovalCard noun="Deposit" approvers={approvers} value={approverId} onChange={setApproverId}
              className="animate-slide-up"
              subtitle={<>Proof uploaded successfully. Please choose the Authorized Approver who should review this request.</>} />
          )}
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
export const MerchantDashboard: React.FC<{ user: User; onNavigate?: (page: string) => void }> = ({ user, onNavigate }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [summary, setSummary] = useState<BalanceSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const go = (p: string) => onNavigate?.(p);

  // The dashboard renders only a 6-row preview of in-flight requests, so fetch just the recent
  // 50 (not the whole history). All cards/charts read their counts from the aggregated summary
  // (statusCounts), so they stay exact & live without pulling every row on each poll.
  const reload = () => Promise.all([transactionAPI.getMine({ limit: 50 }), transactionAPI.summary()])
    .then(([t, s]) => { setTxns(t); setSummary(s); })
    .catch(()=>{});
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  // Counts/charts come from the backend summary (SQL-aggregated over the full history).
  const IN_FLIGHT = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SLIP_SUBMITTED'];
  const sc = summary?.statusCounts;
  const scAt = (g: 'deposit' | 'withdrawal' | 'settlement', s: string) => sc?.[g]?.[s] ?? 0;
  const inFlightCount = (['deposit', 'withdrawal', 'settlement'] as const)
    .reduce((n, g) => n + IN_FLIGHT.reduce((m, s) => m + scAt(g, s), 0), 0);
  const settlementCount = summary?.settlementCount ?? 0;
  // Recent in-flight rows for the 6-row preview table (pending requests are the newest, so the
  // recent-50 window covers them; the accurate total is inFlightCount above).
  const inFlight = txns.filter(t => IN_FLIGHT.includes(t.status));
  const depositGraph = [
    { label: 'Requested', value: scAt('deposit', 'ACCOUNT_REQUESTED'), color: T.warning },
    { label: 'Submitted', value: scAt('deposit', 'ACCOUNT_SUBMITTED'), color: T.info },
    { label: 'Slip', value: scAt('deposit', 'SLIP_SUBMITTED'), color: T.blue },
    { label: 'Deposited', value: scAt('deposit', 'COMPLETED'), color: T.success },
  ];
  const withdrawalGraph = [
    { label: 'Submitted', value: scAt('withdrawal', 'ACCOUNT_REQUESTED'), color: T.warning },
    { label: 'Completed', value: scAt('withdrawal', 'COMPLETED'), color: T.success },
  ];

  // Role-scoped dashboard cards (count-up values; click jumps to the relevant page).
  const role = String(user.merchantRole || '').toUpperCase();
  const balLen = fmt(summary?.available ?? 0).length;
  const pendingCard = <StatCard icon="pending-requests" label="Pending Requests" value={<CountUp value={inFlightCount} />} sub="In progress" color={T.warning} onClick={()=>go('transactions')}/>;
  const balanceCard = <StatCard icon="available-balance" label="Available Balance" value={<CountUp value={summary?.available ?? 0} format={fmt} />} valueLen={balLen} sub="Updated now" color={T.success} onClick={()=>go('balance')}/>;
  let cards: React.ReactNode;
  if (role === 'DEPOSIT_OPERATOR') {
    cards = <><StatCard icon="deposit" label="No. of Deposits" value={<CountUp value={summary?.depositCount ?? 0} />} color={T.blue} onClick={()=>go('deposit')}/>{pendingCard}</>;
  } else if (role === 'WITHDRAWAL_OPERATOR') {
    cards = <>{balanceCard}<StatCard icon="withdrawal" label="No. of Withdrawals" value={<CountUp value={summary?.withdrawalCount ?? 0} />} color={T.danger} onClick={()=>go('withdrawal')}/>{pendingCard}</>;
  } else if (role === 'SUPERVISOR') {
    cards = <>{balanceCard}<StatCard icon="settlement" label="No. of Settlements" value={<CountUp value={settlementCount} />} color={T.info} onClick={()=>go('settlement')}/>{pendingCard}</>;
  } else if (role === 'MANAGER') {
    // Approval-only role: no direct Deposit/Withdrawal creation entry. Balance is view-only;
    // the withdrawals card opens the Approvals (review) queue, not a creation page.
    cards = <>{balanceCard}<StatCard icon="withdrawal" label="No. of Withdrawals" value={<CountUp value={summary?.withdrawalCount ?? 0} />} color={T.danger} onClick={()=>go('approvals')}/>{pendingCard}</>;
  } else {
    cards = <>
      {balanceCard}
      <StatCard icon="deposit" label="No. of Deposits" value={<CountUp value={summary?.depositCount ?? 0} />} color={T.blue} onClick={()=>go('deposit')}/>
      <StatCard icon="withdrawal" label="No. of Withdrawals" value={<CountUp value={summary?.withdrawalCount ?? 0} />} color={T.danger} onClick={()=>go('withdrawal')}/>
      {pendingCard}
    </>;
  }

  if (loading) return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:16,marginBottom:22 }}>
        {[0,1,2,3].map(i => <Card key={i} style={{ padding:18 }}><Skeleton w={90} h={11}/><div style={{height:12}}/><Skeleton w={130} h={26}/></Card>)}
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:16,marginBottom:22 }}>
        {[0,1].map(i => <Card key={i} style={{ padding:22 }}><Skeleton w={150} h={14}/><div style={{height:18}}/><Skeleton h={120}/></Card>)}
      </div>
      <Card style={{ padding:22 }}><Skeleton w={150} h={14}/><div style={{height:14}}/>{[0,1,2].map(i => <div key={i} style={{ padding:'7px 0' }}><Skeleton h={20}/></div>)}</Card>
    </div>
  );

  return (
    <div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(200px,1fr))',gap:16,marginBottom:22 }}>
        {cards}
      </div>
      {/* Deposit/Withdrawal "tap to manage" shortcuts — hidden for the approval-only Manager. */}
      {role !== 'MANAGER' && (
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:16,marginBottom:22 }}>
        <Card className="c5-hover-lift" onClick={()=>go('deposit')} style={{ padding:22,cursor:'pointer' }}>
          <div style={{ marginBottom:14 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Deposits by Status</h3>
            <p style={{ margin:0,fontSize:11,color:T.textMuted }}>Live counts from your deposits · tap to manage</p>
          </div>
          <StatusChart data={depositGraph}/>
        </Card>
        <Card className="c5-hover-lift" onClick={()=>go('withdrawal')} style={{ padding:22,cursor:'pointer' }}>
          <div style={{ marginBottom:14 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Withdrawals</h3>
            <p style={{ margin:0,fontSize:11,color:T.textMuted }}>Submitted vs completed · tap to manage</p>
          </div>
          <StatusChart data={withdrawalGraph}/>
        </Card>
      </div>
      )}
      <div style={{ marginBottom:22 }}>
        <Card style={{ padding:22 }}>
          <div style={{ padding:'2px 0 14px' }}><h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Pending Requests</h3></div>
          {loading ? <LoadingScreen label="Loading requests…"/> : <TxTable txns={inFlight.slice(0,6)} viewerRole="MERCHANT"/>}
        </Card>
      </div>
    </div>
  );
};

// "Send To Approval" — demo only. Identical card/copy/validation to the Agent Deposit/Withdrawal
// screens (see AgentTxnPages.tsx): every request is addressed to a chosen Supervisor/Manager
// approver before it enters the same review queue it always has.
type ApproverOption = { id: number; name: string; role: string };
const SendToApprovalCard: React.FC<{
  noun: string; approvers: ApproverOption[]; value: string; onChange: (v: string) => void;
  // Optional overrides: `subtitle` swaps the default helper copy (used by the proof-gated reveal);
  // `className` carries the house slide-up animation when the card appears after a proof upload.
  subtitle?: React.ReactNode; className?: string;
}> = ({ noun, approvers, value, onChange, subtitle, className }) => (
  <div className={className} style={{ marginTop: 16, padding: 14, borderRadius: 10, background: T.canvas, border: `1px solid ${T.border}` }}>
    <p style={{ margin: '0 0 4px', fontSize: 13, fontWeight: 700, color: T.textMain }}>Send To Approval</p>
    <p style={{ margin: '0 0 12px', fontSize: 11.5, color: T.textMuted }}>
      {subtitle ?? <>Every Merchant {noun} Request goes to an approver — choose who reviews this request.</>}
    </p>
    <div style={{ maxWidth: 360 }}>
      <Sel label="Authorized Approver" value={value} onChange={e => onChange(e.target.value)} required
        options={[{ value: '', label: '— Select approver —' }, ...approvers.map(a => ({ value: String(a.id), label: `${a.name} (${a.role})` }))]} />
    </div>
  </div>
);

// ─── Deposit form (used inside the Request modal) ──────────────────────────────
export const DepositForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ user, onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'',depositType:'UPI',memberName:'',memberId:'',segment:'A',profile:'NEW',notes:'' });
  const [bank, setBank] = useState<BankForm>(emptyBank);
  const [saveNew, setSaveNew] = useState(true);
  const [riskAnalysis, setRiskAnalysis] = useState(false);
  const [loading, setLoading] = useState(false);
  const [senderUpi, setSenderUpi] = useState('');
  const [memberLocked, setMemberLocked] = useState(false);  // Member Name auto-filled from an existing membership → read-only
  // Cash / Crypto member-supplied details + proof (no bank account on these types).
  const [details, setDetails] = useState<Record<string,string>>({ network:'TRC20' });
  const [proofs, setProofs] = useState<string[]>([]);
  // "Send To Approval" (demo only): chosen Authorized Approver + the business's Supervisors/Managers.
  const [approverId, setApproverId] = useState('');
  const [approvers, setApprovers] = useState<ApproverOption[]>([]);
  useEffect(() => { if (SEND_TO_APPROVAL_ENABLED) transactionAPI.approvers().then(setApprovers).catch(()=>{}); }, []);
  const isUpi = form.depositType === 'UPI';
  const isCash = form.depositType === 'CASH';
  const isCrypto = form.depositType === 'CRYPTO';
  const isBankLike = !isCash && !isCrypto;   // UPI / BANK / IMPS / NEFT / RTGS collect a bank account
  // "Send To Approval" appears on THIS form only for CASH/CRYPTO — the one deposit type whose proof
  // is uploaded here, so the approver is revealed once that proof uploads. UPI/bank deposits carry no
  // proof on this form: their approver is chosen later, at the Pay & Submit Proof step (MerchantSlipModal).
  const proofGated = isCash || isCrypto;
  const showApproval = SEND_TO_APPROVAL_ENABLED && proofGated && proofs.length > 0;
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  const setDetail = (k: string, v: string) => setDetails(d => ({ ...d, [k]: v }));
  // Membership IDs are uppercase letters + digits only (auto-converted, lowercase blocked).
  const setMemberId = (raw: string) => setForm(f => ({ ...f, memberId: raw.toUpperCase().replace(/[^A-Z0-9]/g, '') }));

  // Auto-fill from the latest record for this Membership ID (member name, sender UPI; bank is
  // handled by BankAccountFields). A known ID's name is authoritative (one name per ID).
  useEffect(() => {
    const mid = form.memberId.trim();
    if (mid.length < 3) { setMemberLocked(false); return; }
    let alive = true;
    const t = setTimeout(() => {
      transactionAPI.memberProfile(mid).then(p => {
        if (!alive) return;
        // Existing membership → auto-fill the name and lock it; new ID → allow manual entry.
        if (p.memberName) { setForm(f => ({ ...f, memberName: p.memberName as string })); setMemberLocked(true); }
        else setMemberLocked(false);
        if (p.upiId) setSenderUpi(p.upiId);
      }).catch(()=>{});
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [form.memberId]);

  const submit = async () => {
    if(!form.amount||!form.memberName||!form.memberId){ showToast('Fill all required fields','error'); return; }
    if(parseFloat(parseIndianAmount(form.amount)) < 1){ showToast('Amount must be greater than 0.','error'); return; }
    if(isUpi && !senderUpi.includes('@')){ showToast('Enter a valid Sender UPI ID (name@bank)','error'); return; }
    if(isBankLike && (!bank.accountHolder||!bank.accountNumber)){ showToast('Select or add a bank account','error'); return; }
    if(isCash && (!details.village||!details.city||!details.mobile)){ showToast('Enter Village, City and Mobile Number','error'); return; }
    if(isCash && !proofs.length){ showToast('Upload a proof / image of the cash deposit','error'); return; }
    if(isCrypto && (!details.walletAddress||!details.network||!details.txHash)){ showToast('Enter Wallet Address, Network and Transaction Hash','error'); return; }
    if(isCrypto && !proofs.length){ showToast('Upload a proof / screenshot of the transaction','error'); return; }
    // "Send To Approval" (demo only): mandatory for CASH/CRYPTO (approver chosen here, after the
    // proof). UPI/bank deposits choose their approver later at the Pay & Submit Proof step.
    if(showApproval && !approverId){ showToast('Select an Authorized Approver.','error'); return; }
    // Agent assignment is optional on a normal merchant deposit (the selector is labelled
    // "optional"); a mandatory agent belongs to the Agent Management module, not here.
    setLoading(true);
    try {
      const created = await transactionAPI.createDeposit({
        ...form, amount: parseFloat(parseIndianAmount(form.amount)), riskAnalysis,
        ...(showApproval && approverId ? { sentForApproval: true, approverUserId: Number(approverId) } : {}),
        ...(isBankLike ? {
          accountHolder:bank.accountHolder, accountNumber:bank.accountNumber, ifsc:bank.ifsc, branch:bank.branch, bankName:bank.bankName,
          saveBankAccount: saveNew,
        } : {}),
        ...(isUpi ? { senderUpiId: senderUpi.trim() } : {}),
        ...(isCash ? { depositDetails: { village: details.village, city: details.city, mobile: details.mobile }, proofs } : {}),
        ...(isCrypto ? { depositDetails: { walletAddress: details.walletAddress, network: details.network, txHash: details.txHash }, proofs } : {}),
      });
      fireConfetti();
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
        <Sel label="Deposit Type" value={form.depositType} onChange={e=>set('depositType',e.target.value)} options={DEPOSIT_TYPE_OPTIONS} required/>
        <Input label="Amount (INR)" type="text" inputMode="decimal" value={form.amount} onChange={e=>set('amount',formatIndianAmountInput(e.target.value))} placeholder="Min 1" required/>
        <Input label="Member Name" value={form.memberName} onChange={e=>set('memberName',e.target.value)} placeholder="Full name" required readOnly={memberLocked} hint={memberLocked ? 'Auto-filled from existing membership' : undefined}/>
        <Input label="Membership ID" value={form.memberId} onChange={e=>setMemberId(e.target.value)} placeholder="e.g. MBR20240001" required/>
        {isBankLike && <>
          <Sel label="Segment" value={form.segment} onChange={e=>set('segment',e.target.value)} options={['A','B','C','D'].map(v=>({value:v,label:`Segment ${v}`}))}/>
          <Sel label="Profile" value={form.profile} onChange={e=>set('profile',e.target.value)} options={[{value:'OLD',label:'OLD'},{value:'NEW',label:'NEW'}]}/>
        </>}
      </div>
      {isCash && (
        <div style={{ background:T.canvas,borderRadius:12,padding:'12px 14px',margin:'4px 0 14px' }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMain,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 8px' }}>Cash Deposit Details</p>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            <Input label="Village" value={details.village||''} onChange={e=>setDetail('village',e.target.value)} placeholder="Village" required/>
            <Input label="City" value={details.city||''} onChange={e=>setDetail('city',e.target.value)} placeholder="City" required/>
            <Input label="Mobile Number" value={details.mobile||''} onChange={e=>setDetail('mobile',e.target.value.replace(/[^0-9+]/g,''))} placeholder="Mobile number" required/>
          </div>
          <MultiProofUpload values={proofs} onChange={setProofs} label="Proof / Image Upload" required />
        </div>
      )}
      {isCrypto && (
        <div style={{ background:T.canvas,borderRadius:12,padding:'12px 14px',margin:'4px 0 14px' }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMain,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 8px' }}>Crypto (USDT) Details</p>
          <Input label="Wallet Address" value={details.walletAddress||''} onChange={e=>setDetail('walletAddress',e.target.value.trim())} placeholder="USDT wallet address" required/>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
            <Input label="Network" value={details.network||''} onChange={e=>setDetail('network',e.target.value)} placeholder="TRC20" required/>
            <Input label="Transaction Hash ID" value={details.txHash||''} onChange={e=>setDetail('txHash',e.target.value.trim())} placeholder="On-chain transaction hash" required/>
          </div>
          <MultiProofUpload values={proofs} onChange={setProofs} label="Proof / Screenshot Upload" required />
        </div>
      )}
      {isBankLike && (isUpi
        ? <div style={{ background:T.canvas,borderRadius:12,padding:'12px 14px',margin:'4px 0 14px' }}>
            <p style={{ fontSize:11,fontWeight:800,color:T.textMain,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 8px' }}>Sending Account Details</p>
            <Input label="UPI ID" value={senderUpi} onChange={e=>setSenderUpi(e.target.value)} placeholder="e.g. satish@ybl" required
              hint="The UPI the payment is sent from — saved to this Membership ID for future withdrawals" />
            <BankAccountFields memberId={form.memberId} bank={bank} onBank={setBank} saveNew={saveNew} onSaveNew={setSaveNew}/>
          </div>
        : <BankAccountFields memberId={form.memberId} bank={bank} onBank={setBank} saveNew={saveNew} onSaveNew={setSaveNew}/>)}
      <div style={{ marginBottom:14 }}>
        <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Note to Agent (optional)</label>
        <textarea value={form.notes} onChange={e=>set('notes',e.target.value)} placeholder="Any message for the agent reviewing this request"
          style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:60 }}/>
      </div>
      <label style={{ display:'flex',alignItems:'center',gap:8,fontSize:13,color:T.textMain,marginBottom:16,cursor:'pointer' }}>
        <input type="checkbox" checked={riskAnalysis} onChange={e=>setRiskAnalysis(e.target.checked)}/> Perform Risk Analysis
      </label>
      {showApproval && <SendToApprovalCard noun="Deposit" approvers={approvers} value={approverId} onChange={setApproverId}
        className={proofGated ? 'animate-slide-up' : undefined}
        subtitle={proofGated ? <>Your proof has been uploaded successfully. Please select the Authorized Approver before submitting your request.</> : undefined} />}
      <Btn size="lg" full onClick={submit} disabled={loading||!form.amount||!form.memberName} style={showApproval?{ marginTop:16 }:undefined}>{loading?'Submitting...':'Submit Deposit Request →'}</Btn>
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

export const WithdrawalForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ user, onSubmitted }) => {
  const { showToast } = useToast();
  const [amount, setAmount] = useState('');
  const [memberId, setMemberId] = useState('');
  const [memberName, setMemberName] = useState('');
  const [memberLocked, setMemberLocked] = useState(false);  // name auto-filled from an existing membership → read-only
  const [mode, setMode] = useState('BANK');
  const [details, setDetails] = useState<Record<string,string>>({});
  const [available, setAvailable] = useState(0);
  const [maxWithdrawable, setMaxWithdrawable] = useState(0);
  const [summaryLoaded, setSummaryLoaded] = useState(false);  // balance known → safe to validate against it
  const [rb, setRb] = useState(0);
  const [loading, setLoading] = useState(false);
  const [savedBanks, setSavedBanks] = useState<MerchantBankAccount[]>([]);
  const [savedUpis, setSavedUpis] = useState<MerchantBankAccount[]>([]);
  const [destId, setDestId] = useState('');   // '' = none chosen, 'OTHER' = manual entry
  // "Send To Approval" (demo only): chosen Authorized Approver + the business's Supervisors/Managers.
  const [approverId, setApproverId] = useState('');
  const [approvers, setApprovers] = useState<ApproverOption[]>([]);
  useEffect(() => { if (SEND_TO_APPROVAL_ENABLED) transactionAPI.approvers().then(setApprovers).catch(()=>{}); }, []);

  // Pick a saved destination (UPI or bank) → drives payout mode + details.
  const applyDest = (kind: 'UPI' | 'BANK', row: MerchantBankAccount) => {
    if (kind === 'UPI') { setDestId(`upi-${row.id}`); setMode('UPI'); setDetails({ upiId: row.upiId || '' }); }
    else { setDestId(`bank-${row.id}`); setMode('BANK'); setDetails({ accountHolder: row.accountHolder || '', accountNumber: row.accountNumber || '', ifsc: row.ifsc || '', bank: row.bankName || '', branch: row.branch || '' }); }
  };

  useEffect(() => { transactionAPI.summary().then(s => { setAvailable(s.available); setRb(s.runningBalance || 0); setMaxWithdrawable(s.maxWithdrawable ?? s.available); setSummaryLoaded(true); }).catch(()=>{}); }, []);

  // Auto-fill Member Name from the latest record for this Membership ID; lock it when the
  // membership already exists, otherwise allow manual entry for a new member.
  useEffect(() => {
    const mid = memberId.trim();
    if (mid.length < 3) { setMemberLocked(false); return; }
    let alive = true;
    const t = setTimeout(() => {
      transactionAPI.memberProfile(mid).then(p => {
        if (!alive) return;
        if (p.memberName) { setMemberName(p.memberName as string); setMemberLocked(true); }
        else setMemberLocked(false);
      }).catch(()=>{});
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [memberId]);

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
  const amountNum = parseFloat(parseIndianAmount(amount));
  // Inline amount validation — recomputed on every render, so it reacts immediately as the
  // operator types, and is re-checked in submit() before the request goes out. The withdrawable
  // cap is `maxWithdrawable` (balance net of fees), which is also what the server enforces, so we
  // never let the client submit an amount the backend would reject. null = no error / valid.
  const amountErr: string | null = !amount
    ? null
    : (isNaN(amountNum) || amountNum <= 0)
      ? 'Enter a valid amount greater than 0.'
      : (summaryLoaded && amountNum > maxWithdrawable + 0.01)
        ? `Insufficient balance. Maximum withdrawable amount is ${fmt(maxWithdrawable)}.`
        : null;
  const submit = async () => {
    if(!amount||!memberId||!memberName.trim()){ showToast('Enter amount, Membership ID and Member Name','error'); return; }
    if(amountErr){ showToast(amountErr,'error'); return; }
    if(hasSaved && !destId){ showToast('Select a withdrawal destination','error'); return; }
    const missing = fields.filter(f => !(details[f.key]||'').trim());
    if(missing.length){ showToast(`Fill: ${missing.map(m=>m.label).join(', ')}`,'error'); return; }
    // "Send To Approval" (demo only): an Authorized Approver is mandatory, mirroring the Agent module.
    if(SEND_TO_APPROVAL_ENABLED && !approverId){ showToast('Select an Authorized Approver.','error'); return; }
    // Agent assignment is optional on a normal merchant withdrawal — see the deposit path.
    setLoading(true);
    try {
      const payload: Record<string, unknown> = { amount: amountNum, memberId, memberName: memberName.trim(), payoutMode: mode, payoutDetails: details };
      if (mode === 'BANK') { payload.accountHolder = details.accountHolder; payload.accountNumber = details.accountNumber; payload.ifsc = details.ifsc; }
      if (SEND_TO_APPROVAL_ENABLED && approverId) { payload.sentForApproval = true; payload.approverUserId = Number(approverId); }
      const created = await transactionAPI.createWithdrawal(payload);
      fireConfetti();
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
        <div style={{ display:'flex',gap:32,flexWrap:'wrap' }}>
          <div>
            <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 2px',textTransform:'uppercase',letterSpacing:'0.06em',fontWeight:800 }}>Available Balance</p>
            <p style={{ fontSize:26,fontWeight:800,color:T.blue,margin:0 }}>{fmt(available)}</p>
          </div>
        </div>
        <p style={{ fontSize:11,color:T.textMuted,margin:'8px 0 0' }}>
          Max you can withdraw after fees: <b>{fmt(maxWithdrawable)}</b>{rb > 0 ? ` · Reserved (pending): ${fmt(rb)}` : ''}
        </p>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Amount (INR)" type="text" inputMode="decimal" value={amount} onChange={e=>setAmount(formatIndianAmountInput(e.target.value))} placeholder="Min 1" required error={amountErr || undefined}/>
        <Input label="Membership ID" value={memberId} onChange={e=>setMemberId(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,''))} placeholder="Alphanumeric (A-Z, 0-9)" required/>
        <Input label="Member Name" value={memberName} onChange={e=>setMemberName(e.target.value)} placeholder="Full name" required readOnly={memberLocked} hint={memberLocked ? 'Auto-filled from existing membership' : undefined}/>
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
      {SEND_TO_APPROVAL_ENABLED && <SendToApprovalCard noun="Withdrawal" approvers={approvers} value={approverId} onChange={setApproverId} />}
      <Btn size="lg" full variant="danger" style={{ background:T.danger,color:'#fff', ...(SEND_TO_APPROVAL_ENABLED?{ marginTop:16 }:{}) }} onClick={submit} disabled={loading||!amount||!memberId||!!amountErr}>
        {loading?'Submitting...':'Submit Withdrawal Request →'}
      </Btn>
    </div>
  );
};

// ─── Settlement form ───────────────────────────────────────────────────────────
export const SettlementForm: React.FC<{ user: User; onSubmitted?: () => void }> = ({ user, onSubmitted }) => {
  const { showToast } = useToast();
  const [form, setForm] = useState({ amount:'', memberId:'', memberName:'' });
  const [memberLocked, setMemberLocked] = useState(false);  // name auto-filled from an existing membership → read-only
  const [available, setAvailable] = useState(0);
  const [maxSettleable, setMaxSettleable] = useState(0);
  const [rb, setRb] = useState(0);
  const [loading, setLoading] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  useEffect(() => { transactionAPI.summary().then(s => { setAvailable(s.available); setRb(s.runningBalance || 0); setMaxSettleable(s.maxSettleable ?? s.available); }).catch(()=>{}); }, []);

  // Membership ID is optional for settlements; when entered, auto-fill + lock the Member Name.
  useEffect(() => {
    const mid = form.memberId.trim();
    if (mid.length < 3) { setMemberLocked(false); return; }
    let alive = true;
    const t = setTimeout(() => {
      transactionAPI.memberProfile(mid).then(p => {
        if (!alive) return;
        if (p.memberName) { setForm(f => ({ ...f, memberName: p.memberName as string })); setMemberLocked(true); }
        else setMemberLocked(false);
      }).catch(()=>{});
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [form.memberId]);

  const submit = async () => {
    const amountNum = parseFloat(parseIndianAmount(form.amount));
    if(!form.amount){ showToast('Enter an amount','error'); return; }
    if(form.memberId && !form.memberName.trim()){ showToast('Enter the Member Name for this Membership ID','error'); return; }
    if(amountNum > maxSettleable + 0.01){ showToast('We cannot process this request. The requested amount exceeds your available balance.','error'); return; }
    // Agent assignment is optional on a normal merchant settlement — see the deposit path.
    setLoading(true);
    try {
      const created = await transactionAPI.createSettlement({ amount: amountNum, memberId: form.memberId || undefined, memberName: form.memberName.trim() || undefined });
      fireConfetti();
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
        <p style={{ fontSize:11,color:'rgba(255,255,255,0.75)',margin:'10px 0 0' }}>
          Max you can settle after fees: <b>{fmt(maxSettleable)}</b>{rb > 0 ? ` · Reserved (pending): ${fmt(rb)}` : ''}
        </p>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 18px' }}>
        <Input label="Settlement Amount (INR)" type="text" inputMode="decimal" value={form.amount} onChange={e=>set('amount',formatIndianAmountInput(e.target.value))} placeholder="Enter amount" required/>
        <Input label="Membership ID" value={form.memberId} onChange={e=>set('memberId',e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,''))} placeholder="e.g. MBR20240001"/>
        <Input label="Member Name" value={form.memberName} onChange={e=>set('memberName',e.target.value)} placeholder="Full name" readOnly={memberLocked} hint={memberLocked ? 'Auto-filled from existing membership' : undefined}/>
      </div>
      <div style={{ background:T.canvas,borderRadius:10,padding:'8px 12px',margin:'2px 0 16px',fontSize:11,color:T.textMuted }}>
        No proof needed — after the Admin approves, they enter the UTR number and upload the settlement proof, which you can then view.
      </div>
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
  const [detailTx, setDetailTx] = useState<Transaction | null>(null);

  const reload = () => transactionAPI.getMine().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(() => { if (!showForm && !slipTx && !detailTx) reload(); });

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
                  {['Membership - Member',`Total ${noun} Requests`,'Status','Total Amount','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.length === 0 && <tr><td colSpan={5} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No {noun.toLowerCase()} requests yet</td></tr>}
                {groups.map((g,i)=>(
                  <tr key={g.key} style={{ background:i%2===0?T.surface:'#f8faff',cursor:'pointer' }} onClick={()=>setOpenMember(g.key)}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{memberLabel(g.key, g.items[0]?.member)}</td>
                    <td style={{ padding:'11px 14px',fontWeight:800,color:T.blue }}>{g.items.length}</td>
                    <td style={{ padding:'11px 14px' }}><Badge status={g.items[0].status} type={g.items[0].type} viewerRole="MERCHANT" approverRole={g.items[0].approverRole}/></td>
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
          <TxTable txns={active.items} actionMode="merchant" viewerRole="MERCHANT" onAction={(t, action)=> action==='slip' ? setSlipTx(t) : setDetailTx(t)}/>
        </Modal>
      )}

      {slipTx && (
        <MerchantSlipModal tx={slipTx} onClose={()=>setSlipTx(null)} onSubmitted={()=>{ setSlipTx(null); reload(); }}/>
      )}
      {detailTx && (
        <TransactionDetailsModal tx={detailTx} viewerRole="MERCHANT" onClose={()=>setDetailTx(null)} />
      )}
    </div>
  );
};

export const DepositManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Deposit Management" prefix="DEPOSIT" requestLabel="Deposit Request" noun="Deposit" FormComp={DepositForm}/>;

export const WithdrawalManagement: React.FC<{ user: User }> = ({ user }) =>
  <ManagementPage user={user} title="Withdrawal Management" prefix="WITHDRAWAL" requestLabel="Withdrawal Request" noun="Withdrawal" FormComp={WithdrawalForm}/>;

// ─── Settlement completion by Supervisor (demo: agent-assigned settlements skip Admin) ──
// When a settlement is routed through a Non-EPS agent (demo), the agent handles the payout, so the
// Supervisor completes it here by supplying the mandatory UTR + settlement proof (image/PDF) — no
// Admin approval needed. Non-agent settlements are rejected server-side and still go to the Admin.
const SETTLE_ACCEPT = 'image/*,application/pdf';

const SettlementCompleteModal: React.FC<{ tx: Transaction; onClose: () => void; onDone: () => void }> = ({ tx, onClose, onDone }) => {
  const { showToast } = useToast();
  const [d, setD] = useState<Transaction>(tx);
  const [utr, setUtr] = useState('');
  const [remark, setRemark] = useState('');
  const [proof, setProof] = useState('');
  const [proofName, setProofName] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { transactionAPI.getDetail(tx.id).then(setD).catch(() => {}); transactionAPI.recordView(tx.id); }, [tx.id]);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    if (f.size > 8 * 1024 * 1024) { showToast('File too large. Maximum 8 MB.', 'error'); return; }
    try { setProof(await fileToDataUrl(f)); setProofName(f.name); } catch { showToast('Could not read the file', 'error'); }
  };
  const submit = async () => {
    if (!utr.trim()) { showToast('UTR Number is required', 'error'); return; }
    if (!proof) { showToast('Settlement proof (image or PDF) is required', 'error'); return; }
    if (!remark.trim()) { showToast('Remarks are required', 'error'); return; }
    setBusy(true);
    try {
      await transactionAPI.supervisorSettle(tx.id, { remark: remark.trim(), utr: utr.trim(), proof });
      showToast('Settlement completed successfully');
      onDone();
    } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to complete settlement', 'error'); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={`Complete Settlement — ${d.ref}`} onClose={onClose} wide>
      <div style={{ background: T.canvas, borderRadius: 10, padding: 12, marginBottom: 14 }}>
        <SlipRow k="Member" v={memberLabel(d.memberId, d.member) || '—'} />
        <SlipRow k="Amount" v={fmt(d.amount)} />
        <SlipRow k="Status" v={<Badge status={d.status} type={d.type} approverRole={d.approverRole} />} />
        <SlipRow k="Reference" v={d.ref} />
      </div>
      <p style={{ fontSize: 12, color: T.textMuted, marginBottom: 12 }}>
        Routed through an assigned agent — complete it directly (no Admin approval needed). Enter the payment UTR and upload the settlement proof.
      </p>
      <Input label="UTR Number" value={utr} onChange={e => setUtr(e.target.value)} placeholder="Enter the payment UTR number" required />
      <div style={{ marginBottom: 12 }}>
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6 }}>Settlement Proof (image or PDF) *</label>
        <input type="file" accept={SETTLE_ACCEPT} onChange={onFile} />
        {proofName && <p style={{ margin: '6px 0 0', fontSize: 11, color: T.success, fontWeight: 700 }}><Icon name="verified" size={12} /> {proofName}</p>}
      </div>
      <div style={{ marginBottom: 8 }}>
        <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6 }}>Remarks *</label>
        <textarea value={remark} onChange={e => setRemark(e.target.value)} maxLength={1000} placeholder="Enter your remarks (mandatory)…"
          style={{ width: '100%', minHeight: 70, padding: '10px 12px', borderRadius: 10, border: `1.5px solid ${T.border}`, background: T.surface, color: T.textMain, fontSize: 13, resize: 'vertical', boxSizing: 'border-box', fontFamily: 'inherit' }} />
      </div>
      <div style={{ display: 'flex', gap: 10, borderTop: `1px solid ${T.border}`, paddingTop: 14 }}>
        <Btn variant="success" onClick={submit} disabled={busy}>{busy ? 'Completing…' : '<Icon name="approve" size={14} /> Complete Settlement'}</Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </Modal>
  );
};

// Demo-only, Supervisor-only queue of agent-assigned settlements awaiting completion. Renders
// nothing on Production, for non-Supervisors, or when there is nothing to complete.
const AgentSettlementCompletionQueue: React.FC<{ user: User }> = ({ user }) => {
  const [rows, setRows] = useState<Transaction[]>([]);
  const [active, setActive] = useState<Transaction | null>(null);
  const isSupervisor = String(user.merchantRole || '').toUpperCase() === 'SUPERVISOR';
  const on = IS_DEMO && isSupervisor;
  const reload = () => transactionAPI.getAllOverseer()
    .then(txs => setRows(txs.filter(t => t.type.startsWith('SETTLEMENT') && t.status === 'SLIP_SUBMITTED' && t.assignedAgentId != null && t.merchant === user.name)))
    .catch(() => setRows([]));
  useEffect(() => { if (on) reload(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { if (on && !active) reload(); });
  if (!on || rows.length === 0) return null;

  return (
    <Card style={{ marginBottom: 16, borderTop: `3px solid ${T.info}` }}>
      <div style={{ padding: '14px 16px 0' }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 800 }}>Agent Settlements — Awaiting Your Completion</h3>
        <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>Routed through an assigned agent — complete each with the UTR + proof. No Admin approval needed.</p>
      </div>
      <div style={{ overflowX: 'auto', padding: 12 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead><tr style={{ background: T.canvas }}>{['Reference', 'Member', 'Amount', 'Date & Time', 'Action'].map(h => (
            <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `2px solid ${T.border}` }}>{h}</th>
          ))}</tr></thead>
          <tbody>
            {rows.map(t => (
              <tr key={t.id} style={{ borderBottom: `1px solid ${T.borderLight}` }}>
                <td style={{ padding: '11px 14px', fontWeight: 700, color: T.textMain }}>{t.ref}</td>
                <td style={{ padding: '11px 14px', color: T.textMuted }}>{memberLabel(t.memberId, t.member) || '—'}</td>
                <td style={{ padding: '11px 14px', fontWeight: 800, whiteSpace: 'nowrap' }}>{fmt(t.amount)}</td>
                <td style={{ padding: '11px 14px', color: T.textMuted, whiteSpace: 'nowrap' }}>{t.date} {t.time}</td>
                <td style={{ padding: '11px 14px' }}><Btn size="sm" onClick={() => setActive(t)}>Complete</Btn></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {active && <SettlementCompleteModal tx={active} onClose={() => setActive(null)} onDone={() => { setActive(null); reload(); }} />}
    </Card>
  );
};

// Settlement Requests is a Supervisor-only page (App.tsx gates access): the Supervisor creates
// settlement requests and views their own submitted requests + status. On demo, agent-assigned
// settlements are completed by the Supervisor here (UTR + proof, no Admin); on Production they go
// straight to the Admin (Supervisor → Admin → Completed).
export const SettlementManagement: React.FC<{ user: User }> = ({ user }) => (
  <div>
    <AgentSettlementCompletionQueue user={user} />
    <ManagementPage user={user} title="Settlement Requests" prefix="SETTLEMENT" requestLabel="Settlement Request" noun="Settlement" FormComp={SettlementForm}/>
  </div>
);

// ─── Balance Page ─────────────────────────────────────────────────────────────
export const BalancePage: React.FC<{ user: User }> = ({ user }) => {
  const [s, setS] = useState<BalanceSummary | null>(null);
  const reload = () => transactionAPI.summary().then(setS).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(reload);

  // Canonical balance — single source of truth from the backend (completed only):
  //   Total Available Balance = Total Deposits − Total Withdrawals − Total Settlements
  //   Pay-Out Fee             = Withdrawal Commission + Settlement Commission
  //   Available Balance       = Total Available Balance − Deposit Commission − Pay-Out Fee
  const totalDeposit = s?.totalDeposit ?? 0;
  const totalWithdrawn = s?.totalWithdrawn ?? 0;
  const totalSettled = s?.totalSettled ?? 0;
  const depositCommission = s?.depositCommission ?? 0;
  const withdrawalCommission = s?.withdrawalCommission ?? 0;
  const settlementCommission = s?.settlementCommission ?? 0;
  const payoutFee = s?.payoutFee ?? (withdrawalCommission + settlementCommission);
  const totalAvailableBalance = s?.totalAvailableBalance ?? (totalDeposit - totalWithdrawn - totalSettled);
  const available = s?.available ?? (totalAvailableBalance - depositCommission - payoutFee);

  const rows: Array<[string, number, string, boolean]> = [
    ['Total Deposits', totalDeposit, T.success, false],
    ['Total Withdrawals', totalWithdrawn, T.danger, false],
    ['Total Settlements', totalSettled, T.danger, false],
    ['Total Available Balance', totalAvailableBalance, T.textMain, true],
    ['Deposit Commission', depositCommission, T.danger, false],
    ['Pay-Out Fee', payoutFee, T.danger, false],
    ['Available Balance', available, T.blue, true],
  ];

  return (
    <div style={{ maxWidth:600 }}>
      <Card style={{ padding:26 }}>
        <div style={{ background:T.grad3,borderRadius:16,padding:28,marginBottom:22,color:'#fff' }}>
          <p style={{ fontSize:11,color:'rgba(255,255,255,0.55)',margin:'0 0 4px',textTransform:'uppercase',letterSpacing:'0.08em',fontWeight:700 }}>Available Balance</p>
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

// ─── Shared read-only Transaction Details modal ────────────────────────────────
// Used by Merchants (their own completed transactions) and Supervisors/Managers (any
// transaction, permanently — including after completion). Reuses SlipRow / ProofGallery /
// Badge / TxExportButton. Read-only: no edit actions. Theme tokens => Light + Dark mode.
const DetailSection: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={{ marginBottom: 16 }}>
    <p style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>{title}</p>
    <div style={{ background: T.canvas, borderRadius: 10, padding: 12 }}>{children}</div>
  </div>
);

export const TransactionDetailsModal: React.FC<{ tx: Transaction; viewerRole?: string; onClose: () => void }> = ({ tx, viewerRole, onClose }) => {
  const [d, setD] = useState<Transaction>(tx);
  const [audit, setAudit] = useState<AuditLogEntry[]>([]);
  // Full record (incl. slip images), audit history, and a "<role> Viewed" audit entry on open.
  useEffect(() => {
    transactionAPI.getDetail(tx.id).then(setD).catch(() => {});
    transactionAPI.getAudit(tx.id).then(setAudit).catch(() => setAudit([]));
    transactionAPI.recordView(tx.id);
  }, [tx.id]);

  const isDeposit = d.type.startsWith('DEPOSIT');
  const isWithdrawal = d.type.startsWith('WITHDRAWAL');
  const isSettlement = d.type.startsWith('SETTLEMENT');
  const slips = (d.merchantProofs && d.merchantProofs.length) ? d.merchantProofs : (d.merchantProof ? [d.merchantProof] : []);
  const created = d.createdAt ? formatDateTime(d.createdAt) : `${d.date} ${d.time}`;
  const paymentMethod = d.depositType ? depositTypeLabel(d.depositType) : (d.payoutMode || '—');

  // Timeline of status changes: creation + each recorded approval action. This modal is
  // client-facing (only role === 'MERCHANT' users reach it — see isOverseerRole), so actions taken
  // by an internal Clari5Pay role show the role alone; the client's own staff keep their names.
  // The internal audit log is unaffected and still records the real user.
  const timeline: { label: string; who: string; at: string }[] = [
    { label: 'Created', who: `${d.creatorUsername || d.merchant}`, at: created },
    ...((d.remarksHistory || []).map(r => ({
      label: r.action.charAt(0) + r.action.slice(1).toLowerCase(),
      who: isInternalRole(r.role)
        ? (merchantRoleLabel(r.role) || r.role)
        : `${merchantRoleLabel(r.role) || r.role} · ${r.user}`,
      at: r.at,
    }))),
  ];

  return (
    <Modal title={`Transaction Details — ${d.ref}`} onClose={onClose} wide>
      <DetailSection title="Transaction Information">
        <SlipRow k="Reference Number" v={d.ref} />
        <SlipRow k="Type" v={typeLabel(d.type)} />
        <SlipRow k="Status" v={<Badge status={d.status} type={d.type} viewerRole={viewerRole} approverRole={d.approverRole} />} />
        <SlipRow k="Amount" v={fmt(d.amount)} />
        <SlipRow k="Payment Method" v={paymentMethod} />
        {d.riskLevel && <SlipRow k="Risk Level" v={d.riskLevel} />}
        {d.highRisk && <SlipRow k="High Risk" v="Yes" />}
        <SlipRow k="Created Date & Time" v={created} />
      </DetailSection>

      <DetailSection title="Merchant Information">
        <SlipRow k="Merchant Name" v={d.merchant} />
        {d.merchantCode && <SlipRow k="Merchant ID" v={d.merchantCode} />}
        {d.creatorUsername && <SlipRow k="Merchant Username" v={d.creatorUsername} />}
        {d.creatorRole && <SlipRow k="Merchant Role" v={merchantRoleLabel(d.creatorRole) || d.creatorRole} />}
      </DetailSection>

      {(d.memberId || d.member) && (
        <DetailSection title="Member Information">
          <SlipRow k="Membership - Member" v={memberLabel(d.memberId, d.member) || '—'} />
        </DetailSection>
      )}

      <DetailSection title="Financial Information">
        {isDeposit && <SlipRow k="Deposit Amount" v={fmt(d.amount)} />}
        {isWithdrawal && <SlipRow k="Withdrawal Amount" v={fmt(d.amount)} />}
        {isSettlement && <SlipRow k="Settlement Amount" v={fmt(d.amount)} />}
      </DetailSection>

      {/* Client-facing: the Approver is the client's own approval role (Deposit → Supervisor,
          Withdrawal/Settlement → Manager), never the internal admin who actioned it. Supervisor
          and Manager below are the client's OWN staff, so they keep their names. The internal
          "Processed By (Admin)" row is not shown to the client — the audit log still records it. */}
      {(d.approvedBy || d.supervisorName || d.managerName) && (
        <DetailSection title="Approval Information">
          {d.approvedBy && <SlipRow k="Approved By" v={clientApproverLabel(d.type)} />}
          {d.supervisorName && <SlipRow k="Supervisor" v={`${nameWithRole(d.supervisorName, 'SUPERVISOR', '', d.supervisorUsername)}${d.supervisorActionAt ? ` · ${formatDateTime(d.supervisorActionAt)}` : ''}`} />}
          {d.managerName && <SlipRow k="Manager" v={`${nameWithRole(d.managerName, 'MANAGER', '', d.managerUsername)}${d.managerActionAt ? ` · ${formatDateTime(d.managerActionAt)}` : ''}`} />}
        </DetailSection>
      )}

      <DetailSection title="Timeline">
        {timeline.map((e, i) => (
          <div key={i} style={{ display: 'flex', gap: 10, padding: '6px 0', borderBottom: i < timeline.length - 1 ? `1px solid ${T.borderLight}` : 'none' }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: T.textMain, minWidth: 110 }}>{e.label}</span>
            <span style={{ fontSize: 12, color: T.textMuted, flex: 1 }}>{e.who}</span>
            <span style={{ fontSize: 11, color: T.textMuted, whiteSpace: 'nowrap' }}>{e.at}</span>
          </div>
        ))}
      </DetailSection>

      {(slips.length > 0 || d.adminProof || d.adminBankImage) && (
        <DetailSection title="Uploaded Documents / Slips">
          {slips.length > 0 && (
            <>
              <p style={{ fontSize: 11, color: T.textMuted, margin: '0 0 6px' }}>{isDeposit ? 'Deposit Slip' : 'Payment Proof'}{d.merchantRef ? ` · Ref ${d.merchantRef}` : ''}</p>
              <ProofGallery srcs={slips} />
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
                {slips.map((s, i) => <Btn key={i} size="sm" variant="ghost" onClick={() => downloadDataUrl(s, `slip-${d.ref}-${i + 1}.png`)}><Icon name="download" size={14} /> Download Slip{slips.length > 1 ? ` ${i + 1}` : ''}</Btn>)}
              </div>
            </>
          )}
          {d.adminBankImage && (
            <div style={{ marginTop: slips.length ? 12 : 0 }}>
              <p style={{ fontSize: 11, color: T.textMuted, margin: '0 0 6px' }}>Bank Details Image</p>
              <ProofGallery srcs={[d.adminBankImage]} />
              <Btn size="sm" variant="ghost" style={{ marginTop: 8 }} onClick={() => downloadDataUrl(d.adminBankImage!, `bank-details-${d.ref}.png`)}><Icon name="download" size={14} /> Download Bank Details Image</Btn>
            </div>
          )}
          {d.adminProof && (
            <div style={{ marginTop: slips.length ? 12 : 0 }}>
              <p style={{ fontSize: 11, color: T.textMuted, margin: '0 0 6px' }}>Payment Receipt</p>
              <ProofGallery srcs={[d.adminProof]} />
              <Btn size="sm" variant="ghost" style={{ marginTop: 8 }} onClick={() => downloadDataUrl(d.adminProof!, `receipt-${d.ref}.png`)}><Icon name="download" size={14} /> Download Receipt</Btn>
            </div>
          )}
        </DetailSection>
      )}

      {d.remarksHistory && d.remarksHistory.length > 0 && (
        <DetailSection title="Remarks">
          {d.remarksHistory.map((r, i) => (
            <div key={i} style={{ borderLeft: `3px solid ${T.border}`, paddingLeft: 10, marginBottom: 8 }}>
              <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: T.textMain }}>{clientRemarkActor(r.role, r.user, r.username)} — {r.action}</p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>{r.remark}</p>
              <p style={{ margin: '2px 0 0', fontSize: 10, color: T.textMuted }}>{r.at}</p>
            </div>
          ))}
        </DetailSection>
      )}

      {/* Client-facing audit view: an internal Clari5Pay actor shows as the role alone, with no
          username and no IP — the full entry stays intact in the internal audit log. */}
      <DetailSection title="Audit History">
        {audit.length === 0 ? <p style={{ fontSize: 12, color: T.textMuted, margin: 0 }}>No audit entries.</p> : audit.map(a => (
          <div key={a.id} style={{ display: 'flex', gap: 10, padding: '6px 0', borderBottom: `1px solid ${T.borderLight}`, fontSize: 12 }}>
            <span style={{ fontWeight: 700, color: T.textMain, minWidth: 150 }}>{a.action}</span>
            <span style={{ color: T.textMuted, flex: 1 }}>{clientAuditActor(a.role, a.username)}{a.reason ? ` — ${a.reason}` : ''}</span>
            <span style={{ color: T.textMuted, whiteSpace: 'nowrap' }}>{formatDateTime(a.createdAt)}{a.ip && !isInternalRole(a.role) ? ` · ${a.ip}` : ''}</span>
          </div>
        ))}
      </DetailSection>

      {/* Agent Management (Phase 4) — READ-ONLY view of the current agent assignment + history.
          The assign/reassign action lives in the Deposit / Withdrawal / Settlement workflows, not
          here. Demo-gated, merchant-portal only. */}
      {IS_DEMO && viewerRole === 'MERCHANT' && (
        <AgentAssignmentPanel txRef={d.ref} txType={d.type} readOnly />
      )}

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', borderTop: `1px solid ${T.border}`, paddingTop: 14, marginTop: 14 }}>
        <TxExportButton txns={[d]} title={`Transaction ${d.ref}`} />
        <Btn variant="ghost" onClick={onClose}>Close</Btn>
      </div>
    </Modal>
  );
};

// ─── Reviewer (Supervisor / Manager) detail + action modal ─────────────────────
// Shows the complete payment details for an assigned request and lets the reviewer
// Approve / Reject / Resubmit. Remarks are mandatory on every action (review-only —
// reviewers never complete a transaction).
const REMARK_LABELS: Record<string, { title: string; confirm: string; label: string }> = {
  approve: { title: 'Approve & forward to Admin', confirm: 'Approve', label: 'Approval Remarks' },
  reject: { title: 'Reject Request', confirm: 'Reject', label: 'Rejection Remarks' },
  resubmit: { title: 'Return for Resubmission', confirm: 'Resubmit', label: 'Resubmission Remarks' },
};

const ReviewModal: React.FC<{ tx: Transaction; onClose: () => void; onDone: () => void }> = ({ tx, onClose, onDone }) => {
  const { showToast } = useToast();
  const [d, setD] = useState<Transaction>(tx);
  const [action, setAction] = useState<'approve' | 'reject' | 'resubmit' | null>(null);
  const [busy, setBusy] = useState(false);
  // Heavy proof images are omitted from list payloads — fetch the full record on open,
  // and record a "<role> Viewed" audit entry (the reviewer is opening the request).
  useEffect(() => { transactionAPI.getDetail(tx.id).then(setD).catch(() => {}); transactionAPI.recordView(tx.id); }, [tx.id]);

  const slips = (d.merchantProofs && d.merchantProofs.length) ? d.merchantProofs : (d.merchantProof ? [d.merchantProof] : []);
  const isDeposit = d.type.startsWith('DEPOSIT');

  const submit = async (remark: string) => {
    if (!action) return;
    setBusy(true);
    try {
      // Route by the request TYPE, not the reviewer's role — a deposit always uses the deposit
      // (supervisor) gate, a withdrawal the withdrawal (manager) gate — so the selected approver can
      // act whatever their role. On Production a reviewer only ever sees their own role's type, so
      // this is identical to the previous role-based routing there.
      if (isDeposit) await transactionAPI.supervisorReview(tx.id, action, remark);
      else await transactionAPI.managerReview(tx.id, action, remark);
      showToast(action === 'approve' ? 'Approved — forwarded to Admin' : action === 'reject' ? 'Request rejected' : 'Returned for resubmission');
      onDone();
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Action failed', 'error');
    } finally { setBusy(false); }
  };

  if (action) {
    const l = REMARK_LABELS[action];
    return (
      <ReasonModal
        title={`${l.title} — ${d.ref}`} label={l.label} confirmLabel={l.confirm}
        requiredHint="Remarks are required" maxLength={1000} busy={busy}
        placeholder="Enter your remarks (mandatory)…"
        onSubmit={submit} onClose={() => setAction(null)} closeLabel="Back"
      />
    );
  }

  return (
    <Modal title={`Review Request — ${d.ref}`} onClose={onClose} wide>
      <div style={{ background: T.canvas, borderRadius: 10, padding: 12, marginBottom: 14 }}>
        <SlipRow k="Merchant" v={d.merchant} />
        <SlipRow k="Member" v={memberLabel(d.memberId, d.member) || '—'} />
        <SlipRow k="Type" v={typeLabel(d.type)} />
        <SlipRow k="Amount" v={fmt(d.amount)} />
        <SlipRow k="Status" v={<Badge status={d.status} type={d.type} approverRole={d.approverRole} />} />
        <SlipRow k="Reference" v={d.ref} />
        {d.merchantRef && <SlipRow k="Payment / UTR Reference" v={d.merchantRef} />}
        {d.depositType && <SlipRow k="Payment Method" v={depositTypeLabel(d.depositType)} />}
        {d.payoutMode && <SlipRow k="Payout Mode" v={d.payoutMode} />}
        {d.bank && <SlipRow k="Bank" v={d.bank} />}
        {d.accountHolder && <SlipRow k="Account Holder" v={d.accountHolder} />}
        {d.accountNumber && <SlipRow k="Account Number" v={d.accountNumber} />}
        {d.ifsc && <SlipRow k="IFSC" v={d.ifsc} />}
        {d.adminUpiId && <SlipRow k="UPI ID" v={d.adminUpiId} />}
        {d.senderUpiId && <SlipRow k="Sender UPI" v={d.senderUpiId} />}
        {d.payoutDetails && Object.entries(d.payoutDetails).map(([k, v]) => v ? <SlipRow key={k} k={k} v={String(v)} /> : null)}
      </div>

      {/* Timeline (created → reviewer → admin). */}
      <div style={{ background: T.canvas, borderRadius: 10, padding: 12, marginBottom: 14 }}>
        <p style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Timeline</p>
        <SlipRow k="Created By" v={`${nameWithRole(d.merchant, d.creatorRole, 'Merchant User', d.creatorUsername)}${d.createdAt ? ` · ${formatDateTime(d.createdAt)}` : ''}`} />
        {d.supervisorName && <SlipRow k="Supervisor" v={`${nameWithRole(d.supervisorName, 'SUPERVISOR', '', d.supervisorUsername)}${d.supervisorActionAt ? ` · ${formatDateTime(d.supervisorActionAt)}` : ''}`} />}
        {d.managerName && <SlipRow k="Manager" v={`${nameWithRole(d.managerName, 'MANAGER', '', d.managerUsername)}${d.managerActionAt ? ` · ${formatDateTime(d.managerActionAt)}` : ''}`} />}
        {d.adminActionAt && <SlipRow k="Admin Action" v={formatDateTime(d.adminActionAt)} />}
      </div>

      {/* Deposit slip / proof submitted by the merchant. */}
      {isDeposit && slips.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <p style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Deposit Slip</p>
          <ProofGallery srcs={slips} />
        </div>
      )}

      {/* Remarks history. */}
      {d.remarksHistory && d.remarksHistory.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <p style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Remarks History</p>
          {d.remarksHistory.map((r, i) => (
            <div key={i} style={{ borderLeft: `3px solid ${T.border}`, paddingLeft: 10, marginBottom: 8 }}>
              <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: T.textMain }}>{clientRemarkActor(r.role, r.user, r.username)} — {r.action}</p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>{r.remark}</p>
              <p style={{ margin: '2px 0 0', fontSize: 10, color: T.textMuted }}>{r.at}</p>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', borderTop: `1px solid ${T.border}`, paddingTop: 14 }}>
        <Btn variant="success" onClick={() => setAction('approve')}><Icon name="approve" size={14} /> Approve</Btn>
        <Btn variant="danger" onClick={() => setAction('reject')}><Icon name="reject" size={14} /> Reject</Btn>
        <Btn variant="secondary" onClick={() => setAction('resubmit')}><Icon name="refresh" size={14} /> Resubmit</Btn>
        <Btn variant="ghost" onClick={onClose}>Close</Btn>
      </div>
    </Modal>
  );
};

// ─── Approvals Page (Supervisor → deposits & settlements · Manager → withdrawals) ──
// `kind` scopes the queue to one request type. Defaulted from the reviewer's role
// (Supervisor → deposits, Manager → withdrawals); the Supervisor's Settlement
// Management page passes kind="SETTLEMENT" to show settlement requests on their own page.
export const ApprovalsPage: React.FC<{ user: User; kind?: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT' }> = ({ user, kind }) => {
  const isManager = String(user.merchantRole || '').toUpperCase() === 'MANAGER';
  const roleStatus = isManager ? 'MANAGER_REVIEW' : 'SUPERVISOR_REVIEW';
  const rolePrefix = kind || (isManager ? 'WITHDRAWAL' : 'DEPOSIT');
  // Demo "Send To Approval" makes the main Approvals page a single per-user queue: a request
  // addressed to me appears here whatever my role or its type — a Manager sees a deposit chosen for
  // them, a Supervisor a withdrawal. The classic role-partitioned queue still applies on Production,
  // to unassigned requests, and to the Settlement page (kind set).
  const unifiedDemo = SEND_TO_APPROVAL_ENABLED && !kind;
  const heading = kind === 'SETTLEMENT' ? 'Settlement Approvals'
    : unifiedDemo ? 'Approvals'
    : rolePrefix === 'WITHDRAWAL' ? 'Withdrawal Approvals' : 'Deposit Approvals';
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<Transaction | null>(null);

  // A request sits at its review gate when a deposit is at SUPERVISOR_REVIEW or a withdrawal at
  // MANAGER_REVIEW — the status the backend expects the assigned reviewer to act on.
  const atReviewGate = (t: Transaction) =>
    (t.type.startsWith('DEPOSIT') && t.status === 'SUPERVISOR_REVIEW') ||
    (t.type.startsWith('WITHDRAWAL') && t.status === 'MANAGER_REVIEW');

  // Overseer feed, scoped to the reviewer's own business (matches the backend same-business guard).
  const mine = (t: Transaction) => {
    if (t.merchant !== user.name) return false;
    // Demo: a request addressed to me — regardless of my role or its type (backend also 403s others).
    if (unifiedDemo && t.approverUserId) return t.approverUserId === user.id && atReviewGate(t);
    // Production / Settlement / unassigned: classic role-partitioned queue, unchanged.
    return t.status === roleStatus && t.type.startsWith(rolePrefix) && !t.approverUserId;
  };
  const reload = () => transactionAPI.getAllOverseer()
    .then(rows => setTxns(rows.filter(mine)))
    .catch(() => setTxns([]));
  useEffect(() => { reload().finally(() => setLoading(false)); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(() => { if (!active) reload(); });

  if (loading) return <LoadingScreen label="Loading approvals…" />;

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 800 }}>{heading}</h2>
        <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>
          Requests awaiting your review. Approve to forward to Admin, or Reject / Resubmit — remarks are mandatory.
        </p>
      </div>
      <Card>
        {txns.length === 0 ? (
          <p style={{ padding: 24, textAlign: 'center', color: T.textMuted, fontSize: 13 }}>No requests awaiting your review.</p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ background: T.canvas }}>
                  {['Reference', 'Member', 'Type', 'Amount', 'Status', 'Date & Time', 'Action'].map(h => (
                    <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {txns.map(t => (
                  <tr key={t.id} style={{ background: T.surface, borderBottom: `1px solid ${T.borderLight}` }}>
                    <td style={{ padding: '11px 14px', fontWeight: 700, color: T.textMain }}>{t.ref}</td>
                    <td style={{ padding: '11px 14px', color: T.textMuted }}>{memberLabel(t.memberId, t.member) || '—'}</td>
                    <td style={{ padding: '11px 14px' }}>{typeLabel(t.type)}</td>
                    <td style={{ padding: '11px 14px', fontWeight: 800, color: T.textMain, whiteSpace: 'nowrap' }}>{fmt(t.amount)}</td>
                    <td style={{ padding: '11px 14px' }}><Badge status={t.status} type={t.type} approverRole={t.approverRole} /></td>
                    <td style={{ padding: '11px 14px', color: T.textMuted, whiteSpace: 'nowrap' }}>{t.date} {t.time}</td>
                    <td style={{ padding: '11px 14px' }}><Btn size="sm" onClick={() => setActive(t)}>Review</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {active && <ReviewModal tx={active} onClose={() => setActive(null)} onDone={() => { setActive(null); reload(); }} />}
    </div>
  );
};

// ─── Cancel Request Page (DEO / Supervisor) ────────────────────────────────────
export const CancelRequestPage: React.FC<{ user: User }> = () => {
  const { showToast } = useToast();
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [target, setTarget] = useState<Transaction | null>(null);  // request awaiting a cancellation reason

  const reload = () => transactionAPI.getMine().then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(() => { if (!busy && !target) reload(); });

  // Only requests still in flight can be cancelled.
  const cancellable = txns.filter(t => t.status === 'ACCOUNT_REQUESTED' || t.status === 'ACCOUNT_SUBMITTED');

  const confirmCancel = async (reason: string) => {
    if (!target) return;
    const t = target;
    setBusy(t.id);
    try { await transactionAPI.cancel(t.id, reason); setTarget(null); await reload(); showToast(`${t.ref} cancelled`); }
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
                  {['Reference Number','Type','Amount','Membership - Member','Status','Action'].map(h=>(
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
                    <td style={{ padding:'11px 14px',color:T.textMain,fontWeight:600 }}>{memberLabel(t.memberId, t.member)}</td>
                    <td style={{ padding:'11px 14px' }}><Badge status={t.status} type={t.type} viewerRole="MERCHANT" approverRole={t.approverRole}/></td>
                    <td style={{ padding:'11px 14px' }}>
                      <Btn size="sm" variant="danger" disabled={busy===t.id} onClick={()=>setTarget(t)}>{busy===t.id?'Cancelling...':'⊘ Cancel'}</Btn>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {target && (
        <ReasonModal
          title="Cancel Request"
          message={`Please provide the reason for cancelling ${target.ref} (${typeLabel(target.type)}).`}
          label="Cancellation Reason"
          placeholder="e.g. Wrong amount entered, Duplicate request, Customer requested cancellation..."
          confirmLabel="Confirm Cancellation"
          closeLabel="Close"
          requiredHint="Cancellation reason is required."
          maxLength={500}
          busy={busy === target.id}
          onSubmit={confirmCancel}
          onClose={()=>setTarget(null)}
        />
      )}
    </div>
  );
};

// ─── All Templates View (Manager — read-only consolidated list) ─────────────────
export const TemplatesPage: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);

  // Oversight roles (Manager/Supervisor) see every merchant's requests; others see their own.
  const reload = () => {
    const fn = isOverseerRole(user) ? transactionAPI.getAllOverseer : transactionAPI.getMine;
    return fn().then(setTxns).catch(()=>setTxns([]));
  };
  useEffect(() => { reload().finally(()=>setLoading(false)); }, [user.role, user.merchantRole]);
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
      <StatCard icon="risk-management" label="Risk Score" value="22 / 100" color={T.success}/>
      <StatCard icon="priority" label="Flagged Txns" value="0" color={T.warning}/>
      <StatCard icon="velocity" label="Velocity" value="Normal" color={T.info}/>
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
const MERCHANT_STATUSES = ['ACCOUNT_REQUESTED','ACCOUNT_SUBMITTED','PENDING_APPROVAL','SUPERVISOR_REVIEW','MANAGER_REVIEW','SLIP_SUBMITTED','RESUBMITTED','REJECTED','DEPOSITED','COMPLETED','CANCELLED'];

// Oversight merchant roles (Supervisor / Manager) get a read-only, system-wide
// view of every merchant's transactions; regular merchants see only their own.
const isOverseerRole = (user: User) =>
  user.role === 'MERCHANT' && ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());

export const TransactionHistory: React.FC<{ user: User }> = ({ user }) => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [query, setQuery] = useState<TxQuery>({});   // server-side search + date filters
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const [filtering, setFiltering] = useState(false);   // Apply Filters request in flight
  const [slipTx, setSlipTx] = useState<Transaction | null>(null);
  const [detailTx, setDetailTx] = useState<Transaction | null>(null);

  const overseer = isOverseerRole(user);

  const reload = () => {
    // Supervisor/Manager → all transactions (read-only); merchant → own; admin → all.
    // The applied search/date filters are sent server-side for performance.
    const fn = overseer ? transactionAPI.getAllOverseer
      : user.role === 'MERCHANT' ? transactionAPI.getMine
      : transactionAPI.getAll;
    return fn(query).then(setTxns).catch(()=>setTxns([]));
  };
  // Refetch on mount, role change, and whenever the applied filters change.
  useEffect(() => { setFiltering(true); reload().finally(()=>{ setLoading(false); setFiltering(false); }); }, [user.role, user.merchantRole, query]);
  usePoll(() => { if (!slipTx && !detailTx) reload(); });

  // Type/status are lightweight client-side refinements on the server-filtered set.
  const filtered = txns.filter(t => (type === 'ALL' || t.type === type) && (status === 'ALL' || t.status === status));

  return (
    <>
    <Card>
      <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800 }}>{overseer ? 'All Transactions' : 'Transaction Ledger'}</h3>
        {overseer && <p style={{ margin:'-6px 0 12px',fontSize:11,color:T.textMuted }}>Read-only view of every merchant's transactions, ordered by status priority (newest first within each status).</p>}
        <TxSearchFilters onApply={setQuery} onClear={()=>setQuery({})} loading={filtering} />
        <div style={{ display:'flex',gap:8,flexWrap:'wrap',marginTop:12 }}>
          <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...MERCHANT_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
          </select>
          <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...MERCHANT_STATUSES].map(v=><option key={v} value={v}>{v==='ALL'?'All Statuses':typeLabel(v)}</option>)}
          </select>
          <TxExportButton txns={filtered} title="Transaction Ledger" />
        </div>
      </div>
      <TxTable loading={loading} txns={filtered} viewerRole={user.role}
        actionMode={overseer ? 'view' : (user.role==='MERCHANT'?'merchant':'view')}
        onAction={(t, action)=> action==='slip' ? setSlipTx(t) : setDetailTx(t)}/>
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center' }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
    </Card>
    {slipTx && (
      <MerchantSlipModal tx={slipTx} onClose={()=>setSlipTx(null)} onSubmitted={()=>{ setSlipTx(null); reload(); }}/>
    )}
    {detailTx && (
      <TransactionDetailsModal tx={detailTx} viewerRole={user.role} onClose={()=>setDetailTx(null)} />
    )}
    </>
  );
};

// Renders a chat message's attachment: images inline (click to enlarge/open + download);
// documents as a compact card (View / Download). Shared shape across both chat portals.
export const ChatAttachment: React.FC<{ msg: SupportMessage; mine: boolean }> = ({ msg, mine }) => {
  if (!msg.attachment) return null;
  const name = msg.attachmentName || 'attachment';
  const linkColor = mine ? '#fff' : T.blue;
  if (isChatImage(msg.attachmentType, name)) {
    return (
      <div style={{ marginTop: msg.content ? 8 : 0 }}>
        <img src={msg.attachment} alt={name} loading="lazy" onClick={() => openDataUrl(msg.attachment!)}
          style={{ maxWidth: 240, maxHeight: 260, borderRadius: 10, display: 'block', cursor: 'zoom-in', objectFit: 'cover' }} />
        <div style={{ marginTop: 4, display: 'flex', gap: 12 }}>
          <button onClick={() => openDataUrl(msg.attachment!)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, color: linkColor, textDecoration: 'underline' }}>Open</button>
          <a href={msg.attachment} download={name} style={{ fontSize: 10, color: linkColor, textDecoration: 'underline' }}><Icon name="download" size={12} /> Download</a>
        </div>
      </div>
    );
  }
  return (
    <div style={{ marginTop: msg.content ? 8 : 0, display: 'flex', alignItems: 'center', gap: 10, background: mine ? 'rgba(255,255,255,0.15)' : T.surface, border: `1px solid ${mine ? 'rgba(255,255,255,0.25)' : T.border}`, borderRadius: 10, padding: '8px 10px', maxWidth: 260 }}>
      <div style={{ width: 34, height: 34, borderRadius: 8, background: mine ? 'rgba(255,255,255,0.2)' : T.infoBg, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18, flexShrink: 0 }}><Icon name="file" size={18} /></div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: mine ? '#fff' : T.textMain, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</p>
        <p style={{ margin: '1px 0 0', fontSize: 10, color: mine ? 'rgba(255,255,255,0.85)' : T.textMuted }}>{formatBytes(msg.attachmentSize)}</p>
        <div style={{ display: 'flex', gap: 12, marginTop: 4 }}>
          <button onClick={() => openDataUrl(msg.attachment!)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, fontWeight: 700, color: linkColor, textDecoration: 'underline' }}>View</button>
          <a href={msg.attachment} download={name} style={{ fontSize: 10, fontWeight: 700, color: linkColor, textDecoration: 'underline' }}>Download</a>
        </div>
      </div>
    </div>
  );
};

// WhatsApp brand green + logo glyph — used by the Emergency Contact card.
const WA_GREEN = '#25D366';
const WhatsAppIcon: React.FC<{ size?: number; color?: string }> = ({ size = 20, color = '#fff' }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill={color} aria-hidden="true">
    <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.149-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.148-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51l-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.71.306 1.263.489 1.694.625.712.227 1.36.195 1.872.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.002-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/>
  </svg>
);

// ─── Customer Support chat (merchant side, WebSocket) ──────────────────────────
export const MerchantSupportChat: React.FC<{ user: User }> = ({ user }) => {
  const { showToast } = useToast();
  const [messages, setMessages] = useState<SupportMessage[]>([]);
  const [input, setInput] = useState('');
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [conv, setConv] = useState<{ queued: boolean; agentName: string | null; status: string } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  // Refresh assignment status whenever the thread changes (a new message may have just assigned us).
  const refreshConv = () => supportAPI.myConversation().then(setConv).catch(()=>{});
  useEffect(() => { refreshConv(); }, [messages.length]);

  useEffect(() => {
    supportAPI.myMessages().then(setMessages).catch(()=>{});
    refreshConv();
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

  // Attach an image/document: validate client-side, then send (with any typed text) over the
  // socket, or via REST as a fallback. The receiver gets it in real time.
  const sendAttachment = async (f: File) => {
    const err = chatAttachmentError(f);
    if (err) { showToast(err, 'error'); return; }
    setSending(true);
    try {
      const att = await readChatAttachment(f);
      // Always send attachments over REST (not the socket) — large base64 payloads can exceed
      // the WebSocket frame limit. The server still delivers to both parties in real time, so
      // the socket echoes it back; dedupe on id to avoid showing it twice.
      const m = await supportAPI.send(input.trim(), undefined, { dataUrl: att.dataUrl, name: att.name });
      setMessages(prev => prev.some(x => x.id === m.id) ? prev : [...prev, m]);
      setInput('');
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to send attachment', 'error');
    } finally { setSending(false); }
  };
  const onPickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) sendAttachment(f);
    e.target.value = '';
  };

  return (
    <div style={{ maxWidth:800,height:'calc(100vh - 120px)',display:'flex',flexDirection:'column',gap:16 }}>
      {/* Emergency Contact — instant WhatsApp support, above the live chat. */}
      <Card style={{ padding:'16px 20px', borderLeft:`3px solid ${WA_GREEN}` }}>
        <div style={{ display:'flex',alignItems:'center',gap:14,flexWrap:'wrap' }}>
          <div style={{ width:44,height:44,flexShrink:0,borderRadius:14,background:WA_GREEN,display:'flex',alignItems:'center',justifyContent:'center' }}>
            <WhatsAppIcon size={26} color="#fff" />
          </div>
          <div style={{ flex:'1 1 220px',minWidth:0 }}>
            <h3 style={{ margin:0,fontSize:14,fontWeight:800,color:T.textMain }}>Emergency Contact</h3>
            <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Need immediate assistance? Chat with us instantly on WhatsApp.</p>
            <p style={{ margin:'6px 0 0',fontSize:13,fontWeight:800,color:T.textMain,letterSpacing:'0.02em' }}>+91 91778 47799</p>
          </div>
          <a href="https://wa.me/919177847799" target="_blank" rel="noopener noreferrer"
            style={{ flexShrink:0,display:'inline-flex',alignItems:'center',gap:8,padding:'10px 18px',borderRadius:10,background:WA_GREEN,color:'#fff',fontSize:13,fontWeight:800,textDecoration:'none',boxShadow:`0 4px 14px ${WA_GREEN}55` }}>
            <WhatsAppIcon size={18} color="#fff" /> Chat on WhatsApp
          </a>
        </div>
      </Card>

      <Card style={{ padding:'16px 20px' }}>
        <div style={{ display:'flex',alignItems:'center',gap:12 }}>
          <div style={{ width:44,height:44,borderRadius:14,background:T.grad1,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22 }}><Icon name="chat" size={22} /></div>
          <div>
            <h2 style={{ margin:0,fontSize:15,fontWeight:800 }}>Customer Support</h2>
            <p style={{ margin:0,fontSize:12,color: conv?.queued ? T.warning : conv?.agentName ? T.success : T.textMuted }}>
              {conv?.queued
                ? 'No support member is currently available. Your request has been queued.'
                : conv?.agentName
                ? `Assigned to ${conv.agentName}`
                : 'Chat with our support team in real time'}
            </p>
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
          {messages.map((m, i)=>{
            const mine = m.sender === 'MERCHANT';
            const prev = messages[i-1];
            const showSep = i === 0 || chatDateLabel(prev.createdAt) !== chatDateLabel(m.createdAt);
            return (
              <React.Fragment key={m.id}>
                {showSep && (
                  <div style={{ alignSelf:'center',background:T.canvas,color:T.textMuted,fontSize:10,fontWeight:700,padding:'3px 12px',borderRadius:12,margin:'4px 0' }}>{chatDateLabel(m.createdAt)}</div>
                )}
                <div style={{ display:'flex',justifyContent:mine?'flex-end':'flex-start' }}>
                  <div style={{ maxWidth:'75%',padding:'10px 14px',borderRadius:mine?'16px 16px 4px 16px':'16px 16px 16px 4px',background:mine?T.grad1:T.canvas,color:mine?'#fff':T.textMain,fontSize:13,lineHeight:1.5 }}>
                    {!mine && <p style={{ margin:'0 0 2px',fontSize:10,fontWeight:800,color:T.blue }}>{m.senderName}</p>}
                    {m.content && <span style={{ whiteSpace:'pre-wrap',wordBreak:'break-word' }}>{m.content}</span>}
                    <ChatAttachment msg={m} mine={mine} />
                    <p style={{ margin:'3px 0 0',fontSize:9,opacity:0.6,textAlign:'right' }}>{chatTime(m.createdAt)}</p>
                  </div>
                </div>
              </React.Fragment>
            );
          })}
          <div ref={bottomRef}/>
        </div>
        <div style={{ padding:'12px 16px',borderTop:`1px solid ${T.border}`,display:'flex',gap:10,alignItems:'center' }}>
          <input ref={fileRef} type="file" accept={CHAT_ACCEPT} onChange={onPickFile} style={{ display:'none' }} />
          <button onClick={()=>fileRef.current?.click()} disabled={sending} title="Attach image or document" aria-label="Attach file"
            style={{ width:40,height:40,flexShrink:0,borderRadius:10,border:`1.5px solid ${T.border}`,background:T.canvas,color:T.textMuted,fontSize:18,cursor:sending?'default':'pointer',display:'flex',alignItems:'center',justifyContent:'center' }}>{sending ? <Icon name="pending" size={18} /> : <Icon name="attach" size={18} />}</button>
          <input value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>e.key==='Enter'&&send()}
            placeholder={sending ? 'Sending attachment…' : 'Type a message...'} disabled={sending}
            style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:12,fontSize:13,outline:'none',fontFamily:'inherit',color:T.textMain,background:T.canvas }}/>
          <Btn onClick={send} disabled={!input.trim()||sending} style={{ borderRadius:12 }}><Icon name="send" size={14} /> Send</Btn>
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
  const [form, setForm] = useState({ current:'', next:'', confirm:'' });
  const [avatar, setAvatar] = useState<string | null>(user.avatar || null);
  const [waEnabled, setWaEnabled] = useState(user.whatsappEnabled !== false);
  const [saving, setSaving] = useState(false);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  // Dedicated "Contact Details" editor — email + phone only, separate from the general profile edit.
  const [contactEdit, setContactEdit] = useState(false);
  const [contactForm, setContactForm] = useState({ email:user.email, phone:user.phone || '' });
  const [savingContact, setSavingContact] = useState(false);
  const setContact = (k: 'email'|'phone', v: string) => setContactForm(f => ({...f,[k]:v}));
  const openContactEdit = () => { setContactForm({ email:user.email, phone:user.phone || '' }); setContactEdit(true); };
  const saveContact = async () => {
    setSavingContact(true);
    try {
      const updated = await userAPI.updateProfile({
        email: contactForm.email !== user.email ? contactForm.email : undefined,
        phone: contactForm.phone.trim() !== (user.phone || '') ? contactForm.phone.trim() : undefined,
      });
      updateUser({ email: updated.email, phone: updated.phone });
      showToast('Contact details updated');
      setContactEdit(false);
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to update contact details','error');
    } finally {
      setSavingContact(false);
    }
  };
  // WhatsApp notifications apply to internal users only (Admin / Supervisor / Manager).
  const waEligible = user.role === 'ADMIN' || (user.role === 'MERCHANT' && ['SUPERVISOR','MANAGER'].includes(String(user.merchantRole||'').toUpperCase()));

  const onAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { showToast('Image must be under 2 MB', 'error'); return; }
    setAvatar(await fileToDataUrl(f));
  };

  const openEdit = () => { setForm({ current:'', next:'', confirm:'' }); setAvatar(user.avatar || null); setWaEnabled(user.whatsappEnabled !== false); setEdit(true); };

  const save = async () => {
    if(form.next && form.next !== form.confirm){ showToast('Passwords do not match','error'); return; }
    setSaving(true);
    try {
      const avatarChanged = avatar !== (user.avatar || null);
      const updated = await userAPI.updateProfile({
        new_password: form.next || undefined,
        current_password: form.current || undefined,
        avatar: avatarChanged ? (avatar || '') : undefined,
        whatsappEnabled: waEligible && waEnabled !== (user.whatsappEnabled !== false) ? waEnabled : undefined,
      });
      updateUser({ avatar: updated.avatar, whatsappEnabled: updated.whatsappEnabled });
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
          <Btn size="sm" variant="ghost" onClick={openEdit}><Icon name="edit" size={13} /> Edit</Btn>
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

      {/* Dedicated Contact Details section — email + phone, with its own editor */}
      <Card style={{ padding:'20px 24px', marginTop:16 }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:8 }}>
          <h3 style={{ margin:0, fontSize:14, fontWeight:800, color:T.textMain }}>Contact Details</h3>
          <Btn size="sm" variant="ghost" onClick={openContactEdit}><Icon name="edit" size={13} /> Edit</Btn>
        </div>
        {([['Email ID', user.email],['Phone', user.phone || '—']] as [string,string][]).map(([k,v])=>(
          <div key={k} style={{ display:'flex',justifyContent:'space-between',padding:'11px 0',borderBottom:`1px solid ${T.borderLight}`,gap:12 }}>
            <span style={{ fontSize:12,color:T.textMuted }}>{k}</span>
            <span style={{ fontSize:13,fontWeight:700,color:T.textMain,textAlign:'right' }}>{v}</span>
          </div>
        ))}
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
          <div style={{ borderTop:`1px solid ${T.border}`,margin:'4px 0 14px',paddingTop:14 }}>
            <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Change Password</p>
            <Input label="Current Password" type="password" value={form.current} onChange={e=>set('current',e.target.value)} placeholder="Required to change password"/>
            <Input label="New Password" type="password" value={form.next} onChange={e=>set('next',e.target.value)} placeholder="Leave blank to keep current"/>
            <Input label="Confirm New Password" type="password" value={form.confirm} onChange={e=>set('confirm',e.target.value)} placeholder="Re-enter new password"/>
            {form.next && <p style={{ fontSize:11,color:T.textMuted,margin:'-6px 0 0' }}>At least 8 characters with an uppercase letter, a lowercase letter, a number and a special character.</p>}
          </div>
          {waEligible && (
            <div style={{ borderTop:`1px solid ${T.border}`,margin:'4px 0 14px',paddingTop:14 }}>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Notifications</p>
              <div style={{ display:'flex',alignItems:'center',justifyContent:'space-between',padding:'8px 12px',background:T.canvas,borderRadius:10,border:`1px solid ${T.border}` }}>
                <div>
                  <p style={{ margin:0,fontSize:13,fontWeight:700,color:T.textMain }}>Receive WhatsApp Notifications</p>
                  <p style={{ margin:'2px 0 0',fontSize:11,color:T.textMuted }}>{user.phone ? `Sent to ${user.phone}` : 'Add a phone number to receive these'}</p>
                </div>
                <div onClick={()=>setWaEnabled(v=>!v)} role="switch" aria-checked={waEnabled}
                  style={{ width:46,height:26,borderRadius:13,background:waEnabled?T.success:T.border,position:'relative',cursor:'pointer',transition:'background 0.2s',flexShrink:0 }}>
                  <div style={{ position:'absolute',top:3,left:waEnabled?23:3,width:20,height:20,borderRadius:'50%',background:'#fff',boxShadow:'0 1px 3px rgba(0,0,0,0.3)',transition:'left 0.2s' }}/>
                </div>
              </div>
            </div>
          )}
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={save} disabled={saving}>{saving?'Saving...':'Save Changes'}</Btn>
            <Btn variant="secondary" onClick={()=>setEdit(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}

      {contactEdit && (
        <Modal title="Edit Contact Details" onClose={()=>setContactEdit(false)}>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 14px' }}>Update the email and phone number for your account. Your phone number is where WhatsApp transaction notifications are sent.</p>
          <Input label="Email ID" type="email" value={contactForm.email} onChange={e=>setContact('email',e.target.value)} placeholder="you@company.com"/>
          <Input label="Phone Number" type="tel" value={contactForm.phone} onChange={e=>setContact('phone',e.target.value)} placeholder="+91 98123 45678"/>
          <p style={{ fontSize:11,color:T.textMuted,margin:'-6px 0 14px' }}>Include the country code, e.g. +91 for India.</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={saveContact} disabled={savingContact}>{savingContact?'Saving...':'Save Changes'}</Btn>
            <Btn variant="secondary" onClick={()=>setContactEdit(false)}>Cancel</Btn>
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

export const NEWS_CATEGORIES = ['System Updates', 'Security Alerts', 'Maintenance', 'Product Updates', 'Announcements'];
const CAT_COLOR: Record<string, string> = {
  'System Updates': T.info, 'Security Alerts': T.danger, 'Maintenance': T.warning,
  'Product Updates': T.success, 'Announcements': T.blue,
  // legacy / migrated-from-blog categories keep a sensible colour too
  'Offers': T.warning, 'Alerts': T.danger, 'Release Notes': T.cyan,
};
const catColor = (c?: string) => CAT_COLOR[c || ''] || SECTION_COLOR[c || ''] || T.blue;

// ─── Super-Admin News editor (create / edit) ───────────────────────────────────
const NewsEditor: React.FC<{ post: NewsPost | null; onClose: () => void; onSaved: () => void }> = ({ post, onClose, onSaved }) => {
  const { showToast } = useToast();
  const [title, setTitle] = useState(post?.title || '');
  const [category, setCategory] = useState(post?.category || 'Announcements');
  const [body, setBody] = useState(post?.body || '');
  const [image, setImage] = useState<string | null>(post?.image ?? null);
  const [publishDate, setPublishDate] = useState(post?.publishDate || '');
  const [featured, setFeatured] = useState(post?.featured || false);
  const [published, setPublished] = useState(post?.published ?? true);
  const [saving, setSaving] = useState(false);

  const onImg = async (e: React.ChangeEvent<HTMLInputElement>) => { const f = e.target.files?.[0]; if (f) setImage(await fileToDataUrl(f)); };
  const save = async () => {
    if (!title.trim()) { showToast('Title is required', 'error'); return; }
    setSaving(true);
    try {
      const payload = { category, title: title.trim(), body, image: image ?? null, published, featured, publish_date: publishDate || undefined };
      if (post) await newsAPI.update(post.id, payload); else await newsAPI.create(payload);
      showToast(post ? 'News updated' : 'News published'); onSaved(); onClose();
    } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to save news', 'error'); }
    finally { setSaving(false); }
  };
  const lbl = { display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, margin: '0 0 6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' };

  return (
    <Modal title={post ? 'Edit News' : 'New News Post'} onClose={onClose} wide>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
        <Input label="Title" value={title} onChange={e => setTitle(e.target.value)} placeholder="News headline" required />
        <Sel label="Category" value={category} onChange={e => setCategory(e.target.value)} options={NEWS_CATEGORIES.map(c => ({ value: c, label: c }))} />
      </div>
      <div style={{ marginBottom: 14 }}>
        <label style={lbl}>Description</label>
        <textarea value={body} onChange={e => setBody(e.target.value)} placeholder="News content / description"
          style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, color: T.textMain, background: T.surface, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical', minHeight: 120 }} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px', alignItems: 'end', marginBottom: 14 }}>
        <Input label="Publish Date" type="date" value={publishDate} onChange={e => setPublishDate(e.target.value)} />
        <div>
          <label style={lbl}>Image</label>
          <input type="file" accept="image/*" onChange={onImg} style={{ fontSize: 12 }} />
        </div>
      </div>
      {image && <img src={image} alt="" style={{ display: 'block', maxWidth: '100%', maxHeight: 200, objectFit: 'contain', borderRadius: 10, border: `1px solid ${T.border}`, margin: '0 0 14px', background: T.canvas }} />}
      <div style={{ display: 'flex', gap: 20, marginBottom: 16 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: T.textMain, cursor: 'pointer' }}>
          <input type="checkbox" checked={published} onChange={e => setPublished(e.target.checked)} /> Published
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: T.textMain, cursor: 'pointer' }}>
          <input type="checkbox" checked={featured} onChange={e => setFeatured(e.target.checked)} /> Featured
        </label>
      </div>
      <Btn full onClick={save} disabled={saving || !title.trim()}>{saving ? 'Saving…' : (post ? 'Save Changes' : 'Publish News')}</Btn>
    </Modal>
  );
};

export const NewsPage: React.FC<{ user: User }> = ({ user }) => {
  const isSA = user.role === 'SUPER_ADMIN';
  const { showToast } = useToast();
  const [posts, setPosts] = useState<NewsPost[]>([]);
  const [loading, setLoading] = useState(true);
  const [selId, setSelId] = useState<number | null>(null);
  const [q, setQ] = useState('');
  const [cat, setCat] = useState('');
  const [editing, setEditing] = useState<NewsPost | null | undefined>(undefined); // undefined = closed, null = new
  const viewed = useRef<Set<number>>(new Set());

  const load = (resetSel = false) => newsAPI.list()
    .then(rows => { setPosts(rows); setSelId(prev => (!resetSel && prev && rows.some(r => r.id === prev)) ? prev : (rows[0]?.id ?? null)); })
    .catch(() => setPosts([])).finally(() => setLoading(false));
  useEffect(() => { load(true); }, []);
  usePoll(() => { newsAPI.list().then(setPosts).catch(() => {}); });

  // Count a read once per session when a post becomes the selected article.
  useEffect(() => { if (selId && !viewed.current.has(selId)) { viewed.current.add(selId); newsAPI.view(selId); } }, [selId]);

  const selected = posts.find(p => p.id === selId) || null;
  const categories = Array.from(new Set(posts.map(p => p.category).filter(Boolean)));
  const matches = (p: NewsPost) =>
    (!q || p.title.toLowerCase().includes(q.toLowerCase()) || (p.body || '').toLowerCase().includes(q.toLowerCase())) &&
    (!cat || p.category === cat);
  const filtered = posts.filter(matches);
  const latest = [...filtered].sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || '')).slice(0, 6);
  const recent = [...filtered].filter(p => p.updatedAt).sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || '')).slice(0, 6);
  const featured = filtered.filter(p => p.featured).slice(0, 6);
  // Only count actually-viewed articles; an empty list shows a clear message (not zero-view rows).
  const mostViewed = [...filtered].filter(p => (p.views || 0) > 0).sort((a, b) => (b.views || 0) - (a.views || 0)).slice(0, 6);

  const remove = async (p: NewsPost) => {
    if (!window.confirm(`Delete "${p.title}"? This cannot be undone.`)) return;
    try { await newsAPI.remove(p.id); showToast('News deleted'); load(true); } catch { showToast('Failed to delete', 'error'); }
  };
  const togglePublish = async (p: NewsPost) => {
    try { await newsAPI.update(p.id, { category: p.category, title: p.title, body: p.body, image: p.image, published: !p.published, featured: p.featured, publish_date: p.publishDate || undefined }); showToast(p.published ? 'Unpublished' : 'Published'); load(); }
    catch { showToast('Failed to update', 'error'); }
  };

  // Super-Admin overview cards (over all news, not the filtered view).
  const stats = {
    total: posts.length,
    published: posts.filter(p => p.published).length,
    featured: posts.filter(p => p.featured).length,
    views: posts.reduce((a, p) => a + (p.views || 0), 0),
  };

  if (loading) return <LoadingScreen label="Loading news…" />;

  const sideCard = (title: string, items: NewsPost[], meta: (p: NewsPost) => string, emptyMsg = 'Nothing here yet.') => (
    <Card style={{ padding: 14, marginBottom: 12 }}>
      <p style={{ margin: '0 0 10px', fontSize: 12, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{title}</p>
      {items.length === 0 ? <p style={{ fontSize: 12, color: T.textMuted, margin: 0 }}>{emptyMsg}</p> : items.map(p => (
        <div key={p.id} onClick={() => setSelId(p.id)} className="c5-row-hover"
          style={{ display: 'flex', gap: 8, padding: '7px 6px', borderRadius: 8, cursor: 'pointer', borderLeft: `3px solid ${p.id === selId ? catColor(p.category) : 'transparent'}` }}>
          {p.image && <img src={p.image} alt="" style={{ width: 38, height: 38, borderRadius: 6, objectFit: 'cover', flexShrink: 0, border: `1px solid ${T.border}` }} />}
          <div style={{ minWidth: 0 }}>
            <p style={{ margin: 0, fontSize: 12.5, fontWeight: 700, color: T.textMain, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.title}</p>
            <p style={{ margin: '2px 0 0', fontSize: 10.5, color: T.textMuted }}>{meta(p)}</p>
          </div>
        </div>
      ))}
    </Card>
  );

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 800 }}>News &amp; Updates</h2>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>Platform announcements and product updates{isSA ? ' — you can create, edit and publish news.' : ''}</p>
        </div>
        <Input value={q} onChange={e => setQ(e.target.value)} icon="search" placeholder="Search news" style={{ marginBottom: 0, width: 200 }} />
        <Sel value={cat} onChange={e => setCat(e.target.value)} style={{ marginBottom: 0, width: 180 }}
          options={[{ value: '', label: 'All Categories' }, ...categories.map(c => ({ value: c, label: c }))]} />
        {isSA && <Btn onClick={() => setEditing(null)}>＋ New Post</Btn>}
      </div>

      {isSA && (
        <div className="c5-stagger" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))', gap: 12, marginBottom: 16 }}>
          <StatCard icon="news" label="Total News" value={String(stats.total)} color={T.blue} />
          <StatCard icon="verified" label="Published News" value={String(stats.published)} color={T.success} />
          <StatCard icon="star" label="Featured News" value={String(stats.featured)} color={T.warning} />
          <StatCard icon="view" label="Total Views" value={String(stats.views)} color={T.info} />
        </div>
      )}

      {posts.length === 0 ? (
        <Card style={{ padding: 40, textAlign: 'center' }}>
          <div style={{ fontSize: 40, marginBottom: 10 }}><Icon name="news" size={40} /></div>
          <p style={{ fontWeight: 800, color: T.textMain, margin: '0 0 4px' }}>No news yet</p>
          <p style={{ fontSize: 12, color: T.textMuted, margin: 0 }}>{isSA ? 'Create the first post with “＋ New Post”.' : 'Announcements and updates will appear here.'}</p>
        </Card>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,2fr) minmax(260px,1fr)', gap: 18, alignItems: 'start' }}>
          {/* Left — selected article detail */}
          <div>
            {selected ? (
              <Card style={{ padding: 24 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
                  <span style={{ padding: '3px 12px', borderRadius: 20, fontSize: 11, fontWeight: 800, background: `${catColor(selected.category)}22`, color: catColor(selected.category) }}>{selected.category}</span>
                  {selected.featured && <span style={{ fontSize: 11, fontWeight: 700, color: T.warning }}><Icon name="star" size={12} /> Featured</span>}
                  {!selected.published && <span style={{ fontSize: 11, fontWeight: 700, color: T.textMuted }}>(Draft)</span>}
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: T.textMuted }}><Icon name="view" size={12} /> {selected.views} views</span>
                </div>
                {selected.image && <img src={selected.image} alt="" style={{ display: 'block', width: '100%', maxHeight: 320, objectFit: 'contain', borderRadius: 12, border: `1px solid ${T.border}`, margin: '0 0 14px', background: T.canvas }} />}
                <h1 style={{ margin: '0 0 8px', fontSize: 22, fontWeight: 800, color: T.textMain, lineHeight: 1.3 }}>{selected.title}</h1>
                <p style={{ margin: '0 0 16px', fontSize: 12, color: T.textMuted }}>
                  Published {selected.publishDate ? formatDate(selected.publishDate) : formatDate(selected.createdAt)} · By {selected.author}
                </p>
                <p style={{ margin: 0, fontSize: 14, color: T.textMain, lineHeight: 1.7, whiteSpace: 'pre-line' }}>{selected.body}</p>

                {isSA && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 20, paddingTop: 16, borderTop: `1px solid ${T.border}` }}>
                    <Btn size="sm" variant="secondary" onClick={() => setEditing(selected)}><Icon name="edit" size={13} /> Edit</Btn>
                    <Btn size="sm" variant="secondary" onClick={() => togglePublish(selected)}>{selected.published ? '⤓ Unpublish' : '⤒ Publish'}</Btn>
                    <Btn size="sm" variant="danger" onClick={() => remove(selected)}><Icon name="delete" size={13} /> Delete</Btn>
                  </div>
                )}
              </Card>
            ) : (
              <Card style={{ padding: 40, textAlign: 'center', color: T.textMuted }}>No news matches your search.</Card>
            )}
          </div>

          {/* Right — always-visible navigation sidebar */}
          <div>
            <Card style={{ padding: 14, marginBottom: 12 }}>
              <p style={{ margin: '0 0 10px', fontSize: 12, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Categories</p>
              {NEWS_CATEGORIES.map(c => {
                const n = posts.filter(p => p.category === c).length;
                const on = cat === c;
                return (
                  <div key={c} onClick={() => setCat(on ? '' : c)} className="c5-row-hover"
                    style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 6px', borderRadius: 8, cursor: 'pointer', background: on ? `${catColor(c)}18` : 'transparent' }}>
                    <span style={{ width: 9, height: 9, borderRadius: '50%', background: catColor(c), flexShrink: 0 }} />
                    <span style={{ flex: 1, fontSize: 12.5, fontWeight: on ? 800 : 600, color: T.textMain }}>{c}</span>
                    <span style={{ fontSize: 11, fontWeight: 700, color: T.textMuted }}>{n}</span>
                  </div>
                );
              })}
            </Card>
            {sideCard('Latest News', latest, p => `${p.category} · ${formatDate(p.publishDate || p.createdAt)}`)}
            {sideCard('Recent Updates', recent, p => `Updated ${formatDate(p.updatedAt || p.createdAt)}`)}
            {sideCard('Featured News', featured, p => p.category)}
            {sideCard('Most Viewed', mostViewed, p => `👁 ${p.views} views`, 'No viewed articles yet')}
          </div>
        </div>
      )}

      {editing !== undefined && <NewsEditor post={editing} onClose={() => setEditing(undefined)} onSaved={() => load()} />}
    </div>
  );
};
