"""Fundamental-discovery screens — find businesses by *quality*, not price action.

Three screens over the symbols that have financials ingested (the investable universe), each ranked
and forensic-trap-gated, returning a list → reply a number → deep report:

- **Margin Momentum** — margins expanding on growing revenue.
- **Debt Payers**     — cutting debt over the years while staying profitable (healthy deleveraging).
- **Compounders**     — high ROCE, low debt, steady multi-year growth, clean books.

All deterministic — reuses the ratio / forensic / normalisation layer; nothing re-derives numbers.
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from equity_research.analysis import forensic, fundamentals, screener

log = logging.getLogger(__name__)


def _universe(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials ORDER BY symbol").fetchall()]


def _names(con: duckdb.DuckDBPyConnection) -> dict:
    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    return names


def _safe(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Trap gate — drop near-distress / likely-manipulator / heavily-pledged names (a missing metric
    never rejects; we only drop on a *failing* value)."""
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


def _annual(con: duckdb.DuckDBPyConnection, symbol: str) -> pd.DataFrame:
    """The wide annual frame, preferring consolidated, falling back to standalone (empty if neither)."""
    af = fundamentals.load_annual(con, symbol, True)
    if af.empty:
        af = fundamentals.load_annual(con, symbol, False)
    return af


def _col(af: pd.DataFrame, name: str) -> pd.Series:
    return af[name] if name in af.columns else pd.Series(dtype=float)


def _roce_de(af: pd.DataFrame) -> tuple[float | None, float | None]:
    """(ROCE%, D/E) from the latest annual row — computed directly (no price lookup)."""
    if af.empty:
        return None, None
    row = af.loc[af.index[-1]]

    def g(el):
        v = row.get(el)
        return float(v) if v is not None and v == v else None

    eq, pbt, fin = g("Equity"), g("ProfitBeforeTax"), g("FinanceCosts")
    debt = (g("BorrowingsCurrent") or 0.0) + (g("BorrowingsNoncurrent") or 0.0)
    roce = 100 * (pbt + fin) / (eq + debt) if (pbt is not None and fin is not None and eq and (eq + debt)) else None
    de = debt / eq if eq else None
    if roce is not None and not (-200 <= roce <= 300):
        roce = None
    if de is not None and not (0 <= de <= 50):
        de = None
    return roce, de


def _finish(con, rows, keys, weights, *, limit):
    """Shared tail: rank-normalise each key, weight into a 0-100 score, trap-gate the shortlist,
    return the top ``limit`` (best-first)."""
    if not rows:
        return []
    for k in keys:
        screener._normalise(rows, k)
    for r in rows:
        r["score"] = round(100 * sum(weights[k] * r[k + "_n"] for k in keys), 1)
    rows.sort(key=lambda r: (-r["score"], r["symbol"]))
    out = []
    for r in rows[: max(limit * 3, 60)]:
        try:
            if not _safe(con, r["symbol"]):
                continue
        except Exception:  # noqa: BLE001
            log.exception("trap gate failed for %s", r["symbol"])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def margin_momentum(con: duckdb.DuckDBPyConnection, *, limit: int = 20) -> list[dict]:
    """Names whose net margin is **expanding** (latest quarter vs the prior ~4) on **growing** revenue.
    Returns ``[{symbol, name, sector, net_margin, margin_bps, rev_yoy, score}, …]`` best-first."""
    names, sectors = _names(con), dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())
    rows = []
    for sym in _universe(con):
        try:
            m = fundamentals.latest_quarters(con, sym)
        except Exception:  # noqa: BLE001
            continue
        if m.empty or "net_margin_%" not in m:
            continue
        nm = m["net_margin_%"].dropna()
        if len(nm) < 5:
            continue
        latest, base = float(nm.iloc[-1]), float(nm.iloc[-5:-1].mean())
        if not (-100 <= latest <= 100) or not (-100 <= base <= 100):
            continue                                               # implausible margin — holdco/trader
                                                                   # artifact (RevenueFromOperations
                                                                   # excludes the bulk of their income)
        margin_bps = (latest - base) * 100                         # pp → bps
        rev = m["rev_yoy_%"].iloc[-1] if "rev_yoy_%" in m else None
        rev = float(rev) if (rev is not None and rev == rev) else None
        if not (0 < margin_bps <= 3000) or rev is None or rev <= 0:  # a real expansion (≤30pp), on growth
            continue
        rows.append({"symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
                     "net_margin": round(latest, 1), "margin_bps": round(margin_bps),
                     "rev_yoy": round(rev, 1),
                     "_exp": min(margin_bps, 1500.0), "_rev": min(rev, 60.0)})
    return _finish(con, rows, ["_exp", "_rev"], {"_exp": 0.7, "_rev": 0.3}, limit=limit)


