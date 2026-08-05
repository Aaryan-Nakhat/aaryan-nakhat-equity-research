"""Government policy / scheme radar — a standalone screen (no effect on reports or the watchlist).

Pulls the latest **PIB** press releases (primary, government-backed), keeps the ones that look like
economic schemes / policies / reforms, and has the LLM classify each into the **sector(s)** it
affects, the transmission mechanism, and likely **listed beneficiaries** (with a per-company reason)
— which we then resolve to NSE symbols + readable company names and flag against the watchlist.
Catches policy at the announced / cabinet-approved / draft / consultation stage, i.e. often *before*
a scheme's formal launch, but always from an official source (never social media / news rumor — that
would break the primary-only rule).

Coverage note: PIB's public listing exposes only the *latest* ~100 releases (its date filter is a
server-side control that doesn't page reliably), so this is a "recent releases" screen, not a fixed
30-day archive. `scrapers/pib.py` scrapes; `synthesize.policy_impact` is the LLM classifier.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.reports import synthesize
from equity_research.scrapers import nse_shp, pib

log = logging.getLogger("equity-research")

# Broad title gate — cheap first pass over the listing (which carries title + ministry) so we only
# fetch bodies for plausibly-economic releases. The LLM does the real filtering on what survives.
_GATE = ("scheme", "pli", "production linked", "production-linked", "incentive", "policy",
         "mission", "subsidy", "cabinet", "approve", "approv", "allocat", "guideline", "draft",
         "reform", "framework", "mou", "viability gap", "capex", "outlay", "procure", "tender",
         "duty", "tariff", "manufactur", "corridor", "package", "launch", "boost", "invest",
         "crore", "₹", "incentivis", "fund for", "capital", "hydrogen", "semiconductor",
         "renewable", "infrastructure", "ethanol", "defence production")
_MAX_CLASSIFY = 55          # cap releases sent to the LLM in one call (token / latency guard)


def _looks_economic(text: str) -> bool:
    """Broad gate — a release worth sending to the LLM (matched on title, which the listing gives)."""
    low = (text or "").lower()
    return any(k in low for k in _GATE)


def _name_maps(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, str], dict[str, str]]:
    """(norm_name → symbol, symbol → readable company name) over the listed master + sector map."""
    by_norm: dict[str, str] = {}
    by_sym: dict[str, str] = {}
    for q in ("SELECT symbol, company_name FROM equity_master WHERE company_name IS NOT NULL",
              "SELECT symbol, company FROM sector_map WHERE company IS NOT NULL"):
        try:
            for sym, name in con.execute(q).fetchall():
                by_norm.setdefault(nse_shp.norm_name(name), sym)
                by_sym.setdefault(sym, name)
        except Exception:  # noqa: BLE001
            continue
    return by_norm, by_sym


def _resolve(beneficiaries: list, by_norm: dict, by_sym: dict, watch: set[str]) -> list[dict]:
    """Map LLM beneficiaries (``[{company, why}]`` or bare strings) → ``{name, symbol, why,
    on_watchlist}``. Prefers our canonical company name when the symbol resolves; keeps unresolved
    names too. Dedup by symbol/name; watchlist-then-resolved first."""
    out, seen = [], set()
    for b in beneficiaries or []:
        if isinstance(b, dict):
            company, why = (b.get("company") or "").strip(), (b.get("why") or "").strip()
        else:
            company, why = str(b or "").strip(), ""
        if not company:
            continue
        sym = by_norm.get(nse_shp.norm_name(company))
        name = by_sym.get(sym) if sym else company     # prefer our canonical name when resolved
        key = sym or company.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name or company, "symbol": sym, "why": why,
                    "on_watchlist": bool(sym and sym in watch)})
    out.sort(key=lambda d: (not d["on_watchlist"], d["symbol"] is None, d["name"]))
    return out


def policy_scan(con: duckdb.DuckDBPyConnection, *, limit_releases: int = 120) -> list[dict]:
    """Scan the latest PIB releases → classified government schemes with sector + beneficiary
    mapping. Returns ``[{prid, scheme, ministry, stage, sectors, mechanism, what_it_is, benefit,
    confidence, beneficiaries:[{name, symbol, why, on_watchlist}], n_listed, n_watch, link}, …]``
    — items with a watchlist hit first, then by number of resolved listed beneficiaries.
    Best-effort; [] on nothing or any failure."""
    releases = pib.latest_releases(limit_releases)
    if not releases:
        return []
    link_by_prid = {r["prid"]: f"https://pib.gov.in/PressReleaseIframePage.aspx?PRID={r['prid']}"
                    for r in releases}
    title_by_prid = {r["prid"]: r.get("title") for r in releases}
    gated = [r for r in releases if _looks_economic(r.get("title", ""))][:_MAX_CLASSIFY]
    enriched = []
    for r in gated:
        body = pib.release_text(r["prid"])
        if body and body.get("body"):
            enriched.append({"prid": r["prid"], "title": r.get("title"), "body": body["body"]})
    if not enriched:
        return []
    schemes = synthesize.policy_impact(enriched)

    by_norm, by_sym = _name_maps(con)
    watch = {s for (s,) in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = []
    for s in schemes:
        prid = str(s.get("prid") or "").strip()
        bens = _resolve(s.get("beneficiaries") or [], by_norm, by_sym, watch)
        out.append({
            "prid": prid,
            "scheme": s.get("scheme") or title_by_prid.get(prid) or "(scheme)",
            "ministry": s.get("ministry"),
            "stage": s.get("stage"),
            "sectors": s.get("sectors") or [],
            "mechanism": s.get("mechanism"),
            "what_it_is": s.get("what_it_is") or s.get("rationale") or "",
            "benefit": s.get("benefit") or "",
            "confidence": s.get("confidence"),
            "beneficiaries": bens,
            "n_listed": sum(1 for b in bens if b["symbol"]),
            "n_watch": sum(1 for b in bens if b["on_watchlist"]),
            "link": link_by_prid.get(prid, f"https://pib.gov.in/PressReleaseIframePage.aspx?PRID={prid}"),
        })
    out.sort(key=lambda d: (-d["n_watch"], -d["n_listed"]))
    return out
