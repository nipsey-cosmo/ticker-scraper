"""Price parsing, currency normalisation and the fallback chain.

The currency tests are the important ones: an LSE quote arrives in pence, and
storing 3323.5 where 33.235 belongs is a silent 100x error in every chart and
percentage on the page.
"""
from __future__ import annotations

import json
import re

import pytest

from services import events, prices, yahoo


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


# A live SHEL.L answer, trimmed to three bars. Prices are strings in the quote's
# units (pence here), oldest first, and the response names no currency.
CNBC_BARS = {
    "barData": {
        "priceBars": [
            {"open": "3665.5000", "high": "3673.0000", "low": "3585.0000",
             "close": "3624.5000", "volume": 15510301,
             "tradeTime": "20261110000000", "tradeTimeinMills": 1794286800000},
            {"open": "3580.5000", "high": "3605.0000", "low": "3559.0000",
             "close": "3574.5000", "volume": 6255571,
             "tradeTime": "20261119000000", "tradeTimeinMills": 1795064400000},
            {"open": "3564.0000", "high": "3587.5000", "low": "3435.1750",
             "close": "3574.5000", "volume": 10519265,
             "tradeTime": "20261120000000", "tradeTimeinMills": 1795150800000},
        ],
        "symbol": "SHEL.L",
    },
    "requestDetails": {"symbol": "SHEL.L", "startTime": "20261110000000",
                       "endTime": "20261123235959", "adjusted": "UNADJUSTED"},
}

# An unknown symbol is an HTTP 200 carrying this, with no `barData` at all.
CNBC_UNKNOWN = {
    "requestDetails": {"symbol": "NOPE.ZZ", "startTime": "20261110000000",
                       "endTime": "20261123235959", "adjusted": "UNADJUSTED"},
    "status": "ERROR",
    "statusMessage": "Error in getting bars for symbol: NOPE.ZZ and barType: "
                     "ONE_DAY Security Master not found",
}


class TestCnbcBars:
    def test_saved_payload_parses_oldest_first_at_midnight_utc(self):
        bars = prices.parse_cnbc_bars(CNBC_BARS, 1.0)
        assert [b["ts"] for b in bars] == [
            "2026-11-10T00:00:00Z", "2026-11-19T00:00:00Z", "2026-11-20T00:00:00Z"]
        assert bars[-1] == {
            "ts": "2026-11-20T00:00:00Z", "close": 3574.5, "open": 3564.0,
            "high": 3587.5, "low": 3435.175, "volume": 10_519_265,
        }

    def test_pence_becomes_pounds_and_volume_is_untouched(self):
        """Volume is a share count; dividing it would report a hundredth of
        the real turnover on every LSE line."""
        bar = prices.parse_cnbc_bars(CNBC_BARS, 100.0)[-1]
        assert bar["close"] == 35.745
        assert bar["low"] == pytest.approx(34.35175)
        assert bar["volume"] == 10_519_265

    @pytest.mark.parametrize("payload", [
        CNBC_UNKNOWN, None, {}, {"barData": None}, "not json",
        {"barData": {"priceBars": [{"open": "1", "close": None,
                                    "tradeTime": "20261120000000"}]}},
    ])
    def test_unknown_symbol_and_junk_give_no_bars(self, payload):
        assert prices.parse_cnbc_bars(payload, 1.0) == []


