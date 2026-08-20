"""SEC EDGAR filings — the primary source, before anyone reports on it.

Every other source in this package is somebody's write-up of an event. EDGAR
is the event: the 8-K lands here first and the headlines follow. It is also
the only source that cannot go stale, be paywalled, or quietly drop a story.

This reads `data.sec.gov`, which SEC documents as updating in real time with a
sub-second processing delay. The old `cgi-bin/browse-edgar` Atom feed was on a
ten-minute lag, which made polling it faster than hourly pointless.

Three things shape this module:

**Titles are useless as filed.** A filing's own description is `"8-K"` — no
company, no ticker. Stored as-is, every relevance check would drop it and
every dedup key would collide with every other company's 8-K. So the title is
constructed from the company name and the form.

**Most filings are noise.** Forms 3/4/5 and 144 are routine insider paperwork,
dozens a month, and would swamp a feed. Only the forms that move a price are
kept — see `_MATERIAL_FORMS`.

**`filings.recent` is columnar.** Not a list of filings but a dict of parallel
lists — one for forms, one for dates, one for documents — so a row has to be
zipped back together by index.

SEC's fair-access policy requires a declared identity with contact details on
every request, and enforces it: a User-Agent carrying a repo URL rather than an
email gets a flat 403. So this source is opt-in on `STOCKY_SEC_CONTACT` and
skips itself when that is unset, rather than hammering a regulator that asked
to know who we are with requests that will be refused anyway.
"""
from __future__ import annotations

import json
import threading

import settings
from normalize import parse_datetime

from ..http_client import scraper

SOURCE_TYPE = "SEC_EDGAR"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

_MAX_ENTRIES = 25
# `recent` carries up to a thousand filings and most of them are form 4s.
# Scanning a slice bounds how far back a first backfill reaches; anything
# older than this is history, not news.
_MAX_SCAN = 300

# Prefixes, so amendments (`8-K/A`) and series (`424B5`) come along for free.
_MATERIAL_FORMS = (
    "8-K",      # material event — the one that moves prices
    "10-K",     # annual report
    "10-Q",     # quarterly report
    "6-K",      # foreign private issuer interim report
    "20-F",     # foreign private issuer annual report
    "40-F",     # Canadian annual report
    "425",      # merger / business combination communication
    "DEF 14A",  # proxy statement
    "DEFM14A",  # merger proxy
    "SC 13D",   # activist stake
    "SC TO",    # tender offer
    "S-1",      # registration — IPO or new shares
    "S-3",      # shelf registration
    "424B",     # prospectus supplement, i.e. an actual raise
)

_cik_by_ticker: dict[str, int] = {}
_cik_lock = threading.Lock()


def headers() -> dict:
    return {"User-Agent": settings.SEC_USER_AGENT, "Accept": "application/json"}


def cik_for(ticker: str) -> int | None:
    """Ticker → CIK, from a map fetched once and then kept for the process.

    Ten thousand rows that change when a company lists or renames itself —
    far slower than this process lives — so it is loaded lazily and never
    refreshed. The fetch happens under the lock on purpose: the alternative
    is every thread in the pool discovering the empty dict at once and
    downloading the same megabyte.
    """
    with _cik_lock:
        if not _cik_by_ticker:
            response = scraper.get(_TICKERS_URL, headers=headers())
            if not response or not response.ok:
                return None
            try:
                table = response.json()
            except ValueError:
                return None
            for row in table.values():
                symbol = str(row.get("ticker") or "").upper()
                if symbol:
                    _cik_by_ticker[symbol] = int(row["cik_str"])
        return _cik_by_ticker.get(ticker.upper())


def fetch(ticker: str, company_name: str) -> list[dict]:
    """`ticker` is the plain US symbol; the CIK map resolves it."""
    if not settings.sec_enabled():
        return []

    cik = cik_for(ticker)
    if cik is None:
        return []

    # Zero-padded to ten digits in the URL, unpadded in the payload.
    response = scraper.get(_SUBMISSIONS_URL.format(cik=cik), headers=headers())
    if not response or not response.ok:
        return []
    return parse(response.text, company_name or ticker)


def parse(payload: str, company_name: str) -> list[dict]:
    """Split from fetch so it can be tested against a saved payload."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []

    recent = ((data.get("filings") or {}).get("recent")) or {}
    cik = data.get("cik")
    forms = recent.get("form") or []
    articles = []

    for index, raw_form in enumerate(forms[:_MAX_SCAN]):
        if len(articles) >= _MAX_ENTRIES:
            break
        form = str(raw_form or "").strip()
        if not form or not is_material(form):
            continue

        url = _document_url(cik, _at(recent, "accessionNumber", index),
                            _at(recent, "primaryDocument", index))
        if not url:
            continue

        # Usually just the form again ("8-K"), which would title as
        # "Apple Inc. filed 8-K: 8-K". Dropped when it says nothing.
        description = str(_at(recent, "primaryDocDescription", index) or "").strip()
        if description.upper() == form.upper():
            description = ""

        articles.append({
            "title": _title(company_name, form, description),
            "url": url,
            # `acceptanceDateTime` is what makes this a real-time source.
            # `filingDate` is a date alone, so every filing that day would
            # carry the same timestamp and sort arbitrarily.
            "published_at": parse_datetime(_at(recent, "acceptanceDateTime", index)
                                           or _at(recent, "filingDate", index)),
            "description": description or None,
            "image": None,
            "source": "SEC EDGAR",
            "source_domain": "sec.gov",
            "source_type": SOURCE_TYPE,
        })

    return articles


def _at(recent: dict, key: str, index: int):
    """One cell of the columnar payload. The columns are meant to be the same
    length; a short one returns None rather than raising."""
    column = recent.get(key) or []
    return column[index] if index < len(column) else None


def _document_url(cik, accession: str | None, document: str | None) -> str | None:
    """The filing itself where the payload names a primary document, its index
    page otherwise. The archive path uses the *unpadded* CIK."""
    if not cik or not accession:
        return None
    folder = str(accession).replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{folder}"
    return f"{base}/{document}" if document else f"{base}/{accession}-index.htm"


def is_material(form: str) -> bool:
    upper = form.upper().strip()
    return any(upper.startswith(prefix) for prefix in _MATERIAL_FORMS)


def _title(company_name: str, form: str, form_name: str) -> str:
    """"Apple Inc. filed 8-K: Current report".

    Naming the company is what makes the item survive the relevance filter and
    dedup on the right key, so it is not cosmetic.
    """
    suffix = f": {form_name}" if form_name else ""
    return f"{company_name} filed {form}{suffix}"
