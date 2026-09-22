"""Google Trends signal source for the ⛏️ Pickaxe pipeline (see ``analysis/pickaxe.py``).

The demand-side analogue of the Tailwind Scout's news feed: what are Indians *searching to buy*
more of, and which product terms are *spiking*. There is **no official Google Trends API**, and a
plain-HTTP client (pytrends) gets **HTTP 429**'d by Google's bot wall almost immediately — the same
wall NSE's WAF throws. So this uses the project's **browser tier** (scrapling ``StealthyFetcher`` /
Camoufox), exactly like ``scrapers/nse_api.py``: load the real Trends *explore* page for a query in
a stealth browser (which clears the bot challenge and carries real cookies/fingerprint) and
**intercept the ``widgetdata`` XHR responses the page fires itself** to render its charts. Those
carry perfect tokens/timing, so they succeed where our own direct API calls 429.

Two probes, both driven from ONE browser session (a page navigation per target):
* ``scout_buy_surges()`` — rising **"buy"** queries within each consumer category, region India,
  last 3 months (the user's manual move — "buy" + category + India + % change). The discovery net.
* ``interest_over_time(term)`` — confirm a term is genuinely spiking (latest vs its own year).

Still **best-effort**: Google rate-limits, so any failure returns ``[]``/``{}`` and a per-process
**circuit-breaker** trips once Google clearly throttles us, so we don't burn a minute of navigations
on a host that's refusing. When Trends yields nothing the Pickaxe pipeline still runs on the
web-search-grounded LLM alone (qualitative demand read, no hard %) — Trends is an *enricher*, not
a dependency. (Automated access to Google Trends is subject to Google's Terms of Service; this is a
personal-research tool.)
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote

from equity_research import config

log = logging.getLogger("equity-research.pickaxe")

# Consumer-demand categories (Google Trends numeric category IDs) — where a retail buying surge
# shows up. A small, representative catalog (NOT exhaustive); the LLM Analyst catches the long tail.
_CATEGORIES: dict[str, int] = {
    "Health": 45,
    "Food & Drink": 71,
    "Beauty & Fitness": 44,
    "Sports": 20,
    "Home & Garden": 11,
    "Autos & Vehicles": 47,
    "Shopping": 18,
    "Hobbies & Leisure": 65,
}

_GEO = "IN"
_HL = "en-US"
_TZ = "-330"                    # IST offset in minutes (JS getTimezoneOffset convention)
_BUY_DATE = "today 3-m"        # rising "buy" queries over the last 3 months (the user's window)
_IOT_DATE = "today 12-m"       # interest-over-time: a year of context to judge a spike
_NAV_WAIT_S = 3.0              # let the page fire its widgetdata XHRs after navigation settles
_DEAD_AFTER = 3               # consecutive empty navigations → assume Google is throttling, bail

_trends_dead = False           # per-process circuit-breaker


def _explore_url(query: str, *, cat: int, date: str) -> str:
    return (f"https://trends.google.com/trends/explore?geo={_GEO}&hl={_HL}"
            f"&date={quote(date)}&cat={cat}&q={quote(query)}")


def _strip(body: str):
    """Google prefixes its JSON with ``)]}',`` — parse from the first brace. ``None`` on junk."""
    i = body.find("{")
    if i < 0:
        return None
    try:
        return json.loads(body[i:])
    except (ValueError, TypeError):
        return None


def _parse_rising(body: str, category: str) -> list[dict]:
    """Rising related queries from a ``relatedsearches`` response → ``[{phrase, change, category}]``.
    The LAST rankedList is the *rising* set; the endpoint also serves related TOPICS (``.topic.title``
    instead of ``.query``), which we tolerate."""
    j = _strip(body)
    if not j:
        return []
    try:
        lists = j["default"]["rankedList"]
    except (KeyError, TypeError):
        return []
    if not lists:
        return []
    out = []
    for k in lists[-1].get("rankedKeyword", []):
        phrase = k.get("query") or (k.get("topic") or {}).get("title")
        if not phrase:
            continue
        change = str(k.get("formattedValue") or k.get("value") or "").strip()
        out.append({"phrase": phrase.strip(), "change": change, "category": category})
    return out


def _parse_timeline(body: str) -> list[float]:
    """Interest-over-time series from a ``multiline`` response → ``[value, …]``."""
    j = _strip(body)
    if not j:
        return []
    try:
        return [float(p["value"][0]) for p in j["default"]["timelineData"]]
    except (KeyError, TypeError, ValueError, IndexError):
        return []


