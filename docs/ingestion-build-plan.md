# Ingestion build plan

Working state for `/build-parts`. **This file is the memory** — context is
cleared between parts, so anything a future session needs to execute a
sub-point has to be written down here, not remembered.

Derived from the ingestion gap analysis (audit of `0c8bdf1`). Gap ids (`A1`,
`C3`, …) refer to that report.

## Rules

- **One part per invocation.** Finish the part, green-gate it, stop. The user
  runs `/clear` then `/build-parts` for the next one.
- **One sub-point, one commit.** Tick its box in this file *in the same
  commit*, so a lost session can tell exactly where it got to.
- **Branches stack.** Part 2 branches off Part 1's tip, Part 3 off Part 2's.
  Nothing is merged to `main` — the user reviews and merges the stack.
- **Every commit leaves a test** that fails if the change regresses. No
  exceptions on the scheduling and price-storage paths.
- **Ports 3000 and 5000 are somebody else's.** Never kill them. This workflow
  uses **5100** (backend) and **3100** (frontend), and kills only PIDs it
  started itself.

## Progress

| Part | Branch | Base | Commits | State |
|---|---|---|---|---|
| 1 · Stop leaving data on the table | `feat/tier-1-reclaim` | `main` | 5 / 5 | **done, green, unmerged** |
| 2 · New capability | `feat/tier-2-capability` | `feat/tier-1-reclaim` | 0 / 6 | ready |
| 3 · Justified by evidence | `feat/tier-3-evidence` | `feat/tier-2-capability` | 0 / 6 | blocked on 2 |

Suite at the end of Part 1: **670 backend** (from 609), **128 frontend**,
`eslint` clean. Live smoke on 5100/3100 passed — all seven news sources
returned articles, real OHLCV stored, no console errors on `/` or `/graphs`.

---

# Part 1 — Stop leaving data on the table

`feat/tier-1-reclaim`, from `main`. ~150 lines. No new sources, no new failure
modes. Two of these five just stop discarding data already in hand.

### 1.1 — A1 · Conditional GET

- [x] `feat: send conditional GET so an unchanged feed costs a 304`

**Done.** One dict, not two: the entry is `(fresh_until, Fetched)` and is kept
past `fresh_until` rather than deleted, so the body goes stale while its
validator does not. That satisfies "validators outlive `cache_ttl`" with one
lookup method instead of two parallel stores — and it means a 304 always has a
body to hand back, which two stores would not have guaranteed. Cap raised
512 → 1024 because entries now live longer.

**Files** `backend/services/http_client.py`, `backend/tests/test_http_client.py`

**Do** Keep `{cache_key: (etag, last_modified)}` in its own dict beside
`_cache`. Send `If-None-Match` / `If-Modified-Since` on `get()` when known.
Treat a **304** as a cache hit: return the stored `Fetched` with
`from_cache=True`, and score it as a clean answer for the limiter.

**Watch**
- A 304 carries no body — never overwrite the cached entry from it, only push
  its expiry out.
- Validators must outlive `cache_ttl`, so they cannot live inside the `_cache`
  tuple: that is the whole point, the body expires and the validator doesn't.
- 304 is not in `_RETRY_STATUSES` and must not be added — it is success.
- `_on_success(probe=...)`: a 304 *is* headroom evidence, so `probe=True`.

**Test** Transport returns `200 + ETag`, then `304`. Assert the second `get()`
returns the first body, and that the request carried `If-None-Match`.

### 1.2 — C1 · Store OHLC and volume

- [x] `feat: store open, high, low and volume, not just the close`

**Done.** `_ALTERS` now exists in `db/connection.py` as the home for every
post-ship column (the lone `news.impact` ALTER moved into it), so 1.3 and 3.1
get their schema changes for one line each. `parse_chart` emits dicts rather
than `(ts, close)` tuples; `upsert_points` accepts **both** shapes, because 20
existing call sites genuinely only have a close and making them all build a
dict would be noise. The update path `COALESCE`s OHLC so a snapshot landing on
a bar's slot corrects the close without blanking the rest.

