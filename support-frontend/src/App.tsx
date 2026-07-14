import React, { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import {
  login, getUser, clearAuth, fetchConversations, fetchMessages, fetchMerchant,
  fetchMerchantPresence, sendMessage, wsUrl, setAvailability, type Availability,
  type Conversation, type Message, type MerchantDetail, type SupportUser,
} from './api';
import { ThemeToggle } from './theme';
import { chatTime, chatDateLabel, ChatAttachment, chatAttachmentError, readChatAttachment, CHAT_ACCEPT } from './chat';
import DemoBanner from './DemoBanner';

// Colors are CSS variables (defined in theme.css for light + dark); flipping data-theme
// on <html> re-themes the app. `dark` stays dark in both (login + header brand bg).
const T = {
  blue: 'var(--c5-blue)', dark: 'var(--c5-dark)', surface: 'var(--c5-surface)', canvas: 'var(--c5-canvas)',
  textMain: 'var(--c5-text-main)', textMuted: 'var(--c5-text-muted)', textLight: 'var(--c5-text-light)', border: 'var(--c5-border)',
  borderLight: 'var(--c5-border-light)', success: 'var(--c5-success)', successBg: 'var(--c5-success-bg)',
  danger: 'var(--c5-danger)', infoBg: 'var(--c5-info-bg)', grad: 'linear-gradient(135deg,#0052cc,#00a3ff)',
};

// Reactively track narrow viewports so the layout can collapse to one pane on phones.
function useIsMobile(breakpoint = 760): boolean {
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth < breakpoint);
  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < breakpoint);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [breakpoint]);
  return isMobile;
}

// Last-seen timestamp for the offline badge — always rendered in Indian Standard Time.
const IST_TZ = 'Asia/Kolkata';
const lastSeenLabel = (iso: string): string => {
  const d = new Date(iso);
  const date = d.toLocaleDateString('en-GB', { timeZone: IST_TZ, day: '2-digit', month: 'short', year: 'numeric' });
  const time = d.toLocaleTimeString('en-US', { timeZone: IST_TZ, hour: '2-digit', minute: '2-digit', hour12: true });
  return `${date} ${time} IST`;
};

// Customer online/offline badge for the support sidebar. Presence comes from the shared
// Active Users presence service (session heartbeat) — no separate tracking here.
const PresenceBadge: React.FC<{ online?: boolean; lastSeen?: string | null }> = ({ online, lastSeen }) => (
  <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3 }}>
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 7, padding: '4px 12px', borderRadius: 999,
      fontSize: 12, fontWeight: 800,
      background: online ? T.successBg : 'rgba(148,163,184,0.14)',
      color: online ? T.success : T.textMuted,
    }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: online ? T.success : '#94a3b8', display: 'inline-block' }} />
      {online ? 'Online' : 'Offline'}
    </span>
    {!online && lastSeen && (
      <span style={{ fontSize: 10.5, color: T.textMuted }}>Last seen {lastSeenLabel(lastSeen)}</span>
    )}
  </div>
);

