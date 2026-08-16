"""Technical discovery screen — find the strongest *chart* setups to buy, market-wide.

The other screens rank on fundamentals; this one ranks on **price action** and returns names
with a clean, actionable setup (entry zone · stop · target · reward:risk · S/R). It answers
"what looks good to buy technically right now, and at what price?" across the universe — not
just holdings.

Two stages, so it fits the email time budget (full ``levels()`` on the whole universe would be
far too slow):

1. **Rank (fast, whole liquid universe).** Score every *liquid* symbol that has financials on a
   technical composite (each continuous signal rank-normalised across the set, ``_WEIGHTS``):
   trend (above 200-DMA · 50>200) · relative strength vs Nifty · MACD · an RSI *health* score
   (rewards a healthy 40-70 zone, penalises overbought) · breakout proximity (near the 52w high)
   · delivery-volume confirmation.
2. **Setup + safety gate (top shortlist only).** Walk the ranked list top-down; for each, apply a
   **trap gate** (Altman near-distress · Beneish manipulator · promoter pledge >25% — the same
   gates the small-cap screen uses) and run the full ``technical.levels()`` setup. Keep names that
   pass the gate **and** have a real, non-reference setup, best-first, until ``limit`` are found.

**Universe is bounded to symbols with financials ingested** — that's what makes the safety gate
real for every row (it spans micro→large cap incl. the Microcap-250; coverage grows with the
backfill). Illiquid micro-caps are dropped by the turnover floor (garbage TA, un-tradeable).

Honest caveat, surfaced in the email: this is a *candidate finder* with defined risk (entry/stop/
target) — it is **not** back-tested, so it doesn't prove out-performance. Short-term timing is the
tool's least-proven edge. Reply a number → that name's full deep report before you act.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.analysis import forensic, screener, technical

log = logging.getLogger(__name__)

# Technical composite weights (sum 1.0). Trend + relative strength carry the most — a rising
# stock leading the index is the core of a momentum setup; the rest refine the entry timing.
_WEIGHTS = {"trend": 0.30, "rs": 0.25, "macd": 0.15, "rsi_fit": 0.10,
            "breakout": 0.10, "delivery": 0.10}
_SIGNALS = list(_WEIGHTS)

_MIN_TURNOVER_CR = 2.0      # avg daily traded value floor (₹ cr) over the liquidity window
_LIQ_WINDOW = 20            # sessions the liquidity average is taken over
_SHORTLIST_CAP = 60         # how far down the ranked list stage-2 will walk (bounds the time)


def _liquid_universe(con: duckdb.DuckDBPyConnection, min_turnover_cr: float) -> dict[str, float]:
    """Symbols with financials whose **average daily turnover** over the last ``_LIQ_WINDOW``
    sessions clears ``min_turnover_cr`` — returns ``{symbol: avg_turnover_cr}``. ``turnover_lacs``
    is ₹ lakh, so ₹1 cr = 100 lakh."""
    rows = con.execute(
        """
        WITH recent AS (
            SELECT e.symbol, e.turnover_lacs,
                   row_number() OVER (PARTITION BY e.symbol ORDER BY e.trade_date DESC) AS rn
            FROM equity_eod e
            JOIN (SELECT DISTINCT symbol FROM financials) f ON f.symbol = e.symbol
            WHERE e.series IN ('EQ', 'BE', 'BZ')
        )
        SELECT symbol, avg(turnover_lacs) / 100.0 AS turn_cr
        FROM recent WHERE rn <= ?
        GROUP BY symbol
        HAVING avg(turnover_lacs) / 100.0 >= ?
        """,
        [_LIQ_WINDOW, min_turnover_cr]).fetchall()
    return {sym: float(t) for sym, t in rows}


def _rsi_fit(rsi: float | None) -> float | None:
    """Health of the RSI for a *buy*: peaks in the constructive 55-60 band, falls off into
    oversold and (harder) into overbought — we don't want to chase an extended stock."""
    if rsi is None or rsi != rsi:
        return None
    return max(0.0, 1.0 - abs(rsi - 57) / 40)


def _ta_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    """Raw (un-normalised) technical signals for one symbol, or ``None`` if there's no snapshot."""
    snap = technical.snapshot(con, symbol)
    if not snap:
        return None
    close, s50, s200 = snap.get("close"), snap.get("sma50"), snap.get("sma200")
    trend = None
    if close is not None and s200 == s200 and s200:
        trend = 2.0 * (close > s200) + (1.0 * (s50 > s200) if s50 == s50 and s50 else 0.0)
    rs = snap.get("rel_strength_3m_vs_nifty")
    deliv, deliv_avg = snap.get("deliv_per"), snap.get("deliv_avg20")
    delivery = (deliv / deliv_avg) if deliv == deliv and deliv_avg == deliv_avg and deliv_avg else None
    return {
        "trend": trend,
        "rs": (rs - 1) * 100 if rs is not None else None,   # ratio → out/under-performance %
        "macd": snap.get("macd_hist") if snap.get("macd_hist") == snap.get("macd_hist") else None,
        "rsi_fit": _rsi_fit(snap.get("rsi14")),
        "breakout": snap.get("pct_from_52w_high"),          # closer to 0 (less negative) = nearer high
        "delivery": delivery,
        "_signals": snap.get("signals", []),
        "_rs_pct": (rs - 1) * 100 if rs is not None else None,
        "_from_high": snap.get("pct_from_52w_high"),
    }


