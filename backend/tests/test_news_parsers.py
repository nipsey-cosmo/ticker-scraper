"""Source parsers, against captured payloads.

Fixtures are trimmed copies of what each source actually returned, so a parser
that breaks when a source changes shape fails here rather than silently
returning an empty feed in production.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.news import (extract, finviz, google_news, images, publishers,
                          rns, sitemaps, wires, yahoo_news)

GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>"Rocket Lab" - Google News</title>
<item>
  <title>Rocket Lab (RKLB) Stock May Be 16% Undervalued Following Space Force Contract - Yahoo Finance</title>
  <link>https://news.google.com/rss/articles/CBMilAFBVV95cUxOX2JCTHktUFdC?oc=5</link>
  <guid isPermaLink="false">CBMilAFBVV95cUxOX2JCTHktUFdC</guid>
  <pubDate>Fri, 24 Jul 2026 21:12:05 GMT</pubDate>
  <source url="https://finance.yahoo.com">Yahoo Finance</source>
</item>
<item>
  <title>Rocket Lab Stock Is Jumping for 2 Reasons - Barron's</title>
  <link>https://news.google.com/rss/articles/CBMiekFVX3lxTE1HYlItT1lhb3RO?oc=5</link>
  <pubDate>Mon, 27 Jul 2026 15:02:00 GMT</pubDate>
  <source url="https://www.barrons.com">Barron's</source>
</item>
<item>
  <title>No link here</title>
  <pubDate>Mon, 27 Jul 2026 15:02:00 GMT</pubDate>
</item>
</channel></rss>
"""

FINVIZ_HTML = """
<html><body>
<table id="news-table">
  <tr><td width="130" align="right">Jul-29-26 09:41AM</td>
      <td><a href="https://www.investors.com/news/cathie-wood-ark">ARK Buys Millions In Falling Space Names</a>
          <span>(Investor's Business Daily)</span></td></tr>
  <tr><td width="130" align="right">08:41AM</td>
      <td><a href="https://www.barrons.com/articles/spacex-stock">SpaceX Needs Easier Regulations</a>
          <span>(Barrons.com)</span></td></tr>
  <tr><td width="130" align="right">Jul-28-26 04:53PM</td>
      <td><a href="/news/373579/45-investing-insights">Schaeffer's insights</a>
          <span>(Schaeffer's Research)</span></td></tr>
  <tr><td width="130" align="right">04:43PM</td>
      <td><a href="https://www.marketwatch.com/story/space-stocks">Space stocks are falling hard</a>
          <span>(MarketWatch)</span></td></tr>
  <tr><td>malformed row</td></tr>
</table>
</body></html>
"""


class TestGoogleNewsQuery:
    def test_company_name_is_a_phrase_and_ticker_is_qualified(self):
        """A bare ticker would match most of the internet — `APP` and `F` are
        ordinary words — so tickers only appear inside qualified phrases."""
        query = google_news.build_query("RKLB", "Rocket Lab Corp")
        assert '"Rocket Lab Corp"' in query
        assert '"RKLB stock"' in query
        assert " RKLB " not in f" {query} "

    def test_ambiguous_ticker_is_never_left_bare(self):
        query = google_news.build_query("APP", "AppLovin")
        assert '"AppLovin"' in query
        assert '"APP stock"' in query

    def test_ticker_equal_to_the_name_is_not_duplicated(self):
        assert google_news.build_query("BTC", "BTC") == '"BTC"'

    def test_missing_company_name_still_yields_a_query(self):
        assert google_news.build_query("RKLB", "") == '"RKLB stock" OR "RKLB shares"'


class TestGoogleNewsParse:
    def test_entries_are_parsed_with_publisher_and_date(self):
        articles = google_news._parse(GOOGLE_RSS)
        assert len(articles) == 2
        assert articles[0]["source"] == "Yahoo Finance"
        assert articles[0]["published_at"].isoformat().startswith("2026-07-24T21:12:05")

    def test_appended_outlet_is_stripped_from_the_title(self):
        articles = google_news._parse(GOOGLE_RSS)
        assert articles[1]["title"] == "Rocket Lab Stock Is Jumping for 2 Reasons"

    def test_publisher_name_is_mapped_to_a_domain(self):
        """Google's links are opaque redirects, so without this every item would
        look like it came from news.google.com and lose every dedup tie-break."""
        articles = google_news._parse(GOOGLE_RSS)
        assert articles[1]["source_domain"] == "barrons.com"
        assert articles[0]["source_domain"] == "finance.yahoo.com"

    def test_the_redirect_link_is_kept_because_it_still_works(self):
        """Resolving it needs a POST per article; the token redirects correctly
        in a browser, so we keep it and spend no requests."""
        articles = google_news._parse(GOOGLE_RSS)
        assert articles[0]["url"].startswith("https://news.google.com/rss/articles/")

    def test_entry_without_a_link_is_skipped(self):
        assert all(a["url"] for a in google_news._parse(GOOGLE_RSS))

    def test_empty_feed_is_safe(self):
        assert google_news._parse("") == []
        assert google_news._parse("<rss></rss>") == []


