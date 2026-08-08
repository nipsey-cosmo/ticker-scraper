"""The /graphs series and the arithmetic behind them.

The charts on that page are only as trustworthy as these numbers, and most of
the ways they can be wrong are silent — a weekly mean that ignores how many
articles each day carried, a moving average that reports a partial window as a
full one, a "90-day return" measured across the 30 days of prices that actually
exist. Each of those gets a test here, because none of them looks wrong on a
chart.
"""
from __future__ import annotations

import pytest

import db
from normalize import days_ago_iso
from routes import graphs


def news_row(**overrides) -> dict:
    base = {
        "short_name": "AAPL",
        "title": "Apple beats estimates",
        "title_key": "apple beats estimates",
        "url": "https://reuters.com/markets/1",
        "url_hash": "hash-1",
        "source": "Reuters",
        "source_domain": "reuters.com",
        "source_type": "GOOGLE_NEWS",
        "publish_time": "2026-07-29T13:30:00Z",
        "relevance": "direct",
        "sentiment": 0.5,
    }
    base.update(overrides)
    return base


def scored(short_name: str, day: str, scores: list[float]) -> None:
    """One article per score, all on the same UTC day."""
    db.news.insert_news_many([
        news_row(short_name=short_name, publish_time=f"{day}T12:00:00Z",
                 url=f"https://x/{short_name}/{day}/{i}",
                 url_hash=f"{short_name}-{day}-{i}", sentiment=score)
        for i, score in enumerate(scores)
    ])


class TestBucketing:
    def test_days_are_their_own_bucket(self):
        assert graphs._bucket_start("2026-07-17", "day") == "2026-07-17"

    def test_weeks_start_on_monday(self):
        # 2026-07-17 is a Friday; 2026-07-19 the Sunday after it. Both belong
        # to the week that began Monday the 13th.
        assert graphs._bucket_start("2026-07-17", "week") == "2026-07-13"
        assert graphs._bucket_start("2026-07-19", "week") == "2026-07-13"
        assert graphs._bucket_start("2026-07-20", "week") == "2026-07-20"

    def test_months_start_on_the_first(self):
        assert graphs._bucket_start("2026-07-17", "month") == "2026-07-01"

    def test_grid_covers_every_bucket_including_empty_ones(self):
        grid = graphs._grid(30, "day")
        assert len(grid) == 30
        assert grid == sorted(grid)

    def test_timeframe_picks_a_readable_bucket(self):
        assert graphs._bucket_for(7) == "day"
        assert graphs._bucket_for(30) == "day"
        assert graphs._bucket_for(90) == "week"
        assert graphs._bucket_for(365) == "month"


class TestTrailingMean:
    def test_is_absent_until_the_window_is_full(self):
        """The reference dashboard averaged whatever it had, so its first
        '6-week average' point was a 1-week average carrying a 6-week label."""
        assert graphs._trailing_mean([1.0, 2.0, 3.0, 4.0], 3) == [None, None, 2.0, 3.0]

    def test_empty_buckets_are_skipped_not_counted(self):
        """A daily grid is mostly weekends. Counting them as part of the window
        makes a 6-day average that can never be computed, because no six
        consecutive calendar days are all trading days."""
        result = graphs._trailing_mean([1.0, None, 3.0, 4.0, 5.0], 3)
        assert result[:3] == [None, None, None]          # only two observations yet
        assert result[3] == pytest.approx((1 + 3 + 4) / 3)
        assert result[4] == pytest.approx((3 + 4 + 5) / 3)

    def test_a_gap_never_carries_a_value_of_its_own(self):
        assert graphs._trailing_mean([1.0, 2.0, 3.0, None, 4.0], 3)[3] is None

    def test_window_longer_than_the_series_is_all_none(self):
        assert graphs._trailing_mean([1.0, 2.0], 6) == [None, None]


class TestDrawdown:
    def test_measures_from_the_running_peak(self):
        assert graphs._drawdown([100.0, 120.0, 60.0]) == [0.0, 0.0, -50.0]

    def test_peak_survives_a_gap(self):
        """A bucket with no price must not reset the high-water mark — that
        would erase the drawdown rather than pause it."""
        result = graphs._drawdown([100.0, None, 50.0])
        assert result[0] == 0.0
        assert result[1] is None
        assert result[2] == -50.0

    def test_recovery_returns_to_zero_only_at_a_new_high(self):
        assert graphs._drawdown([100.0, 50.0, 100.0, 110.0]) == [0.0, -50.0, 0.0, 0.0]


