// Real bank logos, bundled locally from the auraveni/global-bank-logos collection (MIT).
// Attribution and licence: docs/THIRD_PARTY_ASSETS.md at the repo root.
//
// Vite resolves every SVG in assets/banks at BUILD time and hands back a hashed asset URL, so
// rendering a logo issues no request of its own: the browser fetches each distinct logo once and
// caches it, and a logo that is never displayed is never downloaded.

const FILES = import.meta.glob('../assets/banks/*.svg', {
  eager: true, query: '?url', import: 'default',
}) as Record<string, string>;

const BY_SLUG: Record<string, string> = {};
for (const [path, url] of Object.entries(FILES)) {
  BY_SLUG[path.slice(path.lastIndexOf('/') + 1, -('.svg'.length))] = url;
}

/**
 * Collapse a bank name so the spelling the IFSC registry returns and the one the asset catalogue
 * publishes meet in the middle: case, punctuation, "&" vs "and", a leading "The" and a trailing
 * Ltd/Limited all normalise away. "The Federal Bank Ltd" and "Federal Bank" both become
 * "federal bank".
 */
const norm = (s: string): string => (s || '')
  .toLowerCase()
  .replace(/&/g, ' and ')
  .replace(/[^a-z0-9]+/g, ' ')
  .replace(/\b(ltd|limited)\b/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()
  .replace(/^the\s+/, '');

/**
 * Bank name -> logo slug. Keys are the names the asset catalogue publishes; the aliases beneath
 * each group are the spellings the IFSC registry actually returns, which do not always match
 * (the registry says "IDBI", not "IDBI Bank"). Adding a bank is one line here plus its SVG in
 * assets/banks — no form changes.
 */
const NAME_TO_SLUG: Record<string, string> = {
  'airtel payments bank': 'apb',
  'au small finance bank': 'ausfb',
  'axis bank': 'axis',
  'bandhan bank': 'bandhan',
  'bank of baroda': 'bob',
  'bank of india': 'boi',
  'bank of maharashtra': 'bom',
  'canara bank': 'canara',
  'central bank of india': 'cbi',
  'csb bank': 'csb',
  'catholic syrian bank': 'csb',
  'city union bank': 'cub',
  'dcb bank': 'dcb',
  'development credit bank': 'dcb',
  'dhanlaxmi bank': 'dhanlaxmi',
  'esaf small finance bank': 'esaf',
  'federal bank': 'federal',
  'fino payments bank': 'fino',
  'hdfc bank': 'hdfc',
  'icici bank': 'icici',
  'idbi bank': 'idbi',
  'idbi': 'idbi',
  'idfc first bank': 'idfc',
  'idfc bank': 'idfc',
  'indian bank': 'indian',
  'india post payments bank': 'indiapost',
  'indusind bank': 'indus',
  'indian overseas bank': 'iob',
  'jio payments bank': 'jio',
  'jammu and kashmir bank': 'jnk',
  'karnataka bank': 'karnataka',
  'kotak mahindra bank': 'kotak',
  'karur vysya bank': 'kvb',
  'nainital bank': 'ntb',
  'paytm payments bank': 'paytm',
  'punjab national bank': 'pnb',
  'punjab and sind bank': 'psb',
  'rbl bank': 'rbl',
  'ratnakar bank': 'rbl',
  'state bank of india': 'sbi',
  'south indian bank': 'sib',
  'tamilnad mercantile bank': 'tmb',
  'union bank of india': 'ubi',
  'uco bank': 'uco',
  'ujjivan small finance bank': 'ujjivan',
  'yes bank': 'yes',

  // International banks with Indian branches. The aliases are the spellings the IFSC registry
  // actually returns, which diverge sharply from the trading name — it calls HSBC "Hongkong &
  // Shanghai Banking Corporation" and Citibank "CITI Bank".
  'citibank': 'citi',
  'citi bank': 'citi',
  'standard chartered bank': 'standard',
  'hsbc bank': 'hsbc',
  'hsbc': 'hsbc',
  'hongkong and shanghai banking corporation': 'hsbc',
  'deutsche bank': 'deutsche',
  'dbs bank india': 'dbs',
  'dbs bank': 'dbs',
  'barclays bank': 'barclays',
  'barclays': 'barclays',
  'bank of america': 'boa',
  'jpmorgan chase bank': 'chase',
  'jp morgan chase bank': 'chase',
  'jp morgan chase bank na': 'chase',
};

/**
 * IFSC bank code (the first four characters of any IFSC) -> logo slug. Preferred over the name
 * when an IFSC is to hand, because the code is unambiguous where a name spelling can drift.
 * Every entry below was read back from the IFSC registry itself — none are guessed, because a
 * wrong code would show a confidently wrong bank.
 */
const CODE_TO_SLUG: Record<string, string> = {
  BARB: 'bob', BKID: 'boi', CIUB: 'cub', CNRB: 'canara', FDRL: 'federal',
  HDFC: 'hdfc', IBKL: 'idbi', ICIC: 'icici', IDFB: 'idfc', IDIB: 'indian',
  INDB: 'indus', IOBA: 'iob', KARB: 'karnataka', KKBK: 'kotak', MAHB: 'bom',
  PSIB: 'psb', RATN: 'rbl', SBIN: 'sbi', SIBL: 'sib', UBIN: 'ubi',
  UCBA: 'uco', UTIB: 'axis', YESB: 'yes',
  BOFA: 'boa', CHAS: 'chase', CITI: 'citi', DEUT: 'deutsche', HSBC: 'hsbc', SCBL: 'standard',
};

/**
 * Optical size correction, applied inside the fixed logo box.
 *
 * The assets are drawn by different hands: some fill their viewBox edge to edge, others bake in
 * generous transparent padding. Scaled naively to one box those read as different sizes even
 * though the box is identical. A factor here nudges an outlier back onto the same optical weight
 * as the rest — it scales the artwork within its box and never changes the box, so alignment and
 * the aspect ratio are untouched. Anything absent is 1 (drawn as-is).
 */
const SLUG_SCALE: Record<string, number> = {};

export interface BankLogoAsset {
  /** Bundled asset URL. */
  url: string;
  /** Optical correction to apply inside the fixed box; 1 means draw as-is. */
  scale: number;
}

/**
 * The bundled logo for a bank, or null when we have no asset for it. Null is a real answer and
 * callers must render nothing rather than substitute a placeholder — showing the wrong bank's
 * mark on a payout form is worse than showing none.
 */
export const bankLogo = (name?: string | null, ifsc?: string | null): BankLogoAsset | null => {
  const code = (ifsc || '').trim().toUpperCase().slice(0, 4);
  const slug = (code && CODE_TO_SLUG[code]) || NAME_TO_SLUG[norm(name || '')];
  if (!slug) return null;
  const url = BY_SLUG[slug];
  return url ? { url, scale: SLUG_SCALE[slug] ?? 1 } : null;
};

/** Count of bundled logos — used by the asset test to catch a missing or renamed file. */
export const bundledLogoCount = (): number => Object.keys(BY_SLUG).length;
