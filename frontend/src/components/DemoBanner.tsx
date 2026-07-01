import React from 'react';
import { IS_DEMO } from '../utils/portal';

// Permanent ribbon shown on every page of a Demo/UAT build (VITE_APP_ENV=demo). Renders
// nothing at all in a Production build. Also publishes --demo-banner-h so the fixed
// Header/Sidebar (and the main content's top margin) can shift down to make room —
// see the `var(--demo-banner-h, 0px)` fallback used there, which is a no-op in Production.
const DemoBanner: React.FC = () => {
  if (!IS_DEMO) return null;
  return (
    <>
      <style>{`:root{--demo-banner-h:28px}`}</style>
      <div style={{
        position: 'fixed', top: 0, left: 0, right: 0, height: 28, zIndex: 1000,
        background: '#f59e0b', color: '#1a1a1a', display: 'flex', alignItems: 'center',
        justifyContent: 'center', gap: 8, fontSize: 12.5, fontWeight: 800,
        letterSpacing: 0.4, fontFamily: "'Inter','Segoe UI',sans-serif",
        boxShadow: '0 1px 6px rgba(0,0,0,0.25)',
      }}>
        <span>⚠ DEMO ENVIRONMENT — No live transactions</span>
      </div>
    </>
  );
};

export default DemoBanner;
