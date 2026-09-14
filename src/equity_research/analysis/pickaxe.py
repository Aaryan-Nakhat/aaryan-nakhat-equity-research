"""⛏️ Pickaxe — surging Indian consumer/industrial DEMAND → the indirect listed beneficiary.

The **demand-side mirror of 💨 Tailwind** (``analysis/tailwind.py``). Tailwind works the *supply*
side (a supplier restricts a material → who must keep buying). Pickaxe works the *demand* side, on
the gold-rush principle the user framed it with: **when demand for a product booms, don't buy the
miner digging for gold — buy the one selling the pickaxes.** An egg/poultry boom is cyclical and
margin-volatile for egg producers, but the listed maker of *poultry vaccines / feed additives* rides
the same wave with far less cyclicality — and is usually still off the mainstream radar. This finds
those moves autonomously and maps them to Indian listed names.

A four-tier agent pipeline, each tier one job, chained (identical skeleton to Tailwind):

  ① SCOUT   (``scrapers/trends.py`` + ``scrapers/social.py``) — Google Trends rising "buy" queries
                                                    + trending searches + demand-surge news/Reddit
  ② ANALYST (``synthesize.pickaxe_analyst``)        — genuine durable demand THEME vs fad; DEMANDS a source
  ③ MAPPER  (``synthesize.pickaxe_beneficiaries``)  — grounded search → direct + INDIRECT "pickaxe" names
  ④ AUDITOR (``auditor`` below)                      — verify vs equity_master, add smart-money +
                                                    cyclicality, kill hallucinations, rank pickaxes first

The verification spine is shared with ``analysis/supply_chain.py`` (``_verify`` / ``_implausible``):
no company reaches you unless it resolves to a real NSE symbol AND its business is plausible. Trends
is best-effort — when Google blocks it, the pipeline runs on the Google-Search-grounded LLM alone
(qualitative demand read). An idea *generator* — leads worth your own 10-minute check, never a call;
when there is no clean listed beneficiary it says so.
"""

from __future__ import annotations

import logging
import re

import duckdb

from equity_research.analysis import ownership, supply_chain, tailwind
from equity_research.reports import synthesize
from equity_research.scrapers import social, trends

log = logging.getLogger("equity-research.pickaxe")

# The consumer categories the Analyst reasons over (mirrors the Trends scout's catalog).
_CATEGORIES = list(trends._CATEGORIES.keys())

# Demand-surge probes for the news/Reddit legs of the Scout (breadth beyond Google Trends).
_NEWS_QUERIES = [
    "India consumer demand surge",
    "India sales jump demand rising",
    "India product price hike demand strong",
    "India fastest growing consumer category",
    "India premiumisation demand shift",
    "India import surge rising demand",
]
_REDDIT_QUERIES = [
    "India everyone buying",
    "India demand increasing price",
    "India trending product",
]

_INST_CATS = {"mutual fund", "insurance company", "FPI", "bank / FI"}
_TRENDS_EXPLORE = "https://trends.google.com/trends/explore?geo=IN"


# ── Tier ① — the Scout ──
def _trends_signals() -> list[dict]:
    """Google Trends leg (browser tier): rising "buy" queries per consumer category, shaped as Scout
    signals ``[{title, url, source}]``. Empty when Trends is blocked (LLM path carries it)."""
    out: list[dict] = []
    for row in trends.scout_buy_surges():
        change = f" ({row['change']})" if row.get("change") else ""
        out.append({
            "title": f"Rising 'buy' search in India: {row['phrase']}{change} "
                     f"[{row['category']}, last 3 months]",
            "url": _TRENDS_EXPLORE, "source": "Google Trends", "published": ""})
    return out


