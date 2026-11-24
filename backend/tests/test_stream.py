"""The live price stream (services/stream.py) and the poll that backs it up.

No socket is ever opened: `_session` gets a FakeSocket whose `recv` is a
script, and `run` gets an injected `connect`, `clock` and `sleep`. The clock is
the fake-clock pattern from test_http_client.py — `recv` timing out advances
it to the deadline the session asked for, so a five-second flush window
passes instantly.
"""
from __future__ import annotations

import base64
import json
import struct
import time

import pytest

import db
import jobs
from services import prices, stream, yahoo

# A post-market AAPL tick and a BTC tick cut off at field 47's tag, both
# captured from the live stream.
AAPL_POST_MARKET = "CgRBQVBMFa5nr0MYsJOIt71oKgNOTVMwCDgCRbQKoL5lAM2Mv9gBBPUCACn8v/0Cv/QOvw=="
BTC_TRUNCATED = (
    "CgdCVEMtVVNEFeYAfEcYwOSHt71oIgNVU0QqA0NDQzApOAFFPtuOv0iAgOzA/gFVjsR+R127lndH"
    "ZYAINsR9YDx9R7ABgIDswP4B2AEE4AGAgOzA/gHoAYCA7MD+AfIBA0JUQ4ECAAAAwIQuc0GJAgAA"
    "OMAq3nJC9QKACDbE/QI"
)
# Complete ticks captured 2026-11-23 ~20:18 UTC, US regular session open.
BTC_LIVE = (
    "CgdCVEMtVVNEFXEib0cYgKH9oMFoIgNVU0QqA0NDQzApOAFFIrwmP0iAwJm4xAJVRtl6R13Pz2tH"
    "ZQASxkN9MupuR7ABgMCZuMQC2AEE4AGAwJm4xALoAYDAmbjEAvIBA0JUQ4ECAAAAgDovc0GJAgAA"
    "3qAZ9nFC9QIAEsZD/QIivCY/"
)
AAPL_REGULAR = "CgRBQVBMFRROr0MYsJH9oMFoKgNOTVMwCDgBRfNoFkBIuMORHmXAzABBsAHIAdgBBPUCwMwAQf0C82gWQA=="
# Received 2026-11-23 20:38:59 UTC, four hours after the London close, and
# still flagged as the regular session. Its own timestamp says 16:35:30.
FTSE_REPLAYED = "CgVeRlRTRRVIwyNGGKDhmZTBaCIDR0JQKgNGR0kwCTgBRXNfpj9lAHsGQ9gBBPUCAHsGQ/0Cc1+mPw=="


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode(symbol: str, price: float, market_hours: int = 1,
           change: float = 1.5, change_percent: float = 0.5,
           age: float | None = 0.0) -> str:
    """A base64 tick in the stream's own encoding. market_hours=0 is omitted,
    exactly as proto3 would. `age` is how many seconds old the tick says it
    is; None leaves the time field out altogether."""
    name = symbol.encode()
    body = b"\x0a" + _varint(len(name)) + name + b"\x15" + struct.pack("<f", price)
    if age is not None:
        # sint64, zigzag: a non-negative n travels as 2n.
        body += b"\x18" + _varint(int((time.time() - age) * 1000) << 1)
    if market_hours:
        body += b"\x38" + _varint(market_hours)
    body += b"\x45" + struct.pack("<f", change_percent) + b"\x65" + struct.pack("<f", change)
    return base64.b64encode(body).decode()


def tick(symbol: str, price: float, **kw) -> str:
    return json.dumps({"type": "pricing", "message": encode(symbol, price, **kw)})


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


WAIT = object()   # script item: nothing arrives before the session's deadline


class FakeSocket:
    """Scripted `recv`, recorded `send`. A string is a message, WAIT times out
    (advancing the clock to the deadline), a callable runs between messages,
    an exception is raised. An exhausted script is the server hanging up."""

    def __init__(self, clock: FakeClock, script):
        self.clock, self.script, self.sent = clock, list(script), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self, timeout: float):
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                item()
                continue
            if item is WAIT:
                self.clock.now += timeout
                raise TimeoutError
            if isinstance(item, BaseException):
                raise item
            return item
        raise ConnectionError("server closed the connection")


