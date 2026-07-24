"""Fundamental + forensic universe screener — *find* ideas, don't just analyse named ones.

Ranks every symbol that has ingested financials on a composite of three pillars, each
cross-sectionally normalised to [0, 1] within the scored set:

- **Quality**   — Piotroski F (0-9 fundamental-strength checklist).
- **Forensic**  — Altman Z (solvency), Beneish M (clean earnings), Sloan accruals
                  (cash-backed profit), and no promoter pledge.
- **Cheapness** — the current P/E (or P/B for financials) as a *low percentile of the
                  stock's own history* — cheap vs where it usually trades.

All three pillars reuse the deterministic analysis layer (``forensic``, ``valuation``,
``sector``); nothing here re-derives numbers. The output is a ranked list — the screen
*finds*, the LLM deep-report *diligences* (delivered by replying a number to the email).
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.analysis import forensic, sector, valuation

log = logging.getLogger(__name__)

_WEIGHTS = {"quality": 0.40, "forensic": 0.35, "cheapness": 0.25}


def _forensic_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """0-4 forensic health: +1 each for Altman-safe, Beneish-clean, low accruals, no pledge."""
    score, seen = 0.0, 0
    z = forensic.altman_z(con, symbol)
    if z.value is not None:
        seen += 1
        score += 1 if z.value > 2.99 else 0.5 if z.value >= 1.81 else 0
    m = forensic.beneish_m(con, symbol)
    if m.value is not None:
        seen += 1
        score += 1 if m.value <= -1.78 else 0
    acc = forensic.accruals(con, symbol)
    if acc.value is not None:
        seen += 1
        score += 1 if acc.value <= 10 else 0.5 if acc.value <= 25 else 0
    pledge = con.execute(
        "SELECT pledged_pct_of_promoter FROM shareholding WHERE symbol = ? "
        "ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
    if pledge is not None:
        seen += 1
        p = pledge[0] or 0
        score += 1 if p == 0 else 0.5 if p < 25 else 0
    return score / seen * 4 if seen else None            # scale to a 0-4 range on available checks


def _cheapness_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """100 − own-history percentile of the current multiple (P/B for financials, else P/E).
    Higher = cheaper vs where the stock usually trades. None if history is too thin."""
    lens = sector.valuation_lens(sector.industry_of(con, symbol))
    hist = valuation.valuation_history(con, symbol)
    snap = valuation.snapshot(con, symbol)
    if hist.empty or not snap:
        return None
    col = "pb" if lens == "financial" else "pe"
    cur = snap.get("pb") if lens == "financial" else snap.get("pe_ttm")
    if cur is None or cur != cur or cur <= 0:
        return None
    series = [v for v in hist[col].tolist() if v == v and v > 0]
    if len(series) < 3:
        return None
    pctile = valuation.multiple_percentile(series, cur)
    return None if pctile is None else 100 - pctile


def _quality_raw(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    f = forensic.piotroski_f(con, symbol)
    return float(f.value) if f.value is not None else None


def _normalise(rows: list[dict], key: str) -> None:
    """Rank-normalise ``rows[*][key]`` to ``rows[*][key+'_n']`` in [0,1] (ties share a rank);
    a missing raw value maps to the middle (0.5) so one absent pillar doesn't sink a name."""
    vals = sorted({r[key] for r in rows if r.get(key) is not None})
    if not vals:
        for r in rows:
            r[key + "_n"] = 0.5
        return
    rank = {v: i / (len(vals) - 1) if len(vals) > 1 else 1.0 for i, v in enumerate(vals)}
    for r in rows:
        r[key + "_n"] = rank[r[key]] if r.get(key) is not None else 0.5


def fundamental_screen(con: duckdb.DuckDBPyConnection, *, universe: str = "NIFTY500",
                       limit: int = 25) -> list[dict]:
    """Ranked value+quality+forensic screen over symbols with financials.

    Restricted to ``sector_map.universe`` (default NIFTY500). Returns
    ``[{symbol, name, composite, quality, forensic, cheapness, pe, pb, why}, …]``,
    best composite first. Best-effort: a symbol missing a pillar is scored on the rest.
    """
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT f.symbol FROM financials f "
        "JOIN sector_map s ON s.symbol = f.symbol WHERE s.universe = ?", [universe]).fetchall()]
    names = dict(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    rows: list[dict] = []
    for sym in syms:
        try:
            q, fo, ch = (_quality_raw(con, sym), _forensic_raw(con, sym), _cheapness_raw(con, sym))
        except Exception:  # noqa: BLE001 — a thin name shouldn't break the screen
            log.exception("screen scoring failed for %s", sym)
            continue
        if q is None and fo is None and ch is None:
            continue
        snap = valuation.snapshot(con, sym) or {}
        rows.append({"symbol": sym, "name": names.get(sym, sym),
                     "quality": q, "forensic": fo, "cheapness": ch,
                     "pe": snap.get("pe_ttm"), "pb": snap.get("pb")})
    if not rows:
        return []
    for key in ("quality", "forensic", "cheapness"):
        _normalise(rows, key)
    for r in rows:
        r["composite"] = round(100 * (
            _WEIGHTS["quality"] * r["quality_n"]
            + _WEIGHTS["forensic"] * r["forensic_n"]
            + _WEIGHTS["cheapness"] * r["cheapness_n"]), 1)
        r["why"] = _why(r)
    rows.sort(key=lambda r: r["composite"], reverse=True)
    return rows[:limit]


def _why(r: dict) -> str:
    bits = []
    if r.get("quality") is not None:
        bits.append(f"Piotroski {r['quality']:.0f}/9")
    if r.get("forensic") is not None:
        bits.append(f"forensic {r['forensic']:.1f}/4")
    if r.get("cheapness") is not None:
        bits.append(f"cheaper than {r['cheapness']:.0f}% of its own history")
    return " · ".join(bits)
