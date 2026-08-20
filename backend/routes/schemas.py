"""Response models and the DB-row → API-shape mappings.

One place for the wire format, so a column rename can't silently change the API.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import HTTPException, Query
from pydantic import BaseModel

from normalize import clean_symbol, now_utc, valid_symbol

MAX_SYMBOLS = 50
MAX_LIMIT = 500


class Stock(BaseModel):
    symbol: str
    name: Optional[str] = None
    type: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    currencyCode: Optional[str] = None
    exchange: Optional[str] = None
    yahooSymbol: Optional[str] = None
    price: Optional[float] = None
    change: Optional[float] = None
    changePercent: Optional[float] = None
    priceUpdatedAt: Optional[str] = None
    # ISO date, and 'pre' | 'post' for which side of the bell. Absent for a
    # non-US listing, which is a null rather than a failure.
    nextEarningsDate: Optional[str] = None
    nextEarningsTime: Optional[str] = None


class StockList(BaseModel):
    results: list[Stock]


class Sector(BaseModel):
    sector: str
    # "group" is Yahoo's coarse bucket (Industrials); "industry" is the precise
    # one (Aerospace & Defense). Both are offered as filters.
    level: str = "industry"
    # The coarse bucket an industry belongs to; None on group rows.
    group: Optional[str] = None
    stockCount: int


class SectorList(BaseModel):
    results: list[Sector]


class NewsArticle(BaseModel):
    id: UUID
    short_name: str
    title: str
    url: str
    publish_time: str
    source: str
    source_domain: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    relevance: str = "direct"
    lang: Optional[str] = None
    image: Optional[str] = None
    description: Optional[str] = None
    sentiment: Optional[float] = None
    ai_summary: Optional[str] = None
    # 'high' | 'medium' | 'low' from the event layer. None means unjudged —
    # the article predates the feature or the LLM was unavailable — which is
    # not the same as a judged 'low'.
    impact: Optional[str] = None
    # Only set on /news/trending, where the ranking is the stock's price move.
    movePercent: Optional[float] = None


class NewsList(BaseModel):
    results: list[NewsArticle]


class EventOut(BaseModel):
    id: UUID
    short_name: str
    created_at: str
    headline: str
    why_it_matters: str
    previously_known: Optional[str] = None
    impact: str
    news_ids: list[UUID] = []


class EventList(BaseModel):
    results: list[EventOut]


class PricePoint(BaseModel):
    ts: str
    close: float


class PriceSeries(BaseModel):
    symbol: str
    currency: Optional[str] = None
    price: Optional[float] = None
    change: Optional[float] = None
    changePercent: Optional[float] = None
    points: list[PricePoint]


class BarSeries(BaseModel):
    """Candles for the OHLC chart, on their own grid.

    Separate from the page grid because a candle needs a range to draw, and a
    daily candle here has none: outside the last day or two there is one
    observation per day stored, so open, high, low and close are the same
    number and every candle is a flat dash. Bucketing the candles to at least a
    week gives them a real range built from real observations.
    """
    bucket: str
    buckets: list[str]
    labels: list[str]
    open: list[Optional[float]]
    high: list[Optional[float]]
    low: list[Optional[float]]
    close: list[Optional[float]]
    articles: list[int]                    # the lower panel: coverage, not volume


class StockGraph(BaseModel):
    """One instrument on a bucket grid. Every list below is index-aligned to
    `buckets`, nulls included — the client never has to join on a date."""
    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    bucket: str                            # 'day' | 'week' | 'month'
    buckets: list[str]                     # bucket start dates, YYYY-MM-DD
    labels: list[str]                      # the same, pre-formatted for an axis
    close: list[Optional[float]]
    bars: BarSeries
    maShort: list[Optional[float]]
    maLong: list[Optional[float]]
    maShortWindow: int                     # in buckets, so the UI can label it
    maLongWindow: int
    drawdown: list[Optional[float]]        # percent below the running peak
    sentiment: list[Optional[float]]       # article-weighted mean, −1..+1
    articles: list[int]
    positive: list[int]
    negative: list[int]
    neutral: list[int]
    samples: list[list[float]]             # every scored article, per bucket
    periodReturn: Optional[float] = None
    # The days periodReturn actually spans — routinely narrower than the
    # timeframe requested, because price history only goes back as far as the
    # backfill did. The UI labels the figure with these, not with `days`.
    returnFrom: Optional[str] = None
    returnTo: Optional[str] = None
    maxDrawdown: Optional[float] = None
    correlation: Optional[float] = None
    totalArticles: int = 0
    # Most recent day holding a price or an article. The grid always runs to
    # today; this says where the data stops.
    dataThrough: Optional[str] = None


class UniverseStock(BaseModel):
    symbol: str
    name: Optional[str] = None
    sector: Optional[str] = None
    sentiment: list[Optional[float]]
    articles: list[int]
    latestSentiment: Optional[float] = None
    sentimentChange: Optional[float] = None
    periodReturn: Optional[float] = None
    returnFrom: Optional[str] = None
    returnTo: Optional[str] = None
    # z(sentiment change) − z(return), across the universe. None when the stock
    # is missing either input.
    divergence: Optional[float] = None
    totalArticles: int = 0


class UniverseGraph(BaseModel):
    bucket: str
    buckets: list[str]
    labels: list[str]
    stocks: list[UniverseStock]
    dataThrough: Optional[str] = None


class TrendingStock(BaseModel):
    symbol: str
    name: Optional[str] = None
    articleCount: int = 0
    avgSentiment: Optional[float] = None
    changePercent: Optional[float] = None


class Mover(BaseModel):
    symbol: str
    name: Optional[str] = None
    price: Optional[float] = None
    currencyCode: Optional[str] = None
    changePercent: Optional[float] = None
    sentiment: Optional[float] = None
    sentimentDelta: Optional[float] = None
    articleCount: int = 0


class AiSummary(BaseModel):
    id: Optional[UUID] = None
    symbol: Optional[str] = None
    ai_summary: str
    cached: bool = False


class Ok(BaseModel):
    ok: bool = True


class AppConfig(BaseModel):
    """What the user asked for, and what is actually in effect.

    The two differ whenever there is no usable key: the flag can be on while
    the feature is inert. Sending both means the UI can say "off because you
    switched it off" rather than showing a toggle that appears to do nothing.
    """
    llmScraping: bool
    aiSummaries: bool
    keyPresent: bool
    keyRejected: bool
    scrapingGradesImpact: bool      # llmScraping AND a usable key
    summariesAvailable: bool        # aiSummaries AND a usable key


class AppConfigUpdate(BaseModel):
    """Both optional: a PUT naming one flag must not reset the other."""
    llmScraping: Optional[bool] = None
    aiSummaries: Optional[bool] = None


# ── Mappers ──────────────────────────────────────────────────────────────

def to_stock(row: dict) -> Stock:
    return Stock(
        symbol=row["short_name"],
        name=row.get("name"),
        type=row.get("type"),
        sector=row.get("sector"),
        industry=row.get("industry"),
        # The quote currency is the one the price is actually in; the catalogue
        # currency is unreliable (Trading212 lists TSLA as EUR).
        currencyCode=row.get("quote_currency") or row.get("currency_code"),
        exchange=row.get("exchange"),
        yahooSymbol=row.get("yahoo_symbol"),
        price=row.get("price"),
        change=row.get("price_change"),
        changePercent=row.get("price_change_percent"),
        priceUpdatedAt=row.get("price_updated_at"),
        **_upcoming_earnings(row),
    )


def _upcoming_earnings(row: dict) -> dict:
    """A date that has passed is not upcoming. The daily sweep rewrites
    these, but between runs a date can fall into the past."""
    date = row.get("next_earnings_date")
    if not date or str(date) < now_utc().date().isoformat():
        return {}
    return {"nextEarningsDate": str(date),
            "nextEarningsTime": row.get("next_earnings_time")}


def to_news(row: dict) -> NewsArticle:
    return NewsArticle(
        id=row["id"],
        short_name=row["short_name"],
        title=row["title"],
        url=row["url"],
        publish_time=row["publish_time"],
        source=row["source"],
        source_domain=row.get("source_domain"),
        source_url=row.get("source_url"),
        source_type=row.get("source_type"),
        relevance=row.get("relevance") or "direct",
        lang=row.get("lang"),
        image=row.get("image"),
        description=row.get("description"),
        sentiment=row.get("sentiment"),
        ai_summary=row.get("ai_summary"),
        impact=row.get("impact"),
        movePercent=row.get("move_percent"),
    )


def to_event(row: dict) -> EventOut:
    return EventOut(
        id=row["id"],
        short_name=row["short_name"],
        created_at=row["created_at"],
        headline=row["headline"],
        why_it_matters=row["why_it_matters"],
        previously_known=row.get("previously_known"),
        impact=row["impact"],
        news_ids=row.get("news_ids") or [],
    )


# ── Shared validation ────────────────────────────────────────────────────

def parse_symbols(raw: str) -> list[str]:
    """Validate a comma-separated symbol list.

    Symbols reach outbound scraper URLs, so anything not ticker-shaped is
    rejected here rather than forwarded to a third party.
    """
    symbols, seen = [], set()
    for part in (raw or "").split(","):
        symbol = clean_symbol(part)
        if not symbol or symbol in seen:
            continue
        if not valid_symbol(symbol):
            raise HTTPException(status_code=400, detail=f"Invalid symbol: {part[:20]!r}")
        seen.add(symbol)
        symbols.append(symbol)

    if not symbols:
        raise HTTPException(status_code=400, detail="No valid symbols provided")
    if len(symbols) > MAX_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_SYMBOLS} symbols per request")
    return symbols


def require_symbol(symbol: str) -> str:
    cleaned = clean_symbol(symbol)
    if not valid_symbol(cleaned):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return cleaned


LimitQuery = Query(100, ge=1, le=MAX_LIMIT)
