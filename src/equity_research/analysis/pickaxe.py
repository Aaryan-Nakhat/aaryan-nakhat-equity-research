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
