import type { NavItem, User, UserRole } from '../types';
import { IS_DEMO } from './portal';

// The Blog module has been merged into News — there is no separate Blog menu.

export const NAV: Record<UserRole, NavItem[]> = {
  MERCHANT: [
    { key: 'dashboard', icon: 'dashboard', label: 'Dashboard' },
    { key: 'deposit', icon: 'deposit', label: 'Deposit Management' },
    { key: 'withdrawal', icon: 'withdrawal', label: 'Withdrawal Management' },
    { key: 'settlement', icon: 'settlement', label: 'Settlement Requests' },
    { key: 'approvals', icon: 'approvals', label: 'Approvals' },
    { key: 'cancel', icon: 'cancel', label: 'Cancel Request' },
    { key: 'transactions', icon: 'transactions', label: 'Transactions' },
    // Agent Management — Non-EPS agents (Supervisor & Manager only). Demo-gated until the
    // module is complete (see navForUser gate + App.tsx pageAllowed). Agents never log in.
    {
      key: 'agent-mgmt', icon: 'users', label: 'Agent Management',
      children: [
        { key: 'agent-dashboard', icon: 'dashboard', label: 'Dashboard' },
        { key: 'agents', icon: 'agent', label: 'Agents' },
        { key: 'agent-accounts', icon: 'bank', label: 'Agent Accounts' },
        { key: 'agent-transactions', icon: 'transactions', label: 'Transactions' },
        { key: 'agent-audit', icon: 'audit', label: 'Audit Trail' },
        { key: 'agent-reports', icon: 'reports', label: 'Reports' },
      ],
    },
    { key: 'kyc', icon: 'kyc', label: 'KYC Update' },
    { key: 'reports', icon: 'reports', label: 'Reports' },
    { key: 'risk-mgmt', icon: 'risk-management', label: 'Risk Management' },
    { key: 'templates', icon: 'templates', label: 'All Templates View' },
    { key: 'balance', icon: 'balance', label: 'Balance Enquiry' },
    { key: 'risk', icon: 'risk-analysis', label: 'Risk Analysis' },
    { key: 'news', icon: 'news', label: 'News' },
    { key: 'support', icon: 'support', label: 'Customer Support' },
    { key: 'profile', icon: 'profile', label: 'Profile' },
  ],
  ADMIN: [
    { key: 'admin-dashboard', icon: 'dashboard', label: 'Dashboard' },
    { key: 'admin-merchants', icon: 'merchants', label: 'Merchants' },
    { key: 'admin-active-users', icon: 'active-users', label: 'Active Users' },
    { key: 'admin-support', icon: 'support', label: 'Support Management' },
    { key: 'admin-analytics', icon: 'merchant-analytics', label: 'Merchant Analytics' },
    { key: 'admin-reports', icon: 'reports', label: 'Reports' },
    { key: 'admin-transactions', icon: 'transactions', label: 'All Transactions' },
    { key: 'admin-accounts', icon: 'account-management', label: 'Account Management' },
    { key: 'risk-mgmt', icon: 'risk-management', label: 'Risk Management' },
    { key: 'complaints', icon: 'complaints', label: 'Complaint Management' },
    { key: 'news', icon: 'news', label: 'News' },
    { key: 'admin-whatsapp', icon: 'telegram', label: 'Telegram Management' },
    { key: 'profile', icon: 'profile', label: 'Profile' },
  ],
  SUPER_ADMIN: [
    { key: 'sa-dashboard', icon: 'platform-overview', label: 'Platform Overview' },
    { key: 'sa-admins', icon: 'admin-management', label: 'Admin Management' },
    { key: 'sa-active-users', icon: 'active-users', label: 'Active Users' },
    { key: 'sa-support', icon: 'support', label: 'Support Management' },
    { key: 'sa-analytics', icon: 'merchant-analytics', label: 'Merchant Analytics' },
    { key: 'sa-reports', icon: 'reports', label: 'Reports' },
    { key: 'risk-mgmt', icon: 'risk-management', label: 'Risk Management' },
    { key: 'complaints', icon: 'complaints', label: 'Complaint Management' },
    { key: 'sa-news', icon: 'news', label: 'News Management' },
    { key: 'sa-logs', icon: 'system-logs', label: 'System Logs' },
    { key: 'sa-audit', icon: 'audit-logs', label: 'Audit Logs' },
    // Demo/UAT builds only (VITE_APP_ENV=demo) — never shown in Production.
    ...(IS_DEMO ? [{ key: 'sa-demo', icon: 'demo-tools', label: 'Demo Tools' }] : []),
    { key: 'profile', icon: 'profile', label: 'Profile' },
  ],
  // Support agents use the separate Customer Support portal.
  SUPPORT_AGENT: [
    { key: 'profile', icon: 'profile', label: 'Profile' },
  ],
};

