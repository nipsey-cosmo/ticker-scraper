"""Publisher news sitemaps — the file each newsroom writes for Google News.

A news sitemap lists a publisher's last two days of articles with the exact
publish time, usually the lead image, and often `stock_tickers`: the symbols
the publisher's own editors tagged the article with. One fetch per publisher
serves the whole watchlist, so like the wires it is fetched once per cycle and
shared — and like the wires it is a firehose narrowed per stock in `for_stock`.

**Tags supplement the title test, they never replace it.** An entry is for a
stock when its tags hold the stock's US symbol or its title names the company.
A tag-only match reaches `dedup.prepare` without naming the stock, so this
source is in `_RELATED_ALLOWED` and the article is stored as `related`; spam,
churn and the other-company test still run on it there.

**Tags are US-only.** A bare `BA` in a tag is Boeing. Matching on the root of a
London symbol would hand BAE Systems (`BA.L`) every Boeing story, so a non-US
listing is matched on its name alone, and this adds almost no non-US depth.

**A long tag list is a market wrap.** Google reads at most five tickers; CNBC
and Barron's list up to thirty-seven, and those articles are about the market,
not about each name on the list. More than five symbols counts as none.

CNBC (`/sitemap_news.xml`, skipping `/make-it/` and `/select/`) was in the
first set and is left out: its newest entry was 52 hours old on 2026-11-23, so
the file has stopped updating. Add it back when the live test says otherwise.
Sitemap indexes are not followed: the URLs below are the news files themselves,
read from each site's robots.txt.

Not in the two-minute lane. `events.detect` only sees the ids the inserting
refresh produced, and the fast lane runs with detection off, so anything it
inserted would never be judged; and a publisher's write-up is not the
minutes-matter announcement that lane exists for.

**Licensing.** Same position as `rns.py`: the publishers' terms were not read,
and these are their headlines. Fine for a personal tool reading its own
watchlist, a question to answer before Stocky is hosted for anyone else, and
the reason the test fixtures are synthetic rather than captured.

    ponytail: personal-use ceiling. Each publisher's terms would need reading,
    or the feed dropping, before this serves anyone but its owner.
"""
from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import urlsplit

import settings
from normalize import domain_of, now_utc, parse_datetime

from ..dedup import is_relevant
from ..http_client import scraper

SOURCE_TYPE = "SITEMAPS"

# (source, url, path prefixes to skip)
_FEEDS = (
    # `/data-news/` is automated copy: price moves and filings written by a bot.
    ("MarketWatch", "https://www.marketwatch.com/mw_news_sitemap.xml", ("/data-news/",)),
    ("Barron's", "https://www.barrons.com/bol_news_sitemap.xml", ()),
    ("Benzinga", "https://www.benzinga.com/sitemap-benzinga.com/www/googlenews.xml", ()),
    # `/article/` is contributor opinion, half the file; `/news/` is the newsroom.
    ("Seeking Alpha", "https://seekingalpha.com/sitemap_news.xml", ("/article/",)),
    # UK, and no tickers at all: it is matched on the name in the title.
    ("The Market Reporter", "https://www.themarketreporter.co.uk/news-sitemap.xml", ()),
)

# Exchange prefixes that mean a US listing. ARCA, BATS and OTCMKTS are how
# Benzinga and Seeking Alpha write NYSE Arca, Cboe BZX and OTC Markets.
_US_PREFIXES = frozenset({"US", "NASDAQ", "NYSE", "NYSEARCA", "ARCA", "AMEX",
                          "BATS", "OTC", "OTCMKTS"})
_MAX_TICKERS = 5

# Barron's writes the Nasdaq Composite as a bare `COMP`, which is also Compass
# Inc's NYSE symbol, so a Compass holder would get every index story.
# ponytail: one collision, found by reading one day of tags. Another one goes
# here too; past a handful, a list of each publisher's index codes.
_NOT_LISTINGS = frozenset({"COMP"})

# A news sitemap promises two days. MarketWatch's ran to a month, which would
# feed `prepare` old stories that sort as new to a freshly followed stock.
_MAX_AGE = timedelta(days=3)

