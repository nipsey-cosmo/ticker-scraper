"""Series for the /graphs page.

Two endpoints, one per question the page asks:

  /graphs/stock/{symbol}  — one instrument in depth (distribution, reaction,
                            OHLC, trend and risk)
  /graphs/universe        — every followed instrument side by side (heatmap,
                            cross-section, divergence)

Both answer on a *shared bucket grid*: one list of bucket start dates, and
every series index-aligned to it, nulls included. The frontend does no
arithmetic and no date handling at all — it maps arrays onto axes. That is
deliberate. Every quantity here that is easy to get subtly wrong (weighting a
week's mean by article count, a trailing average that must not report a
partial window as a full one, drawdown from the running peak, z-scored
divergence) is computed once, in one place, under test.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

import db
from normalize import days_ago_iso

from .schemas import BarSeries, StockGraph, UniverseGraph, UniverseStock, require_symbol

router = APIRouter(prefix="/graphs", tags=["Graphs"])

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Trailing-average windows, in buckets. The example dashboard called these
# "3-week" and "6-week" because its grid happened to be weekly; here the grid
# changes with the timeframe, so they are 3 and 6 *buckets* and the UI labels
# them from the bucket it was told about.
_MA_SHORT, _MA_LONG = 3, 6


# ── Bucketing ────────────────────────────────────────────────────────────

def _bar_bucket_for(bucket: str) -> str:
    """The candle grid: never finer than a week.

    A daily candle from this data has no range to draw — see BarSeries. On a
    weekly or monthly page grid the candles simply share it.
    """
    return "week" if bucket == "day" else bucket


def _bucket_for(days: int) -> str:
    """Roughly 12-30 buckets across any timeframe, which is what these chart
    types read well at: a box plot per day over a year is unreadable, and a
    box plot per month over a week has one box."""
    if days <= 30:
        return "day"
    if days <= 180:
        return "week"
    return "month"


def _bucket_start(day: str, bucket: str) -> str:
    """'2026-07-17' → the start of the bucket containing it.

    Weeks start Monday, matching Postgres' `date_trunc('week')` — worth keeping
    aligned even though the grouping now happens here, because anything that
    later moves back into SQL must land on the same boundaries.
    """
    if bucket == "day":
        return day
    d = date.fromisoformat(day)
    if bucket == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    return d.replace(day=1).isoformat()


def _label(bucket_start: str, bucket: str) -> str:
    d = date.fromisoformat(bucket_start)
    if bucket == "month":
        return f"{_MONTHS[d.month - 1]} {d.year % 100:02d}"
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _grid(days: int, bucket: str) -> list[str]:
    """Every bucket in the window, including the empty ones.

    Built from the calendar rather than from the rows returned, so a stock with
    no coverage on a Tuesday still gets a Tuesday — otherwise two stocks in the
    heatmap would silently be drawn on different grids.
    """
    today = date.fromisoformat(days_ago_iso(0)[:10])
    start = today - timedelta(days=days - 1)
    seen, out = set(), []
    for offset in range((today - start).days + 1):
        key = _bucket_start((start + timedelta(days=offset)).isoformat(), bucket)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _index_of(grid: list[str]) -> dict[str, int]:
    return {key: i for i, key in enumerate(grid)}


# ── Maths ────────────────────────────────────────────────────────────────

def _trailing_mean(values: list[float | None], window: int) -> list[float | None]:
    """Trailing average over the last `window` *observations*, not the last
    `window` calendar buckets.

    Two rules, and both matter:

    Empty buckets are skipped rather than breaking the window. A daily grid is
    mostly weekends and holidays — structurally empty, not missing — and
    counting them makes a 6-day average that can never be computed, because no
    six consecutive calendar days are all trading days. "6-day moving average"
    has always meant six sessions.

    The average is still absent until six sessions exist. The reference
    dashboard averaged whatever it had, so its "6-week average" began life as a
    1-week average wearing a 6-week label.
    """
    out: list[float | None] = [None] * len(values)
    observed: list[float] = []
    for i, value in enumerate(values):
        if value is None:
            continue
        observed.append(value)
        if len(observed) >= window:
            out[i] = sum(observed[-window:]) / window
    return out


def _drawdown(closes: list[float | None]) -> list[float | None]:
    """Percent below the running peak. Carries the peak across gaps, so a
    missing bucket doesn't reset the high-water mark."""
    out: list[float | None] = []
    peak: float | None = None
    for close in closes:
        if close is None:
            out.append(None)
            continue
        peak = close if peak is None else max(peak, close)
        out.append((close / peak - 1) * 100)
    return out


def _z_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    sd = variance ** 0.5
    # A universe where every stock moved identically has no spread to divide
    # by; every divergence is then genuinely 0, not infinite.
    return [0.0 for _ in values] if sd == 0 else [(v - mean) / sd for v in values]


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / den if den else None


