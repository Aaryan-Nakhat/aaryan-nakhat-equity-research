"""Hotlist — the names lighting up across *several* discovery engines at once.

Any single screen surfaces a long list; the signal that matters is **confluence** — a stock that is
breaking out *and* leading the market *and* being accumulated *and* screens cheap-and-clean is a far
higher-conviction lead than one flagged by a single engine. The Hotlist runs the discovery engines,
then ranks every surfaced name by **how many engines flag it** (and how highly each ranks it), so the
strongest multi-signal ideas float to the top.

A heavier build (it runs several screens), so it's computed once and cached for a day; the deep
report (reply a number) diligences each name.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research import config
from equity_research.analysis import accumulation, leaders, momentum, screener, smallcap

log = logging.getLogger(__name__)

# Each engine and how far down its ranked list a name still counts as "flagged".
_ENGINE_DEPTH = config.HOTLIST_ENGINE_DEPTH

# Engine → a per-engine weight (some signals are higher-conviction than others). Sum need not be 1;
# the aggregate score is weight-sum × position, so more engines + higher ranks = a higher score.
_ENGINE_WEIGHT = {
    "Volume Breakouts": 1.0, "Market Beaters": 1.0, "Institutional Buying": 1.2,
    "Value": 1.1, "Small-cap capex": 0.9,
}


def _engine_rows(con: duckdb.DuckDBPyConnection) -> dict[str, list[dict]]:
    """Run every discovery engine (each best-effort — one failing engine never sinks the Hotlist)."""
    runners = {
        "Volume Breakouts": lambda: momentum.scan(con, limit=_ENGINE_DEPTH),
        "Market Beaters": lambda: leaders.scan(con, limit=_ENGINE_DEPTH),
        "Institutional Buying": lambda: accumulation.scan(con, limit=_ENGINE_DEPTH),
        "Value": lambda: screener.fundamental_screen(con, limit=_ENGINE_DEPTH),
        "Small-cap capex": lambda: smallcap.smallcap_screen(con, limit=_ENGINE_DEPTH),
    }
    out: dict[str, list[dict]] = {}
    for label, fn in runners.items():
        try:
            out[label] = fn() or []
        except Exception:  # noqa: BLE001 — a single engine failing must not break the Hotlist
            log.exception("hotlist engine %s failed", label)
            out[label] = []
    return out


def build(con: duckdb.DuckDBPyConnection, *, limit: int = 25) -> list[dict]:
    """Ranked multi-signal Hotlist. Returns ``[{symbol, name, sector, price, n_signals, engines,
    score}, …]`` best-first — names flagged by the most engines (ties broken by aggregate strength).
    ``engines`` is the list of engine labels that surfaced the name."""
    engine_rows = _engine_rows(con)
    if not any(engine_rows.values()):
        return []

    agg: dict[str, dict] = {}
    for label, rows in engine_rows.items():
        n = len(rows)
        w = _ENGINE_WEIGHT.get(label, 1.0)
        for rank, r in enumerate(rows):
            sym = r.get("symbol")
            if not sym:
                continue
            pos = 1.0 - (rank / n) if n > 1 else 1.0            # 1.0 at the top of an engine's list
            a = agg.setdefault(sym, {"symbol": sym, "name": r.get("name") or sym,
                                     "engines": [], "score": 0.0, "price": None})
            a["engines"].append(label)
            a["score"] += w * pos
            if a["price"] is None and r.get("price"):
                a["price"] = r["price"]

    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())

    out = []
    for sym, a in agg.items():
        out.append({
            "symbol": sym, "name": names.get(sym, a["name"]),
            "sector": sectors.get(sym) or "—", "price": a["price"],
            "n_signals": len(a["engines"]), "engines": a["engines"],
            "score": round(a["score"], 2),
        })
    # most engines first, then aggregate strength — the multi-signal confluence is the whole point
    out.sort(key=lambda x: (-x["n_signals"], -x["score"], x["symbol"]))
    return out[:limit]
