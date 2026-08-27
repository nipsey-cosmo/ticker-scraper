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
kept — see `_MATERIAL_FORMS`. The insider forms are not discarded, though:
they are *counted* into `db.insiders` on the way past, because "four insider
filings this month" is real signal in a badge even though it is noise in a
feed.

**Full-text search finds what the submissions feed cannot.** A company's own
filings are only half its regulatory footprint: a customer's 10-K naming it as
a supply concentration risk is filed by the customer, appears under the
customer's CIK, and shows up in no news feed anywhere. `efts.sec.gov` indexes
the text of every filing, so it can be asked who else has been writing about
this company.

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
from normalize import day_str, days_ago_iso, parse_datetime

from ..http_client import scraper

SOURCE_TYPE = "SEC_EDGAR"
FULLTEXT_TYPE = "SEC_FULLTEXT"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"

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

# Counted, never published. Prefixes again, so 4/A comes along with 4.
_INSIDER_FORMS = ("3", "4", "5", "13F")

# Full-text hits worth surfacing. An annual or quarterly report naming another
# company is the concentration-risk case; a proxy or an 8-K naming one is
# usually a business relationship. Insider forms are excluded here too — a
# form 4 mentioning a company is its own filer's paperwork, not news about it.
_FULLTEXT_FORMS = ("10-K", "10-Q", "8-K", "20-F", "DEF 14A")
_FULLTEXT_MAX = 10

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


def fetch(ticker: str, company_name: str,
          short_name: str | None = None) -> list[dict]:
    """`ticker` is the plain US symbol; the CIK map resolves it.

    `short_name` is only needed to file the insider counts; without it the
    filings still come back and the counting is skipped.
    """
    if not settings.sec_enabled():
        return []

    cik = cik_for(ticker)
    if cik is None:
        return []

    # Zero-padded to ten digits in the URL, unpadded in the payload.
    response = scraper.get(_SUBMISSIONS_URL.format(cik=cik), headers=headers())
    if not response or not response.ok:
        return []

    try:
        data = json.loads(response.text)
    except ValueError:
        return []

    if short_name:
        _store_insider_counts(short_name, data)
    return parse_data(data, company_name or ticker)


def _store_insider_counts(short_name: str, data: dict) -> None:
    """Telemetry-grade: a failure here must not cost the refresh its filings."""
    try:
        import db
        db.insiders.record_counts(short_name, insider_counts(data))
    except Exception as exc:
        print(f"[edgar] could not record insider counts for {short_name}: "
              f"{type(exc).__name__}: {exc}")


def insider_counts(data: dict) -> dict[tuple[str, str], int]:
    """{(YYYY-MM, form): filings} for the insider paperwork in this payload.

    Scans the same bounded slice `parse_data` does, so the two always agree
    about which filings they have seen, and counts by filing date rather than
    acceptance time — a form 4 accepted after midnight belongs to the day it
    reports on.
    """
    recent = ((data.get("filings") or {}).get("recent")) or {}
    counts: dict[tuple[str, str], int] = {}

    for index, raw_form in enumerate((recent.get("form") or [])[:_MAX_SCAN]):
        form = _insider_form(str(raw_form or "").strip())
        if not form:
            continue
        filed = str(_at(recent, "filingDate", index) or "")[:7]
        if len(filed) != 7:
            continue
        counts[(filed, form)] = counts.get((filed, form), 0) + 1

    return counts


def _insider_form(form: str) -> str | None:
    """'4/A' -> '4'. Amendments count with the form they amend."""
    if not form:
        return None
    upper = form.upper()
    for prefix in _INSIDER_FORMS:
        if upper == prefix or upper.startswith(prefix + "/") or upper.startswith(prefix + "-"):
            return prefix
    return None


def parse(payload: str, company_name: str) -> list[dict]:
    """Split from fetch so it can be tested against a saved payload."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    return parse_data(data, company_name)


def parse_data(data: dict, company_name: str) -> list[dict]:
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


# ── Full-text search: filings that mention us but are not ours ───────────

def full_text(ticker: str, company_name: str, days: int = 7) -> list[dict]:
    """Filings by *other* companies that name this one.

    The submissions feed answers "what has this company filed". This answers
    the other half: who else has been writing about it. A customer naming a
    supplier as a concentration risk in its 10-K is filed under the customer's
    CIK, appears in no news feed anywhere, and is exactly the kind of thing a
    holder would want to know.

    The company's own filings are excluded — they already arrive through
    `fetch`, and storing them twice would be one story under two source types.
    """
    if not settings.sec_enabled() or not company_name:
        return []

    own_cik = cik_for(ticker)
    response = scraper.get(_FULLTEXT_URL, params={
        # Quoted, so the phrase matches as a phrase. Unquoted, "Rocket Lab"
        # matches every filing containing "rocket" or "lab", which is the
        # over-broad search that made GDELT unusable in this package.
        "q": f'"{company_name}"',
        "forms": ",".join(_FULLTEXT_FORMS),
        "dateRange": "custom",
        "startdt": days_ago_iso(days)[:10],
        "enddt": day_str(),
    }, headers=headers())

    if not response or not response.ok:
        return []
    return parse_full_text(response.text, company_name, own_cik)


def parse_full_text(payload: str, company_name: str,
                    own_cik: int | None) -> list[dict]:
    """Split from `full_text` so it can be tested against a saved payload."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []

    hits = ((data.get("hits") or {}).get("hits")) or []
    articles = []

    for hit in hits:
        if len(articles) >= _FULLTEXT_MAX:
            break
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        if not isinstance(source, dict):
            continue

        ciks = [_int_or_none(c) for c in (source.get("ciks") or [])]
        if own_cik is not None and own_cik in ciks:
            continue                    # our own filing, already in the feed

        filer = _filer_name(source)
        form = str(source.get("file_type") or source.get("root_form") or "").strip()
        url = _fulltext_url(hit.get("_id"), ciks)
        if not filer or not form or not url:
            continue

        articles.append({
            # Names both companies on purpose: the stock so the relevance
            # filter keeps it, and the filer so the headline says who said it.
            "title": f"{filer} names {company_name} in its {form}",
            "url": url,
            "published_at": parse_datetime(source.get("file_date")),
            "description": None,
            "image": None,
            "source": "SEC EDGAR full-text",
            "source_domain": "sec.gov",
            "source_type": FULLTEXT_TYPE,
        })

    return articles


def _filer_name(source: dict) -> str:
    """`display_names` is ["Acme Corp  (ACME)  (CIK 0000123)"] — the name is
    everything before the first bracketed part."""
    names = source.get("display_names") or []
    if not names:
        return ""
    return str(names[0]).split("  (")[0].strip()


def _fulltext_url(hit_id, ciks: list) -> str | None:
    """`_id` is "0000123456-26-000001:doc.htm"; the archive path needs the
    accession without dashes and a CIK to hang it off."""
    if not hit_id or not ciks or ciks[0] is None:
        return None
    accession, _, document = str(hit_id).partition(":")
    if not accession:
        return None
    folder = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{ciks[0]}/{folder}"
    return f"{base}/{document}" if document else f"{base}/{accession}-index.htm"


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
