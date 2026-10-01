"""Dividends you received and the money-weighted return (XIRR).

* **Dividends** — every cash dividend (and REIT / InvIT distribution) per share from NSE's per-symbol
  corporate-action record (``dividends``), refreshed weekly per stock you hold, in the background. What you got
  is worked out from your own buys: the shares you held just before each ex-date × the amount. Undated buys
  can't be placed in time, so they don't collect past dividends. Dividends are taxed at your slab rate.
* **XIRR** — your buys (money out, on their dates), sells and dividends (money in) and today's value. Shares
  that arrived through a demerger aren't a payment. Undated buys are left out.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta

import duckdb

log = logging.getLogger(__name__)
REFRESH_DAYS = 7
_AMOUNT = re.compile(r"(?:dividend|distribution)[^+]*?\br[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_lock = threading.Lock()


def parse_amount(subject: str) -> float | None:
    """₹ per share in a corporate-action subject ('Interim Dividend - Rs 2.50 Per Share + Special Dividend -
    Rs 5 Per Share' → 7.5); None for anything else (incl. old '% of face value' subjects)."""
    got = [float(x) for x in _AMOUNT.findall(str(subject or ""))]
    return sum(got) if got else None


def _parse_date(s) -> date | None:
    try:
        return datetime.strptime(str(s).strip(), "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None


def store_actions(con: duckdb.DuckDBPyConnection, symbol: str, rows) -> int:
    """Keep the dividend rows of NSE's per-symbol corporate-action record."""
    n = 0
    for r in rows if isinstance(rows, list) else []:
        amt, ex = parse_amount(r.get("subject")), _parse_date(r.get("exDate"))
        if amt and ex:
            con.execute("INSERT OR REPLACE INTO dividends VALUES (?, ?, ?, ?)",
                        [symbol, ex, amt, " ".join(str(r.get("subject")).split())])
            n += 1
    con.execute("INSERT OR REPLACE INTO dividend_fetch VALUES (?, now())", [symbol])
    return n


def stale(con: duckdb.DuckDBPyConnection, symbols: list[str]) -> list[str]:
    """Symbols whose dividend history is missing or older than ``REFRESH_DAYS`` (BSE-only ones aren't covered).
    Empty when NSE access is off — nothing would fetch them."""
    from equity_research.scrapers.nse_api import _nse_scraping_enabled

    if not _nse_scraping_enabled():
        return []
    fresh = {r[0] for r in con.execute("SELECT symbol FROM dividend_fetch WHERE fetched_at > ?",
                                       [datetime.now() - timedelta(days=REFRESH_DAYS)]).fetchall()}
    return sorted({s for s in symbols if s not in fresh and not s.startswith("BSE:")})


def refresh(con: duckdb.DuckDBPyConnection, symbols: list[str]) -> int:
    from equity_research.scrapers import nse_api

    if not symbols:
        return 0
    got = nse_api.fetch_api_multi(
        {s: f"/api/corporates-corporateActions?index=equities&symbol={nse_api.q(s)}" for s in symbols})
    n = sum(store_actions(con, s, got.get(s)) for s in symbols if got.get(s) is not None)
    log.info("dividends: %d record(s) for %d stock(s)", n, len(symbols))
    return n


def refresh_async(symbols: list[str]) -> None:
    from equity_research.scrapers.nse_api import _nse_scraping_enabled

    if not symbols or not _nse_scraping_enabled() or not _lock.acquire(blocking=False):
        return

    def run():
        from equity_research.common.db import connect

        con = connect()
        try:
            refresh(con, symbols)
        except Exception:  # noqa: BLE001 — NSE access off / a failed session: retried on a later page load
            log.exception("dividends: refresh failed")
        finally:
            con.close()
            _lock.release()

    threading.Thread(target=run, name="dividends", daemon=True).start()


def for_symbol(con: duckdb.DuckDBPyConnection, symbol: str, since: date | None, until: date) -> list[tuple]:
    """[(ex_date, ₹ per share)] after ``since`` (all if None) up to ``until``."""
    return con.execute("""SELECT ex_date, sum(amount) FROM dividends WHERE symbol = ? AND ex_date <= ?
                          AND (? IS NULL OR ex_date > ?) GROUP BY ex_date ORDER BY ex_date""",
                       [symbol, until, since, since]).fetchall()


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annual money-weighted return of dated cash flows (money out negative); None if it can't be solved or
    the money has been in for under a month (an annualised figure would mislead)."""
    from equity_research.analysis.funds import _xirr

    flows = sorted((d, cf) for d, cf in flows if cf)
    if len(flows) < 2 or not any(cf < 0 for _, cf in flows) or not any(cf > 0 for _, cf in flows):
        return None
    if (flows[-1][0] - flows[0][0]).days < 30:
        return None
    return _xirr(flows)