**Files** `backend/services/yahoo.py`, `backend/db/prices.py`,
`backend/db/connection.py`, `backend/services/prices.py`,
`backend/tests/test_prices.py`

**Do** `parse_chart` already holds `indicators.quote[0]`; take `open`, `high`,
`low`, `volume` out of the same dict at `yahoo.py:101-102`. Widen `points` to
carry them, widen `prices` with four nullable columns, widen
`db.prices.upsert_points`.

**Watch**
- **There is no migration framework.** Add an idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block next to the `CREATE TABLE`
  statements in `db/connection.py`, run under the same advisory lock. ~5 lines,
  and every later column change is then free. Do this first, in this commit.
- Prices divide by `divisor` for minor units (GBp, ZAC, ILA) — OHLC must divide
  too, **volume must not**.
- Yahoo nulls a whole bar on a market holiday: any of the four can be `None`
  independently. Keep the existing `close is None` skip as the gate, store the
  rest as nullable.
- `from_nasdaq` and `from_cnbc` return `"points": []` and stay that way.

**Test** Parse a captured chart payload with a holiday gap; assert OHLCV lands,
that a pence-quoted symbol divides OHLC and not volume, and that a null bar is
skipped rather than stored as zero.

### 1.3 — C2 · Splits and dividends

- [x] `feat: read the splits and dividends the chart call already asks for`

**Done.** Idempotency is the `splits` table's primary key, not application
logic: `record_split` returns True only on first sight and the adjustment is
gated on it, so the database answers "have we applied this?" rather than the
code guessing.

The subtle part turned out to be **ordering**, not the arithmetic. Yahoo
returns the requested window already split-adjusted, so incoming bars are on
the new scale while stored ones are not. `_store` therefore adjusts *before*
inserting: adjust-then-insert moves only the stale rows, insert-then-adjust
would divide the fresh bars a second time. `adjust_for_split` uses `ts <
ex_date` so the ex-date bar itself, which already arrived rescaled, is left
alone.

**Files** `backend/services/yahoo.py`, `backend/db/connection.py`,
`backend/db/prices.py`, `backend/tests/test_prices.py`

**Do** `yahoo.py:79` already sends `events=div,split`; `parse_chart` never
reads `node["events"]`. Parse it. Store dividends in their own table
(`short_name`, `ex_date`, `amount`, `currency`). Use split ratios to
back-adjust stored history on ingest so the series stays continuous.

**Watch**
- A split arriving mid-refresh must adjust **rows already stored**, not just
  incoming ones — otherwise the chart shows a cliff.
- Adjustment must be idempotent. Record the split so a second refresh that sees
  the same event does not halve the history twice. This is the one genuinely
  fiddly thing in Part 1.
- Dividend amounts are in the same minor units as the quote — divide.

**Test** A payload with a 2:1 split back-adjusts prior points once; running the
same payload through twice leaves the series unchanged.

### 1.4 — C3 · Stooq as history fallback

- [x] `feat: fall back to Stooq for history when Yahoo has none`

**Done, but not where the plan said.** Putting the backfill in `get_quote`
worked and was wrong twice over: it made a pure function hit the network, and
because the price job ticks every five minutes while a market is open, one
Yahoo outage would have meant a full CSV download **per stock per tick** for a
file that changes once a day. It moved to `refresh_stock`, gated on the stored
daily series having no bar within `_HISTORY_GRACE_DAYS` (4, so Friday's close
still counts on Monday).

The full suite caught it and the isolated file did not — three older
`TestFallbackChain` tests build quotes with no `points` key, so they silently
started making live requests to stooq.com. Worth remembering: running only the
file you touched can hide a change that widens what other tests reach for.

Suffix map is deliberately narrow (`us`, `uk`, `de`, `jp`, `pl`, `hu`). German
regional venues are left unmapped on purpose — `.MU` onto `.de` is the same
company at a different venue, and a fallback that quietly swaps venues is
worse than one that declines.

**Files** `backend/services/stooq.py` (new), `backend/services/prices.py`,
`backend/tests/test_prices.py`

