import React, { useState } from 'react';
import { useAuth } from './context/AuthContext';
import { T } from './utils/theme';
import { PAGE_TITLES } from './utils/nav';
import Sidebar from './components/Sidebar';
import Header from './components/Header';
import SessionManager from './components/SessionManager';
import DemoBanner from './components/DemoBanner';
import LoginPage from './pages/LoginPage';
import PortalChooser from './pages/PortalChooser';
import { PORTAL, IS_DEMO } from './utils/portal';
import {
  MerchantDashboard, DepositManagement, WithdrawalManagement, SettlementManagement,
  TransactionHistory, BalancePage, RiskPage, MerchantSupportChat, ProfilePage,
  CancelRequestPage, TemplatesPage, NewsPage, ReportsPage, ApprovalsPage,
} from './pages/MerchantPages';
import { AdminReportsPage } from './pages/ReportsPage';
import {
  AdminDashboard, AdminMerchantsPage, AdminTransactionsPage, AdminAccountsPage,
  SaDashboard, SaAdminsPage, SystemLogsPage, AuditLogsPage,
  MerchantAnalyticsPage, WhatsAppSettingsPage, DemoToolsPage,
} from './pages/AdminPages';
import { KYCPage } from './pages/KYCPage';
import { AgentsPage, AgentAccountsPage, AgentTransactionsPage, UnassignedTransactionsPage, AgentAuditPage, AgentReportsPage } from './pages/AgentPages';
import { AgentDashboardPage, AgentOverviewPage, AgentDepositRequestPage, AgentWithdrawalRequestPage, AgentManageTransactionPage, AgentDepositManagementPage, AgentWithdrawalManagementPage, AgentSettlementManagementPage, AgentTxnReportsPage, AgentApprovalsPage, AgentAllTransactionsPage } from './pages/AgentTxnPages';
import { RiskManagementPage } from './pages/RiskPages';
import { ComplaintManagementPage } from './pages/ComplaintPages';
import { ActiveUsersPage } from './pages/ActiveUsersPage';
import { SupportManagementPage } from './pages/SupportManagementPage';
import { usePoll } from './utils/usePoll';
import { activeUsersAPI } from './services/api';

const defaultPageFor = (role?: string) =>
  role === 'MERCHANT' ? 'dashboard' : role === 'ADMIN' ? 'admin-dashboard' : 'sa-dashboard';

// ── Address bar <-> page key ──────────────────────────────────────────────────
// Navigation is state-based (no react-router). Mirroring the page key into the URL is what makes
// a refresh, Back/Forward and a pasted link all land on the page the user was actually on —
// without it the mount always falls back to the role's dashboard. Page keys are already URL-safe
// slugs ('agent-txn-reports'), so the mapping is 1:1 and no route table is introduced.
// nginx serves index.html for any unmatched path (try_files $uri $uri/ /index.html), so a deep
// link loads the SPA rather than 404ing.
const pageFromUrl = () => decodeURIComponent(window.location.pathname).replace(/^\/+|\/+$/g, '');
const urlForPage = (page: string) => `/${page}`;

// Direct-transaction pages an approval-only Manager must never reach.
const MANAGER_BLOCKED_PAGES = ['deposit', 'withdrawal', 'settlement'];

