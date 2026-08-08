import type { ReactNode } from 'react';

interface GraphCardProps {
  /** Two-digit chart number, matching the numbering in the reference deck. */
  index: string;
  /** What is plotted, stated plainly. No interpretation — the chart is the argument. */
  title: string;
  /** Method caveat, shown as a chip. Only where the data needs one. */
  caveat?: string;
  children: ReactNode;
}

/**
 * The frame every chart sits in.
 *
 * FT section grammar rather than the reference deck's cards: a heavy rule
 * above the label and nothing below it, a serif headline. No border box, no
 * radius, no shadow — the rules do the separating.
 */
const GraphCard = ({ index, title, caveat, children }: GraphCardProps) => (
  // Anchored so a chart can be linked to directly — seven charts is more than
  // fits on a screen, and "look at 05" should be a URL.
  <article id={`chart-${index}`} className="ft-section scroll-mt-4">
    <div className="flex items-baseline justify-between gap-4">
      <h3 className="ft-headline min-w-0 text-lg">
        <span className="mr-2 font-mono text-[11px] align-middle text-ftblue">{index}</span>
        {title}
      </h3>
      {caveat && <span className="ft-chip shrink-0 uppercase">{caveat}</span>}
    </div>

    <div className="mt-3">{children}</div>
  </article>
);

export default GraphCard;
