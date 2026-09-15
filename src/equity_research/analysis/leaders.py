"""Relative-strength discovery (surfaced to users as **Market Beaters**, `screen: beaters`) —
surface the market's strongest performers, market-wide.

Answers "what is quietly outrunning the market?" — the names beating the broad index (Nifty 500)
over 3, 6 and 12 months while still trending up. Relative strength is one of the most persistent
edges in equities: leaders tend to keep leading. Ranks on price return vs the index alone, so it
spans the whole liquid equity universe (a surfaced name's financials are pulled on demand when you
open its report), with a light forensic trap gate on the shortlist.

Output is a ranked list; the screen *finds*, the deep report (reply a number) *diligences*.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.analysis import forensic, screener

log = logging.getLogger(__name__)

_BENCHMARK = "Nifty 500"
_MIN_TURNOVER_CR = 10.0     # higher floor than the breakout screen — relative-strength leadership is
_LIQ_WINDOW = 20            # only meaningful for names that are actually tradeable / institution-sized
_SHORTLIST_CAP = 60
_W3M, _W6M, _W1Y = 63, 126, 252        # trading-day windows (~3 / 6 / 12 months)

# Composite weights (sum 1.0): recent leadership counts most, but reward durable multi-horizon strength.
_WEIGHTS = {"out_3m": 0.50, "out_6m": 0.30, "out_1y": 0.20}


def _index_returns(con: duckdb.DuckDBPyConnection) -> dict | None:
    """Benchmark returns over the 3/6/12-month windows, or None if the series is too short."""
    idx = con.execute(
        "SELECT close FROM index_close WHERE index_name = ? ORDER BY trade_date", [_BENCHMARK]
    ).fetchall()
    closes = [c[0] for c in idx if c[0] is not None]
    if len(closes) <= _W1Y:
        return None
    last = closes[-1]
    return {"3m": last / closes[-1 - _W3M] - 1,
            "6m": last / closes[-1 - _W6M] - 1,
            "1y": last / closes[-1 - _W1Y] - 1}


def _safe(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Trap gate — drop near-distress / likely-manipulator / heavily-pledged names (missing metric
    never rejects)."""
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
    """Ranked relative-strength screen. Returns ``[{symbol, name, sector, price, ret_3m, ret_6m,
    ret_1y, out_3m, out_1y, trend, turnover_cr}, …]`` best-first — the top ``limit`` liquid names
    beating the benchmark over 3m/6m/12m and still in an uptrend, that clear the trap gate.
    ``out_*`` are percentage-point outperformance vs the benchmark."""
    idx = _index_returns(con)
    if idx is None:
        return []
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT symbol, trade_date, close, turnover_lacs,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM equity_eod
            WHERE series IN ('EQ', 'BE', 'BZ')
              AND symbol IN (SELECT symbol FROM equity_master)
        ),
        liq AS (
            SELECT symbol, avg(turnover_lacs) / 100.0 AS turn_cr
            FROM ranked WHERE rn <= ?
            GROUP BY symbol
            HAVING avg(turnover_lacs) / 100.0 >= ?
        )
        SELECT r.symbol,
               arg_max(r.close, r.trade_date)          AS last_close,
               max(r.close) FILTER (WHERE r.rn = ?)    AS close_3m,
               max(r.close) FILTER (WHERE r.rn = ?)    AS close_6m,
               max(r.close) FILTER (WHERE r.rn = ?)    AS close_1y,
               avg(r.close) FILTER (WHERE r.rn <= 200) AS sma200,
               max(l.turn_cr)                          AS turn_cr,
               count(*)                                AS n
        FROM ranked r JOIN liq l USING (symbol)
        WHERE r.rn <= 260
        GROUP BY r.symbol
        """,
        [_LIQ_WINDOW, min_turnover_cr, _W3M + 1, _W6M + 1, _W1Y + 1]).fetchdf()
    if rows.empty:
        return []

    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())

    cands: list[dict] = []
    for r in rows.itertuples(index=False):
        if r.n <= _W1Y or r.last_close is None or not r.close_3m or not r.close_6m or not r.close_1y:
            continue
        if r.sma200 is None or r.last_close <= r.sma200:          # must still be in an uptrend
            continue
        ret_3m = r.last_close / r.close_3m - 1
        ret_6m = r.last_close / r.close_6m - 1
        ret_1y = r.last_close / r.close_1y - 1
        out_3m, out_6m, out_1y = ret_3m - idx["3m"], ret_6m - idx["6m"], ret_1y - idx["1y"]
        if out_3m <= 0:                                            # must be leading the market now
            continue
        cands.append({
            "symbol": r.symbol, "name": names.get(r.symbol, r.symbol),
            "sector": sectors.get(r.symbol) or "—", "price": float(r.last_close),
            "ret_3m": round(100 * ret_3m, 1), "ret_6m": round(100 * ret_6m, 1),
            "ret_1y": round(100 * ret_1y, 1),
            "out_3m": round(100 * out_3m, 1), "out_1y": round(100 * out_1y, 1),
            "turnover_cr": round(float(r.turn_cr), 1) if r.turn_cr else None,
            # scoring inputs (percentage-point outperformance per horizon)
            "_out_3m": out_3m, "_out_6m": out_6m, "_out_1y": out_1y,
        })
    if not cands:
        return []

    for horizon in ("_out_3m", "_out_6m", "_out_1y"):
        screener._normalise(cands, horizon)
    for c in cands:
        c["score"] = round(100 * sum(_WEIGHTS[k] * c["_" + k + "_n"] for k in _WEIGHTS), 1)
    cands.sort(key=lambda c: (-c["score"], c["symbol"]))

    out: list[dict] = []
    for rank, c in enumerate(cands[:_SHORTLIST_CAP], 1):
        try:
            if not _safe(con, c["symbol"]):
                continue
        except Exception:  # noqa: BLE001
            log.exception("trap gate failed for %s", c["symbol"])
        c["rs_rank"] = len(out) + 1
        c["trend"] = "↑ >200-DMA"
        out.append({k: c[k] for k in ("symbol", "name", "sector", "price", "ret_3m", "ret_6m",
                                      "ret_1y", "out_3m", "out_1y", "rs_rank", "trend", "turnover_cr")})
        if len(out) >= limit:
            break
    return out
