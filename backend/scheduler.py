"""Background scheduler. Run alongside the API: `python scheduler.py`.

Deliberately separate from the API process so a long scrape can never block a
request, and so restarting the API doesn't interrupt a refresh.
"""
from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

import db
import jobs
import settings

scheduler = BlockingScheduler(timezone="UTC")

# Batch quotes every minute; the job returns at once whenever every market is
# shut, so it buys a one-minute price in session without scraping all night.
scheduler.add_job(
    jobs.refresh_quotes, "interval",
    minutes=settings.QUOTE_REFRESH_MINUTES, id="quotes",
    max_instances=1, coalesce=True,
)

# The chart chain on a five-minute tick: the full pass (daily bars, splits,
# dividends) once an hour, and in between, during a session, only the quotes
# the spark job left stale. Shut, the in-between ticks do nothing.
scheduler.add_job(
    jobs.refresh_prices, "interval",
    minutes=settings.PRICE_REFRESH_OPEN_MINUTES, id="prices",
    max_instances=1, coalesce=True,
)

# Intraday bars stay on the five-minute tick (spark's 5m series is close-only,
# no volume or OHLC, so it cannot stand in for them) and skip themselves
# outside session hours. Attribution rides along with them.
scheduler.add_job(
    jobs.refresh_intraday, "interval",
    minutes=settings.PRICE_REFRESH_OPEN_MINUTES, id="intraday",
    max_instances=1, coalesce=True,
)

scheduler.add_job(
    jobs.refresh_news, "interval",
    minutes=settings.NEWS_REFRESH_MINUTES, id="news",
    max_instances=1, coalesce=True,
)

# The fast lane, for sources where minutes matter. Cheap because 1.1's
# conditional GET turns an unchanged feed into a 304 with no body.
scheduler.add_job(
    jobs.refresh_news_fast, "interval",
    minutes=settings.NEWS_FAST_MINUTES, id="news_fast",
    max_instances=1, coalesce=True,
)

# Rollups after the US close (21:30 UTC in summer), then housekeeping.
scheduler.add_job(jobs.aggregate_sentiment, "cron", hour=22, minute=0, id="sentiment")
scheduler.add_job(jobs.prune, "cron", hour=3, minute=30, id="prune")
# Before the US pre-market window, so a date announced overnight is on the
# stock before anyone looks at it.
scheduler.add_job(jobs.refresh_earnings, "cron", hour=9, minute=0, id="earnings")

# Mention velocity moves over hours, and one call covers the whole
# watchlist, so it rides the same cadence as the news lane.
scheduler.add_job(
    jobs.refresh_social, "interval",
    minutes=settings.NEWS_REFRESH_MINUTES, id="social",
    max_instances=1, coalesce=True,
)

# No catalogue job: instruments arrive from Yahoo search when one is followed
# (see routes/watchlist.py), so there is nothing to bulk refresh.


def main() -> None:
    db.create_tables()
    print(f"[scheduler] quotes every {settings.QUOTE_REFRESH_MINUTES}m while a market is "
          f"open (stale ones re-checked every {settings.PRICE_REFRESH_OPEN_MINUTES}m), "
          f"full price pass every {settings.PRICE_REFRESH_MINUTES}m, "
          f"news every {settings.NEWS_REFRESH_MINUTES}m "
          f"(filings every {settings.NEWS_FAST_MINUTES}m)")

    # Bring everything current on start rather than idling until the first tick.
    jobs.catch_up()
    jobs.refresh_prices()
    jobs.refresh_intraday()
    jobs.refresh_news()
    jobs.aggregate_sentiment()
    jobs.refresh_earnings()
    jobs.refresh_social()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[scheduler] stopped")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
