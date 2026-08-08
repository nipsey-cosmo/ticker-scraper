import { useMemo } from 'react';

import type { UniverseGraph, UniverseStock } from '@/types/graphs';

import Chart from './Chart';
import { C, SENTIMENT_SCALE, layout, scoreColor } from './theme';

interface Props {
  graph: UniverseGraph;
  /** Clicking a stock in any of these switches the single-stock section. */
  onSelect?: (symbol: string) => void;
}

/**
 * The three cross-stock charts: 03 heatmap, 04 cross-section, 05 divergence.
 *
 * These deliberately show the whole watchlist rather than the selected stock —
 * they exist to put one holding in the context of the others, which a
 * per-stock filter would destroy.
 */

const markerSize = (count: number) => (count > 0 ? 10 + Math.sqrt(count) * 1.1 : 8);

const Empty = ({ height, children }: { height: number; children: string }) => (
  <div
    className="flex items-center justify-center border border-dashed border-rule bg-paper-tint/40 px-4 text-center text-xs text-ink-muted"
    style={{ height }}
  >
    {children}
  </div>
);

/** Rows read top-down from most to least positive, so the current leaders are
    the first thing the eye lands on. Plotly draws the first y entry at the
    bottom, hence the reversal. */
const byLatestSentiment = (stocks: UniverseStock[]) =>
  [...stocks]
    .filter((s) => s.totalArticles > 0)
    .sort((a, b) => (a.latestSentiment ?? -2) - (b.latestSentiment ?? -2));

export const SentimentHeatmap = ({ graph, onSelect }: Props) => {
  const ordered = useMemo(() => byLatestSentiment(graph.stocks), [graph.stocks]);

  const data = useMemo(
    () => [
      {
        type: 'heatmap',
        x: graph.labels,
        y: ordered.map((s) => `${s.symbol}${s.sector ? ` · ${s.sector}` : ''}`),
        z: ordered.map((s) => s.sentiment),
        customdata: ordered.map((s) => s.articles),
        zmin: -1,
        zmax: 1,
        zmid: 0,
        colorscale: SENTIMENT_SCALE,
        xgap: 1,
        ygap: 1,
        // Nulls fall through to the paper behind, which is what marks a bucket
        // with no coverage — a cell shaded neutral would claim a reading.
        hoverongaps: false,
        colorbar: {
          title: { text: 'Score', font: { size: 10 } },
          thickness: 10,
          len: 0.8,
          outlinewidth: 0,
          tickfont: { size: 9, color: C.inkMuted },
        },
        hovertemplate: '%{y}<br>%{x}<br>Sentiment %{z:+.2f} · %{customdata} articles<extra></extra>',
      },
    ],
    [graph.labels, ordered],
  );

  const figure = useMemo(
    () => layout({ margin: { l: 150, r: 30, t: 12, b: 48 }, xaxis: { gridcolor: 'rgba(0,0,0,0)' }, yaxis: { gridcolor: 'rgba(0,0,0,0)' } }),
    [],
  );

  if (ordered.length === 0) {
    return <Empty height={400}>No scored coverage for any followed stock in this window.</Empty>;
  }

  return (
    <div onClick={(event) => {
      // Plotly's own click event would be a second listener and a second
      // teardown; the axis label carries the ticker already.
      const label = (event.target as SVGTextElement)?.textContent ?? '';
      const symbol = label.split(' · ')[0];
      if (onSelect && ordered.some((s) => s.symbol === symbol)) onSelect(symbol);
    }}>
      <Chart data={data} layout={figure} height={Math.max(320, ordered.length * 30 + 90)}
             label="Sentiment per stock per bucket" />
    </div>
  );
};

