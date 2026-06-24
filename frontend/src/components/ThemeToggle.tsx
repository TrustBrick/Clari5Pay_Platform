import React from 'react';
import { Sun, Moon, Monitor } from 'lucide-react';
import { T } from '../utils/theme';
import { useTheme } from '../context/ThemeContext';
import type { ThemeMode } from '../utils/themeMode';

const OPTS: { mode: ThemeMode; label: string; Icon: typeof Sun }[] = [
  { mode: 'light', label: 'Light', Icon: Sun },
  { mode: 'dark', label: 'Dark', Icon: Moon },
  { mode: 'system', label: 'System', Icon: Monitor },
];

// 3-way segmented Light / Dark / System control. `compact` shows icons only (header/mobile).
export const ThemeToggle: React.FC<{ compact?: boolean }> = ({ compact }) => {
  const { mode, setMode } = useTheme();
  return (
    <div role="group" aria-label="Theme" style={{
      display: 'inline-flex', gap: 2, padding: 3,
      background: T.canvas, border: `1px solid ${T.border}`, borderRadius: 999,
    }}>
      {OPTS.map(({ mode: m, label, Icon }) => {
        const active = mode === m;
        return (
          <button key={m} type="button" onClick={() => setMode(m)} title={`${label} theme`} aria-pressed={active}
            className="c5-btn" style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: compact ? '6px' : '6px 10px', borderRadius: 999, border: 'none', cursor: 'pointer',
              fontSize: 12, fontWeight: 700, fontFamily: 'inherit', lineHeight: 1,
              background: active ? T.surface : 'transparent',
              color: active ? T.blue : T.textMuted,
              boxShadow: active ? '0 1px 4px rgba(0,0,0,0.15)' : 'none',
            }}>
            <Icon size={15} />{!compact && <span>{label}</span>}
          </button>
        );
      })}
    </div>
  );
};

export default ThemeToggle;
