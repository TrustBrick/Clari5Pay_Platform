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
import { AgentDashboardPage, AgentsPage, AgentAccountsPage, AgentTransactionsPage, UnassignedTransactionsPage, AgentAuditPage, AgentReportsPage } from './pages/AgentPages';
import { AgentOverviewPage, AgentDepositRequestPage, AgentWithdrawalRequestPage } from './pages/AgentTxnPages';
import { RiskManagementPage } from './pages/RiskPages';
import { ComplaintManagementPage } from './pages/ComplaintPages';
import { ActiveUsersPage } from './pages/ActiveUsersPage';
import { SupportManagementPage } from './pages/SupportManagementPage';
import { usePoll } from './utils/usePoll';
import { activeUsersAPI } from './services/api';

const defaultPageFor = (role?: string) =>
  role === 'MERCHANT' ? 'dashboard' : role === 'ADMIN' ? 'admin-dashboard' : 'sa-dashboard';

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
  // KYC Update is restricted to the Supervisor and Manager merchant roles.
  if (page === 'kyc') return ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
  // Agent Management — Supervisor & Manager only, and Demo-gated until the module is complete
  // (mirrors the nav.ts demo gate).
  if (['agent-dashboard', 'agents', 'agent-accounts', 'agent-transactions', 'agent-unassigned', 'agent-audit', 'agent-reports'].includes(page))
    return IS_DEMO && ['SUPERVISOR', 'MANAGER'].includes(String(user.merchantRole || '').toUpperCase());
  // Isolated Agent Transaction subsystem (operator workflow) — demo-gated. Agent Overview is open
  // to every agent role; Agent Deposit Request excludes the Withdrawal Operator.
  if (page === 'agent-overview')
    return IS_DEMO && ['SUPERVISOR', 'MANAGER', 'DEO', 'DEPOSIT_OPERATOR', 'WITHDRAWAL_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
  if (page === 'agent-deposit-req')
    return IS_DEMO && ['SUPERVISOR', 'MANAGER', 'DEO', 'DEPOSIT_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
  if (page === 'agent-withdrawal-req')
    return IS_DEMO && ['SUPERVISOR', 'MANAGER', 'DEO', 'WITHDRAWAL_OPERATOR'].includes(String(user.merchantRole || '').toUpperCase());
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
  const [page, setPage] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Reset to the role's dashboard whenever the logged-in user changes (login / account switch).
  const prevUserId = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (!user) { prevUserId.current = null; return; }
    if (user.id !== prevUserId.current) {
      prevUserId.current = user.id;
      setPage(defaultPageFor(user.role));
    }
  }, [user]);

  // Presence heartbeat — keeps the logged-in user marked Online for the Active Users view.
  // Best-effort; fires on interval + focus/visibility like the rest of the app.
  usePoll(() => { if (user) activeUsersAPI.heartbeat(); }, 25000);

  // app.win365jackpot.com is just a chooser that routes users to their dedicated portal.
  if (PORTAL === 'app') return (<><DemoBanner /><PortalChooser /></>);
  if (!user) return (<><DemoBanner /><LoginPage /></>);

  // Effective page: fall back to the role default if the current page isn't valid for this role
  // (e.g. a stale page left over from a previous session/role).
  const activePage = pageAllowed(user, page) ? page : defaultPageFor(user.role);

  const renderPage = () => {
    const props = { user, onNavigate: setPage };
    const map: Record<string, React.ReactNode> = {
      dashboard: <MerchantDashboard {...props} />,
      deposit: <DepositManagement {...props} />,
      withdrawal: <WithdrawalManagement {...props} />,
      settlement: <SettlementManagement {...props} />,
      approvals: <ApprovalsPage user={user} />,
      cancel: <CancelRequestPage {...props} />,
      transactions: <TransactionHistory {...props} />,
      kyc: <KYCPage user={user} />,
      // Agent Management — demo-gated (see pageAllowed); keys are inert on Production builds.
      ...(IS_DEMO ? {
        'agent-dashboard': <AgentDashboardPage {...props} />,
        agents: <AgentsPage {...props} />,
        'agent-accounts': <AgentAccountsPage {...props} />,
        'agent-transactions': <AgentTransactionsPage {...props} />,
        'agent-unassigned': <UnassignedTransactionsPage {...props} />,
        'agent-audit': <AgentAuditPage {...props} />,
        'agent-reports': <AgentReportsPage {...props} />,
        // Isolated Agent Transaction subsystem (Phase 2).
        'agent-overview': <AgentOverviewPage {...props} />,
        'agent-deposit-req': <AgentDepositRequestPage {...props} />,
        'agent-withdrawal-req': <AgentWithdrawalRequestPage {...props} />,
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
      'sa-dashboard': <SaDashboard onNavigate={setPage} />,
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
        onNav={(key) => { setPage(key); setSidebarOpen(false); }}
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
