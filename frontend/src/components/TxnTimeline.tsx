import React from 'react';
import { T } from '../utils/theme';

// ─── Transaction Timeline — the status progression for one transaction ────────────────────────
// Presentational vertical status rail shared by the Agent and Merchant modules. Each step renders
// done / current / pending; a rejected transaction ends on a red terminal. All geometry, colours
// and spacing live here so both modules look identical — a caller only supplies the computed steps
// and three lifecycle flags, never any styling.
export type TlStep = { key: string; label: string; ts?: string; reached?: boolean };

//   steps        – the workflow rungs, in order, each carrying its label + optional IST timestamp.
//   currentIndex – index of the step the transaction currently sits on (<0 ⇒ still on the first).
//   done         – reached a successful terminal state (every rung shows done).
//   rejected     – was rejected (append a red terminal node; earlier reached rungs stay done).
export const TxnTimeline: React.FC<{
  steps: TlStep[];
  currentIndex: number;
  done: boolean;
  rejected: boolean;
}> = ({ steps, currentIndex, done, rejected }) => {
  const curIdx = currentIndex;
  const nodeState = (i: number): 'done' | 'current' | 'pending' => {
    if (done) return 'done';
    if (rejected) return steps[i].reached || i === 0 ? 'done' : 'pending';
    if (curIdx < 0) return i === 0 ? 'current' : 'pending';
    return i < curIdx ? 'done' : i === curIdx ? 'current' : 'pending';
  };
  const color = { done: T.success, current: T.blue, pending: T.textMuted } as const;
  // One flat node list — the workflow steps, plus the red terminal when the transaction was
  // rejected. Rendering every node through the same row keeps the rail unbroken all the way to
  // the last entry instead of leaving the terminal floating below a stub of line.
  type TlNode = { label: string; ts: string; dot: string; filled: boolean; line: string; muted: boolean; current: boolean };
  const nodes: TlNode[] = steps.map((s, i) => {
    const st = nodeState(i);
    return { label: s.label, ts: s.ts || '', dot: color[st], filled: st !== 'pending',
      line: st === 'done' ? T.success : T.border, muted: st === 'pending', current: st === 'current' };
  });
  if (rejected) nodes.push({ label: 'Rejected', ts: '', dot: T.danger, filled: true, line: T.border, muted: false, current: false });
  // Geometry of the rail, kept in one place so the dot, the connector and the text all line up.
  // ROW is the distance between two dot centres — equal spacing whether or not a step carries a
  // timestamp — and is a minimum, so a wrapped label grows the row instead of clipping. It has to
  // clear the tallest ordinary row (label + timestamp + the 12px tail) or those rows would push
  // past it and the spacing would drift. The connector stretches to fill whatever height the row
  // ends up with, which is what makes the segments meet edge-to-edge as one continuous line.
  // OFF drops the dot onto the centre of the label's first line; since that offset also pushes the
  // NEXT row's dot down, the connector reclaims it with an equal negative bottom margin — without
  // that, every junction shows a 2px break.
  const DOT = 14, LINE = 2, LH = 18, OFF = (LH - DOT) / 2, ROW = 48;
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 10, fontWeight: 800, color: T.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 10 }}>Timeline</div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {nodes.map((n, i) => {
          const last = i === nodes.length - 1;
          return (
            <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'stretch', minHeight: last ? undefined : ROW }}>
              {/* Rail: dot on top, connector filling the rest of the row. The column stretches to
                  the full row height, so the connector ends exactly where the next dot begins. */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: DOT, flexShrink: 0 }}>
                <div style={{ width: DOT, height: DOT, borderRadius: '50%', background: n.filled ? n.dot : 'transparent', border: `2px solid ${n.dot}`, flexShrink: 0, marginTop: OFF, boxSizing: 'border-box' }} />
                {!last && <div style={{ width: LINE, flex: 1, minHeight: 8, background: n.line, borderRadius: LINE, marginBottom: -OFF }} />}
              </div>
              <div style={{ minWidth: 0, paddingBottom: last ? 0 : 12 }}>
                <p style={{ margin: 0, fontSize: 13, lineHeight: `${LH}px`, fontWeight: n.muted ? 600 : 800, color: n.label === 'Rejected' ? T.danger : n.muted ? T.textMuted : T.textMain, wordBreak: 'break-word' }}>{n.label}{n.current && <span style={{ marginLeft: 8, fontSize: 10.5, fontWeight: 700, color: T.blue }}>CURRENT</span>}</p>
                {n.ts && <p style={{ margin: '2px 0 0', fontSize: 11, lineHeight: '15px', color: T.textMuted }}>{n.ts}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
