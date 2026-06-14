import React, { createContext, useContext, useState, useCallback } from 'react';

interface ToastItem {
  id: number;
  msg: string;
  type: 'success' | 'error' | 'info';
}

interface ToastContextType {
  showToast: (msg: string, type?: 'success' | 'error' | 'info') => void;
}

const ToastContext = createContext<ToastContextType | null>(null);

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const showToast = useCallback((msg: string, type: 'success' | 'error' | 'info' = 'success') => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, msg, type }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3500);
  }, []);

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <div style={{ position: 'fixed', bottom: 24, right: 24, zIndex: 9999, display: 'flex', flexDirection: 'column', gap: 10 }}>
        {toasts.map((t) => (
          <Toast key={t.id} msg={t.msg} type={t.type} />
        ))}
      </div>
    </ToastContext.Provider>
  );
};

const Toast: React.FC<{ msg: string; type: string }> = ({ msg, type }) => {
  const colors: Record<string, string> = { success: '#059669', error: '#dc2626', info: '#0052cc' };
  const icon = type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ';
  return (
    <div style={{
      background: '#0a2540', color: '#fff', padding: '14px 20px', borderRadius: 14,
      boxShadow: '0 10px 40px rgba(0,0,0,0.25)', display: 'flex', gap: 10, alignItems: 'center',
      maxWidth: 360, borderLeft: `4px solid ${colors[type] || '#059669'}`, animation: 'fade-in 0.3s ease',
    }}>
      <span style={{ fontSize: 18 }}>{icon}</span>
      <span style={{ fontSize: 13, fontWeight: 500 }}>{msg}</span>
    </div>
  );
};

export const useToast = () => {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used within ToastProvider');
  return ctx;
};
