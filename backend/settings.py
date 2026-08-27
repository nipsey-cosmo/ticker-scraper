"""All configuration in one place, env-driven, with defaults that work out of the box.

Single-user local tool: no auth, no paid API keys required. The only optional
key is DSEEK (DeepSeek, for AI summaries); STOCKY_SEC_CONTACT is an email, not
a credential. Everything else is scraped from public endpoints.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ── Storage ──────────────────────────────────────────────────────────────
# Postgres. The default assumes a local server with trust auth, which is what
# `scoop install postgresql` gives you; docker-compose overrides it.
DB_DSN = os.getenv("STOCKY_DB_DSN") or "postgresql://postgres@localhost:5432/stocky"
# Enough for the API's request handlers plus the scraper's worker threads.
DB_POOL_SIZE = _int("STOCKY_DB_POOL_SIZE", 10)


def db_label() -> str:
    """host/dbname for /health — never the password."""
    parts = urlsplit(DB_DSN)
    return f"{parts.hostname or 'localhost'}/{parts.path.lstrip('/') or '?'}"

# ── API ──────────────────────────────────────────────────────────────────
# Loopback only. This is a personal tool with no auth; it must not be
# reachable from the network. Override deliberately if you ever need to.
HOST = os.getenv("STOCKY_HOST", "127.0.0.1")
PORT = _int("STOCKY_PORT", 5000)

# Explicit allow-list. `*` plus credentials is rejected by browsers anyway,
# and a wildcard on an unauthenticated local API lets any page you visit
# read your watchlist.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "STOCKY_CORS_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000,"
        # The frontend's production preview (`npm run serve`). Without it a
        # build measured against the real bundle silently renders empty.
        "http://localhost:4173,http://127.0.0.1:4173",
    ).split(",")
    if o.strip()
]

# ── Data freshness ───────────────────────────────────────────────────────
BACKFILL_DAYS = _int("STOCKY_BACKFILL_DAYS", 30)      # history pulled for a new follow
# Prices are refreshed on the fast cadence while any major market is in
# session and on the slow one outside it. A closed market's price cannot move,
# so polling it every few minutes would be load on someone else's servers in
# exchange for an unchanged number.
PRICE_REFRESH_OPEN_MINUTES = _int("STOCKY_PRICE_REFRESH_OPEN_MINUTES", 5)
PRICE_REFRESH_MINUTES = _int("STOCKY_PRICE_REFRESH_MINUTES", 60)
NEWS_REFRESH_MINUTES = _int("STOCKY_NEWS_REFRESH_MINUTES", 60)
# The fast lane carries only sources where minutes matter — a filing is
# public the moment it is accepted, and the write-ups follow it.
NEWS_FAST_MINUTES = _int("STOCKY_NEWS_FAST_MINUTES", 2)
QUOTE_STALE_MINUTES = _int("STOCKY_QUOTE_STALE_MINUTES", 15)   # serve cached quote under this age
NEWS_RETENTION_DAYS = _int("STOCKY_NEWS_RETENTION_DAYS", 120)

# ── Scraping ─────────────────────────────────────────────────────────────
SCRAPE_TIMEOUT = _float("STOCKY_SCRAPE_TIMEOUT", 12.0)
SCRAPE_MAX_RETRIES = _int("STOCKY_SCRAPE_MAX_RETRIES", 3)
SCRAPE_CONCURRENCY = _int("STOCKY_SCRAPE_CONCURRENCY", 8)      # total in-flight requests
SCRAPE_CACHE_TTL = _float("STOCKY_SCRAPE_CACHE_TTL", 300.0)    # seconds
FETCH_ARTICLE_IMAGES = _flag("STOCKY_FETCH_ARTICLE_IMAGES", True)
# Load seed/catalogue.json into an empty stocks table on startup. Off means a
# fresh database starts empty and fills up as you follow things.
SEED_ON_START = _flag("STOCKY_SEED_ON_START", True)
IMAGE_FETCH_LIMIT = _int("STOCKY_IMAGE_FETCH_LIMIT", 12)       # per stock per refresh
# Body extraction costs the same one request per article as an image does,
# and is capped the same way. Small on purpose: it runs before scoring, so
# the limit is what keeps a refresh from turning into hundreds of fetches.
EXTRACT_ARTICLE_BODIES = _flag("STOCKY_EXTRACT_ARTICLE_BODIES", True)
BODY_FETCH_LIMIT = _int("STOCKY_BODY_FETCH_LIMIT", 8)          # per refresh

# ── Sentiment ────────────────────────────────────────────────────────────
# "vader"   → finance-tuned VADER, no torch, instant (default)
# "finbert" → ProsusAI/finbert, needs `--extras finbert`, much heavier
SENTIMENT_BACKEND = os.getenv("STOCKY_SENTIMENT", "vader").strip().lower()

# ── Semantic dedup ───────────────────────────────────────────────────────
# The third dedup pass catches rewrites that share a story but not a
# vocabulary ("Nvidia beats on Q3 revenue" / "Chipmaker tops estimates"). It
# needs a sentence-embedding model, which is an opt-in extra for the same
# reason FinBERT is — `poetry install --extras semantic`.
#
# Empty is the off switch as well as the default: unset means no model, and
# the two lexical passes carry on exactly as before. A good small default once
# the extra is installed is "sentence-transformers/all-MiniLM-L6-v2".
EMBED_MODEL = os.getenv("STOCKY_EMBED_MODEL", "").strip()

# Cosine above which two cluster winners are the same story. Deliberately high.
# The original build died of over-broad matching, and a semantic test is much
# easier to make over-broad than a lexical one: at 0.7 two unrelated earnings
# stories about the same company look alike, because they *are* alike in every
# way except the fact they report.
EMBED_SIMILARITY = _float("STOCKY_EMBED_SIMILARITY", 0.82)

# ── Scraper transport ────────────────────────────────────────────────────
# httpx negotiates TLS through OpenSSL, and that ClientHello has been in
# anti-bot fingerprint databases since 2017. A host behind Cloudflare Bot
# Management can therefore refuse at the handshake, before it has read a single
# one of our carefully rotated headers. Two are doing it right now: stooq.com
# answers its CSV endpoint with a 200 carrying a browser-check page, and
# accessnewswire.com returns a flat 403.
#
# Set to a curl_cffi target ("chrome", "safari", "firefox", or a pinned version
# like "chrome131") to present a real browser fingerprint instead. Empty — the
# default — keeps httpx, so this changes nothing until it is asked for.
SCRAPE_IMPERSONATE = os.getenv("STOCKY_SCRAPE_IMPERSONATE", "").strip()

# SEC's fair-access policy requires every caller to identify itself with a
# contact address, and it is enforced, not advisory: a User-Agent carrying a
# repo URL instead of an email gets a flat 403 while the same request with an
# address in it returns 200. So EDGAR is opt-in — set STOCKY_SEC_CONTACT to an
# email you actually read and the source switches on; leave it unset and it is
# skipped. Scraping a regulator that asked to know who we are, without telling
# it, is not a trade worth making for one extra source.
# https://www.sec.gov/os/accessing-edgar-data
SEC_CONTACT = (os.getenv("STOCKY_SEC_CONTACT") or "").strip() or None
SEC_USER_AGENT = f"stocky/0.2 ({SEC_CONTACT})" if SEC_CONTACT else ""


def sec_enabled() -> bool:
    return bool(SEC_CONTACT)

# ── Optional third-party keys ────────────────────────────────────────────
DEEPSEEK_KEY = os.getenv("DSEEK") or None
DEEPSEEK_BASE_URL = os.getenv("STOCKY_DEEPSEEK_URL", "https://api.deepseek.com")
# Env-overridable because DeepSeek retires aliases without warning: the
# `deepseek-chat` this used to hardcode went away on 2026-07-24 and every
# summary silently started failing. Changing a model should not need a deploy.
DEEPSEEK_MODEL = os.getenv("STOCKY_DEEPSEEK_MODEL", "deepseek-v4-flash")


def ai_enabled() -> bool:
    return bool(DEEPSEEK_KEY)
