"""Price parsing, currency normalisation and the fallback chain.

The currency tests are the important ones: an LSE quote arrives in pence, and
storing 3323.5 where 33.235 belongs is a silent 100x error in every chart and
percentage on the page.
"""
from __future__ import annotations

import pytest

from services import prices, stooq, yahoo


def chart_payload(currency="USD", price=338.19, previous=340.08, closes=None, stamps=None,
                  events=None, **bars):
    """`bars` takes open/high/low/volume series.

    Omitting them is the common case and is itself worth exercising: Yahoo
    sends `close` alone often enough that the parser must not assume the other
    four series are present.
    """
    closes = closes if closes is not None else [330.0, 336.91, 340.08, 338.19]
    stamps = stamps if stamps is not None else [1785072600, 1785159000, 1785245400, 1785331800]
    quote = {"close": closes, **{k: v for k, v in bars.items() if v is not None}}
    node_extra = {"events": events} if events else {}
    return {
        "chart": {
            "result": [{
                **node_extra,
                "meta": {
                    "symbol": "TEST", "exchangeName": "NMS", "fullExchangeName": "NasdaqGS",
                    "instrumentType": "EQUITY", "currency": currency,
                    "regularMarketPrice": price, "previousClose": previous,
                    "chartPreviousClose": 320.0,
                },
                "timestamp": stamps,
                "indicators": {"quote": [quote]},
            }],
            "error": None,
        }
    }


class TestCurrencyNormalisation:
    def test_pence_becomes_pounds(self):
        assert yahoo.normalise_currency("GBp") == ("GBP", 100.0)

    def test_pounds_are_left_alone(self):
        assert yahoo.normalise_currency("GBP") == ("GBP", 1.0)

    @pytest.mark.parametrize("code,expected", [
        ("ILA", ("ILS", 100.0)),      # agorot
        ("ZAC", ("ZAR", 100.0)),      # cents
        ("USD", ("USD", 1.0)),
        ("EUR", ("EUR", 1.0)),
    ])
    def test_minor_units(self, code, expected):
        assert yahoo.normalise_currency(code) == expected

    def test_unknown_code_passes_through_rather_than_guessing(self):
        assert yahoo.normalise_currency("XYZ") == ("XYZ", 1.0)

    def test_missing_currency_is_safe(self):
        assert yahoo.normalise_currency(None) == ("", 1.0)