# One file per publisher serves every stock. Matched to the hourly lane.
_TTL = settings.NEWS_REFRESH_MINUTES * 60

_cache: tuple[float, list[dict]] | None = None
_cache_lock = threading.Lock()


def fetch() -> list[dict]:
    """Every recent entry from every publisher, fetched once per cycle and shared.

    An empty cycle is cached too, as for the wires. A publisher that yields
    nothing is printed by name: the yield telemetry counts `SITEMAPS` as one
    source, so one dead feed would otherwise hide behind the other four.
    """
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < _TTL:
            return _cache[1]

        cutoff = now_utc() - _MAX_AGE
        articles: list[dict] = []
        for name, url, skip in _FEEDS:
            response = scraper.get(url)
            fresh = [a for a in (parse(response.text, name, skip)
                                 if response and response.ok else [])
                     if a["published_at"] and a["published_at"] >= cutoff]
            if not fresh:
                print(f"[sitemaps] {name} yielded nothing")
            articles += fresh

        _cache = (now, articles)
        return articles


def for_stock(company_name: str, us_ticker: str | None = None) -> list[dict]:
    """The firehose narrowed to one company: tagged with its US symbol, or named.

    The name test is `is_relevant` with no ticker. Its ticker path matches the
    symbol anywhere in the text, so a holder of `S` would get "Nvidia's
    earnings" and one of `T` every "don't"; here the publisher's tag is the
    ticker test, and an exact one.

    The `tickers` set rides along on each article. Nothing downstream reads it:
    `dedup.prepare` copies the fields it stores, so it never reaches a row.
    """
    return [article for article in fetch()
            if us_ticker in article["tickers"]
            or is_relevant(article["title"], "", company_name)]


def parse(xml: str, source: str, skip: tuple[str, ...] = ()) -> list[dict]:
    """Split from fetch so it can be tested against a saved sitemap.

    Tags are matched on local names because the prefix is the publisher's
    choice (`n:` at CNBC, `news:` elsewhere). And only the direct children of
    `<url>` are walked, never `iter()`: the image block carries its own `loc`
    and `title`, which would overwrite the article's.
    """
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return []
    if _local(root.tag) != "urlset":      # an index, or a bot check that parsed
        return []

    articles = []
    for entry in root:
        parts = {_local(child.tag): child for child in entry}
        loc = (parts["loc"].text or "").strip() if "loc" in parts else ""
        if not loc or "news" not in parts or urlsplit(loc).path.startswith(skip):
            continue

        news = {_local(el.tag): (el.text or "").strip() for el in parts["news"].iter()}
        title = news.get("title")
        if not title or (news.get("language") or "en").lower().split("-")[0] != "en":
            continue

        articles.append({
            "title": title,
            "url": loc,
            "published_at": parse_datetime(news.get("publication_date")),
            "description": None,
            "image": next((el.text.strip() for el in parts.get("image", ())
                           if _local(el.tag) == "loc" and el.text), None),
            "source": source,
            "source_domain": domain_of(loc),
            "source_type": SOURCE_TYPE,
            "tickers": _tickers(news.get("stock_tickers")),
        })

    return articles


def _tickers(raw: str | None) -> set[str]:
    """"NASDAQ:AAPL, US:MSFT, BRK.B, .SPX" -> {"AAPL", "MSFT", "BRK-B"}.

    Indices (`.SPX`, `:DJI`) and futures (`@GC26Z`) do not start with a letter
    or digit, and a foreign exchange prefix (`XLON:BA`) is not a US listing.
    Dots and slashes become Yahoo's dash, which is how the watchlist stores
    `BRK-B`.
    """
    symbols = set()
    for token in (raw or "").upper().split(","):
        token = token.strip()
        if not token[:1].isalnum():
            continue
        prefix, _, symbol = token.rpartition(":")
        if symbol and (not prefix or prefix in _US_PREFIXES):
            symbols.add(symbol.replace(".", "-").replace("/", "-"))
    symbols -= _NOT_LISTINGS
    return symbols if len(symbols) <= _MAX_TICKERS else set()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def reset_cache() -> None:
    """For tests, and for a process that wants the next call to go out."""
    global _cache
    with _cache_lock:
        _cache = None
