"""NSE financial-results scraper — structured fundamentals (the Phase-2 source).

Two steps, validated in ``docs/FUNDAMENTALS.md``:

1. **Catalog** (browser tier): ``/api/corporates-financial-results`` lists every
   result filing for a symbol with period metadata + a direct ``xbrl`` URL.
2. **XBRL** (plain HTTP): each filing's XBRL lives on ``nsearchives.nseindia.com``
   and carries standardised ``in-bse-fin:*`` facts with exact numbers.

XBRL context convention (BSE taxonomy): contexts are numbered periods (OneD =
current quarter, FourD = YTD, etc.); ``NatureOfReportStandaloneConsolidated``
states the file's nature. A filing's headline numbers = the dimensionless
context whose start/end match the filing's declared period.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from lxml import etree

from equity_research.scrapers.nse_api import fetch_api

_XBRLI = "http://www.xbrl.org/2003/instance"


def _fin_local(tag: object) -> str | None:
    """Local name if ``tag`` is a recognised financial-results element, else None.

    Accepts both taxonomies (version-agnostically, so future revisions still parse):
    - legacy result XBRL: ``.../xbrl/fin/<date>/in-bse-fin`` (pre-Dec-2024 filings);
    - SEBI Integrated Filing: ``.../sebi.gov.in/xbrl/<date>/in-capmkt`` (the new
      regime from the Dec-2024 quarter on). Both use identical local names and the
      same OneD/FourD/OneI context convention, so downstream is unchanged.
    """
    if not isinstance(tag, str) or not tag.startswith("{"):
        return None
    uri, local = tag[1:].split("}", 1)
    if "bseindia.com/xbrl/fin/" in uri and uri.endswith("in-bse-fin"):
        return local
    if "sebi.gov.in/xbrl/" in uri and uri.endswith("in-capmkt"):
        return local
    return None


@dataclass(frozen=True)
class Filing:
    symbol: str
    company: str
    consolidated: bool
    audited: bool
    financial_year: str
    from_date: date | None
    to_date: date | None
    filing_date: date | None
    xbrl_url: str


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def list_result_filings(symbol: str, period: str = "Quarterly") -> list[Filing]:
    """All result filings for ``symbol`` (period = ``Quarterly`` or ``Annual``)."""
    rows = fetch_api(
        f"/api/corporates-financial-results?index=equities&symbol={symbol}&period={period}"
    )
    out: list[Filing] = []
    for r in rows:
        out.append(Filing(
            symbol=r.get("symbol", symbol),
            company=r.get("companyName", ""),
            consolidated=str(r.get("consolidated", "")).strip().lower() == "consolidated",
            audited=str(r.get("audited", "")).strip().lower() == "audited",
            financial_year=r.get("financialYear", ""),
            from_date=_parse_date(r.get("fromDate")),
            to_date=_parse_date(r.get("toDate")),
            filing_date=_parse_date(r.get("filingDate") or r.get("broadCastDate")),
            xbrl_url=r.get("xbrl", ""),
        ))
    return out


def list_integrated_filings(symbol: str, period: str = "Quarterly") -> list[Filing]:
    """Result filings under SEBI's **Integrated Filing** regime (the Dec-2024-quarter
    cutover that froze the legacy ``corporates-financial-results`` feed).

    Hits ``/api/integrated-filing-results``; keeps only the *financial* rows (drops
    the parallel Governance filings) and maps them onto the same ``Filing`` shape, so
    the rest of the pipeline treats them identically. Never raises (``[]`` on error)."""
    try:
        resp = fetch_api(
            f"/api/integrated-filing-results?index=equities&symbol={symbol}&period={period}")
    except Exception:  # noqa: BLE001
        return []
    rows = resp.get("data") if isinstance(resp, dict) else None
    out: list[Filing] = []
    for r in rows or []:
        if str(r.get("type", "")).strip().lower() != "integrated filing- financials":
            continue                                   # skip Governance / other integrated rows
        xbrl = (r.get("xbrl") or "").strip()
        cons = (r.get("consolidated") or "").strip().lower()
        if not xbrl or cons not in ("consolidated", "standalone"):
            continue
        out.append(Filing(
            symbol=r.get("symbol", symbol),
            company=r.get("cmName", ""),
            consolidated=cons == "consolidated",
            audited=str(r.get("audited", "")).strip().lower() == "audited",
            financial_year="",
            from_date=None,
            to_date=_parse_date(r.get("qe_Date")),
            filing_date=_parse_date(r.get("broadcast_Date")),
            xbrl_url=xbrl,
        ))
    return out


def list_all_result_filings(symbol: str, period: str = "Quarterly") -> list[Filing]:
    """Legacy + Integrated-Filing result filings merged into one newest-first list.

    The legacy feed carries history up to the Dec-2024 quarter; Integrated Filing
    carries everything after. For ``period="Annual"`` the integrated side uses the
    **fiscal year-end (31-Mar) filings**, whose ``FourD``/``OneI`` contexts hold the
    full-year P&L + cash flow + year-end balance sheet (same as a legacy annual
    filing). Deduped by ``(to_date, consolidated)``, preferring the later-broadcast
    row (so a revision or the integrated copy wins), newest-first."""
    integ = list_integrated_filings(symbol, "Quarterly")     # all integrated financial filings
    if period == "Annual":
        integ = [f for f in integ if f.to_date and (f.to_date.month, f.to_date.day) == (3, 31)]
    merged = integ + list_result_filings(symbol, period)
    best: dict[tuple, Filing] = {}
    for f in merged:
        if not f.to_date:
            continue
        key = (f.to_date, f.consolidated)
        cur = best.get(key)
        if cur is None or (f.filing_date or date.min) > (cur.filing_date or date.min):
            best[key] = f
    return sorted(best.values(), key=lambda f: f.to_date, reverse=True)


# BSE result-XBRL context-ID convention (in-bse-fin taxonomy). The contexts'
# declared start/end dates are unreliable (OneD and FourD share dates), so the
# *period* a fact belongs to is encoded by the context ID, not its dates:
#   OneD   = current quarter (3 months)        FourD = current year-to-date
#   TwoD   = preceding quarter                 FiveD = prior-year YTD
#   ThreeD = year-ago quarter                  SixD  = prior full year
# We store the current quarter (OneD); annual series are derived from quarters.
CURRENT_QUARTER_CTX = "OneD"


# Annual-filing context convention (in addition to the quarterly map above):
#   FourD = current full year (P&L + cash flow)   FiveD = prior full year
#   OneI  = current year-end balance sheet         TwoI  = prior year-end
# We select by context ID, not dates. Older (pre-FY2023) result XBRLs are
# "_WEB.xml" variants that *reference* these plain headline contexts but define
# their dates in a companion file — so the contexts look "orphaned" here. Since
# we go by ID convention anyway, we keep facts on those plain contexts even when
# undefined. (Those older files carry P&L/cash-flow but no balance sheet.)
CURRENT_YEAR_CTX = "FourD"
PRIOR_YEAR_CTX = "FiveD"
CURRENT_BALANCE_SHEET_CTX = "OneI"

# Plain headline context ids: One..Ten + D (duration) or I (instant), nothing else.
_PLAIN_CTX = re.compile(
    r"^(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)[DI]$")


@dataclass
class ParsedXBRL:
    nature: str | None                              # 'Standalone' / 'Consolidated'
    facts_by_context: dict[str, dict[str, float]]   # context_id -> {element: value}
    # context_id -> (start, end, instant); durations have start/end, stocks have instant.
    context_meta: dict[str, tuple[date | None, date | None, date | None]]

    def current_quarter(self) -> dict[str, float]:
        """Headline numbers for the quarter being reported (the OneD context)."""
        return self.facts_by_context.get(CURRENT_QUARTER_CTX, {})

    def current_balance_sheet(self) -> dict[str, float]:
        """Year-end balance-sheet facts (the OneI context). Empty for older
        result XBRLs, which omit the balance sheet."""
        return self.facts_by_context.get(CURRENT_BALANCE_SHEET_CTX, {})


def parse_result_xbrl(raw: bytes) -> ParsedXBRL:
    """Parse a result XBRL into per-context dicts of standardised numeric facts.

    Keeps dimensionless facts only (headline statements, not by-axis breakdowns),
    both duration (P&L / cash flow) and instant (balance sheet). Facts are keyed
    by context ID — see the convention notes on why duration dates can't be
    trusted (instant dates can).
    """
    root = etree.fromstring(raw)

    # Index contexts: id -> (start, end, instant, has_dimensions).
    meta: dict[str, tuple[date | None, date | None, date | None, bool]] = {}
    for ctx in root.findall(f"{{{_XBRLI}}}context"):
        per = ctx.find(f"{{{_XBRLI}}}period")
        start = end = inst = None
        if per is not None:
            start = _parse_date(per.findtext(f"{{{_XBRLI}}}startDate"))
            end = _parse_date(per.findtext(f"{{{_XBRLI}}}endDate"))
            inst = _parse_date(per.findtext(f"{{{_XBRLI}}}instant"))
        has_dims = ctx.find(".//{http://xbrl.org/2006/xbrldi}explicitMember") is not None
        meta[ctx.get("id")] = (start, end, inst, has_dims)

    nature: str | None = None
    facts: dict[str, dict[str, float]] = {}
    for el in root.iter():
        local = _fin_local(el.tag)
        if local is None:
            continue
        if local == "NatureOfReportStandaloneConsolidated" and el.text:
            nature = el.text.strip()
            continue
        ctx_id = el.get("contextRef")
        if el.text is None:
            continue
        if ctx_id in meta:
            _, end, inst, has_dims = meta[ctx_id]
            if has_dims or (end is None and inst is None):  # skip breakdowns + figureless
                continue
        elif not _PLAIN_CTX.match(ctx_id or ""):
            continue                          # orphan + not a plain headline ctx
        try:
            value = float(el.text)
        except ValueError:
            continue                          # non-numeric (text disclosures)
        facts.setdefault(ctx_id, {})[local] = value

    cmeta = {cid: (s, e, i) for cid, (s, e, i, _) in meta.items()}
    return ParsedXBRL(nature=nature, facts_by_context=facts, context_meta=cmeta)
