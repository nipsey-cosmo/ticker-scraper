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


def delete_older_than(cutoff_iso: str) -> int:
    with get_connection() as conn:
        return conn.execute("DELETE FROM prices WHERE ts < %s", (cutoff_iso,)).rowcount