**Do** Free, no key, plain CSV: `https://stooq.com/q/d/l/?s=aapl.us&i=d` →
`Date,Open,High,Low,Close,Volume`. Add it to the chain **for history only** —
`get_quote` keeps its current order, but when the winning source returns no
`points`, fill them from Stooq.

**Watch**
- The work is symbol mapping, not the fetch. Yahoo suffix → Stooq suffix:
  no suffix → `.us`, `.L` → `.uk`, `.DE` → `.de`, `.PA` → `.fr`, `.TO` → `.ca`,
  and so on. Unmapped suffix returns `None` rather than guessing — a wrong
  symbol silently returns *another company's* history.
- Add `stooq.com` to `_HOST_RATES` in `http_client.py`, conservatively.
- Stooq serves an HTTP 200 with the body `No data` for unknown symbols. Check
  the body, not the status.
- Stooq is end-of-day only. It backfills history; it never touches the quote.

**Test** Parse a captured CSV; assert an unmapped suffix returns `None`, that
`No data` yields no points, and that `refresh_stock` stores Stooq bars when the
quote source returned none.

### 1.5 — E1 · Per-source yield metrics

- [x] `feat: report articles per source so a dead feed stops looking like quiet news`

**Done.** Counts live in a `source_yield` table, not process memory, because
the refreshes run in the **scheduler** process and `/health` is served by the
**API** — an in-memory tally would be written by one and read by the other.
`stalled` is computed over the 7-day window rather than the last attempt, so a
source finding nothing for one quiet stock is not an alarm.

The skip accounting matters more than expected: EDGAR declines itself inside
`fetch()` when no contact address is set, which reads externally as "answered
with nothing" — the exact signature of a dead scraper. `collect` now checks
`sec_enabled()` before queuing it and records the skip explicitly.

**Files** `backend/services/news/__init__.py`, `backend/routes/system.py`,
`backend/tests/test_api.py`

**Do** `stats()` counts requests, throttles and failures — all HTTP-level. It
cannot show Bing returning **zero articles** across forty stocks while
answering 200 throughout. Count articles per `source_type` per refresh, keep a
rolling 7-day mean, expose it on `/health` beside the host stats, and flag any
source at zero for a whole cycle.

**Watch**
- Count in `collect()`, where the per-source lists still exist — after the
  merge in `refresh()` the attribution is gone.
- A source skipped by design is not a source at zero: `is_us_listing()` gates
  three of seven, and EDGAR is off unless `STOCKY_SEC_CONTACT` is set. Report
  those as *skipped*, never as *failed*, or the flag cries wolf on every
  non-US stock.

**Test** A refresh where one source returns `[]` flags that source and no
other; a US-only source skipped for a London listing is reported skipped.

---

# Part 2 — New capability

`feat/tier-2-capability`, from Part 1's tip. Each sub-point is a feature the UI
can show. Needs 1.1 (conditional GET) in place before 2.1's fast cadence is
polite.

### 2.1 — B1 + A3 · EDGAR on data.sec.gov, and a fast lane

- [x] `feat: real-time filings from data.sec.gov on a two-minute lane`

**Done.** The lane is a filter, not a second pipeline: `collect(only=...)`
drops the tasks outside the lane and `refresh`/`refresh_watchlist` thread it
through, so the fast lane is the same scrape-dedupe-store path with a smaller
source list. A source outside the lane is **not** recorded as skipped - only a
source that was genuinely unavailable is, or 1.5's `stalled` flag would drown
in thirty skips every two minutes.

`published_at` now comes from `acceptanceDateTime`, not `filingDate`. A date
alone gives every filing that day the same timestamp, which makes a
two-minute lane pointless - that, not the ten-minute lag, was the real reason
the Atom feed could not be polled fast.

`primaryDocDescription` is usually the form again (`"8-K"`), which would have
titled as "Apple Inc. filed 8-K: 8-K", so it is dropped when it only repeats
the form. Both SEC hosts were cut to a ceiling of 4.0 each: the stated 10 req/s
is across `sec.gov` **and** `data.sec.gov` combined, and `www.sec.gov` alone
was already at 8.0.

Fast-lane logging is gated - a per-stock "found nothing" line every two
minutes buries the hourly lane's output in a live run.

