import React, { useState } from 'react';

// Lightweight theme controller for the Support portal (Light / Dark / System),
// mirroring the main app. Preference persists in localStorage (key shared name,
// but localStorage is per-origin so it's scoped to the support subdomain).
export type ThemeMode = 'light' | 'dark' | 'system';
const KEY = 'c5-theme';

export const getMode = (): ThemeMode => {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch { /* ignore */ }
  return 'system';
};

const prefersDark = () =>
  !!window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

export const applyMode = (mode: ThemeMode, animate = false) => {
  const dark = mode === 'dark' || (mode === 'system' && prefersDark());
  const root = document.documentElement;
  if (animate) {
    root.setAttribute('data-theme-anim', '');
    setTimeout(() => root.removeAttribute('data-theme-anim'), 320);
  }
  root.setAttribute('data-theme', dark ? 'dark' : 'light');
};

const OPTS: { m: ThemeMode; icon: string; label: string }[] = [
  { m: 'light', icon: '☀', label: 'Light' },
  { m: 'dark', icon: '🌙', label: 'Dark' },
  { m: 'system', icon: '🖥', label: 'System' },
];

// 3-way toggle. Styled with theme vars so it reads in both modes.
export const ThemeToggle: React.FC = () => {
  const [mode, setMode] = useState<ThemeMode>(getMode);
  const choose = (m: ThemeMode) => {
    setMode(m);
    try { localStorage.setItem(KEY, m); } catch { /* ignore */ }
    applyMode(m, true);
  };
  return (
    <div role="group" aria-label="Theme" style={{
      display: 'inline-flex', gap: 2, padding: 3, borderRadius: 999,
      background: 'rgba(255,255,255,0.12)', border: '1px solid rgba(255,255,255,0.18)',
    }}>
      {OPTS.map(({ m, icon, label }) => {
        const active = mode === m;
        return (
          <button key={m} type="button" title={`${label} theme`} aria-pressed={active} onClick={() => choose(m)}
            style={{
              border: 'none', cursor: 'pointer', borderRadius: 999, padding: '4px 8px', fontSize: 13, lineHeight: 1,
              background: active ? '#fff' : 'transparent', color: active ? '#0052cc' : 'rgba(255,255,255,0.85)',
            }}>{icon}</button>
        );
      })}
    </div>
  );
};
