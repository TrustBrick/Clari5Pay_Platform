import React, { useState } from 'react';
import type { User } from '../types';
import { T } from '../utils/theme';
import { Card, Btn, Input, Sel } from '../components/UI';
import {
  kycAPI, KYC_VALIDATION, OCR_ACCEPT, OCR_MAX_BYTES, kycErrorMessage,
  type AadhaarResult, type PanResult, type PassportResult, type OcrResult, type DigiLockerSession,
} from '../services/kyc';

// ─── Merchant Portal → KYC Update ──────────────────────────────────────────────
// Identity-verification workspace for Supervisor / Manager roles. Five providers
// (Aadhaar, PAN, Passport, OCR, DigiLocker) share one dashboard. The UI, validation,
// loading/error/success states and a session verification history are all live; the
// actual provider responses arrive once the Melento.ai / DigiLocker APIs are connected.

type ViewKey = 'home' | 'aadhaar' | 'pan' | 'passport' | 'ocr' | 'digilocker';

interface CardDef { key: ViewKey; icon: string; title: string; desc: string; }
const CARDS: CardDef[] = [
  { key: 'aadhaar',    icon: '🆔', title: 'Aadhaar Verification',      desc: 'Verify a customer using their 12-digit Aadhaar number.' },
  { key: 'pan',        icon: '💳', title: 'PAN Verification',          desc: 'Validate a PAN and fetch the holder’s details.' },
  { key: 'passport',   icon: '📘', title: 'Passport Verification',     desc: 'Verify a passport number and its validity.' },
  { key: 'ocr',        icon: '📄', title: 'OCR Document Verification', desc: 'Extract details from an uploaded identity document.' },
  { key: 'digilocker', icon: '🔐', title: 'DigiLocker Verification',   desc: 'Securely verify customer documents through DigiLocker.' },
];
const TYPE_LABEL: Record<ViewKey, string> = {
  home: '', aadhaar: 'Aadhaar', pan: 'PAN', passport: 'Passport', ocr: 'OCR Document', digilocker: 'DigiLocker',
};

export interface KycHistoryRow {
  id: number;
  dateTime: string;
  type: string;
  docNumber: string;   // already masked
  customerName: string;
  verifiedBy: string;
  status: 'Verified' | 'Failed';
}

const maskNumber = (v: string): string => {
  const s = (v || '').replace(/\s/g, '');
  if (s.length <= 4) return s || '—';
  return '•'.repeat(Math.max(0, s.length - 4)) + s.slice(-4);
};
const nowIST = () => new Date().toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: true });

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

const ResultCard: React.FC<{ title: string; rows: Array<[string, React.ReactNode]>; photo?: string | null }> = ({ title, rows, photo }) => (
  <Card style={{ marginTop: 18 }}>
    <div style={{ padding: '12px 18px', borderBottom: `1px solid ${T.border}`, background: T.canvas, display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 13, fontWeight: 800, color: T.textMain }}>{title}</span>
      <span style={{ marginLeft: 'auto', fontSize: 11, fontWeight: 700, color: T.success, background: T.successBg, padding: '2px 10px', borderRadius: 20 }}>● Verified</span>
    </div>
    <div style={{ padding: 18, display: 'flex', gap: 20, flexWrap: 'wrap' }}>
      {photo && <img src={photo} alt="Document" style={{ width: 96, height: 96, objectFit: 'cover', borderRadius: 10, border: `1px solid ${T.border}` }} />}
      <div style={{ flex: '1 1 320px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: '12px 20px' }}>
        {rows.map(([k, v]) => (
          <div key={k}>
            <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{k}</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: T.textMain, wordBreak: 'break-word' }}>{v ?? '—'}</div>
          </div>
        ))}
      </div>
    </div>
  </Card>
);

