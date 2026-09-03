"""💨 Tailwind — global policy / supply-shock → Indian listed beneficiaries.

The thesis is **chokepoint arbitrage**: when a dominant supplier (usually China) restricts a
critical material — an export ban, quota, tariff, production cut — the downstream sectors that
*must* keep buying (defence, semiconductors, EV batteries, specialty chemicals) scramble for
alternate sources, and the listed players elsewhere who make that thing get a tailwind. This
module finds those moves **autonomously** and maps them to Indian listed names, so ideas surface
without you naming a material first.

A four-tier agent pipeline, each tier one job, chained:

  ① SCOUT   (``scrapers/social.py::scout``)      — cast the net: Google News + Reddit → raw signals
  ② ANALYST (``synthesize.tailwind_analyst``)     — real disruption vs noise; DEMANDS a source URL
  ③ MAPPER  (``synthesize.tailwind_beneficiaries``)— grounded search → candidate Indian beneficiaries
  ④ AUDITOR (``auditor`` below)                    — verify each vs equity_master, kill hallucinations,
                                                     flag watchlist hits, rank

The verification spine is shared with ``analysis/supply_chain.py`` (``_verify`` / ``_implausible``):
no company reaches you unless it resolves to a real NSE symbol AND its actual business is plausible.
Every disruption ships with its **source link**. This is an idea *generator* — leads worth your own
10-minute check — never a confirmed call; when there is no clean listed beneficiary it says so.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.analysis import supply_chain
from equity_research.reports import synthesize
from equity_research.scrapers import fedregister, social

log = logging.getLogger("equity-research.tailwind")

# ── the one bit of curation: the critical-material chokepoint catalog ──
# Anchors the Analyst (so it reasons from real chokepoints, not thin air) and drives the Scout's
# targeted queries. Small, reliable, grows over time — same philosophy as the sector _CATALOG.
# ``share`` = the dominant supplier's rough share of world supply (context, not a live figure).
_CHOKEPOINTS: list[dict] = [
    {"material": "tungsten", "supplier": "China", "share": "~80%",
     "sectors": ["defence", "cutting tools", "semiconductors"]},
    {"material": "gallium", "supplier": "China", "share": "~98%",
     "sectors": ["semiconductors", "defence radar", "LED / solar"]},
    {"material": "germanium", "supplier": "China", "share": "~60%",
     "sectors": ["semiconductors", "fibre optics", "night-vision / defence"]},
    {"material": "antimony", "supplier": "China", "share": "~50%",
     "sectors": ["defence / ammunition", "flame retardants", "batteries"]},
    {"material": "graphite anode", "supplier": "China", "share": "~90%",
     "sectors": ["EV batteries", "energy storage"]},
    {"material": "rare-earth magnets (NdFeB)", "supplier": "China", "share": "~90%",
     "sectors": ["EV / auto", "defence", "wind turbines", "electronics"]},
    {"material": "rare earths (NdPr / dysprosium)", "supplier": "China", "share": "~70%",
     "sectors": ["magnets", "defence", "EV motors"]},
    {"material": "lithium", "supplier": "China / Chile / Australia", "share": "China ~60% refining",
     "sectors": ["EV batteries", "energy storage"]},
    {"material": "cobalt", "supplier": "DRC / China", "share": "China ~70% refining",
     "sectors": ["batteries", "superalloys"]},
    {"material": "silicon metal", "supplier": "China", "share": "~70%",
     "sectors": ["semiconductors", "solar", "aluminium alloys"]},
    {"material": "fluorspar", "supplier": "China / Mexico", "share": "China ~60%",
     "sectors": ["specialty chemicals", "refrigerants", "steel"]},
    {"material": "molybdenum", "supplier": "China", "share": "~40%",
     "sectors": ["steel alloys", "defence", "energy"]},
    {"material": "titanium sponge", "supplier": "China / Japan", "share": "China ~50%",
     "sectors": ["aerospace", "defence"]},
    {"material": "yellow phosphorus", "supplier": "China", "share": "~80%",
     "sectors": ["specialty chemicals", "semiconductors", "agrochemicals"]},
    {"material": "vanadium", "supplier": "China / Russia", "share": "China ~60%",
     "sectors": ["steel alloys", "grid-scale batteries"]},
]

# Generic supply-shock probes so the Scout catches moves on materials NOT yet in the catalog.
_GENERIC_QUERIES = [
    "China export ban critical minerals",
    "China export controls rare earth this week",
    "US tariff critical minerals",
    "export restriction semiconductor materials",
    "India alternative supplier critical minerals",
    "mineral export quota shortage",
]


def _scout_queries() -> list[str]:
    """Targeted Scout queries: one per catalogued chokepoint + the generic supply-shock probes."""
    per_mat = [f"{c['material']} export ban OR curb OR quota OR tariff OR shortage"
               for c in _CHOKEPOINTS]
    return per_mat + _GENERIC_QUERIES


# ── Tier ④ — the Auditor ──
# Market-cap tiers (₹ cr) — the radar prefers non-obvious small/mid-caps over crowded heavyweights.
_SIZE_BANDS = [(5_000, "small-cap"), (25_000, "mid-cap"), (75_000, "large-cap")]
# Language that betrays an ASPIRATION rather than an existing producer (deprioritised, not dropped —
# an early entrant can still be a real theme play, but it shouldn't outrank an actual producer).
_ASPIRATIONAL = ("bid", "bidder", "plans to", "planning", "to set up", "will set up", "mou",
                 "foray", "intends", "intent", "proposed", "aspir", "eyeing", "to enter",
                 "announced plans", "in talks", "to build")


def _tier(source: str, on_watch: bool) -> str:
    if on_watch:
        return "⭐ watchlist"
    return "🟢 curated" if source == "curated" else "🟡 AI-verified"


def _size_word(con: duckdb.DuckDBPyConnection, symbol: str) -> tuple[str | None, float]:
    """(size word, market-cap ₹cr) for a symbol, best-effort via valuation.snapshot. ``(None, inf)``
    when unknown — unknowns sort last so a name we can't size doesn't crowd out a real small-cap."""
    try:
        from equity_research.analysis import valuation
        snap = valuation.snapshot(con, symbol)
        mcap = snap.get("market_cap_cr") if isinstance(snap, dict) else None
    except Exception:  # noqa: BLE001 — size is a nicety, never break the audit
        mcap = None
    if not mcap or mcap != mcap:                            # None / NaN
        return None, float("inf")
    for hi, word in _SIZE_BANDS:
        if mcap < hi:
            return word, mcap
    return "mega-cap", mcap


def _is_aspirational(c: dict) -> bool:
    text = f"{c.get('role', '')} {c.get('why', '')}".lower()
    return any(k in text for k in _ASPIRATIONAL)


def auditor(con: duckdb.DuckDBPyConnection, candidates: list[dict], *,
            watch: set[str]) -> list[dict]:
    """Verify Mapper candidates against the NSE master, kill hallucinations, flag watchlist hits, and
    rank toward non-obvious small/mid-caps. Reuses the ``supply_chain`` spine: ``_verify`` (real
    symbol + name-consistency) and ``_implausible`` (a media/FMCG/finance name can't be a material
    producer). Sort: watchlist → actual-producer-before-aspirant → smaller-cap-first → name. Returns
    ``[{symbol, name, role, why, industry, mcap_cr, on_watchlist, aspirational, tier}]``."""
    seen: dict[str, dict] = {}
    for c in candidates:
        v = supply_chain._verify(con, c.get("name", ""), c.get("ticker") or "")
        if not v or v["symbol"] in seen:
            continue
        if v["symbol"] in supply_chain._BLOCKLIST or supply_chain._implausible(con, v["symbol"]):
            log.info("tailwind: dropped implausible pick %s", v["symbol"])
            continue
        ind = con.execute("SELECT industry FROM sector_map WHERE symbol = ?",
                          [v["symbol"]]).fetchone()
        on_watch = v["symbol"] in watch
        size_word, mcap = _size_word(con, v["symbol"])
        aspirational = _is_aspirational(c)
        tier = _tier(c.get("source", "ai"), on_watch)
        if size_word and not on_watch:                     # surface the size on AI/curated rows
            tier += f" · {size_word}"
        if aspirational:
            tier += " · ⚠ intent-only"
        seen[v["symbol"]] = {
            "symbol": v["symbol"], "name": v["name"],
            "role": c.get("role", ""), "why": c.get("why", ""),
            "industry": ind[0] if ind else None, "mcap_cr": mcap if mcap != float("inf") else None,
            "on_watchlist": on_watch, "aspirational": aspirational, "tier": tier,
            "_sort_mcap": mcap,
        }
    ranked = sorted(seen.values(),
                    key=lambda d: (not d["on_watchlist"], d["aspirational"], d["_sort_mcap"], d["name"]))
    for d in ranked:                                        # drop the private sort key
        d.pop("_sort_mcap", None)
    return ranked


# ── orchestration ──
_SEV_RANK = {"high": 0, "medium": 1, "low": 2, "": 3}
_FRESH_STATUS = {"in-effect", "proposed"}                  # rumored is too soft for a mid-week alert


def catalyst_key(d: dict) -> str:
    """Stable dedup key for a catalyst — normalised material + action. Lets the mid-week urgent
    break-in skip a shock it (or the weekly push) has already surfaced."""
    return f"{(d.get('material') or '').lower().strip()}|{(d.get('action') or '').lower().strip()}"


def _scout_signals(days: int) -> list[dict]:
    """Tier ① — the merged signal stream: Google News (+ best-effort Reddit) for GLOBAL breadth
    plus the US Federal Register (official, US-only) for the US leg and its proposed rules."""
    def _norm(t: str) -> str:
        return "".join(ch for ch in (t or "").lower() if ch.isalnum())[:80]

    signals = social.scout(_scout_queries(), days=days)
    have = {_norm(s["title"]) for s in signals}
    for r in fedregister.recent_rules(days=max(days, 21)):  # official US anchor; wider window (rules are sparse)
        k = _norm(r["title"])
        if k and k not in have:
            have.add(k)
            signals.append(r)
    return signals


def _map_and_audit(con: duckdb.DuckDBPyConnection, disruptions: list[dict],
                   watch: set[str]) -> list[dict]:
    """Tiers ③+④ — for each sourced disruption, map to beneficiaries then audit them. Returns the
    catalysts (with ``beneficiaries`` + ``n_watch`` attached), sorted watchlist → severity → count."""
    out = []
    for d in disruptions:
        if not d.get("source_url"):                        # unsourced → never surface
            continue
        cands = synthesize.tailwind_beneficiaries(d)
        bens = auditor(con, cands, watch=watch)
        d["beneficiaries"] = bens
        d["n_watch"] = sum(1 for b in bens if b["on_watchlist"])
        out.append(d)
    out.sort(key=lambda d: (-d["n_watch"], _SEV_RANK.get(d.get("severity", ""), 3),
                            -len(d["beneficiaries"])))
    return out


def run_tailwind(con: duckdb.DuckDBPyConnection, *, days: int = 14,
                 max_catalysts: int = 8) -> dict:
    """Run the full 4-tier pipeline. Returns
    ``{catalysts: [{material, imposer, action, status, severity, sectors, headline, source_url,
    source_name, date, beneficiaries: [...]}], keys, n_signals, n_catalysts}``.
    Catalysts with a watchlist hit rank first, then by severity, then by #beneficiaries.
    Best-effort throughout — an empty ``catalysts`` list is a valid, honest result."""
    signals = _scout_signals(days)
    if not signals:
        log.info("tailwind: no signals scouted")
        return {"catalysts": [], "keys": [], "n_signals": 0, "n_catalysts": 0}

    disruptions = synthesize.tailwind_analyst(
        signals, chokepoints=[c["material"] for c in _CHOKEPOINTS])
    if not disruptions:
        log.info("tailwind: analyst found no genuine disruptions in %d signals", len(signals))
        return {"catalysts": [], "keys": [], "n_signals": len(signals), "n_catalysts": 0}

    watch = {s for (s,) in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = _map_and_audit(con, disruptions, watch)[:max_catalysts]
    log.info("tailwind: %d catalysts (from %d signals, %d disruptions)",
             len(out), len(signals), len(disruptions))
    return {"catalysts": out, "keys": [catalyst_key(c) for c in out],
            "n_signals": len(signals), "n_catalysts": len(out)}


def run_tailwind_urgent(con: duckdb.DuckDBPyConnection, *, seen_keys: set[str],
                        days: int = 7, max_catalysts: int = 2) -> dict:
    """Lighter mid-week pass for the daily digest's urgent break-in. Runs Scout + Analyst (cheap),
    keeps only **fresh, high-severity, in-effect/proposed** disruptions NOT already surfaced
    (``seen_keys``), then maps+audits just those. Returns the same shape as ``run_tailwind``, and
    only catalysts that have at least one verified beneficiary (an alert with no actionable name is
    noise). Empty is the common, correct result on a quiet day."""
    signals = _scout_signals(days)
    if not signals:
        return {"catalysts": [], "keys": [], "n_signals": 0, "n_catalysts": 0}
    disruptions = synthesize.tailwind_analyst(
        signals, chokepoints=[c["material"] for c in _CHOKEPOINTS])
    fresh = [d for d in disruptions
             if d.get("severity") == "high" and d.get("status") in _FRESH_STATUS
             and d.get("source_url") and catalyst_key(d) not in seen_keys]
    if not fresh:
        log.info("tailwind-urgent: nothing fresh & high-severity (of %d disruptions)", len(disruptions))
        return {"catalysts": [], "keys": [], "n_signals": len(signals), "n_catalysts": 0}

    watch = {s for (s,) in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = [c for c in _map_and_audit(con, fresh[:max_catalysts], watch) if c["beneficiaries"]]
    log.info("tailwind-urgent: %d actionable fresh catalyst(s)", len(out))
    return {"catalysts": out, "keys": [catalyst_key(c) for c in out],
            "n_signals": len(signals), "n_catalysts": len(out)}