class TestFinvizParse:
    def test_rows_are_parsed_with_real_publisher_urls(self):
        articles = finviz.parse(FINVIZ_HTML)
        assert len(articles) == 4
        assert articles[0]["source_domain"] == "investors.com"
        assert articles[3]["source_domain"] == "marketwatch.com"

    def test_publisher_name_comes_from_the_row(self):
        assert finviz.parse(FINVIZ_HTML)[0]["source"] == "Investor's Business Daily"

    def test_time_only_rows_inherit_the_previous_date(self):
        """Finviz prints the date on the first row of a day and the time alone
        after it; without carrying it forward those rows lose their date."""
        articles = finviz.parse(FINVIZ_HTML)
        assert articles[0]["published_at"].date().isoformat() == "2026-07-29"
        assert articles[1]["published_at"].date().isoformat() == "2026-07-29"
        assert articles[2]["published_at"].date().isoformat() == "2026-07-28"
        assert articles[3]["published_at"].date().isoformat() == "2026-07-28"

    def test_market_times_are_converted_to_utc(self):
        """09:41 Eastern in July is 13:41 UTC."""
        assert finviz.parse(FINVIZ_HTML)[0]["published_at"].hour == 13

    def test_relative_links_are_absolute(self):
        assert finviz.parse(FINVIZ_HTML)[2]["url"].startswith("https://finviz.com/news/")

    def test_finviz_hosted_item_is_attributed_to_its_publisher(self):
        assert finviz.parse(FINVIZ_HTML)[2]["source_domain"] == "schaeffersresearch.com"

    def test_malformed_rows_are_skipped(self):
        assert all(a["title"] and a["url"] for a in finviz.parse(FINVIZ_HTML))

    def test_page_without_the_table_returns_nothing(self):
        assert finviz.parse("<html><body>blocked</body></html>") == []


class TestYahooThumbnails:
    def test_the_widest_resolution_wins(self):
        best = yahoo_news._best_thumbnail({"resolutions": [
            {"url": "https://i/small.jpg", "width": 140},
            {"url": "https://i/large.jpg", "width": 1200},
        ]})
        assert best == "https://i/large.jpg"

    @pytest.mark.parametrize("thumbnail", [None, {}, {"resolutions": []}, "nonsense"])
    def test_missing_thumbnails_are_safe(self, thumbnail):
        assert yahoo_news._best_thumbnail(thumbnail) is None

    def test_html_in_a_summary_is_stripped(self):
        assert yahoo_news.clean_summary("<p>Apple <b>beat</b> estimates</p>") == \
            "Apple beat estimates"


class TestPublisherMapping:
    @pytest.mark.parametrize("name,domain", [
        ("Barron's", "barrons.com"),
        ("Reuters", "reuters.com"),
        ("The Motley Fool", "fool.com"),
        ("Investor's Business Daily", "investors.com"),
        ("reuters.com", "reuters.com"),
        ("www.ft.com", "ft.com"),
    ])
    def test_known_publishers_map(self, name, domain):
        assert publishers.domain_for(name) == domain

    def test_unknown_publisher_gets_no_domain_rather_than_a_guess(self):
        """A wrong domain would corrupt source ranking; empty just means
        'average source'."""
        assert publishers.domain_for("Some Local Paper") == ""

    @pytest.mark.parametrize("name", [None, ""])
    def test_missing_publisher_is_safe(self, name):
        assert publishers.domain_for(name) == ""


class TestImageExtraction:
    def test_og_image_is_preferred(self):
        html = """<html><head>
            <meta property="og:image" content="https://cdn/hero.jpg">
            <meta name="twitter:image" content="https://cdn/other.jpg">
            </head><body><img src="https://cdn/inline.jpg"></body></html>"""
        assert images.extract_image(html, "https://x.com/a") == "https://cdn/hero.jpg"

    def test_twitter_image_is_the_fallback(self):
        html = '<html><head><meta name="twitter:image" content="https://cdn/t.jpg"></head></html>'
        assert images.extract_image(html, "https://x.com/a") == "https://cdn/t.jpg"

    def test_relative_urls_are_resolved(self):
        html = '<html><head><meta property="og:image" content="/img/hero.jpg"></head></html>'
        assert images.extract_image(html, "https://x.com/story/1") == "https://x.com/img/hero.jpg"

    def test_svgs_and_tracking_pixels_are_skipped(self):
        html = """<html><body>
            <img src="https://cdn/logo.svg">
            <img src="https://cdn/pixel.gif" width="1" height="1">
            <img src="https://cdn/real.jpg" width="800">
            </body></html>"""
        assert images.extract_image(html, "https://x.com/a") == "https://cdn/real.jpg"

    def test_page_with_no_usable_image(self):
        assert images.extract_image("<html><body><p>text</p></body></html>", "https://x.com") is None


# Trimmed from live pulls of all three wires. The Arabic item is the trap:
# Business Wire's home feed carries every release in up to ten languages, they
# all name the ticker, and token overlap cannot match Arabic to English.
WIRE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Business Wire News</title>
<item>
  <title>Rocket Lab Awarded USD 515 Million Space Force Contract</title>
  <pubDate>Thu, 20 Aug 2026 20:50:00 UT</pubDate>
  <enclosure url="https://mms.businesswire.com/media/20260820007198/en/neutron.jpg"
             type="image/jpeg" length="2048"/>
  <description>LONG BEACH, Calif.--(BUSINESS WIRE)--&lt;p&gt;Rocket Lab (NASDAQ: RKLB)
    today announced it has been awarded a contract.&lt;/p&gt;</description>
  <link>http://www.businesswire.com/news/home/20260820007198/en/Rocket-Lab-Awarded</link>
</item>
<item>
  <title>Rocket Lab remporte un contrat de 515 millions de dollars</title>
  <pubDate>Thu, 20 Aug 2026 20:50:00 UT</pubDate>
  <description>LONG BEACH--(BUSINESS WIRE)--Rocket Lab (NASDAQ: RKLB) a annonce.</description>
  <link>http://www.businesswire.com/news/home/20260820007198/fr/?feedref=JjAwJuNHiyst</link>
</item>
<item>
  <title>Vitalist Reports First Quarter 2027 Financial Results</title>
  <pubDate>Thu, 20 Aug 2026 21:05 GMT</pubDate>
  <description><![CDATA[Vitalist Reports First Quarter 2027 Financial Results]]></description>
  <link>https://www.globenewswire.com/news-release/2026/08/20/3348846/0/en/vitalist-q1.html</link>
</item>
<item>
  <title></title>
  <link>https://www.prnewswire.com/news-releases/untitled-302345.html</link>