class TestParseChart:
    def test_quote_and_series_are_both_extracted(self):
        parsed = yahoo.parse_chart(chart_payload())
        assert parsed["price"] == 338.19
        assert parsed["exchange"] == "NMS"
        assert parsed["currency"] == "USD"
        assert len(parsed["points"]) == 4

    def test_change_is_computed_from_previous_close(self):
        parsed = yahoo.parse_chart(chart_payload(price=338.19, previous=340.08))
        assert parsed["change"] == pytest.approx(-1.89, abs=1e-6)
        assert parsed["change_percent"] == pytest.approx(-0.556, abs=1e-3)

    def test_an_lse_quote_is_converted_to_pounds_throughout(self):
        """Price, previous close and every history point must share one unit."""
        parsed = yahoo.parse_chart(
            chart_payload(currency="GBp", price=3323.5, previous=3234.5,
                          closes=[3200.0, 3234.5, 3323.5], stamps=[1, 2, 3])
        )
        assert parsed["currency"] == "GBP"
        assert parsed["price"] == pytest.approx(33.235)
        assert parsed["previous_close"] == pytest.approx(32.345)
        assert parsed["points"][-1]["close"] == pytest.approx(33.235)
        # Percentage change must be unaffected by the unit conversion.
        assert parsed["change_percent"] == pytest.approx(2.752, abs=1e-2)

    def test_market_holidays_with_null_closes_are_skipped(self):
        parsed = yahoo.parse_chart(
            chart_payload(closes=[330.0, None, 340.08, None], stamps=[1, 2, 3, 4])
        )
        assert [p["close"] for p in parsed["points"]] == [330.0, 340.08]

    def test_timestamps_become_canonical_iso(self):
        parsed = yahoo.parse_chart(chart_payload())
        assert parsed["points"][-1]["ts"] == "2026-07-29T13:30:00Z"

    def test_full_bars_are_extracted(self):
        parsed = yahoo.parse_chart(chart_payload(
            closes=[330.0, 338.19], stamps=[1, 2],
            open=[328.0, 331.0], high=[335.0, 341.0], low=[327.0, 330.5],
            volume=[41_000_000, 52_500_000],
        ))
        assert parsed["points"][-1] == {
            "ts": "1970-01-01T00:00:02Z", "close": 338.19,
            "open": 331.0, "high": 341.0, "low": 330.5, "volume": 52_500_000,
        }

    def test_pence_divides_the_prices_and_never_the_volume(self):
        """Volume is a share count. Scaling it would report a hundredth of the
        real turnover on every LSE line."""
        parsed = yahoo.parse_chart(chart_payload(
            currency="GBp", price=3323.5, previous=3234.5,
            closes=[3323.5], stamps=[1],
            open=[3300.0], high=[3350.0], low=[3290.0], volume=[8_000_000],
        ))
        bar = parsed["points"][0]
        assert (bar["open"], bar["high"], bar["low"]) == (33.0, 33.5, 32.9)
        assert bar["volume"] == 8_000_000

    def test_a_close_only_payload_leaves_the_rest_none(self):
        """Yahoo often sends `close` alone; the other series must not be
        assumed present."""
        bar = yahoo.parse_chart(chart_payload(closes=[330.0], stamps=[1]))["points"][0]
        assert bar["close"] == 330.0
        assert (bar["open"], bar["high"], bar["low"], bar["volume"]) == (None,) * 4

    def test_a_hole_in_one_series_does_not_cost_the_bar(self):
        """A null high is a hole, not a holiday — the close still stands, and
        the gap is stored NULL rather than defaulted to a trade at zero."""
        parsed = yahoo.parse_chart(chart_payload(
            closes=[330.0, 338.19], stamps=[1, 2],
            open=[328.0, 331.0], high=[335.0, None], volume=[41_000_000, None],
        ))
        assert len(parsed["points"]) == 2
        assert parsed["points"][-1]["high"] is None
        assert parsed["points"][-1]["volume"] is None
        assert parsed["points"][-1]["open"] == 331.0

    def test_a_short_series_does_not_raise(self):
        """The series are not guaranteed to be the same length as `close`."""
        parsed = yahoo.parse_chart(chart_payload(
            closes=[330.0, 336.91, 338.19], stamps=[1, 2, 3], volume=[41_000_000],
        ))
        assert [p["volume"] for p in parsed["points"]] == [41_000_000, None, None]

    def test_previous_close_falls_back_to_the_prior_bar(self):
        payload = chart_payload()
        del payload["chart"]["result"][0]["meta"]["previousClose"]
        parsed = yahoo.parse_chart(payload)
        assert parsed["previous_close"] == pytest.approx(340.08)

    @pytest.mark.parametrize("payload", [
        {}, {"chart": {}}, {"chart": {"result": []}}, {"chart": {"result": None}},
    ])
    def test_malformed_payloads_return_none_instead_of_raising(self, payload):
        assert yahoo.parse_chart(payload) is None

    def test_payload_with_no_price_at_all_returns_none(self):
        payload = chart_payload()
        payload["chart"]["result"][0]["meta"] = {"currency": "USD"}
        payload["chart"]["result"][0]["timestamp"] = []
        payload["chart"]["result"][0]["indicators"] = {"quote": [{"close": []}]}
        assert yahoo.parse_chart(payload) is None


SPLIT_4_FOR_1 = {"splits": {"1785072600": {
    "date": 1785072600, "numerator": 4, "denominator": 1, "splitRatio": "4:1",
}}}

DIVIDEND = {"dividends": {"1785072600": {"amount": 0.24, "date": 1785072600}}}


