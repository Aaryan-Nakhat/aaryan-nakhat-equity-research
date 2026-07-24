"""Holdco-discount screener — the Elcid trade, generalised.

A *holding company* is a listed vehicle whose main assets are stakes in **other listed**
companies. When the market value of those stakes exceeds the holdco's own market cap, it
trades at a **holdco discount** — the situation where Elcid Investments (market cap a few
hundred crore) sat on a ~₹8,000 cr stake in Asian Paints.

We already record, for each analysed company, its holders classified as ``LISTED company``
(``shp_holders``). Inverting that mapping gives, for a listed holder, the stakes it owns:
``holder → [(investee, pct), …]``. Valuing each stake (``pct% × investee market cap``) and
comparing the sum to the holder's own market cap surfaces holdco discounts across whatever
SHP coverage exists. Only **disclosed listed** stakes count (promoter table + public >1%);
unlisted subsidiaries are not valued.
"""

from __future__ import annotations

from collections import defaultdict

import duckdb

from equity_research.analysis import valuation

CR = 1e7


def _mcap_cr(con: duckdb.DuckDBPyConnection, symbol: str, cache: dict) -> float | None:
    """Current market cap (₹cr) for ``symbol`` — cached per screen run. Needs shares from
    ingested financials × the latest EOD close (``valuation.market_cap``)."""
    if symbol in cache:
        return cache[symbol]
    mc = None
    try:
        rupees = valuation.market_cap(con, symbol)          # prefers standalone; None if no shares
        mc = rupees / CR if rupees else None
    except Exception:  # noqa: BLE001 — a thin name shouldn't break the screen
        mc = None
    cache[symbol] = mc
    return mc


def _reverse_index(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str, float]]]:
    """``holder_symbol → [(investee_symbol, investee_name, pct), …]`` from each company's
    latest SHP snapshot, for holders that are themselves NSE-listed."""
    rows = con.execute(
        "SELECT h.matched_symbol, h.symbol, h.pct, COALESCE(s.company, h.symbol) "
        "FROM shp_holders h "
        "LEFT JOIN sector_map s ON s.symbol = h.symbol "
        "WHERE h.classification = 'LISTED company' AND h.matched_symbol IS NOT NULL "
        "AND h.as_of = (SELECT max(as_of) FROM shp_holders x WHERE x.symbol = h.symbol)"
    ).fetchall()
    idx: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for holder, investee, pct, investee_name in rows:
        if pct and pct > 0:
            idx[holder].append((investee, investee_name, pct))
    return idx


def holdco_discounts(con: duckdb.DuckDBPyConnection, *, min_nav_ratio: float = 0.5,
                     min_nav_cr: float = 500.0, limit: int = 30) -> list[dict]:
    """Listed holders ranked by holdco discount (stake NAV vs own market cap).

    ``min_nav_ratio`` keeps only holders whose disclosed listed-stake NAV is at least this
    fraction of their own market cap (filters incidental cross-holdings down to genuine
    holdcos). When a holder's own market cap is unknown (no financials ingested), it can't be
    ratio-filtered, so it's kept only if its absolute stake NAV clears ``min_nav_cr`` — that
    drops trivial cross-holdings (a large operating company owning 0.1% of something) while
    keeping big holdcos worth a look (e.g. Kama Holdings' ~₹39k cr SRF stake). Returns
    ``[{holder, holder_name, own_mcap_cr, stake_nav_cr, discount_pct, n_stakes, priced_stakes,
    top_stakes:[(investee, name, pct, value_cr)]}, …]``, deepest discount first (unknown-own
    last). Best-effort — a holder/investee we can't price is skipped or partial.
    """
    idx = _reverse_index(con)
    if not idx:
        return []
    names = dict(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    cache: dict = {}
    out: list[dict] = []
    for holder, stakes in idx.items():
        priced = []
        nav_cr = 0.0
        for investee, iname, pct in stakes:
            mc = _mcap_cr(con, investee, cache)
            if mc is None:
                continue
            val = pct / 100.0 * mc
            nav_cr += val
            priced.append((investee, iname, pct, val))
        if nav_cr <= 0:
            continue
        own = _mcap_cr(con, holder, cache)
        # keep genuine holdcos: NAV material vs own mcap; when own mcap is unknown, fall back
        # to an absolute-NAV floor so trivial cross-holdings don't leak in.
        if own is not None:
            if nav_cr < min_nav_ratio * own:
                continue
        elif nav_cr < min_nav_cr:
            continue
        discount = (1 - own / nav_cr) if (own and nav_cr) else None
        priced.sort(key=lambda t: t[3], reverse=True)
        out.append({
            "holder": holder, "holder_name": names.get(holder, holder),
            "own_mcap_cr": own, "stake_nav_cr": nav_cr,
            "discount_pct": (discount * 100 if discount is not None else None),
            "n_stakes": len(stakes), "priced_stakes": len(priced),
            "top_stakes": priced[:5],
        })
    # deepest discount first; holders with an unknown own-mcap (discount None) sort last
    out.sort(key=lambda r: (r["discount_pct"] is None, -(r["discount_pct"] or 0)))
    return out[:limit]
