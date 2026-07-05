"""Monthly scheme-portfolio (holdings) scrapers — the per-AMC piece.

SEBI mandates every scheme disclose its full month-end portfolio, but there is **no
consolidated primary feed** — each AMC posts its own file (usually an XLSX, layouts
vary). So this is a **registry of per-AMC parsers**; coverage grows one AMC at a time.
Each parser returns normalised rows the ingest layer maps to AMFI scheme codes.

Row shape: ``{fund_name, isin, instrument, industry, quantity, market_value_cr, pct_nav}``.

Started with **PPFAS** (single-AMC, clean layout: one worksheet per scheme; columns
code | name | ISIN | industry | qty | market-value-₹lakh | %-to-NAV). Add more AMCs by
registering a ``(url_builder, parser)`` in ``REGISTRY``.
"""

from __future__ import annotations

import calendar
import io
import re
from datetime import date

import openpyxl

from equity_research.common.http import fetch_bytes

_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _load_xlsx(raw: bytes) -> openpyxl.Workbook:
    """Load an XLSX from bytes (PPFAS serves xlsx bytes under a .xls name; openpyxl's
    extension gate is bypassed by handing it a BytesIO)."""
    return openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)


# ---------------- PPFAS ----------------
_PPFAS_URL = ("https://amc.ppfas.com/downloads/portfolio-disclosure/"
              "{y}/PPFAS_Monthly_Portfolio_Report_{mon}_{d}_{y}.xls")


def _ppfas_url(as_of: date) -> str:
    me = _month_end(as_of)
    return _PPFAS_URL.format(y=me.year, mon=_MONTHS[me.month], d=me.day)


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_ppfas(wb: openpyxl.Workbook) -> list[dict]:
    out: list[dict] = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        # the scheme title is the first meaningful text cell in the header band
        fund_name = sheet
        for r in rows[:6]:
            for c in (r or [])[:3]:
                t = str(c).strip() if c else ""
                if len(t) > 6 and re.search(r"[A-Za-z]{3}", t) and "portfolio" not in t.lower():
                    fund_name = t
                    break
            if fund_name != sheet:
                break
        for r in rows:
            if not r or len(r) < 7:
                continue
            isin = str(r[2]).strip() if r[2] else ""
            if not _ISIN.match(isin):          # data rows have a real ISIN in col 2
                continue
            qty, mv_lakh, pct = _num(r[4]), _num(r[5]), _num(r[6])
            out.append({
                "fund_name": fund_name,
                "isin": isin,
                "instrument": str(r[1]).strip() if r[1] else "",
                "industry": str(r[3]).strip() if r[3] else "",
                "quantity": qty,
                "market_value_cr": mv_lakh / 100 if mv_lakh is not None else None,  # ₹lakh → ₹cr
                "pct_nav": pct * 100 if pct is not None else None,                  # fraction → %
            })
    return out


def fetch_ppfas(as_of: date) -> tuple[str, list[dict]]:
    """PPFAS month-end holdings across all its schemes → ``(source_url, rows)``.
    ``rows`` is ``[]`` if that month isn't published yet."""
    url = _ppfas_url(as_of)
    try:
        raw = fetch_bytes(url, timeout=60)
    except Exception:  # noqa: BLE001 — month may not be out yet
        return url, []
    try:
        return url, _parse_ppfas(_load_xlsx(raw))
    except Exception:  # noqa: BLE001 — layout surprise; degrade, don't crash
        return url, []


# AMC display-name (as in ``mf_scheme.amc``) -> fetcher(as_of) -> (url, rows)
REGISTRY = {
    "PPFAS Mutual Fund": fetch_ppfas,
}


def fetch_amc_holdings(amc_name: str, as_of: date) -> tuple[str, list[dict]]:
    """Holdings for a registered AMC at ``as_of`` (empty if the AMC isn't covered)."""
    fn = REGISTRY.get(amc_name)
    return fn(as_of) if fn else ("", [])
