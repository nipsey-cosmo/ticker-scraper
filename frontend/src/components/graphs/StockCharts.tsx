import { useMemo } from 'react';

import type { StockGraph } from '@/types/graphs';
import { BUCKET_NOUN } from '@/types/graphs';

import Chart from './Chart';
import GraphCard from './GraphCard';
import { C, FILL, SENTIMENT_AXIS, layout, scoreColor } from './theme';

interface Props {
  graph: StockGraph;
}

/**
 * The four single-instrument charts: 01 distribution, 02 reaction, 06 OHLC,
 * 07 trend and risk.
 *
 * All of them read arrays the API already aligned to a bucket grid, so there
 * is no arithmetic here beyond marker sizing. Anything that looked like a
 * calculation in the reference deck — rolling averages, drawdown, returns —
 * happens server-side where it is under test.
 */

/** Bubble area, not diameter, tracks the count: doubling the radius would
    quadruple the ink for twice the articles and overstate a busy week. */
const markerSize = (count: number) => (count > 0 ? 6 + Math.sqrt(count) * 1.3 : 4);

const hasAny = (values: (number | null)[]) => values.some((v) => v !== null);

const Empty = ({ height, children }: { height: number; children: string }) => (
  <div
    className="flex items-center justify-center border border-dashed border-rule bg-paper-tint/40 px-4 text-center text-xs text-ink-muted"
    style={{ height }}
  >
    {children}
  </div>
);

export const DistributionChart = ({ graph }: Props) => {
  const data = useMemo(
    () =>
      graph.buckets.map((_, i) => ({
        type: 'box',
        name: graph.labels[i],
        y: graph.samples[i],
        boxpoints: 'outliers',
        marker: { color: C.inkMuted, size: 3, opacity: 0.55 },
        line: { color: scoreColor(graph.sentiment[i]), width: 1.4 },
        fillcolor: (graph.sentiment[i] ?? 0) >= 0 ? FILL.blueSolid : 'rgba(153, 15, 61, 0.10)',
        whiskerwidth: 0.5,
        hovertemplate: `${graph.labels[i]}<br>Score %{y:.2f}<extra></extra>`,
      })),
    [graph],
  );

  const figure = useMemo(
    () =>
      layout({
        showlegend: false,
        boxgap: 0.3,
        yaxis: { ...SENTIMENT_AXIS, title: { text: 'Article sentiment', font: { size: 11 } } },
        xaxis: { tickangle: 0 },
        margin: { l: 60, r: 20, t: 12, b: 52 },
      }),
    [],
  );

  if (graph.totalArticles === 0) {
    return <Empty height={420}>No scored coverage for this stock in this window.</Empty>;
  }
  return <Chart data={data} layout={figure} height={420} label="Sentiment distribution per bucket" />;
};

export const ReactionChart = ({ graph }: Props) => {
  const data = useMemo(
    () => [
      {
        type: 'bar',
        name: 'News sentiment',
        x: graph.labels,
        y: graph.sentiment,
        yaxis: 'y2',
        marker: {
          color: graph.sentiment.map((v) => ((v ?? 0) >= 0 ? FILL.blue : FILL.claret)),
        },
        customdata: graph.articles,
        hovertemplate: 'Sentiment %{y:+.2f}<br>%{customdata} articles<extra></extra>',
      },
      {
        type: 'scatter',
        mode: 'lines+markers',
        name: `${graph.symbol} close`,
        x: graph.labels,
        y: graph.close,
        connectgaps: false,
        line: { color: C.inkStrong, width: 2 },
        marker: {
          size: graph.articles.map(markerSize),
          color: C.inkStrong,
          line: { color: C.paper, width: 1.5 },
        },
        customdata: graph.articles,
        hovertemplate: 'Close %{y:.2f}<br>%{customdata} articles<extra></extra>',
      },
    ],
    [graph],
  );

  const figure = useMemo(
    () =>
      layout({
        hovermode: 'x unified',
        bargap: 0.35,
        yaxis: {
          title: { text: `Close · ${graph.currency ?? ''}`.trim(), font: { size: 11 } },
        },
        yaxis2: {
          title: { text: 'Sentiment', font: { size: 11 } },
          overlaying: 'y',
          side: 'right',
          range: [-1, 1],
          showgrid: false,
          zeroline: true,
          zerolinecolor: C.rule,
          tickfont: { size: 10, color: C.inkMuted },
        },
        margin: { l: 62, r: 58, t: 28, b: 48 },
      }),
    [graph.currency],
  );

  if (!hasAny(graph.close) && graph.totalArticles === 0) {
    return <Empty height={420}>No prices or coverage for this stock in this window.</Empty>;
  }
  return <Chart data={data} layout={figure} height={420} label="Price, coverage and sentiment on one timeline" />;
};

