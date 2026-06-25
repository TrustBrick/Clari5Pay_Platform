import React, { useState, type CSSProperties } from 'react';
import { T } from '../utils/theme';
import { Btn } from './UI';
import type { TxQuery } from '../services/api';

/**
 * Server-side transaction search & date/time filters (shared by the merchant/
 * supervisor/manager Transaction History and the admin All Transactions page).
 *
 * Filters are applied together only when "Apply Filters" is pressed (not live),
 * and "Clear Filters" resets everything. The parent fetches with the emitted
 * TxQuery and feeds the (already filtered) rows to the table + exports.
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

const TxSearchFilters: React.FC<{
  onApply: (q: TxQuery) => void;
  onClear: () => void;
}> = ({ onApply, onClear }) => {
  const [search, setSearch] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [dtFrom, setDtFrom] = useState('');
  const [dtTo, setDtTo] = useState('');

  const apply = () => onApply({
    search: search.trim() || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    datetime_from: dtFrom || undefined,
    datetime_to: dtTo || undefined,
  });

  const clear = () => {
    setSearch(''); setDateFrom(''); setDateTo(''); setDtFrom(''); setDtTo('');
    onClear();
  };

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-end' }}>
      <div style={{ ...field, flex: '2 1 220px', minWidth: 180 }}>
        <label style={lbl}>Search (Reference / Membership ID)</label>
        <input value={search} onChange={e => setSearch(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply(); }}
          placeholder="e.g. DEP0000101 or MM01" style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 130px' }}>
        <label style={lbl}>From Date</label>
        <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 130px' }}>
        <label style={lbl}>To Date</label>
        <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 170px' }}>
        <label style={lbl}>From Date &amp; Time</label>
        <input type="datetime-local" value={dtFrom} onChange={e => setDtFrom(e.target.value)} style={inp} />
      </div>
      <div style={{ ...field, flex: '1 1 170px' }}>
        <label style={lbl}>To Date &amp; Time</label>
        <input type="datetime-local" value={dtTo} onChange={e => setDtTo(e.target.value)} style={inp} />
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <Btn size="sm" onClick={apply}>🔍 Apply Filters</Btn>
        <Btn size="sm" variant="ghost" onClick={clear}>Clear Filters</Btn>
      </div>
    </div>
  );
};

export default TxSearchFilters;