class TestCorporateActions:
    """The `events` block the chart call has always requested and never read."""

    def test_dividends_are_extracted(self):
        parsed = yahoo.parse_chart(chart_payload(events=DIVIDEND))
        assert parsed["dividends"] == [{"ex_date": "2026-07-26", "amount": 0.24}]

    def test_a_dividend_is_money_so_pence_are_converted(self):
        parsed = yahoo.parse_chart(chart_payload(currency="GBp", events=DIVIDEND))
        assert parsed["dividends"][0]["amount"] == pytest.approx(0.0024)

    def test_splits_are_extracted(self):
        parsed = yahoo.parse_chart(chart_payload(events=SPLIT_4_FOR_1))
        assert parsed["splits"] == [
            {"ex_date": "2026-07-26", "numerator": 4.0, "denominator": 1.0}
        ]

    def test_no_events_block_is_not_an_error(self):
        parsed = yahoo.parse_chart(chart_payload())
        assert parsed["dividends"] == [] and parsed["splits"] == []

    @pytest.mark.parametrize("events", [
        {"splits": {"1": {"date": 1, "numerator": 0, "denominator": 1}}},
        {"splits": {"1": {"date": 1, "numerator": 4}}},
        {"splits": {"1": "not a dict"}},
        {"dividends": {"1": {"date": 1}}},
    ])
    def test_malformed_events_are_skipped_not_raised(self, events):
        parsed = yahoo.parse_chart(chart_payload(events=events))
        assert parsed["dividends"] == [] and parsed["splits"] == []


class TestSplitAdjustment:
    """A split makes every price before it incomparable with every price after.
    Adjusting once is correct; adjusting twice is a 16x error on a 4:1."""

    @staticmethod
    def _seed(short_name="AAPL"):
        import db
        db.prices.upsert_points(short_name, [
            {"ts": "2026-07-24T00:00:00Z", "close": 400.0, "open": 396.0,
             "high": 404.0, "low": 395.0, "volume": 10_000_000},
            {"ts": "2026-07-25T00:00:00Z", "close": 440.0, "open": 430.0,
             "high": 444.0, "low": 428.0, "volume": 12_000_000},
        ])

    def _quote(self):
        return {"price": 110.0, "change": 0.0, "change_percent": 0.0,
                "currency": "USD", "points": [], "dividends": [],
                "splits": [{"ex_date": "2026-07-26", "numerator": 4.0,
                            "denominator": 1.0}]}

    def test_history_before_the_ex_date_is_brought_onto_the_new_scale(self, stocked_db):
        import db
        self._seed()
        prices._store("AAPL", self._quote())

        bars = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["close"] for b in bars] == [100.0, 110.0]
        assert [b["high"] for b in bars] == [101.0, 111.0]
        # Four times the shares at a quarter of the price.
        assert [b["volume"] for b in bars] == [40_000_000, 48_000_000]

    def test_applying_the_same_split_twice_changes_nothing(self, stocked_db):
        """The one way this could corrupt the series rather than merely miss."""
        import db
        self._seed()
        prices._store("AAPL", self._quote())
        after_once = db.prices.get_history("AAPL", "2026-07-01", interval="1d")

        for _ in range(3):                       # three more refreshes carry it
            prices._store("AAPL", self._quote())

        assert db.prices.get_history("AAPL", "2026-07-01", interval="1d") == after_once

    def test_bars_on_or_after_the_ex_date_are_left_alone(self, stocked_db):
        """They already arrived on the post-split scale."""
        import db
        db.prices.upsert_points("AAPL", [
            {"ts": "2026-07-25T00:00:00Z", "close": 440.0},
            {"ts": "2026-07-26T00:00:00Z", "close": 110.0},
            {"ts": "2026-07-27T00:00:00Z", "close": 112.0},
        ])
        prices._store("AAPL", self._quote())

        assert [b["close"] for b in db.prices.get_history("AAPL", "2026-07-01",
                                                          interval="1d")] == \
            [110.0, 110.0, 112.0]

    def test_a_one_for_one_split_is_ignored(self, stocked_db):
        import db
        self._seed()
        quote = {**self._quote(), "splits": [
            {"ex_date": "2026-07-26", "numerator": 1.0, "denominator": 1.0}]}
        prices._store("AAPL", quote)
        assert db.prices.get_splits("AAPL") == []

    def test_dividends_are_stored_and_re_run_cleanly(self, stocked_db):
        import db
        quote = {**self._quote(), "splits": [],
                 "dividends": [{"ex_date": "2026-07-26", "amount": 0.24}]}
        prices._store("AAPL", quote)
        prices._store("AAPL", quote)

        stored = db.prices.get_dividends("AAPL")
        assert len(stored) == 1
        assert stored[0]["amount"] == 0.24 and stored[0]["currency"] == "USD"


