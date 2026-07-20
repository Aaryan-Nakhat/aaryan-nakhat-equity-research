"""NSE IPO scraper — list live / upcoming public issues, their live subscription, and
the official offer documents for pre-listing analysis.

All **primary NSE sources**:
  - ``/api/ipo-current-issue``            — live/open issues + total subscription
  - ``/api/all-upcoming-issues?category=ipo`` — forthcoming issues
  - ``/api/ipo-detail?symbol=&series=EQ`` — category-wise subscription (QIB/NII/Retail)
  - ``nsearchives.nseindia.com/content/ipo/<DOC>_<SYMBOL>.zip`` — the offer documents,
    keyed predictably by symbol: **RHP** (full prospectus — financials, risk factors,
    fresh-issue vs OFS, use of proceeds), **RATIOS** (price-band advertisement carrying
    the KPIs / valuation-at-band / listed-peer table), **ANCHOR** (anchor allotment).

The ``/api/*`` calls go through the browser tier (``nse_api.fetch_api``); the archive
zips are plain HTTP (``common.http``). See ``docs/SCRAPING.md``.
"""

from __future__ import annotations

import io
import zipfile

from scrapling.fetchers import Fetcher

from equity_research.common.http import fetch_bytes
from equity_research.scrapers.nse_api import fetch_api

_ARCHIVE = "https://nsearchives.nseindia.com/content/ipo/"


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _norm(item: dict) -> dict:
    return {
        "symbol": (item.get("symbol") or "").strip(),
        "company": (item.get("companyName") or "").strip(),
        "series": (item.get("series") or "").strip(),
        "start": (item.get("issueStartDate") or "").strip(),
        "end": (item.get("issueEndDate") or "").strip(),
        "price_band": (item.get("issuePrice") or "").strip(),
        "issue_size_shares": _num(item.get("issueSize")),
        "status": (item.get("status") or "").strip(),
        "subscription_x": _num(item.get("noOfTime")),   # total oversubscription (current only)
    }


def list_current() -> list[dict] | None:
    """Live/open IPOs (with total subscription). ``None`` if the NSE fetch **failed**
    (so the caller can say 'try again' rather than a misleading 'no IPOs'); ``[]`` means
    genuinely none open."""
    try:
        data = fetch_api("/api/ipo-current-issue")
    except Exception:  # noqa: BLE001 — signal failure (distinct from an empty list)
        return None
    return [_norm(x) for x in (data or []) if isinstance(x, dict) and x.get("symbol")]


def list_upcoming() -> list[dict] | None:
    """Forthcoming IPOs (excludes ones already open). ``None`` on fetch failure, ``[]`` if none."""
    try:
        data = fetch_api("/api/all-upcoming-issues?category=ipo")
    except Exception:  # noqa: BLE001
        return None
    out = [_norm(x) for x in (data or []) if isinstance(x, dict) and x.get("symbol")]
    return [x for x in out if x["status"].lower() != "active"]


def subscription_detail(symbol: str) -> list[dict]:
    """Category-wise subscription (QIB / NII / Retail …) for a live issue —
    ``[{"category", "times"}, ...]`` for the roll-up categories that carry a figure."""
    try:
        data = fetch_api(f"/api/ipo-detail?symbol={symbol}&series=EQ")
    except Exception:  # noqa: BLE001
        return []
    rows = data.get("bidDetails") if isinstance(data, dict) else None
    out = []
    for r in rows or []:
        x = _num(r.get("noOfTime"))
        cat = (r.get("category") or "").strip()
        # keep the headline roll-ups (QIB / NII / RII / Total); drop the NII bid-amount sub-splits
        if x is not None and cat and "bid amount" not in cat.lower():
            out.append({"category": cat, "times": x})
    return out


def _archive_url(symbol: str, doc: str) -> str:
    return f"{_ARCHIVE}{doc}_{symbol}.zip"


def has_prospectus(symbol: str) -> bool:
    """True if the RHP archive exists (used to keep 'upcoming' to *analysable* issues).
    Uses a tiny Range request so it doesn't pull the whole (10-16 MB) zip."""
    try:
        r = Fetcher.get(_archive_url(symbol, "RHP"), headers={"Range": "bytes=0-3"},
                        stealthy_headers=True, timeout=20)
    except Exception:  # noqa: BLE001
        return False
    return r.status in (200, 206) and bool(r.body) and r.body[:2] == b"PK"


# preferred filename keyword per doc; else fall back to the largest non-newspaper PDF
_DOC_PREF = {"RHP": ("rhp",), "RATIOS": ("price band", "ratio"), "ANCHOR": ("anchor",)}
_ADS = ("financial express", "indian express", "mahasagar", "jansatta",
        "business standard", "navshakti", "loksatta")


def _pick_pdf(raw: bytes, prefer: tuple[str, ...]) -> tuple[str, bytes] | None:
    """From a doc zip, return (filename, pdf-bytes): the file matching a preferred
    keyword, else the largest PDF that isn't a mandatory newspaper advertisement."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    pdfs = [i for i in zf.infolist() if i.filename.lower().endswith(".pdf") and i.file_size > 0]
    if not pdfs:
        return None
    for kw in prefer:
        for i in pdfs:
            if kw in i.filename.lower():
                return i.filename.rsplit("/", 1)[-1], zf.read(i.filename)
    non_ads = [i for i in pdfs if not any(a in i.filename.lower() for a in _ADS)]
    best = max(non_ads or pdfs, key=lambda i: i.file_size)
    return best.filename.rsplit("/", 1)[-1], zf.read(best.filename)


def _fetch_doc(symbol: str, doc: str) -> tuple[str, bytes] | None:
    try:
        raw = fetch_bytes(_archive_url(symbol, doc))
    except Exception:  # noqa: BLE001
        return None
    return _pick_pdf(raw, _DOC_PREF.get(doc, ()))


def documents(symbol: str, *, budget_mb: float = 18.0) -> list[tuple[str, bytes]]:
    """Offer documents for Gemini, primary-source, in priority order within a size
    budget (the RHP is always kept even if large): RHP → price-band KPIs → anchor book.
    Returns ``[(label, pdf-bytes), ...]``; empty if nothing is published yet."""
    out: list[tuple[str, bytes]] = []
    total = 0.0
    for doc, label in (("RHP", "Red Herring Prospectus (RHP)"),
                       ("RATIOS", "Price-band advertisement — KPIs / valuation / listed peers"),
                       ("ANCHOR", "Anchor investor allotment")):
        picked = _fetch_doc(symbol, doc)
        if not picked:
            continue
        fname, data = picked
        if out and total + len(data) > budget_mb * 1e6:   # keep at least the RHP
            continue
        out.append((f"{label} — {fname}", data))
        total += len(data)
    return out
