/** Mirrors StockGraph / UniverseGraph in routes/schemas.py. */

/** How wide one column of the grid is. Chosen by the backend from the timeframe. */
export type Bucket = 'day' | 'week' | 'month';

/**
 * One instrument on a bucket grid.
 *
 * Every array below is index-aligned to `buckets`, nulls included, so a chart
 * maps arrays onto axes without joining anything on a date. The backend owns
 * all the arithmetic; nothing here is recomputed in the client.
 */
/**
 * Candles for the OHLC chart, on their own grid.
 *
 * Separate from the page grid because a candle needs a range to draw, and a
 * daily candle here has none: outside the last day or two there is one
 * observation per day stored, so open, high, low and close are the same number
 * and every candle is a flat dash.
 */
export interface BarSeries {
  bucket: Bucket;
  buckets: string[];
  labels: string[];
  open: (number | null)[];
  high: (number | null)[];
  low: (number | null)[];
  close: (number | null)[];
  /** The lower panel: articles published, not share volume — none is stored. */
  articles: number[];
}

export interface StockGraph {
  symbol: string;
  name: string | null;
  currency: string | null;
  bucket: Bucket;
  /** Bucket start dates, YYYY-MM-DD. */
  buckets: string[];
  /** The same dates, pre-formatted for an axis. */
  labels: string[];
  close: (number | null)[];
  bars: BarSeries;
  maShort: (number | null)[];
  maLong: (number | null)[];
  /** Averaging windows in buckets, so the legend can name them accurately. */
  maShortWindow: number;
  maLongWindow: number;
  /** Percent below the running peak. */
  drawdown: (number | null)[];
  /** Article-weighted mean, −1..+1. Null means no coverage, not neutral coverage. */
  sentiment: (number | null)[];
  articles: number[];
  positive: number[];
  negative: number[];
  neutral: number[];
  /** Every scored article's own value, per bucket — the distribution chart. */
  samples: number[][];
  periodReturn: number | null;
  /** The days periodReturn actually spans, which is often narrower than the timeframe. */
  returnFrom: string | null;
  returnTo: string | null;
  maxDrawdown: number | null;
  correlation: number | null;
  totalArticles: number;
  /** Most recent day holding a price or an article. The grid runs to today. */
  dataThrough: string | null;
}

export interface UniverseStock {
  symbol: string;
  name: string | null;
  sector: string | null;
  sentiment: (number | null)[];
  articles: number[];
  latestSentiment: number | null;
  sentimentChange: number | null;
  periodReturn: number | null;
  returnFrom: string | null;
  returnTo: string | null;
  /** z(sentiment change) − z(return) across the universe. */
  divergence: number | null;
  totalArticles: number;
}

export interface UniverseGraph {
  bucket: Bucket;
  buckets: string[];
  labels: string[];
  stocks: UniverseStock[];
  dataThrough: string | null;
}

/** The timeframes offered in the toolbar. */
export const TIMEFRAMES = [
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
  { days: 365, label: '1 year' },
] as const;

export const DEFAULT_DAYS = 30;

/** What one column covers, for axis and footnote copy. */
export const BUCKET_NOUN: Record<Bucket, string> = {
  day: 'day',
  week: 'week',
  month: 'month',
};
