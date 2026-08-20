"""News collection: fan out to every source, merge, dedupe, store, score.

Seven fetches across five independent operators, so no single company's outage
or rate limit can take the feed down:

    SEC EDGAR     the filing itself, before anyone reports on it (US, opt-in)
    Finviz        real publisher URLs, dense US coverage
    Yahoo search  thumbnails — the only free image source that costs no request
    Yahoo RSS     summaries
    Nasdaq RSS    wire copy and Nasdaq editorial, real URLs (US)
    Bing News     a second search index, unwrapped to real publisher URLs
    Google News   the broadest reach, and the only one that can backfill a month

Spreading the load this way is also the point: seven polite fetches across five
operators is far kinder to each of them than hammering one supplier, and it
needs no proxy or IP rotation to stay welcome.

GDELT was tried here and removed. Its global index sounds like the obvious way
to cover non-US listings, but the phrase search is too loose to use: `"shell"`
returns awards shows and retirement advice, and the legal suffix that would
make it precise (`"Shell plc"`) is absent from almost all body copy. Add to
that a 20-second TLS handshake and a rate limit that 429s at one request per
five seconds, and it cost more latency per refresh than the six remaining
sources combined while contributing articles the relevance filter then dropped.
Bing already supplies the non-US reach it was added for.

They are queried concurrently and merged, so a source going dark degrades
coverage instead of breaking the refresh. If every source returns nothing, that
genuinely means there is no news, not that a scraper broke — because five
independent things would have had to break at once.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import db
import settings
from normalize import days_ago_iso

from .. import dedup, events, sentiment_service, symbols
from . import bing_news, finviz, google_news, images, nasdaq_news, sec_edgar, yahoo_news

__all__ = ["collect", "refresh", "backfill", "refresh_watchlist", "images",
           "FAST_SOURCES"]

# The two-minute lane. Filings are the only source where minutes matter:
# an 8-K lands here before anybody has written about it. Everything else
# is somebody's write-up and can wait for the hour.
FAST_SOURCES = frozenset({sec_edgar.SOURCE_TYPE})


def collect(short_name: str, company_name: str, yahoo_symbol: str | None,
            exchange: str | None, days: int = 2,
            only: frozenset[str] | None = None) -> list[dict]:
    """Every source's articles for one stock, unmerged.

    `only` restricts the fan-out to one lane's sources. A source outside the
    lane is neither fetched nor recorded as skipped — it was not asked because
    this is not its turn, which is not the same as being unavailable.
    """
    ticker = yahoo_symbol or short_name
    us_ticker = ticker.split(".")[0]
    tasks: list[tuple[str, object]] = []
    skipped: set[str] = set()

    # Google News: the only source that can reach back more than a few days.
    if days > 3:
        tasks.append((google_news.SOURCE_TYPE,
                      lambda: google_news.fetch_backfill(short_name, company_name, days=days)))
    else:
        tasks.append((google_news.SOURCE_TYPE,
                      lambda: google_news.fetch_recent(short_name, company_name, days=days)))

    tasks.append((yahoo_news.SEARCH_TYPE, lambda: yahoo_news.fetch_search(ticker)))
    tasks.append((yahoo_news.RSS_TYPE, lambda: yahoo_news.fetch_rss(ticker)))
    tasks.append((bing_news.SOURCE_TYPE,
                  lambda: bing_news.fetch(short_name, company_name, days=days)))

    # Finviz, Nasdaq and EDGAR only carry US listings.
    if symbols.is_us_listing(exchange):
        tasks.append((finviz.SOURCE_TYPE, lambda: finviz.fetch(us_ticker)))
        tasks.append((nasdaq_news.SOURCE_TYPE, lambda: nasdaq_news.fetch(us_ticker)))
        # EDGAR declines itself when no contact address is configured. Left to
        # run it would report as a source that answered with nothing, which is
        # the exact signal a dead scraper gives — so the skip is named here
        # instead.
        if settings.sec_enabled():
            tasks.append((sec_edgar.SOURCE_TYPE,
                          lambda: sec_edgar.fetch(us_ticker, company_name)))
        else:
            skipped.add(sec_edgar.SOURCE_TYPE)
    else:
        skipped.update({finviz.SOURCE_TYPE, nasdaq_news.SOURCE_TYPE,
                        sec_edgar.SOURCE_TYPE})

    if only is not None:
        tasks = [task for task in tasks if task[0] in only]
        skipped &= only

    articles: list[dict] = []
    # Counted here rather than after the merge in refresh(): once the lists are
    # concatenated there is no telling which source produced what, and a source
    # returning nothing leaves no trace at all in the merged result.
    found: dict[str, int] = {}

    if not tasks:
        _record(found, skipped)
        return articles

    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="news") as pool:
        futures = [(source, pool.submit(task)) for source, task in tasks]
        for source, future in futures:
            try:
                batch = future.result() or []
            except Exception as exc:
                print(f"[news] {source} failed for {short_name}: {type(exc).__name__}: {exc}")
                batch = []
            found[source] = found.get(source, 0) + len(batch)
            articles += batch

    _record(found, skipped)
    return articles


def _record(found: dict[str, int], skipped: set[str]) -> None:
    try:
        db.sources.record(found, skipped)
    except Exception as exc:                 # telemetry must never fail a scrape
        print(f"[news] could not record source yield: {type(exc).__name__}: {exc}")


def _aliases(stock: dict) -> tuple[str, ...]:
    """Other tickers this stock is known by, for relevance matching.

    Trading212 calls Rocket Lab `6RJ0`; every article calls it `RKLB`.
    """
    resolved = stock.get("yahoo_symbol") or ""
    aliases = {resolved, resolved.split(".")[0]}
    return tuple(a for a in aliases if a)


def refresh(short_name: str, days: int = 2, score: bool = True,
            detect_events: bool = True, only: frozenset[str] | None = None) -> dict:
    """Scrape, dedupe and store news for one stock."""
    stock = db.stocks.get_stock(short_name) or {}
    company_name = stock.get("name") or short_name

    raw = collect(
        short_name, company_name,
        stock.get("yahoo_symbol"), stock.get("exchange"),
        days=days, only=only,
    )
    if not raw:
        # The fast lane finds nothing on most ticks for most stocks. Saying so
        # every two minutes per stock buries the hourly lane's output.
        if only is None:
            print(f"[news] no source returned anything for {short_name}")
        return {"short_name": short_name, "found": 0, "inserted": 0, "dropped": {}}

    existing = db.news.get_recent_fingerprints(short_name, days=max(days, 7))
    prepared = dedup.prepare(
        raw, short_name, company_name,
        existing=existing,
        aliases=_aliases(stock),
    )

    inserted_ids = db.news.insert_news_many(prepared.rows)

    for news_id, fields in prepared.enrichments.items():
        db.news.update_news(news_id, **fields)

    if score and inserted_ids:
        sentiment_service.score_news_ids(inserted_ids)

    # Same containment as a source failing in collect(): a dead LLM degrades
    # the refresh to "articles stored, nothing judged", it does not fail it.
    if detect_events and inserted_ids:
        try:
            events.detect(short_name, inserted_ids)
        except Exception as exc:
            print(f"[events] {short_name} failed: {type(exc).__name__}: {exc}")

    if inserted_ids or only is None:
        print(f"[news] {short_name}: {len(raw)} found -> {len(inserted_ids)} new "
              f"(dropped {prepared.dropped}, enriched {len(prepared.enrichments)})")

    return {
        "short_name": short_name,
        "found": len(raw),
        "inserted": len(inserted_ids),
        "enriched": len(prepared.enrichments),
        "dropped": prepared.dropped,
    }


def backfill(short_name: str, days: int | None = None) -> dict:
    """Deep pull for a newly followed stock: a month of history by default."""
    days = days or settings.BACKFILL_DAYS
    # A month of history is not "new events" — judging it would spend tokens
    # announcing last month's news. The next hourly refresh takes over.
    result = refresh(short_name, days=days, detect_events=False)
    images.backfill_images([short_name])
    return result


def refresh_watchlist(days: int = 2, only: frozenset[str] | None = None,
                      detect_events: bool = True) -> dict:
    """Scheduled sweep across every followed stock.

    `only=FAST_SOURCES` is the two-minute lane; the default is the hourly one.
    """
    watched = db.watchlist.get_symbols()
    if not watched:
        return {"stocks": 0, "inserted": 0}

    total_inserted = 0
    for short_name in watched:
        try:
            total_inserted += refresh(short_name, days=days, only=only,
                                      detect_events=detect_events)["inserted"]
        except Exception as exc:
            print(f"[news] {short_name} failed: {type(exc).__name__}: {exc}")

    # Both cost a fetch per article and a filing carries no image, so the
    # fast lane stores and moves on. Pruning is a daily concern either way.
    if only is None:
        images.backfill_images(watched)
        prune()

    print(f"[news] refresh complete: {total_inserted} new articles across {len(watched)} stocks")
    return {"stocks": len(watched), "inserted": total_inserted}


def prune() -> int:
    """Drop articles past the retention window so the DB doesn't grow forever."""
    removed = db.news.delete_older_than(days_ago_iso(settings.NEWS_RETENTION_DAYS))
    if removed:
        print(f"[news] pruned {removed} articles older than {settings.NEWS_RETENTION_DAYS}d")
    return removed
