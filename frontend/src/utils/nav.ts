import type { NavItem, UserRole } from '../types';

export const NAV: Record<UserRole, NavItem[]> = {
  MERCHANT: [
    { key: 'dashboard', icon: '⬡', label: 'Dashboard' },
    { key: 'deposit', icon: '↓', label: 'Deposit Management' },
    { key: 'withdrawal', icon: '↑', label: 'Withdrawal Management' },
    { key: 'settlement', icon: '⇄', label: 'Settlement Management' },
    { key: 'transactions', icon: '≡', label: 'Transactions' },
    { key: 'balance', icon: '◎', label: 'Balance Enquiry' },
    { key: 'risk', icon: '⚑', label: 'Risk Analysis' },
    { key: 'support', icon: '💬', label: 'Customer Support' },
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
  ADMIN: [
    { key: 'admin-dashboard', icon: '⬡', label: 'Dashboard' },
    { key: 'admin-merchants', icon: '▤', label: 'Merchants' },
    { key: 'admin-transactions', icon: '≡', label: 'All Transactions' },
    { key: 'admin-accounts', icon: '🏦', label: 'Account Management' },
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
  SUPER_ADMIN: [
    { key: 'sa-dashboard', icon: '⬡', label: 'Platform Overview' },
    { key: 'sa-admins', icon: '🛡', label: 'Admin Management' },
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
  // Support agents use the separate Customer Support portal.
  SUPPORT_AGENT: [
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
};

export const PAGE_TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  deposit: 'Deposit Management',
  withdrawal: 'Withdrawal Management',
  settlement: 'Settlement Management',
  transactions: 'Transactions',
  balance: 'Balance Enquiry',
  risk: 'Risk Analysis',
  support: 'Customer Support',
  profile: 'Profile',
  'admin-dashboard': 'Dashboard',
  'admin-merchants': 'Merchants',
  'admin-transactions': 'All Transactions',
  'admin-accounts': 'Account Management',
  'sa-dashboard': 'Platform Overview',
  'sa-admins': 'Admin Management',
};
