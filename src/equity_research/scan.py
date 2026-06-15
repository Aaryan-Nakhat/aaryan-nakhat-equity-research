"""Watchlist scan orchestrator (Phase 5).

Refreshes the latest EOD, runs every per-symbol detector (technical + fundamental
from the DB, announcements via one batched browser session), and returns the
fired alerts plus a market FII/DII note. The Telegram bot pushes the results and
generates a deep report for any 'results filed' alert.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb

from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.common.http import ScrapeError
from equity_research.ingest import ingest_eod
from equity_research.scrapers import nse_api
from equity_research import watchlist


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
        results: dict[str, list[alerts.Alert]] = {}
        for sym in syms:
            try:
                fired = alerts.scan_symbol(con, sym, anns_by_sym.get(sym, []))
            except Exception:  # noqa: BLE001 — one bad symbol shouldn't kill the scan
                fired = []
            if fired:
                results[sym] = fired
        return results, fii_dii_note()
    finally:
        if own:
            con.close()