</item>
</channel></rss>"""


class TestWireParse:
    def test_translations_are_dropped(self):
        """Half of Business Wire's home feed is the same releases in other
        languages. They name the ticker so relevance keeps them, and dedup
        compares token overlap, which cannot match French to English."""
        titles = [a["title"] for a in wires.parse(WIRE_RSS, "Business Wire")]
        assert "Rocket Lab Awarded USD 515 Million Space Force Contract" in titles
        assert not any("remporte" in t for t in titles)

    def test_a_feed_with_no_language_segment_is_not_treated_as_a_translation(self):
        """PR Newswire URLs carry no language at all, which is not the same as
        carrying a foreign one."""
        assert wires._is_english(
            "https://www.prnewswire.com/news-releases/some-slug-302345.html")

    def test_untitled_items_are_skipped(self):
        assert len(wires.parse(WIRE_RSS, "PR Newswire")) == 2

    def test_the_release_image_is_taken_from_the_enclosure(self):
        """Already in hand, and it saves the Open Graph fetch images.py would
        otherwise make for this article."""
        article = wires.parse(WIRE_RSS, "Business Wire")[0]
        assert article["image"] == \
            "https://mms.businesswire.com/media/20260820007198/en/neutron.jpg"

    def test_html_is_stripped_from_the_release_body(self):
        article = wires.parse(WIRE_RSS, "Business Wire")[0]
        assert "<p>" not in article["description"]
        assert "NASDAQ: RKLB" in article["description"]

    def test_domain_and_date_survive(self):
        article = wires.parse(WIRE_RSS, "Business Wire")[0]
        assert article["source_domain"] == "businesswire.com"
        assert article["published_at"].date().isoformat() == "2026-08-20"
        assert article["source_type"] == "WIRES"

    def test_the_release_outranks_the_write_up_of_it(self):
        """A rewrite should never win a dedup tie-break against the company's
        own words."""
        from normalize import source_rank
        assert source_rank("businesswire.com") < source_rank("fool.com")
        assert source_rank("globenewswire.com") < source_rank("benzinga.com")

    def test_an_unnamed_wire_item_is_never_sector_context(self):
        """Finviz's per-ticker table earns `related`; a firehose does not. An
        unnamed wire item is simply another company's announcement."""
        from services.dedup import _RELATED_ALLOWED
        assert wires.SOURCE_TYPE not in _RELATED_ALLOWED


