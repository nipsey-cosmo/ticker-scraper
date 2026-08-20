"""The sources added for breadth: Bing News, Nasdaq RSS and SEC EDGAR.

Same contract as test_news_parsers.py — fixtures are trimmed copies of what
each source actually returned, so a parser that breaks when a source changes
shape fails here instead of quietly returning an empty feed in production.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from services.news import bing_news, nasdaq_news, publishers, sec_edgar

# Bing wraps every link in an apiclick redirect that carries the real URL in a
# query param, and attributes republished stories as "<publisher> on MSN".
BING_RSS = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0" xmlns:News="https://www.bing.com/news/search">
<channel><title>"Rocket Lab" stock - BingNews</title>
<item>
  <title>Why Rocket Lab stock is soaring today</title>
  <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=6a6b&amp;url=https%3a%2f%2fwww.fool.com%2finvesting%2f2026%2f07%2f30%2fwhy-rocket-lab-stock-is-soaring%2f&amp;c=123</link>
  <description>Rocket Lab has bounced back from yesterday's rout.</description>
  <pubDate>Thu, 30 Jul 2026 11:30:25 GMT</pubDate>
  <News:Source>The Motley Fool on MSN</News:Source>
  <News:Image>http://www.bing.com/th?id=ONUT.g9SH&amp;pid=News</News:Image>
</item>
<item>
  <title>Rocket Lab stock is in freefall</title>
  <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2finvezz.com%2fnews%2f2026%2f07%2f30%2frocket-lab-freefall%2f</link>
  <pubDate>Thu, 30 Jul 2026 02:28:00 GMT</pubDate>
  <News:Source>Invezz</News:Source>
</item>
<item>
  <title>Item with no link at all</title>
  <pubDate>Thu, 30 Jul 2026 02:28:00 GMT</pubDate>
</item>
</channel></rss>"""

NASDAQ_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel><title>AAPL Feed</title>
<item>
  <title>Apple Inc. Bottom Line Climbs In Q3</title>
  <link>https://www.nasdaq.com/articles/apple-inc-bottom-line-climbs-q3</link>
  <description>(RTTNews) - Apple Inc. (AAPL) revealed a profit for its third quarter.</description>
  <pubDate>Thu, 30 Jul 2026 21:35:52 +0000</pubDate>
</item>
<item>
  <title></title>
  <link>https://www.nasdaq.com/articles/empty-title</link>
  <pubDate>Thu, 30 Jul 2026 20:00:00 +0000</pubDate>
