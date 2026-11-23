"""Prices, free of charge, with a real fallback chain.

    0. Yahoo stream    — services/stream.py, quote row only, seconds old
    1. Yahoo chart v8  — quote *and* daily history in one request, two hosts
    2. Nasdaq quote    — US listings, real-time during the session, own host
    3. CNBC quote API  — independent host, no key, global coverage

The stream sits above the chain rather than in it. It is a long-lived socket in
the scheduler process that writes the latest regular-session tick for every
followed stock it can price; the poll below then skips any stock whose quote is
fresher than QUOTE_FRESH_SECONDS. Nothing tells the poll the stream is down —
a dead stream's quotes simply age past the threshold and the five-minute poll
takes them back. The hourly full pass ignores freshness, so daily bars, splits,
dividends and snapshots keep arriving for streamed stocks too, which also
means today's daily bar lags up to an hour for them. Minor-unit listings
(pence, cents) are not streamed yet; see the ponytail in stream._session.

History has its own fallback, because the chain above does not cover it: only
Yahoo returns bars, so a Yahoo outage used to leave the quote fine and the
chart quietly frozen. Stooq (services/stooq.py) fills that gap — end-of-day
CSV, no key — and is deliberately outside the chain, since it cannot answer
what something is trading at right now.

Three different operators, hit through the adaptive scraper, so a throttled
Yahoo drops its rate and the next source picks up the slack rather than the
refresh failing. Diversifying across suppliers is what keeps this polite:
nobody is being leaned on hard enough to notice, and no proxy or IP rotation
is needed to stay welcome.

So a throttled Yahoo now costs neither the quote nor the chart: the quote
fails over to Nasdaq or CNBC, and the bars come from Stooq.

Everything is stored in *major* currency units: LSE quotes arrive in pence and
Tel Aviv in agorot, and a chart that silently mixes 3323.5 with 33.235 is worse
than no chart.

Corporate actions ride along on the same Yahoo response. Dividends are stored;
splits additionally rescale the history already on disk, because a refresh only
re-fetches a few days and every older row would otherwise stay on the pre-split
scale — a cliff in the chart that no source agrees with.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
import settings
from normalize import days_ago_iso, markets_open, now_utc, to_iso

from . import stooq, symbols, yahoo
from .http_client import scraper

_CNBC_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
_NASDAQ_URL = "https://api.nasdaq.com/api/quote/{symbol}/info"

# Yahoo exchange suffix → CNBC country code, for the cases where CNBC will not
# take the Yahoo symbol as-is.
_CNBC_SUFFIX = {
    "L": "GB", "DE": "DE", "F": "DE", "PA": "FR", "AS": "NL", "BR": "BE",
    "MI": "IT", "MC": "ES", "SW": "CH", "ST": "SE", "CO": "DK", "OL": "NO",
    "HE": "FI", "VI": "AT", "LS": "PT", "TO": "CA", "HK": "HK", "T": "JP",
    "AX": "AU", "NZ": "NZ",
}

_NUMBER = re.compile(r"-?[\d,]+\.?\d*")


def _to_float(value) -> float | None:
    """CNBC returns display strings: '3,323.50', '-0.56%', 'UNCH'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value).replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


# ── Sources ──────────────────────────────────────────────────────────────

def from_yahoo(yahoo_symbol: str, range_: str = "1mo") -> dict | None:
    chart = yahoo.chart(yahoo_symbol, range_=range_, interval="1d")
    if not chart or chart.get("price") is None:
        return None
    return {**chart, "source": "yahoo"}


