import { useEffect, useRef } from 'react';

/**
 * Periodically invoke `fn` (default every 8s) to keep a screen's data fresh,
 * so changes made in other portals appear without a manual reload.
 * The latest `fn` is always used without resetting the interval.
 */
export const usePoll = (fn: () => void, ms = 8000) => {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const id = setInterval(() => ref.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
};
