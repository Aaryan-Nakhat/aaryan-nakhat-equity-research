"""Watchlist scan orchestrator (Phase 5).

Refreshes the latest EOD, runs every per-symbol detector (technical + fundamental
from the DB, announcements via one batched browser session), and returns the
fired alerts plus a market FII/DII note. The Telegram bot pushes the results and
generates a deep report for any 'results filed' alert.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.common.http import ScrapeError
from equity_research.ingest import ingest_eod, store_pledge
from equity_research.scrapers import nse_api
from equity_research import watchlist


_IST = ZoneInfo("Asia/Kolkata")


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


def watchlist_deals(con: duckdb.DuckDBPyConnection, syms: list[str]) -> dict[str, list[alerts.Alert]]:
    """Today's bulk/block deals (market-wide, one fetch) filtered to ``syms``."""
    try:
        deals = nse_api.large_deals()
    except Exception:  # noqa: BLE001
        return {}
    symset = set(syms)
    out: dict[str, list[alerts.Alert]] = {}
    for dl in (deals.get("bulk") or []) + (deals.get("block") or []):
        sym = dl.get("symbol")
        if sym in symset and dl.get("client"):
            out.setdefault(sym, []).append(_deal_alert(dl))
    return out


def run_watchlist_scan(con: duckdb.DuckDBPyConnection | None = None
                       ) -> tuple[dict[str, list[alerts.Alert]], str | None]:
    """Returns ({symbol: [alerts]}, market_note). Ingests latest EOD first."""
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
        for sym, deal_alerts in watchlist_deals(con, syms).items():
            results.setdefault(sym, []).extend(deal_alerts)
        return results, None      # market-wide FII/DII note dropped (per-stock now)
    finally:
        if own:
            con.close()