def _scout_signals(days: int) -> list[dict]:
    """Tier ① — merged demand-signal stream: Google Trends (best-effort) + Google News (+ best-effort
    Reddit) for demand-surge / price-hike chatter. Deduped by title."""
    signals = _trends_signals()
    have = {re.sub(r"[^a-z0-9]+", "", s["title"].lower())[:80] for s in signals}
    news = social.scout(_NEWS_QUERIES + _REDDIT_QUERIES, days=days, with_x=False)
    for s in news:
        k = re.sub(r"[^a-z0-9]+", "", s["title"].lower())[:80]
        if k and k not in have:
            have.add(k)
            signals.append(s)
    log.info("pickaxe scout: %d demand signals (%d from Trends)",
             len(signals), sum(1 for s in signals if s["source"] == "Google Trends"))
    return signals


# ── Tier ④ — the Auditor ──
_LAYER_RANK = {"indirect": 0, "direct": 1, "": 1}
_CYC_RANK = {"low": 0, "medium": 1, "": 1, "high": 2}
_SMART_RANK = {"accumulating": 0, "": 1, "mixed": 1, "distributing": 2}
_LAYER_BADGE = {"indirect": "⛏️ pickaxe", "direct": "🎯 direct"}
_CYC_BADGE = {"low": "🟢 low-cyclical", "medium": "🟡 mid-cyclical", "high": "🔁 high-cyclical"}


def _smart_money(con: duckdb.DuckDBPyConnection, symbol: str) -> str:
    """A one-word institutional read for ``symbol`` from its latest SHP QoQ diff: 'accumulating' /
    'distributing' / 'mixed' / '' (unknown). Reuses ``ownership.ownership_changes`` (the same signal
    ``sector_analysis.smart_money`` aggregates) — surfaces names smart money is quietly buying."""
    try:
        oc = ownership.ownership_changes(con, symbol)
    except Exception:  # noqa: BLE001 — smart-money is a nicety, never break the audit
        oc = None
    if not oc:
        return ""

    def _inst(rows):
        return sum(1 for r in rows if r.get("category") in _INST_CATS
                   or r.get("classification") == "LISTED company")

    adds = _inst(oc.get("entered", []) + oc.get("added", []))
    reduces = _inst(oc.get("exited", []) + oc.get("trimmed", []))
    if adds > reduces and adds:
        return "accumulating"
    if reduces > adds and reduces:
        return "distributing"
    return "mixed" if (adds or reduces) else ""


def auditor(con: duckdb.DuckDBPyConnection, candidates: list[dict], *,
            watch: set[str]) -> list[dict]:
    """Verify Mapper candidates vs the NSE master, kill hallucinations, attach market-cap size, a
    smart-money read and the direct/indirect + cyclicality tags, flag watchlist hits, and rank so the
    **non-obvious indirect 'pickaxe'** names surface first. Reuses the ``supply_chain`` spine
    (``_verify`` / ``_implausible``) and ``tailwind._size_word``. Sort: watchlist → indirect-before-
    direct → lower-cyclicality → smart-money-accumulating → smaller-cap → name. Returns
    ``[{symbol, name, role, why, layer, cyclicality, smart_money, revenue_share, market_share,
    industry, mcap_cr, on_watchlist, tier}]``."""
    seen: dict[str, dict] = {}
    for c in candidates:
        v = supply_chain._verify(con, c.get("name", ""), c.get("ticker") or "")
        if not v or v["symbol"] in seen:
            continue
        if v["symbol"] in supply_chain._BLOCKLIST or supply_chain._implausible(con, v["symbol"]):
            log.info("pickaxe: dropped implausible pick %s", v["symbol"])
            continue
        ind = con.execute("SELECT industry FROM sector_map WHERE symbol = ?",
                          [v["symbol"]]).fetchone()
        on_watch = v["symbol"] in watch
        size_word, mcap = tailwind._size_word(con, v["symbol"])
        layer = c.get("layer", "") if c.get("layer") in ("direct", "indirect") else ""
        cyc = c.get("cyclicality", "") if c.get("cyclicality") in ("low", "medium", "high") else ""
        smart = _smart_money(con, v["symbol"])

        tier = "⭐ watchlist" if on_watch else "🟡 AI-verify"
        if layer:
            tier += f" · {_LAYER_BADGE[layer]}"
        if cyc:
            tier += f" · {_CYC_BADGE[cyc]}"
        if smart == "accumulating":
            tier += " · 📈 accumulating"
        elif smart == "distributing":
            tier += " · 📉 distributing"
        if size_word:
            tier += f" · {size_word}"

        seen[v["symbol"]] = {
            "symbol": v["symbol"], "name": v["name"],
            "role": c.get("role", ""), "why": c.get("why", ""),
            "layer": layer, "cyclicality": cyc, "smart_money": smart,
            "revenue_share": c.get("revenue_share", ""), "market_share": c.get("market_share", ""),
            "industry": ind[0] if ind else None, "mcap_cr": mcap if mcap != float("inf") else None,
            "on_watchlist": on_watch, "tier": tier, "_sort_mcap": mcap,
        }
    ranked = sorted(seen.values(), key=lambda d: (
        not d["on_watchlist"], _LAYER_RANK.get(d["layer"], 1), _CYC_RANK.get(d["cyclicality"], 1),
        _SMART_RANK.get(d["smart_money"], 1), d["_sort_mcap"], d["name"]))
    for d in ranked:
        d.pop("_sort_mcap", None)
    return ranked


