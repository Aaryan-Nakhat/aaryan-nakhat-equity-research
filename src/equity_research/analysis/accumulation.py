"""Accumulation discovery (surfaced to users as **Institutional Buying**, `screen: institutions`) —
surface where insiders and big holders are quietly *adding*.

Answers "where is conviction money going in before the story is obvious?" A promoter raising their
own stake, or a marquee institution adding quarter-on-quarter, is one of the more reliable signals
there is — they know the business best and are buying with real money. This screen ranks the market
on **promoter-stake increase (QoQ)** enriched with the notable holders adding alongside them, from
the holder-level shareholding filings.

Bounded to symbols with holder-level shareholding ingested (coverage grows over time) — surfaced in
the email. Output is a ranked list; the screen *finds*, the deep report (reply a number) *diligences*.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research import config
from equity_research.analysis import ownership

log = logging.getLogger(__name__)

_MIN_PROMOTER_DELTA = config.ACCUM_MIN_PROMOTER_DELTA     # promoter stake up ≥ 0.10 pp QoQ to qualify on the promoter leg
_ENRICH_CAP = config.ACCUM_ENRICH_CAP               # how many top candidates get the (heavier) holder-level enrichment


def scan(con: duckdb.DuckDBPyConnection, *, limit: int = 20) -> list[dict]:
    """Ranked accumulation screen. Returns ``[{symbol, name, sector, price, promoter_delta,
    promoter_now, added, n_adders, as_of}, …]`` best-first — the top ``limit`` names where the
    promoter raised their stake QoQ, annotated with the notable holders also adding.
    ``promoter_delta`` is percentage points; ``added`` names the largest institutional adder."""
    prom = con.execute(
        """
        WITH pa AS (
            SELECT symbol, as_of,
                   sum(COALESCE(pct, 0)) FILTER (WHERE is_promoter) AS prom_pct
            FROM shp_holders GROUP BY symbol, as_of
        ),
        r AS (
            SELECT symbol, as_of, prom_pct,
                   row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) AS rq
            FROM pa
        ),
        d AS (
            SELECT symbol,
                   max(CASE WHEN rq = 1 THEN prom_pct END) AS prom_cur,
                   max(CASE WHEN rq = 1 THEN as_of END)    AS cur_asof,
                   max(CASE WHEN rq = 2 THEN prom_pct END) AS prom_prev,
                   count(*)                                AS nq
            FROM r WHERE rq <= 2 GROUP BY symbol
        )
        SELECT symbol, cur_asof, prom_cur, prom_prev, prom_cur - prom_prev AS prom_delta
        FROM d
        WHERE nq >= 2 AND prom_cur IS NOT NULL AND prom_prev IS NOT NULL
          AND prom_cur - prom_prev >= ?
        ORDER BY prom_delta DESC
        """,
        [_MIN_PROMOTER_DELTA]).fetchdf()
    if prom.empty:
        return []

    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())

    out: list[dict] = []
    for r in prom.head(_ENRICH_CAP).itertuples(index=False):
        sym = r.symbol
        added, n_adders, price = "—", 0, None
        try:
            ch = ownership.ownership_changes(con, sym)          # notable institutions adding this quarter
        except Exception:  # noqa: BLE001 — enrichment is best-effort, never fatal
            ch = None
        if ch:
            price = ch.get("current_price")
            adders = [a for a in ch.get("added", []) if not a.get("is_promoter")]
            n_adders = len(adders)
            top = next((a for a in adders if a.get("notable")), adders[0] if adders else None)
            if top:
                added = f"{top['name'][:24]} (+{top['delta']:.2f}pp)"
        out.append({
            "symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
            "price": float(price) if price else None,
            "promoter_delta": round(float(r.prom_delta), 2),
            "promoter_now": round(float(r.prom_cur), 2),
            "added": added, "n_adders": n_adders,
            "as_of": str(r.cur_asof)[:10],
        })
        if len(out) >= limit:
            break
    # promoter conviction leads; a bigger institutional cohort adding alongside breaks ties
    out.sort(key=lambda x: (-x["promoter_delta"], -x["n_adders"]))
    return out