def _period_return(closes_by_day: dict[str, float]) -> tuple[float | None, str | None, str | None]:
    """Return over the days actually observed, and the two days it spans.

    The dates come back with the number because they routinely disagree with
    the timeframe asked for: prices are backfilled 30 days on follow, so a
    90-day view of a stock followed last month holds a month of prices. Calling
    that "90-day return" would overstate what the number covers, so the caller
    is handed the real window and labels the figure with it.
    """
    days = sorted(closes_by_day)
    if not days:
        return None, None, None
    first, last = closes_by_day[days[0]], closes_by_day[days[-1]]
    if len(days) < 2 or not first:
        return None, days[0], days[-1]
    return (last / first - 1) * 100, days[0], days[-1]


def _data_through(closes_by_day: dict[str, float], sentiment_rows: list[dict]) -> str | None:
    """The most recent day with anything in it.

    Surfaced because the grid always runs to today while the data does not: if
    the scraper last ran on Tuesday, the right-hand end of every chart is
    genuinely empty, and a page that doesn't say so looks broken instead of
    looking stale.
    """
    days = list(closes_by_day) + [r["day"] for r in sentiment_rows]
    return max(days) if days else None


# ── Shaping ──────────────────────────────────────────────────────────────

def _bars(days: list[dict], grid: list[str], bucket: str) -> dict:
    """Roll daily OHLC up into the page's buckets.

    Open is the first day's open and close the last day's close, but high and
    low come from the days' own highs and lows rather than from their closes —
    so a week that spiked on Wednesday and gave it all back keeps the spike.
    Taking the range from closes alone would quietly flatten exactly the weeks
    worth looking at.
    """
    grouped: dict[str, list[dict]] = {}
    for row in sorted(days, key=lambda r: r["day"]):
        grouped.setdefault(_bucket_start(row["day"], bucket), []).append(row)

    out: dict[str, list[float | None]] = {k: [] for k in ("open", "high", "low", "close")}
    for key in grid:
        rows_in_bucket = grouped.get(key)
        out["open"].append(rows_in_bucket[0]["open"] if rows_in_bucket else None)
        out["close"].append(rows_in_bucket[-1]["close"] if rows_in_bucket else None)
        out["high"].append(max(r["high"] for r in rows_in_bucket) if rows_in_bucket else None)
        out["low"].append(min(r["low"] for r in rows_in_bucket) if rows_in_bucket else None)
    return out


def _bucketed_sentiment(rows: list[dict], grid: list[str], bucket: str) -> dict:
    """Article-weighted mean per bucket, plus the counts behind it.

    Weighted, not the mean of the daily means: a day with 200 articles and a
    day with 3 do not get an equal say in the week. The unweighted version is
    the easy mistake here and it visibly moves the heatmap.
    """
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        key = _bucket_start(row["day"], bucket)
        acc = totals.setdefault(key, {"sum": 0.0, "articles": 0,
                                      "positive": 0, "negative": 0, "neutral": 0})
        # avg × n recovers the bucket's score total, which is what a weighted
        # mean needs; the row itself only carries the daily average.
        acc["sum"] += float(row["avg_sentiment"]) * row["articles"]
        for field in ("articles", "positive", "negative", "neutral"):
            acc[field] += row[field]

    out: dict[str, list] = {k: [] for k in
                            ("sentiment", "articles", "positive", "negative", "neutral")}
    for key in grid:
        acc = totals.get(key)
        out["sentiment"].append(acc["sum"] / acc["articles"] if acc and acc["articles"] else None)
        for field in ("articles", "positive", "negative", "neutral"):
            out[field].append(int(acc[field]) if acc else 0)
    return out


# ── Endpoints ────────────────────────────────────────────────────────────

DaysQuery = Query(30, ge=7, le=365)


