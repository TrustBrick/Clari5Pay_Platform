// Lightweight, dependency-free confetti burst. Injects coloured particles into a
// fixed overlay layer, then cleans them up. Honours prefers-reduced-motion.
const COLORS = ['#0052cc', '#26d00c', '#f5a623', '#e3342f', '#8b5cf6', '#00c2a8'];

export function fireConfetti(count = 90): void {
  if (typeof document === 'undefined') return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const layer = document.createElement('div');
  layer.className = 'c5-confetti-layer';
  document.body.appendChild(layer);

  let maxDur = 0;
  for (let i = 0; i < count; i++) {
    const p = document.createElement('div');
    p.className = 'c5-confetti';
    const dur = 1.8 + Math.random() * 1.4;
    maxDur = Math.max(maxDur, dur);
    p.style.left = Math.random() * 100 + 'vw';
    p.style.background = COLORS[Math.floor(Math.random() * COLORS.length)];
    p.style.setProperty('--dur', dur + 's');
    p.style.animationDelay = Math.random() * 0.3 + 's';
    p.style.transform = `translateY(-10px) rotate(${Math.random() * 360}deg)`;
    if (Math.random() > 0.5) p.style.borderRadius = '50%';
    p.style.opacity = String(0.8 + Math.random() * 0.2);
    layer.appendChild(p);
  }
  setTimeout(() => layer.remove(), (maxDur + 0.5) * 1000);
}
