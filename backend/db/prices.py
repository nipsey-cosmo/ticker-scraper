"""Price history.

Two kinds of row share the table, distinguished by `interval`:
  '1d'   — official daily close, timestamped at midnight UTC
  'snap' — whatever the price was at an hourly refresh

Daily bars give the 1-month chart; snapshots give intraday shape for the last
day or two without paying for a real intraday feed.

A row carries `open`, `high`, `low` and `volume` alongside `close`, all
nullable. Only the sources that return real bars fill them — a snapshot is one
number and leaves them NULL, which is honest, where a zero would render as a
trade that never happened.
"""
from __future__ import annotations

from .connection import executemany, get_connection, one, rows


def upsert_points(short_name: str, points, interval: str = "1d") -> int:
    """Store bars. Re-running a backfill is a no-op.

    Takes either shape:

        [("2026-08-19T00:00:00Z", 338.19)]                  close only
        [{"ts": ..., "close": ..., "open": ..., "high": ...,
          "low": ..., "volume": ...}]                       a full bar

    The tuple form exists because most callers genuinely have nothing but a
    close — an hourly snapshot is one number — and making them all build a
    dict to say so would be noise. Sources that return real bars (Yahoo's
    chart, Stooq) pass dicts.

    A missing OHLC field is stored NULL rather than defaulted: zero would
    render as a real trade at zero.
    """
    if not points:
        return 0

    rows_ = []
    for point in points:
        if isinstance(point, dict):
            bar = point
        else:
            ts, close = point
            bar = {"ts": ts, "close": close}
        if bar.get("close") is None:
            continue
        rows_.append((
            short_name, bar["ts"], bar["close"], interval,
            bar.get("open"), bar.get("high"), bar.get("low"), bar.get("volume"),
        ))

    if not rows_:
        return 0
    with get_connection() as conn:
        # COALESCE on update so a later close-only write (a snapshot landing on
        # the same slot as a bar) cannot blank an OHLC we already have.
        return executemany(conn, """
            INSERT INTO prices (short_name, ts, close, interval, open, high, low, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(short_name, ts, interval) DO UPDATE SET
                close  = excluded.close,
                open   = COALESCE(excluded.open,   prices.open),
                high   = COALESCE(excluded.high,   prices.high),
                low    = COALESCE(excluded.low,    prices.low),
                volume = COALESCE(excluded.volume, prices.volume)
        """, rows_)


def get_history(short_name: str, since: str, interval: str | None = None) -> list[dict]:
    where, params = ["short_name = %s", "ts >= %s"], [short_name, since]
    if interval:
        where.append("interval = %s")
        params.append(interval)
    with get_connection() as conn:
        return rows(conn.execute(
            f"SELECT ts, close, interval, open, high, low, volume FROM prices "
            f"WHERE {' AND '.join(where)} ORDER BY ts ASC",
            params,
        ))


def get_series(short_name: str, since: str) -> list[dict]:
    """Chart series: daily bars, plus snapshots for days with no bar yet (today
    and any day the daily feed hasn't published)."""
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT ts, close FROM prices
            WHERE short_name = %s AND ts >= %s
              AND (interval = '1d' OR substr(ts, 1, 10) NOT IN (
                    SELECT substr(ts, 1, 10) FROM prices
                    WHERE short_name = %s AND interval = '1d' AND ts >= %s
              ))
            ORDER BY ts ASC
        """, (short_name, since, short_name, since)))


def latest(short_name: str) -> dict | None:
    with get_connection() as conn:
        return one(conn.execute(
            "SELECT ts, close FROM prices WHERE short_name = %s ORDER BY ts DESC LIMIT 1",
            (short_name,),
        ))


def close_on_or_before(short_name: str, ts: str) -> dict | None:
    """Used for "change over the last N days" without a second network call."""
    with get_connection() as conn:
        return one(conn.execute("""
            SELECT ts, close FROM prices
            WHERE short_name = %s AND ts <= %s
            ORDER BY ts DESC LIMIT 1
        """, (short_name, ts)))


# ── Corporate actions ────────────────────────────────────────────────────

def record_split(short_name: str, ex_date: str, numerator: float,
                 denominator: float) -> bool:
    """Remember a split. True only the first time it is seen.

    That return value is the whole idempotency story for `adjust_for_split`:
    the unique key decides whether this split has already been applied, so a
    second refresh that sees the same event does nothing rather than halving
    the history again.
    """
    with get_connection() as conn:
        return conn.execute("""
            INSERT INTO splits (short_name, ex_date, numerator, denominator)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (short_name, ex_date) DO NOTHING
        """, (short_name, ex_date, numerator, denominator)).rowcount > 0


def adjust_for_split(short_name: str, ex_date: str, ratio: float) -> int:
    """Bring stored history onto the post-split scale.

    A 4-for-1 split quadruples the share count and quarters the price, so
    every bar *before* the ex-date was quoted on a scale four times too high.
    Prices divide by the ratio and volume multiplies by it.

    Only rows strictly before the ex-date move: the ex-date bar itself already
    arrived on the new scale.

    Callers must gate this on `record_split` returning True. Running it twice
    would adjust an already-adjusted series.
    """
    if ratio <= 0:
        return 0
    with get_connection() as conn:
        return conn.execute("""
            UPDATE prices SET
                close  = close / %s,
                open   = open  / %s,
                high   = high  / %s,
                low    = low   / %s,
                volume = (volume * %s)::BIGINT
            WHERE short_name = %s AND ts < %s
        """, (ratio, ratio, ratio, ratio, ratio, short_name, ex_date)).rowcount


def upsert_dividends(short_name: str, dividends: list[dict],
                     currency: str | None = None) -> int:
    """dividends = [{"ex_date", "amount"}], amounts already in major units."""
    if not dividends:
        return 0
    with get_connection() as conn:
        return executemany(conn, """
            INSERT INTO dividends (short_name, ex_date, amount, currency)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (short_name, ex_date) DO UPDATE SET
                amount = excluded.amount, currency = excluded.currency
        """, [(short_name, d["ex_date"], d["amount"], currency) for d in dividends])


def get_dividends(short_name: str, since: str = "") -> list[dict]:
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT ex_date, amount, currency FROM dividends
            WHERE short_name = %s AND ex_date >= %s
            ORDER BY ex_date DESC
        """, (short_name, since)))


def get_splits(short_name: str) -> list[dict]:
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT ex_date, numerator, denominator FROM splits
            WHERE short_name = %s ORDER BY ex_date DESC
        """, (short_name,)))


def delete_older_than(cutoff_iso: str) -> int:
    with get_connection() as conn:
        return conn.execute("DELETE FROM prices WHERE ts < %s", (cutoff_iso,)).rowcount