// Is `page` a valid destination for this user? (prevents showing another role's page, and
// blocks an approval-only Manager from the direct-transaction pages)
const pageAllowed = (user: { role: string; merchantRole?: string | null }, page: string) => {
  const role = user.role;
  if (!page) return false;
  if (page === 'profile') return true;
  if (page === 'risk-mgmt') return true;   // Risk Management is available in all three portals
  if (page === 'news') return true;        // News is viewable in all portals (SA also manages it)
  if (page === 'complaints') return role === 'ADMIN' || role === 'SUPER_ADMIN';
  if (role === 'SUPER_ADMIN') return page.startsWith('sa-');
  if (role === 'ADMIN') return page.startsWith('admin-');
  // MERCHANT — no admin/SA pages.
  if (page.startsWith('sa-') || page.startsWith('admin-')) return false;
  // KYC Update is restricted to the Data Operator, Supervisor and Manager merchant roles. The
  // Data Operator performs the verifications; Supervisor and Manager are read-only (see KYCPage).
  if (page === 'kyc') return ['DEO', 'SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
  // Agent Management — Supervisor & Manager only, and Demo-gated until the module is complete
  // (mirrors the nav.ts demo gate).
  if (['agents', 'agent-accounts', 'agent-transactions', 'agent-unassigned', 'agent-audit', 'agent-reports'].includes(page))
    return IS_DEMO && ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
  // Isolated Agent Transaction subsystem (operator workflow) — demo-gated. Agent Overview is open
  // to every agent role.
  // Agent Overview and the isolated Agent Reports page are open to every agent role.
  // Agent Overview, the isolated Agent Reports and the full agent ledger are read-only views,
  // open to every agent role.
  if (['agent-dashboard', 'agent-overview', 'agent-txn-reports', 'agent-all-txns'].includes(page))
    return IS_DEMO && ['SUPERVISOR', 'MANAGER', 'DEO', 'DEPOSIT_OPERATOR', 'WITHDRAWAL_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
  // Agent Deposit/Withdrawal Management (and the request forms they embed) are operator-only:
  // Supervisors and Managers are approval-only for agent payments and never create/manage these.
  if (page === 'agent-deposit-mgmt' || page === 'agent-deposit-req')
    return IS_DEMO && ['DEO', 'DEPOSIT_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
  if (page === 'agent-withdrawal-mgmt' || page === 'agent-withdrawal-req')
    return IS_DEMO && ['DEO', 'WITHDRAWAL_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
  // Agent Settlement Management — Supervisor only.
  if (page === 'agent-settlement-mgmt')
    return IS_DEMO && String(user.merchantRole || '').toUpperCase() === 'SUPERVISOR';
  // Agent Approvals — the review gate: Supervisors review Deposits, Managers review Withdrawals.
  if (page === 'agent-approvals')
    return IS_DEMO && ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
  if (page === 'agent-manage')
    return IS_DEMO && ['SUPERVISOR', 'MANAGER', 'DEO'].includes(String(user.merchantRole || '').toUpperCase());
  // A Manager is an approval-only role — block direct Deposit/Withdrawal/Settlement creation.
  if (String(user.merchantRole || '').toUpperCase() === 'MANAGER' && MANAGER_BLOCKED_PAGES.includes(page)) return false;
  // A Supervisor manages only Settlement Requests — no Deposit/Withdrawal pages, even by
  // direct/stale access (they fall back to the Dashboard).
  if (String(user.merchantRole || '').toUpperCase() === 'SUPERVISOR' && (page === 'deposit' || page === 'withdrawal')) return false;
  // Settlement Requests is a Supervisor-only page — only Supervisors create settlements.
  if (page === 'settlement') return String(user.merchantRole || '').toUpperCase() === 'SUPERVISOR';
  return true;
};

const App: React.FC = () => {
  const { user, logout } = useAuth();
  // Seeded from the URL so a refresh or deep link starts on the right page instead of the
  // dashboard. Still just a page key — everything downstream is unchanged.
  const [page, setPage] = useState(pageFromUrl);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Bumped on every sidebar click, including a click on the page already shown (which changes no
  // state and so would otherwise leave that page's internal view untouched). KYC Management keys
  // off this to drop any open verification form and land back on its dashboard.
  const [navTick, setNavTick] = useState(0);

  // Navigate: set the page AND push a history entry, so Back/Forward walk the pages the user
  // actually visited. Guarded against pushing a duplicate entry for the page already shown.
  const navigate = React.useCallback((key: string) => {
    setPage(key);
    if (pageFromUrl() !== key) window.history.pushState({ page: key }, '', urlForPage(key));
  }, []);

  // Browser Back / Forward — adopt whatever page the restored history entry points at. The
  // pageAllowed gate below still applies, so Back can never reach a page the user may not see.
  React.useEffect(() => {
    const onPop = () => setPage(pageFromUrl());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  // On a fresh mount (refresh, deep link, or returning from login) keep the page the URL asks
  // for; only an actual account switch resets to that role's dashboard.
  const prevUserId = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (!user) { prevUserId.current = null; return; }
    if (user.id !== prevUserId.current) {
      const switched = prevUserId.current !== null;
      prevUserId.current = user.id;
      const wanted = pageFromUrl();
      setPage(switched || !pageAllowed(user, wanted) ? defaultPageFor(user.role) : wanted);
    }
  }, [user]);

  // Effective page: fall back to the role default if the current page isn't valid for this role
  // (a stale link, another role's page, or a page left over from a previous session).
  const activePage = user ? (pageAllowed(user, page) ? page : defaultPageFor(user.role)) : '';

  // Keep the address bar honest about what is actually on screen, so refreshing again is stable.
  // replaceState (not push) — correcting a bad or bare URL must not add a history entry the user
  // would have to Back through. Skipped while logged out so a deep link survives the login round
  // trip and the user returns to the page they were on.
  React.useEffect(() => {
    if (!user || PORTAL === 'app') return;
    if (pageFromUrl() !== activePage) {
      window.history.replaceState({ page: activePage }, '', urlForPage(activePage));
    }
  }, [user, activePage]);

  // Presence heartbeat — keeps the logged-in user marked Online for the Active Users view.
  // Best-effort; fires on interval + focus/visibility like the rest of the app.
  usePoll(() => { if (user) activeUsersAPI.heartbeat(); }, 25000);

  // app.win365jackpot.com is just a chooser that routes users to their dedicated portal.
  if (PORTAL === 'app') return (<><DemoBanner /><PortalChooser /></>);
  if (!user) return (<><DemoBanner /><LoginPage /></>);

  const renderPage = () => {
    const props = { user, onNavigate: navigate };
    const map: Record<string, React.ReactNode> = {
      dashboard: <MerchantDashboard {...props} />,
      deposit: <DepositManagement {...props} />,
      withdrawal: <WithdrawalManagement {...props} />,
      settlement: <SettlementManagement {...props} />,
      approvals: <ApprovalsPage user={user} />,
      cancel: <CancelRequestPage {...props} />,
      transactions: <TransactionHistory {...props} />,
      // Keyed by navTick so clicking "KYC Management" always remounts to the KYC dashboard,
      // closing whichever verification form happened to be open.
      kyc: <KYCPage key={navTick} user={user} />,
      // Agent Management — demo-gated (see pageAllowed); keys are inert on Production builds.
      ...(IS_DEMO ? {
        'agent-dashboard': <AgentDashboardPage {...props} />,
        agents: <AgentsPage {...props} />,
        'agent-accounts': <AgentAccountsPage {...props} />,
        'agent-transactions': <AgentTransactionsPage {...props} />,
        'agent-unassigned': <UnassignedTransactionsPage {...props} />,
        'agent-audit': <AgentAuditPage {...props} />,
        'agent-reports': <AgentReportsPage {...props} />,
        // Isolated Agent Transaction subsystem.
        'agent-overview': <AgentOverviewPage {...props} />,
        'agent-txn-reports': <AgentTxnReportsPage {...props} />,
        'agent-all-txns': <AgentAllTransactionsPage {...props} />,
        'agent-deposit-req': <AgentDepositRequestPage {...props} />,
        'agent-withdrawal-req': <AgentWithdrawalRequestPage {...props} />,
        'agent-deposit-mgmt': <AgentDepositManagementPage {...props} />,
        'agent-withdrawal-mgmt': <AgentWithdrawalManagementPage {...props} />,
        'agent-settlement-mgmt': <AgentSettlementManagementPage {...props} />,
        'agent-approvals': <AgentApprovalsPage {...props} />,
        'agent-manage': <AgentManageTransactionPage {...props} />,
      } : {}),
      reports: <ReportsPage {...props} />,
      'risk-mgmt': <RiskManagementPage user={user} />,
      complaints: <ComplaintManagementPage user={user} />,
      templates: <TemplatesPage {...props} />,
      balance: <BalancePage {...props} />,
      risk: <RiskPage {...props} />,
      news: <NewsPage {...props} />,
      support: <MerchantSupportChat {...props} />,
      profile: <ProfilePage {...props} />,
      'admin-dashboard': <AdminDashboard {...props} />,
      'admin-merchants': <AdminMerchantsPage />,
      'admin-active-users': <ActiveUsersPage user={user} />,
      'admin-support': <SupportManagementPage user={user} />,
      'admin-analytics': <MerchantAnalyticsPage />,
      'admin-reports': <AdminReportsPage {...props} />,
      'admin-transactions': <AdminTransactionsPage />,
      'admin-accounts': <AdminAccountsPage />,
      'admin-whatsapp': <WhatsAppSettingsPage />,
      'sa-dashboard': <SaDashboard onNavigate={navigate} />,
      'sa-active-users': <ActiveUsersPage user={user} />,
      'sa-support': <SupportManagementPage user={user} />,
      'sa-analytics': <MerchantAnalyticsPage />,
      'sa-reports': <AdminReportsPage {...props} />,
      'sa-admins': <SaAdminsPage />,
      'sa-news': <NewsPage {...props} />,
      'sa-logs': <SystemLogsPage />,
      'sa-audit': <AuditLogsPage />,
      ...(IS_DEMO ? { 'sa-demo': <DemoToolsPage /> } : {}),
    };
    return map[activePage] || map[defaultPageFor(user.role)];
  };

  return (
    <>
      <DemoBanner />
      {/* Inactivity session timeout (10 min) — active only while logged in, and only for
          Merchant-portal users (Merchant / Data-Deposit-Withdrawal Operators / Supervisor /
          Manager) and Support. Admin & Super Admin have NO auto-timeout: they stay signed in
          until they click Logout or their token is invalidated. */}
      {user.role !== 'ADMIN' && user.role !== 'SUPER_ADMIN' && <SessionManager />}
      <style>{`
        *{margin:0;padding:0;box-sizing:border-box;}
        body{font-family:'Inter','Segoe UI',system-ui,sans-serif;background:${T.canvas};}
        ::-webkit-scrollbar{width:5px;height:5px;}
        ::-webkit-scrollbar-thumb{background:rgba(0,0,0,0.15);border-radius:3px;}
        input::placeholder,select{font-family:inherit;}
        .sidebar{transform:translateX(0);}
        @media(max-width:860px){
          .sidebar{transform:translateX(-248px)!important;}
          .sidebar.open{transform:translateX(0)!important;}
          .main-header{left:0!important;}
          .main-content{margin-left:0!important;}
          .hamburger{display:flex!important;}
          .mob-close{display:flex!important;}
          .mob-overlay{display:block!important;}
        }
        @media(max-width:600px){.main-content{padding:14px!important;}}
        @keyframes pulse{from{opacity:0.6;transform:scale(1);}to{opacity:1;transform:scale(1.05);}}
      `}</style>

      <Sidebar
        user={user}
        active={activePage}
        onNav={(key) => { navigate(key); setNavTick((t) => t + 1); setSidebarOpen(false); }}
        onLogout={logout}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <Header
        user={user}
        title={PAGE_TITLES[activePage] || 'Dashboard'}
        onMenuClick={() => setSidebarOpen(o => !o)}
      />

      <main
        className="main-content"
        style={{ marginLeft: 248, marginTop: 'calc(60px + var(--demo-banner-h, 0px))', minHeight: 'calc(100vh - 60px)', background: T.canvas, padding: 24, boxSizing: 'border-box' }}
      >
        {renderPage()}
      </main>
    </>
  );
};

export default App;