</item>
</channel></rss>"""

# Real data.sec.gov shape: `filings.recent` is columnar — parallel lists rather
# than a list of filings — and routine insider forms (4, 144) outnumber
# everything else. Nothing in it names the company except the top-level `name`.
SEC_SUBMISSIONS = json.dumps({
    "cik": 320193,
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "filings": {"recent": {
        "accessionNumber": ["0000320193-26-000081", "0000320193-26-000079",
                            "0000320193-26-000078", "0000320193-26-000077"],
        "filingDate": ["2026-07-30", "2026-07-28", "2026-07-27", "2026-07-25"],
        "acceptanceDateTime": ["2026-07-30T16:31:02.000Z", "2026-07-28T21:04:11.000Z",
                               "2026-07-27T18:22:40.000Z", "2026-07-25T20:15:58.000Z"],
        "form": ["8-K", "4", "144", "10-Q/A"],
        "items": ["2.02,9.01", "", "", ""],
        "primaryDocument": ["aapl-20260730.htm", "xslF345X05/wk-form4.xml",
                            "xslF144X01/primary_doc.xml", "aapl-20260627.htm"],
        "primaryDocDescription": ["8-K", "FORM 4", "FORM 144",
                                  "Quarterly report [Sections 13 or 15(d)]"],
    }},
})

# The ticker -> CIK map, keyed by row index rather than by symbol.
SEC_TICKERS = json.dumps({
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
})


class TestBing:
    def test_redirect_is_unwrapped_to_the_publisher_url(self):
        articles = bing_news.parse(BING_RSS)
        assert articles[0]["url"] == (
            "https://www.fool.com/investing/2026/07/30/why-rocket-lab-stock-is-soaring/"
        )
        assert articles[0]["source_domain"] == "fool.com"

    def test_unwrapping_is_what_earns_the_dedup_tie_break(self):
        """A wrapped bing.com link would rank 6, below every real publisher."""
        from normalize import source_rank
        articles = bing_news.parse(BING_RSS)
        assert source_rank(articles[0]["source_domain"]) < source_rank("bing.com")

    def test_image_and_description_are_kept(self):
        article = bing_news.parse(BING_RSS)[0]
        assert article["image"] == "http://www.bing.com/th?id=ONUT.g9SH&pid=News"
        assert article["description"] == "Rocket Lab has bounced back from yesterday's rout."

    def test_publish_time_is_parsed(self):
        article = bing_news.parse(BING_RSS)[0]
        assert article["published_at"].year == 2026
        assert article["published_at"].month == 7

    def test_items_without_a_link_are_skipped(self):
        assert len(bing_news.parse(BING_RSS)) == 2

    def test_unwrap_leaves_anything_unexpected_alone(self):
        """A working redirect beats a mangled link."""
        assert bing_news.unwrap("https://www.fool.com/x") == "https://www.fool.com/x"
        assert bing_news.unwrap("http://www.bing.com/news/apiclick.aspx?ref=x") == (
            "http://www.bing.com/news/apiclick.aspx?ref=x"
        )

    def test_republisher_suffix_resolves_to_the_original_publisher(self):
        assert publishers.domain_for("The Motley Fool on MSN") == "fool.com"
        assert publishers.domain_for("Barron's on MSN") == "barrons.com"

    def test_query_does_not_use_google_s_or_syntax(self):
        """Bing answers a multi-term OR with an empty feed where Google returns
        a full one, so the two searches cannot share a query builder."""
        from services.news import google_news
        query = bing_news.build_query("RKLB", "Rocket Lab Corp")
        assert query == '"Rocket Lab Corp" stock'
        assert " OR " not in query
        assert " OR " in google_news.build_query("RKLB", "Rocket Lab Corp")

    def test_query_falls_back_to_the_ticker_without_a_company_name(self):
        assert bing_news.build_query("RKLB", "") == '"RKLB" stock'
        assert bing_news.build_query("", "") == ""


class TestNasdaq:
    def test_parses_title_link_and_summary(self):
        article = nasdaq_news.parse(NASDAQ_RSS)[0]
        assert article["title"] == "Apple Inc. Bottom Line Climbs In Q3"
        assert article["url"] == "https://www.nasdaq.com/articles/apple-inc-bottom-line-climbs-q3"
        assert article["description"].startswith("(RTTNews)")
        assert article["source_domain"] == "nasdaq.com"
        assert article["source_type"] == "NASDAQ"

    def test_untitled_items_are_skipped(self):
        assert len(nasdaq_news.parse(NASDAQ_RSS)) == 1

    def test_is_not_trusted_for_articles_that_never_name_the_stock(self):
        """Asking Nasdaq for an unknown symbol returns generic market news
        rather than an empty feed, exactly like Yahoo's symbol-keyed
        endpoints — so it must never be allowed to contribute `related`."""
        from services.dedup import _RELATED_ALLOWED
        assert nasdaq_news.SOURCE_TYPE not in _RELATED_ALLOWED


class TestSecEdgar:
    def test_routine_insider_filings_are_dropped(self):
        """Forms 4 and 144 arrive dozens a month and would swamp the feed."""
        titles = [a["title"] for a in sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")]
        assert len(titles) == 2
        assert not any("FORM 4" in t or "FORM 144" in t for t in titles)

    def test_amendments_count_as_material(self):
        titles = [a["title"] for a in sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")]
        assert "Apple Inc. filed 10-Q/A: Quarterly report [Sections 13 or 15(d)]" in titles

    def test_title_names_the_company_because_the_filed_one_does_not(self):
        """A filing's own description is "8-K": no company, no ticker. Left as
        filed, every item would fail the relevance check and every 8-K would
        collide on the same dedup key."""
        from services.dedup import is_relevant
        article = sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")[0]
        assert article["title"] == "Apple Inc. filed 8-K"
        assert is_relevant(article["title"], "AAPL", "Apple Inc.")

    def test_a_description_that_only_repeats_the_form_is_dropped(self):
        """`primaryDocDescription` is usually the form again, which would read
        as "Apple Inc. filed 8-K: 8-K"."""
        article = sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")[0]
        assert article["description"] is None

    def test_acceptance_time_is_kept_not_just_the_filing_date(self):
        """The whole point of data.sec.gov over the Atom feed: a date alone
        gives every filing that day the same timestamp, so a two-minute lane
        cannot tell what has just landed."""
        article = sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")[0]
        assert article["published_at"].isoformat().startswith("2026-07-30T16:31:02")

    def test_url_points_at_the_document_under_the_unpadded_cik(self):
        article = sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc.")[0]
        assert article["url"] == (
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019326000081/aapl-20260730.htm"
        )
        assert article["source_domain"] == "sec.gov"

    def test_an_unparseable_payload_yields_nothing(self):
        assert sec_edgar.parse("<html><body>Not found.</body></html>", "X") == []
        assert sec_edgar.parse(json.dumps({"cik": 1}), "X") == []

    def test_short_columns_do_not_raise(self):
        """The columns are meant to be parallel. If one is not, the filing is
        dropped rather than taking the whole feed down."""
        payload = json.dumps({"cik": 320193,
                              "filings": {"recent": {"form": ["8-K"]}}})
        assert sec_edgar.parse(payload, "Apple Inc.") == []

    def test_is_skipped_entirely_without_a_declared_contact(self, monkeypatch):
        """SEC enforces its fair-access policy with 403s. No contact configured
        means the request would be refused anyway, so don't make it."""
        import settings
        calls = []
        monkeypatch.setattr(settings, "SEC_CONTACT", None)
        monkeypatch.setattr(sec_edgar.scraper, "get", lambda *a, **kw: calls.append(a))
        assert sec_edgar.fetch("AAPL", "Apple Inc.") == []
        assert calls == []

    def test_material_form_prefixes(self):
        assert sec_edgar.is_material("8-K")
        assert sec_edgar.is_material("424B5")
        assert sec_edgar.is_material("DEF 14A")
        assert not sec_edgar.is_material("4")
        assert not sec_edgar.is_material("SC 13G/A")


