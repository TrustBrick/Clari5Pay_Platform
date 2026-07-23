// KYC verification service layer (Merchant Portal → KYC Update).
//
// Reusable client for the /api/kyc/* endpoints. Response models below mirror the
// fields each provider (Melento.ai / DigiLocker) will return. Until the backend
// credentials are connected, every call resolves to a graceful error (HTTP 503),
// which the KYC page surfaces as a "service not available yet" state. When the real
// integration lands, only the backend service seams change — these methods and
// their signatures stay exactly the same.
import api from './api';
import type { Paged } from './api';

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

// ── Live Melento.ai integration (membership-based Aadhaar / PAN / Passport / OCR) ─
export interface KycHistoryItem {
  id: number;
  membershipId?: string | null;
  memberName?: string | null;
  verificationType: 'AADHAAR' | 'PAN' | 'PASSPORT' | 'OCR' | string;
  verificationMethod?: string | null;   // "ID Number" | "Image Upload" | "DigiLocker"
  documentType?: string | null;     // OCR doc_type (passport / pan_card / …)
  referenceId?: string | null;
  transactionId?: string | null;
  status: 'PENDING' | 'SUCCESS' | 'FAILED' | string;
  createdBy?: string | null;
  createdAt?: string | null;
}

export interface KycHistoryDetail extends KycHistoryItem {
  generatedLink?: string | null;
  apiStatus?: string | null;
  errorMessage?: string | null;
  request?: Record<string, unknown> | null;
  response?: Record<string, unknown> | null;
  /** Aadhaar photo parsed server-side out of the response's XML section (data URL), if present. */
  aadhaarPhoto?: string | null;
  updatedAt?: string | null;
}

/** Membership lookup. Never 404s: `exists: false` means the operator names the ID by hand. */
export interface KycMemberLookup {
  membershipId: string;
  memberName: string | null;
  exists: boolean;
  kyc: KycHistoryItem[];      // KYC already on record for this Membership ID
}

export interface AadhaarLinkResult {
  id: number;
  referenceId: string;
  transactionId?: string | null;
  link: string;
  status: string;
  message?: string | null;
}

export interface AadhaarStatusResult {
  pending: boolean;
  status: 'PENDING' | 'SUCCESS' | 'FAILED' | string;
  error?: string;
  message?: string | null;
  details?: AadhaarDetails | null;
}

// Aadhaar getAadhaarDetails response shape.
export interface AadhaarDetails {
  status?: string;
  name?: string;
  uid?: string;
  dob?: string;
  gender?: string;
  care_of?: string;
  address?: string;
  split_address?: Record<string, string> | null;
  xml_file?: string | null;
  error?: string;
  [k: string]: unknown;
}

export interface PanVerifyResult {
  id: number;
  status: string;
  validPan: boolean;
  result?: Record<string, unknown>;
  raw?: Record<string, unknown>;
}

export interface PassportVerifyResult {
  id: number;
  status: string;
  validPassport: boolean;
  result?: Record<string, unknown>;
  raw?: Record<string, unknown>;
}

export interface OcrVerifyResult {
  id: number;
  status: string;
  verified: boolean;
  raw?: Record<string, unknown>;
}

// Document types supported by the General-Document (OCR) API (dropdown value → doc_type).
export const OCR_DOC_TYPES: Array<{ value: string; label: string }> = [
  { value: 'passport', label: 'Passport' },
  { value: 'pan_card', label: 'PAN Card' },
  { value: 'aadhaar_card', label: 'Aadhaar Card' },
  { value: 'driving_licence', label: 'Driving Licence' },
  { value: 'voter_card', label: 'Voter ID' },
];

