"""Quantitative valuation & statistical forensics.

Assumption-driven, transparent, numpy-only (no scipy). Everything is anchored to
the company's *own* history from the `financials` / `equity_eod` / `index_close`
tables — never invented. The value of the Monte-Carlo work is the **distribution
and sensitivity**, not false precision; banks/NBFCs (where an FCFF-DCF is
inappropriate) are flagged so callers show a caveat instead of a bogus number.

Pieces:
  - dcf_inputs()      derive base FCFF-DCF drivers + WACC (CAPM, beta from prices)
  - monte_carlo_dcf() intrinsic value/share distribution, margin of safety, P(undervalued)
  - reverse_dcf()     growth implied by today's price (bisection)
  - scenario_dcf()    bear / base / bull point values
  - benford()         first-digit conformity of reported figures (manipulation tell)
  - sector_zscores()  target ratios vs peer mean/std (how many sigma off)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import sector, valuation
from equity_research.analysis.fundamentals import load_annual, ttm

CR = 1e7
_MARKET_INDEX = "Nifty 50"
_PROJ_YEARS = 10


# --------------------------------------------------------------------------- #
# DCF inputs
# --------------------------------------------------------------------------- #
@dataclass
class DcfInputs:
    symbol: str
    rev0: float | None = None            # base annual revenue (₹)
    growth: float | None = None          # base revenue growth (decimal)
    growth_sigma: float = 0.05
    ebit_margin: float | None = None     # base EBIT margin (decimal)
    margin_sigma: float = 0.03
    tax_rate: float = 0.25
    da_pct: float = 0.0                  # D&A / revenue
    capex_pct: float = 0.0               # capex / revenue
    nwc_pct: float = 0.0                 # net working capital / revenue
    wacc: float | None = None
    terminal_growth: float = 0.05
    net_debt: float = 0.0                # ₹
    shares: float | None = None          # count
    price: float | None = None           # current ₹/share
    beta: float | None = None
    is_financial: bool = False
    missing: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def usable(self) -> bool:
        return (self.rev0 is not None and self.shares not in (None, 0)
                and self.ebit_margin is not None and self.wacc is not None
                and not self.is_financial)


def _series(af: pd.DataFrame, el: str) -> pd.Series:
    return af[el] if el in af.columns else pd.Series(np.nan, index=af.index)


def _beta(con: duckdb.DuckDBPyConnection, symbol: str, lookback: int = 500) -> float | None:
    """Beta vs Nifty 50 from ~2y of daily returns (cov/var). None if insufficient."""
    px = con.execute(
        """SELECT trade_date, close FROM equity_eod
           WHERE symbol = ? AND series = 'EQ' ORDER BY trade_date DESC LIMIT ?""",
        [symbol, lookback]).df()
    idx = con.execute(
        """SELECT trade_date, close FROM index_close
           WHERE index_name = ? ORDER BY trade_date DESC LIMIT ?""",
        [_MARKET_INDEX, lookback]).df()
    if len(px) < 60 or len(idx) < 60:
        return None
    m = px.merge(idx, on="trade_date", suffixes=("_s", "_i")).sort_values("trade_date")
    rs = m["close_s"].pct_change().to_numpy()
    ri = m["close_i"].pct_change().to_numpy()
    mask = np.isfinite(rs) & np.isfinite(ri)
    rs, ri = rs[mask], ri[mask]
    if len(rs) < 60 or np.var(ri) == 0:
        return None
    return float(np.cov(rs, ri)[0, 1] / np.var(ri))


def dcf_inputs(con: duckdb.DuckDBPyConnection, symbol: str,
               consolidated: bool = False, *,
               shares_override: float | None = None,
               risk_free: float | None = None, erp: float = 0.055) -> DcfInputs:
    """Derive FCFF-DCF base drivers + WACC from the company's history."""
    inp = DcfInputs(symbol=symbol)
    af = load_annual(con, symbol, consolidated)
    if af.empty:
        inp.missing.append("<no annual data>")
        return inp

    rev = _series(af, "RevenueFromOperations").dropna()
    pbt = _series(af, "ProfitBeforeTax")
    fin = _series(af, "FinanceCosts")
    dep = _series(af, "DepreciationDepletionAndAmortisationExpense")
    tax = _series(af, "TaxExpense")
    capex = _series(af, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities").abs()
    ca = _series(af, "CurrentAssets")
    cl = _series(af, "CurrentLiabilities")
    cash = _series(af, "CashAndCashEquivalents")
    bc = _series(af, "BorrowingsCurrent")
    ebit = pbt + fin

    # base revenue: prefer TTM, else latest annual
    t = ttm(con, symbol, consolidated)
    inp.rev0 = (t.get("ttm_revenue_cr") or np.nan) * CR
    if not (inp.rev0 and inp.rev0 == inp.rev0):
        inp.rev0 = float(rev.iloc[-1]) if len(rev) else None
    if inp.rev0 is None:
        inp.missing.append("RevenueFromOperations")

    # historical revenue growth + volatility
    if len(rev) >= 2:
        yrs = (rev.index[-1] - rev.index[0]).days / 365.25
        if yrs > 0 and rev.iloc[0] > 0:
            inp.growth = float((rev.iloc[-1] / rev.iloc[0]) ** (1 / yrs) - 1)
        yoy = (rev / rev.shift(1) - 1).dropna()
        if len(yoy):
            inp.growth_sigma = float(np.clip(np.std(yoy), 0.02, 0.08))
    inp.growth = float(np.clip(inp.growth if inp.growth is not None else 0.08, -0.05, 0.25))

    # margins / intensity ratios (means over available years, revenue-weighted-ish)
    def pct(num: pd.Series) -> float | None:
        r = (num / rev).replace([np.inf, -np.inf], np.nan).dropna()
        return float(r.tail(5).mean()) if len(r) else None

    inp.ebit_margin = pct(ebit)
    if inp.ebit_margin is None:
        inp.missing.append("EBIT margin")
    else:
        ms = (ebit / rev).replace([np.inf, -np.inf], np.nan).dropna()
        inp.margin_sigma = float(np.clip(np.std(ms), 0.01, 0.06)) if len(ms) > 1 else 0.03
    inp.da_pct = pct(dep) or 0.0
    inp.capex_pct = pct(capex) or 0.0
    nwc = (ca - cash) - (cl - bc.fillna(0))
    inp.nwc_pct = pct(nwc) or 0.0

    tr = (tax / pbt).replace([np.inf, -np.inf], np.nan).dropna()
    inp.tax_rate = float(np.clip(tr.tail(5).mean(), 0.10, 0.35)) if len(tr) else 0.25

    # balance-sheet position (latest)
    debt = (_series(af, "BorrowingsCurrent").fillna(0)
            + _series(af, "BorrowingsNoncurrent").fillna(0))
    debt_latest = float(debt.iloc[-1]) if len(debt) else 0.0
    cash_latest = float(cash.iloc[-1]) if len(cash.dropna()) else 0.0
    inp.net_debt = debt_latest - cash_latest

    inp.shares = shares_override or valuation.shares_outstanding(af.loc[af.index[-1]])
    if inp.shares in (None, 0):
        inp.missing.append("shares outstanding")

    snap = valuation.snapshot(con, symbol, consolidated, shares_override=shares_override)
    inp.price = snap.get("price")
    mcap = (snap.get("market_cap_cr") or np.nan) * CR

    # WACC via CAPM
    rf = risk_free if risk_free is not None else float(os.environ.get("RISK_FREE_RATE", "0.07"))
    inp.beta = _beta(con, symbol)
    beta = inp.beta if inp.beta is not None else 1.0
    if inp.beta is None:
        inp.note = (inp.note + " beta unavailable→1.0.").strip()
    cost_equity = rf + beta * erp
    fin_latest = float(fin.iloc[-1]) if len(fin.dropna()) else 0.0
    cost_debt = (fin_latest / debt_latest) * (1 - inp.tax_rate) if debt_latest > 0 else 0.0
    if mcap == mcap and (mcap + debt_latest) > 0:
        we, wd = mcap / (mcap + debt_latest), debt_latest / (mcap + debt_latest)
        inp.wacc = float(np.clip(we * cost_equity + wd * cost_debt, 0.08, 0.18))
    else:
        inp.wacc = float(np.clip(cost_equity, 0.08, 0.18))
    inp.terminal_growth = float(min(0.05, inp.wacc - 0.02))

    # FCFF-DCF is wrong for lenders — flag (industry tag or no capex+revenue cost base)
    ind = (sector.industry_of(con, symbol) or "").lower()
    if any(w in ind for w in ("bank", "financ", "nbfc", "insur", "capital market")):
        inp.is_financial = True
        inp.note = (inp.note + " FCFF-DCF not meaningful for financials.").strip()
    return inp


# --------------------------------------------------------------------------- #
# DCF engine (vectorised — g/margin/wacc/tg may be scalars or ndarrays)
# --------------------------------------------------------------------------- #
def _value_per_share(inp: DcfInputs, g, ebit_margin, wacc, tg):
    """FCFF-DCF per share. Revenue growth *fades* linearly from ``g`` (year 1) to
    the terminal rate ``tg`` (year N) — a 2-stage shape that avoids unrealistic
    constant compounding — and the Gordon denominator (wacc−tg) is floored at 3%
    so terminal value can't explode. ``g/ebit_margin/wacc/tg`` may be ndarrays."""
    rev0, tax = inp.rev0, inp.tax_rate
    g, tg = np.asarray(g, dtype=float), np.asarray(tg, dtype=float)
    pv = np.zeros(np.broadcast(g, ebit_margin, wacc, tg).shape)
    rev_prev = np.full_like(pv, float(rev0))
    fcff_t = np.zeros_like(pv)
    for tyr in range(1, _PROJ_YEARS + 1):
        g_t = g + (tg - g) * (tyr - 1) / (_PROJ_YEARS - 1)   # fade g -> tg
        rev_t = rev_prev * (1 + g_t)
        nopat = rev_t * ebit_margin * (1 - tax)
        fcff_t = nopat + rev_t * inp.da_pct - rev_t * inp.capex_pct - inp.nwc_pct * (rev_t - rev_prev)
        pv = pv + fcff_t / (1 + wacc) ** tyr
        rev_prev = rev_t
    tv = fcff_t * (1 + tg) / np.maximum(wacc - tg, 0.03)
    pv = pv + tv / (1 + wacc) ** _PROJ_YEARS
    equity = pv - inp.net_debt
    return equity / inp.shares


@dataclass
class McResult:
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    mean: float | None = None
    price: float | None = None
    margin_of_safety: float | None = None   # (median − price)/median
    prob_undervalued: float | None = None    # P(value > price)
    samples: np.ndarray | None = None
    note: str = ""


def monte_carlo_dcf(inp: DcfInputs, n: int = 20000, seed: int = 42) -> McResult:
    """Distribution of intrinsic value/share by sampling the key drivers."""
    if not inp.usable:
        return McResult(price=inp.price, note=inp.note or "inputs unavailable for DCF")
    rng = np.random.default_rng(seed)
    g = rng.normal(inp.growth, inp.growth_sigma, n).clip(-0.10, 0.30)
    margin = rng.normal(inp.ebit_margin, inp.margin_sigma, n).clip(0.01, 0.60)
    wacc = rng.normal(inp.wacc, 0.01, n).clip(0.07, 0.20)
    tg = rng.normal(inp.terminal_growth, 0.005, n).clip(0.02, 0.06)
    vals = _value_per_share(inp, g, margin, wacc, tg)   # (wacc−tg) floored inside
    vals = vals[np.isfinite(vals) & (vals > 0)]
    if len(vals) < n * 0.5:
        return McResult(price=inp.price, note="DCF unstable (too many invalid draws)")
    median = float(np.median(vals))
    res = McResult(
        median=median, p10=float(np.percentile(vals, 10)),
        p90=float(np.percentile(vals, 90)), mean=float(np.mean(vals)),
        price=inp.price, samples=vals, note=inp.note,
    )
    if inp.price:
        res.margin_of_safety = (median - inp.price) / median
        res.prob_undervalued = float(np.mean(vals > inp.price))
    return res


def reverse_dcf(inp: DcfInputs) -> dict:
    """Constant revenue growth implied by today's price (bisection)."""
    if not inp.usable or not inp.price:
        return {"note": inp.note or "inputs unavailable"}
    lo, hi = -0.20, 0.50

    def f(gg):
        return float(_value_per_share(inp, gg, inp.ebit_margin, inp.wacc, inp.terminal_growth)) - inp.price

    if f(lo) > 0:
        return {"implied_growth": None, "note": "price below even the bear case (deeply undervalued on DCF)"}
    if f(hi) < 0:
        return {"implied_growth": None, "note": "price implies >50% perpetual growth (richly valued)"}
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    implied = (lo + hi) / 2
    return {"implied_growth": implied, "historical_growth": inp.growth,
            "plausible": implied <= (inp.growth or 0) + 0.03}


def scenario_dcf(inp: DcfInputs) -> dict:
    """Bear / base / bull point fair values with explicit assumption deltas."""
    if not inp.usable:
        return {"note": inp.note or "inputs unavailable"}
    g, mg, w = inp.growth, inp.ebit_margin, inp.wacc
    gs, ms = inp.growth_sigma, inp.margin_sigma
    out = {}
    for name, (dg, dm, dw) in {
        "bear": (-gs, -ms, +0.01), "base": (0, 0, 0), "bull": (+gs, +ms, -0.01),
    }.items():
        out[name] = float(_value_per_share(
            inp, max(g + dg, -0.10), max(mg + dm, 0.01),
            float(np.clip(w + dw, 0.07, 0.20)), inp.terminal_growth))
    out["price"] = inp.price
    return out


# --------------------------------------------------------------------------- #
# Benford's law (first-digit conformity of reported figures)
# --------------------------------------------------------------------------- #
_BENFORD = np.log10(1 + 1 / np.arange(1, 10))   # expected P(d) for d=1..9


def benford(con: duckdb.DuckDBPyConnection, symbol: str) -> dict:
    """First-digit test over all reported financial values for the symbol.

    Returns observed vs expected proportions, Nigrini MAD, and a conformity
    verdict. Manipulation/rounding tends to break Benford conformity.
    """
    vals = [r[0] for r in con.execute(
        "SELECT value FROM financials WHERE symbol = ? AND value IS NOT NULL", [symbol]).fetchall()]
    digits = []
    for v in vals:
        a = abs(float(v))
        if a >= 1:                       # ignore 0 and sub-1 (digit ill-defined)
            digits.append(int(str(a).lstrip("0.")[0]))
    digits = [d for d in digits if 1 <= d <= 9]
    n = len(digits)
    if n < 50:
        return {"n": n, "note": "too few data points for a Benford test (<50)"}
    obs = np.array([digits.count(d) for d in range(1, 10)], dtype=float) / n
    mad = float(np.mean(np.abs(obs - _BENFORD)))
    # Nigrini MAD thresholds (first digit)
    verdict = ("close conformity" if mad < 0.006 else
               "acceptable conformity" if mad < 0.012 else
               "marginal conformity" if mad < 0.015 else "nonconformity")
    chi2 = float(n * np.sum((obs - _BENFORD) ** 2 / _BENFORD))
    return {"n": n, "mad": mad, "chi2": chi2, "verdict": verdict,
            "observed": obs.tolist(), "expected": _BENFORD.tolist(),
            "flag": mad >= 0.015}


# --------------------------------------------------------------------------- #
# Sector-relative z-scores
# --------------------------------------------------------------------------- #
def _ratios(con, symbol, consolidated) -> dict[str, float]:
    """Current key ratios for one symbol (for peer comparison)."""
    out: dict[str, float] = {}
    snap = valuation.snapshot(con, symbol, consolidated)
    if snap.get("pe_ttm") == snap.get("pe_ttm"):
        out["P/E"] = snap.get("pe_ttm")
    if snap.get("pb") == snap.get("pb"):
        out["P/B"] = snap.get("pb")
    af = load_annual(con, symbol, consolidated)
    if not af.empty:
        row = af.loc[af.index[-1]]
        def g(el):
            v = row.get(el)
            return float(v) if v is not None and v == v else None
        pat, eq, rev = g("ProfitLossForPeriod"), g("Equity"), g("RevenueFromOperations")
        pbt, fin = g("ProfitBeforeTax"), g("FinanceCosts")
        debt = (g("BorrowingsCurrent") or 0) + (g("BorrowingsNoncurrent") or 0)
        if pat and eq:
            out["ROE%"] = 100 * pat / eq
        if pbt is not None and fin is not None and eq:
            out["ROCE%"] = 100 * (pbt + fin) / (eq + debt) if (eq + debt) else None
        if pat and rev:
            out["NetMargin%"] = 100 * pat / rev
        if eq:
            out["D/E"] = debt / eq
    return {k: v for k, v in out.items() if v is not None and v == v}


# Plausible per-ratio bounds — drop pathological peers (e.g. holding cos with
# PAT > revenue, or near-zero-equity outliers) so they don't distort mean/std.
_SANE = {"P/E": (0, 150), "P/B": (0, 50), "ROE%": (-100, 100),
         "ROCE%": (-100, 100), "NetMargin%": (-100, 100), "D/E": (0, 20)}


def sector_zscores(con: duckdb.DuckDBPyConnection, symbol: str,
                   consolidated: bool = False) -> dict:
    """Target ratios vs peer mean/std (z = (target−mean)/std). Needs ≥3 peers."""
    pl = sector.peers(con, symbol)
    if not pl:
        return {"note": "no sector peers (ingest_sector_map first)"}
    target = _ratios(con, symbol, consolidated)
    peer_vals: dict[str, list[float]] = {k: [] for k in target}
    for p in pl:
        pr = _ratios(con, p, consolidated)
        for k, v in pr.items():
            lo, hi = _SANE.get(k, (-np.inf, np.inf))
            if k in peer_vals and lo <= v <= hi:
                peer_vals[k].append(v)
    rows = {}
    for k, tv in target.items():
        vals = np.array(peer_vals.get(k, []), dtype=float)
        if len(vals) >= 3 and np.std(vals) > 0:
            rows[k] = {"value": tv, "peer_mean": float(np.mean(vals)),
                       "peer_std": float(np.std(vals)), "n": int(len(vals)),
                       "z": float((tv - np.mean(vals)) / np.std(vals))}
    return {"industry": sector.industry_of(con, symbol), "ratios": rows} if rows else \
        {"industry": sector.industry_of(con, symbol), "note": "not enough peers with data"}
