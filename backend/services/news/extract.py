"""Pull the article text out of the page, so scoring sees more than a headline.

Nothing else in this pipeline ever reads an article. Sentiment runs on a
headline and whatever teaser the feed supplied, and the event prompt gets the
same — which is why "Shares slide on guidance cut" and "Shares slide, but
management reaffirms guidance" have been scoring alike.

Costs one request per article, so it is gated exactly like `images.py`: only
articles already stored (which means dedup winners — a loser has no row),
capped per refresh, and always best-effort. An extraction that fails leaves
the row exactly as it was.

Two kinds of URL are skipped without a fetch rather than tried and dropped:

- **Google's `CBMi…` redirects.** There is no article at that address, only a
  consent interstitial. Decoding them is E3's job, and only worth doing if
  this lands first.
- **SEC filing pages.** A 10-K is megabytes of XBRL boilerplate and its useful
  content is the form type, which `sec_edgar` already puts in the title.

Where an article also needs an image, `images.py` has usually just fetched the
same URL — the scraper's response cache means the second read costs nothing.
"""
from __future__ import annotations

import trafilatura

import db
import settings

from ..http_client import scraper

# Enough for the lead and the paragraphs that carry the numbers. Whole articles
# would bloat the rows and neither consumer reads past the first few hundred
# characters anyway.
_MAX_CHARS = 4000
# Below this it is not an article. trafilatura falls back to the raw text of
# whatever it was given, so a consent wall, a paywall stub or a bare "403
# Forbidden" comes back looking like a successful extraction — and would then
# be scored as the article's sentiment.
_MIN_CHARS = 200


def extract_body(html: str, url: str) -> str | None:
    """Split out for testing: the article text, or None if there isn't one.

    `favor_precision` because a false positive here is worse than a miss: a
    body of cookie-banner and related-links copy would poison the sentiment
    score of an article that would otherwise have been judged on its headline.
    """
    try:
        text = trafilatura.extract(
            html, url=url,
            favor_precision=True,
            include_comments=False,
            include_tables=False,
        )
    except Exception:            # a malformed page must not fail the refresh
        return None
    text = (text or "").strip()
    return text[:_MAX_CHARS] if len(text) >= _MIN_CHARS else None


def fill_bodies(news_ids: list, limit: int | None = None) -> int:
    """Extract text for articles just stored. Returns rows updated.

    Scoped to the ids from this refresh rather than to every body-less row for
    the stock: a sweep inserts a handful per stock, so a cycle costs tens of
    fetches. Widening it to history would mean the cap times the watchlist,
    every hour, forever — for articles that were scored and judged long ago.
    """
    if not settings.EXTRACT_ARTICLE_BODIES or not news_ids:
        return 0

    limit = limit if limit is not None else settings.BODY_FETCH_LIMIT
    targets = db.news.get_missing_bodies(news_ids, limit=limit)
    if not targets:
        return 0

    responses = scraper.get_many([{"url": t["url"]} for t in targets])

    found = {}
    for target, response in zip(targets, responses):
        if not response or not response.ok:
            continue
        if "html" not in response.headers.get("content-type", "html"):
            continue
        body = extract_body(response.text, response.url)
        if body:
            found[target["id"]] = body

    updated = db.news.set_bodies(found)
    if updated:
        print(f"[extract] added {updated} article bodies")
    return updated
