import { useEffect, useRef, useState } from 'react';
import { transactionAPI, type ActivitySignal } from '../services/api';

/**
 * Debounce a rapidly-changing value (e.g. a search box) so dependent effects — a
 * server-side search request — fire only after the user pauses typing, instead of on
 * every keystroke. Default 400ms sits in the spec's 300–500ms window.
 */
export const useDebouncedValue = <T,>(value: T, ms = 400): T => {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
};

/**
 * Refresh cadence for the live-presence views (Active Users page, the Super Admin dashboard
 * widget, and per-merchant online counts). These behave like a monitoring dashboard, so they
 * poll far more often than the rest of the app: a login/logout/heartbeat from any browser shows
 * up within ~3s. WebSockets aren't part of this stack (remote RDS, polling architecture — see
 * presence.py), so this is the sanctioned short-interval poll. Only the lightweight presence
 * endpoint uses it; heavier composite fetches keep the conservative default interval.
 */
export const PRESENCE_POLL_MS = 3000;

/**
 * Keep a screen's data fresh so changes made in other portals appear without a
 * manual reload. Refetches:
 *   - on a fixed interval (default 20s),
 *   - whenever the window regains focus,
 *   - whenever the tab becomes visible again (switching back from another tab).
 * The latest `fn` is always used without resetting the interval.
 *
 * The interval is deliberately conservative: every tick is a burst of API calls,
 * and against a remote DB each call costs a full network round-trip. Because we
 * also refetch on focus/visibility, screens are fresh the moment you look at them,
 * so a long interval costs nothing in practice while cutting background load.
 */
export const usePoll = (fn: () => void, ms = 20000) => {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const run = () => ref.current();
    const id = setInterval(run, ms);
    const onVisible = () => { if (document.visibilityState === 'visible') run(); };
    window.addEventListener('focus', run);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(id);
      window.removeEventListener('focus', run);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [ms]);
};

/**
 * Live operational awareness WITHOUT re-fetching transaction tables on a timer.
 *
 * Approval queues have to surface new requests on their own, but polling a full table every
 * 20s is precisely the load the pagination work removed. So this polls a tiny server-side
 * signal instead — a row count, the newest updated_at and the pending count, a few hundred
 * bytes and no transaction rows — and invokes `onChange` ONLY when that signal actually moves.
 *
 * Net effect: an idle queue costs one scalar query per interval and zero table reads; a queue
 * with real activity refreshes just the component that asked, once, when something changed.
 *
 * `onChange` is called with the new signal. It is NOT called on the first read (that would
 * duplicate the component's own initial load).
 */
export const useActivitySignal = (
  onChange: (sig: ActivitySignal) => void,
  opts?: { enabled?: boolean; ms?: number },
) => {
  const enabled = opts?.enabled !== false;
  const ms = opts?.ms ?? 25000;   // spec asks for 20-30s
  const cb = useRef(onChange);
  cb.current = onChange;
  const lastVersion = useRef<string | null>(null);
  const [pending, setPending] = useState<number | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    const tick = async () => {
      try {
        const sig = await transactionAPI.activitySignal();
        if (cancelled) return;
        setPending(sig.pending);
        const first = lastVersion.current === null;
        if (lastVersion.current !== sig.version) {
          lastVersion.current = sig.version;
          if (!first) cb.current(sig);      // skip the very first read
        }
      } catch { /* a failed probe must never break the page */ }
    };

    tick();
    const id = setInterval(tick, ms);
    const onVisible = () => { if (document.visibilityState === 'visible') tick(); };
    window.addEventListener('focus', tick);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      clearInterval(id);
      window.removeEventListener('focus', tick);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, ms]);

  return pending;
};