**Files** `backend/services/news/sec_edgar.py`, `backend/scheduler.py`,
`backend/jobs.py`, `backend/settings.py`, `backend/tests/test_news_sources.py`

**Do** Replace the legacy `cgi-bin/browse-edgar` Atom call (`sec_edgar.py:34`,
a 10-minute feed polled hourly) with `data.sec.gov`, which the SEC documents as
updating in real time with a sub-second processing delay. Fetch
`sec.gov/files/company_tickers.json` **once** for the ticker→CIK map, cache it,
then `data.sec.gov/submissions/CIK##########.json` per stock. Split the news
scheduler into a fast lane (EDGAR + wires, 2 min) and the existing hourly lane.

**Watch**
- Keep `_MATERIAL_FORMS` and the constructed titles exactly as they are — both
  are load-bearing and both are already right.
- Keep the contact-email User-Agent and the opt-in on `STOCKY_SEC_CONTACT`.
  The SEC enforces this; a repo URL gets a flat 403.
- CIK is zero-padded to ten digits in the URL and unpadded in the payload.
- Ceiling stays under SEC's stated 10 req/s across `sec.gov` **and**
  `data.sec.gov` combined — add the new host to `_HOST_RATES`.
- The fast lane must not run the LLM event pass on every tick. Judge on the
  hourly lane; the fast lane stores.

**Test** Parse a captured submissions payload: material forms survive, 3/4/5
and 144 are dropped, titles name the company. Assert the fast lane does not
call event detection.

### 2.2 — B2 · Newswire ingestion

- [x] `feat: read the newswires, where company announcements land first`

**Done, three feeds not four.** ACCESS Newswire's RSS returns a flat 403 to
both curl and the app's scraper - a WAF at the handshake, which is exactly
what **3.4** exists to fix. Add it there; it is the smallest of the four.
GlobeNewswire 403s a plain curl but answers the scraper's rotated fingerprint
fine, so it stayed.

**Half of Business Wire's home feed is translations.** A live pull was 19
English items of 39; the rest were the same releases in Arabic, Japanese,
French and six other languages. They carry the ticker so relevance keeps them,
and dedup compares token overlap, which cannot match Arabic to English - a
holder would have seen one announcement five times. Both wires that translate
put the language in the URL path, so `_is_english` catches it there. This was
not in the audit and would have been a visible bug on day one.

The single fetch is a module-level cache on `wires.fetch()` with a TTL of
`NEWS_FAST_MINUTES`, so a sweep pulls once and the whole watchlist reads it.
`for_stock` narrows the firehose with `dedup.is_relevant` - the same check
every other source is held to - rather than passing a few hundred items per
stock to `prepare`, which would have made 1.5's per-source yield meaningless.

Business Wire attaches the release image as an RSS enclosure, so wire items
land with an image and skip the Open Graph fetch entirely.

`yahoo_news._clean_summary` is now `clean_summary`: PR Newswire bodies are
HTML and it already did exactly the right thing.

Two old tests started making live requests the moment a non-US-gated source
appeared - the 1.4 lesson again. `_quiet` now stubs `wires.for_stock`.

**Files** `backend/services/news/wires.py` (new),
`backend/services/news/__init__.py`, `backend/tests/test_news_parsers.py`

**Do** Company press releases are the primary text for contracts, launches,
trial results and guidance — ahead of any write-up, and often the only English
source for a non-US listing. GlobeNewswire, PR Newswire, Business Wire and
ACCESSWIRE all publish free RSS. Ingest the firehose and match against the
watchlist.

**Watch**
- These feeds are **not per-ticker**. Fetch once per refresh for *all* stocks,
  not once per stock — the naïve loop is 20× the requests for identical bytes.
- Matching is the existing relevance machinery's job, not new code. Feed wire
  items through `dedup.prepare` with the same aliases every other source uses.
- Wires are `direct`-only: an unnamed wire item is not sector context, it is
  another company's announcement. Keep them out of `_RELATED_ALLOWED`.
- Source rank: a company's own release is primary, so rank it high — it should
  win dedup tie-breaks against a rewrite.

