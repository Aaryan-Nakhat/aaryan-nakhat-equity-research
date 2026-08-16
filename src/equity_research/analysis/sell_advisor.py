"""Sell-priority advisor — of *your* holdings, which to sell first if you need cash.

**Version A (merit only).** Ranks the stocks you own purely on forward-looking merit —
which you'd *least regret* parting with — reusing the deterministic analysis layer
(``screener`` / ``quant`` / ``technical`` / ``ownership``). It deliberately knows nothing
about your cost, P&L or tax yet: that's **Version B**, which arrives once holdings carry
quantity + average cost + buy date (then LTCG/STCG and "raise ₹X" sizing layer on top).

Each holding gets a **keep score** (0-100). Every signal is rank-normalised **within your
own book**, so the ranking answers exactly the question asked — *of the stocks I hold,
which is the weakest hand* — not "how does this rank against the Nifty-500". Signals and
weights (``_WEIGHTS``):

- **Valuation headroom** (upside 0.20 + cheapness 0.15) — DCF margin of safety (median
  intrinsic vs price) plus cheap-vs-own-history percentile. Little upside / richly valued
  ⇒ sell first (you forgo the least future return).
- **Quality** (0.25) — Piotroski F (0-9).
- **Forensic** (0.20) — Altman Z · Beneish M · Sloan accruals · no promoter pledge (0-4).
- **Momentum** (0.10) — 3-month relative strength vs Nifty. A laggard is easier to let go.
- **Smart-money flow** (0.10) — net institutional QoQ accumulation; institutions exiting
  ⇒ sell first.

Sell-first = **lowest keep score**; the list is returned worst-first. Names bucket into
🔴 Sell candidates / 🟡 Trim if needed / 🟢 Keep. This is decision *support* — the final
call is yours; reply a number to pull that stock's full deep report before you act.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research import watchlist
from equity_research.analysis import ownership, quant, screener, technical, valuation

log = logging.getLogger(__name__)

# Weights sum to 1.0. Valuation gets the most weight: when you sell, the stock with the
# least upside left is the one whose sale costs you the least going forward.
_WEIGHTS = {"upside": 0.20, "cheapness": 0.15, "quality": 0.25,
            "forensic": 0.20, "momentum": 0.10, "inst_flow": 0.10}
_SIGNALS = list(_WEIGHTS)

# Institutional holder categories whose QoQ move counts as "smart money".
_INST_CATS = {"mutual fund", "insurance company", "FPI", "bank / FI"}


def _upside_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """DCF upside relative to **price**: (median intrinsic − price)/price × 100. Positive =
    undervalued (that much upside to fair value), negative = overvalued (floored at −100%).
    Measured against price — not the DCF median — so it reads as a sane move-to-fair-value
    (dividing by a tiny median otherwise yields absurd four-figure %). ``None`` when the DCF
    isn't usable (e.g. banks/financials) — the cheapness signal then carries valuation."""
    try:
        inp = quant.dcf_inputs(con, symbol)
        if not inp.usable:
            return None
        res = quant.monte_carlo_dcf(inp)
    except Exception:  # noqa: BLE001 — a thin/odd name must not break the ranking
        return None
    if res.median is None or not res.price:
        return None
    return (res.median - res.price) / res.price * 100


def _momentum_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """3-month out/under-performance vs Nifty, in %. ``relative_strength`` returns a *ratio*
    (>1 = outperform), so convert to a percentage: (ratio − 1) × 100. Higher = leading."""
    try:
        rs = technical.relative_strength(con, symbol)
    except Exception:  # noqa: BLE001
        return None
    return None if rs is None else (rs - 1) * 100


def _inst_flow_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """Net institutional accumulation (pp) over the latest QoQ shareholding diff: MF /
    insurer / FPI / bank entering + adding, minus exiting + trimming. Positive = smart
    money moving in. ``None`` when there aren't two quarters to diff."""
    try:
        ch = ownership.ownership_changes(con, symbol)
    except Exception:  # noqa: BLE001
        return None
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


