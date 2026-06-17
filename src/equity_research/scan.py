"""Watchlist scan orchestrator (Phase 5).

Refreshes the latest EOD, runs every per-symbol detector (technical + fundamental
from the DB, announcements via one batched browser session), and returns the
fired alerts plus a market FII/DII note. The Telegram bot pushes the results and
generates a deep report for any 'results filed' alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.common.http import ScrapeError, fetch_bytes
from equity_research.ingest import ingest_eod, store_pledge
from equity_research.scrapers import nse_api
from equity_research import watchlist


_IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("equity_research.scan")

# Event types whose attached filing PDF is worth an inline Gemini read.
_ANALYZE_TITLES = {"Results filed", "Concall / investor meet", "Scheme / M&A",
                   "Open offer / SAST", "Rights issue", "QIP / fund raising"}


@dataclass
class ScanResult:
    results: dict[str, list[alerts.Alert]] = field(default_factory=dict)
    movers: list[dict] = field(default_factory=list)
    upcoming: list[dict] = field(default_factory=list)


def _meta(con, key):
    r = con.execute("SELECT value FROM alert_state WHERE symbol='__meta__' AND key=?", [key]).fetchone()
    return r[0] if r else None


def _set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                "VALUES ('__meta__', ?, ?, now())", [key, value])


def _holidays(con: duckdb.DuckDBPyConnection) -> set[date]:
    """NSE trading holidays, cached in alert_state; refetched if >30 days stale."""
    raw, fetched = _meta(con, "holidays"), _meta(con, "holidays_fetched")
    fresh = False
    if fetched:
        try:
            fresh = (date.today() - date.fromisoformat(fetched)).days <= 30
        except ValueError:
            fresh = False
    if raw and fresh:
        return {date.fromisoformat(x) for x in raw.split(",") if x}
    hs = nse_api.trading_holidays()
    if hs:
        _set_meta(con, "holidays", ",".join(d.isoformat() for d in sorted(hs)))
        _set_meta(con, "holidays_fetched", date.today().isoformat())
        return hs
    return {date.fromisoformat(x) for x in raw.split(",") if x} if raw else set()  # stale fallback


def is_trading_day(con: duckdb.DuckDBPyConnection, d: date) -> bool:
    """Weekday and not an NSE holiday."""
    if d.weekday() >= 5:
        return False
    return d not in _holidays(con)


def market_open_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    """Is today (IST) a trading session? Used to skip weekend/holiday scans."""
    own = con is None
    con = con or connect()
    try:
        return is_trading_day(con, datetime.now(_IST).date())
    finally:
        if own:
            con.close()


def already_scanned_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_scan_date") == datetime.now(_IST).date().isoformat()
    finally:
        if own:
            con.close()


def mark_scanned(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        _set_meta(con, "last_scan_date", datetime.now(_IST).date().isoformat())
    finally:
        if own:
            con.close()


def refresh_eod(con: duckdb.DuckDBPyConnection, lookback: int = 7) -> date | None:
    """Ingest the latest available trading day's full EOD set (idempotent)."""
    today = date.today()
    for i in range(lookback + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            ingest_eod(d, con)
            return d
        except ScrapeError:
            continue
    return None


def fii_dii_note() -> str | None:
    """One-line market note from the latest FII/DII cash activity (event 15)."""
    try:
        rows = nse_api.fii_dii_activity()
    except Exception:  # noqa: BLE001
        return None
    parts = []
    for r in rows if isinstance(rows, list) else []:
        cat = r.get("category", "")
        net = (r.get("netValue") or r.get("buyValue"))
        try:
            net = float(r.get("netValue")) if r.get("netValue") is not None else None
        except (TypeError, ValueError):
            net = None
        if net is not None:
            parts.append(f"{cat} net ₹{net:,.0f} cr")
    return "📊 FII/DII (cash): " + " · ".join(parts) if parts else None


def _fmt_qty(q: float | None) -> str:
    if q is None:
        return "?"
    if q >= 1e7:
        return f"{q / 1e7:.2f} Cr"
    if q >= 1e5:
        return f"{q / 1e5:.1f} L"
    return f"{q:,.0f}"


def _deal_alert(dl: dict) -> alerts.Alert:
    """A bulk/block-deal Alert (green BUY / red SELL) for a watchlist stock."""
    sev = "green" if dl.get("buy_sell") == "BUY" else "red"
    price = f"₹{dl['price']:,.0f}" if dl.get("price") else "?"
    title = f"{dl['deal_type'].title()} deal — {dl.get('buy_sell', '').title()}"
    body = f"{dl.get('client', '?')} {dl.get('buy_sell', '').lower()} {_fmt_qty(dl.get('qty'))} sh @ {price}"
    return alerts.Alert(dl["symbol"], sev, title, body)


def watchlist_deals(syms: list[str], deals: dict) -> dict[str, list[alerts.Alert]]:
    """Bulk/block deals (pre-fetched, market-wide) filtered to ``syms``."""
    symset = set(syms)
    out: dict[str, list[alerts.Alert]] = {}
    for dl in (deals.get("bulk") or []) + (deals.get("block") or []):
        sym = dl.get("symbol")
        if sym in symset and dl.get("client"):
            out.setdefault(sym, []).append(_deal_alert(dl))
    return out


def _parse_dt(s) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except (TypeError, ValueError, AttributeError):
        return None


def watchlist_upcoming(syms: list[str], feeds: dict, days: int = 30) -> list[dict]:
    """Upcoming events for the watchlist (next ``days``): board meetings (with
    purpose), results / fund-raising / AGM (event calendar), and ex-dividend /
    split / bonus dates (corporate actions). Returns [{symbol, date, what}]."""
    symset = set(syms)
    today = datetime.now(_IST).date()
    horizon = today + timedelta(days=days)
    seen: set = set()
    out: list[dict] = []

    def add(sym, d, what):
        if not sym or sym not in symset or d is None or d < today or d > horizon:
            return
        key = (sym, d, what.lower()[:24])
        if key in seen:
            return
        seen.add(key)
        out.append({"symbol": sym, "date": d, "what": what})

    for r in feeds.get("board_meetings") or []:
        desc = (r.get("bm_desc") or "")
        purpose = (desc.split("consider", 1)[1].strip().rstrip(".")
                   if "consider" in desc.lower() else (r.get("bm_purpose") or "meeting"))
        add(r.get("bm_symbol"), _parse_dt(r.get("bm_date")), f"Board meeting — {purpose}"[:70])
    for r in feeds.get("event_calendar") or []:
        add(r.get("symbol"), _parse_dt(r.get("date")), r.get("purpose") or "Event")
    for r in feeds.get("corp_actions") or []:
        add(r.get("symbol"), _parse_dt(r.get("exDate")),
            f"{(r.get('subject') or 'Corporate action')} (ex-date)")
    out.sort(key=lambda u: u["date"])
    return out


def _enrich_event_docs(results: dict[str, list[alerts.Alert]], cap: int = 5) -> None:
    """Download + Gemini-analyse the attached filing for notable doc-bearing events
    (results / concall / scheme / etc.), inline. Capped to keep heavy days bounded."""
    candidates = [(sym, al) for sym, fired in results.items() for al in fired
                  if al.attachment and al.title in _ANALYZE_TITLES]
    prio = {"Results filed": 0, "Concall / investor meet": 1}
    candidates.sort(key=lambda x: prio.get(x[1].title, 5))
    if not candidates:
        return
    from equity_research.reports import synthesize  # lazy: keeps genai off the hot path
    for sym, al in candidates[:cap]:
        try:
            al.analysis = synthesize.analyze_filing(fetch_bytes(al.attachment), sym, al.title)
        except Exception:  # noqa: BLE001 — a bad doc shouldn't break the scan
            log.exception("filing analysis failed for %s (%s)", sym, al.title)


def watchlist_movers(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Per-stock daily snapshot: close, day %change, delivery%, 52-week position.

    The always-populated skeleton of the digest (price/volume are the only things
    that change every day). Sorted biggest-move first. Carries the company name.
    """
    names = dict(watchlist.entries(con))
    out: list[dict] = []
    for sym in watchlist.symbols(con):
        row = con.execute(
            "SELECT trade_date, close, prev_close, deliv_per FROM equity_eod "
            "WHERE symbol = ? AND series = 'EQ' ORDER BY trade_date DESC LIMIT 1", [sym]).fetchone()
        if not row or row[1] is None:
            continue
        d, close, prev, deliv = row
        hl = con.execute(
            "SELECT max(high), min(low) FROM equity_eod WHERE symbol = ? AND series = 'EQ' "
            "AND trade_date >= ?", [sym, d - timedelta(days=365)]).fetchone()
        hi, lo = (hl or (None, None))
        chg = (close / prev - 1) * 100 if prev else None
        pos = (close - lo) / (hi - lo) * 100 if hi and lo and hi > lo else None
        out.append({"symbol": sym, "company": names.get(sym) or sym, "close": close,
                    "chg_pct": chg, "deliv": deliv, "pos_52w": pos})
    out.sort(key=lambda m: abs(m["chg_pct"]) if m["chg_pct"] is not None else 0, reverse=True)
    return out


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{p:,.2f}" if p < 100 else f"{p:,.0f}"   # decimals for low-priced/penny stocks


def _pos_label(pos: float | None) -> str:
    if pos is None:
        return ""
    if pos >= 90:
        return "near 52w-high"
    if pos <= 10:
        return "near 52w-low"
    return f"{pos:.0f}% of 52w range"


def format_digest(date_str: str, sr: ScanResult) -> str:
    """Build the digest markdown — Upcoming events, a per-stock Movers snapshot,
    and Events (with any inline filing analysis), all by company name (ticker in
    parens). Shared by the email and Telegram channels."""
    results, movers, upcoming = sr.results, sr.movers, sr.upcoming
    names = {m["symbol"]: m["company"] for m in movers}
    parts = [f"# Watchlist — {date_str}"]

    if upcoming:
        rows = ["## 📅 Upcoming"]
        for u in upcoming:
            nm = names.get(u["symbol"]) or u["symbol"]
            rows.append(f"- **{nm}** ({u['symbol']}) — {u['date']:%d-%b}: {u['what']}")
        parts.append("\n".join(rows))

    if movers:
        rows = ["## Movers (today)"]
        for m in movers:
            chg = f"{m['chg_pct']:+.1f}%" if m["chg_pct"] is not None else "n/a"
            deliv = f"deliv {m['deliv']:.0f}%" if m["deliv"] is not None else "deliv n/a"
            tail = f" · {_pos_label(m['pos_52w'])}" if _pos_label(m["pos_52w"]) else ""
            rows.append(f"- **{m['company']}** ({m['symbol']}) — ₹{_fmt_price(m['close'])} · {chg} · {deliv}{tail}")
        parts.append("\n".join(rows))

    if results:
        ev = ["## Events (today)"]
        for sym in sorted(results, key=lambda s: names.get(s, s)):
            lines = [f"**{names.get(sym) or sym}** ({sym})"]
            for al in results[sym]:
                emo = alerts.EMOJI.get(al.severity, "🔔")
                lines.append(f"- {emo} {al.title}" + (f" — {al.body}" if al.body else ""))
                if al.analysis:                       # inline filing read, as a quote block
                    lines.append("")
                    lines += [f"> {ln}" for ln in al.analysis.splitlines() if ln.strip()]
                    lines.append("")
            ev.append("\n".join(lines))
        parts.append("\n\n".join(ev))
    else:
        parts.append("_No corporate events, institutional deals, or forensic changes today._")

    parts.append("_Reply with a company name to get its full report._")
    return "\n\n".join(parts)


def run_watchlist_scan(con: duckdb.DuckDBPyConnection | None = None) -> ScanResult:
    """Scan the watchlist → ScanResult(results, movers, upcoming). Ingests EOD first."""
    own = con is None
    con = con or connect()
    try:
        refresh_eod(con)
        syms = watchlist.symbols(con)
        # one batched browser session for all symbols' announcements
        try:
            anns_by_sym = nse_api.corporate_announcements_batch(syms) if syms else {}
        except Exception:  # noqa: BLE001
            anns_by_sym = {}
        # one more batched session for promoter-pledge snapshots (persist + alert)
        try:
            pledge_by_sym = nse_api.promoter_pledge_batch(syms) if syms else {}
            store_pledge(con, pledge_by_sym)
        except Exception:  # noqa: BLE001
            pledge_by_sym = {}
        # one session for all market-wide feeds: deals + upcoming events
        try:
            feeds = nse_api.market_feeds() if syms else {}
        except Exception:  # noqa: BLE001
            feeds = {}
        results: dict[str, list[alerts.Alert]] = {}
        for sym in syms:
            try:
                fired = alerts.scan_symbol(con, sym, anns_by_sym.get(sym, []),
                                           pledge_by_sym.get(sym))
            except Exception:  # noqa: BLE001 — one bad symbol shouldn't kill the scan
                fired = []
            if fired:
                results[sym] = fired
        # per-stock bulk/block deals (institutional buy/sell) — merge in
        for sym, deal_alerts in watchlist_deals(syms, feeds.get("deals") or {}).items():
            results.setdefault(sym, []).extend(deal_alerts)
        _enrich_event_docs(results)                         # inline Gemini read of filings
        return ScanResult(results, watchlist_movers(con), watchlist_upcoming(syms, feeds))
    finally:
        if own:
            con.close()