STOOQ_CSV = """Date,Open,High,Low,Close,Volume
2026-08-17,328.00,335.00,327.00,330.00,41000000
2026-08-18,331.00,341.00,330.50,338.19,52500000
"""


class TestStooqSymbols:
    """Mapping is the whole job. A wrong symbol does not fail — it silently
    backfills the chart with another venue's prices."""

    @pytest.mark.parametrize("yahoo_symbol,expected", [
        ("AAPL", "aapl.us"),
        ("SHEL.L", "shel.uk"),
        ("SAP.DE", "sap.de"),
        ("7203.T", "7203.jp"),
    ])
    def test_known_venues_map(self, yahoo_symbol, expected):
        assert stooq.to_stooq(yahoo_symbol) == expected

    @pytest.mark.parametrize("yahoo_symbol", [
        "SHEL.MU",      # Munich — same company, different venue, different price
        "BHP.AX",       # Sydney — Stooq does not carry it
        "^GSPC",        # index, named differently there
        "", "   ", ".L",
    ])
    def test_anything_unmapped_declines_rather_than_guesses(self, yahoo_symbol):
        assert stooq.to_stooq(yahoo_symbol) is None


class TestStooqBlocked:
    """An anti-bot interstitial arrives as a 200 carrying HTML, which is not
    the same as "no data for this symbol" even though both give no bars — one
    of them means the fallback itself has stopped falling back."""

    def test_a_browser_check_page_is_reported_not_swallowed(self, monkeypatch, capsys):
        from types import SimpleNamespace
        monkeypatch.setattr(stooq.scraper, "get", lambda *a, **kw: SimpleNamespace(
            status=200, ok=True,
            text='<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
                 "<body><noscript>This site requires JavaScript</noscript></body></html>"))

        assert stooq.history("AAPL") == []
        assert "blocked" in capsys.readouterr().out

    def test_a_genuine_no_data_answer_stays_quiet(self, monkeypatch, capsys):
        from types import SimpleNamespace
        monkeypatch.setattr(stooq.scraper, "get",
                            lambda *a, **kw: SimpleNamespace(status=200, ok=True,
                                                             text="No data"))
        assert stooq.history("AAPL") == []
        assert "blocked" not in capsys.readouterr().out


class TestStooqParsing:
    def test_bars_are_parsed_oldest_first(self):
        bars = stooq.parse_csv(STOOQ_CSV)
        assert [b["ts"] for b in bars] == \
            ["2026-08-17T00:00:00Z", "2026-08-18T00:00:00Z"]
        assert bars[-1] == {
            "ts": "2026-08-18T00:00:00Z", "close": 338.19, "open": 331.0,
            "high": 341.0, "low": 330.5, "volume": 52_500_000,
        }

    def test_the_no_data_body_is_not_a_bar(self):
        """Stooq answers an unknown symbol with HTTP 200 and this body, so the
        status code cannot be trusted."""
        assert stooq.parse_csv("No data") == []

    def test_a_row_with_no_close_is_skipped(self):
        text = "Date,Open,High,Low,Close,Volume\n2026-08-18,331.00,341.00,330.50,,0\n"
        assert stooq.parse_csv(text) == []

    def test_empty_and_junk_bodies_are_safe(self):
        assert stooq.parse_csv("") == []
        assert stooq.parse_csv("<html>nope</html>") == []


