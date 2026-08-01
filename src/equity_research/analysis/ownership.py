"""Ownership-change tracking — quarter-over-quarter diff of the holder-level SHP.

Reads the two most recent ``shp_holders`` snapshots (populated by
``ingest.ingest_shp_history``) and reports who **entered / exited / added / trimmed**,
with the percentage-point move. "Notable" holders — promoters, mutual funds, FPIs,
insurers, and listed-company holders (the Elcid pattern) — are flagged so real
conviction/distribution sorts above retail churn. Pure DB read; no scraping.

Matching across quarters is **name-normalised** (case/punctuation-insensitive) with a
conservative **token-subset** fallback, so the *same* holder disclosed under a slightly
different label (``LIFE INSURANCE CORPORATION OF INDIA`` vs ``Life Insurance Corporation
of India``; ``QUANT MUTUAL FUND`` vs ``QUANT MUTUAL FUND - QUANT ARBITRAGE FUND``) reads
as one continuous ``x% → y%`` move, not a spurious exit-and-re-entry.
"""

from __future__ import annotations

import re

import duckdb

# below this a new/exited holder is noise (tiny odd-lot promoter accounts, sub-1% public)
_APPEAR_FLOOR = 0.20        # % of shares
# below this a change in an existing holder's stake is noise
_DELTA_FLOOR = 0.10         # percentage points

_NOTABLE_CATEGORIES = {"mutual fund", "insurance company", "FPI", "bank / FI"}


def _is_notable(category: str, classification: str, is_promoter: bool) -> bool:
    return (is_promoter or classification == "LISTED company"
            or category in _NOTABLE_CATEGORIES)


def _norm(name: str) -> str:
    """Case/punctuation-insensitive key: lowercase, non-alphanumerics → spaces, collapsed."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def _quarters(con: duckdb.DuckDBPyConnection, symbol: str) -> list:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT as_of FROM shp_holders WHERE symbol = ? ORDER BY as_of DESC LIMIT 2",
        [symbol]).fetchall()]


def _match(cur: list[dict], prev: list[dict]) -> list[tuple[dict | None, dict | None]]:
    """Pair current-quarter holders with previous-quarter holders so the same holder is
    tracked across a relabel. Returns ``[(cur_rec|None, prev_rec|None), ...]`` — a filled
    pair is a continuing holder, ``(rec, None)`` a new entrant, ``(None, rec)`` an exit.

    Pass 1: identical normalised name. Pass 2 (fallback): one holder's token set is a strict
    subset of the other's (a scheme-family relabel), guarded to the same promoter flag and
    category so distinct holders are never merged. Both passes are bijective (greedy)."""
    pairs: list[tuple[dict | None, dict | None]] = []
    cur_left = list(cur)
    prev_by_norm: dict[str, list[dict]] = {}
    for p in prev:
        prev_by_norm.setdefault(p["norm"], []).append(p)

    # pass 1 — exact normalised-name match
    still: list[dict] = []
    for c in cur_left:
        bucket = prev_by_norm.get(c["norm"])
        if bucket:
            pairs.append((c, bucket.pop(0)))
        else:
            still.append(c)
    prev_left = [p for bucket in prev_by_norm.values() for p in bucket]

    # pass 2 — conservative token-subset fallback (same promoter flag + category)
    unmatched_cur: list[dict] = []
    for c in still:
        best, best_overlap = None, 0
        for p in prev_left:
            if p["is_promoter"] != c["is_promoter"] or p["category"] != c["category"]:
                continue
            a, b = c["toks"], p["toks"]
            if len(a) < 2 or len(b) < 2:
                continue
            if a < b or b < a:                       # strict subset either way
                overlap = len(a & b)
                if overlap > best_overlap:
                    best, best_overlap = p, overlap
        if best is not None:
            pairs.append((c, best))
            prev_left.remove(best)
        else:
            unmatched_cur.append(c)

    # pass 3 — filing re-spellings of the SAME holder (e.g. promoter "K NITYANANDA REDDY" one
    # quarter, "KAMBAM NITHYANANDA REDDY" the next). Match only when the promoter flag AND
    # category agree, the two share a real word (a token ≥4 chars — a surname/AMC, not an
    # initial), AND the stake is near-identical (≤0.05pp) — so genuinely different holders are
    # never merged. Greedy on the closest stake.
    still2: list[dict] = []
    for c in unmatched_cur:
        best, best_gap = None, 0.051
        c_words = {t for t in c["toks"] if len(t) >= 4}
        for p in prev_left:
            if p["is_promoter"] != c["is_promoter"] or p["category"] != c["category"]:
                continue
            if not (c_words & {t for t in p["toks"] if len(t) >= 4}):
                continue
            gap = abs(c["pct"] - p["pct"])
            if gap < best_gap:
                best, best_gap = p, gap
        if best is not None:
            pairs.append((c, best))
            prev_left.remove(best)
        else:
            still2.append(c)

    pairs += [(c, None) for c in still2]             # new entrants
    pairs += [(None, p) for p in prev_left]          # exits
    return pairs


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
        out = []
        for name, pct, cat, cls, prom in con.execute(
                "SELECT holder_name, pct, category, classification, is_promoter "
                "FROM shp_holders WHERE symbol = ? AND as_of = ?", [symbol, as_of]).fetchall():
            n = _norm(name)
            out.append({"name": name, "pct": pct or 0.0, "category": cat,
                        "classification": cls, "is_promoter": prom, "notable":
                        _is_notable(cat, cls, prom), "norm": n, "toks": frozenset(n.split())})
        return out

    cur, prev = _snap(cur_as_of), _snap(prev_as_of)
    entered, exited, added, trimmed = [], [], [], []

    for c, p in _match(cur, prev):
        if c is not None and p is None:                       # new entrant
            if c["pct"] >= _APPEAR_FLOOR:
                entered.append({**c, "prev_pct": 0.0})
        elif c is None and p is not None:                     # full exit
            if p["pct"] >= _APPEAR_FLOOR:
                exited.append({**p, "prev_pct": p["pct"], "pct": 0.0})
        else:                                                 # continuing holder
            delta = c["pct"] - p["pct"]
            rec = {**c, "prev_pct": p["pct"], "delta": delta}
            if delta >= _DELTA_FLOOR:
                added.append(rec)
            elif delta <= -_DELTA_FLOOR:
                trimmed.append(rec)

    def _rank_appear(x):        # notable first, then biggest stake
        return (not x["notable"], -(x.get("pct") or x.get("prev_pct") or 0))

    def _rank_delta(x):         # notable first, then biggest move
        return (not x["notable"], -abs(x["delta"]))

    entered.sort(key=_rank_appear)
    exited.sort(key=_rank_appear)
    added.sort(key=_rank_delta)
    trimmed.sort(key=_rank_delta)
    return {"as_of": cur_as_of, "prev_as_of": prev_as_of,
            "entered": entered, "exited": exited, "added": added, "trimmed": trimmed}
