import React, { useCallback, useEffect, useRef, useState } from 'react';
import { T } from '../utils/theme';
import { Btn } from './UI';
import { useAuth } from '../context/AuthContext';

// Auto-logout after 10 minutes of inactivity; warn at 9 minutes (1 minute left).
const IDLE_LIMIT_MS = 10 * 60 * 1000;
const WARN_AT_MS = 9 * 60 * 1000;
const CHECK_EVERY_MS = 5000;          // how often idle time is evaluated
const ACTIVITY_THROTTLE_MS = 1000;    // coalesce high-frequency events (mousemove/scroll)

// Set on the login page after an inactivity logout so it can show the expiry notice.
export const SESSION_EXPIRED_KEY = 'clari5pay_session_expired';

/**
 * Inactivity session manager. Mounted only while a user is logged in.
 *
 * The timer resets on any genuine user activity — mouse move/click, keyboard,
 * scroll, touch, wheel — which also covers SPA navigation and user-initiated API
 * calls (each is triggered by one of those interactions). Background polling is
 * intentionally NOT treated as activity, so an idle tab still times out.
 */
const SessionManager: React.FC = () => {
  const { logout } = useAuth();
  const [warning, setWarning] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(60);
  const lastActivity = useRef(Date.now());
  const warningRef = useRef(false);
  const throttleRef = useRef(0);

  const resetTimer = useCallback(() => {
    lastActivity.current = Date.now();
    if (warningRef.current) { warningRef.current = false; setWarning(false); }
  }, []);

  const expire = useCallback(() => {
    warningRef.current = false;
    setWarning(false);
    logout();                                       // clears token/storage/cookies + redirect
    try { sessionStorage.setItem(SESSION_EXPIRED_KEY, '1'); } catch { /* ignore */ }
  }, [logout]);

  // Activity listeners. While the warning is showing we ignore passive activity so
  // the user must explicitly choose Continue / Logout.
  useEffect(() => {
    const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'click', 'wheel'];
    const onActivity = () => {
      if (warningRef.current) return;
      const now = Date.now();
      if (now - throttleRef.current < ACTIVITY_THROTTLE_MS) return;
      throttleRef.current = now;
      resetTimer();
    };
    events.forEach((e) => window.addEventListener(e, onActivity, { passive: true }));
    return () => events.forEach((e) => window.removeEventListener(e, onActivity));
  }, [resetTimer]);

  // Periodic check: open the warning at 9 min, force logout at 10 min.
  useEffect(() => {
    const id = setInterval(() => {
      const idle = Date.now() - lastActivity.current;
      if (idle >= IDLE_LIMIT_MS) {
        expire();
      } else if (idle >= WARN_AT_MS && !warningRef.current) {
        warningRef.current = true;
        setWarning(true);
      }
    }, CHECK_EVERY_MS);
    return () => clearInterval(id);
  }, [expire]);

  // Live countdown while the warning dialog is open.
  useEffect(() => {
    if (!warning) return;
    const tick = () => setSecondsLeft(
      Math.max(0, Math.ceil((IDLE_LIMIT_MS - (Date.now() - lastActivity.current)) / 1000))
    );
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [warning]);

  if (!warning) return null;

  return (
    <div role="alertdialog" aria-modal="true"
      style={{ position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(10,37,64,0.55)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20 }}>
      <div style={{ background: T.surface, borderRadius: 16, padding: 28, width: 'min(420px, 100%)',
        boxShadow: '0 24px 70px rgba(0,0,0,0.3)', textAlign: 'center', boxSizing: 'border-box' }}>
        <div style={{ fontSize: 40, marginBottom: 10 }}>⏳</div>
        <h2 style={{ margin: '0 0 8px', fontSize: 18, fontWeight: 800, color: T.textMain }}>Session about to expire</h2>
        <p style={{ margin: '0 0 6px', fontSize: 13.5, color: T.textMuted }}>
          Your session will expire in 1 minute due to inactivity.
        </p>
        <p style={{ margin: '0 0 20px', fontSize: 26, fontWeight: 800, color: T.danger }}>{secondsLeft}s</p>
        <div style={{ display: 'flex', gap: 10 }}>
          <Btn full variant="secondary" onClick={() => logout()}>Logout Now</Btn>
          <Btn full onClick={() => resetTimer()}>Continue Session</Btn>
        </div>
      </div>
    </div>
  );
};

export default SessionManager;
