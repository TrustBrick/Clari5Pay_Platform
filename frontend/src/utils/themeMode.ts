// Theme mode controller: persists the user's choice (Light / Dark / System) in
// localStorage and applies the resolved theme to <html data-theme>. The CSS variables
// in index.css do the actual re-coloring, so applying a theme is just an attribute flip.
export type ThemeMode = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

const STORAGE_KEY = 'c5-theme';

export const getStoredMode = (): ThemeMode => {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch { /* ignore */ }
  return 'system';
};

const systemPrefersDark = (): boolean =>
  typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false;

export const resolveMode = (mode: ThemeMode): ResolvedTheme =>
  mode === 'system' ? (systemPrefersDark() ? 'dark' : 'light') : mode;

// Apply the theme to the document. `animate` adds a short transition window so the
// switch fades smoothly without making every hover transition globally.
export const applyTheme = (mode: ThemeMode, animate = false): ResolvedTheme => {
  const resolved = resolveMode(mode);
  const root = document.documentElement;
  if (animate) {
    root.setAttribute('data-theme-anim', '');
    window.setTimeout(() => root.removeAttribute('data-theme-anim'), 320);
  }
  root.setAttribute('data-theme', resolved);
  return resolved;
};

export const storeMode = (mode: ThemeMode) => {
  try { localStorage.setItem(STORAGE_KEY, mode); } catch { /* ignore */ }
};

// Concrete colors for charts (recharts renders SVG attributes that don't resolve var()).
// Brand series colors stay constant; grid/axis/tooltip flip with the theme.
export const chartPalette = (resolved: ResolvedTheme) =>
  resolved === 'dark'
    ? {
        grid: '#283342',
        axis: '#9fb0c5',
        tooltipBg: '#161d29',
        tooltipBorder: '#283342',
        tooltipText: '#e6edf6',
      }
    : {
        grid: '#f1f5f9',
        axis: '#4a5568',
        tooltipBg: '#ffffff',
        tooltipBorder: '#e2e8f0',
        tooltipText: '#0a2540',
      };
