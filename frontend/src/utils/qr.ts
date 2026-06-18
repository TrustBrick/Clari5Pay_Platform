import QRCode from 'qrcode';

/**
 * Build a UPI payment URI. The amount is embedded so the payer never types it
 * manually — scanning the resulting QR pre-fills the exact amount.
 * Format: upi://pay?pa=<vpa>&pn=<payee>&am=<amount>&cu=INR&tn=<note>
 */
export const buildUpiUri = (
  vpa: string,
  payeeName: string,
  amount: number,
  note?: string,
): string => {
  const params = new URLSearchParams();
  params.set('pa', vpa.trim());
  if (payeeName) params.set('pn', payeeName);
  params.set('am', Number(amount).toFixed(2));
  params.set('cu', 'INR');
  if (note) params.set('tn', note);
  return `upi://pay?${params.toString()}`;
};

/** Render a UPI payment QR (with the amount baked in) to a PNG data URL. */
export const upiQrDataUrl = async (
  vpa: string,
  payeeName: string,
  amount: number,
  note?: string,
): Promise<string> => {
  const uri = buildUpiUri(vpa, payeeName, amount, note);
  return QRCode.toDataURL(uri, {
    width: 240,
    margin: 1,
    errorCorrectionLevel: 'M',
    color: { dark: '#0a2540', light: '#ffffff' },
  });
};
