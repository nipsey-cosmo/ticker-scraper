"""Live quotes from Yahoo's WebSocket price stream.

One long-lived connection in the scheduler process delivers a tick roughly
every second per symbol during the session, where the poll in services/prices.py
makes one chart request per stock every five minutes. The stream sits above
that poll, not instead of it: it writes only the quote row (no snapshots, no
bars), and the poll skips any stock whose quote is under two minutes old. So a
dead or mute stream needs no detection of its own — its quotes age, and the
five-minute poll takes every stock back with no extra code.

A sync client on a daemon thread rather than asyncio, for the same reason
routes/ws.py chose one: no event loop to fight on Windows, and the scheduler
is a blocking process anyway.

No key, cookie or crumb: the server wants an Origin of finance.yahoo.com and a
browser User-Agent. `subscribe` and `unsubscribe` work incrementally on an open
socket. Replies are JSON whose `message` is one base64 protobuf tick, decoded
by yahoo.parse_pricing.

Ticks are coalesced: the last one per symbol wins, and the lot is written in
one `bulk_set_quotes` every FLUSH_SECONDS. That write fires the `stocks`
notify trigger, so open browser tabs re-fetch every FLUSH_SECONDS while a
session runs; it is the knob if that ever costs too much.

A tick carries its own timestamp, and one more than MAX_TICK_AGE_SECONDS old
is dropped. The server replays the last tick of a shut market at odd moments
with the regular-session flag still set, hours or days after the close;
stored, it would stamp an old price as updated now and keep the poll away.

There is no silence timer. Without a holiday calendar a closed market looks
exactly like a mute server, so stale-connection detection is the library's
ping, the re-subscribe every RESUBSCRIBE_SECONDS, and the database staleness
that hands a quiet stock back to the poll.

Assumed, not verified:
- pre-market ticks arrive with marketHours absent (proto3 omits a zero, and
  PRE_MARKET is 0) — never observed, but it is why only an explicit 1 counts;
- the 15 s re-subscribe is needed for a long-lived connection to keep
  delivering (a socket stayed up 70 s without one);
- stability over hours, and any cap on symbols per connection;
- every exchange streams. One that does not simply stays on the poll.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from websockets.sync.client import connect as _ws_connect

import db

from . import yahoo

URL = "wss://streamer.finance.yahoo.com/?version=2"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

FLUSH_SECONDS = 5
# Also how a follow made in the API process reaches this one: the watchlist
# is re-read from the database on the same beat.
RESUBSCRIBE_SECONDS = 15
MAX_BACKOFF_SECONDS = 300

# marketHours on a tick. 2 is post-market; pre-market is 0, so it never arrives.
_REGULAR = 1

# A tick older than this by its own timestamp is a replay, not a quote. The
# server re-sends the last tick of a shut market now and then, still flagged
# as the regular session: ^FTSE four hours after the London close, ^N225 three
# days after Tokyo's. Half an hour because a delayed exchange is live at a
# distance: Tokyo ticks arrive a steady 15 minutes old, the same delayed price
# the chart serves, and US ticks ran up to 47 s behind at the closing bell.
MAX_TICK_AGE_SECONDS = 30 * 60


def _connect():
    return _ws_connect(URL, additional_headers={"Origin": "https://finance.yahoo.com"},
                       user_agent_header=_USER_AGENT)


def run(connect=_connect, clock=time.monotonic, sleep=time.sleep) -> None:
    """Forever: connect, stream, and reconnect after 1, 2, 4 ... 300 s.

    A session that delivered any tick resets the backoff, so a server that
    drops healthy connections now and then is rejoined within a second, while
    one that refuses outright is asked at most every five minutes.
    """
    delay = 1
    while True:
        ticks = 0
        try:
            with connect() as ws:
                ticks = _session(ws, clock)
        except Exception as exc:                   # connecting or closing failed
            print(f"[stream] connection failed: {type(exc).__name__}: {exc}")
        if ticks:
            delay = 1
        print(f"[stream] reconnecting in {delay}s")
        sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF_SECONDS)


def _session(ws, clock) -> int:
    """Stream until the connection ends, however it ends. Returns the number
    of ticks received, which is all `run` needs to judge the session."""
    ticks = 0
    names: dict[str, list[str]] = {}          # Yahoo symbol -> short names
    pending: dict[str, dict] = {}             # short name -> latest quote
    flush_at = clock() + FLUSH_SECONDS
    subscribe_at = clock()
    try:
        while True:
            now = clock()
            if now >= subscribe_at:
                names = _resubscribe(ws, names)
                subscribe_at = now + RESUBSCRIBE_SECONDS
            if now >= flush_at:
                _flush(pending)
                flush_at = now + FLUSH_SECONDS

            try:
                raw = ws.recv(timeout=max(0.0, min(flush_at, subscribe_at) - now))
                tick = yahoo.parse_pricing(json.loads(raw).get("message"))
            except TimeoutError:
                continue
            except (ValueError, AttributeError):  # not JSON, or not an object
                continue
            if not tick:
                continue
            ticks += 1

            # Regular session only, as prices._nasdaq_session does for the
            # poll: an extended-hours print is not the day's price.
            if tick["market_hours"] != _REGULAR:
                continue
            # The tick's own clock, not the moment it arrived. The write stamps
            # the row as updated now, and that stamp is all that hands a quiet
            # stock back to the poll, so a replayed price must never earn it.
            # A tick with no time at all reads as 1970 and is dropped too.
            if time.time() * 1000 - tick["time"] > MAX_TICK_AGE_SECONDS * 1000:
                continue
            # No divisor means the symbol has not been charted since start-up.
            # Never guess one: a wrong guess is a 100x error on a stored price.
            #
            # ponytail: minor-unit lines (divisor 100, e.g. SHEL.L) stay on the
            # poll too. Whether the stream quotes them in pence or pounds is
            # unverified — London was shut at every probe. Upgrade path: probe
            # SHEL.L after 08:00 UTC, compare against chart regularMarketPrice,
            # then divide here and lift this guard.
            if yahoo.minor_units.get(tick["symbol"]) != 1.0:
                continue

            for short_name in names.get(tick["symbol"], ()):
                pending[short_name] = {
                    "short_name": short_name,
                    "price": tick["price"],
                    "change": tick["change"],
                    "change_percent": tick["change_percent"],
                    # The stream carries no currency for equities. None keeps
                    # the stored one: bulk_set_quotes COALESCEs it.
                    "currency": None,
                }
    except Exception as exc:
        print(f"[stream] connection ended after {ticks} ticks: "
              f"{type(exc).__name__}: {exc}")
    return ticks


def _resubscribe(ws, names: dict[str, list[str]]) -> dict[str, list[str]]:
    """Subscribe to every followed stock and drop the unfollowed ones.

    The full list every time rather than the difference: the server takes a
    repeat without complaint, and it is the assumed keep-alive.
    """
    followed: dict[str, list[str]] = defaultdict(list)
    for row in db.watchlist.get_watchlist():
        # Several short names can share one listing (6RJ0 and RKLB are both
        # Rocket Lab), and a tick for RKLB must update every one of them.
        if row.get("yahoo_symbol"):
            followed[row["yahoo_symbol"]].append(row["short_name"])

    if followed.keys() != names.keys():
        print(f"[stream] following {len(followed)} symbols")
    if followed:
        ws.send(json.dumps({"subscribe": sorted(followed)}))
    if gone := sorted(names.keys() - followed.keys()):
        ws.send(json.dumps({"unsubscribe": gone}))
    return dict(followed)


def _flush(pending: dict[str, dict]) -> None:
    """One write for the whole window. A failure keeps the quotes for the next
    attempt — last tick wins, so the backlog is one row per stock at most."""
    if not pending:
        return
    try:
        db.stocks.bulk_set_quotes(list(pending.values()))
        pending.clear()
    except Exception as exc:
        print(f"[stream] flush failed, retrying next window: {type(exc).__name__}: {exc}")
