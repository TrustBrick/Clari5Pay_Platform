import React from 'react';
import { T } from '../utils/theme';
import { bankLogo } from '../utils/bankLogos';

/** The logo box every bank mark is drawn into, so marks line up column-to-column across forms. */
const BOX = 30;

// THE bank logo. Every surface in the platform that shows an identified bank renders this, so
// the asset, its size, its alignment and the rule for when it appears are identical everywhere —
// Admin, Merchant, Agent, Deposit, Withdrawal, Settlement, Account Management, Risk.
//
// Sizing is the point of the box. The assets come from different hands and differ in intrinsic
// dimensions and in how much transparent padding they bake in, so drawing them at their natural
// size gives a ragged row. Each is instead centred inside a FIXED square and scaled down to fit
// with object-fit: contain, so every mark occupies the same footprint and sits on the same
// baseline as the bank name beside it, while keeping its own aspect ratio — never stretched,
// never cropped. Assets that still read light for their box get an optical factor in
// utils/bankLogos, applied to the artwork inside the box and never to the box itself.
//
// It shows ONLY once the IFSC lookup has actually identified the bank: pass the hook's `locked`
// as `show`. An invalid code, an unreachable registry or a hand-typed name renders nothing.
//
// A bank we hold no asset for renders its NAME with no mark and no empty box — there is
// deliberately no placeholder or initials stand-in.
export const BankLogo: React.FC<{
  /** Bank name from the IFSC lookup. */
  name: string;
  /** IFSC, when the form has it. Its first four characters identify the bank unambiguously and
   *  are preferred over the name, which can vary in spelling between sources. */
  ifsc?: string;
  /** Gate — pass the IFSC hook's `locked`. No logo unless the lookup identified the bank. */
  show?: boolean;
  /** Render the bank name beside the logo. */
  withName?: boolean;
  /** Optional branch, shown after the name where the surface has room. */
  branch?: string;
  /** Box edge in px. The artwork is fitted inside it; it is not the artwork's own size. */
  size?: number;
  style?: React.CSSProperties;
}> = ({ name, ifsc, show = true, withName = true, branch, size = BOX, style }) => {
  const n = (name || '').trim();
  if (!show || !n) return null;
  const logo = bankLogo(n, ifsc);
  if (!logo && !withName) return null;      // nothing to draw and nothing to say
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9, minWidth: 0, fontSize: 11.5, ...style }}>
      {logo && (
        <span
          style={{
            width: size, height: size, flexShrink: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            // No plate behind the mark: several of these logos carry their own background and a
            // second one would double it up. Any that needs a ground has it drawn in.
            overflow: 'hidden',
          }}
        >
          <img
            src={logo.url}
            alt={withName ? '' : n}
            aria-hidden={withName || undefined}
            style={{
              maxWidth: '100%', maxHeight: '100%', width: 'auto', height: 'auto',
              objectFit: 'contain', display: 'block',
              transform: logo.scale === 1 ? undefined : `scale(${logo.scale})`,
            }}
          />
        </span>
      )}
      {withName && (
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          <b style={{ color: T.textMain, fontWeight: 700 }}>{n}</b>
          {branch ? <span style={{ color: T.textMuted }}> · {branch}</span> : null}
        </span>
      )}
    </span>
  );
};

/** Box edge for a logo sitting INSIDE a field. Deliberately smaller than the standalone BOX and
 *  close to the 16px glyph Input draws for its named icons, so a bank mark reads as part of the
 *  field's own furniture rather than as artwork dropped into it. Input reserves 26px between the
 *  icon's left edge and the text, so the box must stay under that to keep its gap. */
const IN_FIELD = 18;

/**
 * The logo to hand to an Input's `icon`, so the mark sits inside the Bank Name field itself
 * rather than repeating the name on a line beneath it.
 *
 * Returns undefined — not an empty element — when the lookup has not identified the bank or we
 * hold no asset for it. That matters: Input reserves its left padding whenever `icon` is
 * present, so a component that merely renders null would still leave the text indented past a
 * gap that has nothing in it.
 */
export const bankLogoIcon = (
  name?: string | null, ifsc?: string | null, show = true,
): React.ReactNode | undefined => {
  const n = (name || '').trim();
  if (!show || !n || !bankLogo(n, ifsc)) return undefined;
  return <BankLogo name={n} ifsc={ifsc || undefined} show withName={false} size={IN_FIELD} />;
};
