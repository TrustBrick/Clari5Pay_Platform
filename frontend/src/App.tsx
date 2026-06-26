import React, { useState } from 'react';
import { useAuth } from './context/AuthContext';
import { T } from './utils/theme';
import { PAGE_TITLES } from './utils/nav';
import Sidebar from './components/Sidebar';
import Header from './components/Header';
import SessionManager from './components/SessionManager';
import LoginPage from './pages/LoginPage';
import PortalChooser from './pages/PortalChooser';
import { PORTAL } from './utils/portal';
import {
  MerchantDashboard, DepositManagement, WithdrawalManagement, SettlementManagement,
  TransactionHistory, BalancePage, RiskPage, MerchantSupportChat, ProfilePage,
  CancelRequestPage, TemplatesPage, NewsPage, ReportsPage, ApprovalsPage,
} from './pages/MerchantPages';
import {
  AdminDashboard, AdminMerchantsPage, AdminTransactionsPage, AdminAccountsPage,
  SaDashboard, SaAdminsPage, SystemLogsPage, AuditLogsPage,
  MerchantAnalyticsPage,
} from './pages/AdminPages';
import { RiskManagementPage } from './pages/RiskPages';
import { ComplaintManagementPage } from './pages/ComplaintPages';

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
  // A Manager is an approval-only role — block direct Deposit/Withdrawal/Settlement creation.
  if (String(user.merchantRole || '').toUpperCase() === 'MANAGER' && MANAGER_BLOCKED_PAGES.includes(page)) return false;
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

  // app.win365jackpot.com is just a chooser that routes users to their dedicated portal.
  if (PORTAL === 'app') return <PortalChooser />;
  if (!user) return <LoginPage />;

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
      'admin-analytics': <MerchantAnalyticsPage />,
      'admin-transactions': <AdminTransactionsPage />,
      'admin-accounts': <AdminAccountsPage />,
      'sa-dashboard': <SaDashboard />,
      'sa-analytics': <MerchantAnalyticsPage />,
      'sa-admins': <SaAdminsPage />,
      'sa-news': <NewsPage {...props} />,
      'sa-logs': <SystemLogsPage />,
      'sa-audit': <AuditLogsPage />,
    };
    return map[activePage] || map[defaultPageFor(user.role)];
  };

  return (
    <>
      {/* Inactivity session timeout (10 min) — active only while logged in. */}
      <SessionManager />
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
        style={{ marginLeft: 248, marginTop: 60, minHeight: 'calc(100vh - 60px)', background: T.canvas, padding: 24, boxSizing: 'border-box' }}
      >
        {renderPage()}
      </main>
    </>
  );
};

export default App;
