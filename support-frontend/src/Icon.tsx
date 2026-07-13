// ─── Icon system (Customer Support portal) ─────────────────────────────────────
// Mirrors the main frontend's approach: one Phosphor family behind a small semantic
// registry. <Icon name="..."/> defaults to 18px outline (regular), inheriting colour.
// Components that take a string `icon` render <Icon> when the value is a registered
// name (see isIconName) and fall back to the raw string otherwise.
import React from 'react';
import type { Icon as PhIcon } from '@phosphor-icons/react';
import {
  Warning, Info, MagnifyingGlass, HourglassMedium, Paperclip, PaperPlaneTilt, X,
  DownloadSimple, File, Sun, Moon, Monitor,
} from '@phosphor-icons/react';

const REGISTRY = {
  warning: Warning,
  info: Info,
  search: MagnifyingGlass,
  pending: HourglassMedium,
  attach: Paperclip,
  send: PaperPlaneTilt,
  close: X,
  download: DownloadSimple,
  file: File,
  // Theme toggle
  light: Sun,
  dark: Moon,
  system: Monitor,
} satisfies Record<string, PhIcon>;

export type IconName = keyof typeof REGISTRY;

export const isIconName = (s: unknown): s is IconName =>
  typeof s === 'string' && Object.prototype.hasOwnProperty.call(REGISTRY, s);

export interface IconProps {
  name: IconName;
  size?: number;
  weight?: 'thin' | 'light' | 'regular' | 'bold' | 'fill' | 'duotone';
  color?: string;
  style?: React.CSSProperties;
}

export const Icon: React.FC<IconProps> = ({ name, size = 18, weight = 'regular', color = 'currentColor', style }) => {
  const Cmp = REGISTRY[name];
  if (!Cmp) return null;
  return <Cmp size={size} weight={weight} color={color} style={{ flexShrink: 0, verticalAlign: 'middle', ...style }} />;
};

export default Icon;
