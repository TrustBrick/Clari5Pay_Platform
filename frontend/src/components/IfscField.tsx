import React from 'react';
import { T } from '../utils/theme';
import { Input } from './UI';
import type { IfscAutoFill } from '../utils/useIfscAutoFill';

// IFSC input wired to useIfscAutoFill: the standard Input plus an inline spinner while the
// lookup runs and a single status line beneath it. Uses the existing Input so the field keeps
// the platform's theme, spacing and focus behaviour exactly.
export const IfscField: React.FC<{
  value: string;
  ifsc: IfscAutoFill;
  label?: string;
  required?: boolean;
  style?: React.CSSProperties;
}> = ({ value, ifsc, label = 'IFSC Code', required, style }) => (
  <div style={{ marginBottom: 16, ...style }}>
    <div style={{ position: 'relative' }}>
      <Input
        label={label} value={value} required={required}
        onChange={e => ifsc.onChange(e.target.value)}
        onBlur={ifsc.onBlur}
        placeholder="e.g. HDFC0000001"
        style={{ marginBottom: 0 }}
      />
      {ifsc.loading && (
        <span
          aria-label="Looking up IFSC"
          style={{
            position: 'absolute', right: 12, top: label ? 'calc(50% + 11px)' : '50%',
            transform: 'translateY(-50%)', width: 15, height: 15, borderRadius: '50%',
            border: `2px solid ${T.border}`, borderTopColor: T.blue,
            animation: 'c5spin 0.8s linear infinite', pointerEvents: 'none',
          }}
        />
      )}
    </div>
    <p style={{ fontSize: 11, marginTop: 4, marginBottom: 0, color: ifsc.error ? T.danger : T.textMuted }}>
      {ifsc.loading ? 'Looking up bank details…' : ifsc.error || 'Auto-fills bank & branch'}
    </p>
    <style>{'@keyframes c5spin{to{transform:rotate(360deg)}}'}</style>
  </div>
);