class TestStooqFetch:
    @staticmethod
    def _serving(text):
        from services.http_client import Fetched
        return lambda *a, **kw: Fetched(
            url="https://stooq.com/q/d/l/", status=200, text=text,
            content=text.encode(), headers={},
        )

    def test_history_drops_bars_older_than_the_requested_range(self, monkeypatch):
        """Stooq hands back decades; storing all of it for a 1-month chart
        would be a lot of rows nobody asked for."""
        old = "Date,Open,High,Low,Close,Volume\n1999-01-04,1,1,1,1.23,100\n"
        monkeypatch.setattr(stooq.scraper, "get", self._serving(old + STOOQ_CSV.split("\n", 1)[1]))
        bars = stooq.history("AAPL", range_="1mo")
        assert all(bar["ts"] > "2000" for bar in bars)

    def test_an_unmapped_symbol_never_reaches_the_network(self, monkeypatch):
        called = []
        monkeypatch.setattr(stooq.scraper, "get", lambda *a, **kw: called.append(1))
        assert stooq.history("SHEL.MU") == []
        assert not called


class TestStooqBackfill:
    """Runs in `refresh_stock`, not `get_quote`: deciding whether history has
    fallen behind needs the database, and the gate is what stops a Yahoo
    outage becoming a CSV download per stock every five minutes."""

    @staticmethod
    def _bar_less_quote(monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {
            "price": 338.19, "change": 0.0, "change_percent": 0.0,
            "points": [], "source": "cnbc", "currency": "USD"})

    def test_it_fills_history_when_the_stored_series_is_stale(self, stocked_db, monkeypatch):
        """A CNBC quote carries no bars; without this the chart just freezes."""
        import db
        self._bar_less_quote(monkeypatch)
        monkeypatch.setattr(prices.stooq, "history",
                            lambda *a, **kw: stooq.parse_csv(STOOQ_CSV))

        prices.refresh_stock("AAPL", range_="1mo")
        stored = db.prices.get_history("AAPL", "2026-01-01", interval="1d")
        assert [b["close"] for b in stored] == [330.0, 338.19]
        assert [b["volume"] for b in stored] == [41_000_000, 52_500_000]

    def test_a_current_series_is_not_re_downloaded(self, stocked_db, monkeypatch):
        """The five-minute price job must not pull a daily CSV every tick."""
        import db
        from normalize import days_ago_iso
        db.prices.upsert_points("AAPL", [
            {"ts": days_ago_iso(1)[:10] + "T00:00:00Z", "close": 338.19}])

        called = []
        self._bar_less_quote(monkeypatch)
        monkeypatch.setattr(prices.stooq, "history",
                            lambda *a, **kw: called.append(1) or [])

        prices.refresh_stock("AAPL", range_="5d")
        assert not called

    def test_a_source_with_its_own_bars_never_asks_stooq(self, stocked_db, monkeypatch):
        called = []
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: {
            "price": 338.19, "change": 0.0, "change_percent": 0.0,
            "source": "yahoo", "currency": "USD",
            "points": [{"ts": "2026-08-18T00:00:00Z", "close": 338.19}]})
        monkeypatch.setattr(prices.stooq, "history",
                            lambda *a, **kw: called.append(1) or [])

        prices.refresh_stock("AAPL")
        assert not called

    def test_every_source_failing_still_returns_none(self, stocked_db, monkeypatch):
        """Stooq backfills a quote; it does not invent one."""
        for name in ("from_yahoo", "from_nasdaq", "from_cnbc"):
            monkeypatch.setattr(prices, name, lambda *a, **kw: None)
        monkeypatch.setattr(prices.stooq, "history",
                            lambda *a, **kw: stooq.parse_csv(STOOQ_CSV))
        assert prices.refresh_stock("AAPL") is None

    def test_get_quote_alone_never_touches_the_network_for_history(self, monkeypatch):
        """It is a pure three-source chain; the DB-aware half lives elsewhere."""
        called = []
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {
            "price": 1.0, "points": [], "source": "cnbc"})
        monkeypatch.setattr(prices.stooq, "history",
                            lambda *a, **kw: called.append(1) or [])

        assert prices.get_quote("AAPL")["source"] == "cnbc"
        assert not called