// Shell that wraps every verification view: title, back link, and children.
const VerifyShell: React.FC<{ icon: string; title: string; children: React.ReactNode; onBack: () => void }> = ({ icon, title, children, onBack }) => (
  <div style={{ maxWidth: 720 }}>
    <button onClick={onBack} style={{ background: 'none', border: 'none', color: T.blue, fontWeight: 700, fontSize: 13, cursor: 'pointer', padding: 0, marginBottom: 14, fontFamily: 'inherit' }}>← Back to KYC Dashboard</button>
    <Card style={{ padding: 22 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 18 }}>
        <div style={{ width: 44, height: 44, borderRadius: 12, background: `${T.blue}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>{icon}</div>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: T.textMain }}>{title}</h2>
      </div>
      {children}
    </Card>
  </div>
);

interface ViewProps { onRecord: (r: Omit<KycHistoryRow, 'id' | 'dateTime' | 'verifiedBy'>) => void; onBack: () => void; }

// ─── Aadhaar ───────────────────────────────────────────────────────────────────
const AadhaarView: React.FC<ViewProps> = ({ onRecord, onBack }) => {
  const [num, setNum] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState(false);
  const [result, setResult] = useState<AadhaarResult | null>(null);

  const submit = async () => {
    setErr(''); setOk(false); setResult(null);
    if (!KYC_VALIDATION.aadhaar(num)) { setErr('Invalid Aadhaar Number — must be exactly 12 digits.'); return; }
    setLoading(true);
    try {
      const r = await kycAPI.verifyAadhaar(num.replace(/\s/g, ''));
      setResult(r); setOk(true);
      onRecord({ type: 'Aadhaar', docNumber: maskNumber(num), customerName: r.fullName || '—', status: 'Verified' });
    } catch (e) {
      setErr(kycErrorMessage(e, 'Aadhaar verification failed.'));
      onRecord({ type: 'Aadhaar', docNumber: maskNumber(num), customerName: '—', status: 'Failed' });
    } finally { setLoading(false); }
  };

  return (
    <VerifyShell icon="🆔" title="Aadhaar Verification" onBack={onBack}>
      {ok && <Banner kind="success">Verification completed successfully.</Banner>}
      {err && <Banner kind="error">{err}</Banner>}
      <Input label="Aadhaar Number" value={num} onChange={e => setNum(e.target.value.replace(/[^\d\s]/g, ''))} placeholder="1234 5678 9012" inputMode="numeric" hint="12-digit Aadhaar number" />
      <Btn onClick={submit} disabled={loading}>{loading ? <><Spinner /> Verifying...</> : 'Verify Aadhaar'}</Btn>
      {result && (
        <ResultCard title="Aadhaar Details" photo={result.photo} rows={[
          ['Aadhaar Number', maskNumber(result.aadhaarNumber || num)],
          ['Full Name', result.fullName], ['Date of Birth', result.dateOfBirth], ['Gender', result.gender],
          ['Address', result.address], ['State', result.state], ['District', result.district],
          ['Pincode', result.pincode], ['Verification Status', result.status],
        ]} />
      )}
    </VerifyShell>
  );
};

// ─── PAN ─────────────────────────────────────────────────────────────────────
const PanView: React.FC<ViewProps> = ({ onRecord, onBack }) => {
  const [num, setNum] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState(false);
  const [result, setResult] = useState<PanResult | null>(null);

  const submit = async () => {
    setErr(''); setOk(false); setResult(null);
    if (!KYC_VALIDATION.pan(num)) { setErr('Invalid PAN Number — expected format ABCDE1234F.'); return; }
    setLoading(true);
    try {
      const r = await kycAPI.verifyPAN(num.toUpperCase().trim());
      setResult(r); setOk(true);
      onRecord({ type: 'PAN', docNumber: maskNumber(num), customerName: r.fullName || '—', status: 'Verified' });
    } catch (e) {
      setErr(kycErrorMessage(e, 'PAN verification failed.'));
      onRecord({ type: 'PAN', docNumber: maskNumber(num), customerName: '—', status: 'Failed' });
    } finally { setLoading(false); }
  };

  return (
    <VerifyShell icon="💳" title="PAN Verification" onBack={onBack}>
      {ok && <Banner kind="success">Verification completed successfully.</Banner>}
      {err && <Banner kind="error">{err}</Banner>}
      <Input label="PAN Number" value={num} onChange={e => setNum(e.target.value.toUpperCase())} placeholder="ABCDE1234F" hint="10-character PAN" />
      <Btn onClick={submit} disabled={loading}>{loading ? <><Spinner /> Verifying...</> : 'Verify PAN'}</Btn>
      {result && (
        <ResultCard title="PAN Details" rows={[
          ['PAN Number', maskNumber(result.panNumber || num)], ['Full Name', result.fullName],
          ["Father's Name", result.fatherName], ['Date of Birth', result.dateOfBirth],
          ['Category', result.category], ['PAN Status', result.status],
        ]} />
      )}
    </VerifyShell>
  );
};

// ─── Passport ──────────────────────────────────────────────────────────────────
const PassportView: React.FC<ViewProps> = ({ onRecord, onBack }) => {
  const [num, setNum] = useState('');
  const [dob, setDob] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState(false);
  const [result, setResult] = useState<PassportResult | null>(null);

  const submit = async () => {
    setErr(''); setOk(false); setResult(null);
    if (!KYC_VALIDATION.passport(num)) { setErr('Invalid Passport Number — expected format A1234567.'); return; }
    setLoading(true);
    try {
      const r = await kycAPI.verifyPassport(num.toUpperCase().trim(), dob || undefined);
      setResult(r); setOk(true);
      onRecord({ type: 'Passport', docNumber: maskNumber(num), customerName: r.fullName || '—', status: 'Verified' });
    } catch (e) {
      setErr(kycErrorMessage(e, 'Passport Not Found.'));
      onRecord({ type: 'Passport', docNumber: maskNumber(num), customerName: '—', status: 'Failed' });
    } finally { setLoading(false); }
  };

  return (
    <VerifyShell icon="📘" title="Passport Verification" onBack={onBack}>
      {ok && <Banner kind="success">Verification completed successfully.</Banner>}
      {err && <Banner kind="error">{err}</Banner>}
      <Input label="Passport Number" value={num} onChange={e => setNum(e.target.value.toUpperCase())} placeholder="A1234567" hint="8-character passport number" />
      <Input label="Date of Birth" type="date" value={dob} onChange={e => setDob(e.target.value)} hint="Optional — required by some issuers" />
      <Btn onClick={submit} disabled={loading}>{loading ? <><Spinner /> Verifying...</> : 'Verify Passport'}</Btn>
      {result && (
        <ResultCard title="Passport Details" rows={[
          ['Passport Number', maskNumber(result.passportNumber || num)], ['Full Name', result.fullName],
          ['Nationality', result.nationality], ['Gender', result.gender], ['Date of Birth', result.dateOfBirth],
          ['Issue Date', result.issueDate], ['Expiry Date', result.expiryDate], ['Passport Status', result.status],
        ]} />
      )}
    </VerifyShell>
  );
};

// ─── OCR Document ──────────────────────────────────────────────────────────────
const OcrView: React.FC<ViewProps> = ({ onRecord, onBack }) => {
  const [docType, setDocType] = useState('AADHAAR_FRONT');
  const [file, setFile] = useState<File | null>(null);
  const [dataUrl, setDataUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState(false);
  const [result, setResult] = useState<OcrResult | null>(null);

  const onFile = (f: File | null) => {
    setErr(''); setOk(false); setResult(null); setDataUrl(''); setFile(null);
    if (!f) return;
    const ext = f.name.split('.').pop()?.toLowerCase() || '';
    if (!['jpg', 'jpeg', 'png', 'pdf'].includes(ext)) { setErr('Unsupported file type — allowed: JPG, JPEG, PNG, PDF.'); return; }
    if (f.size > OCR_MAX_BYTES) { setErr('File too large — maximum size is 10 MB.'); return; }
    const reader = new FileReader();
    reader.onload = () => { setDataUrl(String(reader.result)); setFile(f); };
    reader.onerror = () => setErr('Could not read the file — please try again.');
    reader.readAsDataURL(f);
  };

  const submit = async () => {
    setErr(''); setOk(false); setResult(null);
    if (!file || !dataUrl) { setErr('Please select a document to extract.'); return; }
    setLoading(true);
    try {
      const r = await kycAPI.verifyOCR(docType, file.name, dataUrl);
      setResult(r); setOk(true);
      onRecord({ type: 'OCR Document', docNumber: maskNumber(r.documentNumber || file.name), customerName: r.name || '—', status: 'Verified' });
    } catch (e) {
      setErr(kycErrorMessage(e, 'OCR Extraction Failed.'));
      onRecord({ type: 'OCR Document', docNumber: '—', customerName: '—', status: 'Failed' });
    } finally { setLoading(false); }
  };

  const isImage = file && /\.(jpg|jpeg|png)$/i.test(file.name);

  return (
    <VerifyShell icon="📄" title="OCR Document Verification" onBack={onBack}>
      {ok && <Banner kind="success">Verification completed successfully.</Banner>}
      {err && <Banner kind="error">{err}</Banner>}
      <Sel label="Document Type" value={docType} onChange={e => setDocType(e.target.value)} options={[
        { value: 'AADHAAR_FRONT', label: 'Aadhaar Front' }, { value: 'AADHAAR_BACK', label: 'Aadhaar Back' },
        { value: 'PAN', label: 'PAN Card' }, { value: 'PASSPORT', label: 'Passport' }, { value: 'OTHER', label: 'Other Identity Document' },
      ]} />
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
      <Btn onClick={submit} disabled={loading || !file}>{loading ? <><Spinner /> Extracting...</> : 'Extract Details'}</Btn>
      {result && (
        <ResultCard title="Extracted Information" rows={[
          ['Document Type', result.documentType || docType], ['Document Number', maskNumber(result.documentNumber || '')],
          ['Name', result.name], ['Date of Birth', result.dateOfBirth], ['Address', result.address],
          ...Object.entries(result.fields || {}).map(([k, v]) => [k, v] as [string, React.ReactNode]),
        ]} />
      )}
    </VerifyShell>
  );
};

// ─── DigiLocker ────────────────────────────────────────────────────────────────
const DigiLockerView: React.FC<ViewProps> = ({ onRecord, onBack }) => {
  const [mode, setMode] = useState<'intro' | 'auth'>('intro');
  const [mobile, setMobile] = useState('');
  const [aadhaar, setAadhaar] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [ok, setOk] = useState(false);
  const [session, setSession] = useState<DigiLockerSession | null>(null);

  const submit = async () => {
    setErr(''); setOk(false); setSession(null);
    const m = mobile.trim(); const a = aadhaar.replace(/\s/g, '').trim();
    if (!m && !a) { setErr('Enter the customer’s mobile number or Aadhaar number to continue.'); return; }
    if (m && !KYC_VALIDATION.mobile(m)) { setErr('Invalid mobile number — must be 10 digits.'); return; }
    if (a && !KYC_VALIDATION.aadhaar(a)) { setErr('Invalid Aadhaar Number — must be exactly 12 digits.'); return; }
    setLoading(true);
    try {
      const s = await kycAPI.verifyDigiLocker({ mobile: m || undefined, aadhaar: a || undefined });
      setSession(s); setOk(true);
      onRecord({ type: 'DigiLocker', docNumber: maskNumber(a || m), customerName: '—', status: 'Verified' });
    } catch (e) {
      setErr(kycErrorMessage(e, 'DigiLocker Authentication Failed.'));
      onRecord({ type: 'DigiLocker', docNumber: maskNumber(a || m), customerName: '—', status: 'Failed' });
    } finally { setLoading(false); }
  };

  return (
    <VerifyShell icon="🔐" title="DigiLocker Verification" onBack={onBack}>
      {mode === 'intro' ? (
        <>
          <p style={{ margin: '0 0 18px', fontSize: 13, color: T.textMuted, lineHeight: 1.6 }}>
            Securely verify customer documents through DigiLocker. The customer authorises access to their
            government-issued documents, then verified copies are retrieved automatically.
          </p>
          <Btn onClick={() => setMode('auth')}>Connect DigiLocker</Btn>
        </>
      ) : (
        <>
          {ok && <Banner kind="success">Verification completed successfully.</Banner>}
          {err && <Banner kind="error">{err}</Banner>}
          <p style={{ margin: '0 0 16px', fontSize: 13, fontWeight: 700, color: T.textMain }}>Connect with DigiLocker</p>
          <Input label="Customer Mobile Number" value={mobile} onChange={e => setMobile(e.target.value.replace(/\D/g, ''))} placeholder="10-digit mobile" inputMode="numeric" />
          <div style={{ textAlign: 'center', color: T.textMuted, fontSize: 12, fontWeight: 700, margin: '-4px 0 12px' }}>or</div>
          <Input label="Customer Aadhaar Number" value={aadhaar} onChange={e => setAadhaar(e.target.value.replace(/[^\d\s]/g, ''))} placeholder="12-digit Aadhaar" inputMode="numeric" />
          <Btn onClick={submit} disabled={loading}>{loading ? <><Spinner /> Connecting...</> : 'Continue'}</Btn>

          <div style={{ marginTop: 22 }}>
            <h3 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Retrieved Documents</h3>
            {session?.documents && session.documents.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {session.documents.map((d, i) => (
                  <Card key={i} style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 13, fontWeight: 700, color: T.textMain, flex: '1 1 160px' }}>{d.type}</span>
                    <span style={{ fontSize: 11, fontWeight: 700, color: T.success, background: T.successBg, padding: '2px 10px', borderRadius: 20 }}>{d.status || 'Verified'}</span>
                    <Btn size="sm" variant="ghost">View</Btn>
                    <Btn size="sm" variant="secondary">Download</Btn>
                  </Card>
                ))}
              </div>
            ) : (
              <p style={{ fontSize: 12, color: T.textMuted, padding: '14px 16px', border: `1px dashed ${T.border}`, borderRadius: 10, margin: 0 }}>
                No documents retrieved yet. Verified documents appear here after the customer authorises DigiLocker access.
              </p>
            )}
          </div>
        </>
      )}
    </VerifyShell>
  );
};

// ─── History table ─────────────────────────────────────────────────────────────
const HistoryTable: React.FC<{ rows: KycHistoryRow[] }> = ({ rows }) => (
  <Card style={{ marginTop: 24 }}>
    <div style={{ padding: '14px 18px', borderBottom: `1px solid ${T.border}` }}>
      <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>Verification History</h2>
      <p style={{ margin: '2px 0 0', fontSize: 12, color: T.textMuted }}>Identity checks performed in this session.</p>
    </div>
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: T.canvas }}>
            {['Date & Time', 'Verification Type', 'Document Number', 'Customer Name', 'Verified By', 'Status', 'Action'].map(h => (
              <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `2px solid ${T.border}` }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={7} style={{ padding: 28, textAlign: 'center', color: T.textMuted }}>No verifications yet.</td></tr>}
          {rows.map((r, i) => (
            <tr key={r.id} style={{ background: i % 2 === 0 ? T.surface : T.canvas }}>
              <td style={{ padding: '11px 14px', color: T.textMuted, whiteSpace: 'nowrap' }}>{r.dateTime}</td>
              <td style={{ padding: '11px 14px', fontWeight: 700, color: T.textMain }}>{r.type}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted, fontFamily: 'monospace' }}>{r.docNumber}</td>
              <td style={{ padding: '11px 14px', color: T.textMain }}>{r.customerName}</td>
              <td style={{ padding: '11px 14px', color: T.textMuted }}>{r.verifiedBy}</td>
              <td style={{ padding: '11px 14px' }}>
                <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 10px', borderRadius: 20, color: r.status === 'Verified' ? T.success : T.danger, background: r.status === 'Verified' ? T.successBg : T.dangerBg }}>{r.status}</span>
              </td>
              <td style={{ padding: '11px 14px' }}><Btn size="sm" variant="ghost">View Details</Btn></td>
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
  const [history, setHistory] = useState<KycHistoryRow[]>([]);
  const verifiedBy = user.name || user.username;

  const record = (r: Omit<KycHistoryRow, 'id' | 'dateTime' | 'verifiedBy'>) =>
    setHistory(prev => [{ id: Date.now(), dateTime: nowIST(), verifiedBy, ...r }, ...prev]);

  const back = () => setView('home');
  const viewProps: ViewProps = { onRecord: record, onBack: back };

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
                <div style={{ width: 48, height: 48, borderRadius: 14, background: `${T.blue}15`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 24 }}>{c.icon}</div>
                <div style={{ fontSize: 15, fontWeight: 800, color: T.textMain }}>{c.title}</div>
                <div style={{ fontSize: 12, color: T.textMuted, lineHeight: 1.5, flex: 1 }}>{c.desc}</div>
                <Btn size="sm" full onClick={() => setView(c.key)}>{c.key === 'digilocker' ? 'Connect DigiLocker' : c.key === 'ocr' ? 'Open' : `Verify ${TYPE_LABEL[c.key]}`}</Btn>
              </Card>
            ))}
          </div>
          <HistoryTable rows={history} />
        </>
      )}

      {view === 'aadhaar' && <AadhaarView {...viewProps} />}
      {view === 'pan' && <PanView {...viewProps} />}
      {view === 'passport' && <PassportView {...viewProps} />}
      {view === 'ocr' && <OcrView {...viewProps} />}
      {view === 'digilocker' && <DigiLockerView {...viewProps} />}
    </div>
  );
};

export default KYCPage;
