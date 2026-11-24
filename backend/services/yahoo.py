"""Raw Yahoo Finance endpoints. Parsing only — no DB, no business rules.

These are the public JSON endpoints a browser hits, so no key and no crumb
dance. The chart endpoint is the workhorse: one call returns the live quote
*and* the historical series for one symbol. Spark is the bulk-quote path beside
it — twenty symbols per request, so a one-minute quote does not cost a chart
call per stock per minute. It is undocumented, which is why nothing depends on
it alone: the chart chain in services/prices.py still covers every quote it
misses.

The one non-JSON payload is the price stream's tick (`parse_pricing`), which
services/stream.py reads off Yahoo's WebSocket.
"""
from __future__ import annotations

import base64
import struct

from normalize import to_iso

from .http_client import Fetched, scraper

# Two hostnames serve identical data. When one starts throttling, the other is
# usually still happy — a free extra token bucket.
_CHART_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]

_SEARCH_HOSTS = [
    "https://query2.finance.yahoo.com",
    "https://query1.finance.yahoo.com",
]

# Quotes come back in the exchange's minor unit for a few venues; divide to get
# major units so the number next to a "£" is actually pounds.
_MINOR_UNIT_DIVISORS = {
    "GBP": 1.0, "GBp": 100.0, "GBX": 100.0,   # LSE quotes in pence
    "ZAC": 100.0, "ZAR": 1.0,                  # Johannesburg in cents
    "ILA": 100.0, "ILS": 1.0,                  # Tel Aviv in agorot
}


def normalise_currency(currency: str | None) -> tuple[str, float]:
    """('GBp', 100.0) → quotes are in pence, divide by 100 to get GBP."""
    if not currency:
        return "", 1.0
    divisor = _MINOR_UNIT_DIVISORS.get(currency)
    if divisor is None:
        # Unknown code: pass through untouched rather than guess.
        return currency.upper(), 1.0
    major = {"GBp": "GBP", "GBX": "GBP", "ZAC": "ZAR", "ILA": "ILS"}.get(currency, currency)
    return major.upper(), divisor


def _api_headers(referer: str = "https://finance.yahoo.com/") -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://finance.yahoo.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def _try_hosts(hosts: list[str], path: str, params: dict, cache: bool = True) -> dict | None:
    """First host that answers wins; the rest are free retries on a fresh bucket."""
    for host in hosts:
        data = scraper.get_json(host + path, params=params, headers=_api_headers(), cache=cache)
        if isinstance(data, dict):
            return data
    return None


# Intervals fine enough that a cached response would be served as live.
_INTRADAY = frozenset({"1m", "2m", "5m", "15m"})

# Yahoo symbol -> minor-unit divisor, as last seen on a chart call. The price
# stream carries no currency for equities, so this is the only place it can
# learn that SHEL.L is quoted in pence; a symbol with no entry yet has simply
# not been charted since start-up. Written by the job threads and read by the
# stream thread — single key assignments, which the GIL keeps whole.
minor_units: dict[str, float] = {}


# ── Chart: quote + history in one request ────────────────────────────────

def chart(symbol: str, range_: str = "1mo", interval: str = "1d") -> dict | None:
    """Parsed chart payload, or None.

    Returns:
        {"symbol", "exchange", "currency", "divisor", "price",
         "previous_close", "change", "change_percent",
         "points": [{"ts", "close", "open", "high", "low", "volume"}]}

    Every field of a point except `ts` and `close` may be None: the feed has
    holes, and a hole is not a zero.
    """
    data = _try_hosts(
        _CHART_HOSTS,
        f"/v8/finance/chart/{symbol}",
        {"range": range_, "interval": interval, "includePrePost": "false", "events": "div,split"},
        cache=(interval not in _INTRADAY),
    )
    if not data:
        return None
    parsed = parse_chart(data)
    if parsed:
        minor_units[symbol] = parsed["divisor"]
    return parsed


