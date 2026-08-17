"""Marquee-investor / HNI tracking — follow the famous names across the SHP data.

SEBI's public-shareholding pattern *names* every holder above ~1% of a company (and
every promoter regardless of size). Those names are already in ``shp_holders``. This
module inverts that: for a curated set of well-known Indian investors it finds **every
company they hold** and **what they did last quarter** (entered / exited / added /
trimmed), so you can piggy-back on their conviction.

Matching is deliberately **curated, not fuzzy** — each investor carries hand-verified
alias token-sets and a holder matches only when an alias is a *subset* of the holder's
name tokens (so 'Vijay Kedia' matches 'VIJAY KISHANLAL KEDIA' but 'damani' never
matches 'CHOODAMANI'). Pure DB read; no scraping.

Caveats the callers surface: only holders **disclosed by name** are visible (public
holders below ~1% aren't filed at all), and coverage is bounded by the SHP universe
ingested so far.
"""

from __future__ import annotations

import re
from collections import defaultdict

import duckdb

# A move (add/trim, or a new/exited position) below this is noise, not a signal.
DETECT_FLOOR = 0.5          # percentage points (add/trim) · % of shares (new/exit)

# Honorifics / vehicle words dropped before tokenising so they never have to appear in
# an alias. Real name tokens are kept (extra tokens never block a subset match).
_STOP = {
    "mr", "mrs", "ms", "smt", "shri", "sri", "shrimati", "dr", "late", "m", "s",
    "the", "and", "of", "huf", "karta", "jt", "jointly", "as", "trustee",
}


def norm_tokens(name: str) -> frozenset[str]:
    """Name → set of lowercased alphanumeric tokens (honorifics/vehicle words dropped,
    single-char initials dropped). Used for subset matching against an alias."""
    toks = re.split(r"[^a-z0-9]+", (name or "").lower())
    return frozenset(t for t in toks if len(t) > 1 and t not in _STOP)


# canonical display name → list of alias token-sets (a holder matches if ANY alias ⊆
# the holder's tokens). Entity aliases (PMS/securities vehicles) included where known.
# Verify/prune against the live DB with the module's __main__ probe.
_INVESTORS_RAW: dict[str, list[set[str]]] = {
    "Rekha Jhunjhunwala": [{"rekha", "jhunjhunwala"}, {"rakesh", "jhunjhunwala"},
                           {"aryaman", "jhunjhunwala"}],
    "Radhakishan Damani": [{"radhakishan", "damani"}],
    "Mukul Agrawal": [{"mukul", "agrawal"}],
    "Vijay Kedia": [{"vijay", "kedia"}, {"kedia", "securities"}],
    "Ashish Kacholia": [{"ashish", "kacholia"}],
    "Dolly Khanna": [{"dolly", "khanna"}, {"rajiv", "khanna"}],
    "Anil Kumar Goel": [{"anil", "goel"}],
    "Sunil Singhania": [{"sunil", "singhania"}, {"abakkus"}],
    "Ashish Dhawan": [{"ashish", "dhawan"}],
    "Porinju Veliyath": [{"porinju", "veliyath"}, {"porinju"}, {"equity", "intelligence"}],
    "Nemish Shah": [{"nemish", "shah"}],
    "Ramesh Damani": [{"ramesh", "damani"}],
    "Madhusudan Kela": [{"madhusudan", "kela"}],
    "Bhavook Tripathi": [{"bhavook", "tripathi"}, {"bhavook"}],
    "Dilipkumar Lakhi": [{"dilipkumar", "lakhi"}, {"dilip", "lakhi"}],
    "Akash Bhanshali": [{"akash", "bhanshali"}],
    "Vallabh Bhanshali": [{"vallabh", "bhanshali"}],
    "Hemendra Kothari": [{"hemendra", "kothari"}],
    "Raamdeo Agrawal": [{"raamdeo", "agrawal"}, {"ramdeo", "agrawal"}],
    "Mohnish Pabrai": [{"mohnish", "pabrai"}, {"pabrai"}],
    "Shankar Sharma / Devina Mehra": [{"shankar", "sharma"}, {"devina", "mehra"}],
    "Nikhil Vora": [{"nikhil", "vora"}],
    "Ajay Upadhyaya": [{"ajay", "upadhyaya"}],
    "Sanjay Bakshi": [{"sanjay", "bakshi"}],
    "Rajiv Jain": [{"rajiv", "jain"}],
}

