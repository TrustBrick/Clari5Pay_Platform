import React from 'react';
import { Icon } from './Icon';

// Customer Support chat helpers — mirrors the merchant app's helpers. Timestamps are ALWAYS
// rendered in Indian Standard Time (Asia/Kolkata), regardless of the viewer's device timezone.
const IST_TZ = 'Asia/Kolkata';

export const chatTime = (iso: string) =>
  new Date(iso).toLocaleTimeString('en-US', { timeZone: IST_TZ, hour: '2-digit', minute: '2-digit', hour12: true });

export const chatDateLabel = (iso: string): string => {
  const key = (d: Date) => d.toLocaleDateString('en-CA', { timeZone: IST_TZ });   // YYYY-MM-DD in IST
  const k = key(new Date(iso));
  if (k === key(new Date())) return 'Today';
  if (k === key(new Date(Date.now() - 86400000))) return 'Yesterday';
  return new Date(iso).toLocaleDateString('en-GB', { timeZone: IST_TZ, day: '2-digit', month: 'short', year: 'numeric' });
};

export const formatBytes = (n?: number | null): string => {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};

export const CHAT_ACCEPT = '.jpg,.jpeg,.png,.webp,.pdf,.doc,.docx,.xls,.xlsx,.txt,.zip,image/*';
const IMG_TYPES = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp'];
const DOC_TYPES = [
  'application/pdf', 'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'text/plain', 'application/zip', 'application/x-zip-compressed',
];
const IMG_EXT = ['jpg', 'jpeg', 'png', 'webp'];
const DOC_EXT = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'zip'];
const EXT_MIME: Record<string, string> = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp',
  pdf: 'application/pdf', doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xls: 'application/vnd.ms-excel',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  txt: 'text/plain', zip: 'application/zip',
};

export const isChatImage = (type?: string | null, name?: string | null) => {
  const t = (type || '').toLowerCase();
  if (t.startsWith('image/')) return true;
  const ext = (name || '').split('.').pop()?.toLowerCase() || '';
  return IMG_EXT.includes(ext);
};

export const chatAttachmentError = (f: File): string | null => {
  const t = (f.type || '').toLowerCase();
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  const isImg = IMG_TYPES.includes(t) || IMG_EXT.includes(ext);
  const isDoc = DOC_TYPES.includes(t) || DOC_EXT.includes(ext);
  if (!isImg && !isDoc) return 'Unsupported file type. Allowed: images, PDF, Word, Excel, TXT, ZIP.';
  if (f.size > 8 * 1024 * 1024) return 'File too large. Maximum 8 MB.';
  return null;
};

export const readChatAttachment = (f: File): Promise<{ dataUrl: string; name: string; type: string; size: number }> =>
  new Promise((resolve, reject) => {
    const ext = (f.name.split('.').pop() || '').toLowerCase();
    const mime = (f.type && f.type !== 'application/octet-stream') ? f.type : (EXT_MIME[ext] || 'application/octet-stream');
    const r = new FileReader();
    r.onload = () => {
      let d = String(r.result || '');
      d = d.replace(/^data:[^;,]*;base64,/, `data:${mime};base64,`);
      resolve({ dataUrl: d, name: f.name, type: mime, size: f.size });
    };
    r.onerror = () => reject(new Error('read failed'));
    r.readAsDataURL(f);
  });

export const openDataUrl = (dataUrl: string) => {
  try {
    const [head, b64] = dataUrl.split(',');
    const mime = (head.match(/data:([^;]+)/) || [])[1] || 'application/octet-stream';
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], { type: mime }));
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch { window.open(dataUrl, '_blank'); }
};

type ChatTheme = { blue: string; surface: string; border: string; infoBg: string; textMain: string; textMuted: string };
type AttachMsg = { attachment?: string | null; attachmentName?: string | null; attachmentType?: string | null; attachmentSize?: number | null; content?: string };

// Renders a message's attachment: images inline (enlarge/open + download); documents as a
// compact card (View / Download). Colours are passed in so it matches the host app theme.
export const ChatAttachment: React.FC<{ msg: AttachMsg; mine: boolean; theme: ChatTheme }> = ({ msg, mine, theme }) => {
  if (!msg.attachment) return null;
  const name = msg.attachmentName || 'attachment';
  const linkColor = mine ? '#fff' : theme.blue;
  if (isChatImage(msg.attachmentType, name)) {
    return (
      <div style={{ marginTop: msg.content ? 8 : 0 }}>
        <img src={msg.attachment} alt={name} loading="lazy" onClick={() => openDataUrl(msg.attachment!)}
          style={{ maxWidth: 240, maxHeight: 260, borderRadius: 10, display: 'block', cursor: 'zoom-in', objectFit: 'cover' }} />
        <div style={{ marginTop: 4, display: 'flex', gap: 12 }}>
          <button onClick={() => openDataUrl(msg.attachment!)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, color: linkColor, textDecoration: 'underline' }}>Open</button>
          <a href={msg.attachment} download={name} style={{ fontSize: 10, color: linkColor, textDecoration: 'underline', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="download" size={11} /> Download</a>
        </div>
      </div>
    );
  }
  return (
    <div style={{ marginTop: msg.content ? 8 : 0, display: 'flex', alignItems: 'center', gap: 10, background: mine ? 'rgba(255,255,255,0.15)' : theme.surface, border: `1px solid ${mine ? 'rgba(255,255,255,0.25)' : theme.border}`, borderRadius: 10, padding: '8px 10px', maxWidth: 260 }}>
      <div style={{ width: 34, height: 34, borderRadius: 8, background: mine ? 'rgba(255,255,255,0.2)' : theme.infoBg, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18, flexShrink: 0, color: mine ? '#fff' : theme.textMuted }}><Icon name="file" size={18} /></div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: mine ? '#fff' : theme.textMain, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</p>
        <p style={{ margin: '1px 0 0', fontSize: 10, color: mine ? 'rgba(255,255,255,0.85)' : theme.textMuted }}>{formatBytes(msg.attachmentSize)}</p>
        <div style={{ display: 'flex', gap: 12, marginTop: 4 }}>
          <button onClick={() => openDataUrl(msg.attachment!)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, fontWeight: 700, color: linkColor, textDecoration: 'underline' }}>View</button>
          <a href={msg.attachment} download={name} style={{ fontSize: 10, fontWeight: 700, color: linkColor, textDecoration: 'underline' }}>Download</a>
        </div>
      </div>
    </div>
  );
};