def parse_chart(data: dict) -> dict | None:
    """Split out so it can be tested against a captured payload."""
    try:
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        node = result[0]
        meta = node.get("meta") or {}
    except (AttributeError, TypeError):
        return None

    currency, divisor = normalise_currency(meta.get("currency"))

    stamps = node.get("timestamp") or []
    quote_blocks = ((node.get("indicators") or {}).get("quote") or [{}])
    bars = quote_blocks[0] or {}
    closes = bars.get("close") or []
    opens, highs, lows = bars.get("open"), bars.get("high"), bars.get("low")
    volumes = bars.get("volume")

    # A bar with no close is a market holiday and is skipped entirely. The
    # other four are independent: Yahoo will happily hand back a real close
    # with a null high, so each is read separately and stored as nullable
    # rather than defaulted to zero, which would render as a real trade.
    points: list[dict] = []
    for index, (ts, close) in enumerate(zip(stamps, closes)):
        if close is None or ts is None:
            continue
        points.append({
            "ts": _iso_from_epoch(ts),
            "close": round(close / divisor, 6),
            "open": _major(_at(opens, index), divisor),
            "high": _major(_at(highs, index), divisor),
            "low": _major(_at(lows, index), divisor),
            # Volume is a share count, not money. Dividing it by the
            # minor-unit divisor would report a hundredth of the real
            # turnover on every LSE line.
            "volume": _shares(_at(volumes, index)),
        })

    price = meta.get("regularMarketPrice")
    if price is None and points:
        price = points[-1]["close"] * divisor
    if price is None:
        return None
    price = price / divisor

    # previousClose is the reliable field; chartPreviousClose is the close
    # before the window; the prior bar is the last resort.
    previous = meta.get("previousClose")
    if previous is None and len(points) >= 2:
        previous = points[-2]["close"] * divisor
    if previous is None:
        previous = meta.get("chartPreviousClose")
    previous = (previous / divisor) if previous else None

    change = round(price - previous, 6) if previous else None
    change_percent = (
        round((change / previous) * 100, 3) if previous and change is not None else None
    )

    dividends, splits = _corporate_actions(node, divisor)

    return {
        "session": _regular_session(meta),
        "symbol": meta.get("symbol"),
        "exchange": meta.get("exchangeName"),
        "exchange_name": meta.get("fullExchangeName"),
        "instrument_type": meta.get("instrumentType"),
        "currency": currency,
        "divisor": divisor,
        "price": round(price, 6),
        "previous_close": round(previous, 6) if previous else None,
        "change": change,
        "change_percent": change_percent,
        "points": points,
        "dividends": dividends,
        "splits": splits,
    }


# Spark answers twenty symbols per request; a twenty-first fails the whole
# request with a 400 rather than truncating.
_SPARK_CHUNK = 20


def spark(symbols: list[str]) -> dict[str, dict]:
    """Live quotes for many symbols, keyed by Yahoo symbol. Missing = no quote.

    Each result node is shaped exactly like a chart result, so parse_chart
    reads it unchanged; only the quote fields are meant to be used. The points
    are a close-only 5m series and are no substitute for daily bars.

    Results come back in no fixed order and an unknown symbol is silently left
    out, so everything is keyed by the symbol the entry names, never by
    position. `range=1d&interval=5m` is what makes `previousClose` the prior
    session's close; an entry without it is dropped, since parse_chart would
    otherwise fall back to the prior *5m bar* and store a five-minute move as
    the day's change.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(symbols), _SPARK_CHUNK):
        chunk = symbols[start:start + _SPARK_CHUNK]
        data = _try_hosts(
            _CHART_HOSTS, "/v7/finance/spark",
            {"symbols": ",".join(chunk), "range": "1d", "interval": "5m"},
            cache=False,
        )
        if not data:
            continue                                   # costs only this chunk
        for entry in (data.get("spark") or {}).get("result") or []:
            try:
                symbol, nodes = entry["symbol"], entry["response"]
                if nodes[0]["meta"].get("previousClose") is None:
                    continue
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            parsed = parse_chart({"chart": {"result": nodes}})
            if parsed:
                out[symbol] = parsed
                # Same as chart(): the stream learns a new follow's divisor
                # from whichever call names the symbol first, and spark names
                # it within a minute where the chart pass can take an hour.
                minor_units[symbol] = parsed["divisor"]
    return out


def _regular_session(meta: dict) -> dict | None:
    """The exchange's regular hours for this payload, or None if not stated.

    Yahoo is asked for `includePrePost=false` and mostly honours it, but a 5m
    request still comes back with the odd pre-market bar attached. This is the
    feed's own answer to "when was the bell", so it is what intraday storage
    filters against rather than a hardcoded clock per exchange.
    """
    regular = (meta.get("currentTradingPeriod") or {}).get("regular")
    if not isinstance(regular, dict):
        return None
    start, end = regular.get("start"), regular.get("end")
    if start is None or end is None:
        return None
    return {"start": _iso_from_epoch(start), "end": _iso_from_epoch(end)}


def _corporate_actions(node: dict, divisor: float) -> tuple[list[dict], list[dict]]:
    """The `events` block the chart call has always asked for and never read.

    Yahoo keys both maps by epoch second, so the payload is
    `{"dividends": {"1785072600": {"amount": .., "date": ..}}, "splits": {...}}`.
    """
    events = node.get("events")
    if not isinstance(events, dict):
        return [], []

    dividends = []
    for entry in (events.get("dividends") or {}).values():
        if not isinstance(entry, dict):
            continue
        amount, stamp = entry.get("amount"), entry.get("date")
        if amount is None or stamp is None:
            continue
        # A dividend is money, quoted in the same minor unit as the price, so
        # an LSE payout arrives in pence and has to come down with everything
        # else. Volume is the only field here that must *not* be scaled.
        dividends.append({"ex_date": _iso_from_epoch(stamp)[:10],
                          "amount": round(amount / divisor, 6)})

    splits = []
    for entry in (events.get("splits") or {}).values():
        if not isinstance(entry, dict):
            continue
        numerator = entry.get("numerator")
        denominator = entry.get("denominator")
        stamp = entry.get("date")
        if not numerator or not denominator or stamp is None:
            continue
        splits.append({"ex_date": _iso_from_epoch(stamp)[:10],
                       "numerator": float(numerator),
                       "denominator": float(denominator)})

    dividends.sort(key=lambda d: d["ex_date"])
    splits.sort(key=lambda s: s["ex_date"])
    return dividends, splits


def _at(series, index):
    """One bar out of one series, tolerating a series that is short or absent."""
    if not isinstance(series, list) or index >= len(series):
        return None
    return series[index]


def _major(value, divisor: float) -> float | None:
    """A price field converted to major units. None stays None."""
    return None if value is None else round(value / divisor, 6)


def _shares(value) -> int | None:
    """Volume, as a whole number of shares. Never scaled by the divisor."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_from_epoch(seconds: int) -> str:
    from datetime import datetime, timezone
    return to_iso(datetime.fromtimestamp(seconds, timezone.utc))


