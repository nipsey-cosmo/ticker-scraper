"""Reads for the /graphs page.

Everything here is deliberately *daily* and raw. Bucketing into weeks happens
once, in Python (routes/graphs.py), so the price series and the news series
cannot end up on subtly different week boundaries — which is exactly the bug
that makes two charts on the same page disagree about the same fortnight.

Sentiment is read from `news`, not from the `stock_sentiment_history` rollup.
The rollup is a scheduled job and can lag the articles it summarises; a page
that draws per-article box plots beside a heatmap has to derive both from the
same rows or the two will contradict each other.
"""
from __future__ import annotations

from .connection import get_connection, rows
from .sentiment import _NEGATIVE, _POSITIVE


def daily_ohlc(short_names: list[str], since: str) -> list[dict]:
    """Open/high/low/close per stock per UTC day, over every observation stored.

    The table holds a daily bar *and* hourly snapshots, and this is the one
    place that wants both: the snapshots are the only record of what the price
    did between one close and the next, so they are what gives a day a range at
    all. Everything else in the app wants one number per day and would
    double-count them.

    It is still a close-derived range — the high is the highest observation we
    happened to take, not the day's actual high — which is why the UI never
    calls these exchange OHLC bars.
    """
    if not short_names:
        return []
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT short_name,
                   substr(ts, 1, 10)                        AS day,
                   (array_agg(close ORDER BY ts ASC))[1]    AS open,
                   MAX(close)                               AS high,
                   MIN(close)                               AS low,
                   (array_agg(close ORDER BY ts DESC))[1]   AS close
            FROM prices
            WHERE short_name = ANY(%s) AND ts >= %s
            GROUP BY short_name, day
            ORDER BY short_name, day
        """, (short_names, since)))


def daily_sentiment(short_names: list[str], since: str) -> list[dict]:
    """Per stock per day: mean score and the positive/negative/neutral split.

    `articles` counts *scored* articles only, so it is always the denominator
    the mean was taken over. Counting unscored ones here would make a day of
    ungraded coverage look like a day of neutral coverage.
    """
    if not short_names:
        return []
    with get_connection() as conn:
        return rows(conn.execute(f"""
            SELECT short_name,
                   substr(publish_time, 1, 10) AS day,
                   AVG(sentiment)                                                  AS avg_sentiment,
                   COUNT(*)                                                        AS articles,
                   COUNT(*) FILTER (WHERE sentiment >  {_POSITIVE})                AS positive,
                   COUNT(*) FILTER (WHERE sentiment <  {_NEGATIVE})                AS negative,
                   COUNT(*) FILTER (WHERE sentiment BETWEEN {_NEGATIVE} AND {_POSITIVE}) AS neutral
            FROM news
            WHERE short_name = ANY(%s) AND publish_time >= %s AND sentiment IS NOT NULL
            GROUP BY short_name, day
            ORDER BY short_name, day
        """, (short_names, since)))


def article_scores(short_name: str, since: str) -> list[dict]:
    """Every scored article's own value, for the distribution chart.

    One stock at a time on purpose: this is the only query on the page that is
    O(articles) rather than O(days), and a heavily covered ticker runs to a few
    thousand rows over 90 days. Fine for one stock, not for eleven.
    """
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT substr(publish_time, 1, 10) AS day, sentiment
            FROM news
            WHERE short_name = %s AND publish_time >= %s AND sentiment IS NOT NULL
            ORDER BY publish_time, id
        """, (short_name, since)))
