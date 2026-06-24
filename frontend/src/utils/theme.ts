// All UI colors are CSS variables (defined in index.css for light + dark). Components
// use these tokens as inline styles, so flipping `data-theme` on <html> re-themes the
// whole app instantly with no React re-render. Brand gradients + status/risk colors are
// the same in both themes. For charts (recharts), which render colors as SVG attributes
// that don't resolve var(), use the concrete palette from utils/themeMode → useTheme().
export const T = {
  blue: 'var(--c5-blue)',
  green: 'var(--c5-green)',
  dark: 'var(--c5-dark)',
  cyan: 'var(--c5-cyan)',
  blueDark: 'var(--c5-blue-dark)',
  greenDark: 'var(--c5-green-dark)',
  surface: 'var(--c5-surface)',
  canvas: 'var(--c5-canvas)',
  sidebar: 'var(--c5-sidebar)',
  sidebarHover: 'var(--c5-sidebar-hover)',
  sidebarActive: 'var(--c5-sidebar-active)',
  textMain: 'var(--c5-text-main)',
  textMuted: 'var(--c5-text-muted)',
  textLight: 'var(--c5-text-light)',
  border: 'var(--c5-border)',
  borderLight: 'var(--c5-border-light)',
  success: 'var(--c5-success)',
  successBg: 'var(--c5-success-bg)',
  warning: 'var(--c5-warning)',
  warningBg: 'var(--c5-warning-bg)',
  info: 'var(--c5-info)',
  infoBg: 'var(--c5-info-bg)',
  danger: 'var(--c5-danger)',
  dangerBg: 'var(--c5-danger-bg)',
  grad1: 'linear-gradient(135deg,#0052cc,#00a3ff)',
  grad2: 'linear-gradient(135deg,#26d00c,#0052cc)',
  grad3: 'linear-gradient(135deg,#0a2540,#0052cc)',
} as const;

export type ThemeKey = keyof typeof T;