def from_nasdaq(yahoo_symbol: str) -> dict | None:
    """US listings only, and deliberately so.

    A suffixed symbol names a foreign line — `SHEL.L` is the London ordinary,
    not the New York ADR — and Nasdaq would happily answer with the ADR at a
    different price in a different currency. Returning None for anything
    suffixed is what stops that silent mismatch.
    """
    if "." in yahoo_symbol or "^" in yahoo_symbol:
        return None

    data = scraper.get_json(
        _NASDAQ_URL.format(symbol=yahoo_symbol.upper()),
        params={"assetclass": "stocks"},
        headers={"Accept": "application/json"},
    )
    if not isinstance(data, dict):
        return None
    quote = data.get("data")
    if not isinstance(quote, dict):
        return None

    session = _nasdaq_session(quote)
    price = _to_float(session.get("lastSalePrice"))
    if not price:
        return None

    change = _to_float(session.get("netChange"))
    return {
        "symbol": quote.get("symbol") or yahoo_symbol,
        "exchange": quote.get("exchange"),
        "currency": "USD",
        "price": round(price, 6),
        "previous_close": round(price - change, 6) if change is not None else None,
        "change": change,
        "change_percent": _to_float(session.get("percentageChange")),
        "points": [],
        "source": "nasdaq",
    }


def _nasdaq_session(quote: dict) -> dict:
    """The regular-session block, whichever of the two it happens to be.

    Nasdaq splits a quote in two: `primaryData` is whatever is trading right
    now, and once the bell goes that becomes the extended-hours print while
    `secondaryData` holds the regular close. Taking primaryData blindly would
    store an after-hours price as the day's close and put a point on the chart
    that no other source agrees with, so the regular session wins whenever it
    is present.
    """
    secondary = quote.get("secondaryData")
    if isinstance(secondary, dict) and secondary.get("lastSalePrice"):
        return secondary
    primary = quote.get("primaryData")
    return primary if isinstance(primary, dict) else {}


def from_cnbc(yahoo_symbol: str) -> dict | None:
    """CNBC accepts Yahoo-style symbols (`SHEL.L`) as well as its own
    (`SHEL-GB`), so try the symbol we already have before translating."""
    for candidate in _cnbc_candidates(yahoo_symbol):
        parsed = _cnbc_quote(candidate)
        if parsed:
            return parsed
    return None


def _cnbc_candidates(yahoo_symbol: str) -> list[str]:
    candidates = [yahoo_symbol]
    if "." in yahoo_symbol:
        base, suffix = yahoo_symbol.rsplit(".", 1)
        country = _CNBC_SUFFIX.get(suffix.upper())
        if country:
            candidates.append(f"{base}-{country}")
    return candidates