export const CandlestickChart = ({ graph }: Props) => {
  const bars = graph.bars;

  const data = useMemo(
    () => [
      {
        type: 'candlestick',
        name: 'Range',
        x: bars.labels,
        open: bars.open,
        high: bars.high,
        low: bars.low,
        close: bars.close,
        xaxis: 'x',
        yaxis: 'y',
        increasing: { line: { color: C.up, width: 1.4 }, fillcolor: C.up },
        decreasing: { line: { color: C.down, width: 1.4 }, fillcolor: C.down },
        hoverlabel: { namelength: 0 },
      },
      {
        type: 'bar',
        name: 'Articles',
        x: bars.labels,
        y: bars.articles,
        xaxis: 'x2',
        yaxis: 'y2',
        marker: {
          color: bars.close.map((close, i) =>
            close === null || bars.open[i] === null
              ? C.rule
              : close >= (bars.open[i] as number)
                ? 'rgba(0, 107, 69, 0.45)'
                : 'rgba(204, 0, 0, 0.40)',
          ),
        },
        hovertemplate: '%{y} articles<extra></extra>',
      },
    ],
    [bars],
  );

  const figure = useMemo(
    () =>
      layout({
        showlegend: false,
        xaxis: { domain: [0, 1], anchor: 'y', showticklabels: false, rangeslider: { visible: false } },
        yaxis: {
          domain: [0.32, 1],
          title: { text: `Price · ${graph.currency ?? ''}`.trim(), font: { size: 11 } },
          gridcolor: C.ruleLight,
        },
        xaxis2: { domain: [0, 1], anchor: 'y2', gridcolor: 'rgba(0,0,0,0)', tickfont: { size: 10, color: C.inkMuted } },
        yaxis2: {
          domain: [0, 0.22],
          title: { text: 'Articles', font: { size: 11 } },
          gridcolor: C.ruleLight,
          rangemode: 'tozero',
        },
        margin: { l: 62, r: 20, t: 14, b: 48 },
      }),
    [graph.currency],
  );

  if (!hasAny(bars.close)) {
    return (
      <Empty height={420}>
        No price history in this window — the candles need at least one stored close.
      </Empty>
    );
  }
  return <Chart data={data} layout={figure} height={420} label="Price range and article count per bucket" />;
};

export const TrendRiskChart = ({ graph }: Props) => {
  const data = useMemo(
    () => [
      {
        type: 'scatter',
        mode: 'lines+markers',
        name: 'Close',
        x: graph.labels,
        y: graph.close,
        connectgaps: false,
        line: { color: C.inkStrong, width: 2 },
        marker: { size: 4, color: C.inkStrong },
        xaxis: 'x',
        yaxis: 'y',
        hovertemplate: 'Close %{y:.2f}<extra></extra>',
      },
      {
        type: 'scatter',
        mode: 'lines',
        name: `${graph.maShortWindow}-${BUCKET_NOUN[graph.bucket]} average`,
        x: graph.labels,
        y: graph.maShort,
        // Connected across empty buckets, unlike the close above it. A moving
        // average is a statistic over sessions, and a daily grid is mostly
        // weekends — leaving the gaps open shatters the line into two-point
        // fragments and the longer window disappears entirely. The close line
        // underneath still shows honestly where there was no price.
        connectgaps: true,
        line: { color: C.blue, width: 1.8 },
        xaxis: 'x',
        yaxis: 'y',
        hovertemplate: 'Short avg %{y:.2f}<extra></extra>',
      },
      {
        type: 'scatter',
        mode: 'lines',
        name: `${graph.maLongWindow}-${BUCKET_NOUN[graph.bucket]} average`,
        x: graph.labels,
        y: graph.maLong,
        connectgaps: true,
        line: { color: C.teal, width: 1.8, dash: 'dot' },
        xaxis: 'x',
        yaxis: 'y',
        hovertemplate: 'Long avg %{y:.2f}<extra></extra>',
      },
      {
        type: 'scatter',
        mode: 'lines',
        name: 'Drawdown',
        x: graph.labels,
        y: graph.drawdown,
        // Also connected: drawdown is a running measure against the peak, and
        // it does not reset over a weekend.
        connectgaps: true,
        line: { color: C.claret, width: 1.6 },
        fill: 'tozeroy',
        fillcolor: 'rgba(153, 15, 61, 0.14)',
        xaxis: 'x2',
        yaxis: 'y2',
        hovertemplate: 'Drawdown %{y:.1f}%<extra></extra>',
      },
    ],
    [graph],
  );

  const figure = useMemo(
    () =>
      layout({
        legend: { orientation: 'h', x: 0, y: 1.07, font: { size: 10 } },
        xaxis: { domain: [0, 1], anchor: 'y', showticklabels: false },
        yaxis: {
          domain: [0.34, 1],
          title: { text: `Close · ${graph.currency ?? ''}`.trim(), font: { size: 11 } },
        },
        xaxis2: { domain: [0, 1], anchor: 'y2', gridcolor: 'rgba(0,0,0,0)', tickfont: { size: 10, color: C.inkMuted } },
        yaxis2: {
          domain: [0, 0.23],
          title: { text: 'Drawdown', font: { size: 11 } },
          ticksuffix: '%',
          gridcolor: C.ruleLight,
          zeroline: true,
          zerolinecolor: C.rule,
        },
        margin: { l: 64, r: 20, t: 36, b: 48 },
      }),
    [graph.currency],
  );

  if (!hasAny(graph.close)) {
    return <Empty height={420}>No price history for this stock in this window.</Empty>;
  }
  return <Chart data={data} layout={figure} height={420} label="Close, moving averages and drawdown" />;
};

export { GraphCard };
