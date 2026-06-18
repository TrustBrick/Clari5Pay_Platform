import React, { useState } from 'react';
import { useAuth } from './context/AuthContext';
import { T } from './utils/theme';
import { PAGE_TITLES } from './utils/nav';
import Sidebar from './components/Sidebar';
import Header from './components/Header';
import LoginPage from './pages/LoginPage';
import {
  MerchantDashboard, DepositManagement, WithdrawalManagement, SettlementManagement,
  TransactionHistory, BalancePage, RiskPage, MerchantSupportChat, ProfilePage,
  CancelRequestPage, TemplatesPage, NewsPage,
} from './pages/MerchantPages';
import {
  AdminDashboard, AdminMerchantsPage, AdminTransactionsPage, AdminAccountsPage,
  SaDashboard, SaAdminsPage, SystemLogsPage, AuditLogsPage,
} from './pages/AdminPages';

const defaultPageFor = (role?: string) =>
  role === 'MERCHANT' ? 'dashboard' : role === 'ADMIN' ? 'admin-dashboard' : 'sa-dashboard';

// Is `page` a valid destination for this role? (prevents showing another role's page)
const pageAllowed = (role: string, page: string) => {
  if (!page) return false;
  if (page === 'profile') return true;
  if (role === 'SUPER_ADMIN') return page.startsWith('sa-');
  if (role === 'ADMIN') return page.startsWith('admin-');
  return !page.startsWith('sa-') && !page.startsWith('admin-'); // MERCHANT
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

  if (!user) return <LoginPage />;

  // Effective page: fall back to the role default if the current page isn't valid for this role
  // (e.g. a stale page left over from a previous session/role).
  const activePage = pageAllowed(user.role, page) ? page : defaultPageFor(user.role);

  const renderPage = () => {
    const props = { user };
    const map: Record<string, React.ReactNode> = {
      dashboard: <MerchantDashboard {...props} />,
      deposit: <DepositManagement {...props} />,
      withdrawal: <WithdrawalManagement {...props} />,
      settlement: <SettlementManagement {...props} />,
      cancel: <CancelRequestPage {...props} />,
      transactions: <TransactionHistory {...props} />,
      templates: <TemplatesPage {...props} />,
      balance: <BalancePage {...props} />,
      risk: <RiskPage {...props} />,
      news: <NewsPage {...props} />,
      support: <MerchantSupportChat {...props} />,
      profile: <ProfilePage {...props} />,
      'admin-dashboard': <AdminDashboard {...props} />,
      'admin-merchants': <AdminMerchantsPage />,
      'admin-transactions': <AdminTransactionsPage />,
      'admin-accounts': <AdminAccountsPage />,
      'sa-dashboard': <SaDashboard />,
      'sa-admins': <SaAdminsPage />,
      'sa-logs': <SystemLogsPage />,
      'sa-audit': <AuditLogsPage />,
    };
    return map[activePage] || map[defaultPageFor(user.role)];
  };

  return (
    <>
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
