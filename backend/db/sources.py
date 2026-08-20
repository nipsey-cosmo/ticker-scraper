"""How much each news source is actually returning.

`/health` already reports per-host request counters from the scraper, but those
are HTTP-level: they show that Bing answered 200 forty times, not that all
forty answers were empty. A source that quietly stops matching — a changed
query parameter, a renamed table id, a feed that now needs a header — keeps
returning 200 and nothing at all, and the feed just looks like a quiet news
day.

So the scrape records what it got, and the difference between "returned
nothing" and "was not asked" is kept, because three sources are US-only and
one is opt-in: without that distinction every non-US stock would look like
four broken sources.
"""
from __future__ import annotations

from normalize import days_ago_iso, now_utc

from .connection import executemany, get_connection, rows


def record(counts: dict[str, int], skipped: set[str] | None = None) -> None:
    """Add one collection's results. `counts` is source_type → articles found.

    Sources in `skipped` were not asked — they are counted separately so a
    London listing does not make Finviz, Nasdaq and EDGAR look dead.
    """
    today = now_utc().date().isoformat()
    batch = [(source, today, articles, 1, 0) for source, articles in counts.items()]
    batch += [(source, today, 0, 0, 1) for source in (skipped or ())]
    if not batch:
        return

    with get_connection() as conn:
        executemany(conn, """
            INSERT INTO source_yield (source_type, date, articles, fetches, skips)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_type, date) DO UPDATE SET
                articles = source_yield.articles + excluded.articles,
                fetches  = source_yield.fetches  + excluded.fetches,
                skips    = source_yield.skips    + excluded.skips
        """, batch)


def yields(days: int = 7) -> list[dict]:
    """Per source over the window, newest-heavy first.

    `stalled` is the number worth looking at: the source went out, repeatedly,
    and came back with nothing every time.
    """
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT source_type,
                   SUM(articles)::INT                       AS articles,
                   SUM(fetches)::INT                        AS fetches,
                   SUM(skips)::INT                          AS skips,
                   (SUM(fetches) > 0 AND SUM(articles) = 0) AS stalled
            FROM source_yield
            WHERE date >= %s
            GROUP BY source_type
            ORDER BY stalled DESC, articles ASC, source_type ASC
        """, (days_ago_iso(days)[:10],)))


def delete_older_than(cutoff_date: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM source_yield WHERE date < %s", (cutoff_date,)).rowcount
