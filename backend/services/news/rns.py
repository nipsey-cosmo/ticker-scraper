"""RNS — UK regulatory announcements.

RNS carries 70%+ of UK company news: results, trading updates, director
dealings, the lot. Nothing else in this package covers it, and the README
already concedes the depth here is US-biased. For a London listing this is the
equivalent of what SEC EDGAR is for a New York one — the announcement itself,
before anybody writes about it.

**Licensing.** LSEG's official RNS feed is a commercial product. Investegate
republishes announcements and its terms forbid redistribution and commercial
use, which is fine for a personal tool reading its own watchlist and is a
blocker the day Stocky is sold or hosted for other people. Publishing this
source code is a separate question and not restricted — the terms cover their
content, not a description of how to read it — which is why no captured
Investegate page is committed as a test fixture. The tests use a synthetic one.

    ponytail: personal-use ceiling. The upgrade path if Stocky ever ships as a
    product is a licensed LSEG RNS feed, which is a swap of `_INDEX_URL` and
    `parse`, not a rewrite of anything above them.

**Parse the links, not the layout.** The announcement URL is self-describing:

    /announcement/rns/pinewood-technologies-group--pine/notification-and-...-of-transactions/9743377
                      ^ company slug        ^ ticker    ^ headline slug              ^ id

Everything needed is in there, so this reads anchors rather than table cells.
A listing-page redesign moves the rows around; it does not change the shape of
the permalinks they point at.

**A firehose, like the wires.** One index page carries every company's latest
announcements, so it is fetched once per cycle and shared across the watchlist
rather than fetched per stock.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import timedelta

import settings
from normalize import domain_of, now_utc

from ..dedup import is_relevant
from ..http_client import scraper

SOURCE_TYPE = "RNS"

_INDEX_URL = "https://www.investegate.co.uk/source/RNS"
_MAX_ENTRIES = 60

# One index page serves every stock, so the cache is what stops a twenty-stock
# watchlist pulling the same page twenty times. Matched to the news cadence.
_TTL = settings.NEWS_REFRESH_MINUTES * 60

_cache: tuple[float, list[dict]] | None = None
_cache_lock = threading.Lock()

# /announcement/<source>/<company-slug>--<ticker>/<headline-slug>/<id>
# The ticker is optional: a few issuers are listed without one.
_ANNOUNCEMENT = re.compile(
    r"/announcement/(?P<source>[a-z0-9-]+)/(?P<company>[^/]+?)(?:--(?P<ticker>[a-z0-9.]+))?"
    r"/(?P<headline>[^/]+)/(?P<id>\d+)",
    re.IGNORECASE,
)

# "1 hour ago", "23 minutes ago", "2 days ago".
_RELATIVE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week)s?\s+ago", re.IGNORECASE)

_UNITS = {"second": "seconds", "minute": "minutes", "hour": "hours",
          "day": "days", "week": "weeks"}


def fetch() -> list[dict]:
    """Every announcement on the index, fetched once per cycle and shared.

    An empty cycle is cached too: an index that just failed will not answer any
    better for the next nineteen stocks.
    """
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < _TTL:
            return _cache[1]

        response = scraper.get(_INDEX_URL)
        articles = parse(response.text) if response and response.ok else []
        _cache = (now, articles)
        return articles


def for_stock(short_name: str, company_name: str,
              aliases: tuple[str, ...] = ()) -> list[dict]:
    """The firehose narrowed to one company.

    Narrowed here rather than left to `dedup.prepare`, for the same reason the
    wires are: a few hundred announcements counted as "found for this stock"
    would make the per-source yield meaningless and hide a dead index. The test
    is `dedup.is_relevant`, the same one every other source is held to.
    """
    return [article for article in fetch()
            if is_relevant(article["title"], short_name, company_name, aliases)]


def parse(html: str) -> list[dict]:
    """Split from fetch so it can be tested against a saved page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        if len(articles) >= _MAX_ENTRIES:
            break

        match = _ANNOUNCEMENT.search(anchor["href"])
        if not match or match.group("id") in seen:
            continue
        seen.add(match.group("id"))

        # The company comes from the URL slug, never the anchor text. Which of
        # the two the anchor holds depends on the listing — the archive shows
        # the company, the per-source index shows the headline — but the slug
        # is the same on both, so the link is the thing to trust.
        ticker = (match.group("ticker") or "").upper()
        company = _company(match.group("company"), ticker)
        headline = anchor.get_text(" ", strip=True) or _titleise(match.group("headline"))
        if not company or not headline:
            continue

        url = anchor["href"]

        articles.append({
            # Company first, because that is what the relevance filter and the
            # dedup key both need to see. The ticker rides along when the URL
            # named one: an LSE ticker in the title is what lets a watchlist
            # entry stored as `PINE.L` match.
            "title": f"{company} ({ticker}): {headline}" if ticker
                     else f"{company}: {headline}",
            "url": url if url.startswith("http") else f"https://www.investegate.co.uk{url}",
            "published_at": _published(anchor),
            "description": None,
            "image": None,
            "source": f"RNS via {domain_of(url) or 'investegate.co.uk'}",
            "source_domain": domain_of(url) or "investegate.co.uk",
            "source_type": SOURCE_TYPE,
        })

    return articles


def _published(anchor) -> object:
    """The listing states age, not time — "1 hour ago". Resolved against now.

    An unparseable or missing stamp becomes now rather than None: this index
    only carries recent announcements, so "just seen it" is a better answer
    than no timestamp at all, which would sort the row to the epoch.
    """
    row = anchor.find_parent(["li", "tr", "article", "div"])
    text = row.get_text(" ", strip=True) if row else ""
    match = _RELATIVE.search(text)
    if not match:
        return now_utc()
    amount, unit = int(match.group(1)), _UNITS[match.group(2).lower()]
    return now_utc() - timedelta(**{unit: amount})


def _company(slug: str, ticker: str) -> str:
    """"dcc-energy-plc" -> "DCC Energy Plc".

    Title case with the ticker word restored to capitals, which is the one that
    reads as broken otherwise. Only display: `is_relevant` lowercases before it
    matches, so the case here never decides whether an announcement is kept.
    """
    words = [w for w in (slug or "").replace("-", " ").split() if w]
    return " ".join(w.upper() if ticker and w.upper() == ticker else w.capitalize()
                    for w in words)


def _titleise(slug: str) -> str:
    """"notification-of-transactions" -> "Notification of transactions"."""
    words = (slug or "").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def reset_cache() -> None:
    """For tests, and for a process that wants the next call to go out."""
    global _cache
    with _cache_lock:
        _cache = None
