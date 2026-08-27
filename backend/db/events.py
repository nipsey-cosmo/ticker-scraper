"""Materially new developments, one row per judged event.

Distinct from `news`: fifty articles can back one event, and a day with heavy
coverage and nothing new produces no rows at all — which is the answer the feed
could not previously give.
"""
from __future__ import annotations

from uuid import UUID

from normalize import days_ago_iso, hours_ago_iso

from .connection import get_connection, rows


def insert_event(short_name: str, headline: str, why_it_matters: str,
                 impact: str, news_ids: list[UUID],
                 previously_known: str | None = None,
                 tokens_total: int = 0) -> UUID:
    with get_connection() as conn:
        return conn.execute("""
            INSERT INTO events (short_name, headline, why_it_matters,
                                previously_known, impact, news_ids, tokens_total)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (short_name, headline, why_it_matters, previously_known,
              impact, news_ids, tokens_total)).fetchone()["id"]


def get_events(short_names: list[str] | None = None, since: str | None = None,
               limit: int = 50) -> list[dict]:
    """Newest first. `short_names=None` means every stock; an empty list means
    none — the same distinction db.news.get_news makes, for the same reason."""
    if short_names is not None and not short_names:
        return []

    where, params = ["1=1"], []
    if short_names:
        where.append(f"short_name IN ({','.join(['%s'] * len(short_names))})")
        params += short_names
    if since:
        where.append("created_at >= %s")
        params.append(since)

    with get_connection() as conn:
        return rows(conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            [*params, limit],
        ))


def recent_events(short_name: str, days: int = 7, limit: int = 20) -> list[dict]:
    """Prior context for the next judgement: what we have already called an
    event for this stock."""
    return get_events([short_name], since=days_ago_iso(days), limit=limit)


# ── Move attribution ─────────────────────────────────────────────────────

def needing_attribution(settle_hours: int, max_age_days: int,
                        limit: int = 200) -> list[dict]:
    """Events whose reaction window has closed but which nobody has measured.

    Bounded at both ends. The near end waits for the window to finish, so an
    event is not attributed against half a reaction. The far end gives up:
    intraday bars are pruned after a fortnight, so an event older than that can
    never be attributed and would otherwise be re-examined on every pass
    forever. That bound is the only reason this needs no "tried and failed"
    column.

    `first_seen` is the earliest publish time across the backing articles —
    when the story first appeared, not when we happened to judge it.
    """
    with get_connection() as conn:
        return rows(conn.execute("""
            SELECT e.id, e.short_name,
                   (SELECT MIN(n.publish_time) FROM news n
                     WHERE n.id = ANY(e.news_ids)) AS first_seen
            FROM events e
            WHERE e.move_percent IS NULL
              AND e.created_at <= %s
              AND e.created_at >= %s
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT %s
        """, (hours_ago_iso(settle_hours), days_ago_iso(max_age_days), limit)))


def set_move(event_id: UUID, from_ts: str, to_ts: str, percent: float,
             volume_x: float | None) -> None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE events SET move_from_ts = %s, move_to_ts = %s,
                              move_percent = %s, move_volume_x = %s
            WHERE id = %s
        """, (from_ts, to_ts, percent, volume_x, event_id))