class TestWireFanOut:
    """The feeds are not per-ticker: one pull covers the whole watchlist."""

    @staticmethod
    def _stub(monkeypatch, calls):
        from types import SimpleNamespace
        wires.reset_cache()
        monkeypatch.setattr(wires, "_cache", None, raising=False)

        def fake_get(url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status=200, ok=True, text=WIRE_RSS)

        monkeypatch.setattr(wires.scraper, "get", fake_get)

    def test_one_pull_serves_every_stock(self, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        for short_name in ("RKLB", "AAPL", "TSLA", "SHEL", "VITA"):
            wires.for_stock(short_name, short_name)

        assert len(calls) == len(wires._FEEDS)
        wires.reset_cache()

    def test_an_item_naming_the_company_is_kept_and_the_rest_dropped(self, monkeypatch):
        calls = []
        self._stub(monkeypatch, calls)

        kept = wires.for_stock("6RJ0", "Rocket Lab Corp", aliases=("RKLB",))
        assert [a["title"] for a in kept] == [
            "Rocket Lab Awarded USD 515 Million Space Force Contract"] * len(wires._FEEDS)

        assert wires.for_stock("AAPL", "Apple Inc.") == []
        wires.reset_cache()

    def test_a_failing_feed_does_not_take_the_others_down(self, monkeypatch):
        from types import SimpleNamespace
        wires.reset_cache()
        seen = []

        def fake_get(url, **kwargs):
            seen.append(url)
            if "globenewswire" in url:
                return None
            return SimpleNamespace(status=200, ok=True, text=WIRE_RSS)

        monkeypatch.setattr(wires.scraper, "get", fake_get)
        assert len(wires.fetch()) == 2 * (len(wires._FEEDS) - 1)
        assert len(seen) == len(wires._FEEDS)
        wires.reset_cache()

    def test_wires_ride_the_fast_lane(self):
        """They carry the announcement itself, so waiting an hour for it
        defeats the point."""
        from services import news as news_pkg
        assert wires.SOURCE_TYPE in news_pkg.FAST_SOURCES


ARTICLE_HTML = """<html><head><title>Rocket Lab wins contract</title></head><body>
<nav><a href="/">Home</a><a href="/markets">Markets</a></nav>
<article><h1>Rocket Lab wins USD 515 million Space Force contract</h1>
<p>Rocket Lab said on Thursday it had been awarded a contract worth up to 515
million dollars by the United States Space Force, the largest single award in
the company history and one that management said would underpin revenue growth
into 2028.</p>
<p>The company will supply Neutron launch services under the agreement, with the
first mission scheduled for the second half of next year. Shares rose 9 percent
in after-hours trading.</p>
</article>
<aside><h2>Related</h2><ul><li>Cookie policy</li><li>Sign up for our newsletter</li></ul></aside>
</body></html>"""


class TestBodyExtraction:
    def test_the_article_survives_and_the_furniture_does_not(self):
        body = extract.extract_body(ARTICLE_HTML, "https://example.com/a")
        assert "Space Force" in body and "515" in body
        assert "Cookie policy" not in body
        assert "Markets" not in body

    def test_a_consent_wall_is_not_an_article(self):
        """trafilatura falls back to the raw text of whatever it is given, so a
        stub page comes back looking like a successful extraction. Scored as
        sentiment, that is worse than having no body at all."""
        assert extract.extract_body(
            "<html><body><p>403 Forbidden</p></body></html>", "https://x.com/a") is None
        assert extract.extract_body(
            "<html><body><p>Please enable JavaScript.</p></body></html>",
            "https://x.com/a") is None

    def test_a_body_is_capped(self):
        long_page = ("<html><body><article>"
                     + "<p>Rocket Lab reported record revenue this quarter. </p>" * 400
                     + "</article></body></html>")
        assert len(extract.extract_body(long_page, "https://x.com/a")) == extract._MAX_CHARS

    def test_malformed_input_does_not_raise(self):
        assert extract.extract_body("", "https://x.com/a") is None


class TestBodyBackfill:
    _n = 0

    @classmethod
    def _stored(cls, **overrides):
        """One article already in the table, as it would be after `prepare`."""
        import db
        cls._n += 1
        row = {"short_name": "AAPL", "title": "Rocket Lab wins a launch contract",
               "title_key": f"k{cls._n}", "url": f"https://example.com/s{cls._n}",
               "url_hash": f"h{cls._n}", "source": "Reuters",
               "source_domain": "reuters.com", "source_type": "GOOGLE_NEWS",
               "publish_time": "2026-08-20T10:00:00Z"}
        row.update(overrides)
        row["url_hash"] = row["url"]
        return db.news.insert_news_many([row])

    def test_a_failed_extraction_leaves_the_row_intact(self, stocked_db, monkeypatch):
        """Bodies are best-effort, exactly like images: the article is already
        stored by the time this runs."""
        import db
        from types import SimpleNamespace

        ids = self._stored(url="https://example.com/story-1")
        monkeypatch.setattr(extract.scraper, "get_many", lambda reqs: [
            SimpleNamespace(status=200, ok=True, url=r["url"],
                            headers={"content-type": "text/html"},
                            text="<html><body><p>nope</p></body></html>")
            for r in reqs])

        assert extract.fill_bodies(ids) == 0
        row = db.news.get_news_by_id(ids[0])
        assert row["body"] is None
        assert row["title"] and row["url"]

    def test_a_google_redirect_is_skipped_without_a_fetch(self, stocked_db, monkeypatch):
        """There is no article at a CBMi... address, only a consent page."""
        ids = self._stored(
            url="https://news.google.com/rss/articles/CBMilAFBVV95cUxOX2JCTHkt?oc=5",
            source_domain="news.google.com")

        calls = []
        monkeypatch.setattr(extract.scraper, "get_many",
                            lambda reqs: calls.append(reqs) or [])

        assert extract.fill_bodies(ids) == 0
        assert calls == []

    def test_a_filing_is_skipped_without_a_fetch(self, stocked_db, monkeypatch):
        """A 10-K is megabytes of XBRL and its useful content is the form
        type, which sec_edgar already put in the title."""
        ids = self._stored(
            url="https://www.sec.gov/Archives/edgar/data/320193/0001/aapl.htm",
            source_domain="sec.gov")

        calls = []
        monkeypatch.setattr(extract.scraper, "get_many",
                            lambda reqs: calls.append(reqs) or [])

        assert extract.fill_bodies(ids) == 0
        assert calls == []

    def test_a_benzinga_page_is_skipped_without_a_fetch(self, stocked_db, monkeypatch):
        """Its article pages answer a Cloudflare 403, which the scraper retries
        as throttling — a body slot spent on nothing."""
        ids = self._stored(url="https://www.benzinga.com/news/26/11/1/a-story",
                           source_domain="benzinga.com")

        calls = []
        monkeypatch.setattr(extract.scraper, "get_many",
                            lambda reqs: calls.append(reqs) or [])

        assert extract.fill_bodies(ids) == 0
        assert calls == []

    def test_a_good_page_is_stored_and_read_by_the_scorer(self, stocked_db, monkeypatch):
        import db
        from types import SimpleNamespace
        from services import sentiment_service

        ids = self._stored(url="https://example.com/story-2",
                           title="Rocket Lab update", description=None)
        monkeypatch.setattr(extract.scraper, "get_many", lambda reqs: [
            SimpleNamespace(status=200, ok=True, url=r["url"],
                            headers={"content-type": "text/html"}, text=ARTICLE_HTML)
            for r in reqs])

        assert extract.fill_bodies(ids) == 1
        row = db.news.get_news_by_id(ids[0])
        assert "Space Force" in row["body"]

        # The whole point: the scorer now reads the article, not the headline.
        sentiment_service.score_rows([row])
        assert db.news.get_news_by_id(ids[0])["sentiment"] == \
            sentiment_service.score_article(row["title"], row["body"])

    def test_the_cap_bounds_one_refresh(self, stocked_db, monkeypatch):
        from types import SimpleNamespace
        ids = []
        for n in range(6):
            ids += self._stored(url=f"https://example.com/story-cap-{n}")

        seen = []
        monkeypatch.setattr(extract.scraper, "get_many",
                            lambda reqs: seen.append(len(reqs)) or [None] * len(reqs))

        extract.fill_bodies(ids, limit=2)
        assert seen == [2]

    def test_the_flag_turns_it_off(self, stocked_db, monkeypatch):
        ids = self._stored(url="https://example.com/story-3")
        monkeypatch.setattr(extract.settings, "EXTRACT_ARTICLE_BODIES", False)
        monkeypatch.setattr(extract.scraper, "get_many",
                            lambda reqs: pytest.fail("should not fetch"))
        assert extract.fill_bodies(ids) == 0


class TestBodyReachesTheEventPrompt:
    def test_the_body_is_preferred_over_the_feed_teaser_and_truncated(self):
        from services import events
        article = {
            "publish_time": "2026-08-20T10:00:00+00:00",
            "source": "Reuters",
            "title": "Rocket Lab wins contract",
            "description": "teaser copy",
            "body": "REAL BODY. " + "x" * 900,
        }
        prompt = events._build_prompt("RKLB", [article], [])
        assert "REAL BODY." in prompt
        assert "teaser copy" not in prompt
        assert "x" * 900 not in prompt

    def test_the_teaser_is_still_used_when_there_is_no_body(self):
        from services import events
        article = {
            "publish_time": "2026-08-20T10:00:00+00:00",
            "source": "Reuters",
            "title": "Rocket Lab wins contract",
            "description": "teaser copy",
            "body": None,
        }
        assert "teaser copy" in events._build_prompt("RKLB", [article], [])

    def test_the_batch_shrank_when_bodies_joined_the_prompt(self):
        """A body costs roughly twice a description, and a 60-article call
        already took about a minute."""
        from services import events
        assert events._BATCH_SIZE <= 40


# ── RNS: UK regulatory announcements (3.6) ───────────────────────────────

# Synthetic, not captured. Investegate's terms cover their content, and this
# repo is public — so the fixture reproduces the *shape* of the listing (the
# permalink structure and the relative timestamp) without carrying anybody's
# announcements. The live check that the real page still has this shape is
# `test_live_sources.py`, which is opt-in.
RNS_INDEX = """
<html><body>
<ul class="announcements">
  <li>
    <span>1 hour ago</span>
    <a href="https://www.investegate.co.uk/announcement/rns/example-mining-plc--exm/half-year-results/9000001">Half Year Results</a>
  </li>
  <li>
    <span>23 minutes ago</span>
    <a href="https://www.investegate.co.uk/announcement/rns/example-mining-plc--exm/director-shareholding/9000002">Director/PDMR Shareholding</a>
  </li>
  <li>
    <span>2 days ago</span>
    <a href="https://www.investegate.co.uk/announcement/rns/northern-widget-group--nwg/trading-statement/9000003">Trading Statement</a>
  </li>
  <li>
    <span>3 hours ago</span>
    <a href="https://www.investegate.co.uk/announcement/rns/unlisted-holdings-ltd/scheme-of-arrangement/9000004">Scheme of Arrangement</a>
  </li>
  <!-- The same announcement linked twice, as listings do. -->
  <li>
    <span>1 hour ago</span>
    <a href="https://www.investegate.co.uk/announcement/rns/example-mining-plc--exm/half-year-results/9000001">Half Year Results</a>
  </li>
  <li><a href="https://www.investegate.co.uk/about">About us</a></li>
</ul>
</body></html>
"""


class TestRnsParsing:
    """Parses the links, not the layout. The announcement permalink is
    self-describing — company slug, ticker, headline slug, id — so a listing
    redesign moves the rows around without changing what they point at."""

    def test_it_reads_company_and_ticker_out_of_the_permalink(self):
        articles = rns.parse(RNS_INDEX)
        first = articles[0]

        assert first["title"] == "Example Mining Plc (EXM): Half Year Results"
        assert first["source_type"] == "RNS"
        assert first["source_domain"] == "investegate.co.uk"

    def test_the_company_is_named_so_the_relevance_filter_keeps_it(self):
        """A filing's own headline is "Half Year Results" — no company, no
        ticker. Stored as-is every relevance check would drop it and every
        dedup key would collide with every other issuer's results. Same problem
        EDGAR has, same fix."""
        from services import dedup

        article = rns.parse(RNS_INDEX)[0]
        assert dedup.is_relevant(article["title"], "EXM", "Example Mining plc")

    def test_an_issuer_without_a_ticker_still_parses(self):
        titles = [a["title"] for a in rns.parse(RNS_INDEX)]
        assert "Unlisted Holdings Ltd: Scheme of Arrangement" in titles

    def test_the_same_announcement_linked_twice_is_stored_once(self):
        ids = [a["url"].rsplit("/", 1)[-1] for a in rns.parse(RNS_INDEX)]
        assert len(ids) == len(set(ids))

    def test_navigation_links_are_not_announcements(self):
        assert not any("About us" in a["title"] for a in rns.parse(RNS_INDEX))

    def test_relative_ages_are_resolved_against_now(self):
        """The listing states age, not time."""
        from normalize import now_utc

        by_title = {a["title"]: a for a in rns.parse(RNS_INDEX)}
        now = now_utc()

        an_hour = by_title["Example Mining Plc (EXM): Half Year Results"]
        two_days = by_title["Northern Widget Group (NWG): Trading Statement"]

        assert 0.9 < (now - an_hour["published_at"]).total_seconds() / 3600 < 1.1
        assert 1.9 < (now - two_days["published_at"]).days + 0.05 < 2.1

    def test_a_missing_age_becomes_now_rather_than_the_epoch(self):
        """None would sort the row to 1970 and bury a live announcement."""
        from normalize import now_utc

        html = ('<li><a href="https://www.investegate.co.uk/announcement/rns/'
                'example-mining-plc--exm/half-year-results/9000001">Results</a></li>')
        article = rns.parse(html)[0]

        assert (now_utc() - article["published_at"]).total_seconds() < 60

    def test_an_empty_page_is_empty_not_an_exception(self):
        assert rns.parse("<html><body>nothing here</body></html>") == []
        assert rns.parse("") == []


class TestRnsGating:
    """RNS is EDGAR's opposite number, so it is gated on the same test,
    inverted. A New York listing files with the SEC, not the LSE."""

    @staticmethod
    def _quiet(monkeypatch):
        from services import news as news_pkg

        for module, names in (
            (news_pkg.google_news, ("fetch_recent", "fetch_backfill")),
            (news_pkg.yahoo_news, ("fetch_search", "fetch_rss")),
            (news_pkg.bing_news, ("fetch",)),
            (news_pkg.finviz, ("fetch",)),
            (news_pkg.nasdaq_news, ("fetch",)),
            (news_pkg.sec_edgar, ("fetch", "full_text")),
            (news_pkg.wires, ("for_stock",)),
            (news_pkg.rns, ("for_stock",)),
            (news_pkg.sitemaps, ("for_stock",)),
        ):
            for name in names:
                monkeypatch.setattr(module, name, lambda *a, **kw: [])

    def test_a_us_listing_records_rns_as_skipped(self, temp_db, monkeypatch):
        """Skipped, not failed. A dead source and a source that was never
        asked look identical in the yield table otherwise."""
        import db
        from services import news as news_pkg

        self._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)

        news_pkg.collect("AAPL", "Apple Inc.", "AAPL", "NMS")

        row = {r["source_type"]: r for r in db.sources.yields()}["RNS"]
        assert row["skips"] == 1 and row["fetches"] == 0

    def test_a_london_listing_asks_rns(self, temp_db, monkeypatch):
        import db
        from services import news as news_pkg

        self._quiet(monkeypatch)

        news_pkg.collect("SHEL", "Shell plc", "SHEL.L", "LSE")

        row = {r["source_type"]: r for r in db.sources.yields()}["RNS"]
        assert row["fetches"] == 1 and row["skips"] == 0

    def test_a_us_listing_never_reaches_the_network(self, temp_db, monkeypatch):
        """The gate has to be above the fetch, not inside the parser."""
        from services import news as news_pkg

        called = []
        self._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.settings, "sec_enabled", lambda: True)
        monkeypatch.setattr(news_pkg.rns, "for_stock",
                            lambda *a, **kw: called.append(1) or [])

        news_pkg.collect("AAPL", "Apple Inc.", "AAPL", "NMS")

        assert called == []


