"""Stooq — a second source of daily history, free and without a key.

The price chain has three operators, but only Yahoo ever returns *bars*:
Nasdaq and CNBC answer with a quote and an empty `points`. So a Yahoo outage
does not break the quote, it quietly freezes the chart — history stops
extending and nothing says so. Stooq is the fix, and it is a plain CSV
download rather than an API:

    https://stooq.com/q/d/l/?s=aapl.us&i=d
    Date,Open,High,Low,Close,Volume
    2026-08-19,331.00,341.00,330.50,338.19,52500000

End-of-day only, so it never touches the quote — it exists to backfill the
series behind one. Deliberately not in `get_quote`'s fallback chain for that
reason: it cannot answer the question that chain asks.

**The work here is symbol mapping, not fetching.** Stooq names an instrument
by market suffix the way Yahoo does, but with its own vocabulary, and the two
disagree. An unmapped suffix returns nothing rather than a guess: `SHEL.MU` is
the Munich line, and handing Stooq a symbol that happens to resolve on another
venue would silently backfill the chart with a different market's prices.
"""
from __future__ import annotations

import csv
import io

from normalize import days_ago_iso

from .http_client import scraper

_URL = "https://stooq.com/q/d/l/"

# Yahoo exchange suffix → Stooq market suffix. Only the venues Stooq actually
# carries, and only where the mapping is unambiguous.
#
# Deliberately absent: the German regional exchanges (F, MU, BE, DU, HM, HA,
# STU, BER). Mapping them onto `.de` would be plausible and wrong — same
# company, different venue, different prices — and a fallback that quietly
# swaps venues is worse than one that declines.
_MARKETS = {
    "": "us",       # no suffix is a US listing
    "L": "uk",      # London
    "DE": "de",     # XETRA
    "T": "jp",      # Tokyo
    "WA": "pl",     # Warsaw
    "BD": "hu",     # Budapest
}

# Stooq answers an unknown symbol with HTTP 200 and this as the whole body, so
# the status code cannot be trusted to say whether there is data.
_NO_DATA = "no data"

_RANGE_DAYS = {"1d": 5, "5d": 7, "1mo": 31, "3mo": 93, "6mo": 186, "1y": 366}
_DEFAULT_RANGE_DAYS = 31


def to_stooq(yahoo_symbol: str) -> str | None:
    """`AAPL` → `aapl.us`, `SHEL.L` → `shel.uk`. None when unmapped.

    Indices are excluded outright: Yahoo writes them `^GSPC` and Stooq writes
    them differently enough that there is no rule to apply, only a lookup
    table nobody has asked for yet.
    """
    symbol = (yahoo_symbol or "").strip()
    if not symbol or symbol.startswith("^"):
        return None

    base, _, suffix = symbol.partition(".")
    if not base:
        return None

    market = _MARKETS.get(suffix.upper())
    return f"{base.lower()}.{market}" if market else None


def history(yahoo_symbol: str, range_: str = "1mo") -> list[dict]:
    """Daily bars for a symbol, newest last. Empty on any failure."""
    symbol = to_stooq(yahoo_symbol)
    if not symbol:
        return []

    response = scraper.get(_URL, params={"s": symbol, "i": "d"})
    if not response or not response.ok:
        return []

    since = days_ago_iso(_RANGE_DAYS.get(range_, _DEFAULT_RANGE_DAYS))[:10]
    return [bar for bar in parse_csv(response.text) if bar["ts"][:10] >= since]


def parse_csv(text: str) -> list[dict]:
    """Split from `history` so it can be tested against a captured download."""
    if not text or text.strip().lower().startswith(_NO_DATA):
        return []

    bars = []
    for row in csv.DictReader(io.StringIO(text)):
        day = (row.get("Date") or "").strip()
        close = _number(row.get("Close"))
        if not day or close is None:
            continue
        bars.append({
            "ts": f"{day}T00:00:00Z",
            "close": close,
            "open": _number(row.get("Open")),
            "high": _number(row.get("High")),
            "low": _number(row.get("Low")),
            "volume": _whole(row.get("Volume")),
        })

    bars.sort(key=lambda bar: bar["ts"])
    return bars


def _number(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _whole(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
