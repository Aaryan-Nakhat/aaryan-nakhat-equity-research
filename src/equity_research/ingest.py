"""Ingest — scrape a trade date's EOD data and land it in DuckDB.

Each function fetches via ``scrapers``, renames columns to the schema in
``common.db``, and writes idempotently (re-running a date overwrites it).
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from equity_research.common.db import replace_for_date
from equity_research.scrapers import nse_archives

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


def ingest_eod(d: date, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Ingest the full daily EOD set for trade date ``d``."""
    return {
        "equity_eod": ingest_bhavcopy(d, con),
        "index_close": ingest_index_closes(d, con),
        "participant_oi": ingest_participant_oi(d, con),
    }
