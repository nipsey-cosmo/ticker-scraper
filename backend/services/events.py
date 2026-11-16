"""Event detection: what actually happened, as opposed to what was written.

The feed's remaining blind spot after deduplication is "so what?". Clustering
collapses fifty copies of one wire story into one row, but it cannot tell the
50th rewrite of last week's news from a genuinely new development, and it can
never say "nothing happened today" — the one answer a busy holder most wants.

So one LLM call per stock per refresh asks a single question: *would an investor
who already knows the last week's coverage learn anything materially new?* The
database is the prior — no knowledge base, no embeddings, no vector store. What
we already stored **is** what the investor already knows.

Zero events is the expected common answer, not a failure.

Containment is the design constraint: a refresh must survive a dead key, a
timeout, a truncated reply and outright nonsense from the model. Every failure
path here returns `[]` and logs. Nothing raises.

Cost is controlled upstream, not here — services/dedup.py kills churn before it
reaches a prompt, and the call only happens when a refresh inserted something.
The user can also switch this off outright (`db.flags.LLM_SCRAPING`), which
costs the feed its impact tiers and nothing else: articles are stored before
this runs, so scraping with the LLM off is the same scrape minus the judgement.
"""
from __future__ import annotations

import json
from uuid import UUID

import db
from normalize import days_ago_iso

from . import ai_service, calendar

# Impact is a coarse tier on purpose. A confidence score or a "≈4% of quarterly
# revenue" materiality estimate would be fake precision: we have headlines, not
# fundamentals.
#
# The market reaction *is* computed, but as a window rather than a delta — see
# `attribute_pending` below for why that distinction is the whole answer to the
# unreliable-timestamp problem.
IMPACTS = frozenset({"high", "medium", "low"})

# An article no event claimed is, by definition, nothing materially new.
UNCLAIMED_IMPACT = "low"

_PRIOR_DAYS = 7
_PRIOR_LIMIT = 60      # same cap summarise_stock uses
# How many articles go into one call. Not a cap on the refresh: a batch larger
# than this is *paged*, so every inserted article is judged and comes out with
# a tier. Bounding the prompt matters because both the reasoning spend and the
# wall-clock scale with it — a 60-article call already takes ~57 seconds.
#
# Each page is judged independently, so one slow or malformed page costs its
# own articles a tier and no others.
# Dropped from 60 when bodies joined the prompt (2.3): a body costs roughly
# twice a description, so the page shrank to keep the prompt — and the ~57s
# wall-clock a 60-article call took — where they were.
_BATCH_SIZE = 40
_MAX_TOKENS = 900

SYSTEM_PROMPT = (
    "You are a financial analyst. You are given the news coverage an investor "
    "in one stock has already seen, then a batch of new articles. Identify "
    "zero or more materially NEW events in the new articles — developments "
    "someone who read the prior coverage would not already know.\n\n"
    "Most batches contain no new events. An empty list is the expected, "
    "correct answer for a batch of rewrites, opinion, analysis of known facts "
    "or routine coverage. Do not invent an event to fill the list.\n\n"
    "Explain why each event matters qualitatively, in one sentence. Cite "
    "figures only when an article states them; never estimate. Impact is one "
    "of 'high', 'medium', 'low'.\n\n"
    'Reply with JSON only: {"events": [{"headline": str, "why_it_matters": '
    'str, "previously_known": str or null, "impact": str, '
    '"article_numbers": [int]}]}'
)


def enabled() -> bool:
    """Whether a refresh should spend a call grading what it just stored.

    Checks the scraping flag, never the summaries one: switching off the silent
    per-refresh spend must leave the on-demand summary button working.
    """
    return ai_service.key_usable() and db.flags.get_flag(db.flags.LLM_SCRAPING)


def detect(short_name: str, inserted_ids: list[UUID]) -> list[dict]:
    """Judge a batch of newly stored articles against what we already hold.

    Returns the events written. Also stamps `impact` on every article in the
    batch: an event's tier for the articles backing it, 'low' for the rest.

    Off — no key, a rejected key, or the flag switched off — is a no-op, not a
    failure: articles are already stored by the time this runs, and they keep
    their NULL impact so a later refresh with the LLM back on can still judge
    them.
    """
    if not enabled() or not inserted_ids:
        return []

    articles = _fetch(inserted_ids)
    if not articles:
        return []

    pages = [articles[i:i + _BATCH_SIZE] for i in range(0, len(articles), _BATCH_SIZE)]
    if len(pages) > 1:
        print(f"[events] {short_name}: {len(articles)} new articles over {len(pages)} calls")

    written = []
    for page in pages:
        written += _judge(short_name, page, set(inserted_ids))

    if written:
        print(f"[events] {short_name}: {len(written)} new event(s) — "
              + "; ".join(f"[{e['impact']}] {e['headline']}" for e in written))
    else:
        print(f"[events] {short_name}: no new events in {len(articles)} articles")
    return written


