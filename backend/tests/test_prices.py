"""Price parsing, currency normalisation and the fallback chain.

The currency tests are the important ones: an LSE quote arrives in pence, and
storing 3323.5 where 33.235 belongs is a silent 100x error in every chart and
percentage on the page.
"""
from __future__ import annotations

import pytest

from services import events, prices, stooq, yahoo


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


# ── Spark batch quotes ───────────────────────────────────────────────────

def spark_entry(symbol, **chart):
    """One spark result: a chart node wrapped under the symbol it answers."""
    node = chart_payload(**chart)["chart"]["result"][0]
    node["meta"]["symbol"] = symbol
    return {"symbol": symbol, "response": [node]}


def spark_payload(*entries):
    return {"spark": {"result": list(entries)}}


class TestSpark:
    def test_spark_is_keyed_by_symbol_not_position(self, monkeypatch):
        """Spark answers in no fixed order and silently drops an unknown
        symbol, so reading results by position would quote MSFT as AAPL."""
        payload = spark_payload(spark_entry("MSFT", price=500.0),
                                spark_entry("AAPL", price=338.19))
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)

        quotes = yahoo.spark(["AAPL", "NOPE", "MSFT"])

        assert set(quotes) == {"AAPL", "MSFT"}
        assert quotes["AAPL"]["price"] == 338.19
        assert quotes["MSFT"]["price"] == 500.0

    def test_an_lse_spark_quote_is_converted_to_pounds(self, monkeypatch):
        payload = spark_payload(spark_entry(
            "SHEL.L", currency="GBp", price=3567.0, previous=3574.5))
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)

        quote = yahoo.spark(["SHEL.L"])["SHEL.L"]

        assert quote["currency"] == "GBP"
        assert quote["price"] == pytest.approx(35.67)
        assert quote["change"] == pytest.approx(-0.075)
        assert quote["change_percent"] == pytest.approx(-0.21, abs=1e-2)

    def test_an_entry_without_previous_close_is_dropped(self, monkeypatch):
        """parse_chart would fall back to the prior bar, which in spark's
        series is five minutes ago — a five-minute move stored as the day's."""
        payload = spark_payload(spark_entry("AAPL", previous=None),
                                spark_entry("MSFT", price=500.0))
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)

        assert set(yahoo.spark(["AAPL", "MSFT"])) == {"MSFT"}

    def test_more_than_twenty_symbols_split_and_a_failed_chunk_costs_only_itself(
            self, monkeypatch):
        """A twenty-first symbol fails the whole request with a 400."""
        symbols = [f"S{i:02d}" for i in range(45)]
        asked = []

        def fake(hosts, path, params, cache=True):
            chunk = params["symbols"].split(",")
            asked.append(chunk)
            if len(asked) == 2:
                return None                            # this chunk fails
            return spark_payload(*(spark_entry(s) for s in chunk))

        monkeypatch.setattr(yahoo, "_try_hosts", fake)

        quotes = yahoo.spark(symbols)

        assert [len(c) for c in asked] == [20, 20, 5]
        assert sorted(quotes) == symbols[:20] + symbols[40:]


