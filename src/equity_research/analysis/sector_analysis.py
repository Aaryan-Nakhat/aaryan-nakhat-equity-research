"""Sectoral analysis — a top-down read on a whole NSE sector index + the names inside it.

Answers "is this sector one to **enter / add to** right now, and which stocks in it are the
strongest and the most **undervalued**?" — the workbench's missing top-down lens.

Everything here is a **deterministic DB read** (the LLM verdict lives in
``reports/synthesize.sector_thesis``); every external pull is best-effort and degrades to
empty rather than raising. Three data foundations, all already refreshed daily:

* **Sector index technicals + valuation** — from ``index_close`` (full OHLC + PE/PB/div-yield
  per index per day). Reuses ``technical.indicators_from_prices`` on the index close series.
* **Within-sector ranking** — ``screener.fundamental_screen(symbols=…)`` over the index's
  constituents (quality + forensic + cheapness), for the top names and the undervalued ones.
* **Smart-money proxy** — aggregated institutional ownership Δ (``ownership``), marquee-investor
  moves (``investors``), mutual-fund exposure (``mf_holdings``) and bulk/block deals
  (``nse_api``) across the constituents. NSE publishes **no** FII/DII cash by sector, so this
  is an honest aggregate-of-constituents proxy (market-wide FII/DII is shown only as backdrop).
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import investors, ownership, screener, technical
from equity_research.scrapers import nse_archives

log = logging.getLogger("equity_research.sector_analysis")

# Institutional holder categories (from shp_holders.category) that count as "smart money".
_INST_CATS = {"mutual fund", "insurance company", "FPI", "bank / FI"}


# ── sector catalog ────────────────────────────────────────────────────────────
# canonical -> index_name (as in index_close), constituent CSV slug, valuation lens, emoji,
# and query aliases. The one curated bit — small and reliable.
_CATALOG: dict[str, dict] = {
    "defence":   {"index": "Nifty India Defence", "slug": "niftyindiadefence", "lens": "earnings",
                  "emoji": "🛡️", "aliases": ["defense", "defence", "military", "arms"]},
    "pharma":    {"index": "Nifty Pharma", "slug": "niftypharma", "lens": "earnings",
                  "emoji": "💊", "aliases": ["pharmaceutical", "pharmaceuticals", "drugs"]},
    "healthcare": {"index": "Nifty Healthcare Index", "slug": "niftyhealthcare", "lens": "earnings",
                   "emoji": "🏥", "aliases": ["hospital", "hospitals", "health"]},
    "auto":      {"index": "Nifty Auto", "slug": "niftyauto", "lens": "cyclical",
                  "emoji": "🚗", "aliases": ["automobile", "automobiles", "autos", "vehicle"]},
    "it":        {"index": "Nifty IT", "slug": "niftyit", "lens": "earnings",
                  "emoji": "💻", "aliases": ["tech", "technology", "software", "information technology"]},
    "bank":      {"index": "Nifty Bank", "slug": "niftybank", "lens": "financial",
                  "emoji": "🏦", "aliases": ["banks", "banking", "banknifty", "bank nifty"]},
    "psu-bank":  {"index": "Nifty PSU Bank", "slug": "niftypsubank", "lens": "financial",
                  "emoji": "🏛️", "aliases": ["psu bank", "psubank", "public sector bank"]},
    "private-bank": {"index": "Nifty Private Bank", "slug": "nifty_privatebank", "lens": "financial",
                     "emoji": "🏦", "aliases": ["private bank", "pvt bank"]},
    "financial": {"index": "Nifty Financial Services", "slug": "niftyfinance",
                  "lens": "financial", "emoji": "💹",
                  "aliases": ["finance", "financial services", "financials", "bfsi"]},
    "nbfc":      {"index": "Nifty NBFC", "slug": "niftynbfc", "lens": "financial",
                  "emoji": "💳", "aliases": ["nbfcs", "non banking finance"]},
    "insurance": {"index": "Nifty Insurance", "slug": "niftyinsurance", "lens": "financial",
                  "emoji": "🛟", "aliases": ["insurers", "insurer"]},
    "fmcg":      {"index": "Nifty FMCG", "slug": "niftyfmcg", "lens": "earnings",
                  "emoji": "🛒", "aliases": ["consumer staples", "staples", "consumer goods"]},
    "metal":     {"index": "Nifty Metal", "slug": "niftymetal", "lens": "cyclical",
                  "emoji": "⚙️", "aliases": ["metals", "mining", "steel"]},
    "realty":    {"index": "Nifty Realty", "slug": "niftyrealty", "lens": "cyclical",
                  "emoji": "🏠", "aliases": ["real estate", "reality", "property", "realestate"]},
    "energy":    {"index": "Nifty Energy", "slug": "niftyenergy", "lens": "cyclical",
                  "emoji": "⚡", "aliases": ["power", "energies"]},
    "oil-gas":   {"index": "Nifty Oil & Gas", "slug": "niftyoilgas", "lens": "cyclical",
                  "emoji": "🛢️", "aliases": ["oil", "gas", "oil and gas", "oil & gas", "petroleum"]},
    "consumer-durables": {"index": "Nifty Consumer Durables", "slug": "niftyconsumerdurables",
                          "lens": "earnings", "emoji": "📺",
                          "aliases": ["durables", "consumer durable", "white goods"]},
    "media":     {"index": "Nifty Media", "slug": "niftymedia", "lens": "earnings",
                  "emoji": "🎬", "aliases": ["entertainment", "broadcasting"]},
    "chemicals": {"index": "Nifty Chemicals", "slug": "niftychemicals", "lens": "cyclical",
                  "emoji": "🧪", "aliases": ["chemical", "specialty chemicals"]},
    "infra":     {"index": "Nifty Infrastructure", "slug": "niftyinfra", "lens": "cyclical",
                  "emoji": "🏗️", "aliases": ["infrastructure", "infra & logistics"]},
    "capital-goods": {"index": "Nifty Capital Goods", "slug": "niftycapitalgoods", "lens": "cyclical",
                      "emoji": "🔧", "aliases": ["capital goods", "capex", "engineering"]},
}


def catalog() -> dict[str, dict]:
    return _CATALOG


def resolve_sector(query: str) -> str | None:
    """Fuzzy-match a free-text sector name to a canonical key. None if no match."""
    q = " ".join((query or "").lower().replace("-", " ").split())
    if not q:
        return None
    for canon, meta in _CATALOG.items():
        if q == canon or q == canon.replace("-", " "):
            return canon
        if q in [a.lower() for a in meta["aliases"]]:
            return canon
    # substring fallback: query contained in a name/alias or vice-versa
    for canon, meta in _CATALOG.items():
        hay = [canon.replace("-", " "), meta["index"].lower()] + [a.lower() for a in meta["aliases"]]
        if any(q in h or h in q for h in hay):
            return canon
    return None


# ── sector index technicals ─────────────────────────────────────────────────────
def _index_prices(con: duckdb.DuckDBPyConnection, index_name: str) -> pd.DataFrame:
    df = con.execute(
        "SELECT trade_date, open, high, low, close FROM index_close "
        "WHERE index_name = ? AND close IS NOT NULL ORDER BY trade_date", [index_name]).df()
    return df.set_index("trade_date") if not df.empty else df


def _rs_vs_nifty(con: duckdb.DuckDBPyConnection, index_name: str, window: int = 63) -> float | None:
    """Sector-index return ÷ Nifty-50 return over ``window`` sessions (>1 = outperforming)."""
    idx = _index_prices(con, index_name)
    nif = _index_prices(con, "Nifty 50")
    if idx.empty or nif.empty:
        return None
    common = idx.index.intersection(nif.index)
    if len(common) <= window:
        return None
    a, b = idx["close"].reindex(common), nif["close"].reindex(common)
    ir, nr = a.iloc[-1] / a.iloc[-window] - 1, b.iloc[-1] / b.iloc[-window] - 1
    return None if nr == -1 else (1 + ir) / (1 + nr)


def index_technicals(con: duckdb.DuckDBPyConnection, index_name: str) -> dict:
    """Trend / momentum read on the sector index from ``index_close``. ``{}`` if too little
    history. Reuses ``technical.indicators_from_prices`` so the maths match the stock path."""
    p = _index_prices(con, index_name)
    if len(p) < 60:
        return {}
    ind = technical.indicators_from_prices(p)
    last = ind.iloc[-1]

    def g(k):
        v = last.get(k)
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    close, sma50, sma200 = g("close"), g("sma50"), g("sma200")
    hi52 = g("high_52w")
    rs = _rs_vs_nifty(con, index_name)
    sig: list[str] = []
    if close and sma50 and sma200:
        if close > sma50 > sma200:
            sig.append("uptrend (price > 50-DMA > 200-DMA)")
        elif close < sma50 < sma200:
            sig.append("downtrend (price < 50-DMA < 200-DMA)")
        else:
            sig.append("mixed / transitioning trend")
        if sma50 > sma200:
            sig.append("golden-cross regime (50-DMA above 200-DMA)")
        else:
            sig.append("death-cross regime (50-DMA below 200-DMA)")
    rsi = g("rsi14")
    if rsi is not None:
        sig.append(f"RSI {rsi:.0f} — " + ("overbought" if rsi >= 70 else "oversold" if rsi <= 30
                   else "strong" if rsi >= 55 else "weak" if rsi <= 45 else "neutral"))
    mh = g("macd_hist")
    if mh is not None:
        sig.append("MACD momentum positive" if mh > 0 else "MACD momentum negative")
    if rs is not None:
        sig.append(f"{'out' if rs > 1 else 'under'}performing Nifty 50 over ~3m "
                   f"({(rs - 1) * 100:+.1f}%)")
    pct_from_high = (close / hi52 - 1) * 100 if close and hi52 else None
    if pct_from_high is not None:
        sig.append(f"{abs(pct_from_high):.0f}% {'below' if pct_from_high < 0 else 'above'} 52-wk high")
    return {"close": close, "sma50": sma50, "sma200": sma200, "rsi14": rsi, "macd_hist": mh,
            "rs_vs_nifty": rs, "pct_from_52w_high": pct_from_high, "pct_change_1d": g("close") and
            float(ind["close"].pct_change().iloc[-1] * 100), "signals": sig,
            "history_days": len(p)}


# ── sector index valuation ──────────────────────────────────────────────────────
def index_valuation(con: duckdb.DuckDBPyConnection, index_name: str, lens: str) -> dict:
    """Sector index PE/PB/div-yield now, and its **percentile vs the index's own ~5-yr
    history** (is the sector cheap/expensive vs itself), plus vs Nifty 50. ``{}`` if no data."""
    df = con.execute(
        "SELECT trade_date, pe, pb, div_yield FROM index_close "
        "WHERE index_name = ? AND trade_date >= (CURRENT_DATE - INTERVAL 5 YEAR) "
        "ORDER BY trade_date", [index_name]).df()
    if df.empty:
        return {}
    metric = "pb" if lens == "financial" else "pe"
    series = pd.to_numeric(df[metric], errors="coerce").dropna()
    series = series[series > 0]
    if series.empty:
        return {}
    cur = float(series.iloc[-1])
    pctile = float((series < cur).mean() * 100)   # % of own history cheaper than now
    if pctile >= 80:
        reading = "expensive vs its own history"
    elif pctile >= 60:
        reading = "a bit rich vs its own history"
    elif pctile <= 20:
        reading = "cheap vs its own history"
    elif pctile <= 40:
        reading = "below its own average"
    else:
        reading = "around its own average"
    nifty = con.execute(
        "SELECT pe, pb FROM index_close WHERE index_name = 'Nifty 50' "
        "ORDER BY trade_date DESC LIMIT 1").fetchone()
    latest = df.iloc[-1]

    def num(v):
        try:
            f = float(v)
            return f if f == f else None
        except (TypeError, ValueError):
            return None
    return {"metric": metric.upper(), "current": cur, "own_history_pctile": pctile,
            "reading": reading, "pe": num(latest.get("pe")), "pb": num(latest.get("pb")),
            "div_yield": num(latest.get("div_yield")),
            "nifty_pe": num(nifty[0]) if nifty else None,
            "nifty_pb": num(nifty[1]) if nifty else None, "years": round(len(series) / 250, 1)}


# ── constituents ────────────────────────────────────────────────────────────────
_CONSTITUENT_CACHE: dict[str, list[dict]] = {}


def constituents(con: duckdb.DuckDBPyConnection, canonical: str) -> list[dict]:
    """The sector index's member stocks: ``[{symbol, company, isin, industry}]``. Fetched live
    from the NSE archive CSV (plain HTTP); falls back to ``sector_map`` macro-industry members
    if the slug 404s. Cached per process."""
    if canonical in _CONSTITUENT_CACHE:
        return _CONSTITUENT_CACHE[canonical]
    meta = _CATALOG.get(canonical, {})
    out: list[dict] = []
    try:
        df = nse_archives.fetch_constituents(meta["slug"])
        for _, r in df.iterrows():
            sym = str(r.get("Symbol", "")).strip()
            if sym:
                out.append({"symbol": sym, "company": str(r.get("Company Name", "")).strip(),
                            "isin": str(r.get("ISIN Code", "")).strip(),
                            "industry": str(r.get("Industry", "")).strip()})
    except Exception:  # noqa: BLE001 — fall back to the macro-industry members we already have
        log.warning("constituent CSV fetch failed for %s; using sector_map fallback", canonical)
    if not out:
        rows = con.execute(
            "SELECT symbol, company FROM sector_map WHERE lower(industry) LIKE ? "
            "OR lower(basic_industry) LIKE ?",
            [f"%{canonical.replace('-', ' ')}%"] * 2).fetchall()
        out = [{"symbol": s, "company": c or s, "isin": "", "industry": ""} for s, c in rows]
    _CONSTITUENT_CACHE[canonical] = out
    return out


# ── smart-money proxy ───────────────────────────────────────────────────────────
def smart_money(con: duckdb.DuckDBPyConnection, members: list[dict]) -> dict:
    """Aggregate institutional signal across the sector's constituents (a proxy — NSE has no
    FII/DII cash by sector). Returns net institutional add/reduce counts + notable detail."""
    syms = [m["symbol"] for m in members]
    adds, reduces = 0, 0
    add_detail, reduce_detail = [], []
    for sym in syms:
        try:
            oc = ownership.ownership_changes(con, sym)
        except Exception:  # noqa: BLE001
            oc = None
        if not oc:
            continue
        for r in oc["entered"] + oc["added"]:
            if r.get("category") in _INST_CATS or r.get("classification") == "LISTED company":
                adds += 1
                add_detail.append({"symbol": sym, "holder": r["name"],
                                   "pct": r.get("pct"), "delta": r.get("delta")})
        for r in oc["exited"] + oc["trimmed"]:
            if r.get("category") in _INST_CATS or r.get("classification") == "LISTED company":
                reduces += 1
                reduce_detail.append({"symbol": sym, "holder": r["name"],
                                      "prev_pct": r.get("prev_pct"), "delta": r.get("delta")})
    add_detail.sort(key=lambda x: -((x.get("delta") or x.get("pct")) or 0))
    reduce_detail.sort(key=lambda x: (x.get("delta") or 0))

    # marquee investors active in the sector
    marquee = []
    try:
        am = investors.all_moves(con)
        sset = set(syms)
        for inv, mv in am.items():
            for kind in ("entered", "added", "trimmed", "exited"):
                for r in mv[kind]:
                    if r["symbol"] in sset:
                        marquee.append({"investor": inv, "kind": kind, "symbol": r["symbol"],
                                        "pct": r.get("pct")})
    except Exception:  # noqa: BLE001
        pass

    # mutual-fund exposure across constituent ISINs (latest disclosure per scheme×isin)
    isins = [m["isin"] for m in members if m.get("isin")]
    mf = {}
    if isins:
        ph = ",".join("?" * len(isins))
        try:
            row = con.execute(
                f"SELECT COUNT(DISTINCT scheme_code), COALESCE(SUM(market_value_cr),0) FROM ("
                f"  SELECT scheme_code, isin, market_value_cr, "
                f"    row_number() OVER (PARTITION BY scheme_code, isin ORDER BY as_of DESC) rn "
                f"  FROM mf_holdings WHERE isin IN ({ph})) WHERE rn = 1", isins).fetchone()
            mf = {"schemes": int(row[0] or 0), "exposure_cr": float(row[1] or 0)}
        except Exception:  # noqa: BLE001
            mf = {}

    net = "accumulating" if adds > reduces * 1.3 else "distributing" if reduces > adds * 1.3 else "mixed"
    return {"adds": adds, "reduces": reduces, "net": net,
            "add_detail": add_detail[:6], "reduce_detail": reduce_detail[:6],
            "marquee": marquee[:8], "mf": mf}


# ── within-sector ranking ────────────────────────────────────────────────────────
def within_sector_ranking(con: duckdb.DuckDBPyConnection, members: list[dict],
                          top_n: int = 8) -> dict:
    """Rank the constituents on quality+forensic+cheapness (``screener.fundamental_screen``).
    Returns ``{top, undervalued, scored, total}`` — top by composite, undervalued by cheapness
    (cheap vs own history, quality-gated)."""
    syms = [m["symbol"] for m in members]
    name_of = {m["symbol"]: (m.get("company") or m["symbol"]) for m in members}
    rows = screener.fundamental_screen(con, symbols=syms, limit=len(syms))
    for r in rows:                        # prefer the constituent CSV's company name
        r["name"] = name_of.get(r["symbol"], r["name"])
    top = rows[:top_n]
    # cheapest-first among names that aren't a quality trap. "Undervalued" = the ones that clear
    # a genuine-cheapness bar (cheaper than ≥60% of their own history); if a re-rated sector has
    # none, the report falls back to `cheapest` with an honest "none outright cheap" label.
    quality_ok = [r for r in rows if r.get("cheapness") is not None
                  and (r.get("quality") is None or r["quality"] >= 3)]
    quality_ok.sort(key=lambda r: -(r.get("cheapness") or 0))
    uv = [r for r in quality_ok if (r.get("cheapness") or 0) >= 60]
    return {"top": top, "undervalued": uv[:top_n], "cheapest": quality_ok[:top_n],
            "genuinely_cheap": bool(uv), "scored": len(rows), "total": len(syms)}


# ── bundle ──────────────────────────────────────────────────────────────────────
def build_sector_analysis(con: duckdb.DuckDBPyConnection, canonical: str) -> dict | None:
    """Assemble the full sector read (index tech + valuation + smart-money + ranking + news).
    ``None`` if the sector is unknown or the index has no data."""
    meta = _CATALOG.get(canonical)
    if not meta:
        return None
    tech = index_technicals(con, meta["index"])
    val = index_valuation(con, meta["index"], meta["lens"])
    if not tech and not val:
        return None
    members = constituents(con, canonical)
    sm = smart_money(con, members) if members else {}
    ranking = within_sector_ranking(con, members) if members else {"top": [], "undervalued": [],
                                                                    "scored": 0, "total": 0}
    news = _sector_news(canonical, meta)
    fii_dii = _market_fii_dii()
    return {"canonical": canonical, "index_name": meta["index"], "emoji": meta["emoji"],
            "lens": meta["lens"], "technicals": tech, "valuation": val, "smart_money": sm,
            "ranking": ranking, "constituent_count": len(members), "news": news,
            "fii_dii_backdrop": fii_dii}


def _sector_news(canonical: str, meta: dict) -> list[dict]:
    """Market headlines filtered to this sector's keywords (best-effort, [] on failure)."""
    try:
        from equity_research.scrapers import markets_global
        heads = markets_global.market_headlines(30)
    except Exception:  # noqa: BLE001
        return []
    terms = [canonical.replace("-", " ")] + [a.lower() for a in meta["aliases"]] + \
            [meta["index"].lower().replace("nifty", "").strip()]
    terms = [t for t in {t.strip() for t in terms} if len(t) >= 3]
    return [h for h in heads if any(t in h["title"].lower() for t in terms)][:6]


def _market_fii_dii() -> dict:
    """Market-wide FII/DII cash (backdrop only — NOT sector-specific). {} on failure."""
    try:
        from equity_research.scrapers import nse_api
        data = nse_api.fii_dii_activity()
    except Exception:  # noqa: BLE001
        return {}
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
    out = {}
    for r in rows or []:
        cat = (r.get("category") or "").upper()
        try:
            net = float(str(r.get("netValue", "")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if "FII" in cat or "FPI" in cat:
            out["fii_net_cr"] = net
        elif "DII" in cat:
            out["dii_net_cr"] = net
    return out