class TestRnsFirehose:
    """One index page carries every issuer, so it is fetched once per cycle and
    shared — the same arrangement the wires use, for the same reason."""

    @staticmethod
    def _serving(html, calls):
        def get(*args, **kwargs):
            calls.append(1)
            return SimpleNamespace(ok=True, text=html)
        return get

    def test_the_index_is_fetched_once_and_shared(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rns, "_cache", None)
        monkeypatch.setattr(rns.scraper, "get", self._serving(RNS_INDEX, calls))

        rns.for_stock("EXM", "Example Mining plc")
        rns.for_stock("NWG", "Northern Widget Group")

        assert len(calls) == 1

    def test_the_firehose_is_narrowed_to_the_stock(self, monkeypatch):
        """Not left to dedup: a few hundred announcements counted as "found for
        this stock" would make the per-source yield meaningless."""
        monkeypatch.setattr(rns, "_cache", None)
        monkeypatch.setattr(rns.scraper, "get", self._serving(RNS_INDEX, []))

        mine = rns.for_stock("EXM", "Example Mining plc")

        assert mine and all("Example Mining" in a["title"] for a in mine)

    def test_an_empty_cycle_is_cached_too(self, monkeypatch):
        """An index that just failed will not answer better for stock twenty."""
        calls = []
        monkeypatch.setattr(rns, "_cache", None)

        def failing(*args, **kwargs):
            calls.append(1)
            return None

        monkeypatch.setattr(rns.scraper, "get", failing)

        assert rns.for_stock("EXM", "Example Mining plc") == []
        assert rns.for_stock("NWG", "Northern Widget Group") == []
        assert len(calls) == 1