class TestSparkQuotes:
    def test_spark_quotes_update_the_row_and_store_no_bars(self, stocked_db, monkeypatch):
        """Quote only. `_store` would file spark's 5m closes as daily bars, and
        the quote belongs to the short name, not the Yahoo symbol it was asked
        under."""
        import db

        asked = []
        rklb = yahoo.parse_chart(chart_payload(price=70.0, previous=68.0))
        monkeypatch.setattr(prices, "markets_open", lambda: True)
        monkeypatch.setattr(prices.yahoo, "spark",
                            lambda symbols: asked.extend(symbols) or {"RKLB": rklb})

        result = prices.refresh_watchlist_quotes()

        assert sorted(asked) == ["AAPL", "RKLB"]
        assert result["updated"] == ["6RJ0"]
        row = db.stocks.get_stock("6RJ0")
        assert row["price"] == 70.0 and row["price_change"] == 2.0
        assert row["price_updated_at"]
        assert db.stocks.get_stock("RKLB") is None
        assert db.stocks.get_stock("AAPL")["price"] == 338.19      # not answered
        assert db.prices.latest("6RJ0")["close"] == 70.0           # the snapshot
        for interval in ("1d", db.prices.INTRADAY):
            assert db.prices.get_history("6RJ0", "2000-01-01", interval=interval) == []

    def test_a_shut_market_never_requests_quotes(self, stocked_db, monkeypatch):
        called = []
        monkeypatch.setattr(prices, "markets_open", lambda: False)
        monkeypatch.setattr(prices.yahoo, "spark", lambda symbols: called.append(symbols) or {})

        result = prices.refresh_watchlist_quotes()

        assert called == [] and result["skipped"] == "markets closed"

    def test_a_shared_symbol_quotes_every_short_name_and_unresolved_stocks_are_left_out(
            self, stocked_db, monkeypatch):
        """Two short names can resolve to one Yahoo symbol: it is asked for
        once and both rows take the whole quote. A followed stock with no Yahoo
        symbol yet stays out of the request — a None among the symbols would
        fail the join, and with it the whole pass, every minute."""
        import db

        db.stocks.bulk_upsert_stocks([
            {"shortName": "RKLB", "name": "Rocket Lab", "type": "STOCK", "currencyCode": "USD"},
        ])
        db.stocks.set_resolution("RKLB", "RKLB", "NMS", None)
        db.watchlist.add("RKLB")
        db.watchlist.add("TSLA")                       # followed, never resolved
        db.watchlist.add("GHOST")                      # followed, not in the catalogue

        asked = []
        rklb = yahoo.parse_chart(chart_payload(price=70.0, previous=68.0))
        monkeypatch.setattr(prices, "markets_open", lambda: True)
        monkeypatch.setattr(prices.yahoo, "spark",
                            lambda symbols: asked.extend(symbols) or {"RKLB": rklb})

        result = prices.refresh_watchlist_quotes()

        assert sorted(asked) == ["AAPL", "RKLB"]
        assert sorted(result["updated"]) == ["6RJ0", "RKLB"]
        for short_name in ("6RJ0", "RKLB"):
            row = db.stocks.get_stock(short_name)
            assert row["price"] == 70.0 and row["price_change"] == 2.0
            assert row["price_change_percent"] == pytest.approx(2.941)
            assert row["quote_currency"] == "USD"
        assert db.stocks.get_stock("TSLA")["price"] is None

    def test_the_quotes_job_is_the_spark_pass(self, monkeypatch):
        import jobs

        monkeypatch.setattr(jobs.prices, "refresh_watchlist_quotes",
                            lambda: {"updated": ["AAPL"]})

        assert jobs.refresh_quotes() == {"updated": ["AAPL"]}


# ── Intraday bars and move attribution (3.1) ─────────────────────────────

# 2026-08-27, a Thursday. 13:30–20:00 UTC is the New York regular session.
SESSION_OPEN = 1787837400     # 13:30:00Z
SESSION_CLOSE = 1787860800    # 20:00:00Z


def intraday_payload(stamps, closes, volumes=None, session=True):
    """A 5m chart payload carrying an explicit regular-session window."""
    payload = chart_payload(closes=closes, stamps=stamps, volume=volumes)
    meta = payload["chart"]["result"][0]["meta"]
    if session:
        meta["currentTradingPeriod"] = {
            "pre": {"start": SESSION_OPEN - 19800, "end": SESSION_OPEN},
            "regular": {"start": SESSION_OPEN, "end": SESSION_CLOSE},
            "post": {"start": SESSION_CLOSE, "end": SESSION_CLOSE + 14400},
        }
    return payload


def bars(count=31, start_close=100.0, step=0.5, volume=1000, day="2026-08-27"):
    """`count` 5m bars from the open, rising `step` a bar."""
    from datetime import datetime, timezone

    out = []
    for i in range(count):
        stamp = datetime.fromtimestamp(SESSION_OPEN + i * 300, timezone.utc)
        out.append({"ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ").replace("2026-08-27", day),
                    "close": start_close + i * step, "volume": volume})
    return out