def _why(r: dict) -> str:
    """Short, sell-relevant reasoning — lead with valuation, then the weakest signals."""
    bits: list[str] = []
    u = r.get("upside")
    if u is not None:
        # cap the shown magnitude — a DCF median far above price yields silly 3-figure %
        # that reads as noise (the ranking still uses the true value).
        lead = ">" if abs(u) > 200 else "~"
        bits.append(f"DCF {lead}{min(abs(u), 200):.0f}% {'upside' if u >= 0 else 'overvalued'}")
    elif r.get("cheapness") is not None:
        ch = r["cheapness"]
        bits.append(f"{'cheaper than' if ch >= 50 else 'pricier than'} "
                    f"{(ch if ch >= 50 else 100 - ch):.0f}% of own history")
    if r.get("quality") is not None:
        bits.append(f"Piotroski {r['quality']:.0f}/9")
    if r.get("forensic") is not None and r["forensic"] < 3:
        bits.append(f"forensic {r['forensic']:.1f}/4")
    m = r.get("momentum")
    if m is not None:
        bits.append(f"{'leads' if m >= 0 else 'lags'} Nifty {m:+.0f}% (3m)")
    f = r.get("inst_flow")
    if f is not None and abs(f) >= 0.1:
        bits.append(f"institutions {'adding' if f > 0 else 'trimming'} {f:+.1f}pp")
    return " · ".join(bits)


def _verdict(rank: int, n: int) -> str:
    """Bucket by position in your own book: weakest third sell, middle trim, top keep."""
    if n <= 2:
        return "🔴 Sell first" if rank == 1 else "🟢 Keep"
    third = n / 3
    if rank <= third:
        return "🔴 Sell candidate"
    if rank <= 2 * third:
        return "🟡 Trim if needed"
    return "🟢 Keep"


def sell_ranking(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Rank the user's *holdings* sell-first on merit (Version A — no cost/tax yet).

    Returns ``[{symbol, name, keep_score, verdict, upside, cheapness, quality, forensic,
    momentum, inst_flow, pe, pb, why}, …]``, **weakest hand first** (sell candidates on
    top). Empty if no holdings are tagged. Best-effort: a holding missing a signal is
    scored on the rest (a missing signal normalises to the neutral middle)."""
    holdings = watchlist.entries_by_type(con, "holding")
    rows: list[dict] = []
    for sym, name in holdings:
        try:
            snap = valuation.snapshot(con, sym) or {}
            rows.append({
                "symbol": sym, "name": name or sym,
                "upside": _upside_raw(con, sym),
                "cheapness": screener._cheapness_raw(con, sym),
                "quality": screener._quality_raw(con, sym),
                "forensic": screener._forensic_raw(con, sym),
                "momentum": _momentum_raw(con, sym),
                "inst_flow": _inst_flow_raw(con, sym),
                "pe": snap.get("pe_ttm"), "pb": snap.get("pb"),
            })
        except Exception:  # noqa: BLE001 — one bad holding must not sink the whole ranking
            log.exception("sell scoring failed for %s", sym)
            continue
    if not rows:
        return []

    # Split off holdings with no usable signal at all (not yet ingested) — they can't be
    # ranked and would otherwise sit at a meaningless mid-pack 50, mislabelled "trim".
    scored = [r for r in rows if any(r[k] is not None for k in _SIGNALS)]
    unscored = [r for r in rows if r not in scored]

    for key in _SIGNALS:
        screener._normalise(scored, key)
    for r in scored:
        r["keep_score"] = round(100 * sum(_WEIGHTS[k] * r[k + "_n"] for k in _SIGNALS), 1)
    # keep_score asc → weakest hand first; symbol asc as a stable tie-break so the order
    # (and any future week-over-week deltas) doesn't jitter run-to-run.
    scored.sort(key=lambda r: (r["keep_score"], r["symbol"]))
    n = len(scored)
    for i, r in enumerate(scored, 1):
        r["verdict"] = _verdict(i, n)
        r["why"] = _why(r)
    for r in sorted(unscored, key=lambda r: r["symbol"]):
        r["keep_score"] = None
        r["verdict"] = "⚪ No data"
        r["why"] = "not ingested yet — email the symbol once to build its report first"
    return scored + sorted(unscored, key=lambda r: r["symbol"])
