// ─── Central Icon system ───────────────────────────────────────────────────────
// One professional icon family (Phosphor) across the whole platform. Every UI concept
// maps to a single, unique semantic name here — this registry is the single source of
// truth that keeps icons consistent (size / stroke / family) and unique per feature.
//
// Usage:  <Icon name="dashboard" />              → outline (regular weight)
//         <Icon name="dashboard" weight="fill" /> → filled (active / selected states)
//
// Legacy call sites still pass emoji strings; components fall back to rendering the raw
// string when it is not a registered name (see `isIconName`), so the migration is safe
// and incremental — nothing breaks before a surface is converted.
import React from 'react';
import type { Icon as PhIcon } from '@phosphor-icons/react';
import {
  Gauge, Storefront, UsersThree, Headset, ChartLineUp, FileText, Receipt, Bank,
  ShieldWarning, Megaphone, TelegramLogo, Newspaper, UserCircle, Gear, SignOut,
  ShieldCheck, Scroll, ClipboardText, Flask, Buildings, ArrowCircleDown, ArrowCircleUp,
  ArrowsLeftRight, CheckCircle, XCircle, Table, Wallet, WarningDiamond, IdentificationCard,
  HandDeposit, HandWithdraw, ArrowsClockwise, Coins, HourglassMedium, SealCheck, Percent,
  ChartBar, WarningOctagon, CalendarCheck, PlusCircle, Info, Users, Briefcase, CurrencyInr,
  Pulse, GlobeHemisphereEast, Envelope, Phone, Barcode, Vault, UserPlus, Handshake,
  Eye, PencilSimple, TrashSimple, Check, X, UserSwitch, ClockCounterClockwise,
  FileMagnifyingGlass, DownloadSimple, UploadSimple, Copy, ArrowClockwise, FunnelSimple,
  MagnifyingGlass, ArrowsDownUp, Lock, At, CalendarBlank, Clock, Hash, IdentificationBadge,
  AirplaneTilt, CreditCard, Fingerprint, Scan, ImageSquare, ListChecks, Robot, Plugs,
  PlugsConnected, Plug, AddressBook, Bell, Notebook, PaperPlaneTilt, ChatCenteredDots,
  FolderOpen, UserFocus, SpinnerGap, Checks, Archive, ArrowFatLinesUp, Flag, FilePdf,
  FileXls, FileCsv, Printer, Export, ChartPieSlice, Prohibit, CheckFat, Money, TrendUp,
  TrendDown, MapPin, ArrowRight, CaretRight, Warning, WarningCircle, Trophy, ListBullets,
  Tray, DotsThreeOutline, Sliders, Star, House, Key, Link, Broadcast, Wrench,
  WifiHigh, WifiSlash, SignIn, UserList, Recycle, Lightning, Crown, SealWarning,
  PhoneCall, Coffee, ChatCircleDots, Queue, Timer, File, Paperclip, QrCode, CurrencyBtc,
  Tag, FolderSimple, Circle, FloppyDisk, ShareNetwork, WhatsappLogo, Plus, Brain, Lightbulb,
  EyeSlash, Heart, List,
} from '@phosphor-icons/react';

