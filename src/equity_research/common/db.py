"""DuckDB storage — connection, schema, and a date-idempotent writer.

Landing tables for scraped EOD data. Analysis (Phase 2+) reads from here.
Default DB lives under ``data/processed/`` (gitignored).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

# data/processed/equity.duckdb at the repo root (this file is src/equity_research/common/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "processed" / "equity.duckdb"

# One CREATE per landing table. Column order here is the contract ingest writes to.
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS equity_eod (
        trade_date    DATE,
        symbol        VARCHAR,
        series        VARCHAR,
        prev_close    DOUBLE,
        open          DOUBLE,
        high          DOUBLE,
        low           DOUBLE,
        last          DOUBLE,
        close         DOUBLE,
        avg_price     DOUBLE,
        ttl_trd_qnty  BIGINT,
        turnover_lacs DOUBLE,
        no_of_trades  BIGINT,
        deliv_qty     BIGINT,
        deliv_per     DOUBLE,
        PRIMARY KEY (trade_date, symbol, series)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_close (
        trade_date    DATE,
        index_name    VARCHAR,
        open          DOUBLE,
        high          DOUBLE,
        low           DOUBLE,
        close         DOUBLE,
        points_change DOUBLE,
        pct_change    DOUBLE,
        volume        DOUBLE,
        turnover_cr   DOUBLE,
        pe            DOUBLE,
        pb            DOUBLE,
        div_yield     DOUBLE,
        PRIMARY KEY (trade_date, index_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financials (
        symbol        VARCHAR,
        period_end    DATE,
        period_start  DATE,
        period_type   VARCHAR,   -- 'Q' (quarter), 'Y' (full year), 'YTD'
        consolidated  BOOLEAN,
        element       VARCHAR,   -- in-bse-fin tag local name (e.g. ProfitLossForPeriod)
        value         DOUBLE,
        filing_date   DATE,
        source_url    VARCHAR,
        PRIMARY KEY (symbol, period_end, consolidated, period_type, element)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sector_map (
        symbol     VARCHAR PRIMARY KEY,
        company    VARCHAR,
        industry   VARCHAR,
        universe   VARCHAR        -- source index, e.g. 'NIFTY500'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participant_oi (
        trade_date          DATE,
        client_type         VARCHAR,
        fut_idx_long        BIGINT,
        fut_idx_short       BIGINT,
        fut_stk_long        BIGINT,
        fut_stk_short       BIGINT,
        opt_idx_call_long   BIGINT,
        opt_idx_put_long    BIGINT,
        opt_idx_call_short  BIGINT,
        opt_idx_put_short   BIGINT,
        opt_stk_call_long   BIGINT,
        opt_stk_put_long    BIGINT,
        opt_stk_call_short  BIGINT,
        opt_stk_put_short   BIGINT,
        total_long          BIGINT,
        total_short         BIGINT,
        PRIMARY KEY (trade_date, client_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol    VARCHAR PRIMARY KEY,
        company   VARCHAR,
        added_at  TIMESTAMP DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_state (
        symbol      VARCHAR,
        key         VARCHAR,
        value       VARCHAR,
        updated_at  TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol, key)
    )
    """,
]


def connect(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open (creating dirs + schema as needed) the DuckDB database."""
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    ensure_schema(con)
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in _SCHEMA:
        con.execute(ddl)


def replace_for_date(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame,
                     d: date) -> int:
    """Idempotently write ``df`` for trade date ``d``: delete that date, re-insert.

    ``df`` must already carry a ``trade_date`` column and match the table's column
    order. Returns the row count written.
    """
    con.register("_incoming", df)
    try:
        con.execute(f"DELETE FROM {table} WHERE trade_date = ?", [d])
        con.execute(f"INSERT INTO {table} SELECT * FROM _incoming")
    finally:
        con.unregister("_incoming")
    return len(df)