@router.get("/stock/{symbol}", response_model=StockGraph)
def stock_graph(symbol: str, days: int = DaysQuery):
    """One instrument: price structure and the coverage around it."""
    symbol = require_symbol(symbol)
    row = db.stocks.get_stock(symbol)
    if not row:
        raise HTTPException(status_code=404, detail="Stock not found")

    since = days_ago_iso(days)
    bucket = _bucket_for(days)
    grid = _grid(days, bucket)
    at = _index_of(grid)

    price_days = db.graphs.daily_ohlc([symbol], since)
    closes_by_day = {r["day"]: r["close"] for r in price_days}
    sentiment_rows = db.graphs.daily_sentiment([symbol], since)
    page_bars = _bars(price_days, grid, bucket)
    sentiment = _bucketed_sentiment(sentiment_rows, grid, bucket)

    # The candles get their own, coarser grid — and their own coverage counts,
    # because the lower panel has to add up to the candle above it.
    bar_bucket = _bar_bucket_for(bucket)
    bar_grid = _grid(days, bar_bucket)
    candles = _bars(price_days, bar_grid, bar_bucket)
    bar_series = BarSeries(
        bucket=bar_bucket,
        buckets=bar_grid,
        labels=[_label(key, bar_bucket) for key in bar_grid],
        articles=_bucketed_sentiment(sentiment_rows, bar_grid, bar_bucket)["articles"],
        **candles,
    )

    # Raw per-article scores, dropped into their bucket for the box plots.
    samples: list[list[float]] = [[] for _ in grid]
    for score in db.graphs.article_scores(symbol, since):
        index = at.get(_bucket_start(score["day"], bucket))
        if index is not None:
            samples[index].append(score["sentiment"])

    closes = page_bars["close"]
    drawdown = _drawdown(closes)

    # Correlation pairs bucket-over-bucket price change with that bucket's
    # sentiment, and only where both exist — a bucket with no coverage is not
    # a bucket with neutral coverage.
    returns_x, sentiment_y = [], []
    previous: float | None = None
    for index, close in enumerate(closes):
        if close is not None and previous is not None and sentiment["sentiment"][index] is not None:
            returns_x.append((close / previous - 1) * 100)
            sentiment_y.append(sentiment["sentiment"][index])
        if close is not None:
            previous = close

    present_drawdown = [d for d in drawdown if d is not None]
    period_return, return_from, return_to = _period_return(closes_by_day)

    return StockGraph(
        symbol=symbol,
        name=row.get("name"),
        currency=row.get("quote_currency") or row.get("currency_code"),
        bucket=bucket,
        buckets=grid,
        labels=[_label(key, bucket) for key in grid],
        close=closes,
        bars=bar_series,
        maShort=_trailing_mean(closes, _MA_SHORT),
        maLong=_trailing_mean(closes, _MA_LONG),
        maShortWindow=_MA_SHORT,
        maLongWindow=_MA_LONG,
        drawdown=drawdown,
        sentiment=sentiment["sentiment"],
        articles=sentiment["articles"],
        positive=sentiment["positive"],
        negative=sentiment["negative"],
        neutral=sentiment["neutral"],
        samples=samples,
        periodReturn=period_return,
        returnFrom=return_from,
        returnTo=return_to,
        maxDrawdown=min(present_drawdown) if present_drawdown else None,
        correlation=_correlation(returns_x, sentiment_y),
        totalArticles=sum(sentiment["articles"]),
        dataThrough=_data_through(closes_by_day, sentiment_rows),
    )


@router.get("/universe", response_model=UniverseGraph)
def universe_graph(days: int = DaysQuery):
    """Every followed instrument on one grid.

    Scoped to the watchlist, not to everything in `news`: coverage lingers for
    stocks that were followed once and dropped, and a heatmap row for a stock
    the user no longer holds is noise rather than intelligence.
    """
    watched = db.watchlist.get_watchlist()
    symbols = [w["short_name"] for w in watched]

    since = days_ago_iso(days)
    bucket = _bucket_for(days)
    grid = _grid(days, bucket)

    if not symbols:
        return UniverseGraph(bucket=bucket, buckets=grid,
                             labels=[_label(k, bucket) for k in grid], stocks=[])

    # Only the closes matter here: the universe charts rank stocks by what the
    # period did to them, and no view on this side draws a bar.
    closes: dict[str, dict[str, float]] = {}
    for row in db.graphs.daily_ohlc(symbols, since):
        closes.setdefault(row["short_name"], {})[row["day"]] = row["close"]

    sentiment_rows: dict[str, list[dict]] = {}
    for row in db.graphs.daily_sentiment(symbols, since):
        sentiment_rows.setdefault(row["short_name"], []).append(row)

    stocks: list[UniverseStock] = []
    for watch in watched:
        symbol = watch["short_name"]
        series = _bucketed_sentiment(sentiment_rows.get(symbol, []), grid, bucket)
        present = [v for v in series["sentiment"] if v is not None]
        period_return, return_from, return_to = _period_return(closes.get(symbol, {}))
        stocks.append(UniverseStock(
            symbol=symbol,
            name=watch.get("name"),
            sector=watch.get("industry") or watch.get("sector"),
            sentiment=series["sentiment"],
            articles=series["articles"],
            latestSentiment=present[-1] if present else None,
            sentimentChange=present[-1] - present[0] if len(present) > 1 else None,
            periodReturn=period_return,
            returnFrom=return_from,
            returnTo=return_to,
            totalArticles=sum(series["articles"]),
        ))

    # Divergence is only meaningful relative to the rest of the universe, so it
    # is standardised across the stocks that have both numbers. Stocks missing
    # either are left at None rather than being scored against a mean they did
    # not contribute to.
    scorable = [s for s in stocks
                if s.sentimentChange is not None and s.periodReturn is not None]
    if len(scorable) > 1:
        z_sentiment = _z_scores([s.sentimentChange for s in scorable])
        z_return = _z_scores([s.periodReturn for s in scorable])
        for stock, zs, zr in zip(scorable, z_sentiment, z_return):
            stock.divergence = zs - zr

    all_days = [d for days_by_stock in closes.values() for d in days_by_stock]
    all_days += [r["day"] for stock_rows in sentiment_rows.values() for r in stock_rows]

    return UniverseGraph(
        bucket=bucket,
        buckets=grid,
        labels=[_label(key, bucket) for key in grid],
        stocks=stocks,
        dataThrough=max(all_days) if all_days else None,
    )
