// Bank-details helpers used wherever bank details are entered.
// IFSC -> bank + branch via the free Razorpay IFSC API (https://ifsc.razorpay.com/<IFSC>),
// plus a bundled list of Indian banks for name autocomplete and a deterministic logo badge.

export interface IfscInfo { bank: string; branch: string; address?: string; city?: string; state?: string; }

const IFSC_RE = /^[A-Z]{4}0[A-Z0-9]{6}$/;
export const isValidIfsc = (ifsc: string) => IFSC_RE.test((ifsc || '').trim().toUpperCase());

/**
 * Outcome of an IFSC lookup. `invalid` and `unavailable` are deliberately distinct: a code the
 * registry does not know is the user's mistake and warrants a validation message, whereas an
 * unreachable service is ours and must never be allowed to discard details already on the form.
 */
export type IfscLookup =
  | { status: 'ok'; info: IfscInfo }
  | { status: 'invalid' }        // bad format, or the registry returned 404 / no bank
  | { status: 'unavailable' };   // offline, blocked, or a server error — keep existing values

/** Resolve an IFSC to its bank + branch. Uses plain fetch (NOT the app axios client, so our
 *  auth token is never sent to the third-party API). */
export const lookupIfscResult = async (ifsc: string): Promise<IfscLookup> => {
  const code = (ifsc || '').trim().toUpperCase();
  if (!isValidIfsc(code)) return { status: 'invalid' };
  try {
    const res = await fetch(`https://ifsc.razorpay.com/${code}`);
    if (res.status === 404) return { status: 'invalid' };     // registry knows the format, not the code
    if (!res.ok) return { status: 'unavailable' };            // 5xx / rate-limited — not the user's fault
    const d = await res.json();
    if (!d || !d.BANK) return { status: 'invalid' };
    return { status: 'ok', info: { bank: d.BANK, branch: d.BRANCH, address: d.ADDRESS, city: d.CITY, state: d.STATE } };
  } catch {
    return { status: 'unavailable' };   // offline / blocked — caller falls back to manual entry
  }
};

/** Back-compatible wrapper: null for anything that is not a successful lookup. Existing callers
 *  (Admin → Account Management, Merchant onboarding) rely on this exact shape. */
export const lookupIfsc = async (ifsc: string): Promise<IfscInfo | null> => {
  const r = await lookupIfscResult(ifsc);
  return r.status === 'ok' ? r.info : null;
};

// Bundled list for the bank-name autocomplete (datalist).
export const BANK_NAMES = [
  'State Bank of India', 'HDFC Bank', 'ICICI Bank', 'Axis Bank', 'Kotak Mahindra Bank',
  'Punjab National Bank', 'Bank of Baroda', 'Canara Bank', 'Union Bank of India', 'Bank of India',
  'Indian Bank', 'Central Bank of India', 'Indian Overseas Bank', 'UCO Bank', 'Bank of Maharashtra',
  'Punjab & Sind Bank', 'IDBI Bank', 'IDFC FIRST Bank', 'Yes Bank', 'IndusInd Bank',
  'Federal Bank', 'South Indian Bank', 'Karur Vysya Bank', 'City Union Bank', 'RBL Bank',
  'Bandhan Bank', 'DCB Bank', 'Dhanlaxmi Bank', 'Jammu & Kashmir Bank', 'Karnataka Bank',
  'Tamilnad Mercantile Bank', 'CSB Bank', 'Nainital Bank', 'AU Small Finance Bank',
  'Equitas Small Finance Bank', 'Ujjivan Small Finance Bank', 'Jana Small Finance Bank',
  'Suryoday Small Finance Bank', 'Utkarsh Small Finance Bank', 'ESAF Small Finance Bank',
  'Fincare Small Finance Bank', 'Capital Small Finance Bank', 'North East Small Finance Bank',
  'Paytm Payments Bank', 'Airtel Payments Bank', 'India Post Payments Bank', 'Fino Payments Bank',
  'Jio Payments Bank', 'Citibank', 'Standard Chartered Bank', 'HSBC Bank', 'Deutsche Bank',
  'DBS Bank India', 'Barclays Bank', 'Bank of America', 'JPMorgan Chase Bank',
  'Saraswat Co-operative Bank', 'Cosmos Co-operative Bank', 'SVC Co-operative Bank',
  'Abhyudaya Co-operative Bank', 'TJSB Sahakari Bank', 'Bharat Co-operative Bank',
];

const BADGE_COLORS = ['#0052cc', '#26d00c', '#00a3ff', '#6a5acd', '#e0245e', '#f59e0b', '#0a8a6a', '#8b5cf6'];
/** Deterministic colour + initials badge for a bank name. */
export const bankBadge = (name: string): { initials: string; color: string } => {
  const n = (name || '').trim();
  if (!n) return { initials: '🏦', color: '#94a3b8' };
  const initials = n.split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
  let h = 0;
  for (let i = 0; i < n.length; i++) h = (h * 31 + n.charCodeAt(i)) >>> 0;
  return { initials, color: BADGE_COLORS[h % BADGE_COLORS.length] };
};