**Test** Parse a captured feed; assert an item naming a watchlist company is
kept and one naming nothing is dropped, and that one fetch covers N stocks.

### 2.3 — D1 · Article body extraction

- [ ] `feat: extract article text so sentiment sees more than a headline`

**Files** `backend/services/news/extract.py` (new),
`backend/services/news/__init__.py`, `backend/db/connection.py`,
`backend/services/sentiment_service.py`, `backend/services/events.py`,
`pyproject.toml`, `backend/tests/test_news_parsers.py`

**Do** Nothing in the pipeline ever reads an article. Add `trafilatura`
(~0.95 F1, ~44ms/page) and extract on the **dedup winner only**, gated exactly
like `images.py` already gates its Open Graph top-up. Store as `body`; feed it
to the sentiment scorer and the event prompt.

**Watch**
- Winner-only and capped, or this becomes hundreds of fetches per cycle. It
  should be tens.
- Google items carry an opaque `CBMi…` redirect and have no extractable page —
  skip them here. E3 covers decoding, and only if this lands first.
- The event prompt is already near its token budget at 60 articles; bodies must
  be truncated hard before they reach it, and `_BATCH_SIZE` reconsidered.
- Extraction failing must never cost the article its row. Best-effort, after
  storage, same as images.

**Test** Extract from saved HTML; assert a failed extraction leaves the stored
row intact and that a Google-redirect URL is skipped without a fetch.

### 2.4 — B5 · Earnings calendar

- [ ] `feat: track earnings dates, the one market-moving date you can see coming`

**Files** `backend/services/calendar.py` (new), `backend/db/connection.py`,
`backend/jobs.py`, `backend/scheduler.py`, `backend/routes/stocks.py`,
`backend/tests/test_api.py`

**Do** Nasdaq's calendar endpoint is keyless and on a host the scraper already
talks to: `api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD`. One daily
job, forward a couple of weeks. Expose the next date per stock.

**Watch**
- US-only, like everything else on that host. A London listing gets no date;
  that is a null, not a failure.
- Store the date and the before/after-market marker — "reports Thursday" is
  useless without knowing which side of the bell.
- Feed the next date into the event prompt as context. "Reports in 3 days" is
  something the LLM currently has to infer from headlines.

**Test** Parse a captured response; assert a non-US stock stores nothing and
that a stale past date is not served as upcoming.

### 2.5 — B4 · Retail mention velocity

- [ ] `feat: track retail mention velocity as its own signal`

**Files** `backend/services/social.py` (new), `backend/db/connection.py`,
`backend/jobs.py`, `backend/routes/stocks.py`, `backend/tests/test_api.py`

**Do** One keyless call returns the whole ranked table:
`apewisdom.io/api/v1.0/filter/all-stocks` → rank, ticker, mentions, upvotes,
`rank_24h_ago`, `mentions_24h_ago`. Join it to the watchlist rather than
querying per stock.

**Watch**
- **Store this as a separate signal. Never mix it into article sentiment.**
  They measure different things and averaging them destroys both.
- The interesting number is the *delta*, not the level — a stock is always
  mentioned somewhat. Store the 24h change.
- Tickers are unsuffixed US symbols, so match on `short_name.split(".")[0]`
  and accept that non-US listings mostly miss.

**Test** A captured table joins to a watchlist, non-matching tickers are
ignored, and the stored delta is the change and not the level.

### 2.6 — E2 · Live smoke test

- [ ] `test: add an opt-in live smoke test for every source parser`

**Files** `backend/tests/test_live_sources.py` (new),
`backend/pyproject.toml`, `backend/tests/conftest.py`

**Do** Fixture tests pass forever after the live page changes. Finviz is an
HTML scrape of a commercial site and is the most likely silent breakage in the
repo. Add one opt-in test that fetches each source live for a known-liquid
ticker and asserts a non-empty parse.

**Watch**
- **Off by default.** Marker-gated (`-m live`), skipped unless asked for, so
  the normal suite stays offline, fast and deterministic.
- Do this *before* the source count doubles, not after — that is the whole
  point of its position in the tier.
