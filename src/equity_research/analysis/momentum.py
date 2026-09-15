"""Momentum-breakout discovery — surface stocks breaking out to new highs, market-wide.

Answers "what is breaking out *right now*, with real buying behind it?" across the whole liquid
price universe — a discovery screen that hands you names you didn't ask for. It ranks on **price
action alone** (52-week-high proximity, an intact uptrend, a volume surge and delivery
confirmation), so it spans every liquid symbol with price history — not just the ones whose
financials are ingested (a surfaced name's financials are pulled on demand when you open its
report). A light forensic **trap gate** is applied to the shortlist where the financials exist,
so an obvious distress/manipulation flag is dropped, but a name without financials is still shown.

Output is a ranked list; the screen *finds*, the deep report (reply a number) *diligences*.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.analysis import forensic, screener

log = logging.getLogger(__name__)

_MIN_TURNOVER_CR = 2.0      # avg daily traded value floor (₹ cr) over the liquidity window
_LIQ_WINDOW = 20            # sessions the liquidity average is taken over
_MIN_DAYS = 150             # need enough history for a real 52w high + 200-DMA
_NEAR_HIGH = -0.04          # within 4% of the 52-week high counts as "breaking out"
_MIN_VOL_SURGE = 1.3        # latest volume ≥ 1.3× its 20-day average
_SHORTLIST_CAP = 60         # how far down the ranked list the trap gate walks

# Composite weights (sum 1.0): proximity to the high leads, then the volume thrust confirming it.
_WEIGHTS = {"prox": 0.40, "vol_surge": 0.35, "deliv_ratio": 0.25}
_SIGNALS = list(_WEIGHTS)


def _safe(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Trap gate — drop a near-distress / likely-manipulator / heavily-pledged name. A metric that
    can't be computed never rejects (we only drop on a *failing* value, never a missing one)."""
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


def scan(con: duckdb.DuckDBPyConnection, *, limit: int = 20,
         min_turnover_cr: float = _MIN_TURNOVER_CR) -> list[dict]:
    """Ranked momentum-breakout screen. Returns ``[{symbol, name, sector, price, pct_from_high,
    breakout, vol_surge, deliv_ratio, trend, turnover_cr}, …]`` best-first — the top ``limit`` liquid
    names near a 52-week high, in an uptrend, with a volume surge, that clear the trap gate."""
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT symbol, trade_date, close, high,
                   ttl_trd_qnty AS vol, deliv_per, turnover_lacs,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM equity_eod
            WHERE series IN ('EQ', 'BE', 'BZ')
              AND symbol IN (SELECT symbol FROM equity_master)   -- real listed equities (no ETFs/bonds)
        ),
        liq AS (
            SELECT symbol, avg(turnover_lacs) / 100.0 AS turn_cr
            FROM ranked WHERE rn <= ?
            GROUP BY symbol
            HAVING avg(turnover_lacs) / 100.0 >= ?
        ),
        win AS (
            SELECT r.* FROM ranked r JOIN liq USING (symbol) WHERE r.rn <= 252
        )
        SELECT w.symbol,
               arg_max(w.close, w.trade_date)              AS last_close,
               arg_max(w.vol, w.trade_date)                AS last_vol,
               arg_max(w.deliv_per, w.trade_date)          AS last_deliv,
               max(w.high)                                 AS high_252,
               avg(w.close)     FILTER (WHERE w.rn <= 200) AS sma200,
               avg(w.close)     FILTER (WHERE w.rn <= 50)  AS sma50,
               avg(w.vol)       FILTER (WHERE w.rn <= 20)  AS vol_avg20,
               avg(w.deliv_per) FILTER (WHERE w.rn <= 20)  AS deliv_avg20,
               max(l.turn_cr)                              AS turn_cr,
               count(*)                                    AS n
        FROM win w JOIN liq l USING (symbol)
        GROUP BY w.symbol
        """,
        [_LIQ_WINDOW, min_turnover_cr]).fetchdf()
    if rows.empty:
        return []

    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())   # nicer names win
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())

    cands: list[dict] = []
    for r in rows.itertuples(index=False):
        if r.n < _MIN_DAYS or r.last_close is None or not r.high_252:
            continue
        if r.sma200 is None or r.sma50 is None:
            continue
        trend_ok = r.last_close > r.sma200 and r.sma50 > r.sma200      # uptrend + golden-cross regime
        if not trend_ok:
            continue
        pct_from_high = r.last_close / r.high_252 - 1                  # 0 = at the high, negative = below
        if pct_from_high < _NEAR_HIGH:
            continue
        vol_surge = (r.last_vol / r.vol_avg20) if r.vol_avg20 else None
        if vol_surge is None or vol_surge != vol_surge or vol_surge < _MIN_VOL_SURGE:
            continue
        deliv_ok = (r.deliv_avg20 and r.deliv_avg20 == r.deliv_avg20
                    and r.last_deliv == r.last_deliv)
        deliv_ratio = (r.last_deliv / r.deliv_avg20) if deliv_ok else 1.0
        cands.append({
            "symbol": r.symbol, "name": names.get(r.symbol, r.symbol),
            "sector": sectors.get(r.symbol) or "—", "price": float(r.last_close),
            "pct_from_high": round(100 * pct_from_high, 1),
            "vol_surge": round(vol_surge, 1), "deliv_ratio": round(deliv_ratio, 1),
            "turnover_cr": round(float(r.turn_cr), 1) if r.turn_cr else None,
            # scoring inputs (capped so one outlier doesn't dominate the rank-normalisation)
            "prox": min(pct_from_high, 0.0), "vol_signal": min(vol_surge, 5.0),
            "deliv_signal": min(deliv_ratio, 3.0),
        })
    if not cands:
        return []

    # rank-normalise each signal across the candidate set to [0,1], then weight into a score
    for raw_key in ("prox", "vol_signal", "deliv_signal"):
        screener._normalise(cands, raw_key)
    _wkey = {"prox": "prox", "vol_surge": "vol_signal", "deliv_ratio": "deliv_signal"}
    for c in cands:
        c["score"] = round(100 * sum(_WEIGHTS[k] * c[_wkey[k] + "_n"] for k in _SIGNALS), 1)
    cands.sort(key=lambda c: (-c["score"], c["symbol"]))

    out: list[dict] = []
    for c in cands[:_SHORTLIST_CAP]:
        try:
            if not _safe(con, c["symbol"]):
                continue
        except Exception:  # noqa: BLE001 — a scoring/forensic hiccup must not break the screen
            log.exception("trap gate failed for %s", c["symbol"])
        ph = c["pct_from_high"]
        c["breakout"] = "new high" if ph >= -0.5 else f"{abs(ph):.0f}% off high"
        c["trend"] = "↑ >200-DMA · 50>200"
        out.append({k: c[k] for k in ("symbol", "name", "sector", "price", "pct_from_high",
                                      "breakout", "vol_surge", "deliv_ratio", "trend", "turnover_cr")})
        if len(out) >= limit:
            break
    return out
