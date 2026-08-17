"""Supply-chain / indirect-contributor mapping — the smaller LISTED suppliers & ancillaries
behind the marquee names (the "who does BEL / HAL actually buy from?" lens).

There is **no structured supplier dataset** for Indian equities, so this is a **hybrid**:

* a hand-curated seed (``_CURATED_SECTOR`` / ``_CURATED_COMPANY``) for the flagship cases —
  reliable where it matters most (defence, EV…); plus
* LLM suggestions (``synthesize.supply_chain_suppliers``, Google-Search-grounded) for breadth.

**Every** name — curated or AI — is then **verified against the NSE master** (``equity_master``):
anything that doesn't resolve to a real listed symbol is dropped. AI-sourced rows are flagged so
the reader knows to verify them. This is a discovery aid, not a confirmed supplier ledger.
"""

from __future__ import annotations

import logging
import re

import duckdb

from equity_research.reports import synthesize

log = logging.getLogger("equity_research.supply_chain")

_STOP = {"ltd", "limited", "india", "indian", "the", "co", "company", "corporation", "corp",
         "industries", "technologies", "systems", "enterprises", "and", "&"}


# ── curated seed (hand-verified listed ancillaries; symbols confirmed in equity_master) ──
# Sector-level = cross-sector suppliers that are NOT already in the sector's own index (so they're
# genuinely *indirect*). Company-level = specific vendors to one marquee name.
_CURATED_SECTOR: dict[str, list[dict]] = {
    "defence": [
        {"symbol": "BHARATFORG", "role": "forgings / artillery systems (Kalyani)"},
        {"symbol": "CENTUM", "role": "defence & space electronics sub-systems"},
        {"symbol": "AZAD", "role": "precision aerospace & defence components"},
        {"symbol": "WALCHANNAG", "role": "heavy engineering / defence gears & components"},
        {"symbol": "ELECON", "role": "gears & transmission for defence platforms"},
        {"symbol": "TIMETECHNO", "role": "composite / moulded components"},
        {"symbol": "SIKA", "role": "aerospace & defence engineered systems"},
        {"symbol": "UNIMECH", "role": "aero tooling & precision assemblies"},
    ],
    "auto": [
        {"symbol": "SANDHAR", "role": "auto locks / mirrors / structural parts"},
        {"symbol": "MINDACORP", "role": "wiring harnesses & auto electronics"},
        {"symbol": "GABRIEL", "role": "shock absorbers / ride-control"},
        {"symbol": "JAYBARMARU", "role": "sheet-metal auto components"},
    ],
}
_CURATED_COMPANY: dict[str, list[dict]] = {
    "BEL": [{"symbol": "CENTUM", "role": "electronic modules / sub-systems"},
            {"symbol": "ASTRAMICRO", "role": "RF & microwave super-components"}],
    "HAL": [{"symbol": "DYNAMATECH", "role": "airframe & hydraulic assemblies"},
            {"symbol": "AZAD", "role": "engine & precision components"},
            {"symbol": "MTARTECH", "role": "precision machined assemblies"}],
    "TATAMOTORS": [{"symbol": "SANDHAR", "role": "locks, mirrors, structural parts"},
                   {"symbol": "MINDACORP", "role": "wiring harnesses & electronics"}],
}


def _norm_tokens(name: str) -> set[str]:
    toks = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    return {t for t in toks if t not in _STOP and len(t) > 1}


def _verify(con: duckdb.DuckDBPyConnection, name: str, ticker: str = "") -> dict | None:
    """Resolve a supplier to a real NSE symbol via ``equity_master``. Trusts a valid ``ticker``
    first, else matches the name's distinctive tokens. Returns ``{symbol, name}`` or ``None``."""
    toks = _norm_tokens(name)
    if ticker:
        r = con.execute(
            "SELECT symbol, company_name FROM equity_master WHERE symbol = ?", [ticker]).fetchone()
        # Trust the ticker only if its real company name is CONSISTENT with the suggested name
        # (shares a distinctive token) — else the LLM guessed a ticker that belongs to a different
        # company (e.g. suggested a defence-comms firm but ticker 'PNC' = Pritish Nandy). Reject
        # the ticker and fall back to name matching, which finds the right co or nothing.
        if r and (not toks or (toks & _norm_tokens(r[1]))):
            return {"symbol": r[0], "name": r[1]}
    if not toks:
        return None
    # match rows sharing the most distinctive tokens; require the longest token to appear
    anchor = max(toks, key=len)
    rows = con.execute(
        "SELECT symbol, company_name FROM equity_master WHERE company_name ILIKE ?",
        [f"%{anchor}%"]).fetchall()
    best, best_overlap = None, 0
    for sym, comp in rows:
        overlap = len(toks & _norm_tokens(comp))
        if overlap > best_overlap:
            best, best_overlap = {"symbol": sym, "name": comp}, overlap
    return best


def _dedup_verified(con, raw: list[dict], *, exclude: set[str], source: str,
                    seen: dict[str, dict]) -> None:
    """Verify each raw {symbol?|name, ticker?, role, why} and fold into ``seen`` (symbol-keyed)."""
    for r in raw:
        v = _verify(con, r.get("name", ""), r.get("ticker") or r.get("symbol", ""))
        if not v or v["symbol"] in exclude or v["symbol"] in seen:
            continue
        seen[v["symbol"]] = {"symbol": v["symbol"], "name": v["name"],
                             "role": r.get("role", ""), "why": r.get("why", ""), "source": source}


def suppliers_for_company(con: duckdb.DuckDBPyConnection, symbol: str,
                          company_name: str) -> list[dict]:
    """Listed suppliers/ancillaries for ONE company: curated seed + LLM-suggested, all verified.
    Returns ``[{symbol, name, role, why, source}]`` (source 'curated' | 'ai'), curated first."""
    seen: dict[str, dict] = {}
    exclude = {symbol}
    _dedup_verified(con, [{"symbol": c["symbol"], "role": c["role"]}
                          for c in _CURATED_COMPANY.get(symbol, [])],
                    exclude=exclude, source="curated", seen=seen)
    ai = synthesize.supply_chain_suppliers(
        company_name or symbol,
        context=f"{company_name} ({symbol}) — an Indian listed company; list its smaller listed "
                f"suppliers / component-makers / subcontractors.")
    _dedup_verified(con, ai, exclude=exclude, source="ai", seen=seen)
    return list(seen.values())


def sector_supply_chain(con: duckdb.DuckDBPyConnection, canonical: str, index_name: str,
                        primes: list[str], constituent_syms: set[str]) -> list[dict]:
    """Indirect listed beneficiaries of a SECTOR: curated sector seed + LLM (grounded on the
    sector's marquee names). Excludes the sector's own index constituents (those are direct, not
    indirect). Returns ``[{symbol, name, role, why, source}]``, curated first."""
    seen: dict[str, dict] = {}
    exclude = set(constituent_syms)          # indirect = NOT already in the index
    _dedup_verified(con, _CURATED_SECTOR.get(canonical, []),
                    exclude=exclude, source="curated", seen=seen)
    prime_txt = ", ".join(primes[:6])
    ai = synthesize.supply_chain_suppliers(
        f"the {index_name} sector",
        context=f"Main listed players include: {prime_txt}. List the SMALLER listed suppliers / "
                f"component-makers / ancillaries that feed into these — the indirect beneficiaries, "
                f"not the marquee names themselves.")
    _dedup_verified(con, ai, exclude=exclude, source="ai", seen=seen)
    return list(seen.values())
