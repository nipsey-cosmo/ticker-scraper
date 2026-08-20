"""Newswires — the company's own words, before anyone reports on them.

A contract win, a trial result, a guidance change or a launch is announced by
the company on a wire, and every article about it is a rewrite of that release
minutes or hours later. For a non-US listing the wire is often the only English
text that exists at all.

Three feeds, all free and keyless. ACCESS Newswire was the fourth and is left
out: its RSS sits behind a WAF that returns a flat 403 to this client, which is
exactly what 3.4's browser fingerprint would fix — add it there, not here.

**These feeds are not per-ticker.** One pull covers the entire watchlist, so it
is fetched once per cycle and shared; asking per stock would be thirty requests
for identical bytes. That is what `_cache` is for, and why `fetch` takes no
arguments.

**Half of Business Wire's home feed is translations.** A sample pull was 19
English items out of 39, the rest being the same releases in Arabic, Japanese,
German and six other languages. They carry the ticker, so relevance keeps them,
and dedup compares token overlap, which cannot match Arabic to English — a
holder would see one announcement five times. Both wires that translate put the
language in the URL path, so that is where it is caught.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit

import feedparser

import settings
from normalize import domain_of, parse_datetime

from ..dedup import is_relevant
from ..http_client import scraper
from .yahoo_news import clean_summary

SOURCE_TYPE = "WIRES"

_FEEDS = (
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
                      "GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("Business Wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRVQ=="),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
)
_MAX_ENTRIES = 60

# Matched to the fast lane's cadence: shorter and a single sweep would refetch
# halfway through, longer and the lane would be serving the previous tick.
_TTL = settings.NEWS_FAST_MINUTES * 60

_LANGUAGE_SEGMENT = re.compile(r"/([a-z]{2})(?=/|$)")

_cache: tuple[float, list[dict]] | None = None
_cache_lock = threading.Lock()


def fetch() -> list[dict]:
    """Every wire item on offer, fetched once per cycle and shared.

    A cycle that returns nothing is cached too: three feeds that just failed
    will not answer any better for the next twenty-nine stocks.
    """
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < _TTL:
            return _cache[1]

        articles: list[dict] = []
        for name, url in _FEEDS:
            response = scraper.get(url)
            if response and response.ok:
                articles += parse(response.text, name)

        _cache = (now, articles)
        return articles


def for_stock(short_name: str, company_name: str,
              aliases: tuple[str, ...] = ()) -> list[dict]:
    """The firehose narrowed to one company.

    Narrowed here rather than left to `dedup.prepare` purely for the
    accounting: a few hundred wire items counted as "found for this stock"
    would make 1.5's per-source yield meaningless and hide a dead feed. The
    test is `dedup.is_relevant` — the same one every other source is held to,
    not a second matcher.
    """
    return [article for article in fetch()
            if is_relevant(f"{article['title']} {article.get('description') or ''}",
                           short_name, company_name, aliases)]


def parse(xml: str, wire: str) -> list[dict]:
    """Split from fetch so it can be tested against a saved feed."""
    feed = feedparser.parse(xml)
    articles = []

    for entry in feed.entries[:_MAX_ENTRIES]:
        title = (entry.get("title") or "").strip()
        link = entry.get("link")
        if not title or not link or not _is_english(link):
            continue

        articles.append({
            "title": title,
            "url": link,
            "published_at": parse_datetime(entry.get("published") or entry.get("updated")),
            "description": clean_summary(entry.get("summary")),
            "image": _enclosure_image(entry),
            "source": wire,
            "source_domain": domain_of(link) or "globenewswire.com",
            "source_type": SOURCE_TYPE,
        })

    return articles


def _enclosure_image(entry) -> str | None:
    """Business Wire attaches the release's own image to the item. Free here,
    and it saves the Open Graph fetch `images.py` would otherwise make."""
    for enclosure in entry.get("enclosures") or ():
        href = enclosure.get("href")
        if href and str(enclosure.get("type") or "").startswith("image/"):
            return href
    return None


def _is_english(url: str) -> bool:
    """A language segment in the path must say `en`. PR Newswire has none at
    all, which is not the same as being a translation, so no segment passes."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    found = _LANGUAGE_SEGMENT.findall(path)
    return not found or "en" in found


def reset_cache() -> None:
    """For tests, and for a process that wants the next call to go out."""
    global _cache
    with _cache_lock:
        _cache = None
