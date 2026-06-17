"""Fundamental/forensic charts for the PDF report (matplotlib → PNG bytes).

Deliberately fundamental, not price/volume: the visuals reinforce the cash-quality
and balance-sheet story. Each chart is built from the annual `financials` we
already load; a chart is skipped when its data is absent. The Monte-Carlo
fair-value histogram comes from `analysis.quant`.

Uses the non-interactive Agg backend (no display needed) — safe in a headless
bot/service.
"""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import duckdb  # noqa: E402

from equity_research.analysis import quant  # noqa: E402
from equity_research.analysis.fundamentals import load_annual  # noqa: E402

CR = 1e7
_GREEN, _BLUE, _RED, _GREY = "#0a6b3b", "#1f5fb0", "#b0231f", "#888888"


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _series(af: pd.DataFrame, el: str) -> pd.Series:
    return af[el] if el in af.columns else pd.Series(np.nan, index=af.index)


def _annual_frame(con: duckdb.DuckDBPyConnection, symbol: str,
                  consolidated: bool) -> pd.DataFrame:
    """Tidy per-FY derived metrics (₹cr for money, ratios/percent as labelled)."""
    af = load_annual(con, symbol, consolidated)
    if af.empty:
        return pd.DataFrame()
    rev, pat = _series(af, "RevenueFromOperations"), _series(af, "ProfitLossForPeriod")
    cfo = _series(af, "CashFlowsFromUsedInOperatingActivities")
    pbt, fin = _series(af, "ProfitBeforeTax"), _series(af, "FinanceCosts")
    tax = _series(af, "TaxExpense")
    eq = _series(af, "Equity")
    cash = _series(af, "CashAndCashEquivalents")
    capex = _series(af, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities").abs()
    debt = _series(af, "BorrowingsCurrent").add(_series(af, "BorrowingsNoncurrent"), fill_value=0)
    ebit = pbt + fin
    taxrate = (tax / pbt).clip(0, 0.5)
    d = pd.DataFrame(index=[ix.year for ix in af.index])
    d["revenue"] = (rev / CR).to_numpy()
    d["pat"] = (pat / CR).to_numpy()
    d["net_margin"] = (100 * pat / rev).to_numpy()
    d["cfo"] = (cfo / CR).to_numpy()
    d["roe"] = (100 * pat / eq).to_numpy()
    d["roce"] = (100 * ebit / (eq + debt)).to_numpy()
    d["roic"] = (100 * ebit * (1 - taxrate) / (eq + debt - cash)).to_numpy()
    d["de"] = (debt / eq).to_numpy()
    d["int_cover"] = (ebit / fin).to_numpy()
    d["fcf"] = ((cfo - capex) / CR).to_numpy()
    d["fcff"] = ((cfo - capex + fin * (1 - taxrate)) / CR).to_numpy()
    return d.replace([np.inf, -np.inf], np.nan)


def _has(s: pd.Series) -> bool:
    return s.notna().any()


def report_charts(con: duckdb.DuckDBPyConnection, symbol: str,
                  consolidated: bool = False) -> list[tuple[str, bytes]]:
    """Build the fundamental chart set as (caption, png-bytes). Skips empty ones."""
    out: list[tuple[str, bytes]] = []
    d = _annual_frame(con, symbol, consolidated)
    if not d.empty:
        yrs = [str(y) for y in d.index]

        # 1) Revenue & PAT bars + net-margin line
        if _has(d["revenue"]):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            x = np.arange(len(d))
            ax.bar(x - 0.2, d["revenue"], 0.4, label="Revenue (₹cr)", color=_BLUE)
            ax.bar(x + 0.2, d["pat"], 0.4, label="PAT (₹cr)", color=_GREEN)
            ax.set_xticks(x)
            ax.set_xticklabels(yrs)
            ax.set_ylabel("₹ crore")
            ax2 = ax.twinx()
            ax2.plot(x, d["net_margin"], color=_RED, marker="o", label="Net margin %")
            ax2.set_ylabel("Net margin %", color=_RED)
            ax.set_title(f"{symbol} — Revenue, PAT & margin")
            ax.legend(loc="upper left", fontsize=8)
            out.append(("Revenue, PAT & net margin", _png(fig)))

        # 2) CFO vs PAT — the cash-quality (forensic) chart
        if _has(d["cfo"]) and _has(d["pat"]):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            x = np.arange(len(d))
            ax.bar(x - 0.2, d["pat"], 0.4, label="PAT (₹cr)", color=_GREEN)
            ax.bar(x + 0.2, d["cfo"], 0.4, label="CFO (₹cr)", color=_BLUE)
            ax.set_xticks(x)
            ax.set_xticklabels(yrs)
            ax.set_ylabel("₹ crore")
            ax.axhline(0, color=_GREY, lw=0.6)
            ax.set_title(f"{symbol} — CFO vs PAT (cash backing of earnings)")
            ax.legend(loc="upper left", fontsize=8)
            out.append(("CFO vs PAT — earnings quality", _png(fig)))

        # 3) Returns: ROE / ROCE / ROIC
        if _has(d["roce"]):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            x = np.arange(len(d))
            for col, c, lbl in (("roe", _BLUE, "ROE"), ("roce", _GREEN, "ROCE"), ("roic", _RED, "ROIC")):
                if _has(d[col]):
                    ax.plot(x, d[col], marker="o", color=c, label=f"{lbl} %")
            ax.set_xticks(x)
            ax.set_xticklabels(yrs)
            ax.set_ylabel("%")
            ax.set_title(f"{symbol} — Returns (ROE / ROCE / ROIC)")
            ax.legend(loc="best", fontsize=8)
            out.append(("Returns on capital", _png(fig)))

        # 4) Leverage: D/E bars + interest cover line
        if _has(d["de"]) or _has(d["int_cover"]):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            x = np.arange(len(d))
            ax.bar(x, d["de"], 0.5, label="Debt / Equity (x)", color=_BLUE)
            ax.set_xticks(x)
            ax.set_xticklabels(yrs)
            ax.set_ylabel("Debt / Equity (x)")
            ax2 = ax.twinx()
            ax2.plot(x, d["int_cover"], color=_RED, marker="o", label="Interest cover (x)")
            ax2.set_ylabel("Interest cover (x)", color=_RED)
            ax.set_title(f"{symbol} — Leverage & interest cover")
            ax.legend(loc="upper left", fontsize=8)
            out.append(("Leverage & interest cover", _png(fig)))

        # 5) Free cash flow
        if _has(d["fcf"]) or _has(d["fcff"]):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            x = np.arange(len(d))
            ax.bar(x - 0.2, d["fcf"], 0.4, label="FCF (₹cr)", color=_BLUE)
            ax.bar(x + 0.2, d["fcff"], 0.4, label="FCFF (₹cr)", color=_GREEN)
            ax.set_xticks(x)
            ax.set_xticklabels(yrs)
            ax.set_ylabel("₹ crore")
            ax.axhline(0, color=_GREY, lw=0.6)
            ax.set_title(f"{symbol} — Free cash flow")
            ax.legend(loc="upper left", fontsize=8)
            out.append(("Free cash flow (FCF / FCFF)", _png(fig)))

    # 6) Monte-Carlo intrinsic-value distribution
    try:
        mc = quant.monte_carlo_dcf(quant.dcf_inputs(con, symbol, consolidated))
        if mc.samples is not None and len(mc.samples):
            fig, ax = plt.subplots(figsize=(7, 3.2))
            clip = np.percentile(mc.samples, 99)
            ax.hist(np.clip(mc.samples, None, clip), bins=60, color=_BLUE, alpha=0.7)
            if mc.median:
                ax.axvline(mc.median, color=_GREEN, lw=1.5, label=f"median ₹{mc.median:,.0f}")
            if mc.price:
                ax.axvline(mc.price, color=_RED, lw=1.5, label=f"price ₹{mc.price:,.0f}")
            ax.set_xlabel("Intrinsic value / share (₹)")
            ax.set_ylabel("frequency")
            ax.set_title(f"{symbol} — Monte-Carlo DCF fair value")
            ax.legend(loc="best", fontsize=8)
            out.append(("Monte-Carlo DCF fair-value distribution", _png(fig)))
    except Exception:  # noqa: BLE001 — a chart should never break the report
        pass
    return out
