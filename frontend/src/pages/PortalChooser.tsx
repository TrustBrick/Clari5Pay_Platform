import React from 'react';
import { T } from '../utils/theme';
import { Logo } from '../components/UI';
import ThemeToggle from '../components/ThemeToggle';
import { PORTAL_LINKS } from '../utils/portal';

// Shown on app.clari5pay.com — routes each user to their dedicated portal domain.
const PortalChooser: React.FC = () => (
  <div style={{ minHeight: '100vh', background: '#0a2540', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: 24, boxSizing: 'border-box', fontFamily: "'Inter','Segoe UI',sans-serif", position: 'relative' }}>
    <div style={{ position: 'absolute', top: 18, right: 18 }}><ThemeToggle /></div>
    <div style={{ marginBottom: 22 }}><Logo size="lg" dark /></div>
    <p style={{ color: 'rgba(255,255,255,0.65)', fontSize: 14, margin: '0 0 26px', textAlign: 'center' }}>Choose your portal to continue</p>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 18, width: '100%', maxWidth: 800 }}>
      {PORTAL_LINKS.map(p => (
        <a key={p.url} href={p.url}
          style={{ textDecoration: 'none', background: T.surface, borderRadius: 16, padding: '24px 22px', boxShadow: '0 10px 40px rgba(0,0,0,0.28)', display: 'block' }}>
          <div style={{ fontSize: 32, marginBottom: 10 }}>{p.icon}</div>
          <div style={{ fontSize: 16, fontWeight: 800, color: T.textMain, marginBottom: 4 }}>{p.name}</div>
          <div style={{ fontSize: 12.5, color: T.textMuted, marginBottom: 14 }}>{p.desc}</div>
          <span style={{ fontSize: 13, fontWeight: 700, color: T.blue }}>Open →</span>
        </a>
      ))}
    </div>
    <p style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 26 }}>Clari5Pay — Secure Payments. Prevent Fraud.</p>
  </div>
);

export default PortalChooser;
