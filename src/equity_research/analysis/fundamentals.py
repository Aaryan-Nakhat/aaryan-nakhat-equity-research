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