class TestCnbcHistory:
    @staticmethod
    def _recording(monkeypatch, answer=None):
        urls = []
        monkeypatch.setattr(prices.scraper, "get_json",
                            lambda url, **kw: urls.append(url) or answer)
        return urls

    def test_no_stated_currency_refuses_without_fetching_bars(self, monkeypatch):
        """The bars name no currency. Guessing would store pence as pounds."""
        monkeypatch.setattr(prices, "_cnbc_raw",
                            lambda symbol: {"symbol": symbol, "last": "3,574.50"})
        urls = self._recording(monkeypatch, CNBC_BARS)

        assert prices._cnbc_history("SHEL.L") == []
        assert urls == []

    def test_translated_symbol_is_used_when_the_yahoo_form_fails(self, monkeypatch):
        monkeypatch.setattr(prices, "_cnbc_raw", lambda symbol: (
            None if symbol == "SHEL.L" else {"symbol": symbol, "currencyCode": "GBp"}))
        urls = self._recording(monkeypatch, CNBC_BARS)

        bars = prices._cnbc_history("SHEL.L")
        assert bars[-1]["close"] == 35.745
        assert len(urls) == 1
        # Unadjusted, and whole-day stamps so the URL is the same all day.
        assert re.search(r"/bars/SHEL-GB/1D/\d{8}000000/\d{8}235959/unadjusted/",
                         urls[0])

    # CNBC's live record for TEVA.TA, trimmed: agorot, labelled as shekels.
    TEL_AVIV = {"symbol": "TEVA.TA", "last": "13,270.00",
                "previous_day_closing": "13,210.00", "change": "60.00",
                "currencyCode": "ILS", "countryCode": "IL",
                "exchange": "Tel Aviv Stock Exchange"}

    def test_tel_aviv_bars_are_agorot_whatever_the_label_says(self, monkeypatch):
        """Yahoo calls these lines ILA and stores 132.70. Believing CNBC's
        `ILS` would write a month of bars over those rows at 13,270."""
        monkeypatch.setattr(prices, "_cnbc_raw", lambda symbol: dict(self.TEL_AVIV))
        self._recording(monkeypatch, CNBC_BARS)

        bar = prices._cnbc_history("TEVA.TA")[-1]
        assert bar["close"] == 35.745
        assert bar["volume"] == 10_519_265

    def test_tel_aviv_quote_is_agorot_too(self, monkeypatch):
        """Same record, same units: a quote left at 13,270 beside bars at
        132.10 is the cliff the divisor exists to prevent."""
        monkeypatch.setattr(prices, "_cnbc_raw", lambda symbol: dict(self.TEL_AVIV))

        quote = prices.from_cnbc("TEVA.TA")
        assert (quote["price"], quote["previous_close"], quote["change"]) == \
            (132.7, 132.1, 0.6)
        assert quote["currency"] == "ILS"

    def test_shekels_quoted_anywhere_else_are_left_alone(self):
        """The rule is Tel Aviv's quoting convention, not the currency's."""
        assert prices._cnbc_units({"currencyCode": "ILS", "countryCode": "US"}) == \
            ("ILS", 1.0)
        assert prices._cnbc_units({"currencyCode": "GBp", "countryCode": "GB"}) == \
            ("GBP", 100.0)

    @pytest.mark.parametrize("yahoo_symbol", ["BHP.AX", "FPH.NZ"])
    def test_sydney_and_auckland_are_skipped_without_a_request(self, monkeypatch,
                                                                yahoo_symbol):
        """Yahoo stamps those daily bars a UTC day early, so mixing in CNBC's
        would duplicate or drop a session at the seam."""
        calls = []
        monkeypatch.setattr(prices.scraper, "get", lambda *a, **kw: calls.append(a))
        monkeypatch.setattr(prices.scraper, "get_json", lambda *a, **kw: calls.append(a))

        assert prices._cnbc_history(yahoo_symbol) == []
        assert calls == []


