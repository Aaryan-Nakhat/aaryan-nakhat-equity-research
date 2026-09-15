"""🔔 Filing Alerts — standing keyword alerts on exchange announcements.

The user registers keywords ("order win", "capacity", "QIP", "resignation"…); a gated background
sweep matches every new market-wide filing against them and pushes an email the moment one hits.
Forward-looking by design — a **high-watermark** on announcement time means each sweep only looks at
filings newer than the last, so dedup is automatic and a freshly-added keyword never dumps a backlog.

No LLM — plain, transparent substring matching over the raw announcement text.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import duckdb

from equity_research import scan
from equity_research.scrapers import nse_api

log = logging.getLogger(__name__)

_SWEEP_DAYS = 3            # market-wide announcement window each sweep (covers weekends/overnight)


def add_keyword(con: duckdb.DuckDBPyConnection, keyword: str) -> str:
    """Register a keyword (lowercased, trimmed). Idempotent. Returns the stored form."""
    kw = " ".join(keyword.strip().lower().split())
    con.execute("INSERT OR REPLACE INTO alert_keywords(keyword, added_at) VALUES (?, now())", [kw])
    return kw


def remove_keyword(con: duckdb.DuckDBPyConnection, keyword: str) -> bool:
    """Remove a keyword. Returns True if it existed."""
    kw = " ".join(keyword.strip().lower().split())
    existed = con.execute("SELECT 1 FROM alert_keywords WHERE keyword = ?", [kw]).fetchone() is not None
    con.execute("DELETE FROM alert_keywords WHERE keyword = ?", [kw])
    return existed


def list_keywords(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT keyword FROM alert_keywords ORDER BY added_at").fetchall()]


def clear_keywords(con: duckdb.DuckDBPyConnection) -> int:
    n = con.execute("SELECT count(*) FROM alert_keywords").fetchone()[0]
    con.execute("DELETE FROM alert_keywords")
    return n


def _an_dt(a: dict) -> datetime:
    try:
        return datetime.strptime((a.get("an_dt") or "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def _matches(blob: str, keyword: str) -> bool:
    """True if **every word** of ``keyword`` appears (as a substring) in ``blob`` — so 'order win'
    matches 'won a large order' (words present, order-independent) and 'win' matches 'wins'. ``blob``
    must already be lowercased."""
    words = keyword.split()
    return bool(words) and all(w in blob for w in words)


def scan_new(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Match new market-wide filings (newer than the stored watermark) against the saved keywords,
    advance the watermark, and return the matches ``[{symbol, headline, keyword, url, an_dt}]``.

    The **first** run (no watermark) just seeds the watermark and returns nothing — alerts are
    forward-looking, so adding a keyword never dumps a backlog. Never raises."""
    keywords = list_keywords(con)
    if not keywords:
        return []
    f = (date.today() - timedelta(days=_SWEEP_DAYS)).strftime("%d-%m-%Y")
    t = date.today().strftime("%d-%m-%Y")
    try:
        raw = nse_api.corporate_announcements(from_date=f, to_date=t)
    except Exception:  # noqa: BLE001
        log.exception("keyword-alerts: announcement sweep failed")
        return []
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        return []

    parsed = [(a, _an_dt(a)) for a in rows if isinstance(a, dict)]
    parsed = [(a, dt) for a, dt in parsed if dt != datetime.min]
    if not parsed:
        return []
    batch_max = max(dt for _, dt in parsed)

    wm_raw = scan.alert_watermark(con)
    if not wm_raw:                                     # first run — seed, don't push a backlog
        scan.set_alert_watermark(batch_max.isoformat(), con)
        log.info("keyword-alerts: seeded watermark at %s (no backlog push)", batch_max)
        return []
    try:
        watermark = datetime.fromisoformat(wm_raw)
    except (ValueError, TypeError):
        watermark = datetime.min

    matches: list[dict] = []
    for a, dt in parsed:
        if dt <= watermark:
            continue
        blob = f"{a.get('desc', '')} {a.get('attchmntText', '')}".lower()
        sym = (a.get("symbol") or "").strip().upper()
        for kw in keywords:
            if _matches(blob, kw):
                headline = " ".join(f"{a.get('desc', '')} {a.get('attchmntText', '')}".split())[:140]
                matches.append({"symbol": sym or "—", "headline": headline or "—", "keyword": kw,
                                "url": (a.get("attchmntFile") or "").strip(),
                                "an_dt": dt.strftime("%d-%b %H:%M")})
                break                                  # one row → at most one alert line
    if batch_max > watermark:
        scan.set_alert_watermark(batch_max.isoformat(), con)
    return matches