@pytest.fixture
def streaming(stocked_db, monkeypatch):
    """stocked_db with both followed listings charted (divisor 1), and every
    stream write recorded on its way to the database."""
    monkeypatch.setattr(yahoo, "minor_units", {"AAPL": 1.0, "RKLB": 1.0})
    writes: list[list[dict]] = []
    real = db.stocks.bulk_set_quotes
    monkeypatch.setattr(db.stocks, "bulk_set_quotes",
                        lambda quotes: writes.append(quotes) or real(quotes))
    return writes


def session(*script) -> tuple[FakeSocket, int]:
    clock = FakeClock()
    ws = FakeSocket(clock, script)
    return ws, stream._session(ws, clock)


# ── Decoding ─────────────────────────────────────────────────────────────

class TestDecode:
    def test_post_market_sample_decodes(self):
        decoded = yahoo.parse_pricing(AAPL_POST_MARKET)
        assert decoded["symbol"] == "AAPL"
        assert decoded["price"] == 350.81
        assert decoded["market_hours"] == 2
        assert decoded["change"] == pytest.approx(-1.1, abs=1e-3)

    def test_truncated_payload_returns_none(self):
        assert yahoo.parse_pricing(BTC_TRUNCATED) is None
        # The capture lost its base64 padding too, which alone is fatal. Put
        # it back and the cut protobuf underneath must still be refused: the
        # last field has a tag and no value.
        padded = BTC_TRUNCATED + "=" * (-len(BTC_TRUNCATED) % 4)
        assert base64.b64decode(padded, validate=True)
        assert yahoo.parse_pricing(padded) is None

    @pytest.mark.parametrize("junk", [None, "", "not base64!", "CgRBQVBM", "/////w=="])
    def test_malformed_input_returns_none_rather_than_raising(self, junk):
        assert yahoo.parse_pricing(junk) is None

    def test_live_crypto_tick_decodes(self):
        decoded = yahoo.parse_pricing(BTC_LIVE)
        assert decoded["symbol"] == "BTC-USD"
        assert decoded["price"] == 61218.44
        assert decoded["market_hours"] == 1
        assert decoded["change"] == pytest.approx(396.14, abs=1e-2)
        assert decoded["time"] == 1795465128000

    def test_live_regular_session_equity_tick_decodes(self):
        decoded = yahoo.parse_pricing(AAPL_REGULAR)
        assert (decoded["symbol"], decoded["price"], decoded["market_hours"]) == (
            "AAPL", 350.61, 1)
        assert decoded["change_percent"] == pytest.approx(2.35, abs=1e-2)

    def test_absent_market_hours_is_not_regular(self, streaming):
        assert yahoo.parse_pricing(encode("AAPL", 400.0, market_hours=0))["market_hours"] == 0
        session(tick("AAPL", 400.0, market_hours=0), WAIT)
        assert db.stocks.get_stock("AAPL")["price"] == 338.19

    def test_chart_records_the_divisor_for_the_stream(self, monkeypatch):
        from tests.test_prices import chart_payload
        monkeypatch.setattr(yahoo, "minor_units", {})
        monkeypatch.setattr(yahoo, "_try_hosts",
                            lambda *a, **kw: chart_payload(currency="GBp"))
        yahoo.chart("SHEL.L")
        assert yahoo.minor_units == {"SHEL.L": 100.0}

    def test_spark_records_the_divisor_too(self, monkeypatch):
        """A new follow is named by spark within a minute; waiting for the
        hourly chart pass would keep it off the stream that long."""
        from tests.test_prices import spark_entry, spark_payload
        monkeypatch.setattr(yahoo, "minor_units", {})
        payload = spark_payload(spark_entry("SHEL.L", currency="GBp"),
                                spark_entry("AAPL"))
        monkeypatch.setattr(yahoo, "_try_hosts", lambda *a, **kw: payload)
        yahoo.spark(["SHEL.L", "AAPL"])
        assert yahoo.minor_units == {"SHEL.L": 100.0, "AAPL": 1.0}


# ── One session ──────────────────────────────────────────────────────────