class TestIntradayCaching:
    """A cached 5m bar served as live is worse than no bar: a stale number
    wearing a fresh timestamp."""

    @staticmethod
    def _capture(monkeypatch):
        seen = {}

        def fake(*a, cache=True, **kw):
            seen["cache"] = cache
            return None

        monkeypatch.setattr(yahoo.scraper, "get_json", fake)
        return seen

    def test_intraday_requests_skip_the_cache(self, monkeypatch):
        seen = self._capture(monkeypatch)
        yahoo.chart("AAPL", range_="1d", interval="5m")
        assert seen["cache"] is False

    def test_daily_requests_still_cache(self, monkeypatch):
        seen = self._capture(monkeypatch)
        yahoo.chart("AAPL", range_="1mo", interval="1d")
        assert seen["cache"] is True

    def test_spark_requests_skip_the_cache(self, monkeypatch):
        """A one-minute quote behind a five-minute cache is a five-minute quote."""
        seen = self._capture(monkeypatch)
        yahoo.spark(["AAPL"])
        assert seen["cache"] is False


class TestSessionFilter:
    """`includePrePost=false` is already on the request and Yahoo mostly
    honours it. *Mostly* is why this exists — a thin pre-market print is
    exactly the bar that would make an event look like it moved the stock."""

    def test_a_bar_outside_the_session_is_not_stored(self, stocked_db, monkeypatch):
        import db

        # 13:20Z is pre-market; the other three are inside the session.
        stamps = [SESSION_OPEN - 600, SESSION_OPEN, SESSION_OPEN + 300, SESSION_OPEN + 600]
        payload = intraday_payload(stamps, [99.0, 100.0, 101.0, 102.0])
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)

        assert prices.refresh_intraday("AAPL") == 3

        stored = db.prices.get_history("AAPL", "2026-01-01", interval="5m")
        assert [b["close"] for b in stored] == [100.0, 101.0, 102.0]

    def test_yesterdays_bars_survive_todays_session_window(self, stocked_db, monkeypatch):
        """The bug the live smoke caught. `currentTradingPeriod` is the current
        *or next* session: asked before the opening bell it names today, while
        `range=1d` hands back yesterday's bars. Comparing absolute timestamps
        dropped all 79 of them. The bell rings at the same clock time either
        way, so the hours are what gets compared."""
        import db

        # Bars from the 26th; the payload's session block names the 27th.
        yesterday = [SESSION_OPEN - 86400 + i * 300 for i in range(3)]
        payload = intraday_payload(yesterday, [100.0, 101.0, 102.0])
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)

        assert prices.refresh_intraday("AAPL") == 3

        stored = db.prices.get_history("AAPL", "2026-01-01", interval="5m")
        assert [b["ts"][:10] for b in stored] == ["2026-08-26"] * 3

    def test_a_session_that_crosses_midnight_utc_still_bounds(self):
        """Sydney and Tokyo open before midnight UTC and close after it, so the
        window wraps rather than being empty."""
        chart = {
            "session": {"start": "2026-08-26T23:00:00Z", "end": "2026-08-27T06:00:00Z"},
            "points": [
                {"ts": "2026-08-26T23:30:00Z", "close": 1.0},   # in
                {"ts": "2026-08-27T05:30:00Z", "close": 2.0},   # in
                {"ts": "2026-08-27T12:00:00Z", "close": 3.0},   # out
            ],
        }
        assert [b["close"] for b in prices.in_session(chart)] == [1.0, 2.0]

    def test_a_payload_with_no_session_window_is_stored_whole(self):
        """Yahoo's own filter is then the only one there is, and trusting it
        beats discarding a day of bars."""
        chart = yahoo.parse_chart(intraday_payload(
            [SESSION_OPEN - 600, SESSION_OPEN], [99.0, 100.0], session=False))
        assert chart["session"] is None
        assert len(prices.in_session(chart)) == 2

    def test_the_session_window_is_read_off_the_payload(self):
        chart = yahoo.parse_chart(intraday_payload([SESSION_OPEN], [100.0]))
        assert chart["session"] == {"start": "2026-08-27T13:30:00Z",
                                   "end": "2026-08-27T20:00:00Z"}

    def test_intraday_bars_stay_out_of_the_chart_series(self, stocked_db):
        """The chart is a month of daily closes. A fortnight of 5m bars in it
        would render one day of noise across the whole line."""
        import db

        db.prices.upsert_points("AAPL", [("2026-08-27T00:00:00Z", 100.0)], interval="1d")
        db.prices.upsert_points("AAPL", [("2026-08-27T13:35:00Z", 101.0)], interval="5m")

        series = db.prices.get_series("AAPL", "2026-08-01")
        assert [p["close"] for p in series] == [100.0]


