# ticker-scraper

<img width="1596" height="876" alt="image" src="https://github.com/user-attachments/assets/c86951d6-631d-436a-8d6b-a3b2959a4a69" />

**A free, self-hosted stock news aggregator and price tracker.** It watches
the handful of companies you actually follow, pulls their news from five
independent sources and their prices from three, throws away the duplicate
coverage, and ranks what survives by how far the price moved today. It runs on
your own machine against your own Postgres. No account, no subscription tier, no
API key required.

**It is an API first.** The FastAPI backend scrapes, stores and serves
everything on its own, so you can run it alone and read it from a script, a
notebook or your own client. The React frontend is an optional client for it.

FastAPI and Postgres, with an optional React UI. MIT licensed.

## Why it is worth running

**Deduplication that holds up.** Follow five stocks on any finance portal and
the same wire report reaches you fifty times. A fresh follow of Shell found 630
articles and stored 418, after dropping 120 duplicate stories, 29 duplicate
URLs, 62 off-topic hits and one advert.

**Seven news fetches across five operators**, queried concurrently: Google News,
Yahoo, Bing, Finviz, Nasdaq and SEC EDGAR. If one goes quiet the feed thins out
and carries on.

**Prices from a three-source fallback chain.** Yahoo, Nasdaq and CNBC, verified
agreeing to the cent on AAPL, RKLB and SHEL.L. Quotes resolve to the line the
company actually trades on, stored in major currency units.

**12,038 instruments seeded on first start**, so search and follow work straight
away with nothing to import.

**Seven charts over your own scraped history** at `/graphs`: candlesticks,
return distribution, news reaction, trend and drawdown, cross-section,
divergence, and a sentiment heatmap.

**Live without polling.** The backend pushes over a WebSocket whenever it
commits, so a background refresh or an edit in another tab updates the open page.

**Your watchlist is a table in your own database.** The only outbound traffic is
to the sources being scraped.

## Quick start

### The API

You need Python 3.12 and Postgres 18 or newer. The version floor is real:
surrogate keys default to the built-in `uuidv7()`, which arrived in 18 and has no
extension fallback. `docker compose up` in `backend/` brings its own.

```bash
createdb stocky

cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
poetry install
python main.py                  # API on :5000, docs at /docs
```

That is a complete install. Follow a stock, then read its news and prices with
any HTTP client:

```bash
curl -X POST http://127.0.0.1:5000/watchlist \
     -H "Content-Type: application/json" -d '{"symbol": "AAPL"}'
curl "http://127.0.0.1:5000/news?symbols=AAPL&days=7"
curl "http://127.0.0.1:5000/stocks/AAPL/prices?days=30"
```

`/docs` has the full schema, and the [backend README](backend/README.md#api)
lists the route groups. There is no authentication, so the API binds to
loopback by default.

A second terminal is optional. Without it the API still answers, with whatever
was last scraped until you refresh a stock yourself.

```bash
cd backend && python scheduler.py    # background refreshes
```

### The frontend (optional)

You need Node.

```bash
cd frontend
npm install
npm run dev                     # UI on :3000
```

Leave both ports alone. Vite is pinned to 3000, the frontend calls 5000, and the
backend's CORS allow-list names both. Change one on its own and you get a silent
CORS failure that looks like an empty dashboard.

## Using the frontend

Add a stock from the watchlist editor in the ticker strip. Following one kicks
off a month-long backfill of prices and news, so the feed fills in behind you.
Unfollowing keeps the articles.

The page is one screen. Trending leads, Latest runs beside it, the news river
picks up underneath, and the right rail holds tickers. Clicking a ticker
anywhere filters everything to that stock. The search bar filters by text, date
window, sentiment and relevance.

Articles open in a reader dialog with whatever description was published. Plenty
arrive as a Google News redirect with nothing behind them, so there is a
"Summarise with AI" button for those. It costs nothing until you click it.

## The optional LLM

The project is a scraper first. Set `DSEEK` to a DeepSeek key in `backend/.env` and
you get two extras, both switchable from the AI settings dialog in the ticker
strip. Everything else works without a key.

| Toggle | What it costs | What you lose by turning it off |
|---|---|---|
| Grade new articles during scraping | One call per stock, only on refreshes that inserted something | The high/medium/low impact tag on article cards |
| AI summaries on demand | One call per click | The summarise button |

Grading asks one question of each batch: would someone who already read the last
week's coverage learn anything new here? The database is the prior, so there is
no vector store to maintain. A key the provider rejects with 401 latches off
after the first failure, which stops an expired key costing a round trip per
stock every hour.

## Where the detail lives

| Path | Stack | Read |
|---|---|---|
| `backend/` | FastAPI, Postgres, psycopg 3, raw SQL | [backend/README.md](backend/README.md) |
| `frontend/` | Vite, React, TypeScript, Tailwind | [frontend/README.md](frontend/README.md) |

The backend README covers every source and why it is there, the shared scraper's
rate limiting and circuit breaking, exchange normalisation, deduplication,
sentiment, and the full environment variable table. The frontend README covers
the data path and the deliberate absences.

US listings get the most depth, since Finviz, Nasdaq and EDGAR are US only. A
London or German line is covered by Google, Yahoo and Bing, and for thin non-US
tickers Bing is often the only source returning anything.

## License

MIT. See [LICENSE](LICENSE).
