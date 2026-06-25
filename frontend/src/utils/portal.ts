// Which portal this build is. Set at build time via VITE_PORTAL; the same codebase is
// built once per portal (merchant / admin / superadmin) plus an 'app' chooser landing.
export type Portal = 'merchant' | 'admin' | 'superadmin' | 'app';

export const PORTAL: Portal = ((import.meta.env.VITE_PORTAL as Portal) || 'app');

// The single role each role-portal admits (undefined = no restriction, e.g. the chooser).
export const PORTAL_ROLE: Partial<Record<Portal, string>> = {
  merchant: 'MERCHANT',
  admin: 'ADMIN',
  superadmin: 'SUPER_ADMIN',
};

export const PORTAL_NAME: Record<Portal, string> = {
  merchant: 'Merchant Portal',
  admin: 'Admin Portal',
  superadmin: 'Super Admin Portal',
  app: 'Clari5Pay',
};

// Where an account of each role should sign in.
export const ROLE_PORTAL_URL: Record<string, string> = {
  MERCHANT: 'https://win365jackpot.com',
  ADMIN: 'https://admin.win365jackpot.com',
  SUPER_ADMIN: 'https://sa.win365jackpot.com',
};

// Cards shown on the app.win365jackpot.com chooser landing.
export const PORTAL_LINKS = [
  { name: 'Merchant Portal', url: 'https://win365jackpot.com', icon: '🏪', desc: 'Deposits, withdrawals & settlements' },
  { name: 'Admin Portal', url: 'https://admin.win365jackpot.com', icon: '🛡️', desc: 'Process requests & manage merchants' },
  { name: 'Super Admin Portal', url: 'https://sa.win365jackpot.com', icon: '👑', desc: 'Platform oversight & admin accounts' },
];

// True if a user with this role may use the current portal.
export function roleAllowedHere(role: string): boolean {
  const need = PORTAL_ROLE[PORTAL];
  return !need || role === need;
}
