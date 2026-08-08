import { ArrowLeft } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import GraphCard from '@/components/graphs/GraphCard';
import {
  CandlestickChart,
  DistributionChart,
  ReactionChart,
  TrendRiskChart,
} from '@/components/graphs/StockCharts';
import {
  CrossSectionChart,
  DivergenceChart,
  SentimentHeatmap,
} from '@/components/graphs/UniverseCharts';
import { API_BASE_URL } from '@/config/api';
import { useStockGraph, useUniverseGraph } from '@/hooks/useGraphs';
import { useWatchlist } from '@/hooks/useWatchlist';
import { formatPercent, formatSentiment } from '@/lib/format';
import { linkProps } from '@/lib/router';
import { BUCKET_NOUN, DEFAULT_DAYS, TIMEFRAMES } from '@/types/graphs';

/** "2026-08-02" → "2 Aug 2026". Dates from the API are already UTC days. */
const readableDate = (iso: string | null): string => {
  if (!iso) return '—';
  const [year, month, day] = iso.split('-').map(Number);
  return `${day} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct',
    'Nov', 'Dec'][month - 1]} ${year}`;
};

const Metric = ({ label, value, note, tone }: {
  label: string; value: string; note: string; tone?: string;
}) => (
  <div className="border-l-2 border-rule pl-3">
    <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-muted">{label}</p>
    <p className={`mt-1 font-serif text-2xl font-semibold ${tone ?? 'text-ink-strong'}`}>{value}</p>
    <p className="mt-0.5 text-[11px] text-ink-muted">{note}</p>
  </div>
);

/**
 * The graphs desk.
 *
 * Seven charts in two halves: four that interrogate one instrument, three that
 * put every followed instrument beside each other. The stock and timeframe
 * pickers drive both — except where a chart is inherently cross-sectional, in
 * which case filtering it to one stock would leave nothing to compare.
 */
