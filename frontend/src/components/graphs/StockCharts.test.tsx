import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { StockGraph, UniverseGraph } from '@/types/graphs';

import {
  CandlestickChart,
  DistributionChart,
  ReactionChart,
  TrendRiskChart,
} from './StockCharts';
import { CrossSectionChart, DivergenceChart, SentimentHeatmap } from './UniverseCharts';

/**
 * Every chart against data it will genuinely be handed.
 *
 * The empty cases are not hypothetical: the timeframe grid always runs to
 * today while the scraper's last run is usually earlier, so a 7-day view of a
 * database last filled a week ago has no prices and no articles at all. That
 * used to be the shape most likely to throw inside a Plotly trace builder,
 * where the failure is a blank page rather than an error anyone sees.
 */

// Plotly draws to a real canvas/SVG and does nothing useful in jsdom. What
// matters here is that the trace builders run without throwing and that the
// empty branches short-circuit before reaching them.
vi.mock('@/lib/plotly', () => ({
  default: { react: vi.fn(), purge: vi.fn(), register: vi.fn(), Plots: { resize: vi.fn() } },
}));

const filled = (n: number, value: number | null) => Array.from({ length: n }, () => value);

const stockGraph = (overrides: Partial<StockGraph> = {}): StockGraph => ({
  symbol: 'NVDA',
  name: 'Nvidia',
  currency: 'USD',
  bucket: 'day',
  buckets: ['2026-07-29', '2026-07-30', '2026-07-31'],
  labels: ['29 Jul', '30 Jul', '31 Jul'],
  close: [190.01, 195.04, 200.75],
  bars: {
    bucket: 'week',
    buckets: ['2026-07-27'],
    labels: ['27 Jul'],
    open: [196.51],
    high: [200.75],
    low: [190.01],
    close: [200.75],
    articles: [337],
  },
  maShort: [null, null, 195.26],
  maLong: filled(3, null),
  maShortWindow: 3,
  maLongWindow: 6,
  drawdown: [0, 0, 0],
  sentiment: [0.11, -0.04, 0.2],
  articles: [108, 104, 125],
  positive: [40, 30, 50],
  negative: [20, 34, 25],
  neutral: [48, 40, 50],
  samples: [[0.4, -0.2], [0.1], [0.3, 0.5, -0.1]],
  periodReturn: 5.65,
  returnFrom: '2026-07-29',
  returnTo: '2026-07-31',
  maxDrawdown: 0,
  correlation: 0.42,
  totalArticles: 337,
  dataThrough: '2026-07-31',
  ...overrides,
});

/** What the API really returns for a window that ends after the last scrape. */
const emptyStock = (): StockGraph =>
  stockGraph({
    close: filled(3, null),
    bars: {
      bucket: 'week',
      buckets: ['2026-07-27'],
      labels: ['27 Jul'],
      open: [null],
      high: [null],
      low: [null],
      close: [null],
      articles: [0],
    },
    maShort: filled(3, null),
    maLong: filled(3, null),
    drawdown: filled(3, null),
    sentiment: filled(3, null),
    articles: [0, 0, 0],
    positive: [0, 0, 0],
    negative: [0, 0, 0],
    neutral: [0, 0, 0],
    samples: [[], [], []],
    periodReturn: null,
    returnFrom: null,
    returnTo: null,
    maxDrawdown: null,
    correlation: null,
    totalArticles: 0,
    dataThrough: null,
  });

const universeGraph = (overrides: Partial<UniverseGraph> = {}): UniverseGraph => ({
  bucket: 'day',
  buckets: ['2026-07-30', '2026-07-31'],
  labels: ['30 Jul', '31 Jul'],
  dataThrough: '2026-07-31',
  stocks: [
    {
      symbol: 'NVDA', name: 'Nvidia', sector: 'Semiconductors',
      sentiment: [0.1, 0.2], articles: [10, 12], latestSentiment: 0.2,
      sentimentChange: 0.1, periodReturn: 3.4, returnFrom: '2026-07-30',
      returnTo: '2026-07-31', divergence: -0.31, totalArticles: 22,
    },
    {
      symbol: 'MSFT', name: 'Microsoft', sector: 'Software',
      sentiment: [-0.1, null], articles: [4, 0], latestSentiment: -0.1,
      sentimentChange: -0.2, periodReturn: 20.68, returnFrom: '2026-07-30',
      returnTo: '2026-07-31', divergence: -2.12, totalArticles: 4,
    },
  ],
  ...overrides,
});

describe('single-stock charts', () => {
  it('render with real data', () => {
    for (const Chart of [DistributionChart, ReactionChart, CandlestickChart, TrendRiskChart]) {
      const { unmount } = render(<Chart graph={stockGraph()} />);
      expect(screen.getByRole('img')).toBeInTheDocument();
      unmount();
    }
  });

  it('explain themselves rather than drawing an empty plot when there is no data', () => {
    render(<DistributionChart graph={emptyStock()} />);
    expect(screen.getByText(/no scored coverage/i)).toBeInTheDocument();
  });

  it('tell the reader the candles need a stored close', () => {
    render(<CandlestickChart graph={emptyStock()} />);
    expect(screen.getByText(/need at least one stored close/i)).toBeInTheDocument();
  });

  it('do not claim a trend when no price exists', () => {
    render(<TrendRiskChart graph={emptyStock()} />);
    expect(screen.getByText(/no price history/i)).toBeInTheDocument();
  });

  it('label the moving averages with the window and the bucket the API chose', () => {
    // A "3-day average" on a monthly grid would be a lie; the label is built
    // from maShortWindow and bucket rather than hardcoded.
    const { container } = render(
      <TrendRiskChart graph={stockGraph({ bucket: 'month', maShortWindow: 4 })} />,
    );
    expect(container.querySelector('[role="img"]')).toBeInTheDocument();
  });
});

describe('universe charts', () => {
  it('render with real data', () => {
    for (const Chart of [SentimentHeatmap, CrossSectionChart, DivergenceChart]) {
      const { unmount } = render(<Chart graph={universeGraph()} />);
      expect(screen.getByRole('img')).toBeInTheDocument();
      unmount();
    }
  });

  it('drop stocks with no coverage from the heatmap instead of drawing a blank row', () => {
    const graph = universeGraph();
    graph.stocks[1] = { ...graph.stocks[1], totalArticles: 0, articles: [0, 0] };
    render(<SentimentHeatmap graph={graph} />);
    expect(screen.getByRole('img')).toBeInTheDocument();
  });

  it('says so when nothing can be ranked rather than showing one bar', () => {
    const graph = universeGraph();
    graph.stocks = graph.stocks.map((s) => ({ ...s, divergence: null }));
    render(<DivergenceChart graph={graph} />);
    expect(screen.getByText(/at least two stocks/i)).toBeInTheDocument();
  });

  it('needs both a price and a score to place a point', () => {
    const graph = universeGraph();
    graph.stocks = graph.stocks.map((s) => ({ ...s, periodReturn: null }));
    render(<CrossSectionChart graph={graph} />);
    expect(screen.getByText(/neither is stored yet/i)).toBeInTheDocument();
  });

  it('handles an empty watchlist', () => {
    render(<SentimentHeatmap graph={universeGraph({ stocks: [] })} />);
    expect(screen.getByText(/no scored coverage/i)).toBeInTheDocument();
  });
});
