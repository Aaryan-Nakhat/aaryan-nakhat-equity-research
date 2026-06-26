"""MCX (Multi Commodity Exchange of India) — live commodity futures.

MCX's market-watch data sits behind Akamai AND its JSON endpoint is gated on the
``X-Requested-With: XMLHttpRequest`` header (an ASP.NET AJAX check), so a plain GET
404s. We fetch it **in-page via Camoufox** — warm the market-watch page, then run
the same ``fetch`` the page's own ``marketWatch.js`` makes — exactly the NSE pattern.

We return the **near-month future** (``InstrumentName == 'FUTCOM'``, soonest expiry)
per commodity; the feed also carries hundreds of option rows (``OPTFUT``) which we
skip. Best-effort: any failure yields ``{}`` so callers just omit the line.
"""

from __future__ import annotations

import json
from datetime import datetime

from scrapling.fetchers import StealthyFetcher

_PAGE = "https://www.mcxindia.com/market-data/market-watch"
# Run inside the page (same-origin) with the header the server requires.
_FETCH = """async () => {
    const r = await fetch(location.origin + location.pathname + '/GetMarketWatch?culture=en',
        {headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}});
    return await r.text();
}"""

# symbol -> display unit (MCX quotes: gold ₹/10g, silver ₹/kg, crude ₹/barrel)
_WANTED = {"GOLD": "/10g", "SILVER": "/kg", "CRUDEOIL": "/bbl"}


def _expiry(s: str | None):
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


def commodities() -> dict[str, dict]:
    """Near-month futures for gold / silver / crude.

    ``{SYMBOL: {"ltp": float, "pct": float|None, "unit": str, "expiry": str}}``;
    ``{}`` on any failure (never raises)."""
    captured: dict[str, str] = {}

    def _action(page):
        captured["body"] = page.evaluate(_FETCH)
        return page

    try:
        StealthyFetcher.fetch(_PAGE, headless=True, network_idle=True, page_action=_action)
        rows = json.loads(captured.get("body") or "")["data"]["Data"]
    except Exception:  # noqa: BLE001 — best-effort market context, never break the scan
        return {}

    out: dict[str, dict] = {}
    for sym, unit in _WANTED.items():
        futs = [r for r in rows
                if r.get("Symbol") == sym and r.get("InstrumentName") == "FUTCOM"
                and _expiry(r.get("ExpiryDate")) is not None and r.get("LTP") is not None]
        if not futs:
            continue
        f = min(futs, key=lambda r: _expiry(r["ExpiryDate"]))   # front-month future
        out[sym] = {"ltp": f.get("LTP"), "pct": f.get("PercentChange"),
                    "unit": unit, "expiry": f.get("ExpiryDate")}
    return out