const Graphs = () => {
  const { watchlist, error } = useWatchlist();
  const [days, setDays] = useState<number>(DEFAULT_DAYS);
  const [symbol, setSymbol] = useState<string | null>(null);

  // The watchlist arrives a beat after the first paint, so the default lands
  // once rather than being pinned to whatever was first in an empty array.
  useEffect(() => {
    if (!symbol && watchlist.length > 0) setSymbol(watchlist[0].symbol);
  }, [symbol, watchlist]);

  const { data: stock, isLoading: loadingStock } = useStockGraph(symbol, days);
  const { data: universe, isLoading: loadingUniverse } = useUniverseGraph(days);

  const bucketNoun = stock ? BUCKET_NOUN[stock.bucket] : 'bucket';
  const dataThrough = stock?.dataThrough ?? universe?.dataThrough ?? null;

  const returnNote = useMemo(() => {
    if (!stock?.returnFrom || !stock.returnTo) return 'No stored price history';
    return `${readableDate(stock.returnFrom)} → ${readableDate(stock.returnTo)}`;
  }, [stock]);

  return (
    <div className="min-h-screen bg-paper">
      <header className="border-b border-rule bg-paper">
        <div className="mx-auto flex h-12 max-w-[1600px] items-center gap-3 px-3 md:px-5">
          <a
            {...linkProps('/')}
            className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink-strong"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Back to the front page
          </a>
          <span className="ml-auto shrink-0 border-l border-rule pl-3 font-serif text-base font-bold tracking-tight md:text-lg">
            Graphs
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-3 py-6 md:px-5">
        {error && (
          <div className="mb-6 border-l-4 border-down bg-paper-tint px-4 py-3 text-sm">
            <p className="font-semibold text-ink-strong">Cannot reach the Stocky API</p>
            <p className="mt-1 text-ink-muted">
              Expected it at <code className="font-mono">{API_BASE_URL}</code>.
            </p>
          </div>
        )}

        <section className="ft-section">
          <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-ftblue">
            Price and sentiment intelligence
          </p>
          <h1 className="ft-headline mt-2 text-4xl leading-none md:text-5xl">
            Price tells you what. Sentiment asks why.
          </h1>
          <p className="mt-3 max-w-3xl text-sm leading-relaxed text-ink-muted">
            Seven views over the stocks you follow and the coverage scraped for them.
            Everything here is your own stored data — daily closes, and every article
            the scraper scored on a −1 to +1 scale. Nothing is modelled, and nothing
            is advice.
          </p>
        </section>

        {/* Deliberately loud: the grid always runs to today, and the scraper's
            last run is usually earlier. Without this the empty right-hand end
            of every chart reads as a rendering bug. */}
        <div className="mt-4 border-l-4 border-ftblue bg-paper-tint px-4 py-2.5 font-mono text-[11px] leading-relaxed text-ink-muted">
          <strong className="text-ink-strong">Data through {readableDate(dataThrough)}.</strong>{' '}
          Charts run to today, so any gap at the right-hand edge is coverage the scraper
          has not collected yet, not missing rendering.
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-x-6 gap-y-3 border-y border-rule py-3">
          <label className="flex items-center gap-2 text-xs text-ink-muted">
            <span className="font-mono uppercase tracking-[0.08em]">Stock</span>
            <select
              value={symbol ?? ''}
              onChange={(event) => setSymbol(event.target.value)}
              className="border border-rule bg-paper px-2 py-1 font-mono text-xs text-ink-strong"
            >
              {watchlist.length === 0 && <option value="">No stocks followed</option>}
              {watchlist.map((s) => (
                <option key={s.symbol} value={s.symbol}>
                  {s.symbol}
                  {s.name ? ` — ${s.name}` : ''}
                </option>
              ))}
            </select>
          </label>

          <div className="flex items-center gap-2 text-xs text-ink-muted">
            <span className="font-mono uppercase tracking-[0.08em]">Timeframe</span>
            <div className="flex border border-rule">
              {TIMEFRAMES.map((frame) => (
                <button
                  key={frame.days}
                  type="button"
                  onClick={() => setDays(frame.days)}
                  aria-pressed={days === frame.days}
                  className={`border-r border-rule px-2.5 py-1 font-mono text-[11px] last:border-r-0 ${
                    days === frame.days
                      ? 'bg-ink-strong text-paper'
                      : 'text-ink hover:bg-paper-tint'
                  }`}
                >
                  {frame.label}
                </button>
              ))}
            </div>
          </div>

          {stock && (
            <p className="ft-meta">
              One column = one {bucketNoun}. {stock.buckets.length} columns.
            </p>
          )}
        </div>

        {stock && (
          <section className="mt-5 grid grid-cols-2 gap-4 md:grid-cols-4">
            <Metric
              label="Return"
              value={formatPercent(stock.periodReturn)}
              note={returnNote}
              tone={
                stock.periodReturn === null
                  ? 'text-ink-muted'
                  : stock.periodReturn >= 0
                    ? 'text-up'
                    : 'text-down'
              }
            />
            <Metric
              label="Max drawdown"
              value={stock.maxDrawdown === null ? '—' : `${stock.maxDrawdown.toFixed(1)}%`}
              note={`Peak to trough, by ${bucketNoun}`}
            />
            <Metric
              label="Price / sentiment"
              value={stock.correlation === null ? '—' : `r = ${stock.correlation.toFixed(2)}`}
              note={`Contemporaneous, per ${bucketNoun}`}
            />
            <Metric
              label="Articles scored"
              value={stock.totalArticles.toLocaleString()}
              note="In this timeframe"
            />
          </section>
        )}

        {(loadingStock || loadingUniverse) && (
          <p className="mt-6 font-mono text-xs text-ink-muted">Loading series…</p>
        )}

        {stock && (
          <>
            <section className="mt-10">
              <h2 className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                One stock in depth — {stock.symbol}
                {stock.name ? ` · ${stock.name}` : ''}
              </h2>
            </section>

            <div className="mt-4 space-y-10">
              <GraphCard
                index="01"
                kicker="Sentiment distribution"
                title="The average hides how divided the coverage was"
                blurb={`Every scored article for ${stock.symbol}, grouped by ${bucketNoun}. The box spans the middle half, the line is the median, and the dots are outliers.`}
                note="detecting polarisation. A score near zero can mean genuinely neutral coverage, or an even split between strongly positive and strongly negative articles — an average cannot tell you which."
              >
                <DistributionChart graph={stock} />
              </GraphCard>

              <GraphCard
                index="02"
                kicker="Reaction timeline"
                title="Closing price, coverage volume and sentiment on one axis"
                blurb={`Sentiment bars sit behind the close, and each marker grows with the number of articles published that ${bucketNoun}.`}
                caveat="Marker size = articles"
                note="investigating event windows. Each measure keeps its own unit and its own axis, so nothing implies that a currency and a sentiment score are comparable quantities."
              >
                <ReactionChart graph={stock} />
              </GraphCard>

              <GraphCard
                index="06"
                kicker="Range and coverage"
                title="What the closing price alone does not show"
                blurb={`Open, high, low and close per ${BUCKET_NOUN[stock.bars.bucket]}, with articles published in the panel below.`}
                caveat="Close-derived range"
                note="seeing whether a move was steady or violent. The range is built from the closes and hourly snapshots actually stored, not from an exchange feed, and the lower panel counts articles — Stocky stores no share volume."
              >
                <CandlestickChart graph={stock} />
              </GraphCard>

              <GraphCard
                index="07"
                kicker="Trend and risk"
                title="Moving averages frame the trend; drawdown measures the damage"
                blurb={`Close with ${stock.maShortWindow}- and ${stock.maLongWindow}-${bucketNoun} trailing averages above, and the fall from the running peak below.`}
                note="separating ordinary volatility from a sustained break. Each average is absent until its full window exists, so an early point is never a short average wearing a long label."
              >
                <TrendRiskChart graph={stock} />
              </GraphCard>
            </div>
          </>
        )}

        {universe && (
          <>
            <section className="mt-14">
              <h2 className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                Every stock you follow — {universe.stocks.length} instruments
              </h2>
            </section>

            <div className="mt-4 space-y-10">
              <GraphCard
                index="03"
                kicker="Sentiment heatmap"
                title="Coverage rotates between holdings rather than moving as one"
                blurb={`Sentiment per ${bucketNoun} for every followed stock, sorted by the most recent reading. Blank cells are ${bucketNoun}s with no coverage at all.`}
                note="scanning for clusters, reversals and persistent outliers across the whole watchlist at once. Click a row to load that stock above."
              >
                <SentimentHeatmap graph={universe} onSelect={setSymbol} />
              </GraphCard>

              <div className="grid gap-10 lg:grid-cols-2">
                <GraphCard
                  index="04"
                  kicker="Latest cross-section"
                  title="Positive coverage does not always mean a positive return"
                  blurb="Latest sentiment against the return over the price history stored for each stock, with bubble size as article count."
                  caveat="Bubble = articles"
                  note="separating stocks where price and coverage agree from the ones where they have come apart. Only stocks with both a price history and scored coverage appear."
                >
                  <CrossSectionChart graph={universe} />
                </GraphCard>

                <GraphCard
                  index="05"
                  kicker="Ranked divergence"
                  title="Which narratives moved furthest from the price?"
                  blurb="Sentiment change and price return, each standardised across the watchlist, then subtracted."
                  note="prioritising what to read next. A large positive score means coverage improved faster than the price did; a negative one means the price ran ahead of the coverage."
                >
                  <DivergenceChart graph={universe} />
                </GraphCard>
              </div>
            </div>
          </>
        )}

        {!loadingStock && watchlist.length === 0 && (
          <p className="mt-10 border border-dashed border-rule px-4 py-8 text-center text-sm text-ink-muted">
            Follow a stock on the front page and its charts appear here.
          </p>
        )}
      </main>

      <footer className="mt-10 border-t border-rule">
        <div className="mx-auto max-w-[1600px] px-3 py-6 md:px-5">
          <p className="ft-meta">
            Stock Pulse · every series drawn from your own scraped prices and scored articles ·
            not investment advice
          </p>
        </div>
      </footer>
    </div>
  );
};

export default Graphs;
