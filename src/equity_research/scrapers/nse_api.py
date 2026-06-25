"""NSE ``/api/`` scraper (browser tier).

``www.nseindia.com/api/*`` sits behind Akamai Bot Manager: plain HTTP gets 403
even with primed cookies. The working pattern (validated in ``docs/SCRAPING.md``)
is to load a real page in Camoufox — which solves the JS challenge — then run
``fetch()`` **inside the page** (a same-origin XHR carrying the validated
``_abck`` cookie) via scrapling's ``page_action`` hook.

Heavy (launches a browser), so reserve this for ``/api/`` endpoints that have no
plain-HTTP archive-file equivalent. Note ``/api/quote-equity`` is currently
WAF-blocked even here; use BSE for per-scrip quotes instead.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from scrapling.fetchers import StealthyFetcher

from equity_research.common.http import ScrapeError

_HOME = "https://www.nseindia.com/"


def q(symbol: str) -> str:
    """URL-encode an NSE symbol for an ``/api`` query param. NSE symbols can contain
    ``&`` (M&M, M&MFIN, J&KBANK, ARE&M); a bare ``&`` would split the query string and
    silently look up the wrong (truncated) symbol → empty result."""
    return quote(str(symbol), safe="")

# Run inside the page: retry the XHR because Akamai validates _abck asynchronously.
_IN_PAGE_FETCH = """async ({path, retries, delay}) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let last = {status: 0, body: ''};
    for (let i = 0; i < retries; i++) {
        const r = await fetch(path, {headers: {'Accept': 'application/json'}});
        last = {status: r.status, body: await r.text(), attempt: i + 1};
        if (r.status === 200) break;
        await sleep(delay);
    }
    return last;
}"""


def fetch_api(
    path: str,
    *,
    warm_url: str = _HOME,
    retries: int = 4,
    retry_delay_ms: int = 1500,
    headless: bool = True,
) -> Any:
    """Fetch an NSE ``/api/`` endpoint via Camoufox in-page XHR.

    ``path`` is the API path (e.g. ``"/api/marketStatus"``). ``warm_url`` is the
    page loaded first to solve the bot challenge — some endpoints need a matching
    page (e.g. a get-quotes page) rather than the homepage. Raises ``ScrapeError``
    on non-200.
    """
    if not path.startswith("/"):
        path = "/" + path
    captured: dict[str, Any] = {}

    def _action(page):
        captured.update(
            page.evaluate(
                _IN_PAGE_FETCH,
                {"path": path, "retries": retries, "delay": retry_delay_ms},
            )
        )
        return page

    StealthyFetcher.fetch(warm_url, headless=headless, network_idle=True, page_action=_action)

    if captured.get("status") != 200:
        raise ScrapeError(warm_url.rstrip("/") + path, captured.get("status"))
    return json.loads(captured.get("body") or "{}")


# --- Named wrappers for the endpoints validated as working (see SCRAPING.md) ---
# (equity-stockIndices and option-chain-indices currently 404 — paths moved.)

def fii_dii_activity() -> Any:
    """FII/DII cash-market buy/sell activity (latest published day)."""
    return fetch_api("/api/fiidiiTradeReact")


def _parse_deals(data: Any, key: str, deal_type: str) -> list[dict]:
    """Parse one deal array (BULK/BLOCK) from the large-deal snapshot."""
    out: list[dict] = []
    rows = data.get(key) if isinstance(data, dict) else None
    for r in rows or []:
        def num(field: str) -> float | None:
            try:
                return float(str(r.get(field, "")).replace(",", "").strip())
            except (TypeError, ValueError):
                return None
        out.append({
            "symbol": (r.get("symbol") or "").strip(),
            "company": (r.get("name") or "").strip(),
            "deal_type": deal_type,
            "buy_sell": (r.get("buySell") or "").strip().upper(),
            "client": (r.get("clientName") or "").strip(),
            "qty": num("qty"),
            "price": num("watp"),
        })
    return out


def large_deals() -> dict[str, list[dict]]:
    """Today's **bulk** and **block** deals, market-wide (one snapshot call).

    The NSE large-deal snapshot names the counterparty (``clientName``) — FIIs,
    mutual funds, insurers, HNIs — with buy/sell, quantity and VWAP per stock.
    Returns ``{'bulk': [...], 'block': [...]}``; callers filter to their symbols.
    Degrades to empty lists if unavailable.
    """
    try:
        data = fetch_api("/api/snapshot-capital-market-largedeal")
    except Exception:  # noqa: BLE001 — never break the scan
        return {"bulk": [], "block": []}
    return {"bulk": _parse_deals(data, "BULK_DEALS_DATA", "bulk"),
            "block": _parse_deals(data, "BLOCK_DEALS_DATA", "block")}


def fetch_api_multi(paths: dict[str, str], *, retries: int = 3, delay_ms: int = 1200) -> dict[str, Any]:
    """Fetch several market-wide ``/api/`` paths in ONE Camoufox session.

    ``paths`` maps a key -> API path; returns {key: parsed-json-or-None}. Warming
    the browser is the slow part, so batching avoids one launch per endpoint.
    """
    captured: dict[str, Any] = {}

    def _action(page):
        captured.update(page.evaluate(_BATCH_ANN, {"paths": paths, "retries": retries, "delay": delay_ms}))
        return page

    StealthyFetcher.fetch(_HOME, headless=True, network_idle=True, page_action=_action)
    out: dict[str, Any] = {}
    for k, body in captured.items():
        try:
            out[k] = json.loads(body) if body else None
        except (json.JSONDecodeError, TypeError):
            out[k] = None
    return out


def market_feeds(horizon_days: int = 35) -> dict[str, Any]:
    """All market-wide event feeds in one session: bulk/block deals, upcoming
    board meetings, the event calendar, and corporate actions (ex-dates).

    Board/calendar/actions take a date range (else NSE returns only the latest
    ~20 rows) — today → +``horizon_days`` so we see upcoming events, not just the
    last few announced."""
    from datetime import date, timedelta
    f = date.today().strftime("%d-%m-%Y")
    t = (date.today() + timedelta(days=horizon_days)).strftime("%d-%m-%Y")
    raw = fetch_api_multi({
        "largedeal": "/api/snapshot-capital-market-largedeal",
        "board": f"/api/corporate-board-meetings?index=equities&from_date={f}&to_date={t}",
        "calendar": f"/api/event-calendar?from_date={f}&to_date={t}",
        "actions": f"/api/corporates-corporateActions?index=equities&from_date={f}&to_date={t}",
    })
    ld = raw.get("largedeal") or {}
    return {
        "deals": {"bulk": _parse_deals(ld, "BULK_DEALS_DATA", "bulk"),
                  "block": _parse_deals(ld, "BLOCK_DEALS_DATA", "block")},
        "board_meetings": raw.get("board") or [],
        "event_calendar": raw.get("calendar") or [],
        "corp_actions": raw.get("actions") or [],
    }


def corporate_announcements(index: str = "equities", symbol: str | None = None) -> Any:
    """Corporate announcements / filings feed (results, transcripts, PPTs).

    With ``symbol`` set, returns that company's recent announcements (the
    market-wide feed only returns the latest ~20, so per-symbol is needed for
    watchlist coverage)."""
    path = f"/api/corporate-announcements?index={index}"
    if symbol:
        path += f"&symbol={q(symbol)}"
    return fetch_api(path)


_BATCH_ANN = """async ({paths, retries, delay}) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const out = {};
    for (const [key, p] of Object.entries(paths)) {
        let last = null;
        for (let i = 0; i < retries; i++) {
            const r = await fetch(p, {headers: {'Accept': 'application/json'}});
            if (r.status === 200) { last = await r.text(); break; }
            await sleep(delay);
        }
        out[key] = last;
    }
    return out;
}"""


def corporate_announcements_batch(symbols: list[str]) -> dict[str, Any]:
    """Fetch many symbols' announcements in ONE Camoufox session (warm page once,
    then in-page XHR per symbol). Returns {symbol: parsed-json-or-None}."""
    paths = {s: f"/api/corporate-announcements?index=equities&symbol={q(s)}" for s in symbols}
    captured: dict[str, Any] = {}

    def _action(page):
        captured.update(page.evaluate(_BATCH_ANN, {"paths": paths, "retries": 3, "delay": 1200}))
        return page

    StealthyFetcher.fetch(_HOME, headless=True, network_idle=True, page_action=_action)
    out: dict[str, Any] = {}
    for sym, body in captured.items():
        try:
            out[sym] = json.loads(body) if body else []
        except (json.JSONDecodeError, TypeError):
            out[sym] = []
    return out


def corporate_actions(index: str = "equities") -> Any:
    """Corporate actions (dividends, splits, bonuses, buybacks)."""
    return fetch_api(f"/api/corporates-corporateActions?index={index}")


def _parse_pledge(data: Any) -> dict | None:
    """Latest promoter-pledge snapshot from /api/corporate-pledgedata.

    Returns promoter holding %, pledged % of total shares, and the investor-
    relevant **pledged % of promoter holding** (numSharesPledged/totPromoterHolding).
    NSE returns numbers as space-padded strings.
    """
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    r = rows[0]

    def num(k: str) -> float | None:
        try:
            return float(str(r.get(k, "")).strip())
        except (TypeError, ValueError):
            return None

    prom_hold, pledged = num("totPromoterHolding"), num("numSharesPledged")
    # "% of promoter holding pledged" is only meaningful with a real promoter.
    # numSharesPledged is the company's TOTAL encumbered shares; for promoter-run
    # firms that's ~all promoter pledges (valid), but for no-promoter firms (e.g.
    # ITC, promoter 0.02%) it's non-promoter encumbrances and dividing by the tiny
    # promoter stake explodes (>100%, impossible). Reject those → n/a; the always-
    # valid figure is pledged_pct_of_total (NSE's percSharesPledged).
    pledged_of_prom = (100 * pledged / prom_hold) if prom_hold and pledged is not None else None
    if pledged_of_prom is not None and not (0 <= pledged_of_prom <= 100):
        pledged_of_prom = None
    return {
        "as_of": (r.get("shp") or "").strip() or None,
        "promoter_holding_pct": num("percPromoterHolding"),
        "pledged_pct_of_total": num("percSharesPledged"),
        "pledged_pct_of_promoter": pledged_of_prom,
        "num_shares_pledged": pledged,
        "broadcast_dt": (r.get("broadcastDt") or "").strip() or None,
    }


def promoter_pledge(symbol: str) -> dict | None:
    """Latest promoter share-pledge snapshot for ``symbol`` (None if unavailable)."""
    try:
        data = fetch_api(f"/api/corporate-pledgedata?index=equities&symbol={q(symbol)}")
    except Exception:  # noqa: BLE001 — degrade to n/a, never break the report
        return None
    return _parse_pledge(data)


def promoter_pledge_batch(symbols: list[str]) -> dict[str, dict | None]:
    """Pledge snapshot for many symbols in ONE Camoufox session (warm once)."""
    paths = {s: f"/api/corporate-pledgedata?index=equities&symbol={q(s)}" for s in symbols}
    captured: dict[str, Any] = {}

    def _action(page):
        captured.update(page.evaluate(_BATCH_ANN, {"paths": paths, "retries": 3, "delay": 1200}))
        return page

    StealthyFetcher.fetch(_HOME, headless=True, network_idle=True, page_action=_action)
    out: dict[str, dict | None] = {}
    for sym, body in captured.items():
        try:
            out[sym] = _parse_pledge(json.loads(body)) if body else None
        except (json.JSONDecodeError, TypeError):
            out[sym] = None
    return out


def option_chain_equity(symbol: str) -> Any:
    """Stock option chain (strike-wise OI) for ``symbol`` (e.g. ``RELIANCE``)."""
    return fetch_api(f"/api/option-chain-equities?symbol={q(symbol)}")


def trading_holidays() -> set:
    """NSE equity (CM segment) trading holidays as a set of ``date`` objects."""
    from datetime import datetime as _dt
    out: set = set()
    try:
        data = fetch_api("/api/holiday-master?type=trading")
    except Exception:  # noqa: BLE001
        return out
    for r in (data.get("CM") if isinstance(data, dict) else []) or []:
        try:
            out.add(_dt.strptime(r["tradingDate"], "%d-%b-%Y").date())
        except (KeyError, ValueError, TypeError):
            continue
    return out
