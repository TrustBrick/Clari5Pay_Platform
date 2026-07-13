import React from 'react';
import { Icon } from './Icon';

const IS_DEMO = (import.meta as any).env.VITE_APP_ENV === 'demo';

// Permanent ribbon shown on every page of a Demo/UAT build (VITE_APP_ENV=demo). Renders
// nothing in a Production build. Publishes --demo-banner-h so the root layout can pad
// its top to make room — a no-op in Production, where the var is never defined.
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
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}><Icon name="warning" size={14} weight="fill" /> DEMO ENVIRONMENT — No live transactions</span>
      </div>
    </>
  );
};

export default DemoBanner;