class TestSecCikMap:
    """Ten thousand rows that only change when a company lists or renames
    itself. Fetching it per stock would be a megabyte per stock per refresh."""

    @staticmethod
    def _stub(monkeypatch, urls):
        monkeypatch.setattr(sec_edgar, "_cik_by_ticker", {})
        monkeypatch.setattr(sec_edgar, "_cik_lock", threading.Lock())

        def fake_get(url, **kwargs):
            urls.append(url)
            body = SEC_TICKERS if "company_tickers" in url else SEC_SUBMISSIONS
            return SimpleNamespace(status=200, ok=True, text=body,
                                   json=lambda body=body: json.loads(body))

        monkeypatch.setattr(sec_edgar.scraper, "get", fake_get)

    def test_the_map_is_fetched_once_and_reused(self, monkeypatch):
        import settings
        monkeypatch.setattr(settings, "SEC_CONTACT", "someone@example.com")
        urls = []
        self._stub(monkeypatch, urls)

        sec_edgar.fetch("AAPL", "Apple Inc.")
        sec_edgar.fetch("TSLA", "Tesla, Inc.")

        assert sum("company_tickers" in u for u in urls) == 1
        assert "CIK0001318605.json" in urls[-1]

    def test_cik_is_zero_padded_to_ten_digits_in_the_url(self, monkeypatch):
        import settings
        monkeypatch.setattr(settings, "SEC_CONTACT", "someone@example.com")
        urls = []
        self._stub(monkeypatch, urls)

        sec_edgar.fetch("AAPL", "Apple Inc.")
        assert urls[-1] == "https://data.sec.gov/submissions/CIK0000320193.json"

    def test_an_unknown_ticker_makes_no_submissions_request(self, monkeypatch):
        """A CIK guessed from a ticker would fetch another company's filings."""
        import settings
        monkeypatch.setattr(settings, "SEC_CONTACT", "someone@example.com")
        urls = []
        self._stub(monkeypatch, urls)

        assert sec_edgar.fetch("SHEL", "Shell plc") == []
        assert not any("submissions" in u for u in urls)

    def test_sec_hosts_share_one_ceiling(self):
        """SEC states 10 req/s across all of its hosts combined, so the two
        cannot each claim it."""
        from services.http_client import _HOST_RATES
        combined = _HOST_RATES["www.sec.gov"][1] + _HOST_RATES["data.sec.gov"][1]
        assert combined <= 10.0