class TestDivergenceInputs:
    def test_z_scores_centre_and_scale(self):
        assert graphs._z_scores([1.0, 2.0, 3.0]) == pytest.approx([-1.224744, 0.0, 1.224744],
                                                                  abs=1e-6)

    def test_identical_values_diverge_by_zero_not_by_infinity(self):
        """Every stock moving the same amount is zero spread, not undefined
        spread — dividing by it would put NaN into the chart."""
        assert graphs._z_scores([4.0, 4.0, 4.0]) == [0.0, 0.0, 0.0]

    def test_correlation_needs_three_points(self):
        assert graphs._correlation([1.0, 2.0], [1.0, 2.0]) is None
        assert graphs._correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_a_flat_series_has_no_correlation(self):
        assert graphs._correlation([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


class TestPeriodReturn:
    def test_reports_the_days_it_actually_spans(self):
        """Not the timeframe asked for: price history only reaches back as far
        as the backfill did, so a 90-day view routinely holds 30 days of prices
        and must not call the result a 90-day return."""
        value, first, last = graphs._period_return(
            {"2026-06-30": 100.0, "2026-07-31": 64.95})
        assert value == pytest.approx(-35.05, abs=0.01)
        assert (first, last) == ("2026-06-30", "2026-07-31")

    def test_a_single_observation_is_not_a_return(self):
        value, first, last = graphs._period_return({"2026-06-30": 100.0})
        assert value is None
        assert (first, last) == ("2026-06-30", "2026-06-30")

    def test_no_prices_at_all(self):
        assert graphs._period_return({}) == (None, None, None)


def day_bar(day: str, o: float, h: float, l: float, c: float) -> dict:
    return {"day": day, "open": o, "high": h, "low": l, "close": c}


class TestBars:
    def test_a_bucket_opens_on_its_first_day_and_closes_on_its_last(self):
        bars = graphs._bars([day_bar("2026-07-13", 10.0, 11.0, 9.0, 10.5),
                             day_bar("2026-07-17", 10.5, 12.0, 10.0, 12.0)],
                            ["2026-07-13"], "week")
        assert bars["open"] == [10.0]
        assert bars["close"] == [12.0]

    def test_range_comes_from_the_days_highs_and_lows_not_their_closes(self):
        """A week that spiked on Wednesday and gave it all back must keep the
        spike. Taking the range from closes alone flattens exactly the weeks
        worth looking at."""
        bars = graphs._bars([day_bar("2026-07-13", 10.0, 10.0, 10.0, 10.0),
                             day_bar("2026-07-15", 10.0, 30.0, 4.0, 10.0),
                             day_bar("2026-07-17", 10.0, 10.0, 10.0, 10.0)],
                            ["2026-07-13"], "week")
        assert bars["high"] == [30.0]
        assert bars["low"] == [4.0]

    def test_a_bucket_with_no_prices_is_null_not_zero(self):
        bars = graphs._bars([], ["2026-07-13"], "week")
        assert bars["close"] == [None]


class TestWeightedSentiment:
    def test_a_week_is_weighted_by_article_count(self):
        """The mean of the daily means would give a day with 3 articles the
        same say as a day with 200. It visibly moves the heatmap."""
        rows = [
            {"day": "2026-07-13", "avg_sentiment": 1.0, "articles": 1,
             "positive": 1, "negative": 0, "neutral": 0},
            {"day": "2026-07-14", "avg_sentiment": 0.0, "articles": 9,
             "positive": 0, "negative": 0, "neutral": 9},
        ]
        result = graphs._bucketed_sentiment(rows, ["2026-07-13"], "week")
        assert result["sentiment"] == [pytest.approx(0.1)]   # not 0.5
        assert result["articles"] == [10]

    def test_a_bucket_with_no_coverage_is_null_with_zero_articles(self):
        """Null, not 0.0 — no coverage is not neutral coverage, and a heatmap
        cell shaded neutral would claim a reading nobody made."""
        result = graphs._bucketed_sentiment([], ["2026-07-13"], "week")
        assert result["sentiment"] == [None]
        assert result["articles"] == [0]


class TestStockEndpoint:
    def test_every_series_is_aligned_to_the_bucket_grid(self, client):
        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()
        n = len(body["buckets"])
        for field in ("labels", "close", "maShort", "maLong", "drawdown", "sentiment",
                      "articles", "positive", "negative", "neutral", "samples"):
            assert len(body[field]) == n, f"{field} is not aligned to the grid"

        bars = body["bars"]
        for field in ("labels", "open", "high", "low", "close", "articles"):
            assert len(bars[field]) == len(bars["buckets"]), f"bars.{field} is not aligned"

    def test_candles_are_never_drawn_on_a_daily_grid(self, client):
        """A daily candle from this data has no range — one observation per day
        makes open, high, low and close the same number and every candle a
        flat dash."""
        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()
        assert body["bucket"] == "day"
        assert body["bars"]["bucket"] == "week"

    def test_candles_share_the_page_grid_once_it_is_coarse_enough(self, client):
        body = client.get("/graphs/stock/AAPL", params={"days": 90}).json()
        assert body["bucket"] == "week"
        assert body["bars"]["bucket"] == "week"
        assert body["bars"]["buckets"] == body["buckets"]

    def test_scores_land_in_the_right_bucket(self, client):
        day = days_ago_iso(3)[:10]
        scored("AAPL", day, [0.6, -0.2, 0.4])
        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()
        index = body["buckets"].index(day)

        assert body["samples"][index] == [0.6, -0.2, 0.4]
        assert body["articles"][index] == 3
        assert body["sentiment"][index] == pytest.approx((0.6 - 0.2 + 0.4) / 3)
        # 0.4 and 0.6 clear the +0.2 positive threshold; -0.2 is not below
        # -0.2, so it counts as neutral rather than negative.
        assert (body["positive"][index], body["neutral"][index],
                body["negative"][index]) == (2, 1, 0)

    def test_prices_become_close_based_bars(self, client):
        db.prices.upsert_points("AAPL", [
            (f"{days_ago_iso(4)[:10]}T00:00:00Z", 100.0),
            (f"{days_ago_iso(3)[:10]}T00:00:00Z", 120.0),
            (f"{days_ago_iso(2)[:10]}T00:00:00Z", 90.0),
        ])
        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()

        assert body["periodReturn"] == pytest.approx(-10.0)
        assert body["returnFrom"] == days_ago_iso(4)[:10]
        assert body["returnTo"] == days_ago_iso(2)[:10]
        assert body["maxDrawdown"] == pytest.approx(-25.0)

    def test_hourly_snapshots_are_what_give_a_day_its_range(self, client):
        """The daily bar alone would make every candle a flat dash. The
        snapshots are the only record of what the price did between one close
        and the next, so a day's range is taken across all of them."""
        day = days_ago_iso(3)[:10]
        db.prices.upsert_points("AAPL", [(f"{day}T00:00:00Z", 100.0)], interval="1d")
        db.prices.upsert_points("AAPL", [(f"{day}T12:00:00Z", 130.0),
                                         (f"{day}T18:00:00Z", 110.0)], interval="snap")

        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()
        bars = body["bars"]
        index = bars["buckets"].index(graphs._bucket_start(day, bars["bucket"]))
        assert bars["open"][index] == 100.0
        assert bars["high"][index] == 130.0
        assert bars["low"][index] == 100.0
        assert bars["close"][index] == 110.0

    def test_data_through_reports_where_the_data_stops(self, client):
        """The grid always runs to today. Without this the empty right-hand end
        of every chart reads as a bug rather than as a stale scraper."""
        scored("AAPL", days_ago_iso(5)[:10], [0.1])
        body = client.get("/graphs/stock/AAPL", params={"days": 30}).json()
        assert body["dataThrough"] == days_ago_iso(5)[:10]

    def test_a_stock_with_nothing_stored_still_answers(self, client):
        body = client.get("/graphs/stock/TSLA", params={"days": 30}).json()
        assert body["totalArticles"] == 0
        assert set(body["close"]) == {None}
        assert body["periodReturn"] is None
        assert body["dataThrough"] is None

    def test_unknown_symbol_is_404(self, client):
        assert client.get("/graphs/stock/ZZZZ").status_code == 404

    def test_timeframe_is_bounded(self, client):
        assert client.get("/graphs/stock/AAPL", params={"days": 400}).status_code == 422
        assert client.get("/graphs/stock/AAPL", params={"days": 1}).status_code == 422


class TestUniverseEndpoint:
    def test_covers_the_watchlist_and_nothing_else(self, client):
        """Coverage lingers for stocks that were followed once and dropped; a
        heatmap row for a stock the user no longer holds is noise."""
        scored("SHEL", days_ago_iso(3)[:10], [0.9])       # stored, not watched
        body = client.get("/graphs/universe", params={"days": 30}).json()
        assert {s["symbol"] for s in body["stocks"]} == {"AAPL", "6RJ0"}

    def test_divergence_is_relative_to_the_rest_of_the_universe(self, client):
        day_old, day_new = days_ago_iso(20)[:10], days_ago_iso(2)[:10]
        # AAPL: sentiment up, price down. 6RJ0: sentiment down, price up.
        scored("AAPL", day_old, [-0.5])
        scored("AAPL", day_new, [0.5])
        scored("6RJ0", day_old, [0.5])
        scored("6RJ0", day_new, [-0.5])
        db.prices.upsert_points("AAPL", [(f"{day_old}T00:00:00Z", 100.0),
                                         (f"{day_new}T00:00:00Z", 90.0)])
        db.prices.upsert_points("6RJ0", [(f"{day_old}T00:00:00Z", 100.0),
                                         (f"{day_new}T00:00:00Z", 110.0)])

        stocks = {s["symbol"]: s for s in
                  client.get("/graphs/universe", params={"days": 30}).json()["stocks"]}

        assert stocks["AAPL"]["divergence"] > 0     # narrative ran ahead of price
        assert stocks["6RJ0"]["divergence"] < 0     # price ran ahead of narrative

    def test_a_stock_missing_either_input_is_not_scored(self, client):
        """Rather than scored against a mean it did not contribute to."""
        scored("AAPL", days_ago_iso(3)[:10], [0.4])      # no prices for anyone
        body = client.get("/graphs/universe", params={"days": 30}).json()
        assert all(s["divergence"] is None for s in body["stocks"])

    def test_empty_watchlist_still_returns_a_grid(self, temp_db, client):
        db.watchlist.remove("AAPL")
        db.watchlist.remove("6RJ0")
        body = client.get("/graphs/universe", params={"days": 30}).json()
        assert body["stocks"] == []
        assert len(body["buckets"]) == 30