# ── deep per-name enrichment (heavy: ingests financials + reads filings; used off the live path) ──
def _fnum(v):
    """float-or-None, treating NaN as None (numpy/pandas floats)."""
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def enrich_pick(con: duckdb.DuckDBPyConnection, pick: dict, theme: str) -> None:
    """Attach exact quant + a filing-grounded forward projection to ONE pick, in place. Best-effort —
    every sub-step degrades to blanks, never raises. Heavy (on-demand financials ingest + a grounded
    LLM read of the company's filings/concalls), so this runs off the live command path.

    Adds ``pick['quant']`` = {price, pe, pb, mcap_cr, sector_pe, sector, pe_vs_sector, support,
    resistance, trend} and ``pick['proj']`` = the ``synthesize.pickaxe_projection`` dict (revenue
    share now/next, growth, sources)."""
    from equity_research.reports import pipeline
    from equity_research.analysis import sector as sector_mod
    from equity_research.analysis import technical, valuation

    sym = pick["symbol"]
    try:
        pipeline.ensure_ingested(sym, con)                     # cooldown-guarded; safe to call
    except Exception:  # noqa: BLE001
        pass
    try:
        consolidated = pipeline._prefer_consolidated(con, sym)
    except Exception:  # noqa: BLE001
        consolidated = False

    q: dict = {}
    try:
        snap = valuation.snapshot(con, sym, consolidated)
        q["price"] = _fnum(snap.get("price"))
        q["pe"] = _fnum(snap.get("pe_ttm"))
        q["pb"] = _fnum(snap.get("pb"))
        q["mcap_cr"] = _fnum(snap.get("market_cap_cr"))
    except Exception:  # noqa: BLE001
        pass
    try:
        sv = sector_mod.sector_valuation(con, sym, consolidated)
        q["sector"] = sv.get("industry")
        q["sector_pe"] = _fnum(sv.get("sector_median_pe"))
        cheaper = _fnum(sv.get("pe_cheaper_than_%_of_peers"))
        if q.get("pe") and q.get("sector_pe"):
            q["pe_vs_sector"] = ("cheaper" if q["pe"] < q["sector_pe"] else "pricier")
        if cheaper is not None:
            q["pe_cheaper_than_pct"] = round(cheaper)
    except Exception:  # noqa: BLE001
        pass
    try:
        lv = technical.levels(con, sym)
        if lv.get("history_ok"):
            sup = lv.get("supports") or []
            res = lv.get("resistances") or []
            q["support"] = _fnum(sup[0]["mid"]) if sup else None
            q["resistance"] = _fnum(res[0]["mid"]) if res else None
            st = lv.get("structure")
            q["trend"] = st.get("trend") if isinstance(st, dict) else (st or None)
            if q.get("price") is None:
                q["price"] = _fnum(lv.get("close"))            # EOD close as a price fallback
    except Exception:  # noqa: BLE001
        pass
    pick["quant"] = q

    try:
        pick["proj"] = synthesize.pickaxe_projection(pick["name"], sym, theme, pick.get("role", ""))
    except Exception:  # noqa: BLE001
        pick["proj"] = {}