# ── Publisher news sitemaps ──────────────────────────────────────────────
#
# Synthetic, like the RNS fixture: these are publishers' headlines, and their
# terms were never read. The shapes are the real ones — CNBC's `n:` prefix,
# everyone else's `news:` with an `image:` block after the article.

SITEMAP_N = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:n="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.example-news.com/2026/11/23/chip-stocks-slide.html</loc><n:news>
  <n:publication><n:name>Example News</n:name><n:language>en</n:language></n:publication>
  <n:stock_tickers>NVDA,AMD</n:stock_tickers>
  <n:title>Chipmaker shares slide as export curbs bite</n:title>
  <n:publication_date>2026-11-23T10:15:00-04:00</n:publication_date>
</n:news></url>
<url><loc>https://www.example-news.com/2026/11/23/chipaktien-fallen.html</loc><n:news>
  <n:publication><n:name>Example News</n:name><n:language>de</n:language></n:publication>
  <n:title>Chipaktien fallen nach neuen Exportregeln</n:title>
  <n:publication_date>2026-11-23T10:20:00-04:00</n:publication_date>
</n:news></url>
</urlset>"""

SITEMAP_NEWS = """<?xml version='1.0'?>
<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'
        xmlns:news='http://www.google.com/schemas/sitemap-news/0.9'
        xmlns:image='http://www.google.com/schemas/sitemap-image/1.1'>
  <url>
    <loc>https://www.example-markets.com/story/nvidia-earnings-lift-chips</loc>
    <news:news>
      <news:publication><news:name>Example Markets</news:name><news:language>en</news:language></news:publication>
      <news:publication_date>2026-11-23T14:18:56.615Z</news:publication_date>
      <news:title>Nvidia's earnings beat lifts the whole chip sector</news:title>
      <news:stock_tickers>NVDA, US:NVDA</news:stock_tickers>
    </news:news>
    <image:image>
      <image:loc>https://images.example-markets.com/im-1?width=1280</image:loc>
      <image:title>Jensen Huang on stage</image:title>
    </image:image>
  </url>
  <url>
    <loc>https://www.example-markets.com/data-news/apple-stock-rises-monday</loc>
    <news:news>
      <news:publication><news:name>Example Markets</news:name><news:language>en</news:language></news:publication>
      <news:publication_date>2026-11-23T15:00:00Z</news:publication_date>
      <news:title>Apple Inc. stock rises Monday, outperforms market</news:title>
      <news:stock_tickers>AAPL</news:stock_tickers>
    </news:news>
  </url>
  <url>
    <loc>https://www.example-markets.com/story/untitled</loc>
    <news:news><news:title></news:title></news:news>
  </url>
