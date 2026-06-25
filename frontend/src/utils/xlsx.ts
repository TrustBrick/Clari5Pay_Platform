import * as XLSX from 'xlsx';
import { typeLabel, depositTypeLabel, memberLabel } from './helpers';
import type { Transaction } from '../types';

// ─── Shared client-side Excel (.xlsx) export — SheetJS, no server round-trip ──────
// Mirrors the existing print-to-PDF exports: build in the browser and download.
// Header row + auto-fitted column widths (SheetJS community build can't style cells).

export type Col<T> = { header: string; get: (row: T) => unknown; width?: number; z?: string };
export type SheetDef<T = any> = { name: string; columns: Col<T>[]; rows: T[] };

// Excel custom number format for Indian grouping (lakh/crore), e.g. 1,00,000.00.
// Kept numeric so Excel can still sum — only the *display* uses Indian grouping.
export const INR_NUMFMT = '#,##,##0.00';

// Auto width: widest of header / cell content, padded, clamped to a sane range.
const autoWidth = (header: string, cells: unknown[]): number => {
  let max = header.length;
  for (const c of cells) {
    const len = String(c ?? '').length;
    if (len > max) max = len;
  }
  return Math.min(Math.max(max + 2, 10), 60);
};

const buildSheet = <T,>(s: SheetDef<T>): XLSX.WorkSheet => {
  const header = s.columns.map(c => c.header);
  const body = s.rows.map(r => s.columns.map(c => {
    const v = c.get(r);
    return v == null ? '' : v;   // keep numbers numeric so Excel can sum them
  }));
  const ws = XLSX.utils.aoa_to_sheet([header, ...body]);
  ws['!cols'] = s.columns.map((c, i) =>
    ({ wch: c.width ?? autoWidth(c.header, body.map(r => r[i])) }));
  // Apply per-column number formats (e.g. Indian grouping on amounts). Number formats are
  // honoured by the SheetJS community build (unlike cell styling), and keep cells numeric.
  s.columns.forEach((c, ci) => {
    if (!c.z) return;
    for (let ri = 0; ri < body.length; ri++) {
      const cell = ws[XLSX.utils.encode_cell({ r: ri + 1, c: ci })];
      if (cell && cell.t === 'n') cell.z = c.z;
    }
  });
  return ws;
};

/** Download one workbook with one or more sheets. Sheet names are clamped to Excel's 31-char limit. */
export function downloadXlsx(filename: string, sheets: SheetDef[]) {
  const wb = XLSX.utils.book_new();
  const used = new Set<string>();
  sheets.forEach((s, idx) => {
    let name = (s.name || `Sheet${idx + 1}`).replace(/[\\/?*[\]:]/g, ' ').slice(0, 31) || `Sheet${idx + 1}`;
    while (used.has(name)) name = name.slice(0, 28) + (idx + 1);   // de-dupe
    used.add(name);
    XLSX.utils.book_append_sheet(wb, buildSheet(s), name);
  });
  XLSX.writeFile(wb, filename.endsWith('.xlsx') ? filename : `${filename}.xlsx`);
}

const prettyStatus = (s: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

// Transaction type label, with the deposit method appended for deposits
// (e.g. "Deposit Request (Crypto (USDT))").
export const txnTypeLabel = (t: Transaction): string => {
  const base = typeLabel(t.type);
  return String(t.type).startsWith('DEPOSIT') && t.depositType
    ? `${base} (${depositTypeLabel(t.depositType)})`
    : base;
};

// Standard transaction columns used by the History / Member-Reports / Reports exports.
export const txnsToSheet = (rows: Transaction[], name = 'Transactions'): SheetDef<Transaction> => ({
  name,
  columns: [
    { header: 'Reference Number', get: t => t.ref },
    { header: 'Membership - Member', get: t => memberLabel(t.memberId, t.member), width: 28 },
    { header: 'Merchant Name', get: t => t.merchant || '' },
    { header: 'Transaction Type', get: t => txnTypeLabel(t) },
    { header: 'Amount (INR)', get: t => Number(t.amount), width: 14, z: INR_NUMFMT },
    { header: 'Status', get: t => prettyStatus(t.status) },
    { header: 'Date & Time', get: t => `${t.date || ''} ${t.time || ''}`.trim(), width: 20 },
    { header: 'Created By', get: t => t.merchant || '' },
    { header: 'UTR / Reference', get: t => t.adminUtr || t.utr || t.merchantRef || '' },
    { header: 'Remarks', get: t => t.cancelReason ? `Cancelled: ${t.cancelReason}` : (t.rejectReason || t.notes || '') },
  ],
  rows,
});

/** Convenience: export a list of transactions to a single-sheet workbook. */
export const exportTransactionsXlsx = (rows: Transaction[], filename: string, sheetName = 'Transactions') =>
  downloadXlsx(filename, [txnsToSheet(rows, sheetName)]);