// ── Service methods ─────────────────────────────────────────────────────────────
export const kycAPI = {
  // Membership lookup. Never 404s — an unknown ID returns exists:false and the operator supplies
  // the Member Name, which the verification then persists against that ID.
  lookupMember: async (membershipId: string): Promise<KycMemberLookup> =>
    (await api.get(`/api/kyc/member/${encodeURIComponent(membershipId)}`)).data,

  // `memberName` is only used when the Membership ID is not yet on record; for a known ID the
  // server keeps its authoritative name and ignores what was sent.
  generateAadhaarLink: async (membershipId: string, memberName?: string): Promise<AadhaarLinkResult> =>
    (await api.post<AadhaarLinkResult>('/api/kyc/aadhaar/generate-link', { membershipId, memberName })).data,

  getAadhaarStatus: async (historyId: number): Promise<AadhaarStatusResult> =>
    (await api.post<AadhaarStatusResult>('/api/kyc/aadhaar/status', { historyId })).data,

  // PAN — verify by ID Number (pan) OR by uploaded card image (base64 data URL). The backend
  // derives source_type ("id" / "base64") from which field is supplied.
  verifyPanMembership: async (membershipId: string, opts: { pan?: string; image?: string; memberName?: string }): Promise<PanVerifyResult> =>
    (await api.post<PanVerifyResult>('/api/kyc/pan/verify-membership', { membershipId, ...opts })).data,

  // Passport — verify by File Number (+ optional dob) OR by front+back card images (base64).
  verifyPassportMembership: async (
    membershipId: string,
    opts: { passportNumber?: string; dateOfBirth?: string; frontImage?: string; backImage?: string; memberName?: string },
  ): Promise<PassportVerifyResult> =>
    (await api.post<PassportVerifyResult>('/api/kyc/passport/verify-membership', { membershipId, ...opts })).data,

  // Aadhaar — verify from an uploaded card image (General-Document OCR, doc_type=aadhaar_card).
  verifyAadhaarImage: async (membershipId: string, image: string, memberName?: string): Promise<OcrVerifyResult> =>
    (await api.post<OcrVerifyResult>('/api/kyc/aadhaar/verify-image', { membershipId, image, memberName })).data,

  verifyOcrMembership: async (
    membershipId: string, documentType: string, fileName: string, fileData: string, verification: boolean,
    memberName?: string,
  ): Promise<OcrVerifyResult> =>
    (await api.post<OcrVerifyResult>('/api/kyc/ocr/verify-membership', { membershipId, documentType, fileName, fileData, verification, memberName })).data,

  // Server-side paged: one page of rows plus the full-set count. The complete history is never
  // sent to the browser — sorting (newest first) and counting both happen in the database.
  listHistory: async (page = 1, pageSize = 10): Promise<Paged<KycHistoryItem>> =>
    (await api.get<Paged<KycHistoryItem>>('/api/kyc/history', {
      params: { page, page_size: pageSize },
    })).data,

  getHistoryDetail: async (id: number): Promise<KycHistoryDetail> =>
    (await api.get<KycHistoryDetail>(`/api/kyc/history/${id}`)).data,

  // ── Legacy placeholder seams (still used by the Passport / OCR cards) ──
  verifyAadhaar: async (aadhaarNumber: string): Promise<AadhaarResult> =>
    (await api.post<AadhaarResult>('/api/kyc/aadhaar/verify', { aadhaarNumber })).data,

  verifyPAN: async (panNumber: string): Promise<PanResult> =>
    (await api.post<PanResult>('/api/kyc/pan/verify', { panNumber })).data,

  // Aadhaar via DigiLocker — customer authenticates with DigiLocker; the verified Aadhaar
  // document is returned in the same shape as verifyAadhaar (unified result card).
  verifyViaDigiLocker: async (): Promise<AadhaarResult> =>
    (await api.post<AadhaarResult>('/api/kyc/digilocker/verify')).data,
};

// ── Client-side validation helpers (mirror the server-side rules) ───────────────
export const KYC_VALIDATION = {
  aadhaar: (v: string) => /^\d{12}$/.test(v.replace(/\s/g, '')),
  pan: (v: string) => /^[A-Z]{5}[0-9]{4}[A-Z]$/.test(v.toUpperCase().trim()),
  // Passport File Number (from the back page) — not the passport number. The API docs define no
  // strict format, so we only require a non-empty, alphanumeric value and let Melento validate it.
  passport: (v: string) => /^[A-Z0-9]+$/.test(v.toUpperCase().trim()),
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