_INVESTORS: dict[str, list[frozenset[str]]] = {
    canon: [frozenset(a) for a in aliases] for canon, aliases in _INVESTORS_RAW.items()
}


def roster() -> list[str]:
    """The curated canonical investor names."""
    return list(_INVESTORS)


def match_investor(holder_name: str) -> str | None:
    """Canonical investor for a raw SHP holder name, or None. First alias whose token-set
    is a subset of the holder's tokens wins."""
    toks = norm_tokens(holder_name)
    if not toks:
        return None
    for canon, aliases in _INVESTORS.items():
        if any(alias <= toks for alias in aliases):
            return canon
    return None


def resolve(query: str) -> str | None:
    """Free-text ('mukul agrawal', 'kedia') → a canonical investor name. Matches when the
    query tokens are a subset of a canonical name's tokens or hit one of its aliases."""
    q = norm_tokens(query)
    if not q:
        return None
    for canon in _INVESTORS:
        if q <= norm_tokens(canon):
            return canon
    for canon, aliases in _INVESTORS.items():
        if any(alias <= q or q <= alias for alias in aliases):
            return canon
    return None


def _display_names(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    names = {s: c for s, c in con.execute(
        "SELECT symbol, company FROM sector_map WHERE company IS NOT NULL").fetchall()}
    for s, c in con.execute(
            "SELECT symbol, company_name FROM equity_master WHERE company_name IS NOT NULL").fetchall():
        names.setdefault(s, c)
    return names


def _positions(con: duckdb.DuckDBPyConnection):
    """Scan shp_holders once → (positions, sym_quarters).

    ``positions[canon][symbol][as_of]`` = summed % across that investor's vehicles
    (self + HUF + trust) in that company that quarter. ``sym_quarters[symbol]`` = the
    symbol's snapshot dates, newest first.
    """
    positions: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    sym_quarters: dict[str, set] = defaultdict(set)
    for symbol, as_of, holder, pct in con.execute(
            "SELECT symbol, as_of, holder_name, pct FROM shp_holders").fetchall():
        sym_quarters[symbol].add(as_of)
        canon = match_investor(holder)
        if canon is None:
            continue
        cur = positions[canon][symbol]
        cur[as_of] = (cur.get(as_of) or 0.0) + (pct or 0.0)
    sq = {s: sorted(q, reverse=True) for s, q in sym_quarters.items()}
    return positions, sq


def _holding_cost(con: duckdb.DuckDBPyConnection, symbol: str, by_q: dict,
                  quarters_desc: list) -> dict:
    """This investor's inferred **cost zone** in ``symbol`` vs the current price → a
    profit-booking-risk read. Cost is inferred from the price range of the quarters they added
    in (exact prices aren't disclosed); a position held since our earliest snapshot is
    cost-unknown. Reuses the ``ownership`` helpers. ``{}`` if not computable."""
    from equity_research.analysis import ownership
    qs = sorted(quarters_desc)                                 # ascending
    if len(qs) < 2:
        return {}
    cur = ownership.current_price(con, symbol)
    if not cur:
        return {}
    held_before = (by_q.get(qs[0]) or 0.0) > 0
    adds = []
    for i in range(1, len(qs)):
        d = (by_q.get(qs[i]) or 0.0) - (by_q.get(qs[i - 1]) or 0.0)
        if d > 0.05:
            z = ownership._price_window(con, symbol, qs[i - 1], qs[i])
            if z:
                adds.append((d, z))
    if adds:
        wsum = sum(d for d, _ in adds)
        avg = sum(d * z["avg"] for d, z in adds) / wsum
        lo, hi = min(z["lo"] for _, z in adds), max(z["hi"] for _, z in adds)
        gain = (cur - avg) / avg * 100
    elif held_before:
        z = ownership._price_window(con, symbol, qs[0], qs[-1])
        avg, gain = None, None
        lo, hi = (z["lo"], z["hi"]) if z else (None, None)
    else:
        return {}
    emoji, read = ownership.booking_flag(gain)
    return {"cost_avg": avg, "zone_lo": lo, "zone_hi": hi, "gain_pct": gain,
            "cost_emoji": emoji, "cost_read": read, "current_price": cur}


def holdings(con: duckdb.DuckDBPyConnection, canonical: str) -> list[dict] | None:
    """An investor's current book: ``[{symbol, name, as_of, pct, + cost fields}]`` at each
    company's latest snapshot, biggest stake first. Each row also carries the investor's inferred
    **cost zone vs current price** and a booking-risk read (see ``_holding_cost``). None if the
    name isn't tracked."""
    if canonical not in _INVESTORS:
        return None
    positions, sq = _positions(con)
    names = _display_names(con)
    out: list[dict] = []
    for symbol, by_q in positions.get(canonical, {}).items():
        latest = sq[symbol][0]
        pct = by_q.get(latest)
        if pct and pct > 0:
            row = {"symbol": symbol, "name": names.get(symbol, symbol),
                   "as_of": latest, "pct": pct}
            row.update(_holding_cost(con, symbol, by_q, sq[symbol]))
            out.append(row)
    out.sort(key=lambda r: r["pct"], reverse=True)
    return out


def _moves_from(positions_for: dict, sq: dict, names: dict) -> dict:
    """entered / exited / added / trimmed for one investor's positions dict."""
    entered, exited, added, trimmed = [], [], [], []
    for symbol, by_q in positions_for.items():
        qs = sq.get(symbol, [])
        if len(qs) < 2:
            continue
        cur_q, prev_q = qs[0], qs[1]
        cur = by_q.get(cur_q) or 0.0
        prev = by_q.get(prev_q) or 0.0
        rec = {"symbol": symbol, "name": names.get(symbol, symbol),
               "pct": cur, "prev_pct": prev, "as_of": cur_q}
        if prev <= 0 and cur >= DETECT_FLOOR:
            entered.append(rec)
        elif cur <= 0 and prev >= DETECT_FLOOR:
            exited.append(rec)
        elif cur - prev >= DETECT_FLOOR:
            added.append({**rec, "delta": cur - prev})
        elif prev - cur >= DETECT_FLOOR:
            trimmed.append({**rec, "delta": cur - prev})
    entered.sort(key=lambda r: -r["pct"])
    exited.sort(key=lambda r: -r["prev_pct"])
    added.sort(key=lambda r: -r["delta"])
    trimmed.sort(key=lambda r: r["delta"])
    return {"entered": entered, "exited": exited, "added": added, "trimmed": trimmed}


def moves(con: duckdb.DuckDBPyConnection, canonical: str) -> dict | None:
    """Last-quarter moves for one investor (entered/exited/added/trimmed, ≥0.5 floor).
    None if untracked; the lists may all be empty if they stood pat."""
    if canonical not in _INVESTORS:
        return None
    positions, sq = _positions(con)
    return _moves_from(positions.get(canonical, {}), sq, _display_names(con))


def all_moves(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """``{investor: moves}`` for every tracked investor who moved last quarter — for the
    proactive digest. Investors who stood pat (or have no coverage) are omitted."""
    positions, sq = _positions(con)
    names = _display_names(con)
    out: dict[str, dict] = {}
    for canon in _INVESTORS:
        m = _moves_from(positions.get(canon, {}), sq, names)
        if m["entered"] or m["exited"] or m["added"] or m["trimmed"]:
            out[canon] = m
    return out


if __name__ == "__main__":  # read-only alias verification probe
    import duckdb as _d

    from equity_research.common.db import DEFAULT_DB_PATH
    c = _d.connect(str(DEFAULT_DB_PATH), read_only=True)
    pos, _sq = _positions(c)
    print(f"{'investor':32} symbols  sample holders")
    for canon in _INVESTORS:
        syms = pos.get(canon, {})
        raw = c.execute(
            "SELECT DISTINCT holder_name FROM shp_holders").fetchall()
        matched = sorted({h[0] for h in raw if match_investor(h[0]) == canon})
        print(f"{canon:32} {len(syms):>7}  {matched[:3]}")
    c.close()
