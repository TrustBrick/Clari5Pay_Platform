import React, { useState, useEffect, useCallback } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { Card, Btn, Input, Sel, Modal } from '../components/UI';
import { useToast } from '../context/ToastContext';
import {
  kycAPI, KYC_VALIDATION, OCR_ACCEPT, OCR_MAX_BYTES, OCR_DOC_TYPES, kycErrorMessage,
  type KycHistoryItem, type KycHistoryDetail, type AadhaarDetails,
} from '../services/kyc';

// ─── Merchant Portal → KYC Update ──────────────────────────────────────────────
// Identity-verification workspace for Supervisor / Manager roles. Aadhaar, PAN, Passport and OCR
// are live, membership-driven flows backed by the Melento.ai staging APIs — every request/response
// is persisted server-side and shown in the Verification History table.

type ViewKey = 'home' | 'aadhaar' | 'pan' | 'passport' | 'ocr';

// Custom document icons (Aadhaar / PAN / Passport) served from /public/kyc. Used both by the
// dashboard cards and each verification view's header, so the icon is identical in both places.
const KYC_ICONS: Partial<Record<ViewKey, string>> = {
  aadhaar: '/kyc/aadhaar.png',
  pan: '/kyc/pan.png',
  passport: '/kyc/passport.png',
};
// Render a card's icon: the provided image (filling the icon slot, used as-is) or the emoji
// fallback (OCR). object-fit keeps a consistent size and centering across all cards.
const KycIcon: React.FC<{ view: ViewKey; emoji: string }> = ({ view, emoji }) => {
  const src = KYC_ICONS[view];
  return src
    ? <img src={src} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
    : <>{emoji}</>;
};

interface CardDef { key: ViewKey; icon: string; title: string; desc: string; }
const CARDS: CardDef[] = [
  { key: 'aadhaar',  icon: '🆔', title: 'Aadhaar Verification',      desc: 'Generate a DigiLocker verification link for a member and track completion.' },
  { key: 'pan',      icon: '💳', title: 'PAN Verification',          desc: 'Validate a member’s PAN and fetch the holder’s details.' },
  { key: 'passport', icon: '📘', title: 'Passport Verification',     desc: 'Verify a passport number and its validity.' },
  { key: 'ocr',      icon: '📄', title: 'OCR Document Verification', desc: 'Extract details from an uploaded identity document.' },
];
const TYPE_LABEL: Record<ViewKey, string> = {
  home: '', aadhaar: 'Aadhaar', pan: 'PAN', passport: 'Passport', ocr: 'OCR',
};

// Format an ISO/UTC timestamp in Indian Standard Time.
const fmtIST = (iso?: string | null): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true });
};

// Prettify an API snake_case key into a Title Case label.
const prettify = (k: string): string =>
  k.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).trim();

// Extract the customer's Aadhaar photograph (base64 JPEG in the offline eKYC XML's <Pht> node).
export const extractAadhaarPhoto = (xmlFile?: string | null): string | null => {
  if (!xmlFile) return null;
  let xml = String(xmlFile).trim();
  if (!xml.includes('<')) {
    try { xml = atob(xml); } catch { /* not base64 — leave as-is */ }
  }
  let m = xml.match(/<Pht>\s*([\s\S]*?)\s*<\/Pht>/i);
  if (!m) m = xml.match(/Pht="([^"]+)"/i);
  const b64 = m?.[1]?.replace(/\s+/g, '');
  if (!b64) return null;
  return `data:image/jpeg;base64,${b64}`;
};

// ─── Small shared UI atoms (match the app's inline-token styling) ──────────────
const Banner: React.FC<{ kind: 'success' | 'error' | 'info'; children: React.ReactNode }> = ({ kind, children }) => {
  const map = {
    success: { c: T.success, bg: T.successBg, icon: '✓' },
    error:   { c: T.danger,  bg: T.dangerBg,  icon: '⚠' },
    info:    { c: T.info,    bg: T.infoBg,    icon: 'ℹ' },
  }[kind];
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px', borderRadius: 10, background: map.bg, color: map.c, fontSize: 13, fontWeight: 600, marginBottom: 16 }}>
      <span style={{ fontSize: 15 }}>{map.icon}</span>{children}
    </div>
  );
};

const Spinner: React.FC = () => (
  <span style={{ width: 14, height: 14, border: `2px solid rgba(255,255,255,0.5)`, borderTopColor: '#fff', borderRadius: '50%', display: 'inline-block', animation: 'kycspin 0.7s linear infinite' }} />
);

