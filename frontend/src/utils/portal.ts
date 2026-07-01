// Which portal this build is. Set at build time via VITE_PORTAL; the same codebase is
// built once per portal (merchant / admin / superadmin) plus an 'app' chooser landing.
export type Portal = 'merchant' | 'admin' | 'superadmin' | 'app';

export const PORTAL: Portal = ((import.meta.env.VITE_PORTAL as Portal) || 'app');

// True for a Demo/UAT build (VITE_APP_ENV=demo — see Dockerfile). Drives the DemoBanner
// and the SA-only Demo Tools page; a Production build (unset) never sees either.
export const IS_DEMO = import.meta.env.VITE_APP_ENV === 'demo';

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

// Portal base URLs — overridable at build time (see Dockerfile ARGs) so the same
// codebase can be built for Demo/UAT against different subdomains. Defaults are
// byte-identical to the previous hardcoded Production values.
const MERCHANT_URL = import.meta.env.VITE_MERCHANT_URL || 'https://win365jackpot.com';
const ADMIN_URL = import.meta.env.VITE_ADMIN_URL || 'https://admin.win365jackpot.com';
const SA_URL = import.meta.env.VITE_SA_URL || 'https://sa.win365jackpot.com';

// Where an account of each role should sign in.
export const ROLE_PORTAL_URL: Record<string, string> = {
  MERCHANT: MERCHANT_URL,
  ADMIN: ADMIN_URL,
  SUPER_ADMIN: SA_URL,
};

// Cards shown on the app.win365jackpot.com chooser landing.
export const PORTAL_LINKS = [
  { name: 'Merchant Portal', url: MERCHANT_URL, icon: '🏪', desc: 'Deposits, withdrawals & settlements' },
  { name: 'Admin Portal', url: ADMIN_URL, icon: '🛡️', desc: 'Process requests & manage merchants' },
  { name: 'Super Admin Portal', url: SA_URL, icon: '👑', desc: 'Platform oversight & admin accounts' },
];

// True if a user with this role may use the current portal.
export function roleAllowedHere(role: string): boolean {
  const need = PORTAL_ROLE[PORTAL];
  return !need || role === need;
}