def _judge(short_name: str, new_articles: list[dict], inserted_ids: set[UUID]) -> list[dict]:
    """One call over one page of articles.

    Contained on its own: any failure here leaves *this page* unjudged and
    returns, so a sibling page's articles still get their tier.
    """
    result = ai_service._complete(
        SYSTEM_PROMPT,
        _build_prompt(short_name, new_articles, inserted_ids),
        max_tokens=_MAX_TOKENS,
        json_mode=True,
    )
    if not result:
        print(f"[events] {short_name}: no response, leaving {len(new_articles)} articles unjudged")
        return []

    parsed = _parse(result["text"], len(new_articles))
    if parsed is None:
        print(f"[events] {short_name}: unparseable response, "
              f"leaving {len(new_articles)} articles unjudged")
        return []

    tokens = result["tokens_in"] + result["tokens_out"]
    written, claimed = [], {}

    for event in parsed:
        news_ids = [new_articles[n - 1]["id"] for n in event["article_numbers"]]
        event_id = db.events.insert_event(
            short_name,
            headline=event["headline"],
            why_it_matters=event["why_it_matters"],
            previously_known=event["previously_known"],
            impact=event["impact"],
            news_ids=news_ids,
            # Charged once against the first event: the call covered them all,
            # and splitting it would imply a per-event cost that wasn't paid.
            tokens_total=tokens if not written else 0,
        )
        for news_id in news_ids:
            claimed[news_id] = _stronger(claimed.get(news_id), event["impact"])
        written.append({**event, "id": event_id, "news_ids": news_ids})

    # Every article the model was shown comes out with a tier: the one its
    # event carried, or 'low' — nothing claimed it, which is the definition of
    # "nothing materially new here".
    for article in new_articles:
        db.news.update_news(
            article["id"], impact=claimed.get(article["id"], UNCLAIMED_IMPACT)
        )

    return written


# ── Prompt ───────────────────────────────────────────────────────────────

def _fetch(inserted_ids: list[UUID]) -> list[dict]:
    """The batch, newest first.

    The order is load-bearing twice over: article numbers in the prompt index
    into this list, so it must not depend on dict ordering or query plan; and
    paging cuts it in order, so the freshest news lands in the first call
    rather than being scattered across pages.
    """
    articles = [db.news.get_news_by_id(i) for i in inserted_ids]
    articles = [a for a in articles if a]
    articles.sort(key=lambda a: a["publish_time"], reverse=True)
    return articles


def _build_prompt(short_name: str, new_articles: list[dict],
                  inserted_ids: list[UUID]) -> str:
    prior = _prior_coverage(short_name, set(inserted_ids))
    events = db.events.recent_events(short_name, days=_PRIOR_DAYS)

    parts = [f"Stock: {short_name}", ""]

    # The one thing the model cannot infer from headlines. Without it,
    # "reports in three days" has to be guessed from coverage.
    upcoming = calendar.next_for(short_name)
    if upcoming:
        side = {"pre": " before the open", "post": " after the close"}
        parts += [f"UPCOMING: reports earnings on {upcoming['date']}"
                  f"{side.get(upcoming['when'], '')}.", ""]

    parts.append(f"PRIOR COVERAGE (last {_PRIOR_DAYS} days, already known):")
    parts.append("\n".join(f"- {h}" for h in prior) if prior
                 else "- (nothing on file — treat everything below as new)")

    if events:
        parts += ["", "EVENTS ALREADY IDENTIFIED:"]
        parts.append("\n".join(f"- {e['headline']}" for e in events))

    parts += ["", f"NEW ARTICLES ({len(new_articles)}), numbered:"]
    for n, a in enumerate(new_articles, start=1):
        line = f"{n}. [{a['publish_time'][:10]} · {a.get('source') or '?'}] {a['title']}"
        # The extracted body where there is one, the feed's teaser otherwise.
        # Truncated hard either way: this is the lead, not the article.
        blurb = a.get("body") or a.get("description")
        if blurb:
            line += f"\n   {blurb[:400]}"
        parts.append(line)

    return "\n".join(parts)


def _prior_coverage(short_name: str, exclude: set[UUID]) -> list[str]:
    stored = db.news.get_news(
        [short_name], since=days_ago_iso(_PRIOR_DAYS), limit=_PRIOR_LIMIT + len(exclude)
    )
    headlines = [a["title"] for a in stored if a["id"] not in exclude]
    return headlines[:_PRIOR_LIMIT]


# ── Parsing ──────────────────────────────────────────────────────────────

