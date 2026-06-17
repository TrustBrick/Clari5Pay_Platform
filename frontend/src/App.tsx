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
  CancelRequestPage, TemplatesPage,
} from './pages/MerchantPages';
import {
  AdminDashboard, AdminMerchantsPage, AdminTransactionsPage, AdminAccountsPage,
  SaDashboard, SaAdminsPage, SystemLogsPage,
} from './pages/AdminPages';

const App: React.FC = () => {
  const { user, logout } = useAuth();
  const [page, setPage] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Default page per role
  React.useEffect(() => {
    if (user && !page) {
      if (user.role === 'MERCHANT') setPage('dashboard');
      else if (user.role === 'ADMIN') setPage('admin-dashboard');
      else setPage('sa-dashboard');
    }
  }, [user]);

  if (!user) return <LoginPage />;

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
      support: <MerchantSupportChat {...props} />,
      profile: <ProfilePage {...props} />,
      'admin-dashboard': <AdminDashboard {...props} />,
      'admin-merchants': <AdminMerchantsPage />,
      'admin-transactions': <AdminTransactionsPage />,
      'admin-accounts': <AdminAccountsPage />,
      'sa-dashboard': <SaDashboard />,
      'sa-admins': <SaAdminsPage />,
      'sa-logs': <SystemLogsPage />,
    };
    return map[page] || map[user.role === 'MERCHANT' ? 'dashboard' : user.role === 'ADMIN' ? 'admin-dashboard' : 'sa-dashboard'];
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
        active={page}
        onNav={(key) => { setPage(key); setSidebarOpen(false); }}
        onLogout={logout}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <Header
        user={user}
        title={PAGE_TITLES[page] || 'Dashboard'}
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
