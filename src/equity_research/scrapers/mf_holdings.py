"""Monthly scheme-portfolio (holdings) scrapers — the per-AMC piece.

SEBI mandates every scheme disclose its full month-end portfolio, but there is **no
consolidated primary feed** — each AMC posts its own file (usually an XLSX). The good
news: most follow SEBI's prescribed layout (Name of the Instrument · ISIN ·
Industry/Rating · Quantity · Market/Fair Value · % to Net Assets), so a single
**header-detecting generic parser** handles the bulk; each AMC is then just a URL +
a couple of hints. Coverage grows one AMC at a time via ``REGISTRY``.

Row shape: ``{fund_name, isin, instrument, industry, quantity, market_value_cr, pct_nav}``.
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
    """Load an XLSX from bytes (some AMCs serve xlsx bytes under a .xls name; openpyxl's
    extension gate is bypassed by handing it a BytesIO)."""
    return openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------- generic SEBI-format parser ----------------
def _match_col(text: str) -> str | None:
    """Map a header-cell string to one of our logical columns."""
    t = text.lower()
    if "isin" in t:
        return "isin"
    if "% to net" in t or "% of net" in t or "% to nav" in t or "percentage to net" in t \
            or ("% " in t and "net asset" in t):
        return "pct"
    if "name of the instrument" in t or "name of instrument" in t or t.strip() == "instrument":
        return "name"
    if "industry" in t or "rating" in t:
        return "industry"
    if "quantity" in t:
        return "quantity"
    if "market" in t or "fair value" in t:
        return "mv"
    return None


def _header_map(row) -> dict[str, int]:
    m: dict[str, int] = {}
    for idx, c in enumerate(row or []):
        if c is None:
            continue
        col = _match_col(str(c))
        if col and col not in m:
            m[col] = idx
    return m


def _find_header(rows) -> tuple[int, dict[str, int]]:
    """First row that carries an ISIN column + a name/instrument column → (idx, colmap)."""
    for i, r in enumerate(rows[:25]):
        m = _header_map(r)
        if "isin" in m and "name" in m:
            return i, m
    return -1, {}


def _title(rows, header_idx: int, sheet: str) -> str:
    """Scheme title = first meaningful text cell above the header band."""
    for r in rows[:header_idx or 6]:
        for c in (r or [])[:4]:
            t = str(c).strip() if c else ""
            if len(t) > 6 and re.search(r"[A-Za-z]{3}", t) and "portfolio" not in t.lower() \
                    and "name of" not in t.lower():
                return t
    return sheet


def _mv_to_cr(v: float | None, header_text: str) -> float | None:
    if v is None:
        return None
    h = header_text.lower()
    if "lakh" in h:
        return v / 100
    if "crore" in h or "cr." in h or "(rs. in cr" in h:
        return v
    return v / 100          # SEBI default disclosure unit is ₹ lakh


def parse_generic(wb: openpyxl.Workbook) -> list[dict]:
    """Parse any SEBI-format monthly-portfolio workbook (one or many scheme sheets)."""
    out: list[dict] = []
    for sheet in wb.sheetnames:
        rows = list(wb[sheet].iter_rows(values_only=True))
        hi, cm = _find_header(rows)
        if hi < 0 or "pct" not in cm:
            continue
        fund_name = _title(rows, hi, sheet)
        mv_hdr = str(rows[hi][cm["mv"]]) if "mv" in cm and rows[hi][cm["mv"]] else ""
        sheet_rows: list[dict] = []
        for r in rows[hi + 1:]:
            if not r or len(r) <= cm["isin"]:
                continue
            isin = str(r[cm["isin"]]).strip() if r[cm["isin"]] else ""
            if not _ISIN.match(isin):
                continue
            sheet_rows.append({
                "fund_name": fund_name, "isin": isin,
                "instrument": (str(r[cm["name"]]).strip() if r[cm["name"]] else ""),
                "industry": (str(r[cm["industry"]]).strip() if cm.get("industry") is not None
                             and len(r) > cm["industry"] and r[cm["industry"]] else ""),
                "quantity": _num(r[cm["quantity"]]) if cm.get("quantity") is not None
                            and len(r) > cm["quantity"] else None,
                "market_value_cr": _mv_to_cr(_num(r[cm["mv"]]), mv_hdr) if cm.get("mv") is not None
                                   and len(r) > cm["mv"] else None,
                "pct_nav": _num(r[cm["pct"]]),
            })
        # per-sheet: %NAV disclosed as a fraction (sums to ~1) vs a percentage (~100)
        pcts = [x["pct_nav"] for x in sheet_rows if x["pct_nav"] is not None]
        if pcts and max(pcts) <= 1.5:
            for x in sheet_rows:
                if x["pct_nav"] is not None:
                    x["pct_nav"] *= 100
        out.extend(sheet_rows)
    return out


def _fetch_xlsx(url: str) -> tuple[str, list[dict]]:
    """Download an XLSX at ``url`` and parse it with the generic SEBI parser."""
    try:
        raw = fetch_bytes(url, timeout=60)
    except Exception:  # noqa: BLE001 — month may not be published yet
        return url, []
    try:
        return url, parse_generic(_load_xlsx(raw))
    except Exception:  # noqa: BLE001 — layout surprise; degrade, don't crash
        return url, []


# ---------------- per-AMC URL builders ----------------
_PPFAS_URL = ("https://amc.ppfas.com/downloads/portfolio-disclosure/"
              "{y}/PPFAS_Monthly_Portfolio_Report_{mon}_{d}_{y}.xls")


def fetch_ppfas(as_of: date) -> tuple[str, list[dict]]:
    """PPFAS month-end holdings across all its schemes (one workbook, sheet per scheme)."""
    me = _month_end(as_of)
    return _fetch_xlsx(_PPFAS_URL.format(y=me.year, mon=_MONTHS[me.month], d=me.day))


# AMC display-name (as in ``mf_scheme.amc``) -> fetcher(as_of) -> (url, rows)
REGISTRY = {
    "PPFAS Mutual Fund": fetch_ppfas,
}


def fetch_amc_holdings(amc_name: str, as_of: date) -> tuple[str, list[dict]]:
    """Holdings for a registered AMC at ``as_of`` (empty if the AMC isn't covered)."""
    fn = REGISTRY.get(amc_name)
    return fn(as_of) if fn else ("", [])
