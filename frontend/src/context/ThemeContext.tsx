import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import {
  type ThemeMode, type ResolvedTheme,
  getStoredMode, resolveMode, applyTheme, storeMode, chartPalette,
} from '../utils/themeMode';

interface ThemeCtx {
  mode: ThemeMode;                 // user's choice: light | dark | system
  resolved: ResolvedTheme;         // what's actually applied right now
  setMode: (m: ThemeMode) => void;
  chart: ReturnType<typeof chartPalette>;
}

const Ctx = createContext<ThemeCtx | null>(null);

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [mode, setModeState] = useState<ThemeMode>(getStoredMode);
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolveMode(getStoredMode()));

  const setMode = useCallback((m: ThemeMode) => {
    setModeState(m);
    storeMode(m);
    setResolved(applyTheme(m, true));   // animate the switch
  }, []);

  // When in "system" mode, follow OS light/dark changes live.
  useEffect(() => {
    if (mode !== 'system' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => setResolved(applyTheme('system', true));
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [mode]);

  return (
    <Ctx.Provider value={{ mode, resolved, setMode, chart: chartPalette(resolved) }}>
      {children}
    </Ctx.Provider>
  );
};

export const useTheme = (): ThemeCtx => {
  const v = useContext(Ctx);
  if (!v) throw new Error('useTheme must be used within ThemeProvider');
  return v;
};
