"""Does each source still answer? Opt-in, live, and off by default.

Every other test in this suite runs against a captured payload, which is what
makes them fast and deterministic — and also means they pass forever after the
live page changes. Finviz is an HTML scrape of a commercial site and is the
most likely silent breakage in the repo; a renamed table id would keep the
fixture test green while production quietly returned nothing.

So: one test per source, against the real thing, for a liquid US ticker.

    pytest -m live tests/test_live_sources.py

**These are deliberately loose.** They assert "something plausible came back",
not counts or contents, because a live assertion that is precise is a flaky
assertion, and a flaky test is worse than no test — it gets ignored, and then
it is worse than nothing. A source returning *anything* parseable is the whole
signal here; the fixture tests own correctness.

A failure here means the adapter needs looking at, not that the test is wrong.
"""
from __future__ import annotations

import pytest

import settings
from services import calendar, prices, social, stooq, yahoo
from services.news import (bing_news, finviz, google_news, nasdaq_news,
                           sec_edgar, wires, yahoo_news)

# Liquid, US-listed, and covered by everything. A quiet ticker would make an
# empty result ambiguous, which is exactly what these tests exist to avoid.
TICKER = "AAPL"
COMPANY = "Apple Inc."


def _plausible(articles, source: str) -> None:
    assert articles, f"{source} returned nothing"
    first = articles[0]
    assert first.get("title"), f"{source} returned an item with no title"
    assert str(first.get("url", "")).startswith("http"), f"{source} url looks wrong"
    assert first.get("source_type"), f"{source} item has no source_type"


@pytest.mark.live
class TestNewsSources:
    def test_finviz(self):
        """The HTML scrape. Most likely thing in the repo to break silently."""
        _plausible(finviz.fetch(TICKER), "finviz")

    def test_nasdaq_rss(self):
        _plausible(nasdaq_news.fetch(TICKER), "nasdaq")

    def test_google_news(self):
        _plausible(google_news.fetch_recent(TICKER, COMPANY, days=3), "google")

    def test_bing_news(self):
        _plausible(bing_news.fetch(TICKER, COMPANY, days=3), "bing")

    def test_yahoo_search(self):
        _plausible(yahoo_news.fetch_search(TICKER), "yahoo search")

    def test_yahoo_rss(self):
        _plausible(yahoo_news.fetch_rss(TICKER), "yahoo rss")

    def test_wires(self):
        """The firehose, not a per-ticker feed — so this asks whether the
        wires answer at all, not whether they mention Apple today."""
        wires.reset_cache()
        try:
            _plausible(wires.fetch(), "wires")
        finally:
            wires.reset_cache()

    def test_sec_edgar(self):
        """Skipped rather than failed without a contact address: SEC refuses
        the request outright, so there is nothing to learn from making it."""
        if not settings.sec_enabled():
            pytest.skip("STOCKY_SEC_CONTACT not set")
        _plausible(sec_edgar.fetch(TICKER, COMPANY), "sec edgar")


@pytest.mark.live
class TestPriceSources:
    def test_yahoo_chart(self):
        parsed = yahoo.chart(TICKER, range_="5d")
        assert parsed and parsed.get("points"), "yahoo chart returned no bars"
        bar = parsed["points"][-1]
        assert bar.get("close") is not None

    def test_yahoo_quote(self):
        quote = prices.get_quote(TICKER, range_="5d")
        assert quote and quote.get("price"), "no source produced a quote"

    @pytest.mark.xfail(
        reason="stooq serves a JS browser-check interstitial with a 200 instead "
               "of CSV — the anti-bot wall 3.4 exists to get past",
        strict=False,
    )
    def test_stooq_history(self):
        bars = stooq.history(TICKER, range_="1mo")
        assert bars, "stooq returned no history"
        assert bars[-1].get("close") is not None


@pytest.mark.live
class TestSignalSources:
    def test_earnings_calendar(self):
        """A single day can genuinely be empty — weekends and holidays — so
        this asks the week, and only that the shape is right."""
        from datetime import timedelta

        from normalize import now_utc

        today = now_utc().date()
        found = {}
        for offset in range(7):
            found |= calendar.fetch_day((today + timedelta(days=offset)).isoformat())
        assert found, "nasdaq calendar returned no earnings in a whole week"
        assert all(when in (None, "pre", "post") for when in found.values())

    def test_apewisdom(self):
        table = social.fetch()
        assert table, "apewisdom returned nothing"
        row = next(iter(table.values()))
        assert row["mentions"] is not None


class TestTheGate:
    """Unmarked on purpose — this is the part that must run in every suite.

    Everything above is worthless if it quietly starts running offline: the
    normal suite would go from deterministic to whatever three commercial
    websites felt like returning that morning.
    """

    class _Config:
        def __init__(self, m=""):
            self._m = m

        def getoption(self, name):
            return self._m

    class _Item:
        def __init__(self, live=True):
            self.keywords = {"live": True} if live else {}
            self.marks = []

        def add_marker(self, mark):
            self.marks.append(mark)

    def test_a_live_test_is_skipped_when_live_was_not_asked_for(self):
        from tests import conftest

        item = self._Item()
        conftest.pytest_collection_modifyitems(self._Config(), [item])
        assert [m.name for m in item.marks] == ["skip"]

    def test_an_ordinary_test_is_untouched(self):
        from tests import conftest

        item = self._Item(live=False)
        conftest.pytest_collection_modifyitems(self._Config(), [item])
        assert item.marks == []

    def test_asking_for_live_by_name_runs_them(self):
        from tests import conftest

        item = self._Item()
        conftest.pytest_collection_modifyitems(self._Config("live"), [item])
        assert item.marks == []

    def test_every_source_test_in_this_module_is_marked(self):
        """A new class added here without the marker would run live in CI."""
        from tests import test_live_sources as module

        for name, obj in vars(module).items():
            if not name.startswith("Test") or name == "TestTheGate":
                continue
            marks = [m.name for m in getattr(obj, "pytestmark", [])]
            assert "live" in marks, f"{name} is not marked live"
