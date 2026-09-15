"""📈 Results Radar — just-reported companies ranked by growth + acceleration.

The companion to 🎙️ Concalls: where Concalls reads what management *said*, this surfaces who actually
*delivered*. It ranks the companies that **just reported** by the strength of the quarter — YoY revenue
& profit growth, whether that growth is **accelerating** vs the prior quarters, and margin inflection —
all computed from our own financials.

We hold **no analyst-consensus** data (primary-only), so this is **not** "beat vs street" — it ranks by
biggest / accelerating growth vs the company's *own* history. Bounded to names with financials history
(YoY needs the year-ago quarter). No LLM: a background pass refreshes the fresh quarter for names that
just filed, and the radar computes + ranks on-the-fly from the numbers.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import duckdb

from equity_research import ingest
from equity_research.analysis import alerts, fundamentals
from equity_research.scrapers import nse_api

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 45        # market-wide announcement sweep window
_FRESH_DAYS = 35           # a quarterly filing this recent = "just reported" (the radar window)
_STALE_DAYS = 100          # our stored latest quarter older than this ⇒ we're missing the new one


def _universe(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Symbols with financials history — YoY (and the radar) is only real for these."""
    return {r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials").fetchall()}


def _dt(a: dict) -> datetime:
    try:
        return datetime.strptime((a.get("an_dt") or "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def recent_filers(con: duckdb.DuckDBPyConnection, *, days: int = _LOOKBACK_DAYS,
                  universe: set[str] | None = None) -> list[dict]:
    """Symbols with a **"Results filed"** announcement in the last ``days`` (market-wide, one cheap
    NSE call), restricted to our financials universe. Newest first. Never raises."""
    uni = universe if universe is not None else _universe(con)
    f = (date.today() - timedelta(days=days)).strftime("%d-%m-%Y")
    t = date.today().strftime("%d-%m-%Y")
    try:
        raw = nse_api.corporate_announcements(from_date=f, to_date=t)
    except Exception:  # noqa: BLE001
        log.exception("results-radar: announcement sweep failed")
        return []
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out, seen = [], set()
    for a in rows:
        if not isinstance(a, dict):
            continue
        sym = (a.get("symbol") or "").strip().upper()
        if not sym or sym not in uni or sym in seen:
            continue
        _, _, is_result = alerts._categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                             str(a.get("hasXbrl", "")).lower() == "true")
        if not is_result:
            continue
        fdt = _dt(a).date()
        if fdt == date.min:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "filed_date": fdt})
    out.sort(key=lambda r: r["filed_date"], reverse=True)
    return out


def refresh_new(con: duckdb.DuckDBPyConnection, *, max_new: int = 6) -> int:
    """Land the fresh quarter for a bounded batch of names that just filed results but whose stored
    latest quarter is stale (so we're missing the new print). Returns how many were refreshed. Never
    raises — one bad symbol is skipped."""
    stored = dict(con.execute(
        "SELECT symbol, max(period_end) FROM financials WHERE period_type = 'Q' GROUP BY symbol"
    ).fetchall())
    n = 0
    for c in recent_filers(con):
        if n >= max_new:
            break
        sym = c["symbol"]
        mx = stored.get(sym)
        if mx is not None and (date.today() - mx).days <= _STALE_DAYS:
            continue                                   # already have a recent quarter — skip
        try:
            ingest.ingest_financials(sym, con, max_filings=2)
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("results-radar: refresh failed for %s", sym)
    return n


def _acceleration(m) -> tuple[str, float]:
    """Latest quarter's PAT-YoY vs the mean of the prior ~3 quarters' — is growth speeding up?
    Returns ``(tag, delta_pp)`` where tag ∈ {Accelerating, Steady, Decelerating}."""
    if "net_yoy_%" not in m:
        return ("Steady", 0.0)
    net = m["net_yoy_%"].dropna()
    if len(net) < 2:
        return ("Steady", 0.0)
    latest = float(net.iloc[-1])
    prior = net.iloc[-4:-1] if len(net) >= 4 else net.iloc[:-1]
    base = float(prior.mean()) if len(prior) else latest
    delta = latest - base
    tag = "Accelerating" if delta >= 10 else "Decelerating" if delta <= -10 else "Steady"
    return (tag, delta)


def _score(m) -> int:
    """0-100 results strength: profit-growth magnitude + acceleration + margin inflection. Transparent,
    clamped weights so no single leg dominates."""
    last = m.iloc[-1]
    net = last.get("net_yoy_%")
    net = float(net) if (net is not None and net == net) else 0.0
    _, accel = _acceleration(m)
    margin_ch = 0.0
    if len(m) >= 2:
        nm, nmp = last.get("net_margin_%"), m.iloc[-2].get("net_margin_%")
        if nm == nm and nmp == nmp:
            margin_ch = float(nm) - float(nmp)
    s = 50.0
    s += max(-25.0, min(30.0, 0.5 * net))              # magnitude of profit growth
    s += max(-10.0, min(15.0, 0.5 * accel))            # is it accelerating?
    s += max(-8.0, min(8.0, 2.0 * margin_ch))          # margin inflection
    return int(round(max(0.0, min(100.0, s))))


def radar(con: duckdb.DuckDBPyConnection, *, limit: int = 25, days: int = _FRESH_DAYS) -> list[dict]:
    """The ranked radar: names whose latest quarter was **filed within ``days``**, best-first by
    Results Score. Returns ``[{symbol, name, sector, quarter, rev_yoy, net_yoy, execution, accel,
    score, filed_date, watchlist}, …]``."""
    cutoff = date.today() - timedelta(days=days)
    filers = con.execute(
        """SELECT symbol, max(filing_date) AS fdate, max(period_end) AS pend
           FROM financials WHERE period_type = 'Q' AND filing_date IS NOT NULL
           GROUP BY symbol HAVING max(filing_date) >= ?""", [cutoff]).fetchall()
    if not filers:
        return []
    names = dict(con.execute("SELECT symbol, company_name FROM equity_master").fetchall())
    names.update(con.execute("SELECT symbol, company FROM sector_map").fetchall())
    sectors = dict(con.execute("SELECT symbol, industry FROM sector_map").fetchall())
    watch = {r[0] for r in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = []
    for sym, fdate, pend in filers:
        m = fundamentals.latest_quarters(con, sym)
        if m.empty:
            continue
        last = m.iloc[-1]
        rev, net = last.get("rev_yoy_%"), last.get("net_yoy_%")
        if (rev is None or rev != rev) and (net is None or net != net):
            continue                                   # no YoY history yet — nothing to rank on
        accel_tag, _ = _acceleration(m)
        out.append({
            "symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym) or "—",
            "quarter": str(pend)[:10],
            "rev_yoy": round(float(rev), 1) if (rev is not None and rev == rev) else None,
            "net_yoy": round(float(net), 1) if (net is not None and net == net) else None,
            "execution": fundamentals.execution_band_from_metrics(m) or "n/a",
            "accel": accel_tag, "score": _score(m),
            "filed_date": str(fdate)[:10], "watchlist": sym in watch,
        })
    out.sort(key=lambda r: (-r["score"], r["symbol"]))
    return out[:limit]