</urlset>"""


def sitemap(*entries, when=None) -> str:
    """A `news:` sitemap of (path, title, tickers) entries, published now.

    Built per test rather than fixed: `fetch` drops anything over three days
    old, so a dated fixture would start failing the week after it was written.
    """
    from normalize import now_utc
    stamp = (when or now_utc()).isoformat()
    urls = "".join(
        f"<url><loc>https://www.example-markets.com{path}</loc><news:news>"
        f"<news:publication><news:name>Example Markets</news:name>"
        f"<news:language>en</news:language></news:publication>"
        f"<news:publication_date>{stamp}</news:publication_date>"
        f"<news:title>{title}</news:title>"
        f"<news:stock_tickers>{tickers}</news:stock_tickers></news:news></url>"
        for path, title, tickers in entries)
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            f"{urls}</urlset>")


def serve_sitemap(monkeypatch, body, calls=None):
    """Every feed answers with `body`, from an empty cache."""
    monkeypatch.setattr(sitemaps, "_cache", None)

    def get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        return None if body is None else SimpleNamespace(ok=True, text=body)

    monkeypatch.setattr(sitemaps.scraper, "get", get)


class TestSitemapParsing:
    def test_either_namespace_prefix_parses(self):
        """The prefix is each publisher's choice, so tags are matched on their
        local name. The German entry goes: dedup cannot match it to English."""
        cnbc = sitemaps.parse(SITEMAP_N, "CNBC")
        assert [a["title"] for a in cnbc] == ["Chipmaker shares slide as export curbs bite"]
        assert cnbc[0]["tickers"] == {"NVDA", "AMD"}
        assert cnbc[0]["source_domain"] == "example-news.com"
        assert cnbc[0]["source_type"] == "SITEMAPS"

        others = sitemaps.parse(SITEMAP_NEWS, "Example Markets")
        assert [a["title"] for a in others] == [
            "Nvidia's earnings beat lifts the whole chip sector",
            "Apple Inc. stock rises Monday, outperforms market",
        ]

    def test_the_article_title_is_not_the_image_title(self):
        """The image block carries its own `loc` and `title`. Walking the whole
        entry with iter() would let them overwrite the article's."""
        article = sitemaps.parse(SITEMAP_NEWS, "Example Markets")[0]
        assert article["title"] == "Nvidia's earnings beat lifts the whole chip sector"
        assert article["url"] == "https://www.example-markets.com/story/nvidia-earnings-lift-chips"
        assert article["image"] == "https://images.example-markets.com/im-1?width=1280"
        assert article["description"] is None

    def test_publication_time_is_exact(self):
        from datetime import UTC, datetime

        offset = sitemaps.parse(SITEMAP_N, "CNBC")[0]
        millis, zulu = sitemaps.parse(SITEMAP_NEWS, "Example Markets")

        assert offset["published_at"] == datetime(2026, 11, 23, 14, 15, tzinfo=UTC)
        assert millis["published_at"] == datetime(2026, 11, 23, 14, 18, 56, 615000, tzinfo=UTC)
        assert zulu["published_at"] == datetime(2026, 11, 23, 15, 0, tzinfo=UTC)

    def test_ticker_tags_are_normalised(self):
        """US prefixes kept, indices and futures dropped, a London prefix
        dropped, and `BRK.B` written the way Yahoo and the watchlist write it."""
        assert sitemaps._tickers("NASDAQ:AAPL, US:MSFT, BRK.B, .SPX, @GC26Z, XLON:BA") \
            == {"AAPL", "MSFT", "BRK-B"}

    def test_a_long_ticker_list_is_not_a_match(self):
        """Google reads five. Twelve is a market wrap, not twelve stories."""
        twelve = ",".join(["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
                           "TSLA", "AVGO", "JPM", "V", "MA", "COST"])
        assert sitemaps._tickers(twelve) == set()

    def test_automated_paths_are_dropped(self):
        """`/data-news/` is MarketWatch's bot copy. The skip belongs to the
        feed that lists it, not to every publisher."""
        skip = {name: prefixes for name, _, prefixes in sitemaps._FEEDS}

        marketwatch = sitemaps.parse(SITEMAP_NEWS, "MarketWatch", skip["MarketWatch"])
        barrons = sitemaps.parse(SITEMAP_NEWS, "Barron's", skip["Barron's"])

        assert not any("/data-news/" in a["url"] for a in marketwatch)
        assert any("/data-news/" in a["url"] for a in barrons)

    def test_an_index_or_a_bot_check_page_yields_nothing(self):
        """Indexes are not followed. A bot check may be well-formed enough to
        parse, so the root has to be checked as well as the parse."""
        index = ('<?xml version="1.0"?><sitemapindex '
                 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap>'
                 '<loc>https://www.example-markets.com/news-1.xml</loc>'
                 '</sitemap></sitemapindex>')
        assert sitemaps.parse(index, "X") == []
        assert sitemaps.parse("<html><head><title>Just a moment...</title></head>"
                              "<body>Checking your browser</body></html>", "X") == []
        assert sitemaps.parse("<html><body><p>Access denied<br></body></html>", "X") == []
        assert sitemaps.parse("", "X") == []


