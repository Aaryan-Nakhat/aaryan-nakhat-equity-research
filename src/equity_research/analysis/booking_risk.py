"""Portfolio booking-risk heads-up — which of YOUR holdings have institutions sitting on big
gains (so an institutional profit-booking wave could hit the price).

For each holding, ``ownership.institutional_cost`` gives the stake-weighted gain the tracked
institutions are sitting on (inferred from the price zones of the quarters they added in). This
ranks your holdings by that gain — the ones on top are where the "smart money" is deepest in
profit and most likely to book. A heads-up, not a sell signal: a strong stock can keep running.
"""

from __future__ import annotations

import duckdb

from equity_research import watchlist
from equity_research.analysis import ownership


def portfolio_booking_risk(con: duckdb.DuckDBPyConnection,
                           list_type: str = "holding") -> dict:
    """Rank the user's ``list_type`` watchlist names by the gain institutions are sitting on.
    Returns ``{scored:[...], nodata:[...]}`` — ``scored`` rows carry ``avg_gain, emoji, read,
    n_high, top_holder`` (sorted highest-gain first); ``nodata`` = names without enough
    shareholding history to estimate a cost."""
    names = watchlist.entries_by_type(con, list_type)
    scored, nodata = [], []
    for sym, company in names:
        try:
            ic = ownership.institutional_cost(con, sym)
        except Exception:  # noqa: BLE001 — one thin name shouldn't break the scan
            ic = None
        if not ic or ic["summary"]["avg_gain_pct"] is None:
            nodata.append({"symbol": sym, "name": company or sym})
            continue
        s = ic["summary"]
        known_holders = [h for h in ic["holders"] if h["gain_pct"] is not None]
        top = max(known_holders, key=lambda h: h["gain_pct"], default=None)
        scored.append({
            "symbol": sym, "name": company or sym, "current_price": ic["current_price"],
            "avg_gain": s["avg_gain_pct"], "emoji": s["emoji"], "read": s["read"],
            "n_high": sum(1 for h in known_holders if h["gain_pct"] >= 50),
            "known": s["known"], "top_holder": top,
        })
    scored.sort(key=lambda r: -r["avg_gain"])          # deepest-in-profit (highest risk) first
    return {"scored": scored, "nodata": nodata}
