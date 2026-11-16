"""The scraper's rate limiting, backoff and failure isolation.

A fake clock and a MockTransport mean the adaptive behaviour is tested exactly,
with no real waiting and no real hosts.
"""
from __future__ import annotations

import httpx
import pytest

import settings
from services.http_client import (
    _DEFAULT_RATE,
    Scraper,
    _host_of,
    _Impersonator,
    _retry_after,
)


class FakeClock:
    """Monotonic time we control; `sleep` advances it instead of blocking."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def frozen_sleep(self, seconds: float) -> None:
        """Record the wait without advancing. Lets sequential calls stand in for
        concurrent waiters: every caller sees the same `now`, exactly as threads
        arriving together do."""
        self.slept.append(seconds)


def make_scraper(handler, clock=None, **kwargs) -> Scraper:
    clock = clock or FakeClock()
    scraper = Scraper(
        transport=httpx.MockTransport(handler),
        clock=clock, sleep=kwargs.pop("sleep", clock.sleep),
        max_retries=kwargs.pop("max_retries", 2),
        cache_ttl=kwargs.pop("cache_ttl", 300.0),
        **kwargs,
    )
    scraper._clock_obj = clock
    return scraper


def ok(_request):
    return httpx.Response(200, text="hello")


class TestBasics:
    def test_successful_fetch_returns_body(self):
        result = make_scraper(ok).get("https://example.com/a")
        assert result.ok and result.text == "hello"

    def test_404_is_returned_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(404, text="nope")

        result = make_scraper(handler).get("https://example.com/a")
        assert result.status == 404
        assert len(calls) == 1

    def test_host_extraction(self):
        assert _host_of("https://Query1.Finance.Yahoo.com/v8/x") == "query1.finance.yahoo.com"
        assert _host_of("not a url") == "unknown"


class TestCache:
    def test_repeat_request_is_served_from_cache(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, text="hello")

        scraper = make_scraper(handler)
        scraper.get("https://example.com/a")
        second = scraper.get("https://example.com/a")
        assert len(calls) == 1
        assert second.from_cache

    def test_cache_expires(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, text="hello")

        clock = FakeClock()
        scraper = make_scraper(handler, clock=clock, cache_ttl=60.0)
        scraper.get("https://example.com/a")
        clock.now += 61
        scraper.get("https://example.com/a")
        assert len(calls) == 2

    def test_different_params_are_different_cache_entries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, text="hello")

        scraper = make_scraper(handler)
        scraper.get("https://example.com/a", params={"q": "1"})
        scraper.get("https://example.com/a", params={"q": "2"})
        assert len(calls) == 2

    def test_cache_can_be_bypassed(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, text="hello")

        scraper = make_scraper(handler)
        scraper.get("https://example.com/a", cache=False)
        scraper.get("https://example.com/a", cache=False)
        assert len(calls) == 2


class TestConditionalGet:
    """A stale entry keeps its validator, so an unchanged feed costs a 304."""

    @staticmethod
    def _versioned(seen: list, *, etag="\"v1\"", modified=None):
        """200 with a validator first, then 304 for anyone who presents it."""

        def handler(request):
            seen.append(request.headers)
            if (etag and request.headers.get("if-none-match") == etag) or (
                modified and request.headers.get("if-modified-since") == modified
            ):
                return httpx.Response(304)
            headers = {}
            if etag:
                headers["ETag"] = etag
            if modified:
                headers["Last-Modified"] = modified
            return httpx.Response(200, text="feed body", headers=headers)

        return handler

    def test_stale_entry_revalidates_and_returns_the_held_body(self):
        seen = []
        clock = FakeClock()
        scraper = make_scraper(self._versioned(seen), clock=clock, cache_ttl=60.0)

        first = scraper.get("https://example.com/feed")
        clock.now += 61                       # body stale, validator is not
        second = scraper.get("https://example.com/feed")

        assert len(seen) == 2                 # it really did go back out
        assert seen[1].get("if-none-match") == "\"v1\""
        assert second.text == first.text == "feed body"
        assert second.from_cache and second.ok

    def test_last_modified_is_sent_when_there_is_no_etag(self):
        seen = []
        stamp = "Wed, 19 Aug 2026 10:00:00 GMT"
        clock = FakeClock()
        scraper = make_scraper(
            self._versioned(seen, etag=None, modified=stamp), clock=clock, cache_ttl=60.0
        )

        scraper.get("https://example.com/feed")
        clock.now += 61
        result = scraper.get("https://example.com/feed")

        assert seen[1].get("if-modified-since") == stamp
        assert result.text == "feed body"

    def test_304_never_overwrites_the_held_body(self):
        """The one way this could corrupt rather than merely miss."""
        clock = FakeClock()
        scraper = make_scraper(self._versioned([]), clock=clock, cache_ttl=60.0)

        scraper.get("https://example.com/feed")
        for _ in range(3):                    # three unchanged polls in a row
            clock.now += 61
            assert scraper.get("https://example.com/feed").text == "feed body"

    def test_304_makes_the_entry_fresh_again(self):
        seen = []
        clock = FakeClock()
        scraper = make_scraper(self._versioned(seen), clock=clock, cache_ttl=60.0)

        scraper.get("https://example.com/feed")
        clock.now += 61
        scraper.get("https://example.com/feed")     # 304, freshness pushed out
        scraper.get("https://example.com/feed")     # inside the new window

        assert len(seen) == 2                       # the third never left

    def test_a_changed_feed_still_returns_the_new_body(self):
        bodies = iter(["old", "new"])

        def handler(request):
            return httpx.Response(200, text=next(bodies), headers={"ETag": "\"v1\""})

        clock = FakeClock()
        scraper = make_scraper(handler, clock=clock, cache_ttl=60.0)

        assert scraper.get("https://example.com/feed").text == "old"
        clock.now += 61
        assert scraper.get("https://example.com/feed").text == "new"

    def test_first_request_carries_no_validators(self):
        seen = []
        scraper = make_scraper(self._versioned(seen), cache_ttl=60.0)
        scraper.get("https://example.com/feed")
        assert "if-none-match" not in seen[0]

    def test_cache_bypass_sends_no_validators(self):
        seen = []
        clock = FakeClock()
        scraper = make_scraper(self._versioned(seen), clock=clock, cache_ttl=60.0)

        scraper.get("https://example.com/feed")
        clock.now += 61
        scraper.get("https://example.com/feed", cache=False)

        assert "if-none-match" not in seen[1]

    def test_304_is_a_clean_answer_not_a_failure(self):
        """It must not look like an error to the breaker, nor stall the rate."""
        clock = FakeClock()
        scraper = make_scraper(self._versioned([]), clock=clock, cache_ttl=60.0)

        scraper.get("https://example.com/feed")
        for _ in range(9):
            clock.now += 61
            scraper.get("https://example.com/feed")

        host = next(s for s in scraper.stats() if s["host"] == "example.com")
        assert host["failures"] == 0 and host["throttled"] == 0
        assert not host["circuit_open"]
        # Nine clean 304s past _PROBE_AFTER: they count as headroom evidence.
        assert host["rate_per_sec"] > _DEFAULT_RATE[0]


class TestAdaptiveRate:
    def test_rate_is_probed_upward_while_responses_stay_clean(self):
        """The point of adapting: find the host's real ceiling rather than
        sitting on a guessed sleep forever."""
        scraper = make_scraper(ok)
        state = scraper._state("example.com")
        start = state.rate
        for i in range(40):
            scraper.get(f"https://example.com/{i}")
        assert state.rate > start
        assert state.rate <= state.max_rate

    def test_rate_never_exceeds_the_ceiling(self):
        scraper = make_scraper(ok)
        state = scraper._state("example.com")
        for i in range(300):
            scraper.get(f"https://example.com/{i}")
        assert state.rate == state.max_rate

    def test_a_429_halves_the_rate(self):
        scraper = make_scraper(lambda r: httpx.Response(429, text=""), max_retries=0)
        state = scraper._state("example.com")
        before = state.rate
        scraper.get("https://example.com/a")
        assert state.rate == pytest.approx(before * 0.5)
        assert state.throttled >= 1

    def test_repeated_throttling_compounds_the_backoff(self):
        """Each 429 halves again, so a host that keeps refusing gets backed off
        fast rather than after a fixed number of rounds."""
        scraper = make_scraper(lambda r: httpx.Response(429, text=""), max_retries=0)
        state = scraper._state("example.com")
        before = state.rate
        for i in range(3):
            scraper.get(f"https://example.com/{i}")
        # Three halvings would reach before/8, but the floor clamps it.
        assert state.rate <= before * 0.25
        assert state.rate >= 0.2

    def test_rate_has_a_floor(self):
        scraper = make_scraper(lambda r: httpx.Response(429, text=""), max_retries=0)
        state = scraper._state("example.com")
        for i in range(60):
            scraper.get(f"https://example.com/{i}")
        assert state.rate >= 0.2

    def test_throttling_rotates_the_fingerprint(self):
        """A host that dislikes this browser profile may accept another."""
        scraper = make_scraper(lambda r: httpx.Response(429, text=""), max_retries=0)
        state = scraper._state("example.com")
        before = state.fingerprint
        scraper.get("https://example.com/a")
        assert state.fingerprint != before

    def test_limiter_paces_sustained_requests(self):
        clock = FakeClock()
        scraper = make_scraper(ok, clock=clock)
        state = scraper._state("example.com")
        state.rate = state.max_rate = 2.0      # 2 per second, pinned so the
        for i in range(10):                    # probe can't confuse the pacing
            scraper.get(f"https://example.com/{i}")
        # One burst second is spendable up front (3 slots at 2/s: -1.0s, -0.5s,
        # now), then every request waits out its own 0.5s slot.
        assert clock.slept == [0.5] * 7
        assert clock.now == 1003.5

    def test_concurrent_waiters_reserve_distinct_slots(self):
        """The regression test for `nominal rate × worker count`.

        With a frozen clock no caller's wait is observed by the next, which is
        what threads arriving together do. The old bucket handed all of them the
        same wait and let them fire as one; distinct slots must stagger them.
        """
        clock = FakeClock()
        scraper = make_scraper(ok, clock=clock, sleep=clock.frozen_sleep)
        state = scraper._state("example.com")
        state.rate = 1.0
        for i in range(5):
            scraper.get(f"https://example.com/{i}")
        # Two go on the burst allowance, the rest queue a second apart. The old
        # code gave [1.0, 1.0, 1.0] — three waiters landing on the same instant.
        assert clock.slept == [1.0, 2.0, 3.0]

    def test_a_404_storm_does_not_probe_the_rate_upward(self):
        """`resolve(validate=True)` 404s by design, so miss-heavy traffic used to
        walk every host up to its ceiling."""
        clock = FakeClock()
        scraper = make_scraper(lambda r: httpx.Response(404, text="nope"),
                               clock=clock, sleep=clock.frozen_sleep)
        state = scraper._state("example.com")
        before = state.rate
        for i in range(40):
            scraper.get(f"https://example.com/{i}")
        assert state.rate == before
        assert state.blocked_until == 0.0, "a 404 is an answer, not a failure"

    def test_a_404_still_clears_a_failure_run(self):
        responses = [httpx.Response(500, text=""), httpx.Response(404, text="")]

        def handler(_request):
            return responses.pop(0) if responses else httpx.Response(404, text="")

        scraper = make_scraper(handler, max_retries=1)
        state = scraper._state("example.com")
        scraper.get("https://example.com/a")
        assert state.consecutive_fail == 0


class TestRetries:
    def test_a_500_is_retried_then_succeeds(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(200 if len(attempts) > 2 else 500, text="ok")

        result = make_scraper(handler, max_retries=3).get("https://example.com/a")
        assert result.ok and len(attempts) == 3

    def test_retries_are_bounded_and_then_give_up(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(503, text="")

        result = make_scraper(handler, max_retries=2).get("https://example.com/a")
        assert result is None
        assert len(attempts) == 3          # initial + 2 retries

    def test_transport_errors_are_retried(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, text="ok")

        result = make_scraper(handler, max_retries=2).get("https://example.com/a")
        assert result.ok and len(attempts) == 2

    def test_retry_after_is_obeyed_exactly(self):
        clock = FakeClock()

        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "7"}, text="")

        scraper = make_scraper(handler, clock=clock, max_retries=1)
        scraper.get("https://example.com/a")
        assert 7 in clock.slept

    def test_retry_after_is_capped_so_we_can_fail_over(self):
        response = httpx.Response(429, headers={"Retry-After": "99999"})
        assert _retry_after(response) == 120.0

    def test_absent_retry_after_is_none(self):
        assert _retry_after(httpx.Response(429)) is None

    def test_garbage_retry_after_is_none(self):
        assert _retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) is None


class TestCircuitBreaker:
    def test_circuit_opens_after_repeated_failures(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(500, text="")

        clock = FakeClock()
        scraper = make_scraper(handler, clock=clock, max_retries=0)
        for i in range(10):
            scraper.get(f"https://example.com/{i}")

        state = scraper._state("example.com")
        assert state.blocked_until > clock.now
        before = len(attempts)
        assert scraper.get("https://example.com/blocked") is None
        assert len(attempts) == before, "an open circuit must not issue requests"

    def test_circuit_closes_after_the_cooldown(self):
        responses = {"fail": True}

        def handler(request):
            if responses["fail"]:
                return httpx.Response(500, text="")
            return httpx.Response(200, text="back")

        clock = FakeClock()
        scraper = make_scraper(handler, clock=clock, max_retries=0)
        for i in range(6):
            scraper.get(f"https://example.com/{i}")

        responses["fail"] = False
        clock.now += 700          # past the maximum cooldown
        assert scraper.get("https://example.com/after").ok

    def test_one_dead_host_does_not_block_another(self):
        """Failure isolation: a dead source must not stall a whole refresh."""
        def handler(request):
            if request.url.host == "dead.example":
                return httpx.Response(500, text="")
            return httpx.Response(200, text="alive")

        scraper = make_scraper(handler, max_retries=0)
        for i in range(10):
            scraper.get(f"https://dead.example/{i}")

        assert scraper.get("https://alive.example/a").ok


class TestGetMany:
    def test_results_keep_input_order(self):
        def handler(request):
            return httpx.Response(200, text=request.url.path)

        scraper = make_scraper(handler)
        results = scraper.get_many([
            {"url": "https://example.com/one"},
            {"url": "https://example.com/two"},
            {"url": "https://example.com/three"},
        ])
        assert [r.text for r in results] == ["/one", "/two", "/three"]

    def test_one_failure_does_not_lose_the_others(self):
        def handler(request):
            if request.url.path == "/bad":
                return httpx.Response(500, text="")
            return httpx.Response(200, text="ok")

        scraper = make_scraper(handler, max_retries=0)
        results = scraper.get_many([
            {"url": "https://example.com/good"},
            {"url": "https://example.com/bad"},
            {"url": "https://example.com/good2"},
        ])
        assert results[0].ok and results[1] is None and results[2].ok

    def test_empty_input(self):
        assert make_scraper(ok).get_many([]) == []


class TestHeadersAndStats:
    def test_requests_carry_a_browser_fingerprint(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="ok")

        make_scraper(handler).get("https://example.com/a")
        assert "mozilla" in seen["user-agent"].lower()
        assert seen["accept-language"]

    def test_caller_headers_win(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="ok")

        make_scraper(handler).get("https://example.com/a", headers={"Accept": "application/json"})
        assert seen["accept"] == "application/json"

    def test_stats_report_per_host_activity(self):
        scraper = make_scraper(ok)
        scraper.get("https://example.com/a")
        stats = {s["host"]: s for s in scraper.stats()}
        assert stats["example.com"]["requests"] == 1
        assert stats["example.com"]["circuit_open"] is False


# ── The impersonating transport (3.4) ────────────────────────────────────

class FakeCurlResponse:
    """A curl_cffi response: `ok`, not httpx's `is_success`."""

    def __init__(self, status, text="", headers=None, url="https://example.com/a"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = url
        self.ok = 200 <= status < 300


class FakeCurlError(Exception):
    """Stands in for curl_cffi.requests.errors.RequestsError, which is not an
    httpx.HTTPError — the exact reason the except clause had to stop naming a
    library."""


class FakeCurlClient:
    """A curl_cffi-shaped client with no httpx anywhere in the call path."""

    def __init__(self, handler):
        self._handler = handler
        self.closed = False

    def get(self, url, params=None, headers=None, **kwargs):
        self.seen = {"url": url, "params": params, "headers": headers}
        return self._handler(url)

    def close(self):
        self.closed = True


def make_curl_scraper(handler, clock=None, **kwargs) -> Scraper:
    """The same Scraper, driven through a curl-shaped client."""
    clock = clock or FakeClock()
    scraper = Scraper(
        clock=clock, sleep=kwargs.pop("sleep", clock.sleep),
        max_retries=kwargs.pop("max_retries", 2),
        cache_ttl=kwargs.pop("cache_ttl", 300.0),
        **kwargs,
    )
    scraper._client.close()                 # drop the real httpx client
    scraper._client = FakeCurlClient(handler)
    scraper._errors = (FakeCurlError,)
    scraper._clock_obj = clock
    return scraper


@pytest.fixture(params=["httpx", "curl_cffi"])
def transport(request):
    """Builds a Scraper on either transport from one shared response plan.

    `plan` is a zero-argument callable returning `(status, text)`, or raising
    `build.transport_error` to simulate the connection dying. The point of the
    fixture is that every limiter assertion below is written once and asserted
    against both.
    """
    def build(plan, **kwargs):
        if request.param == "httpx":
            def handler(_request):
                status, text = plan()
                return httpx.Response(status, text=text)
            return make_scraper(handler, **kwargs)

        def handler(_url):
            status, text = plan()
            return FakeCurlResponse(status, text)
        return make_curl_scraper(handler, **kwargs)

    build.name = request.param
    build.transport_error = (httpx.ConnectError("boom") if request.param == "httpx"
                             else FakeCurlError("boom"))
    return build


class TestTransportSelection:
    """The flag picks the transport and nothing else. Every rate limit,
    breaker and cache line above it is shared code either way."""

    def test_the_default_is_httpx(self):
        scraper = Scraper()
        assert isinstance(scraper._client, httpx.Client)
        assert scraper._errors == (httpx.HTTPError,)
        scraper.close()

    def test_the_flag_selects_curl_cffi(self):
        scraper = Scraper(impersonate="chrome")
        assert isinstance(scraper._client, _Impersonator)
        assert httpx.HTTPError not in scraper._errors
        scraper.close()

    def test_the_setting_selects_it_too(self, monkeypatch):
        monkeypatch.setattr(settings, "SCRAPE_IMPERSONATE", "chrome")
        scraper = Scraper()
        assert isinstance(scraper._client, _Impersonator)
        scraper.close()

    def test_an_injected_transport_still_wins(self, monkeypatch):
        """An httpx MockTransport means nothing to libcurl, and it is how every
        other test in this file drives the class."""
        monkeypatch.setattr(settings, "SCRAPE_IMPERSONATE", "chrome")
        scraper = make_scraper(ok)
        assert isinstance(scraper._client, httpx.Client)
        assert scraper.get("https://example.com/a").text == "hello"
        scraper.close()

    def test_sessions_are_per_thread(self):
        """`Scraper` is shared across the price and news thread pools, and a
        libcurl handle is not safe to drive from two threads at once."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        client = _Impersonator("chrome", timeout=5.0)
        # A barrier, not just three tasks: a pool will happily serve all three
        # from one worker, and then the test passes whatever the code does.
        gate = threading.Barrier(3, timeout=10)

        def grab(_):
            gate.wait()
            return id(client._session())

        with ThreadPoolExecutor(max_workers=3) as pool:
            sessions = list(pool.map(grab, range(3)))

        assert len(set(sessions)) == 3
        assert id(client._session()) not in sessions      # and one for this thread
        client.close()

    def test_close_releases_every_session(self):
        client = _Impersonator("chrome", timeout=5.0)
        client._session()
        assert client._sessions
        client.close()
        assert client._sessions == []

    def test_a_missing_package_falls_back_rather_than_failing(self, monkeypatch, capsys):
        """The flag being set on a machine without the wheel must degrade to
        httpx, not take the whole scraper down."""
        monkeypatch.setattr(_Impersonator, "errors", staticmethod(
            lambda: (_ for _ in ()).throw(ImportError("no curl_cffi"))))

        scraper = Scraper(impersonate="chrome")

        assert isinstance(scraper._client, httpx.Client)
        assert "staying on httpx" in capsys.readouterr().out
        scraper.close()


class TestLimiterIsTransportAgnostic:
    """Both paths satisfy the same limiter tests — which is the whole claim
    3.4 makes: this swaps the transport and nothing else."""

    def test_a_successful_fetch_returns_the_body(self, transport):
        scraper = transport(lambda: (200, "hello"))
        assert scraper.get("https://example.com/a").text == "hello"

    def test_a_429_halves_the_rate(self, transport):
        scraper = transport(lambda: (429, ""), max_retries=0)
        state = scraper._state("example.com")
        before = state.rate

        scraper.get("https://example.com/a")

        assert state.rate == pytest.approx(before * 0.5)
        assert state.throttled >= 1

    def test_the_rate_is_probed_upward_on_clean_responses(self, transport):
        scraper = transport(lambda: (200, "hello"))
        state = scraper._state("example.com")
        before = state.rate

        for i in range(40):
            scraper.get(f"https://example.com/{i}")

        assert before < state.rate <= state.max_rate

    def test_retries_are_bounded_then_give_up(self, transport):
        attempts = []

        def plan():
            attempts.append(1)
            return 503, ""

        assert transport(plan, max_retries=2).get("https://example.com/a") is None
        assert len(attempts) == 3          # initial + 2 retries

    def test_a_transport_error_is_retried(self, transport):
        """The reason `except httpx.HTTPError` had to go: curl_cffi's error is
        not one, so the old clause would have let it escape the retry loop and
        crash a refresh."""
        attempts = []

        def plan():
            attempts.append(1)
            if len(attempts) < 3:
                raise transport.transport_error
            return 200, "recovered"

        result = transport(plan, max_retries=3).get("https://example.com/a")

        assert result.text == "recovered"
        assert len(attempts) == 3

    def test_a_transport_error_eventually_gives_up_quietly(self, transport):
        def plan():
            raise transport.transport_error

        assert transport(plan, max_retries=1).get("https://example.com/a") is None

    def test_the_cache_serves_the_second_call(self, transport):
        attempts = []

        def plan():
            attempts.append(1)
            return 200, "cached"

        scraper = transport(plan)
        first = scraper.get("https://example.com/a")
        second = scraper.get("https://example.com/a")

        assert first.text == second.text == "cached"
        assert len(attempts) == 1

    def test_the_circuit_opens_after_repeated_failures(self, transport):
        def plan():
            raise transport.transport_error

        scraper = transport(plan, max_retries=0)
        for i in range(12):
            scraper.get(f"https://example.com/{i}")

        assert scraper._state("example.com").blocked_until > scraper._clock()

    def test_stats_are_recorded_either_way(self, transport):
        scraper = transport(lambda: (200, "hello"))
        scraper.get("https://example.com/a")

        stats = scraper.stats()
        assert stats[0]["host"] == "example.com" and stats[0]["requests"] >= 1