def _parse(text: str, article_count: int) -> list[dict] | None:
    """Validated events, or None if the reply was unusable.

    An empty list and None are different answers: [] means the model looked and
    found nothing new (so the articles get judged 'low'), None means we never
    got an opinion (so they stay NULL, unjudged).
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        print(f"[events] response was not JSON: {exc}")
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        print(f"[events] response had no events list: {str(payload)[:120]}")
        return None

    # One malformed event does not discard the rest: the useful ones are still
    # useful, and dropping the batch would lose a genuine high-impact event to
    # a typo in an adjacent one.
    events = []
    for raw in payload["events"]:
        event = _clean(raw, article_count)
        if event:
            events.append(event)
        else:
            print(f"[events] skipped a malformed event: {str(raw)[:120]}")
    return events


def _clean(raw, article_count: int) -> dict | None:
    if not isinstance(raw, dict):
        return None

    headline = _text(raw.get("headline"))
    why = _text(raw.get("why_it_matters"))
    impact = _text(raw.get("impact")).lower()
    if not headline or not why or impact not in IMPACTS:
        return None

    numbers = raw.get("article_numbers")
    if not isinstance(numbers, list):
        return None
    # Out-of-range numbers are dropped rather than clamped: a hallucinated
    # index must not silently attach an event to an unrelated article.
    valid = sorted({n for n in numbers if isinstance(n, int)
                    and not isinstance(n, bool) and 1 <= n <= article_count})
    if not valid:
        return None

    return {
        "headline": headline,
        "why_it_matters": why,
        "previously_known": _text(raw.get("previously_known")) or None,
        "impact": impact,
        "article_numbers": valid,
    }


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _stronger(current: str | None, incoming: str) -> str:
    """An article backing two events carries the higher tier of the two."""
    order = ["low", "medium", "high"]
    if current is None:
        return incoming
    return max(current, incoming, key=order.index)


# ── Did it actually move? ────────────────────────────────────────────────

# Two hours of trading, counted in 5m bars so an overnight gap is not mistaken
# for a flat reaction. Long enough for a real reaction to play out, short enough
# that the next piece of news is usually not the thing being measured.
_MOVE_BARS = 24

# How long after an event before its window is treated as closed. Comfortably
# more than _MOVE_BARS of trading, because those two hours are trading hours: an
# event at Friday's close does not finish its window until Monday morning.
_MOVE_SETTLE_HOURS = 72

# Intraday bars are pruned at a fortnight (jobs.prune), so an event older than
# that can never be attributed. Matching the two is what lets a failed
# attribution simply age out instead of needing a "we tried" column.
_MOVE_MAX_AGE_DAYS = 14


def attribute_pending() -> int:
    """Measure what the stock did after each event nobody has measured yet.

    This is the question `impact` cannot answer. The LLM says a development is
    material; this says whether anyone trading agreed. They disagree often, and
    the disagreement is the interesting part.

    Deliberately a *window*, not a delta. Scraped publish timestamps are good to
    about the hour, which is why no instant-to-instant reaction is computed
    anywhere here: the answer is "over the two trading hours from the bar the
    story landed in", both ends are stored, and nothing downstream should read
    more precision into it than that.

    Runs after the intraday pull rather than at insert time, because at insert
    time the bars being measured have not happened yet.
    """
    pending = db.events.needing_attribution(_MOVE_SETTLE_HOURS, _MOVE_MAX_AGE_DAYS)
    attributed = 0

    for event in pending:
        if not event["first_seen"]:
            continue                      # backing articles gone, nothing to anchor to
        move = measure(event["short_name"], event["first_seen"])
        if not move:
            continue                      # no intraday cover for that window
        db.events.set_move(event["id"], move["from_ts"], move["to_ts"],
                           move["percent"], move["volume_x"])
        attributed += 1

    if attributed:
        print(f"[events] attributed {attributed} of {len(pending)} pending event(s)")
    return attributed


def measure(short_name: str, since: str) -> dict | None:
    """The move over the trading window that follows `since`, or None.

    None whenever the window cannot be honestly measured — no bars, only one
    bar, or a zero base price — rather than a 0.0 that would read as "the market
    shrugged".
    """
    bars = db.prices.window_from(short_name, since, _MOVE_BARS + 1)
    if len(bars) < 2:
        return None

    base, end = bars[0], bars[-1]
    if not base["close"]:
        return None

    return {
        "from_ts": base["ts"],
        "to_ts": end["ts"],
        "percent": round((end["close"] - base["close"]) / base["close"] * 100, 3),
        "volume_x": _volume_ratio(short_name, bars[1:]),
    }


def _volume_ratio(short_name: str, bars: list[dict]) -> float | None:
    """How busy the window was against a median bar for this stock.

    A 2% move on ordinary volume and a 2% move on five times the usual are
    different events, and the percentage alone cannot tell them apart. None when
    there is nothing to compare against — an unknown ratio, not a ratio of 1.
    """
    volumes = [b["volume"] for b in bars if b["volume"]]
    typical = db.prices.typical_intraday_volume(short_name)
    if not volumes or not typical:
        return None
    return round((sum(volumes) / len(volumes)) / typical, 2)
