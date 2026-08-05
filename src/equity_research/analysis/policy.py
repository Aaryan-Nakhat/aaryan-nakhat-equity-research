"""Government policy / scheme radar — a standalone screen (no effect on reports or the watchlist).

Pulls the most recent **PIB** press releases (primary, government-backed), keeps the ones that look
like economic schemes / policies / reforms, and has the LLM classify each into the **sector(s)** it
affects, the transmission mechanism, and likely **listed beneficiaries** — which we then resolve to
NSE symbols and flag against the user's watchlist. Catches policy at the announced / cabinet-approved
/ draft / consultation stage, i.e. often *before* a scheme's formal launch, but always from an
official source (never social media / news rumor — that would break the primary-only rule).

`analysis/policy.py` orchestrates; `scrapers/pib.py` scrapes; `synthesize.policy_impact` is the LLM
classifier. Pure read of `equity_master` / `sector_map` / `watchlist` for the company mapping.
"""

from __future__ import annotations

import logging

import duckdb

from equity_research.reports import synthesize
from equity_research.scrapers import nse_shp, pib

log = logging.getLogger("equity-research")

# Broad title gate — cheap first-pass to avoid fetching/LLM-ing obviously non-economic releases
# (awards, appointments, culture, condolences). The LLM does the real filtering on what survives.
_GATE = ("scheme", "pli", "production linked", "production-linked", "incentive", "policy",
         "mission", "subsidy", "cabinet", "approve", "allocat", "guideline", "draft",
         "reform", "framework", "mou", "viability gap", "capex", "invest", "crore", "outlay",
         "procure", "tender", "duty", "tariff", "manufactur", "corridor", "package", "launch",
         "boost", "sector", "industr", "energy", "infrastructure", "fund", "₹")


def _looks_economic(text: str) -> bool:
    """Broad body-level gate — a release worth sending to the LLM. Gates on the full text (not
    just the title), because many real reforms have a bland title but an economic body."""
    low = (text or "").lower()
    return any(k in low for k in _GATE)


def _name_index(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """norm_name → NSE symbol over the listed master + sector map (the beneficiary resolver)."""
    idx: dict[str, str] = {}
    for q in ("SELECT symbol, company_name FROM equity_master WHERE company_name IS NOT NULL",
              "SELECT symbol, company FROM sector_map WHERE company IS NOT NULL"):
        try:
            for sym, name in con.execute(q).fetchall():
                idx.setdefault(nse_shp.norm_name(name), sym)
        except Exception:  # noqa: BLE001
            continue
    return idx


def _resolve(names: list[str], idx: dict[str, str], watch: set[str]) -> list[dict]:
    """Map LLM beneficiary names → {name, symbol?, on_watchlist}. Keeps unresolved names too
    (still informative), dedup by symbol/name."""
    out, seen = [], set()
    for nm in names or []:
        nm = (nm or "").strip()
        if not nm:
            continue
        sym = idx.get(nse_shp.norm_name(nm))
        key = sym or nm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": nm, "symbol": sym, "on_watchlist": bool(sym and sym in watch)})
    # resolved-and-watchlist first, then resolved, then unresolved
    out.sort(key=lambda d: (not d["on_watchlist"], d["symbol"] is None, d["name"]))
    return out


def policy_scan(con: duckdb.DuckDBPyConnection, *, limit_releases: int = 25) -> list[dict]:
    """Scan the latest PIB releases → classified government schemes with sector + beneficiary
    mapping. Returns ``[{prid, scheme, ministry, stage, sectors, mechanism, rationale,
    confidence, beneficiaries:[{name, symbol, on_watchlist}], link}, …]`` — items with a
    watchlist hit first, then by number of resolved listed beneficiaries. Best-effort; [] on
    no schemes or any failure."""
    releases = pib.recent_releases(limit_releases)
    if not releases:
        return []
    link_by_prid = {r["prid"]: r.get("link") for r in releases}
    title_by_prid = {r["prid"]: r.get("title") for r in releases}
    # fetch each body, then gate on the FULL text (a bland title can hide a real reform)
    enriched = []
    for r in releases:
        body = pib.release_text(r["prid"])
        if not body or not body.get("body"):
            continue
        if _looks_economic(r.get("title", "") + " " + body["body"]):
            enriched.append({"prid": r["prid"], "title": r.get("title"), "body": body["body"]})
    if not enriched:
        return []
    schemes = synthesize.policy_impact(enriched)

    idx = _name_index(con)
    watch = {s for (s,) in con.execute("SELECT symbol FROM watchlist").fetchall()}
    out = []
    for s in schemes:
        prid = str(s.get("prid") or "").strip()
        bens = _resolve(s.get("beneficiaries") or [], idx, watch)
        out.append({
            "prid": prid,
            "scheme": s.get("scheme") or title_by_prid.get(prid) or "(scheme)",
            "ministry": s.get("ministry"),
            "stage": s.get("stage"),
            "sectors": s.get("sectors") or [],
            "mechanism": s.get("mechanism"),
            "rationale": s.get("rationale") or "",
            "confidence": s.get("confidence"),
            "beneficiaries": bens,
            "n_listed": sum(1 for b in bens if b["symbol"]),
            "n_watch": sum(1 for b in bens if b["on_watchlist"]),
            "link": link_by_prid.get(prid) or f"https://pib.gov.in/PressReleaseIframePage.aspx?PRID={prid}",
        })
    out.sort(key=lambda d: (-d["n_watch"], -d["n_listed"]))
    return out
