"""Earnings dates — the one market-moving event you can see coming.

Everything else here is reactive: something happened, a source reported it, we
scraped it. An earnings date is known weeks ahead, and "reports on Thursday" is
the single most useful piece of context an investor can have about a stock this
week. It is also what makes an unexplained move explainable in advance.

Nasdaq's calendar endpoint is keyless and on a host the scraper already talks
to, so this costs one request per day scanned and no new relationship.

**US listings only**, like everything on that host. A London listing gets no
date, and that is a null rather than a failure — the same distinction 1.5 draws
between a source that found nothing and one that was never asked.

**Which side of the bell matters.** "Reports Thursday" is useless without it:
pre-market means Thursday's open gaps, after-hours means Thursday's close is
the last clean print and Friday opens on the news. Nasdaq marks it, so it is
stored.
"""
from __future__ import annotations

from datetime import timedelta

import db
from normalize import now_utc

from . import symbols
from .http_client import scraper

_URL = "https://api.nasdaq.com/api/calendar/earnings"
# Far enough ahead to be useful, short enough to stay one small daily sweep.
_DAYS_AHEAD = 14

# Nasdaq's marker, normalised to the two states that change how a date reads.
_WHEN = {
    "time-pre-market": "pre",
    "time-after-hours": "post",
}


def fetch_day(date: str) -> dict[str, str | None]:
    """`{ticker: 'pre' | 'post' | None}` for one calendar day.

    A day with no earnings answers 200 with a null `rows`, which is not an
    error — most Saturdays look exactly like a broken scraper otherwise.
    """
    response = scraper.get(_URL, params={"date": date})
    if not response or not response.ok:
        return {}
    try:
        return parse(response.json())
    except ValueError:
        return {}


def parse(payload: dict) -> dict[str, str | None]:
    """Split from fetch_day so it can be tested against a saved response."""
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    found = {}
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().upper()
        if ticker:
            found[ticker] = _WHEN.get(str(row.get("time") or "").strip())
    return found


def refresh_watchlist() -> int:
    """Daily. The next earnings date for every followed US listing.

    Every date is rewritten from scratch each run rather than merged: a company
    that moves its date out of the window would otherwise keep the old one
    forever, and there is no signal that says "this date is no longer true".
    """
    wanted = _us_tickers()
    if not wanted:
        db.stocks.set_earnings({})
        return 0

    today = now_utc().date()
    found: dict[str, tuple[str, str | None]] = {}

    for offset in range(_DAYS_AHEAD):
        day = (today + timedelta(days=offset)).isoformat()
        for ticker, when in fetch_day(day).items():
            short_name = wanted.get(ticker)
            # First hit wins: the days are scanned in order, so that is the
            # next one rather than an arbitrary one.
            if short_name and short_name not in found:
                found[short_name] = (day, when)

    updated = db.stocks.set_earnings(found)
    print(f"[calendar] {len(found)} upcoming earnings date(s) across "
          f"{len(wanted)} US listing(s)")
    return updated


def _us_tickers() -> dict[str, str]:
    """`{plain US ticker: short_name}` for the watchlist.

    Our `short_name` is whatever Trading212 called the instrument, so the join
    goes through the resolved Yahoo symbol with its suffix stripped.
    """
    wanted = {}
    for short_name in db.watchlist.get_symbols():
        stock = db.stocks.get_stock(short_name) or {}
        if not symbols.is_us_listing(stock.get("exchange")):
            continue
        ticker = (stock.get("yahoo_symbol") or short_name).split(".")[0].upper()
        if ticker:
            wanted[ticker] = short_name
    return wanted


def next_for(short_name: str) -> dict | None:
    """The upcoming date for one stock, for the event prompt's context."""
    row = db.stocks.get_stock(short_name) or {}
    date = row.get("next_earnings_date")
    if not date or str(date) < now_utc().date().isoformat():
        return None
    return {"date": str(date), "when": row.get("next_earnings_time")}