// Coloured status pill for PENDING / SUCCESS / FAILED (and the friendly labels).
const StatusPill: React.FC<{ status?: string | null }> = ({ status }) => {
  const s = String(status || '').toUpperCase();
  const map: Record<string, { c: string; bg: string; label: string }> = {
    SUCCESS: { c: T.success, bg: T.successBg, label: 'Verified' },
    PENDING: { c: T.warning, bg: T.warningBg, label: 'Pending' },
    FAILED:  { c: T.danger,  bg: T.dangerBg,  label: 'Failed' },
  };
  const m = map[s] || { c: T.textMuted, bg: T.borderLight, label: status || '—' };
  return <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, color: m.c, background: m.bg, whiteSpace: 'nowrap' }}>{m.label}</span>;
};

// Shell that wraps every verification view: title, back link, and children.
const VerifyShell: React.FC<{ icon: string; view?: ViewKey; title: string; children: React.ReactNode; onBack: () => void }> = ({ icon, view, title, children, onBack }) => (
  <div style={{ maxWidth: 720 }}>
    <button onClick={onBack} style={{ background: 'none', border: 'none', color: T.blue, fontWeight: 700, fontSize: 13, cursor: 'pointer', padding: 0, marginBottom: 14, fontFamily: 'inherit' }}>← Back to KYC Dashboard</button>
    <Card style={{ padding: 22 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 18 }}>
        <div style={{ width: 44, height: 44, borderRadius: 12, overflow: 'hidden', background: `${T.blue}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>{view ? <KycIcon view={view} emoji={icon} /> : icon}</div>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>{title}</h2>
      </div>
      {children}
    </Card>
  </div>
);

// ─── Membership lookup field (shared by Aadhaar & PAN) ─────────────────────────
// Auto-fills Member Name for a Membership ID; sets `error` = "Membership not found." for
// unknown IDs so the parent can block the action.
const useMemberLookup = () => {
  const [memberId, setMemberId] = useState('');
  const [memberName, setMemberName] = useState('');
  const [error, setError] = useState('');
  const [looking, setLooking] = useState(false);

  const lookup = useCallback(async (raw: string) => {
    const id = raw.trim();
    setMemberName(''); setError('');
    if (!id) return;
    setLooking(true);
    try {
      const r = await kycAPI.lookupMember(id);
      setMemberName(r.memberName || '');
    } catch (e) {
      setError(kycErrorMessage(e, 'Membership not found.'));
    } finally { setLooking(false); }
  }, []);

  const reset = () => { setMemberId(''); setMemberName(''); setError(''); };
  return { memberId, setMemberId, memberName, error, looking, lookup, reset };
};

const MembershipFields: React.FC<{ m: ReturnType<typeof useMemberLookup> }> = ({ m }) => (
  <>
    <Input
      label="Membership ID"
      value={m.memberId}
      onChange={(e) => m.setMemberId(e.target.value)}
      onBlur={() => m.lookup(m.memberId)}
      placeholder="Enter Membership ID"
      hint={m.looking ? 'Looking up member…' : undefined}
    />
    {m.error && <div style={{ marginTop: -8, marginBottom: 14, fontSize: 12, fontWeight: 700, color: T.danger }}>{m.error}</div>}
    <Input label="Member Name" value={m.memberName} onChange={() => {}} placeholder="Auto-filled from Membership ID" readOnly />
  </>
);

interface FlowProps { onDone: () => void; onBack: () => void; }

// ─── Aadhaar (membership → generate DigiLocker link → poll status) ─────────────
const AadhaarView: React.FC<FlowProps> = ({ onDone, onBack }) => {
  const { showToast } = useToast();
  const m = useMemberLookup();
  const [generating, setGenerating] = useState(false);
  const [checking, setChecking] = useState(false);
  const [link, setLink] = useState('');
  const [referenceId, setReferenceId] = useState('');
  const [historyId, setHistoryId] = useState<number | null>(null);
  const [status, setStatus] = useState<string>('');

  const canGenerate = Boolean(m.memberName) && !m.error && !generating;

  const generate = async () => {
    if (!m.memberName) { showToast('Enter a valid Membership ID first.', 'error'); return; }
    setGenerating(true); setLink(''); setStatus('');
    try {
      const r = await kycAPI.generateAadhaarLink(m.memberId.trim());
      setLink(r.link); setReferenceId(r.referenceId); setHistoryId(r.id); setStatus(r.status);
      showToast('Verification link generated.', 'success');
      onDone();
    } catch (e) {
      showToast(kycErrorMessage(e, 'Could not generate the verification link.'), 'error');
      onDone();   // a FAILED attempt is still persisted — refresh so it appears in history
    } finally { setGenerating(false); }
  };

  const checkStatus = async () => {
    if (historyId == null) return;
    setChecking(true);
    try {
      const r = await kycAPI.getAadhaarStatus(historyId);
      setStatus(r.status);
      if (r.pending) showToast('Verification is still under process.', 'info');
      else if (r.status === 'SUCCESS') showToast('Aadhaar verified successfully.', 'success');
      else showToast(r.error || 'Aadhaar verification failed.', 'error');
      onDone();
    } catch (e) {
      showToast(kycErrorMessage(e, 'Could not check the verification status.'), 'error');
    } finally { setChecking(false); }
  };

  const copyLink = async () => {
    try { await navigator.clipboard.writeText(link); showToast('Link copied to clipboard.', 'success'); }
    catch { showToast('Could not copy the link.', 'error'); }
  };

  return (
    <VerifyShell icon="🆔" view="aadhaar" title="Aadhaar Verification" onBack={onBack}>
      <MembershipFields m={m} />

      <Btn onClick={generate} disabled={!canGenerate}>
        {generating ? <><Spinner /> Generating…</> : 'Generate Link'}
      </Btn>

      {status && (
        <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Verification Status</span>
          <StatusPill status={status} />
        </div>
      )}

      {link && (
        <Card style={{ marginTop: 18 }}>
          <div style={{ padding: '12px 18px', borderBottom: `1px solid ${T.border}`, background: T.canvas, fontSize: 13, fontWeight: 800, color: T.textMain }}>Generated Verification Link</div>
          <div style={{ padding: 18 }}>
            <div style={{ fontSize: 13, color: T.blue, wordBreak: 'break-all', marginBottom: 8 }}>
              <a href={link} target="_blank" rel="noreferrer" style={{ color: T.blue }}>{link}</a>
            </div>
            {referenceId && <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 14 }}>Reference ID: <span style={{ fontFamily: 'monospace' }}>{referenceId}</span></div>}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <Btn size="sm" onClick={copyLink}>📋 Copy Link</Btn>
              <Btn size="sm" variant="ghost" onClick={checkStatus} disabled={checking}>{checking ? <><Spinner /> Checking…</> : '↻ Check Verification Status'}</Btn>
            </div>
          </div>
        </Card>
      )}
    </VerifyShell>
  );
};

// ─── PAN (membership → verify PAN) ─────────────────────────────────────────────
const PanView: React.FC<FlowProps> = ({ onDone, onBack }) => {
  const { showToast } = useToast();
  const m = useMemberLookup();
  const [pan, setPan] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<{ validPan: boolean } | null>(null);

  const validPanFmt = KYC_VALIDATION.pan(pan);
  const canVerify = Boolean(m.memberName) && !m.error && validPanFmt && !verifying;

  const verify = async () => {
    if (!m.memberName) { showToast('Enter a valid Membership ID first.', 'error'); return; }
    if (!validPanFmt) { showToast('Invalid PAN Number — expected format ABCDE1234F.', 'error'); return; }
    setVerifying(true); setResult(null);
    try {
      const r = await kycAPI.verifyPanMembership(m.memberId.trim(), pan.toUpperCase().trim());
      setResult({ validPan: r.validPan });
      showToast(r.validPan ? 'PAN verified successfully.' : 'PAN verification completed.', 'success');
      onDone();
    } catch (e) {
      showToast(kycErrorMessage(e, 'PAN verification failed.'), 'error');
      onDone();   // a FAILED attempt is still persisted — refresh so it appears in history
    } finally { setVerifying(false); }
  };

  return (
    <VerifyShell icon="💳" view="pan" title="PAN Verification" onBack={onBack}>
      <MembershipFields m={m} />
      <Input label="PAN Number" value={pan} onChange={(e) => setPan(e.target.value.toUpperCase())} placeholder="ABCDE1234F" hint="10-character PAN" />
      <Btn onClick={verify} disabled={!canVerify}>{verifying ? <><Spinner /> Verifying…</> : 'Verify PAN'}</Btn>
      {result && (
        <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Valid PAN</span>
          <span style={{ fontSize: 12, fontWeight: 800, padding: '3px 12px', borderRadius: 20, color: result.validPan ? T.success : T.danger, background: result.validPan ? T.successBg : T.dangerBg }}>{result.validPan ? 'YES' : 'NO'}</span>
          <span style={{ fontSize: 12, color: T.textMuted }}>See the Verification History for full details.</span>
        </div>
      )}
    </VerifyShell>
  );
};

// ─── Passport (membership → verify passport number) ────────────────────────────
const PassportView: React.FC<FlowProps> = ({ onDone, onBack }) => {
  const { showToast } = useToast();
  const m = useMemberLookup();
  const [num, setNum] = useState('');
  const [dob, setDob] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<{ validPassport: boolean } | null>(null);

  const validFmt = KYC_VALIDATION.passport(num);
  const canVerify = Boolean(m.memberName) && !m.error && validFmt && !verifying;

  const verify = async () => {
    if (!m.memberName) { showToast('Enter a valid Membership ID first.', 'error'); return; }
    if (!validFmt) { showToast('Invalid Passport Number — expected format A1234567.', 'error'); return; }
    setVerifying(true); setResult(null);
    try {
      const r = await kycAPI.verifyPassportMembership(m.memberId.trim(), num.toUpperCase().trim(), dob || undefined);
      setResult({ validPassport: r.validPassport });
      showToast(r.validPassport ? 'Passport verified successfully.' : 'Passport verification completed.', 'success');
      onDone();
    } catch (e) {
      showToast(kycErrorMessage(e, 'Passport verification failed.'), 'error');
      onDone();   // a FAILED attempt is still persisted — refresh so it appears in history
    } finally { setVerifying(false); }
  };

  return (
    <VerifyShell icon="📘" view="passport" title="Passport Verification" onBack={onBack}>
      <MembershipFields m={m} />
      <Input label="Passport Number" value={num} onChange={e => setNum(e.target.value.toUpperCase())} placeholder="A1234567" hint="8-character passport number" />
      <Input label="Date of Birth" type="date" value={dob} onChange={e => setDob(e.target.value)} hint="YYYY-MM-DD" />
      <Btn onClick={verify} disabled={!canVerify}>{verifying ? <><Spinner /> Verifying…</> : 'Verify Passport'}</Btn>
      {result && (
        <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Valid Passport</span>
          <span style={{ fontSize: 12, fontWeight: 800, padding: '3px 12px', borderRadius: 20, color: result.validPassport ? T.success : T.danger, background: result.validPassport ? T.successBg : T.dangerBg }}>{result.validPassport ? 'YES' : 'NO'}</span>
          <span style={{ fontSize: 12, color: T.textMuted }}>See the Verification History for full details.</span>
        </div>
      )}
    </VerifyShell>
  );
};

// ─── OCR Document (membership → upload → verify via General-Document API) ───────
const OcrView: React.FC<FlowProps> = ({ onDone, onBack }) => {
  const { showToast } = useToast();
  const m = useMemberLookup();
  const [docType, setDocType] = useState(OCR_DOC_TYPES[0].value);
  const [verification, setVerification] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [dataUrl, setDataUrl] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<{ verified: boolean } | null>(null);

  const onFile = (f: File | null) => {
    setResult(null); setDataUrl(''); setFile(null);
    if (!f) return;
    const ext = f.name.split('.').pop()?.toLowerCase() || '';
    if (!['jpg', 'jpeg', 'png', 'pdf'].includes(ext)) { showToast('Unsupported file type — allowed: JPG, JPEG, PNG, PDF.', 'error'); return; }
    if (f.size > OCR_MAX_BYTES) { showToast('File too large — maximum size is 10 MB.', 'error'); return; }
    const reader = new FileReader();
    reader.onload = () => { setDataUrl(String(reader.result)); setFile(f); };
    reader.onerror = () => showToast('Could not read the file — please try again.', 'error');
    reader.readAsDataURL(f);
  };

  const canVerify = Boolean(m.memberName) && !m.error && Boolean(file) && Boolean(dataUrl) && !verifying;

  const submit = async () => {
    if (!m.memberName) { showToast('Enter a valid Membership ID first.', 'error'); return; }
    if (!file || !dataUrl) { showToast('Please select a document to verify.', 'error'); return; }
    setVerifying(true); setResult(null);
    try {
      const r = await kycAPI.verifyOcrMembership(m.memberId.trim(), docType, file.name, dataUrl, verification);
      setResult({ verified: r.verified });
      showToast(r.verified ? 'Document verified successfully.' : 'OCR verification completed.', 'success');
      onDone();
    } catch (e) {
      showToast(kycErrorMessage(e, 'OCR verification failed.'), 'error');
      onDone();   // a FAILED attempt is still persisted — refresh so it appears in history
    } finally { setVerifying(false); }
  };

  const isImage = file && /\.(jpg|jpeg|png)$/i.test(file.name);

  return (
    <VerifyShell icon="📄" title="OCR Document Verification" onBack={onBack}>
      <MembershipFields m={m} />
      <Sel label="Document Type" value={docType} onChange={e => setDocType(e.target.value)} options={OCR_DOC_TYPES} />
      <Sel label="Verification" value={verification ? 'yes' : 'no'} onChange={e => setVerification(e.target.value === 'yes')}
        options={[{ value: 'yes', label: 'Yes — validate the document' }, { value: 'no', label: 'No — extract only' }]} />
      <label style={{ display: 'block', fontSize: 12, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Upload Document</label>
      <input type="file" accept={OCR_ACCEPT} onChange={e => onFile(e.target.files?.[0] || null)}
        style={{ width: '100%', padding: '10px 14px', border: `1.5px dashed ${T.border}`, borderRadius: 10, fontSize: 13, color: T.textMain, background: T.canvas, cursor: 'pointer', fontFamily: 'inherit', boxSizing: 'border-box' }} />
      <p style={{ fontSize: 11, color: T.textMuted, margin: '4px 0 14px' }}>Supported: JPG, JPEG, PNG, PDF · Max 10 MB</p>
      {file && (
        <div style={{ marginBottom: 16 }}>
          {isImage
            ? <img src={dataUrl} alt="Preview" style={{ maxWidth: 220, maxHeight: 160, borderRadius: 10, border: `1px solid ${T.border}` }} />
            : <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 14px', border: `1px solid ${T.border}`, borderRadius: 10, fontSize: 13, color: T.textMain }}>📄 {file.name}</div>}
        </div>
      )}
      <Btn onClick={submit} disabled={!canVerify}>{verifying ? <><Spinner /> Verifying…</> : 'Verify OCR'}</Btn>
      {result && (
        <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Verified</span>
          <span style={{ fontSize: 12, fontWeight: 800, padding: '3px 12px', borderRadius: 20, color: result.verified ? T.success : T.danger, background: result.verified ? T.successBg : T.dangerBg }}>{result.verified ? 'YES' : 'NO'}</span>
          <span style={{ fontSize: 12, color: T.textMuted }}>See the Verification History for full details.</span>
        </div>
      )}
    </VerifyShell>
  );
};

// ─── View Details popup (Aadhaar / PAN / Passport / OCR) ───────────────────────
const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={{ marginBottom: 18 }}>
    <div style={{ fontSize: 12, fontWeight: 800, color: T.textMain, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 10, paddingBottom: 6, borderBottom: `1px solid ${T.border}` }}>{title}</div>
    {children}
  </div>
);

const KVGrid: React.FC<{ rows: Array<[string, React.ReactNode]> }> = ({ rows }) => (
  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '12px 20px' }}>
    {rows.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => (
      <div key={k}>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{k}</div>
        <div style={{ fontSize: 13, fontWeight: 600, color: T.textMain, wordBreak: 'break-word' }}>{v as React.ReactNode}</div>
      </div>
    ))}
  </div>
);

// Render an arbitrary object as a prettified KV grid (used for PAN sub-objects).
const ObjectGrid: React.FC<{ obj?: Record<string, unknown> | null }> = ({ obj }) => {
  const entries = obj ? Object.entries(obj).filter(([, v]) => typeof v !== 'object' || v === null) : [];
  if (!entries.length) return <div style={{ fontSize: 12, color: T.textMuted }}>No data.</div>;
  return <KVGrid rows={entries.map(([k, v]) => [prettify(k), String(v ?? '—')] as [string, React.ReactNode])} />;
};

// Melento nests the actual Aadhaar fields under result.validated_data.result; older/flatter
// responses may carry them higher up. Unwrap to the first object that holds recognizable fields
// so Basic Information renders regardless of nesting depth.
const pickAadhaarData = (raw: AadhaarDetails | Record<string, unknown> | null | undefined): AadhaarDetails => {
  const r = raw as Record<string, any> | null | undefined;
  const candidates = [r, r?.result, r?.result?.validated_data, r?.result?.validated_data?.result, r?.validated_data, r?.validated_data?.result];
  for (const c of candidates) {
    if (c && typeof c === 'object' && (c.name || c.uid || c.dob)) return c as AadhaarDetails;
  }
  return (raw || {}) as AadhaarDetails;
};

const AadhaarDetailsBody: React.FC<{ data: AadhaarDetails }> = ({ data: raw }) => {
  const data = pickAadhaarData(raw);
  const split = (data.split_address || {}) as Record<string, string>;
  const photo = extractAadhaarPhoto(data.xml_file);
  const SPLIT_ORDER = ['country', 'state', 'district', 'subdistrict', 'sub_district', 'vtc', 'village', 'town', 'street', 'house', 'landmark', 'po', 'post_office', 'pincode', 'pin_code', 'pc'];
  const splitEntries = Object.entries(split).sort((a, b) => {
    const ia = SPLIT_ORDER.indexOf(a[0].toLowerCase()); const ib = SPLIT_ORDER.indexOf(b[0].toLowerCase());
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return (
    <>
      <Section title="Basic Information">
        <KVGrid rows={[
          ['Name', data.name], ['UID', data.uid], ['Date of Birth', data.dob], ['Gender', data.gender],
          ['Care Of', data.care_of], ['Address', data.address],
        ]} />
      </Section>
      {splitEntries.length > 0 && (
        <Section title="Split Address">
          <KVGrid rows={splitEntries.map(([k, v]) => [prettify(k), v] as [string, React.ReactNode])} />
        </Section>
      )}
      {photo && (
        <Section title="Aadhaar Photo">
          <img src={photo} alt="Aadhaar" style={{ width: 130, height: 160, objectFit: 'cover', borderRadius: 10, border: `1px solid ${T.border}` }} />
        </Section>
      )}
    </>
  );
};

const PanDetailsBody: React.FC<{ response: Record<string, unknown> }> = ({ response }) => {
  const result = (response?.result || {}) as Record<string, unknown>;
  const extracted = (result.extracted_data || {}) as Record<string, unknown>;
  const validated = (result.validated_data || {}) as Record<string, unknown>;
  const match = (result.data_match || {}) as Record<string, unknown>;
  const validPan = Boolean(result.valid_pan);
  return (
    <>
      <Section title="Extracted Data"><ObjectGrid obj={extracted} /></Section>
      <Section title="Validated Data"><ObjectGrid obj={validated} /></Section>
      <Section title="Data Match">
        <ObjectGrid obj={match} />
        {result.data_match_aggregate != null && (
          <div style={{ marginTop: 10, fontSize: 12, color: T.textMuted }}>Aggregate Match: <strong style={{ color: T.textMain }}>{String(result.data_match_aggregate)}</strong></div>
        )}
      </Section>
      <Section title="PAN Status">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted }}>Valid PAN</span>
          <span style={{ fontSize: 13, fontWeight: 800, padding: '3px 14px', borderRadius: 20, color: validPan ? T.success : T.danger, background: validPan ? T.successBg : T.dangerBg }}>{validPan ? 'YES' : 'NO'}</span>
        </div>
      </Section>
    </>
  );
};

// Passport response → Extracted / Validated / Data-Match sections (mirrors PAN); the exact keys
// returned by the provider are rendered dynamically so no field is ever dropped.
const PassportDetailsBody: React.FC<{ response: Record<string, unknown> }> = ({ response }) => {
  const result = (response?.result || {}) as Record<string, unknown>;
  // profile_image is a base64 portrait in extracted_data — render it as a photo, not raw text.
  const { profile_image, ...extracted } = (result.extracted_data || {}) as Record<string, unknown>;
  const photo = asImageSrc(profile_image);
  const validated = (result.validated_data || {}) as Record<string, unknown>;
  const match = (result.data_match || {}) as Record<string, unknown>;
  const validPassport = Boolean(result.valid_passport);
  return (
    <>
      <Section title="Passport Information">
        <ObjectGrid obj={extracted} />
        {photo && <img src={photo} alt="Passport photo" style={{ marginTop: 12, width: 110, height: 140, objectFit: 'cover', borderRadius: 10, border: `1px solid ${T.border}` }} />}
      </Section>
      <Section title="Validation Result"><ObjectGrid obj={validated} /></Section>
      <Section title="Data Match">
        <ObjectGrid obj={match} />
        {result.data_match_aggregate != null && (
          <div style={{ marginTop: 10, fontSize: 12, color: T.textMuted }}>Aggregate Match: <strong style={{ color: T.textMain }}>{String(result.data_match_aggregate)}</strong></div>
        )}
      </Section>
      <Section title="Passport Status">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted }}>Valid Passport</span>
          <span style={{ fontSize: 13, fontWeight: 800, padding: '3px 14px', borderRadius: 20, color: validPassport ? T.success : T.danger, background: validPassport ? T.successBg : T.dangerBg }}>{validPassport ? 'YES' : 'NO'}</span>
        </div>
      </Section>
    </>
  );
};

// Detect a base64/data-URL image value (graphic fields carry photo/signature crops).
const asImageSrc = (v: unknown): string | null => {
  if (typeof v !== 'string' || v.length < 100) return null;
  if (v.startsWith('data:image')) return v;
  if (/^[A-Za-z0-9+/=\s]+$/.test(v)) return `data:image/jpeg;base64,${v.replace(/\s+/g, '')}`;
  return null;
};

// A grid of base64 image crops (OCR graphic_fields: photo, signature, …).
const GraphicGrid: React.FC<{ obj?: Record<string, unknown> | null }> = ({ obj }) => {
  const imgs = obj ? Object.entries(obj).map(([k, v]) => [k, asImageSrc(v)] as const).filter(([, s]) => s) : [];
  if (!imgs.length) return <ObjectGrid obj={obj} />;
  return (
    <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
      {imgs.map(([k, src]) => (
        <div key={k}>
          <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>{prettify(k)}</div>
          <img src={src as string} alt={k} style={{ maxWidth: 130, maxHeight: 160, objectFit: 'contain', borderRadius: 10, border: `1px solid ${T.border}` }} />
        </div>
      ))}
    </div>
  );
};

// General-Document (OCR) response — rendered fully dynamically since each doc_type returns a
// different structure. Every returned key is surfaced under an appropriate section.
const OcrDetailsBody: React.FC<{ response: Record<string, unknown> }> = ({ response }) => {
  const r = response || {};
  const result = (r.result || {}) as Record<string, unknown>;
  const extracted = (r.extracted_data || result.extracted_data || {}) as Record<string, unknown>;
  const validated = (r.validated_data || result.validated_data || {}) as Record<string, unknown>;
  const graphic = (r.graphic_fields || result.graphic_fields || {}) as Record<string, unknown>;
  const verified = Boolean(r.verified);
  const has = (o: Record<string, unknown>) => o && Object.keys(o).length > 0;
  return (
    <>
      <Section title="Document Details">
        <KVGrid rows={[
          ['Document Type', r.document_type ? prettify(String(r.document_type)) : undefined],
          ['MRZ Validity', r.mrzvalidity != null ? String(r.mrzvalidity) : undefined],
          ['Message', r.message as React.ReactNode],
        ]} />
      </Section>
      {has(extracted) && <Section title="Extracted Information"><ObjectGrid obj={extracted} /></Section>}
      {has(validated) && <Section title="Validated Information"><ObjectGrid obj={validated} /></Section>}
      {/* Any remaining scalar keys inside result that aren't the nested objects above. */}
      {has(result) && <Section title="Verification Result"><ObjectGrid obj={result} /></Section>}
      {has(graphic) && <Section title="Graphic Fields"><GraphicGrid obj={graphic} /></Section>}
      <Section title="Verification Summary">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.textMuted }}>Verified</span>
          <span style={{ fontSize: 13, fontWeight: 800, padding: '3px 14px', borderRadius: 20, color: verified ? T.success : T.danger, background: verified ? T.successBg : T.dangerBg }}>{verified ? 'YES' : 'NO'}</span>
        </div>
      </Section>
    </>
  );
};

const TYPE_TITLE: Record<string, string> = {
  AADHAAR: 'Aadhaar', PAN: 'PAN', PASSPORT: 'Passport', OCR: 'OCR Document',
};

const ViewDetailsModal: React.FC<{ item: KycHistoryItem; onClose: () => void; onRefresh: () => void }> = ({ item, onClose, onRefresh }) => {
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<KycHistoryDetail | null>(null);
  const [aadhaar, setAadhaar] = useState<AadhaarDetails | null>(null);
  const [pendingMsg, setPendingMsg] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true); setErr(''); setPendingMsg('');
      try {
        const d = await kycAPI.getHistoryDetail(item.id);
        if (!alive) return;
        setDetail(d);
        if (item.verificationType === 'AADHAAR') {
          if (d.status === 'SUCCESS' && d.response) {
            setAadhaar(d.response as AadhaarDetails);
          } else {
            // Poll DigiLocker for the latest status.
            const s = await kycAPI.getAadhaarStatus(item.id);
            if (!alive) return;
            if (s.pending) setPendingMsg('Verification Under Process');
            else if (s.status === 'SUCCESS' && s.details) { setAadhaar(s.details); onRefresh(); }
            else { setErr(s.error || 'Aadhaar verification failed.'); onRefresh(); }
          }
        }
      } catch (e) {
        if (alive) setErr(kycErrorMessage(e, 'Could not load the verification details.'));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id]);

  const isAadhaar = item.verificationType === 'AADHAAR';
  const title = TYPE_TITLE[item.verificationType] || item.verificationType;

  return (
    <Modal title={`${title} Verification Details`} onClose={onClose} wide>
      <style>{`@keyframes kycspin{to{transform:rotate(360deg)}}`}</style>
      {/* Header summary */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 20px', marginBottom: 18, fontSize: 12, color: T.textMuted }}>
        <span>Membership ID: <strong style={{ color: T.textMain }}>{item.membershipId || '—'}</strong></span>
        <span>Member: <strong style={{ color: T.textMain }}>{item.memberName || '—'}</strong></span>
        {item.verificationType === 'OCR' && item.documentType && (
          <span>Document: <strong style={{ color: T.textMain }}>{prettify(item.documentType)}</strong></span>
        )}
        <span>Reference: <strong style={{ color: T.textMain, fontFamily: 'monospace' }}>{item.referenceId || '—'}</strong></span>
        <StatusPill status={detail?.status || item.status} />
      </div>

      {loading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '30px 0', justifyContent: 'center', color: T.textMuted, fontSize: 13 }}>
          <span style={{ width: 18, height: 18, border: `2px solid ${T.border}`, borderTopColor: T.blue, borderRadius: '50%', display: 'inline-block', animation: 'kycspin 0.7s linear infinite' }} />
          Loading details…
        </div>
      )}

      {!loading && pendingMsg && (
        <div style={{ padding: '28px 18px', textAlign: 'center', background: T.warningBg, color: T.warning, borderRadius: 12, fontSize: 15, fontWeight: 700 }}>
          ⏳ {pendingMsg}
        </div>
      )}

      {!loading && err && <Banner kind="error">{err}</Banner>}

      {!loading && !pendingMsg && isAadhaar && aadhaar && <AadhaarDetailsBody data={aadhaar} />}
      {!loading && !pendingMsg && item.verificationType === 'PAN' && detail?.response && <PanDetailsBody response={detail.response} />}
      {!loading && !pendingMsg && item.verificationType === 'PASSPORT' && detail?.response && <PassportDetailsBody response={detail.response} />}
      {!loading && !pendingMsg && item.verificationType === 'OCR' && detail?.response && <OcrDetailsBody response={detail.response} />}
      {!loading && !pendingMsg && !isAadhaar && !detail?.response && !err && (
        <Banner kind="info">No response data available for this record.</Banner>
      )}
      {/* A FAILED record still shows its stored error so no information is hidden. */}
      {!loading && !pendingMsg && !isAadhaar && detail?.response && detail?.errorMessage && (
        <Banner kind="error">{detail.errorMessage}</Banner>
      )}
    </Modal>
  );
};

// ─── History table (DB-backed — Aadhaar / PAN / Passport / OCR) ────────────────
const HistoryTable: React.FC<{ rows: KycHistoryItem[]; loading: boolean; onView: (r: KycHistoryItem) => void }> = ({ rows, loading, onView }) => (
  <Card style={{ marginTop: 24 }}>
    <div style={{ padding: '14px 18px', borderBottom: `1px solid ${T.border}` }}>
      <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>Verification History</h2>
      <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>Every Aadhaar, PAN, Passport and OCR verification request for your business.</p>
    </div>
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: T.canvas }}>
            {['Membership ID', 'Member Name', 'Verification Type', 'Reference ID', 'Transaction ID', 'Status', 'Created By', 'Created Date & Time', 'Action'].map(h => (
              <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `2px solid ${T.border}`, whiteSpace: 'nowrap' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading && <tr><td colSpan={9} style={{ padding: 28, textAlign: 'center', color: T.textMuted }}>Loading…</td></tr>}
          {!loading && rows.length === 0 && <tr><td colSpan={9} style={{ padding: 28, textAlign: 'center', color: T.textMuted }}>No verifications yet.</td></tr>}
          {!loading && rows.map((r, i) => (
            <tr key={r.id} style={{ background: i % 2 === 0 ? T.surface : T.canvas }}>
              <td style={{ padding: '11px 14px', fontWeight: 700, color: T.textMain, whiteSpace: 'nowrap' }}>{r.membershipId || '—'}</td>
              <td style={{ padding: '11px 14px', color: T.textMain }}>{r.memberName || '—'}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted }}>{r.verificationType}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{r.referenceId || '—'}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{r.transactionId || '—'}</td>
              <td style={{ padding: '11px 14px' }}><StatusPill status={r.status} /></td>
              <td style={{ padding: '11px 14px', color: T.textMuted, whiteSpace: 'nowrap' }}>{r.createdBy || '—'}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted, whiteSpace: 'nowrap' }}>{fmtIST(r.createdAt)}</td>
              <td style={{ padding: '11px 14px' }}><Btn size="sm" variant="ghost" onClick={() => onView(r)}>View Details</Btn></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </Card>
);

// ─── Page root ─────────────────────────────────────────────────────────────────
export const KYCPage: React.FC<{ user: User }> = ({ user }) => {
  const [view, setView] = useState<ViewKey>('home');
  const [history, setHistory] = useState<KycHistoryItem[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [detailItem, setDetailItem] = useState<KycHistoryItem | null>(null);

  const loadHistory = useCallback(async () => {
    setLoadingHistory(true);
    try { setHistory(await kycAPI.listHistory()); }
    catch { /* leave prior rows on transient error */ }
    finally { setLoadingHistory(false); }
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const back = () => { setView('home'); loadHistory(); };
  const flowProps: FlowProps = { onDone: loadHistory, onBack: back };

  return (
    <div>
      <style>{`@keyframes kycspin{to{transform:rotate(360deg)}}`}</style>
      <div style={{ marginBottom: 18 }}>
        <h1 style={{ margin: '0 0 3px', fontSize: 20, fontWeight: 800, color: T.textMain }}>KYC Verification Dashboard</h1>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>Verify customer identity documents securely. Available to Supervisor and Manager roles.</p>
      </div>

      {view === 'home' && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 16 }}>
            {CARDS.map(c => (
              <Card key={c.key} className="c5-hover-lift" onClick={() => setView(c.key)}
                style={{ padding: 20, cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ width: 48, height: 48, borderRadius: 14, overflow: 'hidden', background: `${T.blue}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 24 }}><KycIcon view={c.key} emoji={c.icon} /></div>
                <div style={{ fontSize: 15, fontWeight: 800, color: T.textMain }}>{c.title}</div>
                <div style={{ fontSize: 12, color: T.textMuted, lineHeight: 1.5, flex: 1 }}>{c.desc}</div>
                <Btn size="sm" full onClick={() => setView(c.key)}>{`Verify ${TYPE_LABEL[c.key]}`}</Btn>
              </Card>
            ))}
          </div>
          <HistoryTable rows={history} loading={loadingHistory} onView={setDetailItem} />
        </>
      )}

      {view === 'aadhaar' && <AadhaarView {...flowProps} />}
      {view === 'pan' && <PanView {...flowProps} />}
      {view === 'passport' && <PassportView {...flowProps} />}
      {view === 'ocr' && <OcrView {...flowProps} />}

      {detailItem && <ViewDetailsModal item={detailItem} onClose={() => setDetailItem(null)} onRefresh={loadHistory} />}
    </div>
  );
};

export default KYCPage;
