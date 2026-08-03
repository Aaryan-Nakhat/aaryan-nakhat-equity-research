"""Small-cap discovery screener — find strong small-caps *early*, led by the capex cycle.

The thesis (see ``docs/REPORTS.md``): committed **capex leads revenue by 1-2 years**, so a
capex boom is the earliest structural tell — *before* the P&L re-rates. But capex alone is a
trap unless it's productive and funded prudently, so the screen scores five pillars and
**hard-gates the traps** (near-distress, earnings-manipulation flags, heavy promoter pledge,
shrinking revenue) out of the universe entirely.

Universe = every symbol with financials whose **market cap sits in the small-cap band**
(default ₹1,000-10,000 cr) and that discloses ≥2 years of capex. As the small-cap backfill
lands (Nifty Smallcap 250 + Microcap 250 → ``sector_map``), those names auto-appear here — the
band, not an index tag, defines eligibility.

Pillars (each metric rank-normalised to [0,1] within the cohort, so scores are relative to the
peer set that quarter), weights in ``_WEIGHTS``:

- **① Capex cycle** — capex vs its own 3-yr base · capex ÷ depreciation (>1 = expanding, not
  just maintaining) · rising capex/sales intensity · self-funded (CFO covers capex).
- **② Capital efficiency** — ROCE level **and** trend · EBITDA-margin expansion.
- **③ Cash & balance sheet** — CFO/PAT · (low) debt/equity · interest cover.
- **④ Forensic safety** — Altman Z · Beneish M · Sloan accruals · no pledge (reuses ``screener``).
- **⑤ Smart money** — promoter holding · net institutional accumulation (QoQ ownership diff).

Valuation is **deliberately not weighted** (shown for context, with a cheap-vs-own-history read):
forcing "cheap" filters out exactly the quality compounders this screen is meant to catch early.

Pure DB read; reuses the deterministic analysis layer (``valuation``/``forensic``/``ownership``/
``screener``) — nothing re-derives numbers.
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import forensic, ownership, screener, valuation

log = logging.getLogger(__name__)

# Band (₹ crore) that defines "small cap" for this screen.
_BAND_LO, _BAND_HI = 1_000.0, 10_000.0

_WEIGHTS = {"capex": 0.30, "efficiency": 0.25, "cashbs": 0.20, "forensic": 0.15, "smart": 0.10}

# Metrics that make up each pillar (all oriented so higher = better).
_PILLARS = {
    "capex": ["capex_growth", "capex_to_dep", "capex_intensity_delta", "self_funded"],
    "efficiency": ["roce", "roce_trend", "margin_expansion"],
    "cashbs": ["cfo_pat", "neg_de", "int_cover"],
    "forensic": ["forensic_raw"],
    "smart": ["promoter_pct", "inst_flow"],
}
_ALL_METRICS = [m for ms in _PILLARS.values() for m in ms]

_INST_CATS = {"mutual fund", "insurance company", "FPI", "bank / FI"}


def _last(s: pd.Series) -> float | None:
    s = s.dropna()
    return float(s.iloc[-1]) if len(s) else None


def _prior_base(s: pd.Series, n: int = 3) -> float | None:
    """Mean of up to ``n`` years *before* the latest — the base a capex boom is measured against."""
    s = s.dropna()
    if len(s) < 2:
        return None
    base = s.iloc[:-1].tail(n)
    return float(base.mean()) if len(base) else None


def _promoter_pct(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    r = con.execute(
        "SELECT sum(pct) FROM shp_holders WHERE symbol = ? AND is_promoter "
        "AND as_of = (SELECT max(as_of) FROM shp_holders WHERE symbol = ?)",
        [symbol, symbol]).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _inst_flow(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """Net institutional accumulation (pp) last quarter: MF/insurer/FPI/bank entering + adding,
    minus exiting + trimming. Positive = smart money moving in. None if no QoQ diff exists."""
    ch = ownership.ownership_changes(con, symbol)
    if not ch:
        return None
    flow = 0.0
    for r in ch["entered"]:
        if r["category"] in _INST_CATS:
            flow += r.get("pct") or 0.0
    for r in ch["added"]:
        if r["category"] in _INST_CATS:
            flow += r.get("delta") or 0.0
    for r in ch["exited"]:
        if r["category"] in _INST_CATS:
            flow -= r.get("prev_pct") or 0.0
    for r in ch["trimmed"]:
        if r["category"] in _INST_CATS:
            flow -= abs(r.get("delta") or 0.0)
    return flow


def _pledge_pct(con: duckdb.DuckDBPyConnection, symbol: str) -> float:
    r = con.execute(
        "SELECT pledged_pct_of_promoter FROM shareholding WHERE symbol = ? "
        "ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def _metrics(con: duckdb.DuckDBPyConnection, symbol: str,
             lo: float, hi: float) -> dict | None:
    """Raw (un-normalised) metrics + gate decision for one symbol. Returns ``None`` when the
    name is outside the band, lacks the capex history the screen is built on, or is **gated out**
    as a trap (near-distress / manipulation flag / heavy pledge / shrinking revenue)."""
    snap = valuation.snapshot(con, symbol) or {}
    mcap = snap.get("market_cap_cr")
    if mcap is None or not (lo <= mcap <= hi):
        return None

    af = valuation.load_annual(con, symbol, consolidated=False)
    if af is None or af.empty or len(af) < 2:
        return None

    def s(el: str) -> pd.Series:
        return af[el] if el in af.columns else pd.Series(np.nan, index=af.index)

    capex = s("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities").abs()
    if capex.dropna().shape[0] < 2:            # capex is the whole thesis — need a trend
        return None
    dep = s("DepreciationDepletionAndAmortisationExpense")
    cfo = s("CashFlowsFromUsedInOperatingActivities")
    rev = s("RevenueFromOperations")
    pat = s("ProfitLossForPeriod")
    fin = s("FinanceCosts")
    pbt = s("ProfitBeforeTax")
    eq = s("Equity")
    debt = s("BorrowingsCurrent").add(s("BorrowingsNoncurrent"), fill_value=0)
    ebit = pbt + fin
    ebitda = pbt + fin + dep

    # ---- hard gates (exclude traps; a capex boom on a fragile balance sheet is the classic trap) ----
    rev_ok = rev.dropna()
    if len(rev_ok) >= 3 and rev_ok.iloc[-1] < rev_ok.iloc[-3]:     # revenue lower than 2 years ago
        return None
    z = forensic.altman_z(con, symbol)
    if z.value is not None and z.value < 1.81:                     # Altman distress zone
        return None
    m = forensic.beneish_m(con, symbol)
    if m.value is not None and m.value > -1.78:                    # likely earnings manipulator
        return None
    if _pledge_pct(con, symbol) > 25:                             # heavy promoter pledge
        return None

    # ---- ① capex cycle ----
    capex_l, capex_base = _last(capex), _prior_base(capex)
    dep_l, cfo_l = _last(dep), _last(cfo)
    capex_growth = (capex_l / capex_base) if capex_l is not None and capex_base and capex_base > 0 else None
    capex_to_dep = (capex_l / dep_l) if capex_l is not None and dep_l and dep_l > 0 else None
    self_funded = (cfo_l / capex_l) if cfo_l is not None and capex_l and capex_l > 0 else None
    intensity = (capex / rev).replace([np.inf, -np.inf], np.nan)   # capex/sales per year
    int_l, int_base = _last(intensity), _prior_base(intensity)
    capex_intensity_delta = (int_l - int_base) if int_l is not None and int_base is not None else None

    # ---- ② capital efficiency ----
    cap_emp = eq.add(debt, fill_value=0)
    roce_s = (100 * ebit / cap_emp).replace([np.inf, -np.inf], np.nan)
    roce, roce_base = _last(roce_s), _prior_base(roce_s)
    roce_trend = (roce - roce_base) if roce is not None and roce_base is not None else None
    marg_s = (100 * ebitda / rev).replace([np.inf, -np.inf], np.nan)
    marg_l, marg_base = _last(marg_s), _prior_base(marg_s)
    margin_expansion = (marg_l - marg_base) if marg_l is not None and marg_base is not None else None

    # ---- ③ cash & balance sheet ----
    cfo_pat_s = (cfo / pat).replace([np.inf, -np.inf], np.nan)
    cfo_pat = float(cfo_pat_s.dropna().tail(3).mean()) if cfo_pat_s.dropna().shape[0] else None
    eq_l, debt_l = _last(eq), _last(debt)
    de = (debt_l / eq_l) if eq_l and eq_l > 0 and debt_l is not None else None
    int_cover = (_last(ebit) / _last(fin)) if _last(fin) and _last(fin) > 0 else None

    return {
        "symbol": symbol, "mcap": mcap, "pe": snap.get("pe_ttm"), "pb": snap.get("pb"),
        "capex_growth": capex_growth, "capex_to_dep": capex_to_dep,
        "capex_intensity_delta": capex_intensity_delta, "self_funded": self_funded,
        "roce": roce, "roce_trend": roce_trend, "margin_expansion": margin_expansion,
        "cfo_pat": cfo_pat, "neg_de": (-de if de is not None else None), "int_cover": int_cover,
        "forensic_raw": screener._forensic_raw(con, symbol),
        "promoter_pct": _promoter_pct(con, symbol), "inst_flow": _inst_flow(con, symbol),
        "cheapness": screener._cheapness_raw(con, symbol),      # display only, not weighted
    }


def smallcap_screen(con: duckdb.DuckDBPyConnection, *, limit: int = 25,
                    lo: float = _BAND_LO, hi: float = _BAND_HI) -> list[dict]:
    """Ranked capex-led small-cap screen over symbols with financials in the ``[lo, hi]`` ₹cr band.

    Returns ``[{symbol, name, composite, mcap, pe, pb, capex_x, roce, why, …}, …]`` best-first.
    Best-effort: a thin name is scored on the pillars it has; a trap is gated out entirely.
    """
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials").fetchall()]
    names = dict(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    rows: list[dict] = []
    for sym in syms:
        try:
            met = _metrics(con, sym, lo, hi)
        except Exception:  # noqa: BLE001 — one thin/odd name must not break the screen
            log.exception("smallcap scoring failed for %s", sym)
            continue
        if met is None:
            continue
        met["name"] = names.get(sym, sym)
        rows.append(met)
    if not rows:
        return []

    for key in _ALL_METRICS:
        screener._normalise(rows, key)
    for r in rows:
        pillars = {p: float(np.mean([r[m + "_n"] for m in ms])) for p, ms in _PILLARS.items()}
        r["pillars"] = pillars
        r["composite"] = round(100 * sum(_WEIGHTS[p] * v for p, v in pillars.items()), 1)
        r["why"] = _why(r)
    rows.sort(key=lambda r: (-r["composite"], r["symbol"]))   # deterministic tie-break for stable ranks
    return rows[:limit]


def _why(r: dict) -> str:
    bits = []
    if r.get("capex_growth") is not None:
        bits.append(f"capex {r['capex_growth']:.1f}× its 3y base")
    if r.get("capex_to_dep") is not None:
        bits.append(f"capex/depr {r['capex_to_dep']:.1f}×")
    if r.get("roce") is not None:
        bits.append(f"ROCE {r['roce']:.0f}%"
                    + (f" (↑{r['roce_trend']:.0f}pp)" if r.get("roce_trend") and r["roce_trend"] > 0 else ""))
    if r.get("cfo_pat") is not None:
        bits.append(f"CFO/PAT {r['cfo_pat']:.1f}")
    if r.get("inst_flow") is not None and r["inst_flow"] > 0:
        bits.append(f"institutions +{r['inst_flow']:.1f}pp")
    return " · ".join(bits)
