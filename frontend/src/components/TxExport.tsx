import React, { useState } from 'react';
import { T } from '../utils/theme';
import { fmt, memberLabel } from '../utils/helpers';
import { exportTransactionsXlsx, txnTypeLabel } from '../utils/xlsx';
import { Btn, Sel } from './UI';
import { Icon } from './Icon';
import type { Transaction } from '../types';

// ─── Branded PDF export of transaction history (print-to-PDF; no extra deps) ──────
const esc = (s: unknown) => String(s ?? '—').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string));
const prettyStatus = (s: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

export function exportTransactionsPdf(rows: Transaction[], title: string, subtitle: string) {
  const w = window.open('', '_blank', 'width=1000,height=800');
  if (!w) { alert('Please allow pop-ups for this site to export the PDF.'); return; }
  const now = new Date().toLocaleString('en-IN');
  const body = rows.map((t, i) => `<tr class="${i % 2 ? 'alt' : ''}">
    <td class="mono">${esc(t.ref)}</td>
    <td>${esc(memberLabel(t.memberId, t.member))}</td>
    <td>${esc(txnTypeLabel(t))}</td>
    <td class="amt">${esc(fmt(t.amount))}</td>
    <td>${esc(prettyStatus(t.status))}</td>
    <td class="nowrap">${esc(t.date)} ${esc(t.time)}</td>
    <td class="mono">${esc(t.adminUtr || t.utr || t.merchantRef)}</td>
    <td>${esc(t.cancelReason ? `Cancelled: ${t.cancelReason}` : (t.rejectReason || t.notes))}</td>
  </tr>`).join('');
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title>
  <style>
    @page { size: A4 landscape; margin: 14mm; }
    * { box-sizing: border-box; }
    body { font-family: Arial, 'Segoe UI', sans-serif; color: #0a2540; margin: 0; }
    .head { display: flex; align-items: center; gap: 14px; border-bottom: 3px solid #0052cc; padding-bottom: 12px; margin-bottom: 6px; }
    .head img { height: 54px; width: auto; }
    .brand { font-size: 26px; font-weight: 800; letter-spacing: -0.5px; }
    .brand .b { color: #0052cc; } .brand .g { color: #26d00c; } .brand .n { color: #0a2540; }
    .meta { margin-left: auto; text-align: right; font-size: 11px; color: #4a5568; line-height: 1.6; }
    h1 { font-size: 16px; margin: 14px 0 2px; }
    .sub { font-size: 12px; color: #4a5568; margin: 0 0 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
    th { background: #0a2540; color: #fff; text-align: left; padding: 7px 8px; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.04em; }
    td { padding: 6px 8px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
    tr.alt td { background: #f5f8ff; }
    .amt { text-align: right; font-weight: 700; white-space: nowrap; }
    .mono { font-family: 'Courier New', monospace; }
    .nowrap { white-space: nowrap; }
    .empty { text-align: center; padding: 30px; color: #9ca3af; }
    footer { margin-top: 14px; font-size: 9.5px; color: #9ca3af; text-align: center; }
  </style></head>
  <body>
    <div class="head">
      <img src="/logo-mark.png" alt="">
      <span class="brand"><span class="b">clari</span><span class="g">5</span><span class="n">pay</span></span>
      <div class="meta">Secure Payments. Prevent Fraud.<br>Generated: ${esc(now)}<br>${rows.length} transaction(s)</div>
    </div>
    <h1>${esc(title)}</h1>
    <p class="sub">${esc(subtitle)}</p>
    <table>
      <thead><tr>
        <th>Reference No.</th><th>Membership - Member</th><th>Type</th>
        <th style="text-align:right">Amount</th><th>Status</th><th>Date &amp; Time</th><th>UTR</th><th>Remarks</th>
      </tr></thead>
      <tbody>${body || '<tr><td class="empty" colspan="8">No transactions for this selection.</td></tr>'}</tbody>
    </table>
    <footer>Clari5Pay — confidential. This report was generated from live platform data.</footer>
  </body></html>`);
  w.document.close();
  w.focus();
  // Give the logo image a moment to load before invoking the print dialog.
  setTimeout(() => { try { w.print(); } catch { /* user can print manually */ } }, 500);
}

/**
 * `txns` is what the caller already has in memory. Callers whose table is server-paginated pass
 * `fetchTxns` as well: it resolves the ENTIRE filtered result set, so an export is never silently
 * reduced to the visible page. Without it, behaviour is exactly as before.
 */
export const TxExportButton: React.FC<{
  txns: Transaction[]; title?: string; fetchTxns?: () => Promise<Transaction[]>;
}> = ({ txns, title = 'Transaction Report', fetchTxns }) => {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'last' | 'range'>('last');
  const [lastN, setLastN] = useState('50');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [busy, setBusy] = useState(false);

  // Apply the chosen Last-N / date-range filter, returning the rows + a subtitle/scope label.
  const selected = async (): Promise<{ rows: Transaction[]; subtitle: string; scope: string }> => {
    let rows = fetchTxns ? await fetchTxns() : [...txns];   // already newest-first from the API
    if (mode === 'range') {
      rows = rows.filter(t => (!from || (t.date || '') >= from) && (!to || (t.date || '') <= to));
      return { rows, subtitle: `Date range: ${from || 'beginning'} → ${to || 'today'}`, scope: `${from || 'all'}_${to || 'today'}` };
    }
    const n = parseInt(lastN, 10) || 50;
    rows = rows.slice(0, n);
    return { rows, subtitle: `Last ${n} transactions`, scope: `last-${n}` };
  };

  const runPdf = async () => {
    if (busy) return;
    setBusy(true);
    try { const { rows, subtitle } = await selected(); exportTransactionsPdf(rows, title, subtitle); setOpen(false); }
    finally { setBusy(false); }
  };
  const runExcel = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { rows, scope } = await selected();
      exportTransactionsXlsx(rows, `${title.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-${scope}.xlsx`, title.slice(0, 31));
      setOpen(false);
    } finally { setBusy(false); }
  };

  const inp = { padding: '8px 12px', border: `1.5px solid ${T.border}`, borderRadius: 10, fontSize: 13, outline: 'none', fontFamily: 'inherit', width: '100%', boxSizing: 'border-box' as const };
  const lbl = { display: 'block', fontSize: 11, fontWeight: 700, color: T.textMuted, margin: '0 0 5px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' };

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <Btn size="sm" variant="secondary" onClick={() => setOpen(o => !o)}><Icon name="export" size={14} /> Export</Btn>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} />
          <div style={{ position: 'absolute', right: 0, top: 'calc(100% + 8px)', zIndex: 41, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 12, boxShadow: '0 12px 40px rgba(0,0,0,0.16)', padding: 16, width: 280 }}>
            <p style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 800, color: T.textMain }}>Export Transactions</p>
            <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
              <Btn size="sm" variant={mode === 'last' ? 'primary' : 'ghost'} onClick={() => setMode('last')}>Last N</Btn>
              <Btn size="sm" variant={mode === 'range' ? 'primary' : 'ghost'} onClick={() => setMode('range')}>Date range</Btn>
            </div>
            {mode === 'last' ? (
              <div style={{ marginBottom: 12 }}>
                <Sel label="How many" value={lastN} onChange={e => setLastN(e.target.value)}
                  options={['10', '50', '100', '500'].map(v => ({ value: v, label: `Last ${v} transactions` }))} />
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 12 }}>
                <div><label style={lbl}>From</label><input type="date" value={from} onChange={e => setFrom(e.target.value)} style={inp} /></div>
                <div><label style={lbl}>To</label><input type="date" value={to} onChange={e => setTo(e.target.value)} style={inp} /></div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <Btn full variant="secondary" onClick={runPdf} disabled={busy}><Icon name="pdf" size={15} /> {busy ? 'Preparing…' : 'Download PDF'}</Btn>
              <Btn full onClick={runExcel} disabled={busy}><Icon name="excel" size={15} /> {busy ? 'Preparing…' : 'Download Excel'}</Btn>
            </div>
          </div>
        </>
      )}
    </div>
  );
};
