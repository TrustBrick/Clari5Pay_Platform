import type { Account } from '../types';

/**
 * Render the selected bank account's details onto a canvas and return a PNG data URL.
 * The admin never uploads an image — this is auto-generated and sent to the merchant.
 */
export const accountToPng = (a: Account): string => {
  const W = 720;
  const H = 420;
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  if (!ctx) return '';

  // Background
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);

  // Header band
  ctx.fillStyle = '#0a2540';
  ctx.fillRect(0, 0, W, 84);
  ctx.fillStyle = '#26d00c';
  ctx.fillRect(0, 84, W, 5);
  ctx.fillStyle = '#ffffff';
  ctx.font = '700 26px Segoe UI, Arial, sans-serif';
  ctx.fillText('Clari5Pay — Payment Details', 32, 52);

  // Body rows
  const rows: Array<[string, string]> = [
    ['Account Name', a.accountName],
    ['Bank', a.bankName],
    ['Account Number', a.accountNumber],
    ['IFSC Code', a.ifscCode],
    ['Branch', a.branch],
    ['Account Type', a.accountType],
    ['Reference', a.referenceNumber],
  ];
  let y = 140;
  rows.forEach(([k, v]) => {
    ctx.fillStyle = '#6b7280';
    ctx.font = '600 15px Segoe UI, Arial, sans-serif';
    ctx.fillText(k.toUpperCase(), 32, y);
    ctx.fillStyle = '#0a2540';
    ctx.font = '700 22px Segoe UI, Arial, sans-serif';
    ctx.fillText(String(v ?? '—'), 32, y + 26);
    ctx.strokeStyle = '#e2e8f0';
    ctx.beginPath();
    ctx.moveTo(32, y + 40);
    ctx.lineTo(W - 32, y + 40);
    ctx.stroke();
    y += 64;
  });

  ctx.fillStyle = '#9ca3af';
  ctx.font = '400 13px Segoe UI, Arial, sans-serif';
  ctx.fillText('Please transfer the amount to the account above and upload your payment slip.', 32, H - 20);

  return canvas.toDataURL('image/png');
};