// Semantic name → Phosphor component. Grouped by the spec's modules for reviewability.
const REGISTRY = {
  // ── Sidebar / modules ──────────────────────────────────────────────────────
  dashboard: Gauge,
  merchants: Storefront,
  'active-users': UsersThree,
  support: Headset,
  'merchant-analytics': ChartLineUp,
  reports: FileText,
  transactions: Receipt,
  'account-management': Bank,
  'risk-management': ShieldWarning,
  complaints: Megaphone,
  telegram: TelegramLogo,
  news: Newspaper,
  profile: UserCircle,
  settings: Gear,
  signout: SignOut,
  'admin-management': ShieldCheck,
  'system-logs': Scroll,
  'audit-logs': ClipboardText,
  'demo-tools': Flask,
  'platform-overview': Buildings,

  // ── Merchant operations ────────────────────────────────────────────────────
  deposit: ArrowCircleDown,
  withdrawal: ArrowCircleUp,
  settlement: ArrowsLeftRight,
  approvals: CheckCircle,
  cancel: Prohibit,
  templates: Table,
  balance: Wallet,
  'risk-analysis': WarningDiamond,
  kyc: IdentificationCard,

  // ── Dashboard KPI cards ─────────────────────────────────────────────────────
  'total-deposits': HandDeposit,
  'total-withdrawals': HandWithdraw,
  'total-settlements': ArrowsClockwise,
  'available-balance': Coins,
  'pending-requests': HourglassMedium,
  'completed-requests': CheckFat,
  commission: Percent,
  volume: ChartBar,
  'risk-alerts': WarningOctagon,
  'today-transactions': CalendarCheck,

  // ── Merchant module fields ──────────────────────────────────────────────────
  'create-merchant': PlusCircle,
  'merchant-details': Info,
  users: Users,
  business: Briefcase,
  fees: Money,
  status: Pulse,
  country: GlobeHemisphereEast,
  email: Envelope,
  phone: Phone,
  'reference-codes': Barcode,

  // ── Account management ──────────────────────────────────────────────────────
  'add-account': Vault,
  bank: Bank,
  branch: MapPin,
  'account-number': Hash,
  ifsc: Buildings,
  upi: At,
  'highest-credit': TrendUp,
  'highest-debit': TrendDown,
  'deposits-received': ArrowCircleDown,
  'view-details': Eye,

  // ── Agent management ────────────────────────────────────────────────────────
  agent: UserCircle,
  'agent-ledger': Notebook,
  'assign-agent': UserSwitch,
  'assignment-history': ClockCounterClockwise,

  // ── Risk management ─────────────────────────────────────────────────────────
  'risk-report': WarningDiamond,
  'risk-intelligence': ChartPieSlice,
  'low-risk': CheckCircle,
  'medium-risk': Warning,
  'high-risk': WarningOctagon,
  fraud: Prohibit,
  behaviour: Pulse,
  alerts: Bell,
  monitoring: Broadcast,

  // ── KYC verification ────────────────────────────────────────────────────────
  aadhaar: Fingerprint,
  pan: CreditCard,
  passport: AirplaneTilt,
  'ocr-upload': Scan,
  'upload-image': ImageSquare,
  'verification-history': ListChecks,
  verified: SealCheck,
  pending: HourglassMedium,
  rejected: XCircle,
  'document-preview': FileMagnifyingGlass,

  // ── Telegram management ─────────────────────────────────────────────────────
  'telegram-bot': Robot,
  webhook: Plugs,
  connected: PlugsConnected,
  disconnected: Plug,
  'linked-users': AddressBook,
  notifications: Bell,
  'delivery-logs': ListBullets,
  'send-test': PaperPlaneTilt,

  // ── Complaint management ────────────────────────────────────────────────────
  complaint: ChatCenteredDots,
  open: FolderOpen,
  assigned: UserFocus,
  'in-progress': SpinnerGap,
  resolved: Checks,
  closed: Archive,
  escalated: ArrowFatLinesUp,
  priority: Flag,

  // ── Reports / export ────────────────────────────────────────────────────────
  pdf: FilePdf,
  excel: FileXls,
  csv: FileCsv,
  print: Printer,
  download: DownloadSimple,
  export: Export,
  analytics: ChartPieSlice,
  treasury: Vault,
  ledger: Notebook,

  // ── Table / row actions ─────────────────────────────────────────────────────
  view: Eye,
  edit: PencilSimple,
  delete: TrashSimple,
  approve: Check,
  reject: X,
  assign: UserPlus,
  history: ClockCounterClockwise,
  audit: FileMagnifyingGlass,
  upload: UploadSimple,
  copy: Copy,
  refresh: ArrowClockwise,
  filter: FunnelSimple,
  search: MagnifyingGlass,
  sort: ArrowsDownUp,

  // ── Form fields ─────────────────────────────────────────────────────────────
  user: UserCircle,
  password: Lock,
  amount: CurrencyInr,
  date: CalendarBlank,
  time: Clock,
  'reference-number': Hash,
  'membership-id': IdentificationBadge,

  // ── Notifications / toasts ──────────────────────────────────────────────────
  success: CheckCircle,
  warning: Warning,
  information: Info,
  error: WarningCircle,
  security: ShieldCheck,
  login: Key,
  logout: SignOut,
  'password-changed': Lock,
  'transaction-approved': CheckCircle,
  'transaction-rejected': XCircle,
  'settlement-completed': Handshake,

  // ── KPI extras (dashboards) ─────────────────────────────────────────────────
  online: WifiHigh,
  offline: WifiSlash,
  'logged-in': SignIn,
  'registered-users': UserList,
  reassignments: Recycle,
  velocity: Lightning,
  largest: Crown,
  'critical-risk': SealWarning,
  available: CheckCircle,
  busy: PhoneCall,
  'on-break': Coffee,
  chat: ChatCircleDots,
  queue: Queue,
  'response-time': Timer,

  // ── Empty states ────────────────────────────────────────────────────────────
  'empty-generic': Tray,
  'no-data': Tray,

  // ── Misc / generic ──────────────────────────────────────────────────────────
  home: House,
  close: X,
  crown: Crown,
  lock: Lock,
  file: File,
  attach: Paperclip,
  send: PaperPlaneTilt,
  qr: QrCode,
  crypto: CurrencyBtc,
  tag: Tag,
  folder: FolderSimple,
  dot: Circle,
  add: Plus,
  brain: Brain,
  insight: Lightbulb,
  'eye-off': EyeSlash,
  likes: Heart,
  menu: List,
  save: FloppyDisk,
  share: ShareNetwork,
  whatsapp: WhatsappLogo,
  chevron: CaretRight,
  more: DotsThreeOutline,
  adjust: Sliders,
  star: Star,
  trophy: Trophy,
  link: Link,
  bell: Bell,
  tune: Wrench,
  arrow: ArrowRight,
} satisfies Record<string, PhIcon>;

export type IconName = keyof typeof REGISTRY;

export const isIconName = (s: unknown): s is IconName =>
  typeof s === 'string' && Object.prototype.hasOwnProperty.call(REGISTRY, s);

export type IconWeight = 'thin' | 'light' | 'regular' | 'bold' | 'fill' | 'duotone';

export interface IconProps {
  name: IconName;
  size?: number;
  weight?: IconWeight;
  color?: string;
  style?: React.CSSProperties;
  className?: string;
  mirrored?: boolean;
}

// The one component every surface renders. Defaults give the consistent house style:
// 18px, regular (outline) weight, inheriting the surrounding text colour.
export const Icon: React.FC<IconProps> = ({
  name, size = 18, weight = 'regular', color = 'currentColor', style, className, mirrored,
}) => {
  const Cmp = REGISTRY[name];
  if (!Cmp) return null;
  return (
    <Cmp
      size={size}
      weight={weight}
      color={color}
      mirrored={mirrored}
      className={className}
      // Keep glyphs optically centred on the text baseline wherever they sit inline.
      style={{ flexShrink: 0, verticalAlign: 'middle', ...style }}
    />
  );
};

export default Icon;
