// KYC verification service layer (Merchant Portal → KYC Update).
//
// Reusable client for the /api/kyc/* endpoints. Response models below mirror the
// fields each provider (Melento.ai / DigiLocker) will return. Until the backend
// credentials are connected, every call resolves to a graceful error (HTTP 503),
// which the KYC page surfaces as a "service not available yet" state. When the real
// integration lands, only the backend service seams change — these methods and
// their signatures stay exactly the same.
import api from './api';

// ── Response models ────────────────────────────────────────────────────────────
export interface AadhaarResult {
  aadhaarNumber?: string;
  fullName?: string;
  dateOfBirth?: string;
  gender?: string;
  address?: string;
  state?: string;
  district?: string;
  pincode?: string;
  photo?: string | null;      // base64 / URL
  status?: string;
  lastSynced?: string;        // populated when verified via DigiLocker
}

export interface PanResult {
  panNumber?: string;
  fullName?: string;
  fatherName?: string;
  dateOfBirth?: string;
  category?: string;
  status?: string;
}

export interface PassportResult {
  passportNumber?: string;
  fullName?: string;
  nationality?: string;
  gender?: string;
  dateOfBirth?: string;
  issueDate?: string;
  expiryDate?: string;
  status?: string;
}

export interface OcrResult {
  documentType?: string;
  documentNumber?: string;
  name?: string;
  dateOfBirth?: string;
  address?: string;
  fields?: Record<string, string>;   // any other detected fields
  status?: string;
}

// ── Service methods (placeholders — backend integration added later) ────────────
export const kycAPI = {
  verifyAadhaar: async (aadhaarNumber: string): Promise<AadhaarResult> =>
    (await api.post<AadhaarResult>('/api/kyc/aadhaar/verify', { aadhaarNumber })).data,

  verifyPAN: async (panNumber: string): Promise<PanResult> =>
    (await api.post<PanResult>('/api/kyc/pan/verify', { panNumber })).data,

  verifyPassport: async (passportNumber: string, dateOfBirth?: string): Promise<PassportResult> =>
    (await api.post<PassportResult>('/api/kyc/passport/verify', { passportNumber, dateOfBirth })).data,

  verifyOCR: async (documentType: string, fileName: string, fileData: string): Promise<OcrResult> =>
    (await api.post<OcrResult>('/api/kyc/ocr/extract', { documentType, fileName, fileData })).data,

  // Aadhaar via DigiLocker — customer authenticates with DigiLocker; the verified Aadhaar
  // document is returned in the same shape as verifyAadhaar (unified result card).
  verifyViaDigiLocker: async (): Promise<AadhaarResult> =>
    (await api.post<AadhaarResult>('/api/kyc/digilocker/verify')).data,
};

// ── Client-side validation helpers (mirror the server-side rules) ───────────────
export const KYC_VALIDATION = {
  aadhaar: (v: string) => /^\d{12}$/.test(v.replace(/\s/g, '')),
  pan: (v: string) => /^[A-Z]{5}[0-9]{4}[A-Z]$/.test(v.toUpperCase().trim()),
  passport: (v: string) => /^[A-Z][0-9]{7}$/.test(v.toUpperCase().trim()),
  mobile: (v: string) => /^\d{10}$/.test(v.trim()),
};

export const OCR_ACCEPT = '.jpg,.jpeg,.png,.pdf';
export const OCR_MAX_BYTES = 10 * 1024 * 1024; // 10 MB

/** Turn an axios error into a human-readable message for the KYC UI. */
export const kycErrorMessage = (err: unknown, fallback: string): string => {
  const e = err as { response?: { data?: { detail?: string }; status?: number }; code?: string };
  if (e?.response?.data?.detail) return e.response.data.detail;
  if (e?.code === 'ECONNABORTED') return 'API Timeout — please try again.';
  if (e?.response?.status === 503) return 'Service Unavailable — please try again later.';
  return fallback;
};
