/**
 * Date/time display for the Support portal.
 *
 * The Support portal is a SEPARATE Vite app with its own dependency graph — it cannot import
 * `frontend/src/utils/helpers`, so this is a deliberate mirror of the formatter there. Keep the
 * two in step: a timestamp must look the same to a support agent as it does to the merchant
 * whose request they are looking at.
 *
 *     04 Sep 2026, 04:06 PM
 *
 * Built from Intl PARTS rather than from a locale's own string, because `toLocaleString` output
 * is not a contract — the separator, the digit padding and the case of am/pm all vary by ICU
 * version and browser, which is how the same build ends up showing two different shapes.
 *
 * TIMEZONE IS UNCHANGED: this portal has always rendered in IST, the timezone conversations are
 * stamped in, and still does. Only the shape is standardised.
 */
const IST_TZ = 'Asia/Kolkata';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = (n: number) => String(n).padStart(2, '0');

/** The platform's empty-value convention — never "Invalid Date". */
export const EMPTY_VALUE = '—';

const toDate = (v?: string | number | Date | null): Date | null => {
  if (v === null || v === undefined || v === '') return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  const raw = typeof v === 'number' ? v : String(v).trim();
  if (raw === '') return null;
  // A zoneless date-TIME is UTC — that is what the API sends.
  const iso = typeof raw === 'string' && !/[zZ]|[+-]\d\d:?\d\d$/.test(raw) && raw.includes('T')
    ? raw + 'Z' : raw;
  const d = new Date(iso as string | number);
  return isNaN(d.getTime()) ? null : d;
};

const wallClock = (dt: Date, tz?: string) => {
  const parts = new Intl.DateTimeFormat('en-US', {
    ...(tz ? { timeZone: tz } : {}),
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hour12: false,
  }).formatToParts(dt);
  const get = (t: string) => Number(parts.find(p => p.type === t)?.value ?? 0);
  return { y: get('year'), mo: get('month'), d: get('day'), h: get('hour') % 24, mi: get('minute') };
};

const datePart = (c: ReturnType<typeof wallClock>) => `${pad2(c.d)} ${MONTHS[c.mo - 1]} ${c.y}`;
const timePart = (c: ReturnType<typeof wallClock>) =>
  `${pad2(c.h % 12 === 0 ? 12 : c.h % 12)}:${pad2(c.mi)} ${c.h < 12 ? 'AM' : 'PM'}`;

type DtOpts = { suffix?: boolean; empty?: string };

/** "04 Sep 2026, 04:06 PM" — IST, the portal's stated timezone. */
export const formatDateTime = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  const c = wallClock(dt, IST_TZ);
  return `${datePart(c)}, ${timePart(c)}${opts.suffix ? ' IST' : ''}`;
};

/** "04 Sep 2026" */
export const formatDate = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  return datePart(wallClock(dt, IST_TZ));
};

/** "04:06 PM" */
export const formatTime = (v?: string | number | Date | null, opts: DtOpts = {}) => {
  const dt = toDate(v);
  if (!dt) return opts.empty ?? EMPTY_VALUE;
  return timePart(wallClock(dt, IST_TZ));
};