class TestCnbcNumbers:
    @pytest.mark.parametrize("raw,expected", [
        ("3,323.50", 3323.5),
        ("-0.56%", -0.56),
        ("338.19", 338.19),
        (338.19, 338.19),
        ("UNCH", None),
        (None, None),
        ("", None),
    ])
    def test_display_strings_are_parsed(self, raw, expected):
        assert prices._to_float(raw) == expected


class TestCnbcSymbolCandidates:
    def test_yahoo_symbol_is_tried_first(self):
        """CNBC accepts `SHEL.L` directly, so no translation is needed."""
        assert prices._cnbc_candidates("SHEL.L")[0] == "SHEL.L"

    def test_suffix_is_translated_as_a_second_attempt(self):
        assert prices._cnbc_candidates("SHEL.L") == ["SHEL.L", "SHEL-GB"]
        assert prices._cnbc_candidates("SAP.DE") == ["SAP.DE", "SAP-DE"]

    def test_us_symbol_needs_no_translation(self):
        assert prices._cnbc_candidates("AAPL") == ["AAPL"]

    def test_unmapped_suffix_yields_only_the_original(self):
        assert prices._cnbc_candidates("XYZ.QQ") == ["XYZ.QQ"]


# Real Nasdaq shape. The two blocks swap meaning with the session: once the
# bell goes, `primaryData` becomes the extended-hours print and `secondaryData`
# holds the regular close.
NASDAQ_AFTER_HOURS = {"data": {
    "symbol": "AAPL", "exchange": "NASDAQ-GS", "marketStatus": "After-Hours",
    "primaryData": {
        "lastSalePrice": "$314.39", "netChange": "-19.04",
        "percentageChange": "-5.71%", "isRealTime": True,
        "lastTradeTimestamp": "Jul 30, 2026 7:26 PM ET",
    },
    "secondaryData": {
        "lastSalePrice": "$333.43", "netChange": "-4.76",
        "percentageChange": "-1.41%", "isRealTime": False,
        "lastTradeTimestamp": "Closed at Jul 30, 2026 4:00 PM ET",
    },
}}

NASDAQ_OPEN = {"data": {
    "symbol": "AAPL", "exchange": "NASDAQ-GS", "marketStatus": "Open",
    "primaryData": {
        "lastSalePrice": "$333.43", "netChange": "-4.76",
        "percentageChange": "-1.41%", "isRealTime": True,
        "lastTradeTimestamp": "Jul 30, 2026 2:10 PM ET",
    },
    "secondaryData": None,
}}


