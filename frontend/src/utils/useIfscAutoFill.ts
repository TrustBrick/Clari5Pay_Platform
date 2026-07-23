import { useCallback, useRef, useState } from 'react';
import { isValidIfsc, lookupIfscResult } from './ifsc';

// Shared IFSC auto-fill behaviour for any form that captures bank details.
// Wraps the existing utils/ifsc lookup (Razorpay IFSC API) — it does NOT introduce a second
// implementation. Everything the forms need is here so each form stays a thin consumer:
//   • fires when the code reaches a valid IFSC, and again on blur (catches short/incorrect codes)
//   • one request at a time; a code already resolved is never re-fetched
//   • distinguishes "not a real IFSC" from "lookup service unreachable"
//   • never clears Bank Name / Branch already on the form — only a SUCCESSFUL lookup writes them
//   • reports `locked` so the form can make auto-filled values read-only

/** Exact wording the spec requires for a code the registry does not recognise. */
const MSG_INVALID = 'Invalid IFSC Code. Please enter a valid IFSC Code.';
const MSG_UNAVAILABLE = 'IFSC lookup is unavailable right now. Please enter the bank and branch manually.';

/** IFSC is 11 chars: 4 letters, a 0, then 6 alphanumerics. Strip anything else as it is typed. */
const sanitizeIfsc = (raw: string) => (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 11);

export interface IfscAutoFill {
  /** True while a lookup is in flight — drive the inline spinner from this. */
  loading: boolean;
  /** Validation / service message to show under the field, else null. */
  error: string | null;
  /** True when Bank Name + Branch were filled by a successful lookup, so they should be
   *  read-only. Stays false for values loaded from an existing record, which remain editable. */
  locked: boolean;
  /** Feed the IFSC input's onChange. Sanitises, then looks up once the code is complete. */
  onChange: (raw: string) => void;
  /** Feed the IFSC input's onBlur, so an incomplete or wrong code still reports itself. */
  onBlur: () => void;
  /** Clear hook state when the form is reset or reopened. */
  reset: () => void;
}

/**
 * @param value    current IFSC value held by the form
 * @param setValue writes the sanitised IFSC back to the form
 * @param onResolved called ONLY on a successful lookup, with the bank and branch to apply
 */
export const useIfscAutoFill = (
  value: string,
  setValue: (v: string) => void,
  onResolved: (bank: string, branch: string) => void,
): IfscAutoFill => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState(false);
  // The code currently being fetched — the in-flight guard.
  const inFlight = useRef<string | null>(null);
  // The last code we successfully resolved, so blur cannot refetch the same one.
  const resolved = useRef<string | null>(null);
  // onResolved is typically an inline arrow; keep it in a ref so the callbacks stay stable.
  const resolvedCb = useRef(onResolved);
  resolvedCb.current = onResolved;

  const run = useCallback(async (code: string) => {
    if (!code) return;
    if (inFlight.current === code || resolved.current === code) return;   // no duplicate requests
    inFlight.current = code;
    setLoading(true);
    try {
      const res = await lookupIfscResult(code);
      // A slow response for a code the user has since edited must not overwrite the new one.
      if (inFlight.current !== code) return;
      if (res.status === 'ok') {
        resolved.current = code;
        setError(null);
        setLocked(true);
        resolvedCb.current(res.info.bank, res.info.branch || '');
      } else {
        // Neither outcome writes or clears Bank Name / Branch — whatever is on the form stays,
        // and the fields stay editable so the user can always proceed manually.
        setLocked(false);
        setError(res.status === 'invalid' ? MSG_INVALID : MSG_UNAVAILABLE);
      }
    } finally {
      if (inFlight.current === code) { inFlight.current = null; setLoading(false); }
    }
  }, []);

  const onChange = useCallback((raw: string) => {
    const code = sanitizeIfsc(raw);
    setValue(code);
    if (code !== resolved.current) {
      // Editing a resolved code releases the auto-filled fields and drops the stale message.
      resolved.current = null;
      setLocked(false);
      setError(null);
    }
    if (isValidIfsc(code)) void run(code);
  }, [run, setValue]);

  const onBlur = useCallback(() => {
    const code = sanitizeIfsc(value);
    if (!code || code === resolved.current) return;
    if (isValidIfsc(code)) void run(code);
    else setError(MSG_INVALID);        // too short / wrong shape — say so on leaving the field
  }, [value, run]);

  const reset = useCallback(() => {
    inFlight.current = null; resolved.current = null;
    setLoading(false); setError(null); setLocked(false);
  }, []);

  return { loading, error, locked, onChange, onBlur, reset };
};