- Assert "parsed something plausible", not exact counts. A flaky assertion is
  worse than no test.

**Test** It is the test. Assert it *skips* cleanly without the marker.

---

# Part 3 — Justified by evidence

`feat/tier-3-evidence`, from Part 2's tip. Build on what Parts 1 and 2 proved.

### 3.1 — C4 · Intraday bars and news-to-move attribution

- [ ] `feat: pull 5m bars so an event can be checked against the move`

**Files** `backend/services/yahoo.py`, `backend/services/prices.py`,
`backend/db/prices.py`, `backend/services/events.py`,
`backend/tests/test_prices.py`

**Do** The differentiator, and the thing `events.py` currently admits it cannot
do: *did the stock actually move on this?* Yahoo serves
`interval=5m&range=1d` from the endpoint already in use. Pull 5m bars for
watchlist stocks **during their session only**, retain a fortnight, and
attribute each event to the move in the window after its timestamp.

**Watch**
- Needs 1.2 (OHLCV) first — attribution without volume is half the signal.
- `chart()` already skips the cache for `interval == "1m"`; extend that to 5m
  or a stale bar gets served as live.
- Session-only, or this is an all-night poll for a flat line.
- Scraped publish timestamps are unreliable, which is exactly why `events.py`
  refused to compute deltas. Attribute against a **window**, not an instant,
  and say so in the schema. Do not overclaim precision.
- Retention: `jobs.prune` drops price points older than 400 days. 5m bars need
  their own much shorter window or the table explodes.

**Test** Attribution picks the right window for a known timestamp; a bar
outside the session is not stored; pruning drops 5m rows on the short window
and daily rows on the long one.

### 3.2 — D2 · Embedding dedup for the semantic tail

- [ ] `feat: catch rewrites that share a story but not a vocabulary`

**Files** `backend/services/dedup.py`, `backend/db/connection.py`,
`pyproject.toml`, `backend/tests/test_dedup.py`

**Do** `token_set_ratio >= 88` (`dedup.py:54,257`) cannot match "Nvidia beats
on Q3 revenue" to "Chipmaker tops estimates, shares jump 6%" — same story,
near-zero token overlap. Add a third pass over cluster **winners only**, using
pgvector and a small local embedding model.

**Watch**
- **Keep both existing passes.** They are cheap and catch the bulk. This is a
  tail-catcher, not a replacement.
- Winners only — a few dozen titles per stock, not thousands.
- Much better after 2.3, because bodies embed far better than headlines.
- The original build died of over-broad matching. Keep the ±72h and
  single-stock scope; a semantic pass across stocks would repeat that mistake
  exactly.

**Test** The Nvidia/chipmaker pair merges; two genuinely different same-day
stories about one stock do not.

### 3.3 — D3 · Cross-stock story linking

- [ ] `feat: link one story across every stock it touches`

**Files** `backend/db/connection.py`, `backend/services/news/__init__.py`,
`backend/routes/news.py`, `backend/tests/test_dedup.py`

**Do** Dedup is scoped to one stock by design, so a supplier story affecting
three holdings appears three times with nothing connecting them. Add a nullable
`story_id`, assigned **after** storage by matching canonical URL hash across
stocks — identical URL, different tickers.

**Watch**
- Zero risk to the existing filter: this runs after storage and changes no
  clustering decision. Keep it that way.
- Match on `url_hash` only. Fuzzy cross-stock matching is precisely what broke
  the original build.

**Test** The same URL stored for three stocks shares one `story_id`; different
URLs do not.

### 3.4 — A2 · curl_cffi transport

- [ ] `feat: allow a browser TLS fingerprint when a host starts refusing`

**Files** `backend/services/http_client.py`, `backend/settings.py`,
`pyproject.toml`, `backend/tests/test_http_client.py`

**Do** httpx negotiates through OpenSSL, and that ClientHello has been in
anti-bot databases since 2017 — a host behind Cloudflare Bot Management blocks
at the handshake, before a header is read. Put `curl_cffi` with
`impersonate="chrome"` behind a flag.

**Watch**
- **Only build this if a host is actually 403-ing.** If nothing is refusing,
  tick the box with a one-line note in this file and skip it — premature
  insurance is just complexity. Check `/health` throttle counters first.