def debt_payers(con: duckdb.DuckDBPyConnection, *, limit: int = 20) -> list[dict]:
    """Names that **cut debt** over the last ~3-4 years while staying profitable (ROCE > 0). Returns
    ``[{symbol, name, sector, de, debt_cut_pct, roce, score}, …]`` best-first."""
    names, sectors = _names(con), dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())
    rows = []
    for sym in _universe(con):
        try:
            af = _annual(con, sym)
        except Exception:  # noqa: BLE001
            continue
        if af.empty:
            continue
        debt = _col(af, "BorrowingsCurrent").fillna(0) + _col(af, "BorrowingsNoncurrent").fillna(0)
        debt = debt[debt.index.notna()].dropna()
        if len(debt) < 2:
            continue
        latest = float(debt.iloc[-1])
        earliest = float(debt.iloc[max(0, len(debt) - 4)])         # ~3-4 FYs back
        if earliest <= 0:
            continue
        cut_pct = (earliest - latest) / earliest * 100
        roce, de = _roce_de(af)
        if cut_pct <= 5 or roce is None or roce <= 0:              # real cut + healthy, not distress
            continue
        rows.append({"symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
                     "de": round(de, 2) if de is not None else None,
                     "debt_cut_pct": round(cut_pct), "roce": round(roce, 1),
                     "_cut": min(cut_pct, 100.0), "_roce": min(roce, 50.0)})
    return _finish(con, rows, ["_cut", "_roce"], {"_cut": 0.6, "_roce": 0.4}, limit=limit)


def compounders(con: duckdb.DuckDBPyConnection, *, limit: int = 20) -> list[dict]:
    """High-ROCE, low-debt, **steadily-growing** businesses with clean books. Returns
    ``[{symbol, name, sector, roce, de, cagr, forensic, score}, …]`` best-first."""
    names, sectors = _names(con), dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())
    rows = []
    for sym in _universe(con):
        try:
            af = _annual(con, sym)
        except Exception:  # noqa: BLE001
            continue
        if af.empty:
            continue
        roce, de = _roce_de(af)
        if roce is None or roce < 15:                              # genuinely high-return
            continue
        if de is not None and de > 0.75:                          # low debt (unknown de allowed through)
            continue
        rev = _col(af, "RevenueFromOperations").dropna()
        if len(rev) < 3:
            continue
        first, last = float(rev.iloc[0]), float(rev.iloc[-1])
        if first <= 0 or last <= 0:
            continue
        cagr = ((last / first) ** (1 / (len(rev) - 1)) - 1) * 100
        if cagr < 8:                                              # a compounder must actually compound
            continue
        steps = rev.pct_change().dropna()
        consistency = float((steps > 0).mean()) if len(steps) else 0.0   # share of up years
        fscore = screener._forensic_raw(con, sym)
        rows.append({"symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
                     "roce": round(roce, 1), "de": round(de, 2) if de is not None else None,
                     "cagr": round(cagr, 1), "forensic": round(fscore, 1) if fscore is not None else None,
                     "_roce": min(roce, 60.0), "_cagr": min(cagr, 40.0),
                     "_cons": consistency, "_for": fscore if fscore is not None else 2.0})
    return _finish(con, rows, ["_roce", "_cagr", "_cons", "_for"],
                   {"_roce": 0.35, "_cagr": 0.30, "_cons": 0.15, "_for": 0.20}, limit=limit)