class TestSitemapMatching:
    """Tags supplement the title test. They are exact where `is_relevant`'s
    ticker path is not, and they are US symbols only."""

    def test_a_ticker_tag_matches_without_the_name_in_the_title(self, monkeypatch):
        serve_sitemap(monkeypatch, sitemap(
            ("/story/a", "Chipmaker shares slide as export curbs bite", "NASDAQ:NVDA")))

        kept = sitemaps.for_stock("NVIDIA Corporation", "NVDA")

        assert kept and {a["title"] for a in kept} == {
            "Chipmaker shares slide as export curbs bite"}
        assert sitemaps.for_stock("Advanced Micro Devices", "AMD") == []

    def test_a_one_letter_ticker_matches_the_tag_not_the_apostrophe(self, monkeypatch):
        """The trap this avoids: `is_relevant` with the ticker finds `S` in
        "Nvidia's", so a SentinelOne holder would get Nvidia's earnings."""
        from services.dedup import is_relevant
        nvidia = "Nvidia's earnings beat lifts the whole chip sector"
        assert is_relevant(nvidia, "S", "SentinelOne Inc")

        serve_sitemap(monkeypatch, sitemap(
            ("/story/a", nvidia, "NVDA"),
            ("/story/b", "Cybersecurity names rally on budget hopes", "NYSE:S")))

        titles = {a["title"] for a in sitemaps.for_stock("SentinelOne Inc", "S")}
        assert titles == {"Cybersecurity names rally on budget hopes"}

    def test_a_non_us_listing_matches_on_name_only(self, monkeypatch):
        """`BA` in a tag is Boeing. BAE Systems is `BA.L`, and stripping the
        suffix would hand it every Boeing story."""
        serve_sitemap(monkeypatch, sitemap(
            ("/story/a", "Boeing wins a tanker order from the Air Force", "BA"),
            ("/story/b", "BAE Systems wins a frigate contract", "")))

        titles = {a["title"] for a in sitemaps.for_stock("BAE Systems plc", None)}
        assert titles == {"BAE Systems wins a frigate contract"}

    def test_one_pull_serves_every_stock(self, monkeypatch):
        calls = []
        serve_sitemap(monkeypatch, sitemap(
            ("/story/a", "Chipmaker shares slide as export curbs bite", "NVDA")), calls)

        for company, ticker in (("NVIDIA Corporation", "NVDA"), ("Apple Inc.", "AAPL"),
                                ("Shell plc", None), ("Rocket Lab Corp", "RKLB")):
            sitemaps.for_stock(company, ticker)

        assert len(calls) == len(sitemaps._FEEDS)

    def test_an_empty_cycle_is_cached_too(self, monkeypatch, capsys):
        """Publishers that just failed will not answer better for stock twenty.
        Each is named, because the yield table only sees `SITEMAPS`."""
        calls = []
        serve_sitemap(monkeypatch, None, calls)

        assert sitemaps.for_stock("NVIDIA Corporation", "NVDA") == []
        assert sitemaps.for_stock("Apple Inc.", "AAPL") == []

        assert len(calls) == len(sitemaps._FEEDS)
        out = capsys.readouterr().out
        assert all(f"{name} yielded nothing" in out for name, _, _ in sitemaps._FEEDS)

    def test_an_entry_past_three_days_is_dropped(self, monkeypatch):
        """A news sitemap promises two days; MarketWatch's ran to a month."""
        from datetime import timedelta

        from normalize import now_utc
        serve_sitemap(monkeypatch, sitemap(
            ("/story/old", "Nvidia shares hit a record high", "NVDA"),
            when=now_utc() - timedelta(days=4)))

        assert sitemaps.for_stock("NVIDIA Corporation", "NVDA") == []


class TestSitemapsThroughPrepare:
    """A tag-only match never names the stock, so `prepare` would drop it as
    off-topic. The publisher's tag is the curation that earns `related`."""

    def test_a_tag_only_match_survives_prepare_as_related(self, stocked_db, monkeypatch):
        import db
        from services import dedup

        serve_sitemap(monkeypatch, sitemap(
            ("/story/a", "iPhone suppliers slide in Asian trading", "NASDAQ:AAPL")))
        raw = sitemaps.for_stock("Apple", "AAPL")

        prepared = dedup.prepare(raw, "AAPL", "Apple")

        assert [r["relevance"] for r in prepared.rows] == ["related"]
        # The `tickers` set on the raw article never reaches a stored row.
        assert len(db.news.insert_news_many(prepared.rows)) == 1

    def test_another_companys_headline_is_still_dropped(self, monkeypatch):
        """The tag gets it past relevance, not past the other-company test."""
        from services import dedup

        serve_sitemap(monkeypatch, sitemap(
            ("/story/b", "AMD Stock Jumps After a Data Center Win", "NVDA, AMD")))
        raw = sitemaps.for_stock("NVIDIA Corporation", "NVDA")
        assert raw

        prepared = dedup.prepare(raw, "NVDA", "NVIDIA Corporation")

        assert prepared.rows == []
        assert prepared.dropped.get("other_stock") == len(raw)


class TestSitemapsLane:
    def test_a_london_listing_asks_the_sitemaps_too(self, temp_db, monkeypatch):
        """Not gated: only the tag half of the match is US-only, so a London
        line is asked with no ticker and matched on its name."""
        import db
        from services import news as news_pkg

        asked = []
        TestRnsGating._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.sitemaps, "for_stock",
                            lambda *a, **kw: asked.append(a) or [])

        news_pkg.collect("BAE", "BAE Systems plc", "BA.L", "LSE")

        row = {r["source_type"]: r for r in db.sources.yields()}["SITEMAPS"]
        assert row["fetches"] == 1 and row["skips"] == 0
        assert asked == [("BAE Systems plc", None)]

    def test_a_us_listing_is_asked_with_its_resolved_symbol(self, temp_db, monkeypatch):
        """Trading212 calls Rocket Lab `6RJ0`; the publishers tag it `RKLB`."""
        from services import news as news_pkg

        asked = []
        TestRnsGating._quiet(monkeypatch)
        monkeypatch.setattr(news_pkg.sitemaps, "for_stock",
                            lambda *a, **kw: asked.append(a) or [])

        news_pkg.collect("6RJ0", "Rocket Lab Corp", "RKLB", "NMS")

        assert asked == [("Rocket Lab Corp", "RKLB")]

    def test_sitemaps_wait_for_the_hourly_lane(self):
        """The fast lane runs without event detection, and `events.detect`
        only sees the inserting refresh's ids — a row the fast lane stored
        would never be judged. A write-up is not a minutes-matter item anyway."""
        from services import news as news_pkg
        assert sitemaps.SOURCE_TYPE not in news_pkg.FAST_SOURCES
