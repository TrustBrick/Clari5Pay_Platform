import React, { useState, useEffect } from 'react';
import { T } from '../utils/theme';
import { fmt, typeLabel, depositTypeLabel, depositDetailLabel, memberLabel, fileToDataUrl, COUNTRY_CODES, formatDateTime, merchantRoleLabel, rolesForProfile, downloadDataUrl, downloadText, passwordPolicyError, PASSWORD_POLICY_TEXT } from '../utils/helpers';
import { accountToPng } from '../utils/image';
import { Card, StatCard, Btn, Input, Sel, RiskBadge, Badge, MiniBar, StatusChart, LoadingScreen, ReasonModal, Modal, BankNamesDatalist } from '../components/UI';
import { lookupIfsc, isValidIfsc, BANK_NAMES } from '../utils/ifsc';
import TxTable from '../components/TxTable';
import { TxExportButton, exportTransactionsPdf } from '../components/TxExport';
import TxSearchFilters from '../components/TxSearchFilters';
import { exportTransactionsXlsx, downloadXlsx } from '../utils/xlsx';
import { ProofGallery } from './MerchantPages';
import { usePoll } from '../utils/usePoll';
import { transactionAPI, userAPI, accountAPI, adminUpiAPI, systemLogAPI, auditLogAPI, newsAPI } from '../services/api';
import type { TxQuery } from '../services/api';
import type { SystemLogEntry, AuditLogEntry, NewsPost } from '../types';
import { useToast } from '../context/ToastContext';
import type { Transaction, User, Account, AccountBalance, MerchantBalance, MerchantStats, AdminUpi } from '../types';

// Actual tx.type is always one of the *_REQUEST values, so only these match the exact-type filter.
const REQUEST_TYPES = ['DEPOSIT_REQUEST', 'WITHDRAWAL_REQUEST', 'SETTLEMENT_REQUEST'];
const REQUEST_STATUSES = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'PENDING_APPROVAL', 'SUPERVISOR_REVIEW', 'MANAGER_REVIEW', 'SLIP_SUBMITTED', 'RESUBMITTED', 'REJECTED', 'DEPOSITED', 'COMPLETED', 'CANCELLED'];

// Active (pending) workflow statuses — anything not yet completed/deposited/cancelled.
const ACTIVE_STATUSES = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'PENDING_APPROVAL', 'SUPERVISOR_REVIEW', 'MANAGER_REVIEW', 'SLIP_SUBMITTED', 'RESUBMITTED', 'PENDING', 'ADMIN_APPROVED'];
const isActive = (s: string) => ACTIVE_STATUSES.includes(s);

const roleLabel = (r?: string | null) => merchantRoleLabel(r) || '—';