class TestCollectionAccounting:
    """`collect` is where per-source attribution still exists — after the merge
    in `refresh` there is no telling which source produced what, and a source
    that returned nothing leaves no trace at all."""

    @staticmethod
    def _quiet(monkeypatch):
        """Every source stubbed to return nothing, so the test is about the
        accounting rather than about parsing."""
        from services import news as news_pkg
        for module, names in (
            (news_pkg.google_news, ("fetch_recent", "fetch_backfill")),
            (news_pkg.yahoo_news, ("fetch_search", "fetch_rss")),
            (news_pkg.bing_news, ("fetch",)),
            (news_pkg.finviz, ("fetch",)),
            (news_pkg.nasdaq_news, ("fetch",)),
            (news_pkg.sec_edgar, ("fetch",)),
        ):
            for name in names:
                monkeypatch.setattr(module, name, lambda *a, **kw: [])

    def test_a_us_listing_asks_every_source(self, temp_db, monkeypatch):
        import db
        from services import news as news_pkg
        self._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)

        news_pkg.collect("AAPL", "Apple Inc.", "AAPL", "NMS")

        rows = {r["source_type"]: r for r in db.sources.yields()}
        assert set(rows) == {"GOOGLE_NEWS", "YAHOO_SEARCH", "YAHOO_RSS",
                             "BING_NEWS", "FINVIZ", "NASDAQ", "SEC_EDGAR"}
        assert all(r["fetches"] == 1 and r["skips"] == 0 for r in rows.values())

    def test_a_london_listing_records_the_us_sources_as_skipped(self, temp_db, monkeypatch):
        """Not as failures. Otherwise every non-US stock reports three dead
        scrapers and the signal is worthless."""
        import db
        from services import news as news_pkg
        self._quiet(monkeypatch)

        news_pkg.collect("SHEL", "Shell plc", "SHEL.L", "LSE")

        rows = {r["source_type"]: r for r in db.sources.yields()}
        for source in ("FINVIZ", "NASDAQ", "SEC_EDGAR"):
            assert rows[source]["skips"] == 1, source
            assert rows[source]["fetches"] == 0, source
            assert rows[source]["stalled"] is False, source
        assert rows["GOOGLE_NEWS"]["fetches"] == 1

    def test_edgar_without_a_contact_address_is_skipped_not_failed(self, temp_db, monkeypatch):
        import db
        from services import news as news_pkg
        self._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: False)

        news_pkg.collect("AAPL", "Apple Inc.", "AAPL", "NMS")

        edgar = {r["source_type"]: r for r in db.sources.yields()}["SEC_EDGAR"]
        assert edgar["skips"] == 1 and edgar["fetches"] == 0

    def test_a_source_that_raises_is_counted_as_a_barren_fetch(self, temp_db, monkeypatch):
        """It went out and produced nothing, which is exactly what `stalled`
        is meant to surface."""
        import db
        from services import news as news_pkg
        self._quiet(monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("selector gone")

        monkeypatch.setattr(news_pkg.bing_news, "fetch", boom)
        news_pkg.collect("SHEL", "Shell plc", "SHEL.L", "LSE")

        bing = {r["source_type"]: r for r in db.sources.yields()}["BING_NEWS"]
        assert bing["fetches"] == 1 and bing["articles"] == 0 and bing["stalled"] is True


class TestFastLane:
    """A filing is public the moment the SEC accepts it, so it gets a
    two-minute lane. Everything else is somebody's write-up and waits."""

    def test_only_the_lane_s_sources_are_asked(self, temp_db, monkeypatch):
        import db
        from services import news as news_pkg
        TestCollectionAccounting._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)

        news_pkg.collect("AAPL", "Apple Inc.", "AAPL", "NMS",
                         only=news_pkg.FAST_SOURCES)

        assert {r["source_type"] for r in db.sources.yields()} == {"SEC_EDGAR"}

    def test_a_source_outside_the_lane_is_not_recorded_as_skipped(self, temp_db, monkeypatch):
        """Not its turn is not the same as unavailable. Recording it as a skip
        every two minutes would drown the daily counts the flag reads."""
        import db
        from services import news as news_pkg
        TestCollectionAccounting._quiet(monkeypatch)

        # A London listing: EDGAR is genuinely skipped, the rest are just not
        # in this lane.
        news_pkg.collect("SHEL", "Shell plc", "SHEL.L", "LSE",
                         only=news_pkg.FAST_SOURCES)

        rows = {r["source_type"]: r for r in db.sources.yields()}
        assert set(rows) == {"SEC_EDGAR"}
        assert rows["SEC_EDGAR"]["skips"] == 1

    def test_the_fast_lane_does_not_run_event_detection(self, stocked_db, monkeypatch):
        """It is an LLM call per batch. Every two minutes across the watchlist
        would spend a day of tokens by lunchtime."""
        import jobs
        from services import news as news_pkg

        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)
        monkeypatch.setattr(
            news_pkg.sec_edgar, "fetch",
            lambda *a, **kw: sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc."))

        detected = []
        monkeypatch.setattr(news_pkg.events, "detect",
                            lambda *a, **kw: detected.append(a))

        assert jobs.refresh_news_fast()["inserted"] > 0
        assert detected == []

    def test_the_hourly_lane_still_judges(self, stocked_db, monkeypatch):
        import jobs
        from services import news as news_pkg

        TestCollectionAccounting._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)
        monkeypatch.setattr(
            news_pkg.sec_edgar, "fetch",
            lambda *a, **kw: sec_edgar.parse(SEC_SUBMISSIONS, "Apple Inc."))
        monkeypatch.setattr(news_pkg.images, "backfill_images", lambda *a, **kw: None)

        detected = []
        monkeypatch.setattr(news_pkg.events, "detect",
                            lambda *a, **kw: detected.append(a))

        jobs.refresh_news()
        assert detected