- Every rate-limit, breaker and cache line stays untouched. This swaps the
  transport, nothing else.
- It also restores real HTTP/2 without the hpack thread-safety bug that forced
  `http2=False`. Do not re-enable h2 on httpx.

**Test** The flag selects the transport; both paths satisfy the same limiter
tests.

### 3.5 — B6 + B7 · Insider aggregates and EDGAR full-text

- [ ] `feat: count insider filings and search what other filings say about us`

**Files** `backend/services/news/sec_edgar.py`, `backend/db/connection.py`,
`backend/routes/stocks.py`, `backend/tests/test_news_sources.py`

**Do** Free riders on 2.1's plumbing. Forms 3/4/5 already arrive in the
submissions JSON and are thrown away — route them to a counters table instead
of the feed ("four insiders sold this month" is a badge, not a headline). 13F
is the same shape, quarterly. Separately, `efts.sec.gov/LATEST/search-index`
finds filings that *mention* a company without being filed by it — a customer's
10-K naming a supplier as a concentration risk appears in no news feed
anywhere.

**Watch**
- Keep 3/4/5 out of the **feed**. The reason they were filtered is still valid:
  dozens a month would swamp it. Counters only.
- Full-text search is speculative. Build it only if 2.1 has been running
  cleanly; drop it otherwise and say so here.
- Same 10 req/s SEC ceiling and same User-Agent rule.

**Test** Form 4s increment the counter and create no news row; a full-text hit
naming another filer is attributed to the right stock.

### 3.6 — B3 · RNS and non-US filings

- [ ] `feat: read UK regulatory announcements`

**Files** `backend/services/news/rns.py` (new),
`backend/services/news/__init__.py`, `backend/tests/test_news_parsers.py`

**Do** RNS distributes 70%+ of UK company news and results, and nothing here
covers it. The README already concedes depth is US-biased.

**Watch**
- **Licensing decision first, before a line of code.** LSEG's official RNS
  feed is commercial. Investegate's terms forbid redistribution and commercial
  use — fine for a single-user local tool, a blocker the day Stocky ships as a
  product. If that call has not been made, **stop and ask**; do not write the
  adapter and settle it later.
- Gate on `not is_us_listing(exchange)`, mirroring the existing US gate.

**Test** Parse a captured page; assert the source is skipped for a US listing.

---

## Green gate

Every sub-point ends green before it is committed. On any failure, fix and
restart from the top.

**Postgres must be up first.** Without it the backend suite *hangs* on connect
rather than failing, so a retry loop spins forever. It is a Scoop install with
no Windows service:

```powershell
& "C:\Users\adamg\scoop\apps\postgresql\current\bin\pg_ctl" `
  -D "C:\Users\adamg\scoop\apps\postgresql\current\data" -l logfile start
```

Per sub-point, run only the relevant test files. Per part, run everything:

```bash
# absolute paths — the shell's cwd persists between calls
/c/Users/adamg/stocky/backend/.venv/Scripts/python.exe -m pytest -q /c/Users/adamg/stocky/backend/tests
cd /c/Users/adamg/stocky/frontend && npm run lint && npm run test
```

Baseline on `main`: 609 backend tests collected, 128 frontend tests passing
across 15 files, `eslint` clean.

Then, once per part rather than per commit, a live boot on the spare ports:

```bash
# backend on 5100 — 3000/5000 belong to whatever is already running
STOCKY_PORT=5100 \
STOCKY_CORS_ORIGINS=http://127.0.0.1:3100,http://localhost:3100 \
backend/.venv/Scripts/python.exe backend/main.py

# frontend on 3100, pointed at it
cd frontend && VITE_API_URL=http://127.0.0.1:5100 npm run dev -- --port 3100 --strictPort
```

Smoke: `/health` returns 200 and names the sources; the frontend loads and
renders a watchlist without console errors. Then kill **only those two PIDs**.

`--strictPort` matters: without it Vite silently hops to 3101 and the backend's
CORS allow-list no longer names the origin, which presents as an empty page
rather than an error.
