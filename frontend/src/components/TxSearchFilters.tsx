import React, { useEffect, useRef, type CSSProperties } from 'react';
import { T } from '../utils/theme';
import { Btn } from './UI';
import { Icon } from './Icon';
import { useSessionState } from '../utils/usePoll';
import type { TxQuery } from '../services/api';
import { IS_DEMO } from '../utils/portal';

/**
 * Server-side transaction search & date/time filters (shared by the merchant/
 * supervisor/manager Transaction History and the admin All Transactions page).
 *
 * Fields: Transaction Reference Number, Membership ID, From/To Date and From/To
 * Date & Time. All match server-side (reference & Membership ID are partial,
 * case-insensitive) and combine together. Filters are applied only when "Apply
 * Filters" is pressed (not live); "Clear Filters" resets everything. The parent
 * fetches with the emitted TxQuery and feeds the (already filtered) rows to the
 * table + exports. While the parent's request is in flight it passes `loading`,
 * which shows a spinner and disables the buttons to prevent duplicate requests.
 */
const inp: CSSProperties = {
  padding: '8px 10px', border: `1.5px solid ${T.border}`, borderRadius: 10,
  fontSize: 12, outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit',
  background: T.surface, color: T.textMain, width: '100%',
};
const lbl: CSSProperties = {
  display: 'block', fontSize: 9.5, fontWeight: 800, color: T.textMuted,
  margin: '0 0 3px', textTransform: 'uppercase', letterSpacing: '0.05em',
};
const field: CSSProperties = { display: 'flex', flexDirection: 'column' };

const EMPTY_FILTERS = { ref: '', memberId: '', dateFrom: '', dateTo: '', dtFrom: '', dtTo: '', txClass: 'ALL' };

const TxSearchFilters: React.FC<{
  onApply: (q: TxQuery) => void;
  onClear: () => void;
  loading?: boolean;
  /** Persists the filter fields (per-tab, via sessionStorage) so they survive navigation and
   *  refresh, and re-applies them once on mount to restore the filtered view. Pass a distinct
   *  key per page so All Transactions and Transaction History remember independently. */
  storageKey: string;
}> = ({ onApply, onClear, loading, storageKey }) => {
  const [f, setF] = useSessionState(`txfilters:${storageKey}`, EMPTY_FILTERS);
  const set = (k: keyof typeof EMPTY_FILTERS, v: string) => setF(p => ({ ...p, [k]: v }));

  const toQuery = (s: typeof EMPTY_FILTERS): TxQuery => ({
    ref: s.ref.trim() || undefined,
    member_id: s.memberId.trim() || undefined,
    date_from: s.dateFrom || undefined,
    date_to: s.dateTo || undefined,
    datetime_from: s.dtFrom || undefined,
    datetime_to: s.dtTo || undefined,
    // Crypto Balance module — Transaction Type filter (demo-only UI, harmless if sent on prod).
    tx_class: s.txClass === 'ALL' ? undefined : s.txClass.toLowerCase(),
  });

  const apply = () => {
    if (loading) return;
    onApply(toQuery(f));
  };

  const clear = () => {
    if (loading) return;
    setF(EMPTY_FILTERS);
    onClear();
  };

  // On mount, if a remembered filter set is present, re-apply it once so the table/exports show
  // the same filtered view the user left — not just the populated fields. Compared against each
  // field's own default (not just truthiness) since txClass defaults to the non-empty 'ALL'.
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    const changed = (Object.keys(EMPTY_FILTERS) as Array<keyof typeof EMPTY_FILTERS>)
      .some(k => f[k] !== EMPTY_FILTERS[k]);
    if (changed) onApply(toQuery(f));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-end' }}>
      <div style={{ ...field, flex: '1 1 180px', minWidth: 160 }}>
        <label style={lbl}>Transaction Reference Number</label>
        <input value={f.ref} onChange={e => set('ref', e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply(); }}
          placeholder="e.g. DEP0000101" style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 150px', minWidth: 140 }}>
        <label style={lbl}>Membership ID</label>
        <input value={f.memberId} onChange={e => set('memberId', e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply(); }}
          placeholder="e.g. MBR20240001" style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 130px' }}>
        <label style={lbl}>From Date</label>
        <input type="date" value={f.dateFrom} onChange={e => set('dateFrom', e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 130px' }}>
        <label style={lbl}>To Date</label>
        <input type="date" value={f.dateTo} onChange={e => set('dateTo', e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 170px' }}>
        <label style={lbl}>From Date &amp; Time</label>
        <input type="datetime-local" value={f.dtFrom} onChange={e => set('dtFrom', e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 170px' }}>
        <label style={lbl}>To Date &amp; Time</label>
        <input type="datetime-local" value={f.dtTo} onChange={e => set('dtTo', e.target.value)} style={inp} />
      </div>
      {IS_DEMO && (
        <div style={{ ...field, flex: '1 1 140px' }}>
          <label style={lbl}>Transaction Type</label>
          <select value={f.txClass} onChange={e => set('txClass', e.target.value)} style={inp}>
            <option value="ALL">All</option>
            <option value="BUSINESS">Business</option>
            <option value="CRYPTO">Crypto</option>
          </select>
        </div>
      )}
      <div style={{ display: 'flex', gap: 8 }}>
        <Btn size="sm" onClick={apply} disabled={loading}>{loading ? <><Icon name="pending" size={14} /> Applying…</> : <><Icon name="search" size={14} /> Apply Filters</>}</Btn>
        <Btn size="sm" variant="ghost" onClick={clear} disabled={loading}>Clear Filters</Btn>
      </div>
    </div>
  );
};

export default TxSearchFilters;