def _cnbc_quote(symbol: str) -> dict | None:
    raw = scraper.get(
        _CNBC_URL,
        params={
            "symbols": symbol, "requestMethod": "itv", "noform": "1",
            "partnerId": "2", "fund": "1", "output": "json",
        },
        headers={"Accept": "application/json, text/plain, */*"},
    )
    if not raw or not raw.ok:
        return None
    try:
        quotes = (json.loads(raw.text).get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
    except (ValueError, AttributeError):
        return None
    if isinstance(quotes, dict):
        quotes = [quotes]
    if not quotes:
        return None

    quote = quotes[0]
    price = _to_float(quote.get("last"))
    if not price:
        return None

    currency, divisor = yahoo.normalise_currency(quote.get("currencyCode"))
    previous = _to_float(quote.get("previous_day_closing"))
    change = _to_float(quote.get("change"))
    if change is None and previous:
        change = price - previous

    return {
        "symbol": quote.get("symbol"),
        "exchange": quote.get("exchange"),
        "currency": currency,
        "price": round(price / divisor, 6),
        "previous_close": round(previous / divisor, 6) if previous else None,
        "change": round(change / divisor, 6) if change is not None else None,
        "change_percent": _to_float(quote.get("change_pct")),
        "points": [],
        "source": "cnbc",
    }


def get_quote(yahoo_symbol: str, range_: str = "1mo") -> dict | None:
    """Walk the chain. None only if every source failed.

    Stooq is deliberately *not* here. It is end-of-day only, so it cannot
    answer the "what is it trading at now" question this chain exists for, and
    deciding whether history needs topping up needs the database — see
    `_stooq_backfill`.
    """
    return (from_yahoo(yahoo_symbol, range_=range_)
            or from_nasdaq(yahoo_symbol)
            or from_cnbc(yahoo_symbol))


# ── Refresh (writes to the DB) ───────────────────────────────────────────

def ensure_resolved(stock: dict) -> str | None:
    """The Yahoo symbol for a stock, resolving and caching it on first use."""
    if stock.get("yahoo_symbol"):
        return stock["yahoo_symbol"]

    resolved = symbols.resolve(stock["short_name"], stock.get("name"))
    if not resolved:
        return None
    db.stocks.set_resolution(
        stock["short_name"], resolved["symbol"],
        resolved.get("exchange"), resolved.get("currency"),
    )
    _store_classification(stock["short_name"], resolved)
    print(f"[prices] resolved {stock['short_name']} -> "
          f"{resolved['symbol']} ({resolved.get('exchange')})")
    return resolved["symbol"]


def _store_classification(short_name: str, resolved: dict) -> None:
    """Persist Yahoo's sector/industry, when it gave us one."""
    fields = {k: resolved.get(k) for k in ("sector", "industry") if resolved.get(k)}
    if fields:
        db.stocks.update_stock(short_name, **fields)


def ensure_classified(stock: dict) -> bool:
    """Fill in a missing sector for an already-resolved stock.

    Resolution caches `yahoo_symbol` on the row, so every stock followed before
    the sector column existed short-circuits ensure_resolved() and would never
    pick one up. This re-runs just the search half — `validate=False` skips the
    chart call, since we already trust the symbol and only want its label.

    ETFs and indices are genuinely unclassified by Yahoo, so a stock that comes
    back empty is not retried into a loop: it is simply left NULL and filtered
    out of the sector list.
    """
    if stock.get("sector") or not stock.get("yahoo_symbol"):
        return False

    resolved = symbols.resolve(stock["short_name"], stock.get("name"), validate=False)
    if not resolved or not resolved.get("sector"):
        return False

    _store_classification(stock["short_name"], resolved)
    print(f"[prices] classified {stock['short_name']} -> {resolved['sector']}"
          f" / {resolved.get('industry')}")
    return True


def refresh_stock(short_name: str, range_: str = "5d") -> dict | None:
    """Update one stock's quote and history. `range_='1mo'` for a backfill."""
    stock = db.stocks.get_stock(short_name)
    if not stock:
        print(f"[prices] {short_name} not in catalogue")
        return None

    yahoo_symbol = ensure_resolved(stock)
    if not yahoo_symbol:
        return None

    quote = get_quote(yahoo_symbol, range_=range_)
    if not quote:
        print(f"[prices] all sources failed for {short_name} ({yahoo_symbol})")
        return None

    if not quote.get("points"):
        quote["points"] = _stooq_backfill(short_name, yahoo_symbol, range_)

    _store(short_name, quote)
    return quote


# A daily bar can legitimately be this old and still be current: Friday's close
# is the newest thing that exists until Monday, and a public holiday stretches
# that further.
_HISTORY_GRACE_DAYS = 4


def _stooq_backfill(short_name: str, yahoo_symbol: str, range_: str) -> list[dict]:
    """Bars for a quote that arrived without them — Nasdaq and CNBC never
    return any, so a Yahoo outage otherwise freezes the chart silently.

    Gated on the stored series actually having fallen behind, and that gate is
    the point rather than an optimisation: this runs inside the five-minute
    price job, and Stooq is a small operation serving an end-of-day CSV.
    Ungated, one Yahoo outage would mean a full history download per stock
    every five minutes, all day, for a file that changes once.
    """
    recent = db.prices.get_history(
        short_name, days_ago_iso(_HISTORY_GRACE_DAYS), interval="1d")
    if recent:
        return []

    bars = stooq.history(yahoo_symbol, range_=range_)
    if bars:
        print(f"[prices] {short_name}: history had gone stale, "
              f"{len(bars)} bars from stooq")
    return bars


def _store(short_name: str, quote: dict) -> None:
    # Before the points, and that ordering is the whole trick. Yahoo returns
    # the requested window *already* split-adjusted, so incoming bars are on
    # the new scale while rows stored before the split are still on the old
    # one. Adjusting first moves only the stale rows; the fresh ones then land
    # on top at the scale they arrived in. Insert first and the adjustment
    # would divide the new bars through a second time.
    _store_corporate_actions(short_name, quote)

    if quote.get("points"):
        # Daily bars are stamped at midnight UTC so one trading day is one row
        # regardless of which session time the exchange reports.
        db.prices.upsert_points(
            short_name,
            [{**bar, "ts": bar["ts"][:10] + "T00:00:00Z"} for bar in quote["points"]],
            interval="1d",
        )

    _store_snapshot(short_name, quote["price"])

    db.stocks.bulk_set_quotes([{
        "short_name": short_name,
        "price": quote["price"],
        "change": quote.get("change"),
        "change_percent": quote.get("change_percent"),
        "currency": quote.get("currency"),
    }])


def _store_corporate_actions(short_name: str, quote: dict) -> None:
    """Record dividends, and bring history onto the scale of any new split.

    `record_split` is the guard: it returns True only for a split we have not
    seen, so the adjustment below runs exactly once per event no matter how
    many refreshes carry it. Without that, every hourly refresh for the next
    month would halve the same history again.
    """
    if dividends := quote.get("dividends"):
        db.prices.upsert_dividends(short_name, dividends, quote.get("currency"))

    for split in quote.get("splits") or []:
        ratio = split["numerator"] / split["denominator"]
        if ratio == 1:
            continue
        if not db.prices.record_split(short_name, split["ex_date"],
                                      split["numerator"], split["denominator"]):
            continue                                   # already applied
        moved = db.prices.adjust_for_split(short_name, split["ex_date"], ratio)
        print(f"[prices] {short_name}: {split['numerator']:g}:"
              f"{split['denominator']:g} split on {split['ex_date']}, "
              f"adjusted {moved} stored bars")


def _store_snapshot(short_name: str, price: float) -> None:
    """One point per hour, and only when the price actually moved.

    Skipping unchanged prices means an overnight or weekend refresh doesn't
    stamp out a flat line of duplicate points — cheaper than tracking the
    opening hours of forty exchanges just to decide whether to write a row.
    """
    latest = db.prices.latest(short_name)
    if latest and abs(latest["close"] - price) < 1e-9:
        return
    stamp = to_iso(now_utc().replace(minute=0, second=0, microsecond=0))
    db.prices.upsert_points(short_name, [(stamp, price)], interval="snap")


# ── Intraday bars ────────────────────────────────────────────────────────

# One trading day of 5m bars is ~78 rows. A day's range is all that is needed:
# the fortnight of history builds up a day at a time from repeated calls, and
# asking for more would re-download bars already stored on every tick.
_INTRADAY_RANGE = "1d"


def in_session(chart: dict) -> list[dict]:
    """Bars that fall inside the exchange's regular hours.

    `includePrePost=false` is already on the request and Yahoo mostly honours
    it, but a 5m payload still arrives with the occasional pre-market bar. A
    pre-market print on thin volume is exactly the kind of bar that would make
    an event look like it moved the stock 3%, so it is dropped rather than
    stored and reasoned about later.

    Compared as a **time of day**, not as an absolute window, and that is not a
    simplification — it is the bug fix. `currentTradingPeriod` is the current
    *or next* session: ask before the opening bell and it names today's session
    while `range=1d` hands back yesterday's bars. Matching absolute timestamps
    then drops a whole day of them. The bell rings at the same clock time
    either way, so the hours are what to compare.

    A payload that does not state its session is stored whole — the feed's own
    filter is then the only one we have, and dropping everything would be worse
    than trusting it.
    """
    points = chart.get("points") or []
    session = chart.get("session")
    if not session:
        return points

    # "HH:MM:SS" out of an ISO stamp. Fixed width, so string order is clock
    # order and no parsing is needed.
    start, end = session["start"][11:19], session["end"][11:19]

    def in_hours(stamp: str) -> bool:
        clock = stamp[11:19]
        if start <= end:
            return start <= clock <= end
        # Sydney and Tokyo open before midnight UTC and close after it, so the
        # window wraps rather than being empty.
        return clock >= start or clock <= end

    return [bar for bar in points if in_hours(bar["ts"])]


def refresh_intraday(short_name: str) -> int:
    """Today's 5m bars for one stock. Returns the number stored."""
    stock = db.stocks.get_stock(short_name)
    if not stock:
        return 0
    yahoo_symbol = ensure_resolved(stock)
    if not yahoo_symbol:
        return 0

    chart = yahoo.chart(yahoo_symbol, range_=_INTRADAY_RANGE, interval=db.prices.INTRADAY)
    if not chart:
        return 0

    bars = in_session(chart)
    if not bars:
        return 0
    return db.prices.upsert_points(short_name, bars, interval=db.prices.INTRADAY)


def refresh_watchlist_intraday() -> dict:
    """Scheduled: 5m bars for every followed stock, while a market is open.

    Session-gated at the top, and that gate is the feature rather than an
    optimisation. Ungated this is an all-night poll returning the same flat
    line: one request per stock per tick, all of them re-fetching bars already
    on disk, for a price that cannot move.
    """
    if not markets_open():
        return {"stored": 0, "skipped": "markets closed"}

    watched = db.watchlist.get_symbols()
    if not watched:
        return {"stored": 0}

    def one(short_name: str) -> int:
        try:
            return refresh_intraday(short_name)
        except Exception as exc:                      # one bad symbol must not
            print(f"[prices] intraday {short_name} errored: {exc}")   # stall the rest
            return 0

    workers = min(settings.SCRAPE_CONCURRENCY, len(watched))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="intraday") as pool:
        stored = sum(pool.map(one, watched))

    print(f"[prices] {stored} intraday bars over {len(watched)} stocks")
    return {"stored": stored}