def _collect(targets: list[dict]) -> dict[str, dict]:
    """Run ONE stealth-browser session, navigating the explore page for each target and intercepting
    the ``widgetdata`` XHR bodies the page fires. ``targets`` is
    ``[{label, q, cat, date, kind: 'buy'|'iot'}]``; returns ``{label: {rising: [...] | timeline: [...]}}``.
    Best-effort: trips the circuit-breaker (and returns what it has) once Google throttles us. ``{}``
    if the browser tier is unavailable."""
    global _trends_dead
    if _trends_dead or not targets:
        return {}
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception:  # noqa: BLE001 — scrapling missing → degrade to LLM-only
        _trends_dead = True
        log.info("trends: scrapling unavailable — Pickaxe will run on the LLM/Search path only")
        return {}

    results: dict[str, dict] = {}
    state = {"bodies": {}, "empty_streak": 0}

    def _on_response(resp):
        u = resp.url
        if "widgetdata/multiline" in u or "widgetdata/relatedsearches" in u:
            try:
                state["bodies"].setdefault(u, resp.text())
            except Exception:  # noqa: BLE001 — body not readable → skip
                pass

    def _action(page):
        global _trends_dead
        page.on("response", _on_response)
        for t in targets:
            state["bodies"] = {}
            try:
                page.goto(_explore_url(t["q"], cat=t["cat"], date=t["date"]),
                          wait_until="networkidle", timeout=45000)
            except Exception:  # noqa: BLE001 — one bad nav shouldn't kill the sweep
                state["empty_streak"] += 1
                if state["empty_streak"] >= _DEAD_AFTER:
                    _trends_dead = True
                    log.info("trends: Google throttling (nav failures) — LLM path carries the rest")
                    break
                continue
            time.sleep(_NAV_WAIT_S)
            got = False
            for u, body in state["bodies"].items():
                if t["kind"] == "buy" and "relatedsearches" in u:
                    rows = _parse_rising(body, t.get("category", ""))
                    if rows:
                        results.setdefault(t["label"], {}).setdefault("rising", []).extend(rows)
                        got = True
                elif t["kind"] == "iot" and "multiline" in u:
                    tl = _parse_timeline(body)
                    if tl:
                        results.setdefault(t["label"], {})["timeline"] = tl
                        got = True
            state["empty_streak"] = 0 if got else state["empty_streak"] + 1
            if state["empty_streak"] >= _DEAD_AFTER:
                _trends_dead = True
                log.info("trends: Google throttling (%d empty navs) — LLM path carries the rest",
                         _DEAD_AFTER)
                break
        return page

    first = targets[0]
    try:
        StealthyFetcher.fetch(_explore_url(first["q"], cat=first["cat"], date=first["date"]),
                              headless=True, network_idle=True, page_action=_action)
    except Exception:  # noqa: BLE001 — browser launch / fetch failed → degrade
        _trends_dead = True
        log.info("trends: browser tier failed to launch — LLM path carries the pipeline")
    return results


def scout_buy_surges(*, per_cat: int = config.TRENDS_PER_CAT, max_signals: int = config.TRENDS_MAX_SIGNALS) -> list[dict]:
    """Sweep every consumer category (ONE browser session) for rising "buy" queries and merge into a
    deduped list of demand signals ``[{phrase, change, category}]``. ``[]`` if Trends is unavailable
    (the pipeline then runs LLM-only). The raw list is deliberately unfiltered (spam/fad queries
    included) — the LLM Demand Analyst is what separates real product themes from noise."""
    targets = [{"label": name, "q": "buy", "cat": cid, "date": _BUY_DATE, "kind": "buy"}
               for name, cid in _CATEGORIES.items()]
    collected = _collect(targets)
    seen: set[str] = set()
    out: list[dict] = []
    for name in _CATEGORIES:
        for row in (collected.get(name, {}).get("rising") or [])[:per_cat]:
            key = row["phrase"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            if len(out) >= max_signals:
                log.info("trends: %d rising buy-query signals (hit cap)", len(out))
                return out
    log.info("trends: %d rising buy-query signals across %d categories", len(out), len(_CATEGORIES))
    return out


def _summarise(term: str, series: list[float]) -> dict:
    """Compute the spike summary + keep the raw series for charting. ``{}`` if too thin."""
    if len(series) < 4:
        return {}
    latest, avg, peak = float(series[-1]), sum(series) / len(series), max(series)
    n = len(series)
    recent = sum(series[-max(1, n // 4):]) / max(1, n // 4)
    prior_part = series[:n - n // 4] or series
    prior = sum(prior_part) / len(prior_part)
    slope_pct = round((recent - prior) / prior * 100, 1) if prior else None
    spiking = latest > avg * 1.25 and (slope_pct or 0) > 15
    return {"term": term, "series": series, "latest": round(latest, 1), "avg": round(avg, 1),
            "peak": round(peak, 1), "slope_pct": slope_pct, "spiking": spiking}


def interest_details(terms: list[str]) -> dict[str, dict]:
    """Interest-over-time for several terms in India over the last year, in ONE browser session
    (minimises rate-limit exposure). Returns ``{term: {term, series, latest, avg, peak, slope_pct,
    spiking}}`` for each term that came back. ``{}`` if Trends is unavailable."""
    terms = [t for t in dict.fromkeys(t.strip() for t in terms) if t]
    if not terms:
        return {}
    targets = [{"label": t, "q": t, "cat": 0, "date": _IOT_DATE, "kind": "iot"} for t in terms]
    collected = _collect(targets)
    out = {}
    for t in terms:
        s = _summarise(t, collected.get(t, {}).get("timeline") or [])
        if s:
            out[t] = s
    return out


def interest_over_time(term: str) -> dict:
    """Confirm ONE ``term`` is spiking in India over the last year (ONE browser session). Returns
    ``{term, series, latest, avg, peak, slope_pct, spiking}`` (``slope_pct`` = recent-quarter mean vs
    the prior mean; ``spiking`` = latest well above its own average and rising). ``{}`` on failure."""
    return interest_details([term]).get(term.strip(), {})
