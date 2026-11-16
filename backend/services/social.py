"""Retail mention velocity — how loud a stock is on Reddit right now.

This is not sentiment and must never be averaged into it. Sentiment says
whether the coverage reads well or badly; mentions say how many people are
talking, which is a different axis entirely — a stock can be loudly hated or
quietly upgraded. Blending them destroys both numbers.

**The delta is the signal, not the level.** Every stock is mentioned somewhat,
and the absolute count mostly measures market cap. A move from 13 mentions to
155 in a day is the thing worth seeing, and it is what put Walmart fourth on
the board the morning it reported.

One keyless call returns the whole ranked table, so this joins to the watchlist
rather than asking per stock. Only the top hundred are ranked, and a stock
outside them has no retail chatter worth reporting — that absence is the
correct answer, not a miss.

ApeWisdom's tickers are unsuffixed US symbols, so a non-US listing mostly finds
nothing. That is expected and is why the number is nullable.
"""
from __future__ import annotations

import db

from .http_client import scraper

_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks"


def fetch() -> dict[str, dict]:
    """`{ticker: row}` for the whole ranked table, in one request."""
    response = scraper.get(_URL)
    if not response or not response.ok:
        return {}
    try:
        return parse(response.json())
    except ValueError:
        return {}


def parse(payload: dict) -> dict[str, dict]:
    """Split from fetch so it can be tested against a saved response."""
    found = {}
    for row in (payload or {}).get("results") or []:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        found[ticker] = {
            "mentions": _int(row.get("mentions")),
            "mentions_24h_ago": _int(row.get("mentions_24h_ago")),
        }
    return found


def refresh_watchlist() -> int:
    """Hourly. Join the ranked table to the watchlist and store the counts.

    Rewritten from scratch each run, like earnings dates: a stock that falls
    out of the top hundred has gone quiet, and keeping yesterday's count would
    report the opposite.
    """
    table = fetch()
    if not table:
        return 0

    found = {}
    for short_name in db.watchlist.get_symbols():
        stock = db.stocks.get_stock(short_name) or {}
        for ticker in _tickers(short_name, stock.get("yahoo_symbol")):
            row = table.get(ticker)
            if row:
                found[short_name] = (row["mentions"], row["mentions_24h_ago"])
                break

    updated = db.stocks.set_social(found)
    print(f"[social] {len(found)} of {len(table)} ranked tickers on the watchlist")
    return updated


def _tickers(short_name: str, yahoo_symbol: str | None) -> tuple[str, ...]:
    """The plain US symbols this stock might be talked about under.

    `short_name` is whatever Trading212 called it, so Rocket Lab is `6RJ0`
    locally and `RKLB` everywhere else — the resolved symbol is what matches.
    """
    candidates = [(yahoo_symbol or "").split(".")[0].upper(),
                  short_name.split(".")[0].upper()]
    return tuple(dict.fromkeys(c for c in candidates if c))


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
