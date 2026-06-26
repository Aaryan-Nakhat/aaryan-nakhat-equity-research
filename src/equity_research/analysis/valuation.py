"""Valuation — multiples vs the company's own history.

Joins annual financials (`financials`, period_type='Y') with prices
(`equity_eod`). Market cap is computed per period from **contemporaneous shares**
(that year's `EquityShareCapital` / face value) × the period's price, which makes
P/E and P/B bonus/split-invariant and comparable across time.

Caveat on the *current* snapshot: shares come from the latest annual filing, so a
corporate action since then (bonus/split/buyback) makes it stale — surfaced in
the output; pass ``shares_override`` to correct it.
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis.fundamentals import load_annual, ttm

CR = 1e7


def shares_outstanding(annual_row) -> float | None:
    """Shares = EquityShareCapital / face value (both in rupees)."""
    cap = annual_row.get("EquityShareCapital")
    fv = annual_row.get("FaceValueOfEquityShareCapital")
    if cap is None or not fv or cap != cap or fv != fv:
        return None
    return cap / fv


def _price_on_or_before(con, symbol: str, d: date) -> tuple[date, float] | None:
    row = con.execute(
        """SELECT trade_date, close FROM equity_eod
           WHERE symbol = ? AND series = 'EQ' AND trade_date <= ?
           ORDER BY trade_date DESC LIMIT 1""",
        [symbol, d],
    ).fetchone()
    return (row[0], row[1]) if row else None


def valuation_history(con: duckdb.DuckDBPyConnection, symbol: str,
                      consolidated: bool = False) -> pd.DataFrame:
    """Per-fiscal-year P/E and P/B at each year-end (contemporaneous shares)."""
    a = load_annual(con, symbol, consolidated)
    if a.empty:
        return pd.DataFrame()
    out = []
    for period_end in a.index:
        row = a.loc[period_end]
        sh = shares_outstanding(row)
        px = _price_on_or_before(con, symbol, period_end)
        if sh is None or px is None:
            continue
        price_date, price = px
        mcap = sh * price
        net = row.get("ProfitLossForPeriod")
        eq = row.get("Equity")
        out.append({
            "fy_end": period_end,
            "price_date": price_date,
            "price": price,
            "shares_cr": sh / CR,
            "mcap_cr": mcap / CR,
            "pe": mcap / net if net and net == net else np.nan,
            "pb": mcap / eq if eq and eq == eq else np.nan,
        })
    return pd.DataFrame(out).set_index("fy_end") if out else pd.DataFrame()


def snapshot(con: duckdb.DuckDBPyConnection, symbol: str,
             consolidated: bool = False, *,
             shares_override: float | None = None) -> dict:
    """Current valuation multiples (TTM earnings, latest price)."""
    a = load_annual(con, symbol, consolidated)
    px = con.execute(
        """SELECT trade_date, close FROM equity_eod
           WHERE symbol = ? AND series = 'EQ' ORDER BY trade_date DESC LIMIT 1""",
        [symbol],
    ).fetchone()
    if a.empty or px is None:
        return {}
    latest = a.loc[a.index[-1]]
    sh = shares_override or shares_outstanding(latest)
    if sh is None:
        return {"note": "shares outstanding unavailable"}
    price_date, price = px
    mcap = sh * price
    t = ttm(con, symbol, consolidated)
    ttm_net = (t.get("ttm_net_profit_cr") or np.nan) * CR
    eq = latest.get("Equity")
    note = ("shares from latest annual (FY-end %s) - unadjusted for any later "
            "bonus/split; pass shares_override to correct" % a.index[-1].year)
    return {
        "price": price,
        "price_date": price_date,
        "shares_cr": sh / CR,
        "market_cap_cr": mcap / CR,
        "pe_ttm": mcap / ttm_net if ttm_net == ttm_net and ttm_net else np.nan,
        "pb": mcap / eq if eq and eq == eq else np.nan,
        "earnings_yield_%": 100 * ttm_net / mcap if ttm_net == ttm_net else np.nan,
        "note": note if shares_override is None else "",
    }


def market_cap(con: duckdb.DuckDBPyConnection, symbol: str,
               consolidated: bool = False, *,
               shares_override: float | None = None) -> float | None:
    """Current market cap in rupees (for Altman X4 etc.)."""
    s = snapshot(con, symbol, consolidated, shares_override=shares_override)
    mc = s.get("market_cap_cr")
    return mc * CR if mc is not None else None


def _hist_ebitda_margins(a: pd.DataFrame) -> list[float]:
    """EBITDA margin (%) for each annual year (EBITDA = PBT + finance cost + D&A)."""
    out: list[float] = []
    if a is None or a.empty:
        return out
    for _, row in a.iterrows():
        def g(el):
            v = row.get(el)
            return float(v) if v is not None and v == v else None
        rev = g("RevenueFromOperations")
        pbt = g("ProfitBeforeTax")
        if not rev or pbt is None:
            continue
        ebitda = pbt + (g("FinanceCosts") or 0) + (g("DepreciationDepletionAndAmortisationExpense") or 0)
        m = 100 * ebitda / rev
        if -50 <= m <= 90:                                  # drop pathological years
            out.append(m)
    return out


def ev_ebitda(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool = False, *,
              shares_override: float | None = None) -> dict:
    """EV/EBITDA on TTM EBITDA, plus a **mid-cycle** variant (avg historical EBITDA
    margin × TTM revenue) so peak/trough cyclicals aren't mis-valued.

    EV = market cap + net debt (borrowings − cash, latest annual). ``{}`` if the
    inputs (mcap, ≥4-quarter TTM EBITDA) aren't available."""
    snap = snapshot(con, symbol, consolidated, shares_override=shares_override)
    t = ttm(con, symbol, consolidated)
    mcap_cr = snap.get("market_cap_cr")
    rev_cr = t.get("ttm_revenue_cr")
    margin = t.get("ttm_ebitda_margin_%")
    if not mcap_cr or not rev_cr or margin is None or margin != margin:
        return {}
    a = load_annual(con, symbol, consolidated)
    net_debt_cr = 0.0
    if not a.empty:
        row = a.loc[a.index[-1]]
        def g(el):
            v = row.get(el)
            return float(v) if v is not None and v == v else 0.0
        net_debt_cr = (g("BorrowingsCurrent") + g("BorrowingsNoncurrent")
                       - g("CashAndCashEquivalents")) / CR
    ev_cr = mcap_cr + net_debt_cr
    ttm_ebitda_cr = rev_cr * margin / 100
    out = {"ev_cr": ev_cr, "net_debt_cr": net_debt_cr, "ttm_ebitda_cr": ttm_ebitda_cr,
           "ev_ebitda": ev_cr / ttm_ebitda_cr if ttm_ebitda_cr > 0 else np.nan}
    margins = _hist_ebitda_margins(a)
    if len(margins) >= 2:
        mid_ebitda_cr = rev_cr * float(np.mean(margins)) / 100
        out["midcycle_margin_%"] = float(np.mean(margins))
        out["ev_ebitda_midcycle"] = ev_cr / mid_ebitda_cr if mid_ebitda_cr > 0 else np.nan
    return out


def multiple_percentile(hist_values, current: float | None) -> float | None:
    """Where ``current`` sits in the stock's own history: % of historical values at
    or below it (0–100). 45 ⇒ current sits at the 45th percentile of its range —
    cheaper than ~55% of history. ``None`` if too little history or no current value."""
    vals = [float(v) for v in hist_values
            if v is not None and v == v and 0 < float(v) < 100_000]
    if len(vals) < 3 or current is None or current != current:
        return None
    return 100 * sum(1 for v in vals if v <= current) / len(vals)
