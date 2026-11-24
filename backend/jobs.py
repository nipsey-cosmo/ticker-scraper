"""Background work: refreshes, the on-follow backfill, and rollups.

Everything here is safe to run concurrently with the API and is idempotent, so
a job that dies halfway can simply be run again.
"""
from __future__ import annotations

import threading
import time

import db
import settings
from normalize import days_ago_iso, markets_open
from services import calendar, events, news, prices, sentiment_service, social

# One backfill per symbol at a time. Adding a stock twice in quick succession
# would otherwise fan out two identical month-long scrapes.
_backfilling: set[str] = set()
_backfill_lock = threading.Lock()

# Monotonic stamp of the last full chart pass, for the hourly cadence. Minus
# infinity, not 0.0: the monotonic clock can start near zero, so a machine up
# for less than an hour would otherwise skip the start-up pass when shut.
_last_price_refresh = float("-inf")

# A fortnight of 5m bars. Long enough that every event reaches its attribution
# window with bars still on disk, short enough that the table stays small.
INTRADAY_RETENTION_DAYS = 14


# ── Scheduled ────────────────────────────────────────────────────────────

def refresh_quotes() -> dict:
    """Every minute. Spark quotes while a market is open; nothing otherwise."""
    return prices.refresh_watchlist_quotes()



def refresh_prices() -> dict:
    """The chart chain, on a five-minute tick.

    Once an hour, open or shut, it is the full pass: quote, recent daily bars,
    splits, dividends and snapshots for every followed stock. On the other
    ticks of an open market it walks the chain only for quotes that have gone
    stale — the ones neither the price stream nor refresh_quotes is keeping
    fresh, or all of them if both are down — so an open market is never more
    than a few minutes stale either way. Shut, those ticks return here
    immediately: the price cannot move.

    ponytail: bars, splits and dividends are hourly, so in session the chart's
    last bar can trail the live quote by up to an hour, and a split's history
    rescale can trail the post-split quote as long. "An hour" is 60 or 65
    minutes: the pass is due on the first five-minute tick at or past the
    hour, and a tick that lands a millisecond early waits for the next. Shorten
    PRICE_REFRESH_MINUTES if that ever shows.
    """
    global _last_price_refresh
    now = time.monotonic()
    due = now - _last_price_refresh >= settings.PRICE_REFRESH_MINUTES * 60
    if not due and not markets_open():
        return {"updated": [], "failed": [], "skipped": "markets closed"}

    if due:
        _last_price_refresh = now
        return prices.refresh_watchlist(range_="5d")
    return prices.refresh_watchlist(stale_only=True)


def refresh_intraday() -> dict:
    """Fast cadence. 5m bars while a market is open, then attribution.

    The two belong in one job: attribution reads exactly the bars the pull just
    stored, and running it anywhere else would measure yesterday's window with
    today's data missing. It is pure SQL, so it costs nothing on the ticks where
    the market is shut and the pull returns immediately.
    """
    stored = prices.refresh_watchlist_intraday()
    return {**stored, "attributed": events.attribute_pending()}


def refresh_news() -> dict:
    """Hourly. New articles for every followed stock, then image top-up."""
    result = news.refresh_watchlist(days=2)
    sentiment_service.score_unscored()
    return result


def refresh_news_fast() -> dict:
    """Two-minute lane. Filings only, stored and scored but not judged.

    Event detection is an LLM call per batch; running it every two minutes
    across the watchlist would spend a day's tokens by lunchtime for no more
    signal than the hourly pass gives. The fast lane's job is to get the
    filing into the feed the minute it exists.
    """
    return news.refresh_watchlist(days=2, only=news.FAST_SOURCES,
                                  detect_events=False)


def aggregate_sentiment() -> int:
    """Daily. Rebuild the sentiment rollups the trending panels read."""
    sentiment_service.score_unscored()
    count = db.sentiment.aggregate_all(days=settings.NEWS_RETENTION_DAYS)
    print(f"[jobs] aggregated sentiment for {count} stock-days")
    return count


def refresh_earnings() -> dict:
    """Daily. The next earnings date for every followed US listing.

    One request per day scanned, so it is a small sweep and there is nothing
    to gain from running it more often — a date does not move hourly.
    """
    return {"updated": calendar.refresh_watchlist()}


def refresh_social() -> dict:
    """Hourly. One call for the whole ranked table, joined to the watchlist."""
    return {"updated": social.refresh_watchlist()}


def prune() -> dict:
    """Daily. Keep the DB bounded."""
    articles = news.prune()
    points = db.prices.delete_older_than(days_ago_iso(400))
    # 5m bars get their own far shorter window. At ~78 rows per stock per
    # trading day they would be the largest table in the database inside a
    # month, and nothing reads them once an event is past attributing.
    intraday = db.prices.delete_older_than(
        days_ago_iso(INTRADAY_RETENTION_DAYS), interval=db.prices.INTRADAY)
    # /health only ever asks for the last week of yields; a year of daily rows
    # per source is already generous for looking back at when a source died.
    yields = db.sources.delete_older_than(days_ago_iso(365)[:10])
    return {"articles": articles, "price_points": points,
            "intraday_points": intraday, "source_yields": yields}


# ── On demand ────────────────────────────────────────────────────────────

def backfill_stock(short_name: str, days: int | None = None) -> dict:
    """Everything a newly followed stock needs, in one go.

    Runs when a stock is added so the feed is populated immediately instead of
    being empty until the next hourly tick: resolve the best listing, pull a
    month of daily prices, then a month of news.
    """
    days = days or settings.BACKFILL_DAYS

    with _backfill_lock:
        if short_name in _backfilling:
            print(f"[jobs] backfill for {short_name} already running")
            return {"short_name": short_name, "skipped": "already_running"}
        _backfilling.add(short_name)

    try:
        print(f"[jobs] backfilling {short_name}: {days}d of prices and news")
        quote = prices.refresh_stock(short_name, range_="1mo")
        article_result = news.backfill(short_name, days=days)
        db.sentiment.aggregate_all(days=days)
        db.watchlist.mark_backfilled(short_name)

        return {
            "short_name": short_name,
            "price": quote.get("price") if quote else None,
            "articles": article_result.get("inserted", 0),
            "found": article_result.get("found", 0),
        }
    except Exception as exc:
        print(f"[jobs] backfill failed for {short_name}: {type(exc).__name__}: {exc}")
        return {"short_name": short_name, "error": str(exc)}
    finally:
        with _backfill_lock:
            _backfilling.discard(short_name)


def catch_up() -> dict:
    """Run on API start: finish any backfill that never completed."""
    pending = db.watchlist.needing_backfill()
    if not pending:
        return {"backfilled": []}
    print(f"[jobs] {len(pending)} stock(s) need backfilling: {pending}")
    for short_name in pending:
        backfill_stock(short_name)
    return {"backfilled": pending}


def refresh_all() -> dict:
    """Manual full refresh, exposed on POST /refresh."""
    return {
        # Not refresh_prices() while a market is open. Off the hour that tick
        # only walks stale quotes, and whoever pressed refresh wants the bars,
        # splits and dividends too. Shut, the hourly gate still applies: the
        # price cannot move, so a second press has nothing new to fetch.
        "prices": (prices.refresh_watchlist(range_="5d") if markets_open()
                   else refresh_prices()),
        "intraday": refresh_intraday(),
        "news": refresh_news(),
        "sentiment_days": aggregate_sentiment(),
        "earnings": refresh_earnings(),
        "social": refresh_social(),
    }
