"""Watchlist storage + ensure-data helper."""

from __future__ import annotations

import duckdb

from equity_research.reports.pipeline import ensure_ingested


def add(con: duckdb.DuckDBPyConnection, symbol: str, company: str = "") -> None:
    con.execute("INSERT OR REPLACE INTO watchlist(symbol, company, added_at) "
                "VALUES (?, ?, now())", [symbol.upper(), company])


def remove(con: duckdb.DuckDBPyConnection, symbol: str) -> None:
    con.execute("DELETE FROM watchlist WHERE symbol = ?", [symbol.upper()])


def symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()]


def entries(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return [(r[0], r[1] or "") for r in
            con.execute("SELECT symbol, company FROM watchlist ORDER BY symbol").fetchall()]


def ensure_data(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Ingest financials for a watchlist symbol (price history is market-wide)."""
    return ensure_ingested(symbol, con)
