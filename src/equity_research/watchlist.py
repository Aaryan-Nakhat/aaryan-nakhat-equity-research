"""Watchlist storage + ensure-data helper."""

from __future__ import annotations

import duckdb

from equity_research.reports.pipeline import ensure_ingested


def add(con: duckdb.DuckDBPyConnection, symbol: str, company: str = "",
        list_type: str = "holding") -> None:
    """Add/update a watchlist entry. ``list_type`` is 'holding' (a stock the user owns —
    shown as *Your Holdings*) or 'tracking' (watching but not owned — *Your Tracking List*)."""
    con.execute("INSERT OR REPLACE INTO watchlist(symbol, company, added_at, list_type) "
                "VALUES (?, ?, now(), ?)", [symbol.upper(), company, list_type])


def remove(con: duckdb.DuckDBPyConnection, symbol: str) -> None:
    con.execute("DELETE FROM watchlist WHERE symbol = ?", [symbol.upper()])


def symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()]


def entries(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return [(r[0], r[1] or "") for r in
            con.execute("SELECT symbol, company FROM watchlist ORDER BY symbol").fetchall()]


def entries_by_type(con: duckdb.DuckDBPyConnection, list_type: str) -> list[tuple[str, str]]:
    """Watchlist entries in one bucket — 'holding' (Your Holdings) or 'tracking'
    (Your Tracking List). Legacy rows with a NULL type count as holdings."""
    where = ("(list_type = 'holding' OR list_type IS NULL)" if list_type == "holding"
             else "list_type = ?")
    params = [] if list_type == "holding" else [list_type]
    return [(r[0], r[1] or "") for r in con.execute(
        f"SELECT symbol, company FROM watchlist WHERE {where} ORDER BY symbol", params).fetchall()]


def ensure_data(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Ingest financials for a watchlist symbol (price history is market-wide)."""
    return ensure_ingested(symbol, con)
