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

from dataclasses import dataclass
from datetime import date, datetime

from lxml import etree

from equity_research.common.http import fetch_bytes
from equity_research.scrapers.nse_api import fetch_api

_FIN_NS = "http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin"
_XBRLI = "http://www.xbrl.org/2003/instance"


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
#   year-end balance sheet = the *instant* context dated at the year-end. Instants
#   carry reliable single dates (no collision), so we match those by date.
CURRENT_YEAR_CTX = "FourD"
PRIOR_YEAR_CTX = "FiveD"


@dataclass
class ParsedXBRL:
    nature: str | None                              # 'Standalone' / 'Consolidated'
    facts_by_context: dict[str, dict[str, float]]   # context_id -> {element: value}
    # context_id -> (start, end, instant); durations have start/end, stocks have instant.
    context_meta: dict[str, tuple[date | None, date | None, date | None]]

    def current_quarter(self) -> dict[str, float]:
        """Headline numbers for the quarter being reported (the OneD context)."""
        return self.facts_by_context.get(CURRENT_QUARTER_CTX, {})

    def instant_facts(self, on: date) -> dict[str, float]:
        """Balance-sheet facts as of the given instant date (matched by date)."""
        out: dict[str, float] = {}
        for cid, (_, _, inst) in self.context_meta.items():
            if inst == on:
                out.update(self.facts_by_context.get(cid, {}))
        return out


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
        if not isinstance(el.tag, str) or not el.tag.startswith(f"{{{_FIN_NS}}}"):
            continue
        local = etree.QName(el).localname
        if local == "NatureOfReportStandaloneConsolidated" and el.text:
            nature = el.text.strip()
            continue
        ctx_id = el.get("contextRef")
        if ctx_id not in meta or el.text is None:
            continue
        start, end, inst, has_dims = meta[ctx_id]
        if has_dims or (end is None and inst is None):  # skip breakdowns + figureless
            continue
        try:
            value = float(el.text)
        except ValueError:
            continue                          # non-numeric (text disclosures)
        facts.setdefault(ctx_id, {})[local] = value

    cmeta = {cid: (s, e, i) for cid, (s, e, i, _) in meta.items()}
    return ParsedXBRL(nature=nature, facts_by_context=facts, context_meta=cmeta)