class TestSession:
    def test_many_ticks_in_one_window_are_one_write_of_the_last_price(self, streaming):
        session(tick("AAPL", 340.0), tick("AAPL", 341.0), tick("AAPL", 342.5), WAIT, WAIT)

        # One write, and the quiet window after it writes nothing: re-flushing
        # an old tick would keep re-stamping it fresh and stop a silent stream
        # ever handing the stock back to the poll.
        assert len(streaming) == 1
        assert [(q["short_name"], q["price"]) for q in streaming[0]] == [("AAPL", 342.5)]
        stored = db.stocks.get_stock("AAPL")
        assert stored["price"] == 342.5
        # Each to its own column: the percent is a percent (0.5, not 0.005).
        assert (stored["price_change"], stored["price_change_percent"]) == (1.5, 0.5)
        assert stored["quote_currency"] == "USD"      # a None currency keeps it

    def test_a_post_market_tick_never_reaches_the_stock_row(self, streaming):
        before = db.stocks.get_stock("AAPL")
        session(tick("AAPL", 400.0, market_hours=2), WAIT)

        assert streaming == []
        after = db.stocks.get_stock("AAPL")
        assert (after["price"], after["price_updated_at"]) == (
            before["price"], before["price_updated_at"])

    def test_a_replayed_tick_from_a_shut_market_is_not_stored(self, streaming):
        # The captured replay, for a followed listing the stream may price.
        db.stocks.upsert_stock("UKX", "FTSE 100")
        db.stocks.set_resolution("UKX", "^FTSE", "FGI", "GBP")
        db.watchlist.add("UKX")
        yahoo.minor_units["^FTSE"] = 1.0
        assert yahoo.parse_pricing(FTSE_REPLAYED)["market_hours"] == 1

        session(json.dumps({"type": "pricing", "message": FTSE_REPLAYED}), WAIT)

        assert streaming == []
        assert db.stocks.get_stock("UKX")["price_updated_at"] is None

    @pytest.mark.parametrize("age, stored", [
        (5, True),
        (15 * 60 + 2, True),      # Tokyo: a delayed exchange, live at a distance
        (stream.MAX_TICK_AGE_SECONDS + 30, False),
        (None, False),            # no time field at all
    ])
    def test_only_a_tick_that_is_itself_recent_is_stored(self, streaming, age, stored):
        # Storing an old tick would stamp its price as updated now, and a
        # fresh stamp is exactly what keeps the poll away from the stock.
        before = db.stocks.get_stock("AAPL")
        session(tick("AAPL", 400.0, age=age), WAIT)

        after = db.stocks.get_stock("AAPL")
        assert len(streaming) == (1 if stored else 0)
        assert after["price"] == (400.0 if stored else before["price"])
        if not stored:
            assert after["price_updated_at"] == before["price_updated_at"]

    def test_tick_lands_on_the_short_name_not_the_yahoo_symbol(self, streaming):
        # A second short name for the same listing must move with it.
        db.stocks.upsert_stock("RKLB", "Rocket Lab")
        db.stocks.set_resolution("RKLB", "RKLB", "NMS", "USD")
        db.watchlist.add("RKLB")

        session(tick("RKLB", 21.5), WAIT)

        assert db.stocks.get_stock("6RJ0")["price"] == 21.5
        assert db.stocks.get_stock("RKLB")["price"] == 21.5

    def test_minor_unit_symbols_are_left_to_the_poll(self, streaming):
        db.stocks.set_resolution("SHEL", "SHEL.L", "LSE", "GBP")
        db.watchlist.add("SHEL")
        yahoo.minor_units["SHEL.L"] = 100.0

        ws, _ = session(tick("SHEL.L", 2650.0), WAIT)

        assert "SHEL.L" in ws.sent[0]["subscribe"]    # followed, just not stored
        assert streaming == []
        assert db.stocks.get_stock("SHEL")["price"] is None

    def test_unknown_divisor_is_skipped_not_guessed(self, streaming):
        del yahoo.minor_units["RKLB"]                 # never charted
        session(tick("RKLB", 21.5), tick("AAPL", 340.0), WAIT)

        assert db.stocks.get_stock("6RJ0")["price"] is None
        assert db.stocks.get_stock("AAPL")["price"] == 340.0

    def test_a_new_follow_is_subscribed_next_cycle(self, streaming):
        def change_watchlist():
            db.stocks.set_resolution("TSLA", "TSLA", "NMS", "USD")
            db.watchlist.add("TSLA")
            db.watchlist.remove("6RJ0")

        ws, _ = session(change_watchlist, WAIT, WAIT, WAIT)   # 5 s, 10 s, 15 s

        assert ws.sent == [
            {"subscribe": ["AAPL", "RKLB"]},
            {"subscribe": ["AAPL", "TSLA"]},
            {"unsubscribe": ["RKLB"]},
        ]

    def test_a_failed_flush_does_not_kill_the_loop(self, streaming, monkeypatch):
        real = db.stocks.bulk_set_quotes
        calls = []

        def flaky(quotes):
            calls.append(quotes)
            if len(calls) == 1:
                raise RuntimeError("database went away")
            return real(quotes)

        monkeypatch.setattr(db.stocks, "bulk_set_quotes", flaky)
        _, ticks = session(tick("AAPL", 340.0), WAIT, tick("RKLB", 21.5), WAIT)

        assert ticks == 2                             # still reading after the failure
        assert len(calls) == 2
        # The failed window's quote is kept and lands with the next one.
        assert db.stocks.get_stock("AAPL")["price"] == 340.0
        assert db.stocks.get_stock("6RJ0")["price"] == 21.5


