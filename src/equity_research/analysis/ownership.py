"""Ownership-change tracking — quarter-over-quarter diff of the holder-level SHP.

Reads the two most recent ``shp_holders`` snapshots (populated by
``ingest.ingest_shp_history``) and reports who **entered / exited / added / trimmed**,
with the percentage-point move. "Notable" holders — promoters, mutual funds, FPIs,
insurers, and listed-company holders (the Elcid pattern) — are flagged so real
conviction/distribution sorts above retail churn. Pure DB read; no scraping.
"""

from __future__ import annotations

import duckdb

# below this a new/exited holder is noise (tiny odd-lot promoter accounts, sub-1% public)
_APPEAR_FLOOR = 0.20        # % of shares
# below this a change in an existing holder's stake is noise
_DELTA_FLOOR = 0.10         # percentage points

_NOTABLE_CATEGORIES = {"mutual fund", "insurance company", "FPI", "bank / FI"}


def _is_notable(category: str, classification: str, is_promoter: bool) -> bool:
    return (is_promoter or classification == "LISTED company"
            or category in _NOTABLE_CATEGORIES)


def _quarters(con: duckdb.DuckDBPyConnection, symbol: str) -> list:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT as_of FROM shp_holders WHERE symbol = ? ORDER BY as_of DESC LIMIT 2",
        [symbol]).fetchall()]


def ownership_changes(con: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    """QoQ ownership diff for ``symbol`` between its two most recent SHP snapshots.

    Returns ``{as_of, prev_as_of, entered, exited, added, trimmed}`` where each list holds
    ``{name, category, classification, is_promoter, notable, pct, prev_pct, delta}`` (pct/delta
    as applicable), notable-first then by magnitude. ``None`` if fewer than two snapshots exist.
    """
    qs = _quarters(con, symbol)
    if len(qs) < 2:
        return None
    cur_as_of, prev_as_of = qs[0], qs[1]

    def _snap(as_of):
        return {r[0]: r for r in con.execute(
            "SELECT holder_name, pct, category, classification, is_promoter "
            "FROM shp_holders WHERE symbol = ? AND as_of = ?", [symbol, as_of]).fetchall()}

    cur, prev = _snap(cur_as_of), _snap(prev_as_of)
    entered, exited, added, trimmed = [], [], [], []

    for name, (_, pct, cat, cls, prom) in cur.items():
        rec = {"name": name, "category": cat, "classification": cls, "is_promoter": prom,
               "notable": _is_notable(cat, cls, prom)}
        if name not in prev:
            if (pct or 0) >= _APPEAR_FLOOR:
                entered.append({**rec, "pct": pct})
        else:
            delta = (pct or 0) - (prev[name][1] or 0)
            if delta >= _DELTA_FLOOR:
                added.append({**rec, "pct": pct, "prev_pct": prev[name][1], "delta": delta})
            elif delta <= -_DELTA_FLOOR:
                trimmed.append({**rec, "pct": pct, "prev_pct": prev[name][1], "delta": delta})

    for name, (_, pct, cat, cls, prom) in prev.items():
        if name not in cur and (pct or 0) >= _APPEAR_FLOOR:
            exited.append({"name": name, "category": cat, "classification": cls,
                           "is_promoter": prom, "notable": _is_notable(cat, cls, prom),
                           "prev_pct": pct})

    def _rank_appear(x):        # notable first, then biggest stake
        return (not x["notable"], -(x.get("pct") or x.get("prev_pct") or 0))

    def _rank_delta(x):         # notable first, then biggest move
        return (not x["notable"], -abs(x["delta"]))

    entered.sort(key=_rank_appear)
    exited.sort(key=_rank_appear)
    added.sort(key=_rank_delta)
    trimmed.sort(key=_rank_delta)
    if not (entered or exited or added or trimmed):
        return {"as_of": cur_as_of, "prev_as_of": prev_as_of,
                "entered": [], "exited": [], "added": [], "trimmed": []}
    return {"as_of": cur_as_of, "prev_as_of": prev_as_of,
            "entered": entered, "exited": exited, "added": added, "trimmed": trimmed}
