"""Fundamental analysis — ratios derived from the quarterly P&L series.

Reads the long-format ``financials`` table (see ``ingest.ingest_financials``) and
computes margin / efficiency / growth metrics per quarter, plus TTM aggregates.

Scope note: quarterly result XBRL is P&L-heavy, so this layer covers
profitability + growth. ROE/ROCE/ROIC, leverage/liquidity and the forensic
scores (Piotroski F, Altman Z, Beneish M) need annual balance-sheet / cash-flow
data and arrive once that ingest lands — see ``docs/FUNDAMENTALS.md``.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

CR = 1e7  # 1 crore = 10^7 rupees


def load_quarters(con: duckdb.DuckDBPyConnection, symbol: str,
                  consolidated: bool = False) -> pd.DataFrame:
    """Wide quarterly frame: index = period_end, columns = XBRL elements."""
    df = con.execute(
        """SELECT period_end, element, value FROM financials
           WHERE symbol = ? AND consolidated = ? AND period_type = 'Q'""",
        [symbol, consolidated],
    ).df()
    if df.empty:
        return pd.DataFrame()
    return (df.pivot_table(index="period_end", columns="element", values="value",
                           aggfunc="first")
              .sort_index())


def _col(q: pd.DataFrame, name: str) -> pd.Series:
    """Element column if present, else an all-NaN series (keeps arithmetic safe)."""
    if name in q.columns:
        return q[name]
    return pd.Series(np.nan, index=q.index)


def quarterly_metrics(con: duckdb.DuckDBPyConnection, symbol: str,
                      consolidated: bool = False) -> pd.DataFrame:
    """Per-quarter ratios (margins, coverage, tax rate, YoY growth)."""
    q = load_quarters(con, symbol, consolidated)
    if q.empty:
        return pd.DataFrame()

    rev = _col(q, "RevenueFromOperations")
    net = _col(q, "ProfitLossForPeriod")
    pbt = _col(q, "ProfitBeforeTax")
    fin = _col(q, "FinanceCosts")
    dep = _col(q, "DepreciationDepletionAndAmortisationExpense")
    tax = _col(q, "TaxExpense")
    oth = _col(q, "OtherIncome")

    ebit = pbt + fin              # add back interest
    ebitda = ebit + dep           # add back depreciation

    m = pd.DataFrame(index=q.index)
    m["revenue_cr"] = rev / CR
    m["net_profit_cr"] = net / CR
    m["net_margin_%"] = 100 * net / rev
    m["pbt_margin_%"] = 100 * pbt / rev
    m["ebit_margin_%"] = 100 * ebit / rev
    m["ebitda_margin_%"] = 100 * ebitda / rev
    m["interest_cover_x"] = ebit / fin
    m["eff_tax_%"] = 100 * tax / pbt
    m["other_income_to_pbt_%"] = 100 * oth / pbt
    m["rev_yoy_%"] = 100 * (rev / rev.shift(4) - 1)      # vs same quarter last year
    m["net_yoy_%"] = 100 * (net / net.shift(4) - 1)
    return m.replace([np.inf, -np.inf], np.nan)


def latest_quarters(con: duckdb.DuckDBPyConnection, symbol: str) -> pd.DataFrame:
    """The per-quarter metrics frame preferring **consolidated**, falling back to standalone
    (empty if neither exists) — the shared basis for the earnings-quality reads below."""
    m = quarterly_metrics(con, symbol, consolidated=True)
    if m.empty:
        m = quarterly_metrics(con, symbol, consolidated=False)
    return m


def execution_band_from_metrics(m: pd.DataFrame) -> str | None:
    """How the latest reported quarter **executed**, from the numbers alone (never words):
    ``Firing`` / ``Delivering`` / ``Holding`` / ``Slipping`` / ``Struggling`` — from the latest
    quarter's YoY revenue & profit growth plus the net-margin trend vs the prior quarter. ``None``
    when it can't be computed. Shared by 🎙️ Concalls (the cross-check on management tone) and
    📈 Results Radar (the delivery read)."""
    if m is None or m.empty:
        return None
    last = m.iloc[-1]
    rev_yoy, net_yoy = last.get("rev_yoy_%"), last.get("net_yoy_%")
    if (rev_yoy is None or rev_yoy != rev_yoy) and (net_yoy is None or net_yoy != net_yoy):
        return None
    rev = rev_yoy if (rev_yoy is not None and rev_yoy == rev_yoy) else 0.0
    net = net_yoy if (net_yoy is not None and net_yoy == net_yoy) else 0.0
    margin = 0.0                                       # net-margin trend vs the prior quarter (nudge)
    if len(m) >= 2:
        nm, nm_prev = m.iloc[-1].get("net_margin_%"), m.iloc[-2].get("net_margin_%")
        if nm == nm and nm_prev == nm_prev:
            margin = 0.5 if nm >= nm_prev + 0.5 else (-0.5 if nm <= nm_prev - 0.5 else 0.0)
    score = 0.0                                        # profit growth leads, revenue confirms
    score += 2 if net >= 30 else 1 if net >= 15 else 0 if net >= 0 else -1 if net > -20 else -2
    score += 1 if rev >= 12 else 0 if rev >= 0 else -1
    score += margin
    if score >= 2.5:
        return "Firing"
    if score >= 1:
        return "Delivering"
    if score >= -0.5:
        return "Holding"
    if score >= -2:
        return "Slipping"
    return "Struggling"


def execution_band(con: duckdb.DuckDBPyConnection, symbol: str) -> str | None:
    """``execution_band_from_metrics`` for ``symbol`` (loads the preferred quarterly frame)."""
    return execution_band_from_metrics(latest_quarters(con, symbol))


def ttm(con: duckdb.DuckDBPyConnection, symbol: str,
        consolidated: bool = False) -> dict[str, float]:
    """Trailing-twelve-month aggregates from the last 4 quarters."""
    q = load_quarters(con, symbol, consolidated)
    if len(q) < 4:
        return {}
    last4 = q.tail(4)
    rev = _col(last4, "RevenueFromOperations").sum()
    net = _col(last4, "ProfitLossForPeriod").sum()
    pbt = _col(last4, "ProfitBeforeTax").sum()
    fin = _col(last4, "FinanceCosts").sum()
    dep = _col(last4, "DepreciationDepletionAndAmortisationExpense").sum()
    return {
        "ttm_revenue_cr": rev / CR,
        "ttm_net_profit_cr": net / CR,
        "ttm_net_margin_%": 100 * net / rev if rev else np.nan,
        "ttm_ebit_margin_%": 100 * (pbt + fin) / rev if rev else np.nan,
        "ttm_ebitda_margin_%": 100 * (pbt + fin + dep) / rev if rev else np.nan,
        "quarters_used": float(len(last4)),
    }


def ttm_pl(con: duckdb.DuckDBPyConnection, symbol: str,
           consolidated: bool = False) -> pd.Series:
    """Trailing-twelve-month sum of **every** P&L element over the last 4 quarters —
    for a 'TTM' column alongside the annual statements. Empty unless 4 *consecutive*
    quarters exist (≈9-13 months end-to-end), so a missing quarter never understates."""
    q = load_quarters(con, symbol, consolidated)
    if len(q) < 4:
        return pd.Series(dtype=float)
    last4 = q.tail(4)
    span = (last4.index[-1] - last4.index[0]).days
    if not (250 <= span <= 400):
        return pd.Series(dtype=float)
    return last4.sum(min_count=1)


def load_annual(con: duckdb.DuckDBPyConnection, symbol: str,
                consolidated: bool = False) -> pd.DataFrame:
    """Wide annual frame: index = fiscal year-end, columns = XBRL elements."""
    df = con.execute(
        """SELECT period_end, element, value FROM financials
           WHERE symbol = ? AND consolidated = ? AND period_type = 'Y'""",
        [symbol, consolidated],
    ).df()
    if df.empty:
        return pd.DataFrame()
    return (df.pivot_table(index="period_end", columns="element", values="value",
                           aggfunc="first")
              .sort_index())


def annual_overview(con: duckdb.DuckDBPyConnection, symbol: str,
                    consolidated: bool = False) -> pd.DataFrame:
    """Per-year headline + earnings-quality signals (incl. CFO-vs-PAT)."""
    a = load_annual(con, symbol, consolidated)
    if a.empty:
        return pd.DataFrame()

    rev = _col(a, "RevenueFromOperations")
    net = _col(a, "ProfitLossForPeriod")
    cfo = _col(a, "CashFlowsFromUsedInOperatingActivities")
    assets = _col(a, "Assets")

    o = pd.DataFrame(index=a.index)
    o["revenue_cr"] = rev / CR
    o["net_profit_cr"] = net / CR
    o["cfo_cr"] = cfo / CR
    o["assets_cr"] = assets / CR
    # CFO-vs-PAT: cash should back the profit. < 1 persistently = a quality flag.
    o["cfo_to_pat_x"] = cfo / net
    o["accruals_%_assets"] = 100 * (net - cfo) / assets   # high positive = aggressive
    o["roa_%"] = 100 * net / assets
    return o.replace([np.inf, -np.inf], np.nan)
