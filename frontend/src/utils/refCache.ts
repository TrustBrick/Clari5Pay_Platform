// Client-side cache for slowly-changing reference data — the dropdown/lookup lists (agent form
// data, merchant lists, approvers, banks, …) that several screens each re-fetch on mount and
// again on every poll, even though the underlying data changes rarely.
//
// Two separate jobs, both needed:
//   1. TTL caching — a repeat call inside the window reuses the last value, no request at all.
//   2. In-flight de-duplication — concurrent callers (three components mounting together) share
//      ONE request instead of firing three identical ones.
//
// Deliberately NOT used for transactions, balances, or anything a user action changes: those must
// stay live. Reference data only.

interface Entry<T> { at: number; value?: T; inflight?: Promise<T> }

const store = new Map<string, Entry<unknown>>();

/** Default lifetime. Long enough to collapse a burst of mounts + polls, short enough that an
 *  edit made elsewhere in the app shows up quickly. */
export const REF_TTL_MS = 60_000;

/**
 * Wrap a loader so repeat calls within `ttlMs` reuse the cached value and concurrent calls share
 * one request. `key` must be unique per logical dataset (include any parameters that change it).
 */
export const cachedRef = <T,>(key: string, loader: () => Promise<T>, ttlMs = REF_TTL_MS): Promise<T> => {
  const hit = store.get(key) as Entry<T> | undefined;
  const now = Date.now();
  if (hit) {
    if (hit.inflight) return hit.inflight;                       // someone is already fetching
    if (hit.value !== undefined && now - hit.at < ttlMs) return Promise.resolve(hit.value);
  }
  const inflight = loader()
    .then(value => { store.set(key, { at: Date.now(), value }); return value; })
    .catch(err => { store.delete(key); throw err; });             // never cache a failure
  store.set(key, { at: now, inflight });
  return inflight;
};

/**
 * Drop cached reference data so the next read re-fetches. Call after a mutation that changes one
 * of these lists (creating an agent, adding a merchant, …). No argument clears everything;
 * a prefix clears just the matching keys.
 */
export const invalidateRef = (prefix?: string) => {
  if (!prefix) { store.clear(); return; }
  for (const k of [...store.keys()]) if (k.startsWith(prefix)) store.delete(k);
};