class TestMoveAttribution:
    """The one thing `impact` cannot say: whether anyone trading agreed.

    Measured over a window rather than an instant on purpose — scraped publish
    timestamps are good to about the hour, and both ends are stored so nothing
    downstream can read more precision into it than that.
    """

    def test_the_window_runs_from_the_bar_the_news_landed_in(self, stocked_db):
        import db

        db.prices.upsert_points("AAPL", bars(), interval="5m")

        # 14:02Z lands inside the 14:00 bar, which is the base: the last price
        # before anyone could have read the story.
        move = events.measure("AAPL", "2026-08-27T14:02:00Z")

        assert move["from_ts"] == "2026-08-27T14:00:00Z"
        # 24 bars on from 14:00 is 16:00 — two hours of trading.
        assert move["to_ts"] == "2026-08-27T16:00:00Z"
        # Base 103.0, end 115.0.
        assert move["percent"] == pytest.approx(11.65, abs=0.01)

    def test_the_window_is_counted_in_bars_not_clock_time(self, stocked_db):
        """An overnight gap is not a flat reaction. Bars exist only while the
        exchange is open, so 24 of them is two *trading* hours either way."""
        import db

        db.prices.upsert_points(
            "AAPL", bars(count=6) + bars(count=24, start_close=110.0, day="2026-08-28"),
            interval="5m")

        # News after the first day's last bar. A two-hour clock window would
        # find nothing and report 0%; a 24-bar window crosses into the next day.
        move = events.measure("AAPL", "2026-08-27T14:30:00Z")

        assert move["from_ts"] == "2026-08-27T13:55:00Z"
        assert move["to_ts"].startswith("2026-08-28")
        assert move["percent"] > 0

    def test_a_window_with_no_bars_is_none_not_zero(self, stocked_db):
        """None means unmeasurable. 0.0 would read as "the market shrugged"."""
        assert events.measure("AAPL", "2026-08-27T14:02:00Z") is None

    def test_a_single_bar_is_not_a_window(self, stocked_db):
        import db

        db.prices.upsert_points("AAPL", bars(count=1), interval="5m")
        assert events.measure("AAPL", "2026-08-27T14:02:00Z") is None

    def test_volume_is_read_against_a_median_bar(self, stocked_db):
        """A 2% move on ordinary volume and a 2% move on five times the usual
        are different events, and the percentage cannot tell them apart."""
        import db

        quiet = bars(count=20, volume=1000)
        busy = [{**b, "volume": 5000} for b in bars(count=30)[20:]]
        db.prices.upsert_points("AAPL", quiet + busy, interval="5m")

        move = events.measure("AAPL", quiet[-1]["ts"])
        assert move["volume_x"] == pytest.approx(5.0, abs=0.01)

    def test_an_unknown_volume_ratio_is_none_rather_than_one(self, stocked_db):
        import db

        db.prices.upsert_points(
            "AAPL", [{**b, "volume": None} for b in bars()], interval="5m")
        assert events.measure("AAPL", "2026-08-27T14:02:00Z")["volume_x"] is None

    def test_attribution_fills_in_a_settled_event(self, stocked_db):
        import db
        from normalize import days_ago_iso

        db.prices.upsert_points("AAPL", bars(), interval="5m")
        news_ids = db.news.insert_news_many([{
            "short_name": "AAPL", "title": "Apple beats", "title_key": "apple-beats",
            "url": "https://x.test/1", "url_hash": "h1", "source": "Reuters",
            "source_domain": "reuters.com", "source_type": "GOOGLE_NEWS",
            "publish_time": "2026-08-27T14:02:00Z",
        }])
        event_id = db.events.insert_event(
            "AAPL", headline="Beat", why_it_matters="Revenue",
            impact="high", news_ids=news_ids)
        # Backdate past the settle window so the event is eligible.
        with db.get_connection() as conn:
            conn.execute("UPDATE events SET created_at = %s WHERE id = %s",
                         (days_ago_iso(4), event_id))

        assert events.attribute_pending() == 1

        stored = db.events.get_events(["AAPL"])[0]
        assert stored["move_from_ts"] == "2026-08-27T14:00:00Z"
        assert stored["move_to_ts"] == "2026-08-27T16:00:00Z"
        assert stored["move_percent"] == pytest.approx(11.65, abs=0.01)
        # Measured once. A second pass must not re-examine it.
        assert events.attribute_pending() == 0

    def test_an_event_inside_its_window_is_left_alone(self, stocked_db):
        """Attributing half a reaction is worse than not attributing it."""
        import db

        db.prices.upsert_points("AAPL", bars(), interval="5m")
        db.events.insert_event("AAPL", headline="Fresh", why_it_matters="x",
                               impact="high", news_ids=[])
        assert events.attribute_pending() == 0

    def test_an_event_older_than_the_bars_ages_out(self, stocked_db):
        """5m bars are pruned at a fortnight, so an older event can never be
        attributed. It has to fall out of the pending set rather than be
        re-examined on every pass forever."""
        import db
        from normalize import days_ago_iso

        event_id = db.events.insert_event("AAPL", headline="Ancient",
                                          why_it_matters="x", impact="low",
                                          news_ids=[])
        with db.get_connection() as conn:
            conn.execute("UPDATE events SET created_at = %s WHERE id = %s",
                         (days_ago_iso(30), event_id))
        assert db.events.needing_attribution(72, 14) == []