// ─── Login ─────────────────────────────────────────────────────────────────────
const Login: React.FC<{ onLogin: (u: SupportUser) => void }> = ({ onLogin }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (loading) return;                              // guard against double-submit
    if (!username.trim()) { setError('Username is required.'); return; }
    if (!password) { setError('Password is required.'); return; }
    setError(''); setLoading(true);
    // On failure `login` throws before storing anything — no token, no session, no redirect.
    try { onLogin(await login(username, password)); }
    catch (e: any) { setError(e.message || 'Invalid username or password.'); }
    finally { setLoading(false); }
  };

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: T.dark, fontFamily: "'Inter','Segoe UI',sans-serif", padding: 16, paddingTop: 'calc(16px + var(--demo-banner-h, 0px))', boxSizing: 'border-box', position: 'relative' }}>
      <DemoBanner />
      <div style={{ position: 'absolute', top: 18, right: 18 }}><ThemeToggle /></div>
      <div style={{ background: T.surface, borderRadius: 20, padding: 'clamp(24px, 5vw, 40px)', width: 'min(380px, 100%)', boxSizing: 'border-box', boxShadow: '0 24px 80px rgba(0,0,0,0.35)' }}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <img src="/logo-mark.png" alt="Clari5Pay" style={{ height: 84, width: 'auto', display: 'block', margin: '0 auto 8px' }} />
          <div style={{ fontFamily: "'Montserrat','Segoe UI',Arial,sans-serif", fontWeight: 800, fontSize: 30, letterSpacing: '-1px', lineHeight: 1 }}>
            <span style={{ color: '#0052cc' }}>clari</span><span style={{ color: '#26d00c' }}>5</span><span style={{ color: T.textMain }}>pay</span>
          </div>
          <p style={{ margin: '6px 0 0', fontSize: 12, color: T.textMuted, letterSpacing: '0.3px' }}>Secure Payments. Prevent Fraud.</p>
          <p style={{ margin: '10px 0 0', fontSize: 13, fontWeight: 700, color: T.textMain }}>Customer Support Portal</p>
        </div>
        {error && <div style={{ background: 'rgba(220,38,38,0.1)', color: T.danger, padding: '10px 14px', borderRadius: 10, fontSize: 12, marginBottom: 14, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="warning" size={13} /> {error}</div>}
        <Field label="Username" value={username} onChange={setUsername} placeholder="support1" onEnter={submit} />
        <Field label="Password" value={password} onChange={setPassword} placeholder="Your password" type="password" onEnter={submit} />
        <button onClick={submit} disabled={loading}
          style={{ width: '100%', marginTop: 8, padding: '12px', borderRadius: 10, border: 'none', background: T.grad, color: '#fff', fontWeight: 700, fontSize: 14, cursor: loading ? 'default' : 'pointer', opacity: loading ? 0.6 : 1 }}>
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
        <p style={{ marginTop: 18, fontSize: 11, color: T.textLight, textAlign: 'center' }}>Demo: <code>support1 / pass123</code></p>
      </div>
    </div>
  );
};