# A quote younger than this is being kept current by the price stream, so the
# fast poll leaves it alone. Comfortably more than one stream flush (5 s) plus
# a scheduler tick's jitter, and well under the five-minute poll it defers to.
QUOTE_FRESH_SECONDS = 120


def refresh_watchlist(range_: str = "5d", stale_only: bool = False) -> dict:
    """Scheduled job: refresh every followed stock, concurrently.

    Sequentially this took one full chain-walk per stock, so a twenty-stock
    watchlist was minutes behind the market by the time it finished — the
    opposite of the point. The per-host token buckets in the scraper still
    pace each source, so this parallelises across stocks without hitting any
    one source harder than the sequential version did.

    `stale_only` skips every stock whose quote is under QUOTE_FRESH_SECONDS
    old, which in practice means the ones the stream is feeding. A stock with
    no quote at all counts as stale. Database age rather than asking the
    stream thread is the point: it needs no shared state, and a stream that
    dies hands its stocks back simply by going quiet.
    """
    if stale_only:
        # price_updated_at is canonical ISO, so string order is time order.
        cutoff = to_iso(now_utc() - timedelta(seconds=QUOTE_FRESH_SECONDS))
        watched = [row["short_name"] for row in db.watchlist.get_watchlist()
                   if (row["price_updated_at"] or "") < cutoff]
    else:
        watched = db.watchlist.get_symbols()
    if not watched:
        return {"updated": [], "failed": []}

    def refresh_one(short_name: str) -> bool:
        try:
            ok = bool(refresh_stock(short_name, range_=range_))
            # After the quote, so a classification lookup can never cost the
            # price its refresh. Only ever does work once per stock.
            stock = db.stocks.get_stock(short_name)
            if stock:
                ensure_classified(stock)
            return ok
        except Exception as exc:                      # one bad symbol must not
            print(f"[prices] {short_name} errored: {exc}")   # stall the rest
            return False

    workers = min(settings.SCRAPE_CONCURRENCY, len(watched))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prices") as pool:
        outcomes = list(pool.map(refresh_one, watched))

    updated = [s for s, ok in zip(watched, outcomes) if ok]
    failed = [s for s, ok in zip(watched, outcomes) if not ok]

    print(f"[prices] {len(updated)} updated, {len(failed)} failed {failed if failed else ''}")
    return {"updated": updated, "failed": failed}


def get_series(short_name: str, days: int = 30) -> list[dict]:
    """Chart data, backfilling from the network if the DB has nothing yet."""
    since = days_ago_iso(days)
    series = db.prices.get_series(short_name, since)
    if not series:
        refresh_stock(short_name, range_="1mo")
        series = db.prices.get_series(short_name, since)
    return series