class TestIntradayRetention:
    """5m bars are ~78 rows per stock per trading day. Kept on the 400-day
    price window they would be the biggest table in the database in a month."""

    def test_5m_prunes_on_the_short_window_and_daily_on_the_long_one(self, stocked_db):
        import db
        import jobs
        from normalize import days_ago_iso

        db.prices.upsert_points("AAPL", [
            (days_ago_iso(20), 100.0),        # old 5m — dropped
            (days_ago_iso(3), 101.0),         # recent 5m — kept
        ], interval="5m")
        db.prices.upsert_points("AAPL", [
            (days_ago_iso(500), 90.0),        # old daily — dropped
            (days_ago_iso(20), 95.0),         # daily inside the 5m window — kept
        ], interval="1d")

        pruned = jobs.prune()

        kept_5m = db.prices.get_history("AAPL", days_ago_iso(900), interval="5m")
        kept_1d = db.prices.get_history("AAPL", days_ago_iso(900), interval="1d")
        assert [b["close"] for b in kept_5m] == [101.0]
        assert [b["close"] for b in kept_1d] == [95.0]
        assert pruned["intraday_points"] == 1


class TestIntradaySessionGate:
    def test_a_shut_market_never_reaches_the_network(self, stocked_db, monkeypatch):
        """Ungated this is an all-night poll for a price that cannot move."""
        called = []
        monkeypatch.setattr(prices, "markets_open", lambda: False)
        monkeypatch.setattr(prices, "refresh_intraday", lambda s: called.append(s))

        result = prices.refresh_watchlist_intraday()

        assert called == [] and result["skipped"] == "markets closed"

    def test_an_open_market_pulls_every_followed_stock(self, stocked_db, monkeypatch):
        called = []
        monkeypatch.setattr(prices, "markets_open", lambda: True)
        monkeypatch.setattr(prices, "refresh_intraday",
                            lambda s: called.append(s) or 5)

        result = prices.refresh_watchlist_intraday()

        assert sorted(called) == ["6RJ0", "AAPL"]
        assert result["stored"] == 10