const Field: React.FC<{ label: string; value: string; onChange: (v: string) => void; placeholder?: string; type?: string; onEnter?: () => void }> =
  ({ label, value, onChange, placeholder, type = 'text', onEnter }) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: 'block', fontSize: 11, fontWeight: 700, color: T.textMuted, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</label>
      <input type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        onKeyDown={e => e.key === 'Enter' && onEnter?.()}
        style={{ width: '100%', padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 14, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit' }} />
    </div>
  );

// ─── Availability toggle (Available / Busy) — Offline is automatic on logout ───
const AV_META: Record<Availability, { label: string; dot: string }> = {
  AVAILABLE: { label: 'Available', dot: '#26d00c' },
  BUSY: { label: 'Busy', dot: '#f5a623' },
  ON_BREAK: { label: 'On Break', dot: '#dc2626' },
};
const AvailabilityToggle: React.FC<{ value: Availability; onChange: (v: Availability) => void; compact?: boolean }> = ({ value, onChange, compact }) => {
  const set = async (v: Availability) => {
    if (v === value) return;
    onChange(v);                                  // optimistic
    try { await setAvailability(v); } catch { /* best-effort; SSE will reconcile */ }
  };
  return (
    <div style={{ display: 'flex', background: 'rgba(255,255,255,0.12)', borderRadius: 8, padding: 2, gap: 2 }} title="Set your availability">
      {(['AVAILABLE', 'BUSY', 'ON_BREAK'] as Availability[]).map(v => {
        const active = v === value;
        return (
          <button key={v} onClick={() => set(v)} aria-label={AV_META[v].label}
            style={{ display: 'flex', alignItems: 'center', gap: 5, border: 'none', cursor: 'pointer', borderRadius: 6, padding: compact ? '4px 7px' : '5px 10px', fontSize: 11, fontWeight: 700, background: active ? '#fff' : 'transparent', color: active ? 'var(--c5-text-main)' : 'rgba(255,255,255,0.75)' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: AV_META[v].dot }} />
            {!compact && AV_META[v].label}
          </button>
        );
      })}
    </div>
  );
};

// ─── Console ─────────────────────────────────────────────────────────────────
const Console: React.FC<{ user: SupportUser; onLogout: () => void }> = ({ user, onLogout }) => {
  const [avail, setAvail] = useState<Availability>((user.supportAvailability as Availability) || 'AVAILABLE');
  const [convos, setConvos] = useState<Conversation[]>([]);
  const [search, setSearch] = useState('');
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [merchant, setMerchant] = useState<MerchantDetail | null>(null);
  const [input, setInput] = useState('');
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [attachError, setAttachError] = useState('');
  const [showDetails, setShowDetails] = useState(false);   // mobile: merchant-details slide-over
  const isMobile = useIsMobile();
  const wsRef = useRef<WebSocket | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const activeIdRef = useRef<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  activeIdRef.current = activeId;

  const refreshConvos = () => fetchConversations().then(setConvos).catch(() => {});

  useEffect(() => { refreshConvos(); }, []);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  // WebSocket for real-time delivery
  useEffect(() => {
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data) as Message;
        if (m.merchantId === activeIdRef.current) {
          setMessages(prev => prev.some(x => x.id === m.id) ? prev : [...prev, m]);
        }
        refreshConvos();
      } catch { /* ignore */ }
    };
    return () => ws.close();
  }, []);

  // Poll the open customer's online status so the sidebar badge stays current without a page
  // refresh. Uses the shared presence service (same heartbeat that powers Active Users).
  useEffect(() => {
    if (activeId == null) return;
    let cancelled = false;
    const apply = (p: { online: boolean; lastSeen?: string | null }) =>
      !cancelled && setMerchant(prev => (prev && prev.id === activeId ? { ...prev, online: p.online, lastSeen: p.lastSeen } : prev));
    fetchMerchantPresence(activeId).then(apply).catch(() => {});
    const t = window.setInterval(() => fetchMerchantPresence(activeId).then(apply).catch(() => {}), 5000);
    return () => { cancelled = true; window.clearInterval(t); };
  }, [activeId]);

  const openConversation = async (id: number) => {
    setActiveId(id);
    try {
      const [msgs, det] = await Promise.all([fetchMessages(id), fetchMerchant(id)]);
      setMessages(msgs);
      setMerchant(det);
      refreshConvos(); // unread reset
    } catch { /* ignore */ }
  };

  const send = async () => {
    const content = input.trim();
    if (!content || activeId == null) return;
    setInput('');
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ merchantId: activeId, content }));
    } else {
      try { const m = await sendMessage(content, activeId); setMessages(prev => [...prev, m]); refreshConvos(); } catch { /* ignore */ }
    }
  };

  // Attach an image/document to the active conversation (validated client-side, sent with any
  // typed text). Delivered to the customer in real time.
  const sendAttachment = async (f: File) => {
    if (activeId == null) return;
    const err = chatAttachmentError(f);
    if (err) { setAttachError(err); setTimeout(() => setAttachError(''), 4000); return; }
    setAttachError('');
    setSending(true);
    try {
      const att = await readChatAttachment(f);
      // Attachments go over REST (not the socket) — large base64 payloads can exceed the
      // WebSocket frame limit. The server delivers to both parties, so dedupe on id.
      const m = await sendMessage(input.trim(), activeId, { dataUrl: att.dataUrl, name: att.name });
      setMessages(prev => prev.some(x => x.id === m.id) ? prev : [...prev, m]);
      refreshConvos();
      setInput('');
    } catch (e: any) {
      setAttachError(e?.message || 'Failed to send attachment'); setTimeout(() => setAttachError(''), 4000);
    } finally { setSending(false); }
  };
  const onPickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) sendAttachment(f);
    e.target.value = '';
  };

  const filtered = convos.filter(c => !search || c.merchantName.toLowerCase().includes(search.toLowerCase()));

  // On mobile only one pane shows at a time: the list, or (once a convo is open) the chat.
  const showList = !isMobile || activeId == null;
  const showChat = !isMobile || activeId != null;
  const backToList = () => { setActiveId(null); setMessages([]); setMerchant(null); setShowDetails(false); };

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', fontFamily: "'Inter','Segoe UI',sans-serif", background: T.canvas, paddingTop: 'var(--demo-banner-h, 0px)', boxSizing: 'border-box' }}>
      <DemoBanner />
      {/* Top bar */}
      <header style={{ height: 56, background: T.dark, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: isMobile ? '0 12px' : '0 20px', flexShrink: 0, gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: isMobile ? 7 : 10, minWidth: 0 }}>
          {isMobile && activeId != null && (
            <button onClick={backToList} aria-label="Back to conversations"
              style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', width: 30, height: 30, borderRadius: 8, fontSize: 16, cursor: 'pointer', flexShrink: 0, lineHeight: 1 }}>←</button>
          )}
          <img src="/logo-mark.png" alt="" style={{ height: 28, width: 'auto', display: 'block' }} />
          <span style={{ color: '#fff', fontWeight: 800, fontSize: isMobile ? 13 : 15, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {isMobile ? 'Clari5Pay Support' : 'Clari5Pay Customer Support'}
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5, marginLeft: isMobile ? 4 : 12, flexShrink: 0 }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: connected ? T.success : T.textLight }} />
            {!isMobile && <span style={{ fontSize: 11, color: connected ? '#7ee0b8' : 'rgba(255,255,255,0.5)' }}>{connected ? 'Live' : 'Offline'}</span>}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: isMobile ? 8 : 12, flexShrink: 0 }}>
          <AvailabilityToggle value={avail} onChange={setAvail} compact={isMobile} />
          <ThemeToggle />
          {!isMobile && <span style={{ color: 'rgba(255,255,255,0.8)', fontSize: 12 }}>{user.name}</span>}
          {isMobile && activeId != null && merchant && (
            <button onClick={() => setShowDetails(true)} aria-label="Merchant details"
              style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', width: 30, height: 30, borderRadius: 8, fontSize: 14, cursor: 'pointer', lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}><Icon name="info" size={16} /></button>
          )}
          <button onClick={onLogout} style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer', fontWeight: 600, whiteSpace: 'nowrap' }}>Sign Out</button>
        </div>
      </header>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', position: 'relative' }}>
        {/* Conversations list */}
        {showList && (
        <aside style={{ width: isMobile ? '100%' : 300, background: T.surface, borderRight: isMobile ? 'none' : `1px solid ${T.border}`, display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
          <div style={{ padding: 14, borderBottom: `1px solid ${T.border}` }}>
            <div style={{ position: 'relative' }}>
              <span style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', display: 'flex', color: T.textMuted }}><Icon name="search" size={15} /></span>
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search merchant..."
                style={{ width: '100%', padding: '8px 12px 8px 32px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 13, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit' }} />
            </div>
          </div>
          <div style={{ flex: 1, overflowY: 'auto' }}>
            {filtered.length === 0 && <p style={{ padding: 20, textAlign: 'center', color: T.textMuted, fontSize: 13 }}>No conversations</p>}
            {filtered.map(c => (
              <div key={c.merchantId} onClick={() => openConversation(c.merchantId)}
                style={{ padding: '12px 14px', borderBottom: `1px solid ${T.borderLight}`, cursor: 'pointer', background: activeId === c.merchantId ? T.infoBg : 'transparent' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontWeight: 700, fontSize: 13, color: T.textMain, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.merchantName}</span>
                  {c.unread > 0 && <span style={{ background: T.danger, color: '#fff', borderRadius: 10, fontSize: 10, padding: '1px 7px', fontWeight: 800, flexShrink: 0 }}>{c.unread}</span>}
                </div>
                <p style={{ margin: '3px 0 0', fontSize: 11, color: T.textMuted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.lastMessage || 'No messages yet'}</p>
              </div>
            ))}
          </div>
        </aside>
        )}

        {/* Chat thread */}
        {showChat && (
        <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {activeId == null ? (
            <div style={{ margin: 'auto', textAlign: 'center', color: T.textMuted }}>
              <img src="/logo-mark.png" alt="" style={{ height: 64, width: 'auto', margin: '0 auto 8px', opacity: 0.55 }} />
              <p>Select a conversation to start chatting</p>
            </div>
          ) : (
            <>
              <div style={{ padding: '14px 20px', borderBottom: `1px solid ${T.border}`, background: T.surface }}>
                <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: T.textMain }}>{merchant?.name || 'Merchant'}</h2>
                <p style={{ margin: 0, fontSize: 11, color: T.textMuted }}>{merchant?.email}</p>
              </div>
              <div style={{ flex: 1, overflowY: 'auto', padding: isMobile ? 14 : 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
                {messages.length === 0 && <p style={{ margin: 'auto', color: T.textMuted, fontSize: 13 }}>No messages yet</p>}
                {messages.map((m, i) => {
                  const mine = m.sender === 'SUPPORT';
                  const prev = messages[i - 1];
                  const showSep = i === 0 || chatDateLabel(prev.createdAt) !== chatDateLabel(m.createdAt);
                  return (
                    <React.Fragment key={m.id}>
                      {showSep && (
                        <div style={{ alignSelf: 'center', background: T.canvas, color: T.textMuted, fontSize: 10, fontWeight: 700, padding: '3px 12px', borderRadius: 12, margin: '4px 0' }}>{chatDateLabel(m.createdAt)}</div>
                      )}
                      <div style={{ display: 'flex', justifyContent: mine ? 'flex-end' : 'flex-start' }}>
                        <div style={{ maxWidth: isMobile ? '85%' : '70%', padding: '10px 14px', borderRadius: mine ? '16px 16px 4px 16px' : '16px 16px 16px 4px', background: mine ? T.grad : T.surface, color: mine ? '#fff' : T.textMain, fontSize: 13, lineHeight: 1.5, border: mine ? 'none' : `1px solid ${T.border}` }}>
                          {!mine && <p style={{ margin: '0 0 2px', fontSize: 10, fontWeight: 800, color: T.blue }}>{m.senderName}</p>}
                          {m.content && <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.content}</span>}
                          <ChatAttachment msg={m} mine={mine} theme={{ blue: T.blue, surface: T.surface, border: T.border, infoBg: T.infoBg, textMain: T.textMain, textMuted: T.textMuted }} />
                          <p style={{ margin: '3px 0 0', fontSize: 9, opacity: 0.6, textAlign: 'right' }}>{chatTime(m.createdAt)}</p>
                        </div>
                      </div>
                    </React.Fragment>
                  );
                })}
                <div ref={bottomRef} />
              </div>
              <div style={{ borderTop: `1px solid ${T.border}`, background: T.surface }}>
                {attachError && (
                  <p style={{ margin: 0, padding: '8px 16px 0', fontSize: 11, color: T.danger, fontWeight: 600 }}>{attachError}</p>
                )}
                <div style={{ padding: '12px 16px', display: 'flex', gap: 10, alignItems: 'center' }}>
                  <input ref={fileRef} type="file" accept={CHAT_ACCEPT} onChange={onPickFile} style={{ display: 'none' }} />
                  <button onClick={() => fileRef.current?.click()} disabled={sending} title="Attach image or document" aria-label="Attach file"
                    style={{ width: 40, height: 40, flexShrink: 0, borderRadius: 10, border: `1.5px solid ${T.border}`, background: T.canvas, color: T.textMuted, fontSize: 18, cursor: sending ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>{sending ? <Icon name="pending" size={18} /> : <Icon name="attach" size={18} />}</button>
                  <input value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && send()}
                    placeholder={sending ? 'Sending attachment…' : 'Type your reply...'} disabled={sending}
                    style={{ flex: 1, padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 12, fontSize: 13, outline: 'none', fontFamily: 'inherit', background: T.canvas }} />
                  <button onClick={send} disabled={!input.trim() || sending}
                    style={{ padding: '10px 20px', borderRadius: 12, border: 'none', background: T.grad, color: '#fff', fontWeight: 700, fontSize: 13, cursor: 'pointer', opacity: (input.trim() && !sending) ? 1 : 0.6 }}><span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}><Icon name="send" size={14} /> Send</span></button>
                </div>
              </div>
            </>
          )}
        </main>
        )}

        {/* Merchant details — fixed right pane on desktop, slide-over on mobile */}
        {merchant && (isMobile ? showDetails : activeId != null) && (
          <aside style={isMobile
            ? { position: 'absolute', inset: 0, zIndex: 30, background: T.surface, padding: 20, overflowY: 'auto' }
            : { width: 260, background: T.surface, borderLeft: `1px solid ${T.border}`, padding: 20, overflowY: 'auto', flexShrink: 0 }}>
            {isMobile && (
              <button onClick={() => setShowDetails(false)}
                style={{ width: '100%', marginBottom: 14, padding: '9px', borderRadius: 10, border: `1.5px solid ${T.border}`, background: T.canvas, color: T.textMain, fontSize: 13, fontWeight: 700, cursor: 'pointer' }}><span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}><Icon name="close" size={13} /> Close</span></button>
            )}
            <div style={{ textAlign: 'center', marginBottom: 18 }}>
              <div style={{ width: 60, height: 60, borderRadius: '50%', background: T.grad, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 24, fontWeight: 800, color: '#fff', margin: '0 auto 10px' }}>{merchant.name.charAt(0)}</div>
              <p style={{ margin: 0, fontWeight: 800, fontSize: 15, color: T.textMain }}>{merchant.name}</p>
              <span style={{ display: 'inline-block', marginTop: 6, padding: '2px 10px', borderRadius: 12, fontSize: 11, fontWeight: 700, background: merchant.active ? T.successBg : 'rgba(220,38,38,0.1)', color: merchant.active ? T.success : T.danger }}>{merchant.active ? 'Active' : 'Inactive'}</span>
              <PresenceBadge online={merchant.online} lastSeen={merchant.lastSeen} />
            </div>
            <h4 style={{ fontSize: 11, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Merchant Details</h4>
            {([
              ['User ID', merchant.username],
              ['Email', merchant.email],
              ['Phone', merchant.phone || '—'],
              ['Balance', merchant.balance != null ? `₹${merchant.balance.toLocaleString('en-IN')}` : '—'],
              ['Risk', merchant.risk || '—'],
              ['Profile', merchant.profile || '—'],
              ['Pay-In', merchant.payIn || '—'],
              ['Pay-Out', merchant.payOut || '—'],
              ['Member Since', merchant.created],
            ] as Array<[string, string]>).map(([k, v]) => (
              <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: `1px solid ${T.borderLight}`, gap: 8 }}>
                <span style={{ fontSize: 12, color: T.textMuted }}>{k}</span>
                <span style={{ fontSize: 12, fontWeight: 700, color: T.textMain, textAlign: 'right', wordBreak: 'break-word' }}>{v}</span>
              </div>
            ))}
          </aside>
        )}
      </div>
    </div>
  );
};

// ─── Root ──────────────────────────────────────────────────────────────────────
const App: React.FC = () => {
  const [user, setUser] = useState<SupportUser | null>(getUser());
  if (!user) return <Login onLogin={setUser} />;
  return <Console user={user} onLogout={() => { clearAuth(); setUser(null); }} />;
};

export default App;