export const PAGE_TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  deposit: 'Deposit Management',
  withdrawal: 'Withdrawal Management',
  settlement: 'Settlement Requests',
  approvals: 'Approvals',
  cancel: 'Cancel Request',
  transactions: 'Transactions',
  'agent-mgmt': 'Agent Management',
  'agent-dashboard': 'Agent Dashboard',
  agents: 'Agents',
  'agent-accounts': 'Agent Accounts',
  'agent-transactions': 'Agent Transactions',
  'agent-unassigned': 'Unassigned Transactions',
  'agent-audit': 'Agent Audit Trail',
  'agent-reports': 'Agent Reports',
  kyc: 'KYC Update',
  reports: 'Reports',
  'risk-mgmt': 'Risk Management',
  complaints: 'Complaint Management',
  templates: 'All Templates View',
  balance: 'Balance Enquiry',
  risk: 'Risk Analysis',
  news: 'News & Updates',
  support: 'Customer Support',
  profile: 'Profile',
  'admin-dashboard': 'Dashboard',
  'admin-merchants': 'Merchants',
  'admin-active-users': 'Active Users',
  'admin-support': 'Support Management',
  'admin-analytics': 'Merchant Analytics',
  'admin-reports': 'Reports',
  'admin-transactions': 'All Transactions',
  'admin-accounts': 'Account Management',
  'admin-whatsapp': 'Telegram Management',
  'sa-dashboard': 'Platform Overview',
  'sa-admins': 'Admin Management',
  'sa-active-users': 'Active Users',
  'sa-support': 'Support Management',
  'sa-analytics': 'Merchant Analytics',
  'sa-reports': 'Reports',
  'sa-news': 'News Management',
  'sa-logs': 'System Logs',
  'sa-audit': 'Audit Logs',
  'sa-demo': 'Demo Tools',
};

// Sidebar pages permitted per merchant role (drives the dynamic sidebar).
// Maintain Profile + Profile collapse to a single Profile link.
// Customer Support is available to every merchant role (default for all merchants).
export const MERCHANT_ROLE_NAV: Record<string, string[]> = {
  DEO: ['dashboard', 'deposit', 'withdrawal', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'support', 'profile'],
  DEPOSIT_OPERATOR: ['dashboard', 'deposit', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'support', 'profile'],
  WITHDRAWAL_OPERATOR: ['dashboard', 'withdrawal', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'support', 'profile'],
  SUPERVISOR: ['dashboard', 'approvals', 'settlement', 'transactions', 'agent-mgmt', 'kyc', 'reports', 'risk-mgmt', 'news', 'support', 'profile'],
  MANAGER: ['dashboard', 'approvals', 'transactions', 'agent-mgmt', 'templates', 'kyc', 'reports', 'risk-mgmt', 'news', 'support', 'profile'],
};

/**
 * Resolve the sidebar items for a user. Merchants with a known role see only
 * the pages permitted for that role; a role-less merchant gets the full merchant
 * nav EXCEPT Settlement Requests, which is a Supervisor-only page.
 */
export const navForUser = (user: User): NavItem[] => {
  const base = NAV[user.role] || [];
  if (user.role !== 'MERCHANT') return base;
  const role = user.merchantRole ? String(user.merchantRole).toUpperCase() : '';
  const allowed = MERCHANT_ROLE_NAV[role];
  // KYC Update and Agent Management are Demo-only for now — the KYC / DigiLocker integrations
  // are not configured on Production, and the Agent Management module is still being built out
  // across phases. Hide both menus on Production; flip these to config-driven flags once ready.
  // Applied to every merchant menu via this single gate.
  const demoOnly = new Set(['kyc', 'agent-mgmt']);
  const gate = (items: NavItem[]): NavItem[] => (IS_DEMO ? items : items.filter((i) => !demoOnly.has(i.key)));
  // Role-less merchant: full menu minus Settlement Requests (Supervisor-only), KYC Update and
  // Agent Management (both Supervisor/Manager-only).
  if (!allowed) return gate(base.filter((i) => i.key !== 'settlement' && i.key !== 'kyc' && i.key !== 'agent-mgmt'));
  const byKey = new Map(base.map((i) => [i.key, i]));
  return gate(allowed.map((k) => byKey.get(k)).filter((i): i is NavItem => Boolean(i)));
};