# ── Reconnecting ─────────────────────────────────────────────────────────

class Stop(Exception):
    pass


def run_until(sleeps: int, connect) -> list[float]:
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == sleeps:
            raise Stop

    with pytest.raises(Stop):
        stream.run(connect=connect, clock=FakeClock(), sleep=sleep)
    return slept


def refuse():
    raise OSError("connection refused")


class TestReconnect:
    def test_reconnect_backs_off_and_caps(self):
        assert run_until(12, refuse) == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]

    def test_backoff_resets_after_ticks(self, streaming):
        attempts = iter([refuse, refuse, refuse,
                         lambda: FakeSocket(FakeClock(), [tick("AAPL", 340.0)]),
                         refuse])
        assert run_until(5, lambda: next(attempts)()) == [1, 2, 4, 1, 2]


# ── The poll behind it (jobs.refresh_prices) ─────────────────────────────

@pytest.fixture
def poll(stocked_db, monkeypatch):
    """Which stocks each refresh_prices call actually refreshed. The market is
    open and the hourly pass not due unless a test says otherwise."""
    refreshed: list[str] = []

    def fake_refresh(short_name, range_="5d"):
        refreshed.append(short_name)
        db.stocks.bulk_set_quotes([{"short_name": short_name, "price": 1.0}])
        return {"price": 1.0}

    monkeypatch.setattr(prices, "refresh_stock", fake_refresh)
    monkeypatch.setattr(prices, "ensure_classified", lambda stock: False)
    monkeypatch.setattr(jobs, "markets_open", lambda: True)
    monkeypatch.setattr(jobs, "_last_price_refresh", time.monotonic())
    return refreshed


def age_quotes(*short_names: str) -> None:
    for name in short_names:
        db.stocks.update_stock(name, price=1.0, price_updated_at="2020-01-01T00:00:00Z")


class TestPollGate:
    def test_fresh_quotes_skip_the_fast_poll(self, poll):
        # stocked_db just quoted AAPL, as a stream flush would; 6RJ0 has none.
        jobs.refresh_prices()
        assert poll == ["6RJ0"]

    def test_a_silent_stream_hands_every_stock_back_to_the_poll(self, poll):
        age_quotes("AAPL", "6RJ0")
        jobs.refresh_prices()
        assert sorted(poll) == ["6RJ0", "AAPL"]

    def test_the_full_sweep_still_runs_hourly_while_streaming(self, poll, monkeypatch):
        db.stocks.bulk_set_quotes([{"short_name": "6RJ0", "price": 21.5}])   # both fresh
        jobs.refresh_prices()
        assert poll == []

        monkeypatch.setattr(jobs, "_last_price_refresh", time.monotonic() - 3601)
        jobs.refresh_prices()
        assert sorted(poll) == ["6RJ0", "AAPL"]

        poll.clear()                                  # the pass re-stamped the hour
        jobs.refresh_prices()
        assert poll == []

    def test_closed_market_ticks_stay_hourly(self, poll, monkeypatch):
        monkeypatch.setattr(jobs, "markets_open", lambda: False)
        age_quotes("AAPL", "6RJ0")

        assert jobs.refresh_prices()["skipped"] == "markets closed"
        assert poll == []

        monkeypatch.setattr(jobs, "_last_price_refresh", float("-inf"))
        jobs.refresh_prices()
        assert sorted(poll) == ["6RJ0", "AAPL"]
