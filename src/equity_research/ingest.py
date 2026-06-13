"""Ingest — scrape a trade date's EOD data and land it in DuckDB.

Each function fetches via ``scrapers``, renames columns to the schema in
``common.db``, and writes idempotently (re-running a date overwrites it).
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from equity_research.common.db import replace_for_date
from equity_research.common.http import ScrapeError, fetch_bytes
from equity_research.scrapers import nse_archives, nse_financials

# Source-column -> schema-column maps (schema order preserved on write).
_EOD_MAP = {
    "SYMBOL": "symbol", "SERIES": "series", "PREV_CLOSE": "prev_close",
    "OPEN_PRICE": "open", "HIGH_PRICE": "high", "LOW_PRICE": "low",
    "LAST_PRICE": "last", "CLOSE_PRICE": "close", "AVG_PRICE": "avg_price",
    "TTL_TRD_QNTY": "ttl_trd_qnty", "TURNOVER_LACS": "turnover_lacs",
    "NO_OF_TRADES": "no_of_trades", "DELIV_QTY": "deliv_qty", "DELIV_PER": "deliv_per",
}
_INDEX_MAP = {
    "Index Name": "index_name", "Open Index Value": "open",
    "High Index Value": "high", "Low Index Value": "low",
    "Closing Index Value": "close", "Points Change": "points_change",
    "Change(%)": "pct_change", "Volume": "volume",
    "Turnover (Rs. Cr.)": "turnover_cr", "P/E": "pe", "P/B": "pb",
    "Div Yield": "div_yield",
}
_POI_MAP = {
    "Client Type": "client_type", "Future Index Long": "fut_idx_long",
    "Future Index Short": "fut_idx_short", "Future Stock Long": "fut_stk_long",
    "Future Stock Short": "fut_stk_short", "Option Index Call Long": "opt_idx_call_long",
    "Option Index Put Long": "opt_idx_put_long", "Option Index Call Short": "opt_idx_call_short",
    "Option Index Put Short": "opt_idx_put_short", "Option Stock Call Long": "opt_stk_call_long",
    "Option Stock Put Long": "opt_stk_put_long", "Option Stock Call Short": "opt_stk_call_short",
    "Option Stock Put Short": "opt_stk_put_short", "Total Long Contracts": "total_long",
    "Total Short Contracts": "total_short",
}


def _prepare(df: pd.DataFrame, colmap: dict[str, str], d: date,
             numeric: list[str]) -> pd.DataFrame:
    """Select+rename mapped columns, coerce numerics, prepend trade_date."""
    out = df[list(colmap)].rename(columns=colmap)
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out.insert(0, "trade_date", d)
    return out


def ingest_bhavcopy(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_bhavcopy(d)
    num = ["prev_close", "open", "high", "low", "last", "close", "avg_price",
           "ttl_trd_qnty", "turnover_lacs", "no_of_trades", "deliv_qty", "deliv_per"]
    return replace_for_date(con, "equity_eod", _prepare(df, _EOD_MAP, d, num), d)


def ingest_index_closes(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_index_closes(d)
    num = ["open", "high", "low", "close", "points_change", "pct_change",
           "volume", "turnover_cr", "pe", "pb", "div_yield"]
    return replace_for_date(con, "index_close", _prepare(df, _INDEX_MAP, d, num), d)


def ingest_participant_oi(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_participant_oi(d)
    num = [c for c in _POI_MAP.values() if c != "client_type"]
    return replace_for_date(con, "participant_oi", _prepare(df, _POI_MAP, d, num), d)


def ingest_financials(symbol: str, con: duckdb.DuckDBPyConnection, *,
                      period: str = "Quarterly", max_filings: int | None = None) -> int:
    """Land structured quarterly financial line items for ``symbol`` (long format).

    Lists result filings (browser), downloads + parses each XBRL (plain HTTP),
    and stores the **current-quarter** facts (the OneD context) per filing —
    giving a clean, non-overlapping quarterly series. Annual figures are derived
    downstream by summing four quarters. Returns rows written.
    """
    filings = nse_financials.list_result_filings(symbol, period=period)
    filings = [f for f in filings if f.xbrl_url and f.to_date]
    if max_filings:
        filings = filings[:max_filings]

    rows: list[dict] = []
    for f in filings:
        try:
            parsed = nse_financials.parse_result_xbrl(fetch_bytes(f.xbrl_url))
        except (ScrapeError, ValueError):
            continue
        facts = parsed.current_quarter()      # OneD = the reported quarter
        if not facts:
            continue
        for element, value in facts.items():
            rows.append({
                "symbol": symbol, "period_end": f.to_date, "period_start": f.from_date,
                "period_type": "Q", "consolidated": f.consolidated,
                "element": element, "value": value,
                "filing_date": f.filing_date, "source_url": f.xbrl_url,
            })
    if not rows:
        return 0

    df = pd.DataFrame(rows, columns=["symbol", "period_end", "period_start",
                                     "period_type", "consolidated", "element",
                                     "value", "filing_date", "source_url"])
    con.register("_fin", df)
    try:
        con.execute("INSERT OR REPLACE INTO financials SELECT * FROM _fin")
    finally:
        con.unregister("_fin")
    return len(df)


def ingest_eod(d: date, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Ingest the full daily EOD set for trade date ``d``."""
    return {
        "equity_eod": ingest_bhavcopy(d, con),
        "index_close": ingest_index_closes(d, con),
        "participant_oi": ingest_participant_oi(d, con),
    }