class TestPriceCadence:
    """jobs.refresh_prices on its five-minute tick: the full chart pass once an
    hour, open or shut; in between, the chain for stale quotes while open and
    nothing while shut."""

    @staticmethod
    def _clock(monkeypatch, start):
        from types import SimpleNamespace

        import jobs

        now = [start]
        monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: now[0]))
        # Left at its module default, so the start-up case tests the real
        # initial value; setattr only makes sure the test's stamps are undone.
        monkeypatch.setattr(jobs, "_last_price_refresh", jobs._last_price_refresh)
        return now

    @staticmethod
    def _passes(monkeypatch):
        import jobs

        calls = []
        monkeypatch.setattr(jobs.prices, "refresh_watchlist",
                            lambda **kw: calls.append(kw) or {"updated": [], "failed": []})
        return calls

    def test_closed_market_ticks_stay_hourly(self, monkeypatch):
        import jobs

        # Five seconds after boot: the start-up pass must still run.
        now = self._clock(monkeypatch, 5.0)
        calls = self._passes(monkeypatch)
        monkeypatch.setattr(jobs, "markets_open", lambda: False)

        jobs.refresh_prices()
        now[0] += 300
        skipped = jobs.refresh_prices()
        now[0] += 3300
        jobs.refresh_prices()

        assert skipped["skipped"] == "markets closed"
        assert calls == [{"range_": "5d"}, {"range_": "5d"}]

    def test_the_full_chart_pass_still_runs_hourly_while_open(self, monkeypatch):
        import jobs

        now = self._clock(monkeypatch, 10_000.0)
        calls = self._passes(monkeypatch)
        monkeypatch.setattr(jobs, "markets_open", lambda: True)

        for _ in range(13):                    # an hour of five-minute ticks
            jobs.refresh_prices()
            now[0] += 300

        assert calls[0] == calls[12] == {"range_": "5d"}
        assert calls[1:12] == [{"stale_only": True}] * 11

    def test_an_open_tick_only_walks_stale_quotes(self, stocked_db, monkeypatch):
        """Spark keeps AAPL fresh; 6RJ0 has never been quoted. If spark then
        fails outright, every quote goes stale and every stock falls back."""
        from datetime import timedelta

        import db
        import jobs
        import settings
        from normalize import now_utc, to_iso

        now = self._clock(monkeypatch, 10_000.0)
        monkeypatch.setattr(jobs, "_last_price_refresh", now[0])   # not due
        monkeypatch.setattr(jobs, "markets_open", lambda: True)
        walked = []
        monkeypatch.setattr(prices, "refresh_stock",
                            lambda short_name, **kw: walked.append(short_name) or {"price": 1.0})
        monkeypatch.setattr(prices, "ensure_classified", lambda stock: False)

        jobs.refresh_prices()
        assert walked == ["6RJ0"]

        # A minute past two spark intervals.
        age = timedelta(minutes=2 * settings.QUOTE_REFRESH_MINUTES + 1)
        with db.get_connection() as conn:
            conn.execute("UPDATE stocks SET price_updated_at = %s WHERE short_name = 'AAPL'",
                         (to_iso(now_utc() - age),))
        walked.clear()
        now[0] += 300
        jobs.refresh_prices()
        assert sorted(walked) == ["6RJ0", "AAPL"]


def test_scheduler_has_a_minute_quotes_job():
    """Importing scheduler builds the jobs without starting anything."""
    from datetime import timedelta

    import jobs
    import scheduler
    import settings

    job = scheduler.scheduler.get_job("quotes")
    assert job.func is jobs.refresh_quotes
    assert job.trigger.interval == timedelta(minutes=settings.QUOTE_REFRESH_MINUTES)
    assert not scheduler.scheduler.running
