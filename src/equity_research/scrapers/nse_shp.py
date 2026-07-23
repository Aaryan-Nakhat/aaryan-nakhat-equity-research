"""NSE shareholding-pattern (SEBI Reg 31) — holder-level tables from the SHP XBRL.

The quarterly SHP catalog comes from the browser-tier ``/api/corporate-share-holdings-master``
(one row per quarter, carrying promoter/public totals and a direct **XBRL url** on
``nsearchives`` — plain HTTP, no bot wall). The XBRL (BSE ``in-bse-shp`` taxonomy) holds the
full holder tables: every promoter/promoter-group account (Table II) and every public
shareholder above 1% (Table III), each as a typed-dimension member whose **axis names the
holder category** (Individuals/HUF, Mutual Funds, Insurance, FPI, "Others" = bodies
corporate…). Names, share counts and percentages live in sibling contexts joined by the
typed-member value; promoter rows are the members that also carry a
``TypeOfPromoterShareholding`` fact.

This is what surfaces the *Elcid pattern* — a small listed investment company sitting on a
meaningful stake of a giant (Elcid Investments held ~2.95% of Asian Paints).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime
from xml.etree import ElementTree as ET

from equity_research.common.http import fetch_bytes
from equity_research.scrapers.nse_api import fetch_api, q

log = logging.getLogger(__name__)

_MASTER = "/api/corporate-share-holdings-master?index=equities&symbol="

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"

# axis → human category (fallback prettifies the axis name, so new axes never break)
_AXIS_CATEGORY = (
    ("IndividualsOrHUF", "individual / HUF"),
    ("NonResidentIndividuals", "NRI / foreign individual"),
    ("ForeignIndividuals", "NRI / foreign individual"),
    ("MutualFundsOrUTI", "mutual fund"),
    ("InsuranceCompanies", "insurance company"),
    ("ForeignPortfolioInvestor", "FPI"),
    ("FinancialInstitutionsOrBanks", "bank / FI"),
    ("CentralGovernment", "government"),
    ("StateGovernment", "government"),
    ("EmployeeTrusts", "employee trust"),
    ("OthersIndianShareholders", "body corporate / other"),
    ("AnyOther", "body corporate / other"),
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _axis_label(axis: str) -> str:
    for key, label in _AXIS_CATEGORY:
        if key in axis:
            return label
    # unseen axis — prettify "DetailsOfSharesHeldByXyzAxis" → "xyz"
    s = re.sub(r"^Details(OfShares|Shares)HeldBy", "", axis)
    s = re.sub(r"Axis$", "", s)
    return re.sub(r"(?<!^)(?=[A-Z])", " ", s).lower() or "other"


def latest_shp(symbol: str) -> dict | None:
    """Newest SHP catalog row for ``symbol`` →
    ``{as_of, xbrl_url, promoter_pct, public_pct}`` (or None)."""
    try:
        rows = fetch_api(_MASTER + q(symbol))
    except Exception:  # noqa: BLE001 — catalog fetch is best-effort
        log.exception("SHP master fetch failed for %s", symbol)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    r = rows[0]
    url = (r.get("xbrl") or "").strip()
    if not url:
        return None

    def _num(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    as_of = None
    try:
        as_of = datetime.strptime((r.get("date") or "").strip(), "%d-%b-%Y").date()
    except ValueError:
        pass
    return {"as_of": as_of, "xbrl_url": url,
            "promoter_pct": _num(r.get("pr_and_prgrp")), "public_pct": _num(r.get("public_val"))}


def parse_shp_xbrl(raw: bytes) -> list[dict]:
    """Holder rows from an SHP XBRL →
    ``[{name, pct, shares, category, is_promoter}, ...]`` sorted by pct desc.

    Facts for one holder are spread over several contexts sharing a typed-dimension
    member value — join on ``(axis, member)``. Percentages are usually filed as
    fractions (0.0295 = 2.95%); scale adaptively.
    """
    tree = ET.fromstring(raw)

    # context id → (axis, typed-member value)
    ctx_key: dict[str, tuple[str, str]] = {}
    for ctx in tree.iter(f"{_XBRLI}context"):
        for mem in ctx.iter(f"{_XBRLDI}typedMember"):
            inner = list(mem)
            val = (inner[0].text or "").strip() if inner else ""
            ctx_key[ctx.get("id")] = (mem.get("dimension", "").split(":")[-1], val)
            break

    want = {"NameOfTheShareholder", "ShareholdingAsAPercentageOfTotalNumberOfShares",
            "NumberOfFullyPaidUpEquityShareHeld", "TotalNumberOfSharesHeld",
            "TypeOfPromoterShareholding"}
    members: dict[tuple[str, str], dict] = defaultdict(dict)
    for e in tree.iter():
        ln = _local(e.tag)
        cr = e.get("contextRef")
        if ln in want and cr in ctx_key:
            members[ctx_key[cr]].setdefault(ln, (e.text or "").strip())

    rows: list[dict] = []
    for (axis, _mval), f in members.items():
        name = f.get("NameOfTheShareholder")
        if not name:
            continue
        try:
            pct = float(f.get("ShareholdingAsAPercentageOfTotalNumberOfShares") or 0.0)
        except ValueError:
            pct = 0.0
        shares = None
        for k in ("TotalNumberOfSharesHeld", "NumberOfFullyPaidUpEquityShareHeld"):
            try:
                shares = int(float(f[k]))
                break
            except (KeyError, ValueError):
                continue
        rows.append({"name": re.sub(r"\s+", " ", name).strip(), "pct": pct, "shares": shares,
                     "category": _axis_label(axis),
                     "is_promoter": "TypeOfPromoterShareholding" in f})
    if not rows:
        return []
    # fraction → percent: the >1% public table guarantees values >1 when filed as percent
    if max(r["pct"] for r in rows) <= 1.0:
        for r in rows:
            r["pct"] = r["pct"] * 100.0
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def holders(symbol: str) -> dict | None:
    """Latest holder-level shareholding for ``symbol`` →
    ``{as_of, promoter_pct, public_pct, holders: [...]}`` (or None)."""
    meta = latest_shp(symbol)
    if not meta:
        return None
    try:
        rows = parse_shp_xbrl(fetch_bytes(meta["xbrl_url"]))
    except Exception:  # noqa: BLE001 — parse is best-effort
        log.exception("SHP XBRL parse failed for %s (%s)", symbol, meta["xbrl_url"])
        return None
    if not rows:
        return None
    meta["holders"] = rows
    return meta


# ----------------- holder classification (the Elcid finder) -----------------
_SUFFIX = re.compile(r"\b(limited|ltd|private|pvt|llp|and|company|co|the)\b", re.I)


def norm_name(name: str) -> str:
    """Normalize a company name for listed-master matching: case/punct/space-proof
    ('EL CID Investments Limited' == 'Elcid Investments Ltd')."""
    s = _SUFFIX.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def classify(row: dict, listed: dict[str, str]) -> tuple[str, str | None]:
    """(classification, matched_listed_symbol) for a holder row. ``listed`` maps
    ``norm_name(company) → symbol`` for every NSE-listed company."""
    cat = row["category"]
    if cat != "body corporate / other":
        return cat, None
    name = row["name"]
    sym = listed.get(norm_name(name))
    if sym:
        return "LISTED company", sym
    low = name.lower()
    if "private" in low or re.search(r"\bpvt\b", low):
        return "unlisted pvt company", None
    if "llp" in low:
        return "LLP", None
    if "trust" in low:
        return "trust", None
    if re.search(r"\b(limited|ltd)\b", low):
        return "unlisted company", None
    return "body corporate / other", None
