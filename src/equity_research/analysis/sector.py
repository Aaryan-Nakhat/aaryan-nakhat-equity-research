"""Valuation vs sector — percentile-rank a stock's multiples against its peers.

Peers come from the `sector_map` (NSE index-constituent industry tags, ingested
via `ingest_sector_map`). Each peer's current P/E / P/B come from
`valuation.snapshot`, so a peer only participates if its financials are ingested.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import valuation


def industry_of(con: duckdb.DuckDBPyConnection, symbol: str) -> str | None:
    row = con.execute("SELECT industry FROM sector_map WHERE symbol = ?", [symbol]).fetchone()
    return row[0] if row else None


def peers(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    """Symbols sharing the target's industry (excluding the target)."""
    ind = industry_of(con, symbol)
    if ind is None:
        return []
    return [r[0] for r in con.execute(
        "SELECT symbol FROM sector_map WHERE industry = ? AND symbol <> ? ORDER BY symbol",
        [ind, symbol]).fetchall()]


def _pctile(values: list[float], x: float) -> float:
    """% of peer values strictly greater than x (so for P/E: % of peers more
    expensive => higher means the target is cheaper than that many peers).

    Robust to missing data: drops None/NaN peers, and returns NaN if the target
    value ``x`` is itself missing (e.g. banks have no standard P/E from our XBRL —
    ``None == None`` would otherwise slip past a NaN guard and crash the compare)."""
    vals = [v for v in values if v is not None and v == v]
    if not vals or x is None or x != x:
        return float("nan")
    return 100 * sum(1 for v in vals if v > x) / len(vals)


def sector_valuation(con: duckdb.DuckDBPyConnection, symbol: str,
                     consolidated: bool = False, *,
                     target_shares_override: float | None = None) -> dict:
    """Target P/E & P/B vs the sector peers that have valuation data.

    ``target_shares_override`` corrects the target's current shares for a
    bonus/split since its last annual filing (peers assume no such action).
    """
    ind = industry_of(con, symbol)
    if ind is None:
        return {"note": f"{symbol} not in sector_map (ingest_sector_map first)"}

    target = valuation.snapshot(con, symbol, consolidated,
                                shares_override=target_shares_override)
    rows = []
    for p in [symbol, *peers(con, symbol)]:
        ovr = target_shares_override if p == symbol else None
        s = valuation.snapshot(con, p, consolidated, shares_override=ovr)
        pe, pb = s.get("pe_ttm"), s.get("pb")
        if pe == pe and pe and pe > 0:        # finite, positive
            rows.append({"symbol": p, "pe": pe, "pb": pb})
    if not rows:
        return {"industry": ind, "note": "no peers with ingested financials yet"}

    df = pd.DataFrame(rows).set_index("symbol")
    peer_pe = [v for s, v in df["pe"].items() if s != symbol]
    peer_pb = [v for s, v in df["pb"].items() if s != symbol and v == v]
    t_pe, t_pb = target.get("pe_ttm"), target.get("pb")
    return {
        "industry": ind,
        "peers_with_data": len(peer_pe),
        "target_pe": t_pe,
        "sector_median_pe": float(np.median(df["pe"])),
        "pe_cheaper_than_%_of_peers": _pctile(peer_pe, t_pe) if t_pe == t_pe else np.nan,
        "target_pb": t_pb,
        "sector_median_pb": float(np.nanmedian(df["pb"])) if df["pb"].notna().any() else np.nan,
        "pb_cheaper_than_%_of_peers": _pctile(peer_pb, t_pb) if t_pb == t_pb else np.nan,
        "table": df.sort_values("pe"),
    }
