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

from equity_research.analysis import quant, technical  # noqa: E402
from equity_research.analysis.fundamentals import load_annual  # noqa: E402

CR = 1e7
_GREEN, _BLUE, _RED, _GREY = "#0a6b3b", "#1f5fb0", "#b0231f", "#888888"


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def fund_charts(con: duckdb.DuckDBPyConnection, scheme_code: int) -> list[tuple[str, bytes]]:
    """Charts for a mutual-fund report: NAV growth (₹100 rebased) + the rolling
    1-year return distribution. Each is skipped when its data is too thin."""
    from equity_research.analysis import funds
    s = funds.nav_series(con, scheme_code)
    out: list[tuple[str, bytes]] = []
    if s.empty:
        return out

    # 1) Growth of ₹100 invested (NAV rebased) — the compounding story.
    fig, ax = plt.subplots(figsize=(7, 3.2))
    rebased = s / float(s.iloc[0]) * 100
    ax.plot(rebased.index, rebased.values, color=_BLUE, lw=1.3)
    ax.axhline(100, color=_GREY, lw=0.8, ls="--")
    ax.set_title("Growth of ₹100 invested (NAV, rebased)")
    ax.set_ylabel("Value of ₹100")
    ax.grid(True, alpha=0.3)
    out.append(("NAV growth — ₹100 rebased over the history on file", _png(fig)))

    # 2) Rolling 1-year return distribution — the consistency read.
    end = s.index[-1]
    vals = []
    for ts, nav in s.items():
        tgt = ts + pd.Timedelta(days=365)
        if tgt > end:
            break
        fnav = s.asof(tgt)
        if fnav and nav and nav > 0:
            vals.append((fnav / nav - 1) * 100)
    if len(vals) >= 30:
        arr = np.array(vals)
        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.hist(arr, bins=30, color=_BLUE, alpha=0.75)
        ax.axvline(0, color=_RED, lw=1.0)
        ax.axvline(float(np.median(arr)), color=_GREEN, lw=1.2,
                   label=f"median {np.median(arr):.0f}%")
        ax.set_title("Rolling 1-year returns — distribution")
        ax.set_xlabel("1-year return (%)")
        ax.set_ylabel("Frequency")
        ax.legend(fontsize=8)
        out.append(("Rolling 1-year returns — every 1-yr holding period in the history", _png(fig)))
    return out


def levels_chart(con: duckdb.DuckDBPyConnection, symbol: str,
                 lv: dict, *, bars: int = 180, draw_setup: bool = True) -> tuple[str, bytes] | None:
    """Annotated price chart: last ~``bars`` daily candles with the computed support
    (green) and resistance (red) zones shaded, the 50/200-DMA, and the setup's entry
    zone / stop / targets drawn on. ``lv`` is ``technical.levels(...)``. None when the
    history is too thin (the caller then just omits the chart)."""
    if not lv or not lv.get("history_ok"):
        return None
    ind = technical.indicators(con, symbol)
    if ind.empty:
        return None
    d = ind.iloc[-bars:]
    x = np.arange(len(d))
    o, h, low_, c = d["open"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
    up = c >= o

    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    # candlesticks — thin wick (high-low) + thick body (open-close)
    ax.vlines(x[up], low_[up], h[up], color=_GREEN, linewidth=0.6)
    ax.vlines(x[~up], low_[~up], h[~up], color=_RED, linewidth=0.6)
    ax.vlines(x[up], o[up], c[up], color=_GREEN, linewidth=2.4)
    ax.vlines(x[~up], o[~up], c[~up], color=_RED, linewidth=2.4)
    # moving averages for context
    for col, cl, lw in (("sma50", _BLUE, 0.9), ("sma200", "#c07f1f", 0.9)):
        if col in d and d[col].notna().any():
            ax.plot(x, d[col].to_numpy(), color=cl, lw=lw, label=col.upper().replace("SMA", "SMA "))

    xr = (x[0] - 1, x[-1] + 1)
    # zones: support green, resistance red — shaded bands across the panel
    for z in lv.get("supports", []):
        ax.axhspan(z["lo"], z["hi"], color=_GREEN, alpha=0.10)
        ax.hlines(z["mid"], *xr, color=_GREEN, lw=0.7, alpha=0.5)
    for z in lv.get("resistances", []):
        ax.axhspan(z["lo"], z["hi"], color=_RED, alpha=0.10)
        ax.hlines(z["mid"], *xr, color=_RED, lw=0.7, alpha=0.5)

    # setup overlay — entry zone / stop / targets (only for an actionable long setup)
    s = lv.get("setup", {})
    if draw_setup and s.get("kind") in ("accumulate", "watch", "hold-trail") and s.get("stop"):
        if s.get("entry_lo") and s.get("entry_hi"):
            ax.axhspan(s["entry_lo"], s["entry_hi"], color=_BLUE, alpha=0.10)
        ax.axhline(s["stop"], color=_RED, lw=1.1, ls="--", label=f"stop ₹{s['stop']:,.0f}")
        for i, t in enumerate(s.get("targets", [])):
            ax.axhline(t, color=_GREEN, lw=1.0, ls=":",
                       label=f"target ₹{t:,.0f}" if i == 0 else None)

    ax.set_xlim(*xr)
    ax.set_ylabel("₹")
    ax.set_xticks([])
    kind = s.get("kind", "")
    ax.set_title(f"{symbol} — price with support/resistance zones & setup"
                 + (f"  ({kind})" if kind else ""))
    ax.legend(loc="upper left", fontsize=7, framealpha=0.85)
    ax.grid(True, axis="y", alpha=0.25)
    cap = (f"{symbol}: last {len(d)} sessions — support (green) / resistance (red) zones, "
           "50 & 200-DMA, and the entry/stop/target setup. Levels are computed, not advice.")
    return cap, _png(fig)


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

    # 7) Price with support/resistance zones (verdict-neutral facts; the verdict-aware
    # entry/stop/target lives in the report's "Trading levels & setup" text section).
    try:
        lv = technical.levels(con, symbol)
        pc = levels_chart(con, symbol, lv, draw_setup=False)
        if pc:
            out.append(pc)
    except Exception:  # noqa: BLE001
        pass
    return out
