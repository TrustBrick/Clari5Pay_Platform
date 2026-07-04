import { useEffect, useRef } from 'react';

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