def _safe(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Trap gate — reject near-distress / likely-manipulator / heavily-pledged names. A metric
    that can't be computed doesn't reject (we only drop on a *failing* value, never a missing one)."""
    z = forensic.altman_z(con, symbol)
    if z.value is not None and z.value < 1.81:
        return False
    m = forensic.beneish_m(con, symbol)
    if m.value is not None and m.value > -1.78:
        return False
    pl = con.execute(
        "SELECT pledged_pct_of_promoter FROM shareholding WHERE symbol = ? "
        "ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
    return not (pl and pl[0] is not None and pl[0] > 25)


def _screen_setup(lv: dict) -> dict | None:
    """A *momentum-appropriate* setup for the screen: entry at the **nearest** support (a shallow
    pullback), not ``technical._setup``'s strongest/deepest zone — which for names near their 52w
    high sits far below spot and reads as an impossible 'buy 50% lower'. Returns None when there's
    no support to define risk against.

    kinds: ``extended`` (price well above the nearest floor — wait for a pullback) · ``breakout``
    (near highs, no overhead resistance mapped — trail, blue sky) · ``accumulate`` (R:R ≥ 1.5) ·
    ``watch`` (thin R:R)."""
    price, atr = lv.get("close"), lv.get("atr")
    sups, ress = lv.get("supports") or [], lv.get("resistances") or []
    if not sups or not price or atr is None:
        return None
    near = sups[0]                                    # nearest support below (levels sorts near-first)
    entry_lo, entry_hi, entry_mid = near["lo"], near["hi"], near["mid"]
    stop = round(entry_lo - atr, 2)
    dist = (price - entry_mid) / price                # how far above the buy zone we are now
    target = round(ress[0]["mid"], 2) if ress else None
    risk = entry_mid - stop
    rr = round((target - entry_mid) / risk, 2) if target and risk > 0 else None
    if dist > 0.12:
        kind = "extended"
    elif target is None:
        kind = "breakout"
    elif rr and rr >= 1.5:
        kind = "accumulate"
    else:
        kind = "watch"
    return {"kind": kind, "entry_lo": round(entry_lo, 2), "entry_hi": round(entry_hi, 2),
            "stop": stop, "target": target, "rr": rr, "dist_pct": round(100 * dist, 1)}


def _why(r: dict, setup: dict) -> str:
    bits: list[str] = []
    sig = r.get("_signals") or []
    if any("above 200-DMA" in s for s in sig):
        bits.append("uptrend >200-DMA")
    elif any("below 200-DMA" in s for s in sig):
        bits.append("below 200-DMA")
    if any("golden-cross" in s for s in sig):
        bits.append("50>200")
    if any(s.startswith("MACD bullish") for s in sig):
        bits.append("MACD+")
    if r.get("_rs_pct") is not None:
        bits.append(f"RS {r['_rs_pct']:+.0f}% vs Nifty")
    if r.get("_from_high") is not None:
        bits.append(f"{abs(r['_from_high']):.0f}% below 52w high")
    if any("delivery% spike" in s for s in sig):
        bits.append("delivery spike")
    if setup.get("rr"):
        bits.append(f"R:R {setup['rr']:.1f}:1")
    return " · ".join(bits)


def technical_screen(con: duckdb.DuckDBPyConnection, *, limit: int = 15,
                     min_turnover_cr: float = _MIN_TURNOVER_CR) -> list[dict]:
    """Ranked technical-setup screen. Returns ``[{symbol, name, score, price, turnover_cr, kind,
    entry_lo, entry_hi, stop, target, rr, support, resistance, why}, …]`` best-first — the top
    ``limit`` names that clear the liquidity floor + trap gate and carry a real setup.

    Best-effort throughout: a thin/odd name is skipped, never fatal."""
    universe = _liquid_universe(con, min_turnover_cr)
    if not universe:
        return []
    names = dict(con.execute("SELECT symbol, company FROM sector_map").fetchall())

    rows: list[dict] = []
    for sym in universe:
        try:
            raw = _ta_raw(con, sym)
        except Exception:  # noqa: BLE001 — one bad name must not break the screen
            log.exception("technical scoring failed for %s", sym)
            continue
        if raw is None:
            continue
        raw["symbol"] = sym
        rows.append(raw)
    if not rows:
        return []

    for key in _SIGNALS:
        screener._normalise(rows, key)
    for r in rows:
        r["score"] = round(100 * sum(_WEIGHTS[k] * r[k + "_n"] for k in _SIGNALS), 1)
    rows.sort(key=lambda r: (-r["score"], r["symbol"]))

    # stage 2 — walk the ranked list; keep gate-passing names with a real, actionable setup.
    out: list[dict] = []
    for r in rows[:_SHORTLIST_CAP]:
        sym = r["symbol"]
        try:
            if not _safe(con, sym):
                continue
            lv = technical.levels(con, sym)
        except Exception:  # noqa: BLE001
            log.exception("technical levels failed for %s", sym)
            continue
        if not lv.get("history_ok"):
            continue
        setup = _screen_setup(lv)
        if setup is None or setup["kind"] == "extended":     # no floor, or too far above entry
            continue
        sup = lv["supports"][0]["mid"] if lv.get("supports") else None
        res = lv["resistances"][0]["mid"] if lv.get("resistances") else None
        out.append({
            "symbol": sym, "name": names.get(sym, sym), "score": r["score"],
            "price": lv.get("close"), "turnover_cr": universe.get(sym),
            "kind": setup["kind"],
            "entry_lo": setup["entry_lo"], "entry_hi": setup["entry_hi"],
            "stop": setup["stop"], "target": setup["target"],
            "rr": setup["rr"], "support": sup, "resistance": res,
            "why": _why(r, setup),
        })
        if len(out) >= limit:
            break
    # actionable "accumulate" first, then "breakout", then by technical score
    _order = {"accumulate": 0, "breakout": 1, "watch": 2}
    out.sort(key=lambda r: (_order.get(r["kind"], 3), -r["score"]))
    return out
