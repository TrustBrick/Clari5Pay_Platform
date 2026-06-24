import type { NavItem, User, UserRole } from '../types';

// Blog Management submenu group — shared across roles. The sidebar hides the
// adminOnly children (Create / Draft / Analytics) for read-only roles.
export const BLOG_GROUP: NavItem = {
  key: 'blog', icon: '📝', label: 'Blog Management',
  children: [
    { key: 'blog-all', icon: '▦', label: 'All Blogs' },
    { key: 'blog-create', icon: '✚', label: 'Create Blog', adminOnly: true },
    { key: 'blog-categories', icon: '🏷', label: 'Categories' },
    { key: 'blog-published', icon: '✓', label: 'Published Blogs' },
    { key: 'blog-drafts', icon: '🗒', label: 'Draft Blogs', adminOnly: true },
    { key: 'blog-analytics', icon: '📊', label: 'Blog Analytics', adminOnly: true },
  ],
};

export const NAV: Record<UserRole, NavItem[]> = {
  MERCHANT: [
    { key: 'dashboard', icon: '⬡', label: 'Dashboard' },
    { key: 'deposit', icon: '↓', label: 'Deposit Management' },
    { key: 'withdrawal', icon: '↑', label: 'Withdrawal Management' },
    { key: 'settlement', icon: '⇄', label: 'Settlement Management' },
    { key: 'cancel', icon: '⊘', label: 'Cancel Request' },
    { key: 'transactions', icon: '≡', label: 'Transactions' },
    { key: 'reports', icon: '📊', label: 'Reports' },
    { key: 'risk-mgmt', icon: '🛡️', label: 'Risk Management' },
    { key: 'templates', icon: '▦', label: 'All Templates View' },
    { key: 'balance', icon: '◎', label: 'Balance Enquiry' },
    { key: 'risk', icon: '⚑', label: 'Risk Analysis' },
    { key: 'news', icon: '📰', label: 'News' },
    BLOG_GROUP,
    { key: 'support', icon: '💬', label: 'Customer Support' },
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
  ADMIN: [
    { key: 'admin-dashboard', icon: '⬡', label: 'Dashboard' },
    { key: 'admin-merchants', icon: '▤', label: 'Merchants' },
    { key: 'admin-analytics', icon: '📊', label: 'Merchant Analytics' },
    { key: 'admin-transactions', icon: '≡', label: 'All Transactions' },
    { key: 'admin-accounts', icon: '🏦', label: 'Account Management' },
    { key: 'risk-mgmt', icon: '🛡️', label: 'Risk Management' },
    { key: 'complaints', icon: '🚨', label: 'Complaint Management' },
    BLOG_GROUP,
    { key: 'profile', icon: '◉', label: 'Profile' },
  ],
  SUPER_ADMIN: [
    { key: 'sa-dashboard', icon: '⬡', label: 'Platform Overview' },
    { key: 'sa-admins', icon: '🛡', label: 'Admin Management' },
    { key: 'sa-analytics', icon: '📊', label: 'Merchant Analytics' },
    { key: 'risk-mgmt', icon: '🛡️', label: 'Risk Management' },
    { key: 'complaints', icon: '🚨', label: 'Complaint Management' },
    { key: 'sa-news', icon: '📰', label: 'News Management' },
    { key: 'sa-logs', icon: '🧾', label: 'System Logs' },
    { key: 'sa-audit', icon: '📋', label: 'Audit Logs' },
    BLOG_GROUP,
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
  cancel: 'Cancel Request',
  transactions: 'Transactions',
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
  'admin-analytics': 'Merchant Analytics',
  'admin-transactions': 'All Transactions',
  'admin-accounts': 'Account Management',
  'sa-dashboard': 'Platform Overview',
  'sa-admins': 'Admin Management',
  'sa-analytics': 'Merchant Analytics',
  'sa-news': 'News Management',
  'sa-logs': 'System Logs',
  'sa-audit': 'Audit Logs',
  'blog-all': 'All Blogs',
  'blog-create': 'Create Blog',
  'blog-categories': 'Blog Categories',
  'blog-published': 'Published Blogs',
  'blog-drafts': 'Draft Blogs',
  'blog-analytics': 'Blog Analytics',
  'blog-details': 'Blog Details',
};

// Sidebar pages permitted per merchant role (drives the dynamic sidebar).
// Maintain Profile + Profile collapse to a single Profile link.
// Customer Support is available to every merchant role (default for all merchants).
export const MERCHANT_ROLE_NAV: Record<string, string[]> = {
  DEO: ['dashboard', 'deposit', 'withdrawal', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'blog', 'support', 'profile'],
  DEPOSIT_OPERATOR: ['dashboard', 'deposit', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'blog', 'support', 'profile'],
  WITHDRAWAL_OPERATOR: ['dashboard', 'withdrawal', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'blog', 'support', 'profile'],
  SUPERVISOR: ['dashboard', 'settlement', 'cancel', 'transactions', 'reports', 'risk-mgmt', 'news', 'blog', 'support', 'profile'],
  MANAGER: ['dashboard', 'templates', 'reports', 'risk-mgmt', 'news', 'blog', 'support', 'profile'],
};

/**
 * Resolve the sidebar items for a user. Merchants with a known role see only
 * the pages permitted for that role; everyone else (incl. role-less merchants)
 * gets the full nav for their role.
 */
export const navForUser = (user: User): NavItem[] => {
  const base = NAV[user.role] || [];
  if (user.role !== 'MERCHANT') return base;
  const role = user.merchantRole ? String(user.merchantRole).toUpperCase() : '';
  const allowed = MERCHANT_ROLE_NAV[role];
  if (!allowed) return base;
  const byKey = new Map(base.map((i) => [i.key, i]));
  return allowed.map((k) => byKey.get(k)).filter((i): i is NavItem => Boolean(i));
};