# ── Price stream: one protobuf tick ──────────────────────────────────────

# Stream field numbers that are read; every other field is skipped by its wire
# type, so a field Yahoo adds tomorrow costs nothing.
_ID, _PRICE, _TIME, _MARKET_HOURS, _CHANGE_PERCENT, _CHANGE = 1, 2, 3, 7, 8, 12


def parse_pricing(message_b64: str) -> dict | None:
    """One stream tick: {"symbol", "price", "time", "market_hours", "change",
    "change_percent"}, or None for anything malformed or truncated.

    Hand-decoded rather than via the protobuf package: five fields of one
    message do not justify a dependency and a generated module. The wire format
    is proto3, which omits a field whose value is zero, so an absent number is
    returned as 0 — that is what the sender meant. It matters most for
    `market_hours`, where 0 is pre-market and only 1 is the regular session.

    Prices are float32 on the wire. Rounding to seven significant digits — all
    a float32 holds — turns 350.80999756 back into the 350.81 that was quoted.
    """
    try:
        data = base64.b64decode(message_b64, validate=True)
        fields: dict[int, int | bytes] = {}
        pos = 0
        while pos < len(data):
            key, pos = _varint(data, pos)
            wire = key & 7
            if wire == 0:
                value, pos = _varint(data, pos)
            elif wire in (1, 2, 5):
                if wire == 2:
                    size, pos = _varint(data, pos)
                else:
                    size = 8 if wire == 1 else 4
                value, pos = data[pos:pos + size], pos + size
                if pos > len(data):
                    return None                    # cut off mid-value
            else:
                return None                        # groups: not in this schema
            fields[key >> 3] = value

        symbol = fields[_ID].decode()
        price = _float32(fields[_PRICE])
        return {
            "symbol": symbol,
            "price": price,
            "time": _zigzag(fields.get(_TIME, 0)),
            "market_hours": fields.get(_MARKET_HOURS, 0),
            "change": _float32(fields.get(_CHANGE, bytes(4))),
            "change_percent": _float32(fields.get(_CHANGE_PERCENT, bytes(4))),
        }
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, struct.error):
        return None


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """(value, next position). Raises IndexError if the bytes run out."""
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _zigzag(n: int) -> int:
    """sint64 decoding: 0, -1, 1, -2 ... are sent as 0, 1, 2, 3 ..."""
    return (n >> 1) ^ -(n & 1)


def _float32(raw: bytes) -> float:
    return float(f"{struct.unpack('<f', raw)[0]:.7g}")


# ── Search: symbol resolution + a news side-channel ──────────────────────

def search(query: str, quotes: int = 10, news: int = 0) -> dict:
    """Yahoo's autocomplete. Doubles as a news source with real thumbnails."""
    data = _try_hosts(
        _SEARCH_HOSTS,
        "/v1/finance/search",
        {
            "q": query,
            "quotesCount": quotes,
            "newsCount": news,
            "listsCount": 0,
            "enableFuzzyQuery": "false",
            "quotesQueryId": "tss_match_phrase_query",
            "newsQueryId": "news_cie_vespa",
        },
    )
    if not data:
        return {"quotes": [], "news": []}
    return {"quotes": data.get("quotes") or [], "news": data.get("news") or []}


def rss_headlines(symbol: str) -> Fetched | None:
    """Yahoo's per-ticker RSS. Older endpoint, still live, carries summaries."""
    return scraper.get(
        "https://feeds.finance.yahoo.com/rss/2.0/headline",
        params={"s": symbol, "region": "US", "lang": "en-US"},
        headers={"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
