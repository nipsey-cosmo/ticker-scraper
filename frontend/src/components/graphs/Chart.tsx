import { useEffect, useRef } from 'react';

import Plotly from '@/lib/plotly';

import { CONFIG } from './theme';

interface ChartProps {
  data: unknown[];
  layout: object;
  /** Announced in place of the drawing. Plotly renders SVG with no semantics. */
  label: string;
  height?: number;
}

/**
 * One Plotly figure, redrawn in place.
 *
 * `Plotly.react` diffs against what is already on screen rather than tearing
 * the plot down, so switching timeframe animates the axes instead of flashing
 * an empty box. The purge is deliberately in its own effect with an empty
 * dependency list — running it on every data change would defeat that.
 *
 * Callers must memoise `data` and `layout`; they are compared by reference.
 */
const Chart = ({ data, layout, label, height = 360 }: ChartProps) => {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ref.current) Plotly.react(ref.current, data, layout, CONFIG);
  }, [data, layout]);

  useEffect(() => {
    const element = ref.current;
    // Plotly attaches window listeners for `responsive`, so an unmounted chart
    // that was never purged keeps the whole figure alive on every resize.
    return () => {
      if (element) Plotly.purge(element);
    };
  }, []);

  return <div ref={ref} role="img" aria-label={label} style={{ width: '100%', height }} />;
};

export default Chart;