class TestNasdaqSource:
    def test_regular_session_price_wins_after_the_bell(self, monkeypatch):
        """Taking the live block blindly would store a 314.39 after-hours print
        as the day's close and put a point on the chart that neither Yahoo nor
        CNBC agrees with."""
        monkeypatch.setattr(prices.scraper, "get_json", lambda *a, **kw: NASDAQ_AFTER_HOURS)
        quote = prices.from_nasdaq("AAPL")
        assert quote["price"] == 333.43
        assert quote["change"] == -4.76
        assert quote["change_percent"] == -1.41
        assert quote["source"] == "nasdaq"

    def test_live_price_is_used_while_the_market_is_open(self, monkeypatch):
        monkeypatch.setattr(prices.scraper, "get_json", lambda *a, **kw: NASDAQ_OPEN)
        assert prices.from_nasdaq("AAPL")["price"] == 333.43

    def test_previous_close_is_derived_from_the_change(self, monkeypatch):
        monkeypatch.setattr(prices.scraper, "get_json", lambda *a, **kw: NASDAQ_AFTER_HOURS)
        assert prices.from_nasdaq("AAPL")["previous_close"] == 338.19

    def test_foreign_listings_are_refused_without_a_request(self, monkeypatch):
        """`SHEL.L` is the London ordinary; Nasdaq would answer with the New
        York ADR, at a different price in a different currency."""
        calls = []
        monkeypatch.setattr(prices.scraper, "get_json",
                            lambda *a, **kw: calls.append(a) or None)
        assert prices.from_nasdaq("SHEL.L") is None
        assert prices.from_nasdaq("^GSPC") is None
        assert calls == []

    def test_malformed_payloads_return_none_rather_than_raising(self, monkeypatch):
        for payload in (None, {}, {"data": None}, {"data": {}},
                        {"data": {"primaryData": {"lastSalePrice": "UNCH"}}}):
            monkeypatch.setattr(prices.scraper, "get_json", lambda *a, _p=payload, **kw: _p)
            assert prices.from_nasdaq("AAPL") is None


class TestFallbackChain:
    def test_nasdaq_is_used_when_yahoo_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: {"price": 3.0, "source": "nasdaq"})
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {"price": 1.0, "source": "cnbc"})
        assert prices.get_quote("AAPL")["source"] == "nasdaq"

    def test_cnbc_is_used_when_yahoo_and_nasdaq_return_nothing(self, monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {"price": 1.0, "source": "cnbc"})
        assert prices.get_quote("AAPL")["source"] == "cnbc"

    def test_yahoo_is_preferred_when_available(self, monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: {"price": 2.0, "source": "yahoo"})
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: {"price": 3.0, "source": "nasdaq"})
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {"price": 1.0, "source": "cnbc"})
        assert prices.get_quote("AAPL")["source"] == "yahoo"

    def test_every_source_failing_returns_none(self, monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: None)
        assert prices.get_quote("AAPL") is None


class TestStoring:
    def test_daily_bars_snap_to_midnight_so_one_day_is_one_row(self, stocked_db):
        """Exchanges report session times; without normalising, the same trading
        day would land in different rows depending on the venue."""
        import db
        prices._store("AAPL", {
            "price": 338.19, "change": -1.89, "change_percent": -0.556, "currency": "USD",
            "points": [
                {"ts": "2026-07-29T13:30:00Z", "close": 338.19, "volume": 52_500_000},
                {"ts": "2026-07-28T13:30:00Z", "close": 340.08, "volume": 41_000_000},
            ],
        })
        bars = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["ts"] for b in bars] == ["2026-07-28T00:00:00Z", "2026-07-29T00:00:00Z"]
        assert [b["volume"] for b in bars] == [41_000_000, 52_500_000]

    def test_quote_is_written_to_the_stock_row(self, stocked_db):
        import db
        prices._store("AAPL", {"price": 400.0, "change": 1.0, "change_percent": 0.25,
                               "currency": "USD", "points": []})
        row = db.stocks.get_stock("AAPL")
        assert row["price"] == 400.0 and row["price_updated_at"]

    def test_unchanged_price_does_not_add_another_snapshot(self, stocked_db):
        """Overnight and weekend refreshes would otherwise stamp out a flat line
        of duplicate points."""
        import db
        prices._store_snapshot("AAPL", 338.19)
        first = db.prices.get_history("AAPL", "2000-01-01", interval="snap")
        prices._store_snapshot("AAPL", 338.19)
        assert db.prices.get_history("AAPL", "2000-01-01", interval="snap") == first

    def test_a_moved_price_does_add_a_snapshot(self, stocked_db):
        import db
        prices._store_snapshot("AAPL", 338.19)
        prices._store_snapshot("AAPL", 339.00)
        assert len(db.prices.get_history("AAPL", "2000-01-01", interval="snap")) >= 1
        assert db.prices.latest("AAPL")["close"] == 339.00
