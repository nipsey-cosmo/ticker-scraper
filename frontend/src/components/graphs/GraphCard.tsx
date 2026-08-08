import type { ReactNode } from 'react';

interface GraphCardProps {
  /** Two-digit chart number, matching the numbering in the reference deck. */
  index: string;
  kicker: string;
  title: string;
  blurb: string;
  /** What the chart is good at — the footer line under the rule. */
  note: string;
  /** Method caveat, shown as a chip. Only where the data needs one. */
  caveat?: string;
  children: ReactNode;
}

/**
 * The frame every chart sits in.
 *
 * FT section grammar rather than the reference deck's cards: a heavy rule
 * above the label and nothing below it, a serif headline, sans supporting
 * copy. No border box, no radius, no shadow — the rules do the separating.
 */
const GraphCard = ({ index, kicker, title, blurb, note, caveat, children }: GraphCardProps) => (
  // Anchored so a chart can be linked to directly — seven charts is more than
  // fits on a screen, and "look at 05" should be a URL.
  <article id={`chart-${index}`} className="ft-section scroll-mt-4">
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-ftblue">
          {index} · {kicker}
        </p>
        <h3 className="ft-headline mt-1 text-xl">{title}</h3>
        <p className="mt-1 max-w-3xl text-xs leading-relaxed text-ink-muted">{blurb}</p>
      </div>
      {caveat && <span className="ft-chip shrink-0 uppercase">{caveat}</span>}
    </div>

    <div className="mt-3">{children}</div>

    <p className="mt-2 border-t border-rule-light pt-2 text-xs leading-relaxed text-ink-muted">
      <strong className="font-semibold text-ink-strong">Best for:</strong> {note}
    </p>
  </article>
);

export default GraphCard;
