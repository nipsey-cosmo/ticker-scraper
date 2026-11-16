"""Insider filing counts, per stock per month.

Deliberately not in `news`. Forms 3/4/5 are dozens a month and would swamp a
feed — that is why they are filtered out of it, and that reason has not changed.
But "four insider filings this month, none last month" is real signal, so they
are counted instead of published: a badge, not a headline.
"""
from __future__ import annotations

from .connection import executemany, get_connection, rows


def record_counts(short_name: str, counts: dict[tuple[str, str], int]) -> int:
    """`counts` is {(month, form): filings}, absolute rather than incremental.

    GREATEST on conflict, which is the whole idempotency story. A refresh
    recounts a bounded slice of recent filings, so re-running it writes the same
    totals — but as new filings push older ones out of that slice, an old
    month's count would start shrinking. Keeping the high-water mark means a
    month that has fallen out of the window holds the most complete number we
    ever saw for it rather than decaying towards zero.
    """
    if not counts:
        return 0
    with get_connection() as conn:
        return executemany(conn, """
            INSERT INTO insider_activity (short_name, month, form, filings)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (short_name, month, form) DO UPDATE SET
                filings = GREATEST(insider_activity.filings, excluded.filings)
        """, [(short_name, month, form, filings)
              for (month, form), filings in sorted(counts.items())])


def get_activity(short_name: str, months: int = 6) -> list[dict]:
    """Newest month first. One row per month per form."""
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT month, form, filings FROM insider_activity
            WHERE short_name = %s
            ORDER BY month DESC, form ASC
            LIMIT %s
        """, (short_name, months * 4)))
