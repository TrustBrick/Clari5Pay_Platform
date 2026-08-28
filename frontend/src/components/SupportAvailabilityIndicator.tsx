import React, { useCallback, useEffect, useState } from 'react';
import { T } from '../utils/theme';
import { usePoll } from '../utils/usePoll';
import { supportAPI } from '../services/api';
import type { SupportTeamAvailability } from '../types';

/**
 * Compact "Support Available / Support Unavailable" pill for the Merchant Portal header.
 *
 * The state is the SUPPORT TEAM's real availability, fetched from /api/support/availability —
 * the same rule the backend uses to decide whether a conversation can be auto-assigned (a member
 * who is online, not Busy/On-Break and below their conversation limit). Being signed in, or the
 * Support Center page existing, never turns this green.
 *
 * Rendered once inside the shared Header, so it appears on every Merchant Portal page without
 * touching any individual template. Clicking it opens the existing Customer Support page.
 */
const SupportAvailabilityIndicator: React.FC<{ onOpen?: () => void }> = ({ onOpen }) => {
  const [state, setState] = useState<SupportTeamAvailability | null>(null);

  const load = useCallback(() => {
    supportAPI.availability().then(setState).catch(() => { /* keep the last known state */ });
  }, []);

  useEffect(() => { load(); }, [load]);
  // Reuses the app's shared poll helper (pauses on a hidden tab) — same cadence as the header's
  // notification poll, so the pill flips within one beat of a member going on/off shift.
  usePoll(load, 20000);

  // Nothing is claimed until the first response lands — the pill would otherwise flash a state
  // it has not verified.
  if (!state) return null;

  const on = state.available;
  const label = on ? 'Support Available' : 'Support Unavailable';
  const dot = on ? T.success : T.textLight;

  return (
    <>
      {/* Subtle two-part breathe: the dot's own opacity plus a soft halo. Slow (2.4s) and
          low-amplitude on purpose — a status cue, not an alarm. Only the available state animates. */}
      <style>{`
        @keyframes c5supportdot{0%,100%{opacity:1;}50%{opacity:0.45;}}
        @keyframes c5supporthalo{0%{transform:scale(1);opacity:0.5;}70%,100%{transform:scale(2.4);opacity:0;}}
        @media (prefers-reduced-motion: reduce){
          .c5-support-dot,.c5-support-halo{animation:none!important;}
        }
        /* Narrow screens keep only the dot, so the pill never crowds the header controls. */
        @media(max-width:600px){
          .c5-support-label{display:none;}
          .c5-support-pill{padding:6px!important;}
        }
      `}</style>
      <button
        type="button"
        onClick={onOpen}
        className="c5-support-pill"
        title={on
          ? `Support is online — ${state.availableAgents} member${state.availableAgents === 1 ? '' : 's'} available`
          : 'No support member is available right now'}
        aria-label={label}
        style={{
          display: 'flex', alignItems: 'center', gap: 7, padding: '5px 11px 5px 10px', borderRadius: 999,
          // Glass pill matching the header popups: translucent surface + blur, theme-aware tokens.
          background: `color-mix(in srgb, ${on ? T.success : T.textMuted} 10%, transparent)`,
          border: `1px solid color-mix(in srgb, ${on ? T.success : T.textMuted} 28%, transparent)`,
          backdropFilter: 'blur(10px) saturate(1.3)', WebkitBackdropFilter: 'blur(10px) saturate(1.3)',
          color: on ? T.success : T.textMuted,
          fontSize: 11, fontWeight: 700, fontFamily: 'inherit', lineHeight: 1,
          cursor: onOpen ? 'pointer' : 'default', whiteSpace: 'nowrap', transition: 'background 0.2s',
        }}
      >
        <span style={{ position: 'relative', display: 'inline-flex', width: 8, height: 8, flexShrink: 0 }}>
          {on && (
            <span
              className="c5-support-halo"
              style={{ position: 'absolute', inset: 0, borderRadius: '50%', background: T.success,
                       animation: 'c5supporthalo 2.4s ease-out infinite' }}
            />
          )}
          <span
            className={on ? 'c5-support-dot' : undefined}
            style={{ position: 'relative', width: 8, height: 8, borderRadius: '50%', background: dot,
                     boxShadow: on ? `0 0 6px ${T.success}` : 'none',
                     animation: on ? 'c5supportdot 2.4s ease-in-out infinite' : undefined }}
          />
        </span>
        <span className="c5-support-label">{label}</span>
      </button>
    </>
  );
};

export default SupportAvailabilityIndicator;