class TestHistoryBackfill:
    """Runs in `refresh_stock`, not `get_quote`: deciding whether history has
    fallen behind needs the database, and the gate is what stops a Yahoo
    outage becoming a month of bars per stock every five minutes."""

    @pytest.fixture(autouse=True)
    def _nothing_asked_yet(self, monkeypatch):
        # The once-a-day memo is a module global; without this, one test's ask
        # would silence the next test's.
        monkeypatch.setattr(prices, "_history_asked", {})

    @staticmethod
    def _bar_less_quote(monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_nasdaq", lambda *a, **kw: None)
        monkeypatch.setattr(prices, "from_cnbc", lambda *a, **kw: {
            "price": 338.19, "change": 0.0, "change_percent": 0.0,
            "points": [], "source": "cnbc", "currency": "USD"})

    @staticmethod
    def _counting(monkeypatch, bars=()):
        called = []
        monkeypatch.setattr(prices, "_cnbc_history",
                            lambda *a, **kw: called.append(1) or [dict(b) for b in bars])
        return called

    def test_yahoo_down_stores_fallback_quote_and_cnbc_bars(self, stocked_db, monkeypatch):
        """End to end below the network: Nasdaq answers the quote, CNBC's quote
        supplies the currency, CNBC's bars supply the history."""
        import db
        from services.http_client import Fetched

        cnbc_quote = json.dumps({"FormattedQuoteResult": {"FormattedQuote": [
            {"symbol": "AAPL", "last": "333.43", "currencyCode": "USD"}]}})

        def get_json(url, **kw):
            if "api.nasdaq.com" in url:
                return NASDAQ_OPEN
            if "ts-api.cnbc.com" in url:
                return CNBC_BARS
            raise AssertionError(f"unexpected request to {url}")

        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: None)
        monkeypatch.setattr(prices.scraper, "get_json", get_json)
        monkeypatch.setattr(prices.scraper, "get", lambda url, **kw: Fetched(
            url=url, status=200, text=cnbc_quote, content=cnbc_quote.encode(),
            headers={}))

        assert prices.refresh_stock("AAPL")["source"] == "nasdaq"
        assert db.stocks.get_stock("AAPL")["price"] == 333.43
        stored = db.prices.get_history("AAPL", "2026-01-01", interval="1d")
        assert [b["close"] for b in stored] == [3624.5, 3574.5, 3574.5]
        assert stored[-1]["volume"] == 10_519_265

    def test_a_current_series_is_not_fetched(self, stocked_db, monkeypatch):
        """The five-minute price job must not pull a month of bars every tick."""
        import db
        from normalize import days_ago_iso
        db.prices.upsert_points("AAPL", [
            {"ts": days_ago_iso(1)[:10] + "T00:00:00Z", "close": 338.19}])
        self._bar_less_quote(monkeypatch)
        called = self._counting(monkeypatch)

        prices.refresh_stock("AAPL")
        assert not called

    def test_a_stale_series_is_asked_once_a_day(self, stocked_db, monkeypatch):
        """A stock CNBC has no bars for stays stale, so the gate alone would
        ask again every five minutes."""
        self._bar_less_quote(monkeypatch)
        called = self._counting(monkeypatch)

        prices.refresh_stock("AAPL")
        prices.refresh_stock("AAPL")
        assert len(called) == 1

    def test_a_source_with_its_own_bars_never_asks(self, stocked_db, monkeypatch):
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: {
            "price": 338.19, "change": 0.0, "change_percent": 0.0,
            "source": "yahoo", "currency": "USD",
            "points": [{"ts": "2026-08-18T00:00:00Z", "close": 338.19}]})
        called = self._counting(monkeypatch)

        prices.refresh_stock("AAPL")
        assert not called

    def test_every_source_failing_still_returns_none(self, stocked_db, monkeypatch):
        """The fallback backfills a quote; it does not invent one."""
        for name in ("from_yahoo", "from_nasdaq", "from_cnbc"):
            monkeypatch.setattr(prices, name, lambda *a, **kw: None)
        self._counting(monkeypatch, prices.parse_cnbc_bars(CNBC_BARS, 1.0))
        assert prices.refresh_stock("AAPL") is None

    def test_get_quote_alone_never_fetches_history(self, monkeypatch):
        """It is a pure three-source chain; the DB-aware half lives elsewhere."""
        self._bar_less_quote(monkeypatch)
        called = self._counting(monkeypatch)

        assert prices.get_quote("AAPL")["source"] == "cnbc"
        assert not called

    # A 4:1 split on 2026-07-26. Unadjusted, the bar before it is on the old
    # scale and the bar after on the new one.
    UNADJUSTED = [
        {"ts": "2026-07-24T00:00:00Z", "close": 400.0, "high": 404.0,
         "volume": 10_000_000},
        {"ts": "2026-07-27T00:00:00Z", "close": 110.0, "high": 111.0,
         "volume": 40_000_000},
    ]
    SPLIT = {"ex_date": "2026-07-26", "numerator": 4.0, "denominator": 1.0}

    def test_bars_before_a_recorded_split_land_on_the_stored_scale(self, stocked_db,
                                                                    monkeypatch):
        """The stored row was already rescaled when Yahoo reported the split.
        Unadjusted bars written over it as-is would put it back at 400."""
        import db
        db.prices.record_split("AAPL", **self.SPLIT)
        db.prices.upsert_points("AAPL", [{"ts": "2026-07-24T00:00:00Z", "close": 100.0}])
        self._bar_less_quote(monkeypatch)
        self._counting(monkeypatch, self.UNADJUSTED)

        prices.refresh_stock("AAPL")

        stored = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["close"] for b in stored] == [100.0, 110.0]
        assert [b["high"] for b in stored] == [101.0, 111.0]
        assert [b["volume"] for b in stored] == [40_000_000, 40_000_000]

    def test_an_unrecorded_split_heals_when_yahoo_reports_it(self, stocked_db,
                                                             monkeypatch):
        """A split during the outage is a real cliff until Yahoo is back. Then
        the recording rescales the old-scale rows once, and the series is one
        scale. Adjusted bars would have been divided a second time here."""
        import db
        self._bar_less_quote(monkeypatch)
        self._counting(monkeypatch, self.UNADJUSTED)
        prices.refresh_stock("AAPL")

        prices._store("AAPL", {
            "price": 112.0, "change": 2.0, "change_percent": 1.8, "currency": "USD",
            "dividends": [], "splits": [self.SPLIT],
            "points": [{"ts": "2026-07-28T00:00:00Z", "close": 112.0}],
        })

        stored = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["close"] for b in stored] == [100.0, 110.0, 112.0]
        assert stored[0]["volume"] == 40_000_000

    def test_the_ex_date_bar_itself_is_not_rescaled(self, stocked_db, monkeypatch):
        """It traded on the new scale already — the same boundary
        `adjust_for_split` draws for stored rows."""
        import db
        db.prices.record_split("AAPL", **{**self.SPLIT, "ex_date": "2026-07-27"})
        self._bar_less_quote(monkeypatch)
        self._counting(monkeypatch, self.UNADJUSTED)

        prices.refresh_stock("AAPL")

        stored = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["close"] for b in stored] == [100.0, 110.0]
        assert [b["volume"] for b in stored] == [40_000_000, 40_000_000]

    def test_a_split_on_the_bar_less_quote_itself_still_rescales(self, stocked_db,
                                                                 monkeypatch):
        """Yahoo can answer with a price, a split and no bars — a halted line.
        The backfill reads the splits table, so that split has to be on record
        before it runs, not after: recorded late, the old-scale bars land after
        the one rescale that would ever have moved them."""
        import db
        monkeypatch.setattr(prices, "from_yahoo", lambda *a, **kw: {
            "price": 110.0, "change": 0.0, "change_percent": 0.0,
            "source": "yahoo", "currency": "USD", "points": [],
            "dividends": [], "splits": [self.SPLIT]})
        self._counting(monkeypatch, self.UNADJUSTED)

        prices.refresh_stock("AAPL")

        stored = db.prices.get_history("AAPL", "2026-07-01", interval="1d")
        assert [b["close"] for b in stored] == [100.0, 110.0]

    def test_the_next_utc_day_asks_again(self, stocked_db, monkeypatch):
        """The memo is per day, not per process: a stock CNBC had nothing for
        yesterday may have bars today."""
        from datetime import timedelta
        from normalize import now_utc
        self._bar_less_quote(monkeypatch)
        called = self._counting(monkeypatch)

        prices.refresh_stock("AAPL")
        monkeypatch.setattr(prices, "now_utc", lambda: now_utc() + timedelta(days=1))
        prices.refresh_stock("AAPL")
        assert len(called) == 2


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
