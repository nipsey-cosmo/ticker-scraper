"""Background scheduler. Run alongside the API: `python scheduler.py`.

Deliberately separate from the API process so a long scrape can never block a
request, and so restarting the API doesn't interrupt a refresh.
"""
from __future__ import annotations

import threading

from apscheduler.schedulers.blocking import BlockingScheduler

import db
import jobs
import settings
from services import stream

scheduler = BlockingScheduler(timezone="UTC")

# Prices on the fast cadence. `jobs.refresh_prices` makes one tick an hour a
# full pass; the rest poll only stocks the price stream (started in main) is
# not keeping fresh, and do nothing at all once every market is shut.
scheduler.add_job(
    jobs.refresh_prices, "interval",
    minutes=settings.PRICE_REFRESH_OPEN_MINUTES, id="prices",
    max_instances=1, coalesce=True,
)

# Intraday bars ride the same fast cadence as the quote, and skip themselves
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
    print(f"[scheduler] live quotes from the Yahoo stream, flushed every "
          f"{stream.FLUSH_SECONDS}s; prices polled every "
          f"{settings.PRICE_REFRESH_OPEN_MINUTES}m while a market is open for "
          f"stocks the stream is not feeding, full pass every "
          f"{settings.PRICE_REFRESH_MINUTES}m; news every {settings.NEWS_REFRESH_MINUTES}m "
          f"(filings every {settings.NEWS_FAST_MINUTES}m)")

    # Bring everything current on start rather than idling until the first tick.
    jobs.catch_up()
    jobs.refresh_prices()
    jobs.refresh_intraday()
    jobs.refresh_news()
    jobs.aggregate_sentiment()
    jobs.refresh_earnings()
    jobs.refresh_social()

    # After the start-up price pass, so every followed symbol has been charted
    # and yahoo.minor_units knows its divisor: the stream drops a tick it
    # cannot scale rather than guess. Daemon, so Ctrl+C is not held up by a
    # socket waiting on its next tick.
    threading.Thread(target=stream.run, name="stream", daemon=True).start()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[scheduler] stopped")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
