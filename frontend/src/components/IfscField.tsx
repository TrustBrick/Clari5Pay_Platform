import React from 'react';
import { T } from '../utils/theme';
import { Input } from './UI';
import type { IfscAutoFill } from '../utils/useIfscAutoFill';

// THE standard IFSC input. Every form in the platform that captures an IFSC renders this —
// Admin, Merchant, Agent, Withdrawal, Settlement, Account Management, Complaints — so the
// validation, the message, the loading state and the auto-fill behave identically everywhere.
// Pair it with useIfscAutoFill, and mark the Bank Name / Branch inputs readOnly={ifsc.locked}.
//
// It is the platform Input underneath, so the field keeps the standard theme, spacing and focus
// behaviour — and, on a bad code, the standard red border and red message the Input already
// renders for every other validated field.
export const IfscField: React.FC<{
  value: string;
  ifsc: IfscAutoFill;
  label?: string;
  required?: boolean;
  style?: React.CSSProperties;
  /** Replaces the default idle helper text. The loading and error states always win. */
  hint?: string;
}> = ({ value, ifsc, label = 'IFSC Code', required, style, hint = 'Auto-fills bank & branch' }) => (
  <div style={{ position: 'relative', ...style }}>
    <Input
      label={label} value={value} required={required}
      onChange={e => ifsc.onChange(e.target.value)}
      onBlur={ifsc.onBlur}
      placeholder="e.g. HDFC0000001"
      error={ifsc.error || undefined}
      hint={ifsc.loading ? 'Looking up bank details…' : hint}
    />
    {ifsc.loading && (
      <span
        aria-label="Looking up IFSC"
        // Offset from the top, not centred: the helper/error line below the input makes the
        // wrapper's height vary, so a percentage would drift. 20px clears the uppercase label,
        // and the input itself is ~40px tall.
        style={{
          position: 'absolute', right: 12, top: label ? 40 : 20,
          transform: 'translateY(-50%)', width: 15, height: 15, borderRadius: '50%',
          border: `2px solid ${T.border}`, borderTopColor: T.blue,
          animation: 'c5spin 0.8s linear infinite', pointerEvents: 'none',
        }}
      />
    )}
    <style>{'@keyframes c5spin{to{transform:rotate(360deg)}}'}</style>
  </div>
);