export const CrossSectionChart = ({ graph }: Props) => {
  // Sorted by return so that neighbouring points are adjacent in the array,
  // which is what makes alternating the label above and below the marker
  // actually separate them. Plotly does no collision avoidance of its own.
  const plotted = useMemo(
    () =>
      graph.stocks
        .filter((s) => s.periodReturn !== null && s.latestSentiment !== null)
        .sort((a, b) => (a.periodReturn as number) - (b.periodReturn as number)),
    [graph.stocks],
  );

  const data = useMemo(
    () => [
      {
        type: 'scatter',
        mode: 'markers+text',
        x: plotted.map((s) => s.periodReturn),
        y: plotted.map((s) => s.latestSentiment),
        text: plotted.map((s) => s.symbol),
        textposition: plotted.map((_, i) => (i % 2 ? 'bottom center' : 'top center')),
        textfont: { size: 10, color: C.ink },
        cliponaxis: false,
        customdata: plotted.map((s) => [s.name ?? s.symbol, s.totalArticles]),
        marker: {
          size: plotted.map((s) => markerSize(s.totalArticles)),
          color: plotted.map((s) => scoreColor(s.latestSentiment)),
          opacity: 0.8,
          line: { color: C.paper, width: 1.5 },
        },
        hovertemplate:
          '%{text} · %{customdata[0]}<br>Return %{x:+.1f}%<br>' +
          'Sentiment %{y:+.2f}<br>%{customdata[1]} articles<extra></extra>',
      },
    ],
    [plotted],
  );

  const figure = useMemo(
    () =>
      layout({
        showlegend: false,
        shapes: [
          { type: 'line', xref: 'x', yref: 'paper', x0: 0, x1: 0, y0: 0, y1: 1, line: { color: C.rule, width: 1 } },
          { type: 'line', xref: 'paper', yref: 'y', x0: 0, x1: 1, y0: 0, y1: 0, line: { color: C.rule, width: 1 } },
        ],
        xaxis: { title: { text: 'Return over the stored price history', font: { size: 11 } }, ticksuffix: '%' },
        yaxis: { title: { text: 'Latest sentiment', font: { size: 11 } }, range: [-1, 1] },
        margin: { l: 58, r: 24, t: 24, b: 52 },
      }),
    [],
  );

  if (plotted.length === 0) {
    return <Empty height={380}>Needs both a price history and scored coverage; neither is stored yet.</Empty>;
  }
  return <Chart data={data} layout={figure} height={380} label="Latest sentiment against period return" />;
};

export const DivergenceChart = ({ graph }: Props) => {
  const ranked = useMemo(
    () =>
      graph.stocks
        .filter((s) => s.divergence !== null)
        .sort((a, b) => (a.divergence as number) - (b.divergence as number)),
    [graph.stocks],
  );

  const data = useMemo(
    () => [
      {
        type: 'bar',
        orientation: 'h',
        x: ranked.map((s) => s.divergence),
        y: ranked.map((s) => s.symbol),
        text: ranked.map((s) => (s.divergence as number).toFixed(2)),
        textposition: 'outside',
        cliponaxis: false,
        textfont: { size: 10, color: C.inkMuted },
        customdata: ranked.map((s) => [s.name ?? s.symbol, s.sentimentChange, s.periodReturn]),
        // The sentiment palette, not the price one. Green and red would read as
        // good and bad; divergence is neither, it is only "which side ran
        // ahead" — and blue-means-coverage matches the heatmap above.
        marker: { color: ranked.map((s) => scoreColor(s.divergence)) },
        hovertemplate:
          '%{y} · %{customdata[0]}<br>Divergence %{x:+.2f}<br>' +
          'Sentiment %{customdata[1]:+.2f} · Return %{customdata[2]:+.1f}%<extra></extra>',
      },
    ],
    [ranked],
  );

  const figure = useMemo(
    () =>
      layout({
        showlegend: false,
        xaxis: {
          title: { text: 'Standardised sentiment change − standardised return', font: { size: 11 } },
          zeroline: true,
          zerolinecolor: C.ink,
        },
        yaxis: { gridcolor: 'rgba(0,0,0,0)' },
        margin: { l: 56, r: 46, t: 14, b: 56 },
      }),
    [],
  );

  if (ranked.length < 2) {
    return (
      <Empty height={380}>
        Needs at least two stocks with both a sentiment change and a return to rank against each other.
      </Empty>
    );
  }
  return <Chart data={data} layout={figure} height={380} label="Stocks ranked by sentiment and price divergence" />;
};