// Withdrawal payout modes + a readable label for the JSON detail keys (upiId → UPI Id, etc.).
const PAYOUT_MODE_LABELS: Record<string, string> = { BANK: 'Bank Transfer', UPI: 'UPI', CASH: 'Cash', CRYPTO: 'Crypto (USDT)' };
const prettyKey = (k: string) =>
  k.replace(/([A-Z])/g, ' $1').replace(/^./, c => c.toUpperCase())
   .replace(/\bUpi\b/, 'UPI').replace(/\bIfsc\b/, 'IFSC').trim();

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
  const depType = (tx.depositType || '').toUpperCase();
  const isCashDeposit = isDeposit && depType === 'CASH';   // "Request Additional Information" workflow
  // Withdrawal payout mode drives what proof the agent must capture (Crypto → Hash; Cash → image only).
  const payoutMode = (tx.payoutMode || 'BANK').toUpperCase();
  const isCryptoPayout = payoutMode === 'CRYPTO';
  const isCashPayout = payoutMode === 'CASH';
  const needUtr = !isCashPayout;
  const needReceipt = !isCryptoPayout;
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountRef, setAccountRef] = useState('');
  const [reusedRef, setReusedRef] = useState('');
  const [sendVia, setSendVia] = useState<'BANK' | 'UPI'>('BANK');   // deposit: send a bank account or a linked UPI
  const [linkedUpis, setLinkedUpis] = useState<AdminUpi[]>([]);
  const [upiId, setUpiId] = useState('');
  const [receipt, setReceipt] = useState<string | null>(null);
  const [bankImage, setBankImage] = useState<string | null>(null);   // optional custom bank-details image
  const [payUtr, setPayUtr] = useState('');
  const [saving, setSaving] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [rejecting, setRejecting] = useState(false);
  const [riskConfirm, setRiskConfirm] = useState(false);
  // Proof/receipt images are omitted from list payloads; fetch them when the modal opens.
  const [imgs, setImgs] = useState<{ adminProof?: string | null; adminBankImage?: string | null; merchantProof?: string | null; merchantProofs?: string[] | null }>({ adminProof: tx.adminProof, adminBankImage: tx.adminBankImage, merchantProof: tx.merchantProof, merchantProofs: tx.merchantProofs });
  // Full record incl. proof images + the review-gate workflow trail; also records an
  // "Admin Viewed" audit entry (the admin is opening the request).
  const [record, setRecord] = useState<Transaction>(tx);
  useEffect(() => {
    transactionAPI.getDetail(tx.id).then(d => { setImgs({ adminProof: d.adminProof, adminBankImage: d.adminBankImage, merchantProof: d.merchantProof, merchantProofs: d.merchantProofs }); setRecord(d); }).catch(()=>{});
    transactionAPI.recordView(tx.id);
  }, [tx.id]);

  // Admin steps. Deposits reach the admin as SLIP_SUBMITTED only after Supervisor approval;
  // withdrawals/settlements reach the admin as SLIP_SUBMITTED only after Manager approval
  // (legacy withdrawals may still sit in ACCOUNT_REQUESTED — keep them payable).
  const chooseStep = canAct && isDeposit && tx.status === 'ACCOUNT_REQUESTED'; // pick account → auto PNG
  const depositDoneStep = canAct && isDeposit && tx.status === 'SLIP_SUBMITTED'; // review slip → Deposited
  const payStep = canAct && !isDeposit && (tx.status === 'SLIP_SUBMITTED' || tx.status === 'ACCOUNT_REQUESTED'); // pay merchant → upload receipt → Completed
  const active = ['ACCOUNT_REQUESTED', 'ACCOUNT_SUBMITTED', 'SLIP_SUBMITTED'].includes(tx.status);
  const canReject = canAct && active;
  const title = chooseStep ? 'Choose Account' : depositDoneStep ? 'Review Payment Slip' : payStep ? 'Pay & Complete' : 'Request Details';

  const doReject = async () => {
    if (!rejectReason.trim()) { showToast('Enter a rejection reason', 'error'); return; }
    setSaving(true);
    try {
      await transactionAPI.reject(tx.id, rejectReason.trim());
      showToast(`${tx.ref} rejected`);
      onDone?.(); onClose();
    } catch { showToast('Failed to reject', 'error'); }
    finally { setSaving(false); }
  };

  // Wrong/unverifiable slip → send the deposit back so the merchant re-uploads the correct proof.
  const recheck = async () => {
    setSaving(true);
    try {
      await transactionAPI.recheck(tx.id);
      showToast(`${tx.ref}: sent back to merchant for re-upload`);
      onDone?.(); onClose();
    } catch { showToast('Failed to request re-upload', 'error'); }
    finally { setSaving(false); }
  };

  // Payment still not received after re-upload → flag the member HIGH RISK (and reject).
  const flagRisk = async () => {
    setSaving(true);
    try {
      await transactionAPI.flagRisk(tx.id);
      showToast(`Member ${tx.memberId || tx.ref} flagged HIGH RISK`);
      onDone?.(); onClose();
    } catch { showToast('Failed to flag high risk', 'error'); }
    finally { setSaving(false); }
  };

  useEffect(() => {
    if (!chooseStep) return;
    accountAPI.list().then(a => {
      const active = a.filter(x => (x.status || '').toUpperCase() === 'ACTIVE');
      setAccounts(active);
      // Reuse the account previously assigned to this Membership ID (agent can still change it).
      if (tx.memberId) {
        accountAPI.lastForMember(tx.memberId).then(r => {
          if (r.referenceNumber && active.some(x => x.referenceNumber === r.referenceNumber)) {
            setAccountRef(r.referenceNumber);
            setReusedRef(r.referenceNumber);
          }
        }).catch(()=>{});
      }
    }).catch(()=>{});
  }, [chooseStep, tx.memberId]);

  // Load UPIs that belong to an account — the agent can send one instead of a bank account.
  useEffect(() => {
    if (!chooseStep) return;
    adminUpiAPI.listActive().then(rows => setLinkedUpis(rows.filter(u => u.accountRef))).catch(()=>{});
  }, [chooseStep]);

  const onReceipt = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) setReceipt(await fileToDataUrl(f));
  };

  // Optional custom Bank-Details image (JPG/JPEG/PNG/WEBP). When set, it overrides the auto card.
  const BANK_IMG_TYPES = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp'];
  const onBankImage = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (!BANK_IMG_TYPES.includes(f.type)) { showToast('Allowed image types: JPG, JPEG, PNG, WEBP', 'error'); return; }
    setBankImage(await fileToDataUrl(f));
  };

  const sendAccount = async () => {
    const acc = accounts.find(a => a.referenceNumber === accountRef);
    if (!acc && !bankImage) { showToast('Select an account or upload a bank details image', 'error'); return; }
    setSaving(true);
    try {
      if (bankImage) {
        // Custom image becomes the official bank details — the auto card is not generated.
        await transactionAPI.submitAccount(tx.id, { adminBankImage: bankImage, adminRef: acc?.referenceNumber });
      } else {
        const png = accountToPng(acc!);
        const summary = [
          `Account Name: ${acc!.accountName}`,
          `Bank: ${acc!.bankName}`,
          `A/C: ${acc!.accountNumber}`,
          `IFSC: ${acc!.ifscCode}`,
          `Branch: ${acc!.branch}`,
        ].join('\n');
        await transactionAPI.submitAccount(tx.id, { adminProof: png, adminBankDetails: summary, adminRef: acc!.referenceNumber });
      }
      showToast(`${tx.ref} — account details sent`);
      onDone?.(); onClose();
    } catch { showToast('Failed to send account', 'error'); }
    finally { setSaving(false); }
  };

  // Send a linked UPI — the deposit is credited to that UPI's parent account.
  const sendUpi = async () => {
    const u = linkedUpis.find(x => x.upiId === upiId);
    if (!u) { showToast('Select a UPI ID', 'error'); return; }
    setSaving(true);
    try {
      await transactionAPI.submitAccount(tx.id, { adminUpiId: u.upiId, adminRef: u.accountRef || undefined });
      showToast(`${tx.ref} — UPI sent`);
      onDone?.(); onClose();
    } catch { showToast('Failed to send UPI', 'error'); }
    finally { setSaving(false); }
  };

  const complete = async (withReceipt: boolean) => {
    if (withReceipt && needReceipt && !receipt) { showToast('Upload the payment proof', 'error'); return; }
    if (withReceipt && needUtr && !payUtr.trim()) { showToast(isCryptoPayout ? 'Enter the transaction hash' : 'Enter the UTR number', 'error'); return; }
    setSaving(true);
    try {
      await transactionAPI.markDone(tx.id, withReceipt ? { adminProof: receipt || undefined, adminUtr: payUtr.trim() || undefined } : undefined);
      showToast(`${tx.ref} ${isDeposit ? 'deposited' : 'completed'}`);
      onDone?.(); onClose();
    } catch { showToast('Failed to complete', 'error'); }
    finally { setSaving(false); }
  };

  return (
    <Modal title={`${title} — ${tx.ref}`} onClose={onClose} wide>
      <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 24px' }}>
        <div>
          <Row k="Receiver" v={tx.merchant} />
          <Row k="Type" v={typeLabel(tx.type)} />
          <Row k="Amount" v={fmt(tx.amount)} />
          <Row k="Status" v={<Badge status={tx.status} type={tx.type} viewerRole="ADMIN" />} />
          {(tx.memberId || tx.member) && <Row k="Membership - Member" v={memberLabel(tx.memberId, tx.member)} />}
          {tx.depositType && <Row k="Deposit Type" v={depositTypeLabel(tx.depositType)} />}
          {isDeposit && tx.depositDetails && Object.entries(tx.depositDetails).map(([k, v]) =>
            v ? <Row key={k} k={depositDetailLabel(k)} v={String(v)} /> : null)}
          {tx.utr && <Row k="UTR Number" v={tx.utr} />}
          {tx.riskAnalysis && <Row k="Risk Analysis" v="Requested" />}
          <Row k="Date" v={`${tx.date} ${tx.time}`} />
          {tx.notes && (
            <div style={{ marginTop:10,background:T.warningBg,borderRadius:10,padding:'8px 12px' }}>
              <p style={{ fontSize:10,fontWeight:800,color:T.warning,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 4px' }}>Merchant Note</p>
              <p style={{ fontSize:12,color:T.textMain,margin:0 }}>{tx.notes}</p>
            </div>
          )}
          {tx.status === 'CANCELLED' && (
            <div style={{ marginTop:10,background:T.dangerBg,borderRadius:10,padding:'8px 12px' }}>
              <p style={{ fontSize:10,fontWeight:800,color:T.danger,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 4px' }}>Cancellation Reason</p>
              <p style={{ fontSize:12,color:T.textMain,margin:0 }}>{tx.cancelReason || '—'}</p>
              {(tx.cancelledBy || tx.cancelledAt) && (
                <p style={{ fontSize:11,color:T.textMuted,margin:'4px 0 0' }}>
                  Cancelled by {tx.cancelledBy || 'merchant'}{tx.cancelledAt ? ` · ${new Date(tx.cancelledAt).toLocaleString('en-IN')}` : ''}
                </p>
              )}
            </div>
          )}
        </div>
        <div>
          {isDeposit ? (
            <>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Account Sent</p>
              {(tx.adminBankDetails || tx.adminUpiId || tx.adminRef || tx.hasAdminBankImage) ? (
                <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12 }}>
                  {tx.adminUpiId && <Row k="UPI ID" v={tx.adminUpiId} />}
                  {tx.adminBankDetails && <p style={{ margin:0,whiteSpace:'pre-line',lineHeight:1.6,color:T.textMain,fontWeight:600 }}>{tx.adminBankDetails}</p>}
                  {tx.hasAdminBankImage && !tx.adminBankDetails && <p style={{ margin:0,color:T.textMuted }}>Custom bank details image sent.</p>}
                </div>
              ) : <div style={{ padding:24,textAlign:'center',color:T.textMuted,background:T.canvas,borderRadius:10,fontSize:12 }}>Not sent yet</div>}
              {imgs.adminProof && <img src={imgs.adminProof} alt="Account details" style={{ display:'block',width:'100%',height:'auto',objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
              {imgs.adminBankImage && <img src={imgs.adminBankImage} alt="Bank details" style={{ display:'block',width:'100%',height:'auto',objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,marginTop:10,background:T.canvas }} />}
            </>
          ) : (
            <>
              <p style={{ fontSize:12,fontWeight:800,color:T.blue,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Receiver Payout Details (pay here)</p>
              <div style={{ background:T.canvas,borderRadius:10,padding:12,fontSize:12 }}>
                {tx.payoutMode && <Row k="Payout Mode" v={PAYOUT_MODE_LABELS[tx.payoutMode] || tx.payoutMode} />}
                {tx.payoutDetails && Object.keys(tx.payoutDetails).length
                  ? Object.entries(tx.payoutDetails).map(([k,v]) => <Row key={k} k={prettyKey(k)} v={String(v)} />)
                  : <>
                      {tx.accountHolder && <Row k="Account Holder" v={tx.accountHolder} />}
                      {tx.accountNumber && <Row k="Account Number" v={tx.accountNumber} />}
                      {tx.ifsc && <Row k="IFSC" v={tx.ifsc} />}
                      {tx.bank && <Row k="Bank" v={tx.bank} />}
                      {!tx.accountHolder && !tx.accountNumber && !tx.bank && <p style={{ margin:0,color:T.textMuted }}>No payout details provided.</p>}
                    </>}
              </div>
              {imgs.adminProof && <><p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'12px 0 8px' }}>Payment Receipt</p>
                <img src={imgs.adminProof} alt="Receipt" style={{ width:'100%',maxHeight:160,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,background:T.canvas }} /></>}
            </>
          )}
        </div>
      </div>

      {/* Merchant payment slip (deposit) */}
      {isDeposit && (imgs.merchantProof || tx.merchantRef) && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Merchant Payment Slip</p>
          {tx.merchantRef && <Row k="Reference Number" v={tx.merchantRef} />}
          {(() => {
            const list = (imgs.merchantProofs && imgs.merchantProofs.length) ? imgs.merchantProofs : (imgs.merchantProof ? [imgs.merchantProof] : []);
            return list.length ? <ProofGallery srcs={list} /> : null;
          })()}
          {(imgs.merchantProof || tx.merchantRef) && (
            <Btn size="sm" variant="ghost" style={{ marginTop:10 }}
              onClick={() => imgs.merchantProof
                ? downloadDataUrl(imgs.merchantProof, `payment-slip-${tx.ref}.png`)
                : downloadText(`Payment slip — ${tx.ref}\nReference: ${tx.merchantRef}`, `payment-slip-${tx.ref}.txt`)}>
              ⬇ Download Payment Slip
            </Btn>
          )}
        </div>
      )}

      {/* Approval record — Created/Supervisor/Manager/Admin trail + remarks history. */}
      {(record.supervisorName || record.managerName || record.approvedBy || record.processedBy || (record.remarksHistory && record.remarksHistory.length > 0)) && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Approval Record</p>
          <Row k="Created By" v={`${record.merchant}${record.createdAt ? ` · ${formatDateTime(record.createdAt)}` : ''}`} />
          {record.supervisorName && <Row k="Supervisor" v={`${record.supervisorName}${record.supervisorActionAt ? ` · ${formatDateTime(record.supervisorActionAt)}` : ''}`} />}
          {record.managerName && <Row k="Manager" v={`${record.managerName}${record.managerActionAt ? ` · ${formatDateTime(record.managerActionAt)}` : ''}`} />}
          {record.processedBy && <Row k="Admin" v={`${record.processedBy}${record.adminActionAt ? ` · ${formatDateTime(record.adminActionAt)}` : ''}`} />}
          {record.remarksHistory && record.remarksHistory.length > 0 && (
            <div style={{ marginTop:8 }}>
              <p style={{ fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'0 0 6px' }}>Remarks History</p>
              {record.remarksHistory.map((r, i) => (
                <div key={i} style={{ borderLeft:`3px solid ${T.border}`,paddingLeft:10,marginBottom:8 }}>
                  <p style={{ margin:0,fontSize:12,fontWeight:700,color:T.textMain }}>{merchantRoleLabel(r.role) || r.role} · {r.user} — {r.action}</p>
                  <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{r.remark}</p>
                  <p style={{ margin:'2px 0 0',fontSize:10,color:T.textMuted }}>{r.at}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {chooseStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <div style={{ display:'flex',gap:8,marginBottom:12 }}>
            <Btn size="sm" variant={sendVia==='BANK'?'primary':'ghost'} onClick={()=>setSendVia('BANK')}>🏦 Bank Account</Btn>
            <Btn size="sm" variant={sendVia==='UPI'?'primary':'ghost'} onClick={()=>setSendVia('UPI')}>📲 UPI ID</Btn>
          </div>
          {sendVia === 'BANK' ? (
            <>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Select an account to send ({accounts.length} active)</p>
              <Sel label="Account" value={accountRef} onChange={e=>setAccountRef(e.target.value)}
                options={[{ value:'', label:'— Select an account —' }, ...accounts.map(a => ({ value:a.referenceNumber, label:`${a.accountName} — ${a.bankName} (A/C ${a.accountNumber})` }))]} />
              {reusedRef && accountRef === reusedRef && (
                <p style={{ fontSize:11,color:T.success,margin:'-8px 0 10px',fontWeight:600 }}>↻ Reused from Member {tx.memberId}'s previous deposit — change it if needed.</p>
              )}
              <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 12px' }}>A PNG of the selected account is auto-generated and sent to the merchant.</p>
              {/* Optional: upload a custom bank-details image. When set, it overrides the auto card. */}
              <div style={{ marginBottom:12,paddingTop:12,borderTop:`1px solid ${T.borderLight}` }}>
                <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:6 }}>Upload Bank Details Image (optional)</p>
                <input type="file" accept="image/jpeg,image/png,image/webp" onChange={onBankImage} style={{ fontSize:12 }} />
                {bankImage && (
                  <div style={{ marginTop:8 }}>
                    <img src={bankImage} alt="Bank details preview" style={{ display:'block',maxWidth:'100%',maxHeight:180,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,background:T.canvas }} />
                    <Btn size="sm" variant="ghost" style={{ marginTop:6 }} onClick={()=>setBankImage(null)}>✕ Remove Image</Btn>
                  </div>
                )}
                <p style={{ fontSize:11,color:T.textMuted,margin:'6px 0 0' }}>JPG, JPEG, PNG or WEBP. Re-select to replace. If uploaded, this image is sent instead of the auto-generated card.</p>
              </div>
              <div style={{ display:'flex',gap:10 }}>
                <Btn onClick={sendAccount} disabled={saving||(!accountRef && !bankImage)}>{saving ? 'Sending...' : (bankImage ? '🖼 Send Bank Details Image' : '🏦 Send Account')}</Btn>
                <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
              </div>
            </>
          ) : (
            <>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Select a UPI to send ({linkedUpis.length} linked)</p>
              {linkedUpis.length === 0
                ? <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 12px' }}>No account-linked UPIs yet. Add one in Account Management → an account's "Add UPI".</p>
                : <Sel label="UPI ID" value={upiId} onChange={e=>setUpiId(e.target.value)}
                    options={[{ value:'', label:'— Select a UPI —' }, ...linkedUpis.map(u => ({ value:u.upiId, label:`${u.upiId}  ·  ${u.label}` }))]} />}
              <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 12px' }}>The deposit is credited to this UPI's parent account. No QR is sent.</p>
              <div style={{ display:'flex',gap:10 }}>
                <Btn onClick={sendUpi} disabled={saving||!upiId}>{saving ? 'Sending...' : '📲 Send UPI'}</Btn>
                <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
              </div>
            </>
          )}
        </div>
      )}

      {depositDoneStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 12px' }}>Review the merchant's payment slip above, then mark this deposit complete.</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={()=>complete(false)} disabled={saving}>{saving ? 'Saving...' : '✓ Mark Deposited'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
          <div style={{ display:'flex',gap:10,flexWrap:'wrap',marginTop:12,paddingTop:12,borderTop:`1px dashed ${T.border}` }}>
            <Btn size="sm" variant="secondary" onClick={recheck} disabled={saving}>{isCashDeposit ? '↻ Request Additional Information' : '↻ Recheck Payment — request re-upload'}</Btn>
            {!riskConfirm ? (
              <Btn size="sm" variant="danger" onClick={()=>setRiskConfirm(true)} disabled={saving}>⚠ Mark High Risk</Btn>
            ) : (
              <>
                <Btn size="sm" variant="danger" onClick={flagRisk} disabled={saving}>{saving ? '...' : 'Confirm High Risk'}</Btn>
                <Btn size="sm" variant="ghost" onClick={()=>setRiskConfirm(false)}>Cancel</Btn>
              </>
            )}
          </div>
          <p style={{ fontSize:11,color:T.textMuted,margin:'8px 0 0' }}>Wrong slip? Send it back for re-upload. If payment is still not received after re-upload, mark the member <b>High Risk</b>.</p>
        </div>
      )}

      {payStep && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:10 }}>Pay & Confirm{tx.payoutMode ? ` — ${PAYOUT_MODE_LABELS[payoutMode] || payoutMode}` : ''}</p>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 10px' }}>Pay the Receiver using the details above, then record the proof below. It's shared with the Receiver.</p>
          {needUtr && <Input label={isCryptoPayout ? 'Transaction Hash (Hash ID)' : 'UTR Number'} value={payUtr} onChange={e=>setPayUtr(e.target.value)} placeholder={isCryptoPayout ? 'On-chain transaction hash' : 'Bank UTR / payment reference'} required/>}
          {needReceipt && <>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>{isCashPayout ? 'Proof Image' : 'Payment Receipt'}<span style={{ color:T.danger }}> *</span></label>
            <input type="file" accept="image/*,.pdf" onChange={onReceipt} style={{ fontSize:12 }} />
            {receipt && <img src={receipt} alt="Receipt" style={{ width:'auto',maxWidth:240,maxHeight:200,objectFit:'contain',borderRadius:10,border:`1px solid ${T.border}`,margin:'12px 0',background:T.canvas }} />}
          </>}
          <div style={{ display:'flex',gap:10,marginTop:12 }}>
            <Btn onClick={()=>complete(true)} disabled={saving||(needReceipt&&!receipt)||(needUtr&&!payUtr.trim())}>{saving ? 'Saving...' : '✓ Complete'}</Btn>
            <Btn variant="secondary" onClick={onClose}>Cancel</Btn>
          </div>
        </div>
      )}

      {canReject && (
        <div style={{ marginTop:18,paddingTop:16,borderTop:`1px solid ${T.border}` }}>
          {!rejecting ? (
            <Btn size="sm" variant="danger" onClick={()=>setRejecting(true)}>✕ Reject Request</Btn>
          ) : (
            <div>
              <label style={{ display:'block',fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:6 }}>Rejection Reason</label>
              <textarea value={rejectReason} onChange={e=>setRejectReason(e.target.value)} placeholder="Why is this request being rejected? (sent to the merchant)" autoFocus
                style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:60,marginBottom:10 }}/>
              <div style={{ display:'flex',gap:10 }}>
                <Btn variant="danger" onClick={doReject} disabled={saving||!rejectReason.trim()}>{saving?'Rejecting...':'Confirm Reject'}</Btn>
                <Btn variant="secondary" onClick={()=>{ setRejecting(false); setRejectReason(''); }}>Cancel</Btn>
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
};

// ─── Admin Dashboard ──────────────────────────────────────────────────────────
export const AdminDashboard: React.FC<{ user: User }> = () => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [merchants, setMerchants] = useState<User[]>([]);
  const [stats, setStats] = useState<MerchantStats[]>([]);
  const [loading, setLoading] = useState(true);
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [active, setActive] = useState<Transaction | null>(null);

  const reload = () => Promise.all([transactionAPI.getAll(), userAPI.getMerchants(), transactionAPI.merchantStats()])
    .then(([t,m,s]) => { setTxns(t); setMerchants(m); setStats(s); })
    .catch(() => {});

  useEffect(() => { reload().finally(() => setLoading(false)); }, []);
  usePoll(() => { if (!active) reload(); });

  // Pending = every active (in-flight) request: Account Requested, Account Submitted,
  // Slip Submitted, Pending. Only completed/cancelled are excluded.
  const pending = txns.filter(t => isActive(t.status));
  const completed = txns.filter(t => t.status === 'COMPLETED' || t.status === 'DEPOSITED');
  // Default view = pending; picking a status filters the full set (so completed/cancelled are reachable too).
  const base = status === 'ALL' ? pending : txns.filter(t => t.status === status);
  const filtered = base.filter(t => type === 'ALL' || t.type === type);

  // Canonical financial-summary figures — the SINGLE source of truth (backend
  // compute_balance), aggregated across this admin's merchant businesses. Each business's
  // value is the same one shown in every other portal, so the Available Balance matches
  // everywhere. (Per-business amounts/commissions are completed-only; see compute_balance.)
  const sumStat = (k: keyof MerchantStats) => stats.reduce((a, s) => a + (Number(s[k]) || 0), 0);
  const totalDepositsAmt = sumStat('depositAmount');
  const totalWithdrawnAmt = sumStat('withdrawalAmount');
  const totalSettledAmt = sumStat('settlementAmount');
  const depositCommission = sumStat('depositCommission');
  const withdrawalCommission = sumStat('withdrawalCommission');
  const settlementCommission = sumStat('settlementCommission');
  const totalCommission = sumStat('totalCommission');
  const totalAvailableBalance = sumStat('totalAvailableBalance');
  const payoutFee = sumStat('payoutFee');
  const availableBalance = sumStat('available');
  const depReqs = txns.filter(t => t.type.startsWith('DEPOSIT')).length;
  const wdReqs = txns.filter(t => t.type.startsWith('WITHDRAWAL')).length;
  const setReqs = txns.filter(t => t.type.startsWith('SETTLEMENT')).length;

  // Per-type status breakdown for the three dashboard graphs.
  const byTypeStatus = (pfx: string) => {
    const arr = txns.filter(t => t.type.startsWith(pfx));
    return [
      { label: 'Requested', value: arr.filter(t => t.status === 'ACCOUNT_REQUESTED').length, color: T.warning },
      { label: 'Submitted', value: arr.filter(t => t.status === 'ACCOUNT_SUBMITTED').length, color: T.info },
      { label: 'In Review', value: arr.filter(t => t.status === 'PENDING_APPROVAL' || t.status === 'SUPERVISOR_REVIEW' || t.status === 'MANAGER_REVIEW' || t.status === 'RESUBMITTED').length, color: T.warning },
      { label: 'Slip', value: arr.filter(t => t.status === 'SLIP_SUBMITTED').length, color: T.blue },
      { label: 'Completed', value: arr.filter(t => t.status === 'COMPLETED' || t.status === 'DEPOSITED').length, color: T.success },
      { label: 'Rejected', value: arr.filter(t => t.status === 'REJECTED' || t.status === 'CANCELLED').length, color: T.danger },
    ];
  };

  if (loading) return <LoadingScreen label="Loading dashboard…"/>;

  return (
    <div>
      <div className="ad-stat-money" style={{ display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:14,marginBottom:14 }}>
        <FinanceCard icon="💰" label="Total Available Balance" value={totalAvailableBalance} color={T.success}
          rows={[['Total Deposits', totalDepositsAmt], ['Total Withdrawals', totalWithdrawnAmt], ['Total Settlements', totalSettledAmt]]} />
        <FinanceCard icon="％" label="Total Commission Amount" value={totalCommission} color={T.warning}
          rows={[['Deposit Commission', depositCommission], ['Withdrawal Commission', withdrawalCommission], ['Settlement Commission', settlementCommission], ['Total Commission', totalCommission]]} />
        <FinanceCard icon="◎" label="Available Balance" value={availableBalance} color={T.green}
          rows={[['Total Available Balance', totalAvailableBalance], ['Deposit Commission', depositCommission], ['Pay-Out Fee', payoutFee], ['Available Balance', availableBalance]]} />
      </div>
      <div className="ad-stat-counts" style={{ display:'grid',gridTemplateColumns:'repeat(7,minmax(0,1fr))',gap:12,marginBottom:20 }}>
        <StatCard icon="🏪" label="My Merchants" value={merchants.length} color={T.blue}/>
        <StatCard icon="✓" label="Completed" value={completed.length} color={T.success}/>
        <StatCard icon="⧗" label="Pending" value={pending.length} color={T.warning}/>
        <StatCard icon="↓" label="No. of Deposit Requests" value={depReqs} color={T.blue}/>
        <StatCard icon="↑" label="No. of Withdrawal Requests" value={wdReqs} color={T.danger}/>
        <StatCard icon="⇄" label="No. of Settlement Requests" value={setReqs} color={T.info}/>
        <StatCard icon="≡" label="Total Requests" value={txns.length} color={T.info}/>
      </div>
      <style>{`
        @media(max-width:1180px){.ad-stat-money{grid-template-columns:repeat(2,minmax(0,1fr))!important;}.ad-stat-counts{grid-template-columns:repeat(3,minmax(0,1fr))!important;}}
        @media(max-width:560px){.ad-stat-money,.ad-stat-counts{grid-template-columns:repeat(2,minmax(0,1fr))!important;}}
      `}</style>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(300px,1fr))',gap:16,marginBottom:20 }}>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800,color:T.textMain }}>Deposits</h3>
          <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>{depReqs} total · by status</p>
          <StatusChart data={byTypeStatus('DEPOSIT')}/>
        </Card>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800,color:T.textMain }}>Withdrawals</h3>
          <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>{wdReqs} total · by status</p>
          <StatusChart data={byTypeStatus('WITHDRAWAL')}/>
        </Card>
        <Card style={{ padding:22 }}>
          <h3 style={{ margin:'0 0 4px',fontSize:14,fontWeight:800,color:T.textMain }}>Settlements</h3>
          <p style={{ margin:'0 0 14px',fontSize:11,color:T.textMuted }}>{setReqs} total · by status</p>
          <StatusChart data={byTypeStatus('SETTLEMENT')}/>
        </Card>
      </div>
      <Card>
        <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}`,display:'flex',justifyContent:'space-between',alignItems:'center',gap:8,flexWrap:'wrap' }}>
          <h3 style={{ margin:0,fontSize:14,fontWeight:800 }}>Requests ({filtered.length})</h3>
          <div style={{ display:'flex',gap:8,flexWrap:'wrap' }}>
            <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'7px 10px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
              {['ALL',...REQUEST_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
            </select>
            <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'7px 10px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
              {['ALL',...REQUEST_STATUSES].map(v=><option key={v} value={v}>{v==='ALL'?'Pending (default)':typeLabel(v)}</option>)}
            </select>
          </div>
        </div>
        <TxTable loading={loading} txns={filtered} actionMode="admin" viewerRole="ADMIN" onAction={(t)=>setActive(t)}/>
      </Card>
      {active && <RequestModal tx={active} canAct onClose={()=>setActive(null)} onDone={reload}/>}
    </div>
  );
};

// ─── Admin All Transactions ─────────────────────────────────────────────────────
export const AdminTransactionsPage: React.FC = () => {
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [query, setQuery] = useState<TxQuery>({});   // server-side search + date filters
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const [filtering, setFiltering] = useState(false);   // Apply Filters request in flight
  const [active, setActive] = useState<Transaction | null>(null);

  const reload = () => transactionAPI.getAll(query).then(setTxns).catch(()=>setTxns([]));
  useEffect(() => { setFiltering(true); reload().finally(()=>{ setLoading(false); setFiltering(false); }); }, [query]);
  usePoll(() => { if (!active) reload(); });

  // Type/status are client-side refinements on the server-filtered set.
  const filtered = txns.filter(t => (type === 'ALL' || t.type === type) && (status === 'ALL' || t.status === status));

  return (
    <Card>
      <div style={{ padding:'16px 20px',borderBottom:`1px solid ${T.border}` }}>
        <h3 style={{ margin:'0 0 12px',fontSize:14,fontWeight:800 }}>All Transactions</h3>
        <TxSearchFilters onApply={setQuery} onClear={()=>setQuery({})} loading={filtering} />
        <div style={{ display:'flex',gap:8,flexWrap:'wrap',marginTop:12 }}>
          <select value={type} onChange={e=>setType(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...REQUEST_TYPES].map(v=><option key={v} value={v}>{v==='ALL'?'All Types':typeLabel(v)}</option>)}
          </select>
          <select value={status} onChange={e=>setStatus(e.target.value)} style={{ padding:'8px 12px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',fontFamily:'inherit' }}>
            {['ALL',...REQUEST_STATUSES].map(v=><option key={v} value={v}>{v==='ALL'?'All Statuses':typeLabel(v)}</option>)}
          </select>
          <TxExportButton txns={filtered} title="All Transactions" />
        </div>
      </div>
      <TxTable loading={loading} txns={filtered} actionMode="admin" viewerRole="ADMIN" onAction={(t)=>setActive(t)}/>
      <div style={{ padding:'10px 20px',borderTop:`1px solid ${T.border}` }}>
        <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Showing {filtered.length} of {txns.length}</p>
      </div>
      {active && <RequestModal tx={active} canAct onClose={()=>setActive(null)} onDone={reload}/>}
    </Card>
  );
};

// ─── Merchant Analytics (Admin + Super Admin) ───────────────────────────────────
// One card per merchant business with deposit/withdrawal/settlement totals + Available /
// Net Available Balance. Backend scopes the merchant list by role (Admin → only
// merchants they created; Super Admin → all). Card amounts are computed client-side
// from completed transactions so the date-range filter visibly re-scopes them.
const ANALYTICS_TABS: Array<{ pfx: 'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT'; label: string }> = [
  { pfx: 'DEPOSIT', label: 'Deposits' },
  { pfx: 'WITHDRAWAL', label: 'Withdrawals' },
  { pfx: 'SETTLEMENT', label: 'Settlements' },
];
const EXPORT_SCOPES: Array<{ value: string; label: string }> = [
  { value: 'ALL', label: 'Complete Merchant Report' },
  { value: 'DEPOSIT', label: 'Deposits Only' },
  { value: 'WITHDRAWAL', label: 'Withdrawals Only' },
  { value: 'SETTLEMENT', label: 'Settlements Only' },
];

export const MerchantAnalyticsPage: React.FC = () => {
  const [stats, setStats] = useState<MerchantStats[]>([]);
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [merchant, setMerchant] = useState('ALL');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [type, setType] = useState('ALL');
  const [status, setStatus] = useState('ALL');
  const [drill, setDrill] = useState<MerchantStats | null>(null);
  const [tab, setTab] = useState<'DEPOSIT' | 'WITHDRAWAL' | 'SETTLEMENT'>('DEPOSIT');
  const [exportScope, setExportScope] = useState<Record<string, string>>({});

  const reload = () => Promise.all([transactionAPI.merchantStats(), transactionAPI.getAll()])
    .then(([s, t]) => { setStats(s); setTxns(t); })
    .catch(() => { setStats([]); setTxns([]); });
  useEffect(() => { reload().finally(() => setLoading(false)); }, []);
  usePoll(() => { if (!drill) reload(); });

  const inRange = (t: Transaction) => (!from || (t.date || '') >= from) && (!to || (t.date || '') <= to);
  const merchTx = (name: string) => txns.filter(t => t.merchant === name);

  // Card numbers: counts include all statuses in range; amounts use COMPLETED/DEPOSITED only
  // (fees realise on completion). Available/Net Available mirror the canonical balance formulas.
  const cardStats = (s: MerchantStats) => {
    const tx = merchTx(s.name).filter(inRange);
    const ofType = (pfx: string) => tx.filter(t => t.type.startsWith(pfx));
    const done = (pfx: string) => ofType(pfx)
      .filter(t => t.status === 'COMPLETED' || t.status === 'DEPOSITED')
      .reduce((a, t) => a + t.amount, 0);
    const dep = done('DEPOSIT'), wd = done('WITHDRAWAL'), set = done('SETTLEMENT');
    // Deposit/Withdrawal/Settlement counts & amounts stay date-scoped (the analytics
    // breakdown honours the date filter). The canonical balance figures — Total Available
    // Balance, Commission, Pay-Out Fee and Available Balance — come straight from the
    // backend compute_balance (single source of truth, all-time per business), so the
    // Available Balance shown here is identical to every other portal for the same business.
    return {
      depositCount: ofType('DEPOSIT').length, depositAmount: dep,
      withdrawalCount: ofType('WITHDRAWAL').length, withdrawalAmount: wd,
      settlementCount: ofType('SETTLEMENT').length, settlementAmount: set,
      depositCommission: s.depositCommission,
      withdrawalCommission: s.withdrawalCommission,
      settlementCommission: s.settlementCommission,
      totalCommission: s.totalCommission,
      totalAvailableBalance: s.totalAvailableBalance,
      payoutFee: s.payoutFee ?? (s.withdrawalCommission + s.settlementCommission),
      availableBalance: s.availableBalance ?? s.available,
    };
  };

  const visible = stats.filter(s => merchant === 'ALL' || s.name === merchant);
  const typePfx = type === 'ALL' ? null : type.split('_')[0];
  const tabsShown = ANALYTICS_TABS.filter(t => !typePfx || t.pfx === typePfx);

  // Rows for a merchant scoped by date + status (+ optional type prefix), for tables & export.
  const scopedRows = (name: string, pfx: string | null) => txns.filter(t =>
    t.merchant === name && inRange(t)
    && (status === 'ALL' || t.status === status)
    && (!pfx || t.type.startsWith(pfx)));

  const dateSub = () => {
    const parts: string[] = [];
    if (from || to) parts.push(`Date ${from || 'start'} → ${to || 'today'}`);
    if (status !== 'ALL') parts.push(`Status ${typeLabel(status)}`);
    return parts.length ? parts.join(' · ') : 'All dates & statuses';
  };

  const runExport = (s: MerchantStats, fmtKind: 'pdf' | 'excel' = 'pdf') => {
    const scope = exportScope[s.name] || 'ALL';
    const pfx = scope === 'ALL' ? null : scope;
    const rows = scopedRows(s.name, pfx);
    const scopeLabel = EXPORT_SCOPES.find(e => e.value === scope)?.label || 'Report';
    if (fmtKind === 'excel') {
      exportTransactionsXlsx(rows, `${s.name.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-${scope.toLowerCase()}.xlsx`, s.name.slice(0, 31));
    } else {
      exportTransactionsPdf(rows, `${s.name} — ${scopeLabel}`, dateSub());
    }
  };

  const selStyle = { padding: '8px 12px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 12, outline: 'none', fontFamily: 'inherit' as const };

  if (loading) return <LoadingScreen label="Loading merchant analytics…" />;

  // ── Drill-down: one merchant's transactions, tabbed by type ──
  if (drill) {
    // Fee charged on a single transaction: deposits use Pay-In, withdrawals/settlements Pay-Out.
    const feeOf = (t: Transaction) => {
      const rate = (t.type.startsWith('DEPOSIT') ? (drill.payInFee || 0) : (drill.payOutFee || 0)) / 100;
      return t.amount * rate;
    };
    // Per-type summary (respects active date + status filters).
    const summary = (pfx: string) => {
      const r = scopedRows(drill.name, pfx);
      return { count: r.length, amount: r.reduce((a, t) => a + t.amount, 0) };
    };
    const sumCards: Array<[string, ReturnType<typeof summary>, string]> = [
      ['Deposits', summary('DEPOSIT'), T.blue],
      ['Withdrawals', summary('WITHDRAWAL'), T.danger],
      ['Settlements', summary('SETTLEMENT'), T.info],
    ];
    const info: Array<[string, string]> = [
      ['Business Name', drill.name],
      ['Merchant Username', drill.username],
      ['Merchant Email', drill.email],
      ['Pay-In Fee', `${drill.payInFee}%`],
      ['Pay-Out Fee', `${drill.payOutFee}%`],
    ];
    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14, flexWrap: 'wrap' }}>
          <Btn size="sm" variant="secondary" onClick={() => setDrill(null)}>← Back to Analytics</Btn>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 800, color: T.textMain }}>{drill.name}</h2>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: T.textMuted }}>Available: <b style={{ color: T.green }}>{fmt(drill.available)}</b></span>
        </div>
        <Card style={{ padding: '14px 18px', marginBottom: 14 }}>
          <div style={{ display: 'flex', gap: 26, flexWrap: 'wrap' }}>
            {info.map(([k, v]) => (
              <div key={k}><p style={{ margin: 0, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em', color: T.textMuted, fontWeight: 700 }}>{k}</p><p style={{ margin: '2px 0 0', fontSize: 13, fontWeight: 700, color: T.textMain }}>{v}</p></div>
            ))}
          </div>
        </Card>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,minmax(0,1fr))', gap: 12, marginBottom: 14 }} className="ma-sum">
          {sumCards.map(([label, s, col]) => (
            <Card key={label} style={{ padding: 16 }}>
              <p style={{ margin: 0, fontSize: 12, fontWeight: 800, color: col }}>{label}</p>
              <p style={{ margin: '8px 0 0', fontSize: 12, color: T.textMuted }}>Total {label} Requests: <b style={{ color: T.textMain }}>{s.count}</b></p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>Total {label} Amount: <b style={{ color: T.textMain }}>{fmt(s.amount)}</b></p>
            </Card>
          ))}
        </div>
        <style>{`@media(max-width:680px){.ma-sum{grid-template-columns:1fr!important;}}`}</style>
        <div style={{ display: 'flex', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
          <Input label="From" type="date" value={from} onChange={e => setFrom(e.target.value)} />
          <Input label="To" type="date" value={to} onChange={e => setTo(e.target.value)} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)}
            options={[{ value: 'ALL', label: 'All Statuses' }, ...REQUEST_STATUSES.map(v => ({ value: v, label: typeLabel(v) }))]} />
        </div>
        <Card>
          <div style={{ display: 'flex', borderBottom: `1px solid ${T.border}` }}>
            {tabsShown.map(tb => {
              const n = scopedRows(drill.name, tb.pfx).length;
              const on = tab === tb.pfx;
              return (
                <button key={tb.pfx} onClick={() => setTab(tb.pfx)}
                  style={{ flex: 1, padding: '12px 8px', border: 'none', background: on ? T.surface : 'transparent', borderBottom: on ? `2px solid ${T.blue}` : '2px solid transparent', color: on ? T.textMain : T.textMuted, fontWeight: on ? 800 : 600, fontSize: 13, cursor: 'pointer', fontFamily: 'inherit' }}>
                  {tb.label} ({n})
                </button>
              );
            })}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ background: T.canvas }}>
                  {['Reference No.', 'Membership - Member', 'Type', 'Amount', 'Fee Amount', 'Status', 'Date & Time'].map(h => (
                    <th key={h} style={{ textAlign: (h === 'Amount' || h === 'Fee Amount') ? 'right' : 'left', padding: '10px 14px', fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '0.04em', color: T.textMuted, fontWeight: 800 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {scopedRows(drill.name, tab).map(t => (
                  <tr key={t.id} style={{ borderBottom: `1px solid ${T.borderLight}` }}>
                    <td style={{ padding: '9px 14px', fontFamily: 'monospace', fontWeight: 700, color: T.blue }}>{t.ref}</td>
                    <td style={{ padding: '9px 14px', color: T.textMain, fontWeight: 600 }}>{memberLabel(t.memberId, t.member)}</td>
                    <td style={{ padding: '9px 14px' }}>{typeLabel(t.type)}</td>
                    <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700 }}>{fmt(t.amount)}</td>
                    <td style={{ padding: '9px 14px', textAlign: 'right', color: T.danger }}>{fmt(feeOf(t))}</td>
                    <td style={{ padding: '9px 14px' }}><Badge status={t.status} type={t.type} viewerRole="ADMIN" /></td>
                    <td style={{ padding: '9px 14px', whiteSpace: 'nowrap', color: T.textMuted }}>{t.date} {t.time}</td>
                  </tr>
                ))}
                {scopedRows(drill.name, tab).length === 0 && (
                  <tr><td colSpan={8} style={{ padding: 28, textAlign: 'center', color: T.textMuted }}>No {tab.toLowerCase()} transactions for this selection.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    );
  }

  // ── Analytics overview: one card per merchant ──
  return (
    <div>
      <Card style={{ padding: '14px 18px', marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <Sel label="Merchant" value={merchant} onChange={e => setMerchant(e.target.value)}
            options={[{ value: 'ALL', label: 'All Merchants' }, ...stats.map(s => ({ value: s.name, label: s.name }))]} />
          <Input label="From" type="date" value={from} onChange={e => setFrom(e.target.value)} />
          <Input label="To" type="date" value={to} onChange={e => setTo(e.target.value)} />
          <Sel label="Transaction Type" value={type} onChange={e => setType(e.target.value)}
            options={[{ value: 'ALL', label: 'All Types' }, ...REQUEST_TYPES.map(v => ({ value: v, label: typeLabel(v) }))]} />
          <Sel label="Status" value={status} onChange={e => setStatus(e.target.value)}
            options={[{ value: 'ALL', label: 'All Statuses' }, ...REQUEST_STATUSES.map(v => ({ value: v, label: typeLabel(v) }))]} />
          {(from || to || type !== 'ALL' || status !== 'ALL' || merchant !== 'ALL') && (
            <Btn size="sm" variant="ghost" onClick={() => { setMerchant('ALL'); setFrom(''); setTo(''); setType('ALL'); setStatus('ALL'); }}>Clear</Btn>
          )}
        </div>
      </Card>

      {visible.length === 0 ? (
        <Card style={{ padding: 40, textAlign: 'center', color: T.textMuted }}>No merchants to display.</Card>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(340px,1fr))', gap: 16 }}>
          {visible.map(s => {
            const c = cardStats(s);
            const rows: Array<[string, string, string?]> = [
              ['Total Deposit Requests', String(c.depositCount)],
              ['Deposit Amount', fmt(c.depositAmount)],
              ['Total Withdrawal Requests', String(c.withdrawalCount)],
              ['Withdrawal Amount', fmt(c.withdrawalAmount)],
              ['Total Settlement Requests', String(c.settlementCount)],
              ['Settlement Amount', fmt(c.settlementAmount)],
              ['Total Available Balance', fmt(c.totalAvailableBalance), T.success],
              ['Total Commission Amount', fmt(c.totalCommission), T.warning],
              ['Available Balance', fmt(c.availableBalance), T.blue],
            ];
            return (
              <Card key={s.name} style={{ padding: 20, display: 'flex', flexDirection: 'column' }}>
                <h3 style={{ margin: '0 0 2px', fontSize: 16, fontWeight: 800, color: T.textMain }}>{s.name}</h3>
                <p style={{ margin: '0 0 14px', fontSize: 11, color: T.textMuted }}>Pay-In {s.payInFee}% · Pay-Out {s.payOutFee}%</p>
                <div style={{ flex: 1 }}>
                  {rows.map(([k, v, col], i) => {
                    const strong = i >= 6;
                    return (
                      <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '7px 0', borderBottom: i < rows.length - 1 ? `1px solid ${T.borderLight}` : 'none' }}>
                        <span style={{ fontSize: 12.5, color: strong ? T.textMain : T.textMuted, fontWeight: strong ? 700 : 400 }}>{k}</span>
                        <span style={{ fontSize: 13, fontWeight: 800, color: col || T.textMain }}>{v}</span>
                      </div>
                    );
                  })}
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
                  <Btn size="sm" full onClick={() => { setDrill(s); setTab(tabsShown[0]?.pfx || 'DEPOSIT'); }}>View Transactions</Btn>
                  <div style={{ display: 'flex', gap: 6, width: '100%' }}>
                    <select value={exportScope[s.name] || 'ALL'} onChange={e => setExportScope(m => ({ ...m, [s.name]: e.target.value }))} style={{ ...selStyle, flex: 1 }}>
                      {EXPORT_SCOPES.map(e => <option key={e.value} value={e.value}>{e.label}</option>)}
                    </select>
                    <Btn size="sm" variant="secondary" onClick={() => runExport(s, 'pdf')}>📄 PDF</Btn>
                    <Btn size="sm" variant="secondary" onClick={() => runExport(s, 'excel')}>📊 Excel</Btn>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
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
  const [toggleM, setToggleM] = useState<User | null>(null);
  const [busy, setBusy] = useState(false);

  const doToggle = async (reason: string) => {
    if (!toggleM) return;
    setBusy(true);
    try { await userAPI.toggleStatus(toggleM.id, reason); await reload(); showToast(`${toggleM.name} ${toggleM.active?'deactivated':'activated'}`); setToggleM(null); }
    catch { showToast('Failed to update merchant','error'); }
    finally { setBusy(false); }
  };
  const passwordMismatch = !!form.confirmPassword && form.password !== form.confirmPassword;

  const [mBal, setMBal] = useState<MerchantBalance[]>([]);
  const balByName = Object.fromEntries(mBal.map(b => [b.name, b]));
  const reload = () => {
    userAPI.getMerchants().then(setMerchants).catch(()=>{});
    transactionAPI.merchantBalances().then(setMBal).catch(()=>{});
  };
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!showCreate && !toggleM) reload(); });

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
                <input value={form.phone} onChange={e=>set('phone',e.target.value.replace(/[^\d]/g,'').slice(0,10))} placeholder="Phone number"
                  style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,outline:'none',fontFamily:'inherit',boxSizing:'border-box' }}/>
              </div>
            </div>
            <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} placeholder="Set login password" required hint="Merchant login password"/>
            <div>
              <Input label="Confirm Password" type="password" value={form.confirmPassword} onChange={e=>set('confirmPassword',e.target.value)} placeholder="Re-enter password" required/>
              {passwordMismatch && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
            </div>
            <Sel label="Role Selection" value={form.merchantRole} onChange={e=>set('merchantRole',e.target.value)} required options={rolesForProfile(form.profile)}/>
            <Sel label="Profile Type" value={form.profile} onChange={e=>{ const p=e.target.value; const opts=rolesForProfile(p); setForm(f=>({ ...f, profile:p, merchantRole: opts.some(o=>o.value===f.merchantRole) ? f.merchantRole : (opts[0]?.value || '') })); }} options={['Admin','User','Maker','Checker'].map(v=>({value:v,label:v}))}/>
            <Input label="Pay-In Code" value={form.payIn} onChange={e=>set('payIn',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. DEP (max 3 chars)" required/>
            <Input label="Pay-Out Code" value={form.payOut} onChange={e=>set('payOut',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. WIT" required/>
            <Input label="Settlement Code" value={form.settlement} onChange={e=>set('settlement',e.target.value.slice(0,3).toUpperCase())} placeholder="e.g. SET" required/>
            <Input label="Pay-In Fee (%)" type="number" value={form.payInFee} onChange={e=>set('payInFee',e.target.value)} required/>
            <Input label="Pay-Out Fee (%)" type="number" value={form.payOutFee} onChange={e=>set('payOutFee',e.target.value)} required/>
            <Sel label="Risk Level" value={form.risk} onChange={e=>set('risk',e.target.value)} options={['LOW','MEDIUM','HIGH','CRITICAL'].map(v=>({value:v,label:v}))}/>
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
                {['Business','Merchant ID','Username','Role','Email','Phone','Codes','Available Balance','Running Balance','Status','Action'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merchants.map((m)=>(
                <tr key={m.id} style={{ background:T.surface,borderBottom:`1px solid ${T.borderLight}` }}>
                  <td style={{ padding:'11px 14px',fontWeight:800,color:T.textMain }}>{m.name}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.canvas,color:T.textMain,padding:'2px 7px',borderRadius:5,fontSize:11,fontWeight:700,whiteSpace:'nowrap' }}>{m.merchantCode||'—'}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMain }}>{m.username}</td>
                  <td style={{ padding:'11px 14px' }}><span style={{ background:T.infoBg,color:T.blue,padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,whiteSpace:'nowrap' }}>{roleLabel(m.merchantRole)}</span></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{m.email}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11,whiteSpace:'nowrap' }}>{m.phone||'—'}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',gap:3,flexWrap:'wrap' }}>
                      {[m.payIn,m.payOut,m.settlement].filter(Boolean).map(c=><code key={c} style={{ background:T.infoBg,color:T.blue,padding:'1px 5px',borderRadius:4,fontSize:10,fontWeight:700 }}>{c}</code>)}
                    </div>
                  </td>
                  <td style={{ padding:'11px 14px',fontWeight:800,color:T.success,whiteSpace:'nowrap' }}>{fmt(balByName[m.name]?.available ?? 0)}</td>
                  <td style={{ padding:'11px 14px',fontWeight:700,color:(balByName[m.name]?.runningBalance ?? 0)>0?T.danger:T.textMuted,whiteSpace:'nowrap' }}>{fmt(balByName[m.name]?.runningBalance ?? 0)}</td>
                  <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:m.active?T.successBg:T.dangerBg,color:m.active?T.success:T.danger }}>{m.active?'Active':'Inactive'}</span></td>
                  <td style={{ padding:'11px 14px' }}>
                    <Btn size="sm" variant={m.active?'danger':'success'} onClick={()=>setToggleM(m)}>{m.active?'Deactivate':'Activate'}</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {toggleM && (
        <ReasonModal
          title={`${toggleM.active ? 'Deactivate' : 'Activate'} Merchant — ${toggleM.name}`}
          label={toggleM.active ? 'Reason for Deactivation' : 'Reason for Activation'}
          confirmLabel={toggleM.active ? 'Deactivate' : 'Activate'}
          busy={busy} onSubmit={doToggle} onClose={()=>setToggleM(null)}
        />
      )}
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
  const empty = { account_name:'',account_number:'',ifsc_code:'',bank_name:'',branch:'',account_type:'Savings Account',status:'ACTIVE',upiId:'' };
  const [form, setForm] = useState(empty);
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));

  const [toggleAcc, setToggleAcc] = useState<Account | null>(null);
  const [busy, setBusy] = useState(false);
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [acctMerchants, setAcctMerchants] = useState<AccountBalance | null>(null);
  // Admin UPI IDs (separate from bank accounts).
  const [upis, setUpis] = useState<AdminUpi[]>([]);
  const [showAddUpi, setShowAddUpi] = useState(false);
  const [upiForm, setUpiForm] = useState({ label:'', upiId:'', accountRef:'' });

  const reload = () => {
    accountAPI.list().then(setAccounts).catch(()=>{});
    accountAPI.balances().then(setBalances).catch(()=>{});
    adminUpiAPI.list().then(setUpis).catch(()=>{});
  };

  const addUpi = async () => {
    if (!upiForm.upiId.includes('@')) { showToast('Enter a valid UPI ID (name@bank)','error'); return; }
    try {
      await adminUpiAPI.create({ label: upiForm.label || undefined, upiId: upiForm.upiId.trim(), accountRef: upiForm.accountRef || undefined });
      setUpiForm({ label:'', upiId:'', accountRef:'' }); setShowAddUpi(false); await reload(); showToast('UPI ID saved');
    } catch (e: any) { showToast(e?.response?.data?.detail || 'Failed to save UPI','error'); }
  };
  const toggleUpi = async (u: AdminUpi) => {
    try { await adminUpiAPI.toggle(u.id); await reload(); } catch { showToast('Failed to update UPI','error'); }
  };
  const relinkUpi = async (u: AdminUpi, ref: string) => {
    try { await adminUpiAPI.link(u.id, ref || null); await reload(); showToast('UPI link updated'); }
    catch { showToast('Failed to link UPI','error'); }
  };
  const acctName = (ref?: string | null) => accounts.find(a => a.referenceNumber === ref)?.accountName;
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!detail && !showCreate && !toggleAcc) reload(); });

  const doToggle = async (reason: string) => {
    if (!toggleAcc) return;
    setBusy(true);
    try { await accountAPI.toggle(toggleAcc.referenceNumber, reason); await reload(); showToast(`${toggleAcc.referenceNumber} updated`); setToggleAcc(null); }
    catch { showToast('Failed to update account','error'); }
    finally { setBusy(false); }
  };

  const filtered = accounts.filter(a => !search || (a.accountName || '').toLowerCase().includes(search.toLowerCase()));
  const balMap = Object.fromEntries(balances.map(b => [b.referenceNumber, b]));

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
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search by Account Name..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
        </div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Account Name','Reference ID','Account Number','IFSC Code','Branch','Deposits Received','Available','Merchants','Status','Details'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && <tr><td colSpan={10} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No accounts found</td></tr>}
              {filtered.map((a,i)=>{ const bal = balMap[a.referenceNumber]; return (
                <tr key={a.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ fontWeight:700,color:T.textMain }}>{a.accountName}</div>
                    <div style={{ fontSize:10,color:T.textMuted }}>{a.bankName}</div>
                  </td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.infoBg,color:T.blue,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{a.referenceNumber}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{a.accountNumber}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{a.ifscCode}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{a.branch}</td>
                  <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{fmt(bal?.totalDeposited ?? 0)}</td>
                  <td style={{ padding:'11px 14px',fontWeight:800,color:T.success }}>{fmt(bal?.available ?? 0)}</td>
                  <td style={{ padding:'11px 14px' }}>
                    {bal && bal.merchants.length > 0
                      ? <Btn size="sm" variant="ghost" onClick={()=>setAcctMerchants(bal)}>👥 {bal.merchants.length} merchant{bal.merchants.length>1?'s':''}</Btn>
                      : <span style={{ color:T.textLight }}>0</span>}
                  </td>
                  <td style={{ padding:'11px 14px' }}>
                    <Btn size="sm" variant={a.status==='ACTIVE'?'success':'danger'} onClick={()=>setToggleAcc(a)}>{a.status==='ACTIVE'?'● ACTIVE':'○ INACTIVE'}</Btn>
                  </td>
                  <td style={{ padding:'11px 14px' }}><Btn size="sm" variant="ghost" onClick={()=>setDetail(a)}>View</Btn></td>
                </tr>
              );})}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Admin UPI IDs — separate from bank accounts; reused when the agent sends a UPI deposit. */}
      <div style={{ marginTop:22 }}>
        <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12,gap:8,flexWrap:'wrap' }}>
          <div>
            <h2 style={{ margin:'0 0 2px',fontSize:16,fontWeight:800 }}>Admin UPI IDs</h2>
            <p style={{ margin:0,fontSize:12,color:T.textMuted }}>UPI IDs for receiving deposits — each links to a parent account; deposits via a UPI credit that account.</p>
          </div>
          <Btn onClick={()=>setShowAddUpi(v=>!v)}>{showAddUpi ? 'Cancel' : '+ Add UPI'}</Btn>
        </div>
        <Card>
          {showAddUpi && (
            <div style={{ display:'flex',gap:10,flexWrap:'wrap',alignItems:'flex-end',padding:'14px 18px',borderBottom:`1px solid ${T.border}`,background:T.canvas }}>
              <div style={{ flex:'1 1 180px' }}><Input label="Name / Label" value={upiForm.label} onChange={e=>setUpiForm(f=>({...f,label:e.target.value}))} placeholder="e.g. Satish Kumar"/></div>
              <div style={{ flex:'1 1 200px' }}><Input label="UPI ID (VPA)" value={upiForm.upiId} onChange={e=>setUpiForm(f=>({...f,upiId:e.target.value}))} placeholder="e.g. satish@ybl" required/></div>
              <div style={{ flex:'1 1 200px' }}><Sel label="Linked Account" value={upiForm.accountRef} onChange={e=>setUpiForm(f=>({...f,accountRef:e.target.value}))} options={[{value:'',label:'— Auto-link by name —'}, ...accounts.map(a=>({value:a.referenceNumber,label:`${a.accountName} (${a.referenceNumber})`}))]} /></div>
              <Btn onClick={addUpi} disabled={!upiForm.upiId.includes('@')}>Save UPI</Btn>
            </div>
          )}
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['UPI ID','Name / Label','Linked Account','Status','Action'].map(h=>(
                    <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {upis.length === 0 && <tr><td colSpan={5} style={{ padding:28,textAlign:'center',color:T.textMuted }}>No UPI IDs saved yet — add one and link it to an account.</td></tr>}
                {upis.map((u,i)=>(
                  <tr key={u.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'11px 14px',fontWeight:700,color:T.textMain }}>{u.upiId}</td>
                    <td style={{ padding:'11px 14px',color:T.textMuted }}>{u.label}</td>
                    <td style={{ padding:'8px 14px' }}>
                      <select value={u.accountRef||''} onChange={e=>relinkUpi(u, e.target.value)}
                        style={{ padding:'6px 8px',border:`1.5px solid ${u.accountRef?T.border:T.warning}`,borderRadius:8,fontSize:11,fontFamily:'inherit',maxWidth:170,outline:'none' }}>
                        <option value="">— Not linked —</option>
                        {accounts.map(a=> <option key={a.referenceNumber} value={a.referenceNumber}>{a.accountName}</option>)}
                      </select>
                    </td>
                    <td style={{ padding:'11px 14px' }}><span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:u.status==='ACTIVE'?T.successBg:T.dangerBg,color:u.status==='ACTIVE'?T.success:T.danger }}>{u.status}</span></td>
                    <td style={{ padding:'11px 14px' }}><Btn size="sm" variant={u.status==='ACTIVE'?'danger':'success'} onClick={()=>toggleUpi(u)}>{u.status==='ACTIVE'?'Deactivate':'Activate'}</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      {/* Per-account balances: deposits routed to each account, with each merchant's AB / RB / MAB */}
      <div style={{ marginTop:22 }}>
        <h2 style={{ margin:'0 0 4px',fontSize:16,fontWeight:800 }}>Account Balances</h2>
        <p style={{ margin:'0 0 14px',fontSize:12,color:T.textMuted }}>Available (AB), Running (RB) and Monthly-Average (MAB) balance per merchant, per account.</p>
        {balances.filter(b => b.merchants.length).length === 0 && (
          <Card><div style={{ padding:28,textAlign:'center',color:T.textMuted,fontSize:13 }}>No deposits routed to any account yet.</div></Card>
        )}
        {balances.filter(b => b.merchants.length).map(b => (
          <Card key={b.referenceNumber} style={{ marginBottom:14 }}>
            <div style={{ padding:'14px 18px',borderBottom:`1px solid ${T.border}` }}>
              <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',gap:8,flexWrap:'wrap' }}>
                <div>
                  <span style={{ fontWeight:800,fontSize:14,color:T.textMain }}>{b.accountName}</span>
                  <span style={{ marginLeft:8,fontSize:12,color:T.textMuted }}>{b.bankName} · A/C {b.accountNumber}</span>
                </div>
                <code style={{ background:T.infoBg,color:T.blue,padding:'2px 7px',borderRadius:5,fontSize:11,fontWeight:700 }}>{b.referenceNumber}</code>
              </div>
              <div style={{ display:'flex',gap:18,flexWrap:'wrap',marginTop:10,fontSize:12,color:T.textMuted }}>
                <span>Bank: <b style={{ color:T.textMain }}>{fmt(b.bankDeposited ?? 0)}</b></span>
                <span>UPI: <b style={{ color:T.textMain }}>{fmt(b.upiDeposited ?? 0)}</b></span>
                <span>Total Deposits: <b style={{ color:T.textMain }}>{fmt(b.totalDeposited)}</b></span>
                <span>Withdrawals: <b style={{ color:T.textMain }}>{fmt(b.withdrawals ?? 0)}</b></span>
                <span>Settlements: <b style={{ color:T.textMain }}>{fmt(b.settlements ?? 0)}</b></span>
                <span style={{ color:T.success }}>Available: <b>{fmt(b.available)}</b></span>
              </div>
              {b.linkedUpis && b.linkedUpis.length > 0 && (
                <div style={{ display:'flex',gap:6,flexWrap:'wrap',alignItems:'center',marginTop:8 }}>
                  <span style={{ fontSize:11,color:T.textMuted }}>Linked UPIs:</span>
                  {b.linkedUpis.map(u => <code key={u.id} style={{ background:T.canvas,color:T.textMain,padding:'2px 7px',borderRadius:5,fontSize:11 }}>{u.upiId}</code>)}
                </div>
              )}
            </div>
            <div style={{ overflowX:'auto' }}>
              <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
                <thead>
                  <tr style={{ background:T.canvas }}>
                    {['Merchant','Merchant ID','Deposited (this a/c)','Available (AB)','Running (RB)','Monthly Avg (MAB)'].map(h=>(
                      <th key={h} style={{ padding:'9px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.merchants.map((m,i)=>(
                    <tr key={m.merchantName} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                      <td style={{ padding:'10px 14px',fontWeight:700,color:T.textMain }}>{m.merchantName}</td>
                      <td style={{ padding:'10px 14px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{m.merchantCode||'—'}</code></td>
                      <td style={{ padding:'10px 14px',color:T.textMuted }}>{fmt(m.deposited)}</td>
                      <td style={{ padding:'10px 14px',fontWeight:800,color:T.success }}>{fmt(m.available)}</td>
                      <td style={{ padding:'10px 14px',fontWeight:700,color:m.runningBalance>0?T.danger:T.textMuted }}>{fmt(m.runningBalance)}</td>
                      <td style={{ padding:'10px 14px',fontWeight:700,color:T.blue }}>{fmt(m.mab)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))}
      </div>

      {acctMerchants && (
        <Modal title={`Merchants using ${acctMerchants.accountName} (${acctMerchants.referenceNumber})`} onClose={()=>setAcctMerchants(null)} wide>
          <p style={{ margin:'0 0 12px',fontSize:12,color:T.textMuted }}>
            {acctMerchants.merchants.length} merchant{acctMerchants.merchants.length>1?'s have':' has'} deposited into this account · Total received: <b style={{ color:T.textMain }}>{fmt(acctMerchants.totalDeposited)}</b>
          </p>
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
              <thead>
                <tr style={{ background:T.canvas }}>
                  {['Merchant','Merchant ID','Deposited (this a/c)'].map(h=>(
                    <th key={h} style={{ padding:'9px 12px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {acctMerchants.merchants.map((m,i)=>(
                  <tr key={m.merchantName} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                    <td style={{ padding:'10px 12px',fontWeight:700,color:T.textMain }}>{m.merchantName}</td>
                    <td style={{ padding:'10px 12px' }}><code style={{ background:T.canvas,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{m.merchantCode||'—'}</code></td>
                    <td style={{ padding:'10px 12px',fontWeight:700,color:T.textMain }}>{fmt(m.deposited)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Modal>
      )}

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
            <BankNamesDatalist names={BANK_NAMES}/>
            <Input label="Account Name" value={form.account_name} onChange={e=>set('account_name',e.target.value)} required/>
            <Input label="Account Number" value={form.account_number} onChange={e=>set('account_number',e.target.value)} required/>
            <Input label="IFSC Code" value={form.ifsc_code} required hint="Auto-fills bank & branch"
              onChange={async e=>{ const up=e.target.value.toUpperCase(); set('ifsc_code',up); if(isValidIfsc(up)){ const info=await lookupIfsc(up); if(info) setForm(f=>({...f,ifsc_code:up,bank_name:info.bank,branch:info.branch})); } }}/>
            <Input label="Bank Name" value={form.bank_name} onChange={e=>set('bank_name',e.target.value)} list="bank-names" required/>
            <Input label="Branch" value={form.branch} onChange={e=>set('branch',e.target.value)} required/>
            <Sel label="Account Type" value={form.account_type} onChange={e=>set('account_type',e.target.value)} options={['Savings Account','Current Account'].map(v=>({value:v,label:v}))}/>
            <Sel label="Status" value={form.status} onChange={e=>set('status',e.target.value)} options={['ACTIVE','INACTIVE'].map(v=>({value:v,label:v}))}/>
            <Input label="UPI ID (optional)" value={form.upiId} onChange={e=>set('upiId',e.target.value)} placeholder="e.g. satish@ybl — links to this account"/>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create}>Create Account</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      {toggleAcc && (
        <ReasonModal
          title={`${toggleAcc.status==='ACTIVE' ? 'Deactivate' : 'Activate'} Account — ${toggleAcc.referenceNumber}`}
          label={toggleAcc.status==='ACTIVE' ? 'Reason for Deactivation' : 'Reason for Activation'}
          confirmLabel={toggleAcc.status==='ACTIVE' ? 'Deactivate' : 'Activate'}
          busy={busy} onSubmit={doToggle} onClose={()=>setToggleAcc(null)}
        />
      )}
    </div>
  );
};

// ─── SA Dashboard ─────────────────────────────────────────────────────────────
// Financial summary card: big headline number + a labelled breakdown underneath.
const FinanceCard: React.FC<{ icon: string; label: string; value: number; color: string; rows: Array<[string, number]> }> = ({ icon, label, value, color, rows }) => (
  <Card style={{ padding:18 }}>
    <div style={{ display:'flex',alignItems:'center',gap:8,marginBottom:6 }}>
      <span style={{ width:30,height:30,borderRadius:9,background:`${color}1a`,display:'inline-flex',alignItems:'center',justifyContent:'center',fontSize:16 }}>{icon}</span>
      <span style={{ fontSize:11.5,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em' }}>{label}</span>
    </div>
    <p style={{ fontSize:26,fontWeight:800,color,margin:'0 0 10px' }}>{fmt(value)}</p>
    {rows.map(([k,v],i)=>(
      <div key={i} style={{ display:'flex',justifyContent:'space-between',padding:'5px 0',fontSize:12.5,borderTop:`1px solid ${T.borderLight}` }}>
        <span style={{ color:T.textMuted }}>{k}</span><b style={{ color:T.textMain }}>{fmt(v)}</b>
      </div>
    ))}
  </Card>
);

export const SaDashboard: React.FC = () => {
  const [merchants, setMerchants] = useState<User[]>([]);
  const [admins, setAdmins] = useState<User[]>([]);
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [stats, setStats] = useState<MerchantStats[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = () => Promise.all([userAPI.getMerchants(), userAPI.getAdmins(), transactionAPI.getAll(), transactionAPI.merchantStats()])
    .then(([m,a,t,s]) => { setMerchants(m); setAdmins(a); setTxns(t); setStats(s); })
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

  // Platform-wide canonical balances — the SINGLE source of truth (backend
  // compute_balance), summed across every merchant business so the figures match the
  // per-business values shown in every other portal (completed-only; see compute_balance).
  const sumStat = (k: keyof MerchantStats) => stats.reduce((a, s) => a + (Number(s[k]) || 0), 0);
  const totalDeposits = sumStat('depositAmount');
  const totalWithdrawn = sumStat('withdrawalAmount');
  const totalSettled = sumStat('settlementAmount');
  const depositCommission = sumStat('depositCommission');
  const withdrawalCommission = sumStat('withdrawalCommission');
  const settlementCommission = sumStat('settlementCommission');
  const totalCommission = sumStat('totalCommission');
  const totalAvailableBalance = sumStat('totalAvailableBalance');
  const payoutFee = sumStat('payoutFee');
  const availableBalance = sumStat('available');

  return (
    <div>
      {/* 3 per row → counts on top, money on the bottom row */}
      <div style={{ display:'grid',gridTemplateColumns:'repeat(4,minmax(0,1fr))',gap:14,marginBottom:16 }} className="sa-stat-grid">
        <StatCard icon="🛡" label="Total Admins" value={admins.length} color={T.blue}/>
        <StatCard icon="🏪" label="Total Merchants" value={merchants.length} color={T.success}/>
        <StatCard icon="✅" label="Active Admins" value={admins.filter(a=>a.active).length} color={T.info}/>
        <StatCard icon="📊" label="Monthly Volume" value={fmt(monthlyVolume)} color={T.warning}/>
      </div>
      <div style={{ display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:14,marginBottom:20 }} className="sa-fin-grid">
        <FinanceCard icon="💰" label="Total Available Balance" value={totalAvailableBalance} color={T.success}
          rows={[['Total Deposits', totalDeposits], ['Total Withdrawals', totalWithdrawn], ['Total Settlements', totalSettled]]} />
        <FinanceCard icon="％" label="Total Commission Amount" value={totalCommission} color={T.warning}
          rows={[['Deposit Commission', depositCommission], ['Withdrawal Commission', withdrawalCommission], ['Settlement Commission', settlementCommission], ['Total Commission', totalCommission]]} />
        <FinanceCard icon="◎" label="Available Balance" value={availableBalance} color={T.green}
          rows={[['Total Available Balance', totalAvailableBalance], ['Deposit Commission', depositCommission], ['Pay-Out Fee', payoutFee], ['Available Balance', availableBalance]]} />
      </div>
      <style>{`@media(max-width:760px){.sa-stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;}.sa-fin-grid{grid-template-columns:1fr!important;}}@media(max-width:460px){.sa-stat-grid{grid-template-columns:1fr!important;}}`}</style>
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
  const [form, setForm] = useState({ name:'',username:'',email:'',countryCode:'+91',phone:'',password:'',confirmPassword:'',reason:'' });
  const set = (k: string, v: string) => setForm(f => ({...f,[k]:v}));
  const passwordMismatch = !!form.confirmPassword && form.password !== form.confirmPassword;
  const [overview, setOverview] = useState<User | null>(null);
  const [overviewMerchants, setOverviewMerchants] = useState<User[]>([]);
  const [toggleA, setToggleA] = useState<User | null>(null);
  const [resetA, setResetA] = useState<User | null>(null);
  const [resetPw, setResetPw] = useState({ next:'', confirm:'' });
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = () => userAPI.getAdmins().then(setAdmins).catch(()=>{});
  useEffect(() => { reload(); }, []);
  usePoll(() => { if (!overview && !showCreate && !toggleA && !resetA) reload(); });

  const openOverview = async (a: User) => {
    setOverview(a);
    try { setOverviewMerchants(await userAPI.getAdminMerchants(a.id)); }
    catch { setOverviewMerchants([]); }
  };

  const doUnlock = async (a: User) => {
    try { await userAPI.unlock(a.id); await reload(); showToast(`${a.name} unlocked`); }
    catch { showToast('Failed to unlock admin','error'); }
  };

  const doReset = async () => {
    if (!resetA) return;
    if (resetPw.next !== resetPw.confirm) { showToast('Passwords do not match','error'); return; }
    const policy = passwordPolicyError(resetPw.next);
    if (policy) { showToast(policy,'error'); return; }
    setBusy(true);
    try {
      await userAPI.resetPassword(resetA.id, resetPw.next);
      showToast(`Password reset for ${resetA.name}`);
      setResetA(null); setResetPw({ next:'', confirm:'' });
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to reset password','error');
    } finally { setBusy(false); }
  };

  const filteredAdmins = admins.filter(a => !search ||
    [a.name, a.username, a.email, a.phone].some(v => (v||'').toLowerCase().includes(search.toLowerCase())));

  const create = async () => {
    if(!form.name||!form.username||!form.email||!form.password){ showToast('Fill all fields','error'); return; }
    if(form.password !== form.confirmPassword){ showToast('Passwords do not match','error'); return; }
    if(!form.reason.trim()){ showToast('A reason is required to create an admin','error'); return; }
    try {
      await userAPI.createAdmin({ name:form.name, username:form.username, email:form.email, phone:form.phone?`${form.countryCode} ${form.phone}`:undefined, password:form.password, reason:form.reason.trim(), role:'ADMIN' });
      await reload();
      setShowCreate(false);
      setForm({ name:'',username:'',email:'',countryCode:'+91',phone:'',password:'',confirmPassword:'',reason:'' });
      showToast(`Admin "${form.name}" created`);
    } catch (e: any) {
      showToast(e?.response?.data?.detail || 'Failed to create admin','error');
    }
  };

  const doToggleAdmin = async (reason: string) => {
    if (!toggleA) return;
    setBusy(true);
    try { await userAPI.toggleStatus(toggleA.id, reason); await reload(); showToast(`${toggleA.name} ${toggleA.active?'deactivated':'activated'}`); setToggleA(null); }
    catch { showToast('Failed to update admin','error'); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <div style={{ display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:18 }}>
        <div>
          <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Admin Management</h2>
          <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>{admins.length} admin{admins.length===1?'':'s'} · Overview, reset password, activate/deactivate</p>
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
              <input value={form.phone} onChange={e=>set('phone',e.target.value.replace(/[^\d]/g,'').slice(0,10))} placeholder="Phone number"
                style={{ flex:1,padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,outline:'none',fontFamily:'inherit',boxSizing:'border-box' }}/>
            </div>
          </div>
          <Input label="Password" type="password" value={form.password} onChange={e=>set('password',e.target.value)} required placeholder="Set login password" hint="Admin will use this to login"/>
          <Input label="Confirm Password" type="password" value={form.confirmPassword} onChange={e=>set('confirmPassword',e.target.value)} required placeholder="Re-enter password"/>
          {passwordMismatch && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
          <div style={{ marginBottom:16 }}>
            <label style={{ display:'block',fontSize:12,fontWeight:700,color:T.textMuted,marginBottom:6,textTransform:'uppercase',letterSpacing:'0.05em' }}>Reason<span style={{ color:T.danger }}> *</span></label>
            <textarea value={form.reason} onChange={e=>set('reason',e.target.value)} placeholder="Why is this admin being created?"
              style={{ width:'100%',padding:'10px 14px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:14,color:T.textMain,background:T.surface,outline:'none',boxSizing:'border-box',fontFamily:'inherit',resize:'vertical',minHeight:60 }}/>
          </div>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={create} disabled={passwordMismatch||!form.reason.trim()}>Create Admin</Btn>
            <Btn variant="secondary" onClick={()=>setShowCreate(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      <Card>
        <div style={{ padding:'14px 20px',borderBottom:`1px solid ${T.border}` }}>
          <div style={{ position:'relative',maxWidth:340 }}>
            <span style={{ position:'absolute',left:10,top:'50%',transform:'translateY(-50%)',color:T.textMuted,fontSize:14 }}>🔍</span>
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search name, username, email or phone..."
              style={{ width:'100%',padding:'8px 12px 8px 32px',border:`1.5px solid ${T.border}`,borderRadius:10,fontSize:12,outline:'none',boxSizing:'border-box',fontFamily:'inherit' }}/>
          </div>
        </div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Admin Name','Username','Email','Phone','Status','Merchants','Created','Actions'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}`,whiteSpace:'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredAdmins.length === 0 && <tr><td colSpan={9} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No admins found</td></tr>}
              {filteredAdmins.map((a,i)=>(
                <tr key={a.id} style={{ background:i%2===0?T.surface:'#f8faff',borderBottom:`1px solid ${T.borderLight}` }}>
                  <td style={{ padding:'11px 14px',fontWeight:800,color:T.textMain }}>{a.name}</td>
                  <td style={{ padding:'11px 14px',color:T.textMain }}>{a.username}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{a.email}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{a.phone||'—'}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',flexDirection:'column',gap:3,alignItems:'flex-start' }}>
                      <span style={{ padding:'2px 8px',borderRadius:12,fontSize:11,fontWeight:700,background:a.active?T.successBg:T.dangerBg,color:a.active?T.success:T.danger }}>{a.active?'Active':'Inactive'}</span>
                      {a.locked && <span style={{ padding:'2px 8px',borderRadius:12,fontSize:10,fontWeight:700,background:T.warningBg,color:T.warning }}>🔒 Locked</span>}
                    </div>
                  </td>
                  <td style={{ padding:'11px 14px',fontWeight:800,color:T.blue,textAlign:'center' }}>{a.merchantCount ?? 0}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{formatDateTime(a.createdAt || a.created)}</td>
                  <td style={{ padding:'11px 14px' }}>
                    <div style={{ display:'flex',gap:6,flexWrap:'wrap' }}>
                      <Btn size="sm" variant="ghost" onClick={()=>openOverview(a)}>Overview</Btn>
                      <Btn size="sm" variant="secondary" onClick={()=>{ setResetA(a); setResetPw({ next:'', confirm:'' }); }}>Reset Password</Btn>
                      {a.locked && <Btn size="sm" variant="success" onClick={()=>doUnlock(a)}>Unlock</Btn>}
                      <Btn size="sm" variant={a.active?'danger':'success'} onClick={()=>setToggleA(a)}>{a.active?'Deactivate':'Activate'}</Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {overview && (
        <Modal title={`Admin Overview — ${overview.name}`} onClose={()=>{setOverview(null);setOverviewMerchants([]);}} wide>
          <div style={{ display:'grid',gridTemplateColumns:'1fr 1fr',gap:'0 24px',marginBottom:16 }}>
            <div>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Admin Details</p>
              <Row k="Full Name" v={overview.name} />
              <Row k="Username" v={overview.username} />
              <Row k="Email" v={overview.email} />
              <Row k="Phone" v={overview.phone || '—'} />
              <Row k="Status" v={<span style={{ color:overview.active?T.success:T.danger,fontWeight:800 }}>{overview.active?'Active':'Inactive'}{overview.locked?' · 🔒 Locked':''}</span>} />
              <Row k="Created" v={formatDateTime(overview.createdAt || overview.created)} />
            </div>
            <div>
              <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',marginBottom:8 }}>Activity Summary</p>
              <div style={{ display:'flex',gap:12,flexWrap:'wrap',marginBottom:12 }}>
                <div style={{ background:T.infoBg,borderRadius:10,padding:'10px 16px' }}>
                  <p style={{ margin:0,fontSize:10,color:T.textMuted,fontWeight:700,textTransform:'uppercase' }}>Merchants Created</p>
                  <p style={{ margin:0,fontSize:24,fontWeight:800,color:T.blue }}>{overview.merchantCount ?? overviewMerchants.length}</p>
                </div>
                <div style={{ background:overview.active?T.successBg:T.dangerBg,borderRadius:10,padding:'10px 16px' }}>
                  <p style={{ margin:0,fontSize:10,color:T.textMuted,fontWeight:700,textTransform:'uppercase' }}>Account State</p>
                  <p style={{ margin:0,fontSize:18,fontWeight:800,color:overview.active?T.success:T.danger }}>{overview.active?'Active':'Inactive'}</p>
                </div>
              </div>
              {overview.failedAttempts ? <p style={{ fontSize:11,color:T.textMuted,margin:0 }}>Failed login attempts: <b>{overview.failedAttempts}</b></p> : null}
            </div>
          </div>
          <p style={{ fontSize:11,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.05em',margin:'8px 0 8px' }}>Merchant Information ({overviewMerchants.length})</p>
          {overviewMerchants.length === 0
            ? <div style={{ padding:20,textAlign:'center',color:T.textMuted,background:T.canvas,borderRadius:10,fontSize:12 }}>No merchants created by this admin.</div>
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
                    {overviewMerchants.map((m,i)=>(
                      <tr key={m.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                        <td style={{ padding:'11px 14px',fontWeight:700 }}>{m.name}</td>
                        <td style={{ padding:'11px 14px',color:T.textMain }}>{m.username}</td>
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

      {resetA && (
        <Modal title={`Reset Password — ${resetA.name}`} onClose={()=>setResetA(null)}>
          <p style={{ fontSize:12,color:T.textMuted,margin:'0 0 14px' }}>Set a new password for <b style={{ color:T.textMain }}>{resetA.username}</b>. Use this when the admin can't receive OTPs. They can sign in immediately with the new password.</p>
          <Input label="New Password" type="password" value={resetPw.next} onChange={e=>setResetPw(p=>({...p,next:e.target.value}))} placeholder="Enter new password" required/>
          <Input label="Confirm Password" type="password" value={resetPw.confirm} onChange={e=>setResetPw(p=>({...p,confirm:e.target.value}))} placeholder="Re-enter new password" required/>
          {resetPw.confirm && resetPw.next !== resetPw.confirm && <p style={{ fontSize:11,color:T.danger,margin:'-10px 0 12px',fontWeight:600 }}>Passwords do not match</p>}
          <p style={{ fontSize:11,color:T.textMuted,margin:'0 0 14px' }}>{PASSWORD_POLICY_TEXT}</p>
          <div style={{ display:'flex',gap:10 }}>
            <Btn onClick={doReset} disabled={busy||!resetPw.next||!resetPw.confirm}>{busy?'Resetting...':'Reset Password'}</Btn>
            <Btn variant="secondary" onClick={()=>setResetA(null)}>Cancel</Btn>
          </div>
        </Modal>
      )}
      {toggleA && (
        <ReasonModal
          title={`${toggleA.active ? 'Deactivate' : 'Activate'} Admin — ${toggleA.name}`}
          label={toggleA.active ? 'Reason for Deactivation' : 'Reason for Activation'}
          confirmLabel={toggleA.active ? 'Deactivate' : 'Activate'}
          busy={busy} onSubmit={doToggleAdmin} onClose={()=>setToggleA(null)}
        />
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

// ─── Audit Logs (Super Admin) ──────────────────────────────────────────────────
export const AuditLogsPage: React.FC = () => {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');
  const [userF, setUserF] = useState('');
  const [roleF, setRoleF] = useState('');
  const [actionF, setActionF] = useState('');
  const [caseF, setCaseF] = useState('');
  const [fromF, setFromF] = useState('');
  const [toF, setToF] = useState('');

  const reload = () => auditLogAPI.list().then(setLogs).catch(()=>setLogs([]));
  useEffect(() => { reload().finally(()=>setLoading(false)); }, []);
  usePoll(reload);

  const day = (s: string) => (s || '').slice(0, 10);
  const filtered = logs.filter(l =>
    (!q || l.username.toLowerCase().includes(q.toLowerCase()) || l.action.toLowerCase().includes(q.toLowerCase()) ||
      (l.entityId || '').toLowerCase().includes(q.toLowerCase()) || (l.reason || '').toLowerCase().includes(q.toLowerCase())) &&
    (!userF || l.username.toLowerCase().includes(userF.toLowerCase())) &&
    (!roleF || (l.role || '') === roleF) &&
    (!actionF || l.action === actionF) &&
    (!caseF || (l.entityId || '').toLowerCase().includes(caseF.toLowerCase())) &&
    (!fromF || day(l.createdAt) >= fromF) && (!toF || day(l.createdAt) <= toF));

  const actionTypes = Array.from(new Set(logs.map(l => l.action))).sort();
  const today = new Date().toISOString().slice(0, 10);
  const exportExcel = () => {
    downloadXlsx(`clari5pay-audit-logs-${today}.xlsx`, [{
      name: 'Audit Logs',
      columns: [
        { header: 'Time', get: l => formatDateTime(l.createdAt), width: 20 },
        { header: 'User', get: l => l.username || '' },
        { header: 'Role', get: l => (l.role || '').replace('_', ' ') },
        { header: 'Action', get: l => l.action || '' },
        { header: 'Entity Type', get: l => l.entityType || '' },
        { header: 'Entity Id', get: l => l.entityId || '' },
        { header: 'Old Value', get: l => l.oldValue || '' },
        { header: 'New Value', get: l => l.newValue || '' },
        { header: 'Reason', get: l => l.reason || '' },
        { header: 'IP', get: l => l.ip || '' },
        { header: 'Location', get: l => l.location || '' },
      ],
      rows: filtered,
    }]);
  };
  const exportPdf = () => {
    const w = window.open('', '_blank', 'width=1100,height=800');
    if (!w) { alert('Please allow pop-ups to export.'); return; }
    const esc = (s: unknown) => String(s ?? '—').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string));
    const body = filtered.map((l, i) => `<tr class="${i % 2 ? 'alt' : ''}"><td class="nw">${esc(formatDateTime(l.createdAt))}</td><td>${esc(l.username)}</td><td>${esc((l.role || '').replace('_', ' '))}</td><td class="mono">${esc(l.action)}</td><td>${esc(l.entityType)} ${esc(l.entityId || '')}</td><td>${esc(l.oldValue)} → ${esc(l.newValue)}</td><td>${esc(l.reason)}</td><td>${esc(l.ip)}</td></tr>`).join('');
    w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Audit Logs</title><style>@page{size:A4 landscape;margin:12mm}body{font-family:Arial,sans-serif;color:#0a2540}h1{font-size:16px}.sub{font-size:11px;color:#555;margin:0 0 10px}table{width:100%;border-collapse:collapse;font-size:9.5px}th{background:#0a2540;color:#fff;text-align:left;padding:5px 6px;font-size:8.5px;text-transform:uppercase}td{padding:4px 6px;border-bottom:1px solid #e2e8f0}tr.alt td{background:#f5f8ff}.mono{font-family:monospace}.nw{white-space:nowrap}.runfoot{position:fixed;bottom:5mm;left:0;right:0;text-align:center;font-size:8px;color:#9ca3af}</style></head><body><div class="runfoot">Clari5Pay · Audit Logs · CONFIDENTIAL · enable "Headers and footers" in the print dialog for page numbers</div><h1><span style="color:#0052cc">clari</span><span style="color:#26d00c">5</span>pay — Audit Logs</h1><p class="sub">Generated ${new Date().toLocaleString('en-IN')} · ${filtered.length} record(s)${fromF || toF ? ` · ${fromF || 'start'} → ${toF || 'today'}` : ''}</p><table><thead><tr><th>Time</th><th>User</th><th>Role</th><th>Action</th><th>Entity</th><th>Old → New</th><th>Reason</th><th>IP</th></tr></thead><tbody>${body || '<tr><td colspan=8>No records</td></tr>'}</tbody></table></body></html>`);
    w.document.close(); w.focus(); setTimeout(() => { try { w.print(); } catch { /* manual */ } }, 500);
  };

  return (
    <div>
      <div style={{ marginBottom:18 }}>
        <h2 style={{ margin:0,fontSize:16,fontWeight:800 }}>Audit Logs</h2>
        <p style={{ margin:'2px 0 0',fontSize:12,color:T.textMuted }}>Detailed audit trail with actor, role, reason, old/new value and IP</p>
      </div>
      <Card>
        <div style={{ padding:'14px 20px',borderBottom:`1px solid ${T.border}` }}>
          <div style={{ display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(150px,1fr))',gap:10,alignItems:'end' }}>
            <Input label="Search" value={q} onChange={e=>setQ(e.target.value)} placeholder="User / action / entity" icon="🔍" style={{ marginBottom:0 }}/>
            <Input label="User" value={userF} onChange={e=>setUserF(e.target.value)} style={{ marginBottom:0 }}/>
            <Sel label="Role" value={roleF} onChange={e=>setRoleF(e.target.value)} style={{ marginBottom:0 }}
              options={[{value:'',label:'All Roles'},...['SUPER_ADMIN','ADMIN','MERCHANT','SUPPORT_AGENT'].map(r=>({value:r,label:r.replace('_',' ')}))]}/>
            <Sel label="Action Type" value={actionF} onChange={e=>setActionF(e.target.value)} style={{ marginBottom:0 }}
              options={[{value:'',label:'All Actions'},...actionTypes.map(a=>({value:a,label:a}))]}/>
            <Input label="Case / Entity ID" value={caseF} onChange={e=>setCaseF(e.target.value)} placeholder="e.g. CMP000003" style={{ marginBottom:0 }}/>
            <Input label="From" type="date" value={fromF} onChange={e=>setFromF(e.target.value)} style={{ marginBottom:0 }}/>
            <Input label="To" type="date" value={toF} onChange={e=>setToF(e.target.value)} style={{ marginBottom:0 }}/>
          </div>
          <div style={{ display:'flex',gap:8,marginTop:12,alignItems:'center',flexWrap:'wrap' }}>
            <Btn size="sm" variant="secondary" onClick={exportPdf}>📄 Download PDF</Btn>
            <Btn size="sm" variant="secondary" onClick={exportExcel}>📊 Download Excel</Btn>
            <span style={{ fontSize:12,color:T.textMuted }}>{filtered.length} record(s)</span>
          </div>
        </div>
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%',borderCollapse:'collapse',fontSize:12 }}>
            <thead>
              <tr style={{ background:T.canvas }}>
                {['Time','User','Role','Action','Entity','Old → New','Reason','IP','Location'].map(h=>(
                  <th key={h} style={{ padding:'10px 14px',textAlign:'left',fontSize:10,fontWeight:800,color:T.textMuted,textTransform:'uppercase',letterSpacing:'0.06em',borderBottom:`2px solid ${T.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && <tr><td colSpan={9} style={{ padding:32,textAlign:'center',color:T.textMuted }}>Loading...</td></tr>}
              {!loading && filtered.length === 0 && <tr><td colSpan={9} style={{ padding:32,textAlign:'center',color:T.textMuted }}>No audit records found</td></tr>}
              {filtered.map((l,i)=>(
                <tr key={l.id} style={{ background:i%2===0?T.surface:'#f8faff' }}>
                  <td style={{ padding:'11px 14px',color:T.textMuted,whiteSpace:'nowrap' }}>{formatDateTime(l.createdAt)}</td>
                  <td style={{ padding:'11px 14px',fontWeight:700 }}>{l.username}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{(l.role||'').replace('_',' ')}</td>
                  <td style={{ padding:'11px 14px' }}><code style={{ background:T.infoBg,color:T.blue,padding:'2px 6px',borderRadius:4,fontSize:11,fontWeight:700 }}>{l.action}</code></td>
                  <td style={{ padding:'11px 14px',color:T.textMuted }}>{l.entityType ? `${l.entityType}${l.entityId ? ' '+l.entityId : ''}` : '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{(l.oldValue || l.newValue) ? `${l.oldValue ?? '—'} → ${l.newValue ?? '—'}` : '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMain }}>{l.reason || '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{l.ip || '—'}</td>
                  <td style={{ padding:'11px 14px',color:T.textMuted,fontSize:11 }}>{l.location || '—'}</td>
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