def enrich_report(con: duckdb.DuckDBPyConnection, themes: list[dict]) -> None:
    """Deep-enrich EVERY beneficiary across all themes, in place (see ``enrich_pick``). Heavy — one
    on-demand ingest + one grounded filing-read per name — so it's driven from the background worker
    / weekly push, never the live 7-min command path."""
    n = sum(len(t.get("beneficiaries") or []) for t in themes)
    log.info("pickaxe: deep-enriching %d names across %d themes (heavy)…", n, len(themes))
    done = 0
    for t in themes:
        for b in t.get("beneficiaries") or []:
            enrich_pick(con, b, t.get("theme", ""))
            done += 1
            if done % 5 == 0:
                log.info("pickaxe: enriched %d/%d names", done, n)
    log.info("pickaxe: enrichment complete (%d names)", n)


# ── orchestration ──
def theme_key(d: dict) -> str:
    """Stable dedup key for a theme — normalised theme text. Lets the weekly push avoid repeating a
    theme the mid-week/urgent pass (if added later) already surfaced."""
    return re.sub(r"[^a-z0-9]+", "", (d.get("theme") or "").lower())[:60]


def _map_and_audit(con: duckdb.DuckDBPyConnection, themes: list[dict],
                   watch: set[str]) -> list[dict]:
    """Tiers ③+④ — for each sourced theme, map to beneficiaries then audit them. Returns the themes
    (with ``beneficiaries`` + ``n_watch`` + ``n_indirect`` attached), sorted watchlist → durability →
    #indirect names → count."""
    dur_rank = {"structural": 0, "emerging": 1, "faddish": 2, "": 2}
    out = []
    for t in themes:
        if not t.get("source_url"):                            # unsourced → never surface
            continue
        cands = synthesize.pickaxe_beneficiaries(t)
        bens = auditor(con, cands, watch=watch)
        t["beneficiaries"] = bens
        t["n_watch"] = sum(1 for b in bens if b["on_watchlist"])
        t["n_indirect"] = sum(1 for b in bens if b["layer"] == "indirect")
        out.append(t)
    out.sort(key=lambda t: (-t["n_watch"], dur_rank.get(t.get("durability", ""), 2),
                            -t["n_indirect"], -len(t["beneficiaries"])))
    return out


def run_pickaxe(con: duckdb.DuckDBPyConnection, *, days: int = 21,
                max_themes: int = 6) -> dict:
    """Run the full 4-tier demand pipeline. Returns
    ``{themes: [{theme, category, driver, cotrends, india_supply, durability, headline, source_url,
    source_name, beneficiaries: [...]}], keys, n_signals, n_themes}``. Themes with a watchlist hit
    rank first, then by durability, then by #indirect names. Best-effort throughout — an empty
    ``themes`` list is a valid, honest result."""
    signals = _scout_signals(days)
    if not signals:
        log.info("pickaxe: no demand signals scouted")
        return {"themes": [], "keys": [], "n_signals": 0, "n_themes": 0}

    themes = synthesize.pickaxe_analyst(signals, categories=_CATEGORIES)
    if not themes:
        log.info("pickaxe: analyst found no durable demand theme in %d signals", len(signals))
        return {"themes": [], "keys": [], "n_signals": len(signals), "n_themes": 0}

    watch = {s for (s,) in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = _map_and_audit(con, themes, watch)[:max_themes]
    log.info("pickaxe: %d themes (from %d signals, %d analyst themes)",
             len(out), len(signals), len(themes))
    return {"themes": out, "keys": [theme_key(t) for t in out],
            "n_signals": len(signals), "n_themes": len(out)}
