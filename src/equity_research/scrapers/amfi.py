"""AMFI (Association of Mutual Funds in India) — official mutual-fund NAV data.

AMFI is the industry SRO; its NAV files are the authoritative, primary source for
every Indian mutual-fund scheme's NAV. Both feeds are plain-HTTP text (no browser
tier), semicolon-delimited, grouped by category header then AMC header.

- ``fetch_navall()`` — the full daily universe: one row per scheme (code, ISINs,
  name, AMC, category) + that day's NAV. Backbone for the ``mf_scheme`` master and
  the daily ``mf_nav`` point (accumulated forward → a NAV time series over time).
- ``fetch_nav_history(amc_code, frm, to)`` — historical NAV for one AMC over a date
  range, for on-demand backfill of a scheme's return series.

The old ``www.amfiindia.com`` host 302-redirects to ``portal.amfiindia.com``; we hit
the portal host directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from equity_research.common.http import fetch_text

_NAVALL = "https://portal.amfiindia.com/spages/NAVAll.txt"
_HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
_DISCLOSURE = "https://www.amfiindia.com/online-center/portfolio-disclosure"

# Fallback active-AMC code list (scraped 2026-07 from the disclosure page's RssNAV links),
# used if the live scrape of amc_codes() comes back empty.
_AMC_CODES_FALLBACK = (
    3, 4, 6, 9, 13, 16, 17, 18, 20, 21, 22, 25, 26, 27, 28, 32, 33, 37, 41, 42, 45, 46,
    47, 48, 53, 54, 55, 58, 61, 62, 63, 64, 65, 67, 69, 70, 71, 72, 73, 74, 75, 76, 77,
    78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89,
)


@dataclass(frozen=True, slots=True)
class SchemeNav:
    """One scheme's identity + a single-day NAV point (from a NAV feed row)."""
    scheme_code: int
    isin_growth: str | None
    isin_reinvest: str | None
    scheme_name: str
    amc: str | None
    category: str | None
    asset_class: str
    plan: str | None
    option: str | None
    nav: float | None
    nav_date: date | None


def _asset_class(category: str) -> str:
    """Coarse asset class from an AMFI category header string."""
    c = category.lower()
    if "equity" in c:
        return "Equity"
    if "hybrid" in c:
        return "Hybrid"
    if "solution" in c:          # retirement / children's funds
        return "Solution"
    if "debt" in c or "income" in c or "gilt" in c or "liquid" in c or "money market" in c:
        return "Debt"
    return "Other"               # index / ETF / FoF / other schemes


def _plan_option(name: str) -> tuple[str | None, str | None]:
    """Parse Direct/Regular and Growth/IDCW out of a scheme name."""
    n = name.upper()
    plan = "Direct" if "DIRECT" in n else ("Regular" if "REGULAR" in n else None)
    if "IDCW" in n or "DIVIDEND" in n:
        option = "IDCW"
    elif "GROWTH" in n:
        option = "Growth"
    else:
        option = None
    return plan, option


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None


def _num(s: str) -> float | None:
    try:
        return float(s.strip())
    except (TypeError, ValueError):
        return None


def _is_amc(line: str) -> bool:
    return "Mutual Fund" in line


def _is_category(line: str) -> bool:
    # Category headers read like "Open Ended Schemes(Equity Scheme - Multi Cap Fund)".
    return "Scheme" in line and not _is_amc(line)


def _parse_navall(text: str) -> list[SchemeNav]:
    """Parse the NAVAll layout: header, then repeating [category, AMC, data rows...]."""
    out: list[SchemeNav] = []
    category: str | None = None
    amc: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("Scheme Code;"):
            continue
        if ";" not in line:
            if _is_amc(line):
                amc = line
            elif _is_category(line):
                category = line
                amc = None       # new category block resets the AMC context
            continue
        parts = line.split(";")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue
        name = parts[3].strip()
        plan, option = _plan_option(name)
        out.append(SchemeNav(
            scheme_code=int(parts[0].strip()),
            isin_growth=(parts[1].strip() or None) if parts[1].strip() != "-" else None,
            isin_reinvest=(parts[2].strip() or None) if parts[2].strip() != "-" else None,
            scheme_name=name,
            amc=amc,
            category=category,
            asset_class=_asset_class(category or ""),
            plan=plan,
            option=option,
            nav=_num(parts[4]),
            nav_date=_parse_date(parts[5]),
        ))
    return out


def fetch_navall() -> list[SchemeNav]:
    """Full daily NAV universe — one ``SchemeNav`` per scheme (``[]`` on failure)."""
    try:
        text = fetch_text(_NAVALL, timeout=60)
    except Exception:  # noqa: BLE001 — best-effort, never break the pipeline
        return []
    return _parse_navall(text)


def amc_codes() -> list[int]:
    """Active AMFI AMC codes, scraped from the portfolio-disclosure page's ``RssNAV``
    links (falls back to a captured snapshot if the scrape yields nothing)."""
    import re
    try:
        text = fetch_text(_DISCLOSURE, timeout=45)
        codes = sorted({int(m) for m in re.findall(r"RssNAV\.aspx\?mf=(\d+)", text)})
    except Exception:  # noqa: BLE001
        codes = []
    return codes or list(_AMC_CODES_FALLBACK)


def amc_name(amc_code: int, on: date) -> str | None:
    """The AMC's display name for ``amc_code`` — read from the history report's AMC
    header line (a one-day window is enough). None if the code has no schemes."""
    url = f"{_HISTORY}?frmdt={on:%d-%b-%Y}&todt={on:%d-%b-%Y}&mf={amc_code}"
    try:
        text = fetch_text(url, timeout=45)
    except Exception:  # noqa: BLE001
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if _is_amc(line):
            return line
    return None


def fetch_nav_history(amc_code: int, frm: date, to: date) -> list[tuple[int, date, float]]:
    """Historical NAV rows ``(scheme_code, nav_date, nav)`` for one AMC over a range.

    ``amc_code`` is AMFI's numeric fund-house id (the report's ``mf`` param). Returns
    ``[]`` on any failure. Used for on-demand backfill of a scheme's return series.
    """
    url = (f"{_HISTORY}?frmdt={frm:%d-%b-%Y}&todt={to:%d-%b-%Y}&mf={amc_code}")
    try:
        text = fetch_text(url, timeout=90)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int, date, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ";" not in line or line.startswith("Scheme Code;"):
            continue
        parts = line.split(";")
        # Scheme Code;Scheme Name;ISIN..;ISIN..;NAV;Repurchase;Sale;Date
        if len(parts) < 8 or not parts[0].strip().isdigit():
            continue
        nav, d = _num(parts[4]), _parse_date(parts[7])
        if nav is not None and d is not None:
            out.append((int(parts[0].strip()), d, nav))
    return out
