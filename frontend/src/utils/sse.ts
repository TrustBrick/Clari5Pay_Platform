import { useEffect, useRef, useState } from 'react';
import { BASE_URL } from '../services/api';
import type { ActiveUsersData } from '../types';

type SSEHandlers = {
  onMessage: (data: unknown) => void;
  onOpen?: () => void;
  onError?: () => void;
};

/**
 * Minimal Server-Sent-Events client over fetch().
 *
 * We can't use the native EventSource because it can't send an Authorization header, and this
 * app authenticates with a Bearer token (not a cookie). fetch() + a ReadableStream reader lets us
 * attach the token and still consume `text/event-stream`. Auto-reconnects with capped backoff;
 * stops on 401 so the axios layer's existing logout-redirect handles an expired token.
 */
export function openSSE(path: string, h: SSEHandlers): { close: () => void } {
  let closed = false;
  let ctrl: AbortController | null = null;
  let attempt = 0;

  const connect = async () => {
    if (closed) return;
    ctrl = new AbortController();
    const token = localStorage.getItem('clari5pay_token');
    try {
      const res = await fetch(BASE_URL + path, {
        headers: { Authorization: token ? `Bearer ${token}` : '', Accept: 'text/event-stream' },
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (res.status === 401) { closed = true; h.onError?.(); return; }  // token expired → let axios redirect
      if (!res.ok || !res.body) throw new Error(`SSE HTTP ${res.status}`);
      attempt = 0;
      h.onOpen?.();

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let sep: number;
        while ((sep = buf.indexOf('\n\n')) >= 0) {   // one SSE frame per blank-line separator
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const data = frame
            .split('\n')
            .filter(l => l.startsWith('data:'))
            .map(l => l.slice(5).replace(/^ /, ''))
            .join('\n');
          if (!data) continue;   // comment / keep-alive line
          try { h.onMessage(JSON.parse(data)); } catch { /* ignore a malformed frame */ }
        }
      }
      throw new Error('SSE stream closed');   // server ended the stream → reconnect
    } catch (err) {
      if (closed || (err as { name?: string })?.name === 'AbortError') return;
      h.onError?.();
      attempt = Math.min(attempt + 1, 6);
      setTimeout(connect, Math.min(1000 * 2 ** attempt, 15000));   // 2s → … → 15s backoff
    }
  };

  void connect();
  return { close() { closed = true; ctrl?.abort(); } };
}

/**
 * Subscribe a component to the live Active-Users presence stream. Returns whether the stream is
 * currently connected, so callers can suppress their polling fallback while push is live.
 */
export function usePresenceStream(onData: (d: ActiveUsersData) => void): boolean {
  const [live, setLive] = useState(false);
  const cb = useRef(onData);
  cb.current = onData;
  useEffect(() => {
    const conn = openSSE('/api/active-users/stream', {
      onMessage: d => cb.current(d as ActiveUsersData),
      onOpen: () => setLive(true),
      onError: () => setLive(false),
    });
    return () => conn.close();
  }, []);
  return live;
}
