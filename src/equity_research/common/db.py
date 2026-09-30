"""DuckDB storage — connection, schema, and a date-idempotent writer.

Landing tables for scraped EOD data. Analysis (Phase 2+) reads from here.
Default DB lives under ``data/processed/`` (gitignored).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

# data/processed/equity.duckdb at the repo root (this file is src/equity_research/common/),
# unless the EQR_DB_PATH environment variable points elsewhere (a Docker volume, a test).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = (Path(os.environ["EQR_DB_PATH"]) if os.environ.get("EQR_DB_PATH")
                   else _REPO_ROOT / "data" / "processed" / "equity.duckdb")

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
        symbol         VARCHAR PRIMARY KEY,
        company        VARCHAR,
        industry       VARCHAR,        -- NSE macro-sector, e.g. 'Consumer Durables'
        basic_industry VARCHAR,        -- NSE granular basic-industry, e.g. 'Gems Jewellery And Watches'
        universe       VARCHAR         -- source index, e.g. 'NIFTY500'
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
        symbol     VARCHAR PRIMARY KEY,
        company    VARCHAR,
        added_at   TIMESTAMP DEFAULT now(),
        list_type  VARCHAR DEFAULT 'holding'   -- 'holding' (owned) | 'tracking' (watching, not owned)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS insider_trades (
        symbol           VARCHAR,
        did              VARCHAR,    -- NSE disclosure id (unique per disclosure)
        disclosure_dt    VARCHAR,    -- when filed (intimation), 'DD-Mon-YYYY HH:MM'
        trade_to_dt      VARCHAR,    -- trade 'to' date
        acq_name         VARCHAR,
        category         VARCHAR,    -- Promoter / Promoter Group / Director / Designated Person / ...
        mode             VARCHAR,    -- Market Purchase / Market Sale / Off Market / ...
        txn_type         VARCHAR,    -- Buy / Sell / Pledge / ...
        qty              DOUBLE,
        value_cr         DOUBLE,
        hold_before_pct  DOUBLE,
        hold_after_pct   DOUBLE,
        regulation       VARCHAR,
        updated_at       TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol, did)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shareholding (
        symbol                   VARCHAR,
        period_end               DATE,      -- shareholding-pattern 'as of' date
        promoter_holding_pct     DOUBLE,
        pledged_pct_of_promoter  DOUBLE,    -- pledged shares / promoter holding
        pledged_pct_of_total     DOUBLE,    -- pledged shares / total issued
        num_shares_pledged       DOUBLE,
        broadcast_dt             VARCHAR,
        source_url               VARCHAR,
        updated_at               TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol, period_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shp_holders (
        symbol          VARCHAR,
        as_of           DATE,      -- SHP quarter-end
        holder_name     VARCHAR,
        pct             DOUBLE,    -- % of total shares
        shares          BIGINT,
        category        VARCHAR,   -- individual/HUF · mutual fund · FPI · body corporate …
        is_promoter     BOOLEAN,   -- Table II (promoter/promoter group) vs public >1%
        classification  VARCHAR,   -- LISTED company · unlisted pvt company · trust · …
        matched_symbol  VARCHAR,   -- NSE symbol when the holder itself is listed (Elcid pattern)
        updated_at      TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol, as_of, holder_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_master (
        symbol        VARCHAR,     -- every NSE-listed company (EQUITY_L.csv)
        company_name  VARCHAR,
        isin          VARCHAR,
        updated_at    TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_scheme (
        scheme_code   INTEGER,          -- AMFI scheme code (unique per plan/option)
        isin_growth   VARCHAR,          -- ISIN (Div Payout / Growth)
        isin_reinvest VARCHAR,          -- ISIN (Div Reinvestment)
        scheme_name   VARCHAR,
        amc           VARCHAR,          -- fund house (e.g. 'Axis Mutual Fund')
        category      VARCHAR,          -- AMFI category header (e.g. 'Equity Scheme - Multi Cap Fund')
        asset_class   VARCHAR,          -- coarse: Equity | Debt | Hybrid | Solution | Other
        plan          VARCHAR,          -- Direct | Regular (parsed from name)
        option        VARCHAR,          -- Growth | IDCW (parsed from name)
        updated_at    TIMESTAMP DEFAULT now(),
        PRIMARY KEY (scheme_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_nav (
        scheme_code  INTEGER,
        nav_date     DATE,
        nav          DOUBLE,
        PRIMARY KEY (scheme_code, nav_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_holdings (
        scheme_code  INTEGER,          -- AMFI scheme (the Direct-Growth share class we report on)
        as_of        DATE,             -- portfolio 'as of' month-end
        isin         VARCHAR,          -- holding's ISIN (stock/bond); '' for non-ISIN lines
        instrument   VARCHAR,          -- holding name as disclosed
        industry     VARCHAR,          -- industry / rating as disclosed
        quantity     DOUBLE,
        market_value_cr DOUBLE,        -- market/fair value, ₹ crore
        pct_nav      DOUBLE,           -- % to net assets
        source_url   VARCHAR,
        updated_at   TIMESTAMP DEFAULT now(),
        PRIMARY KEY (scheme_code, as_of, isin, instrument)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mf_amc (
        amc_code    INTEGER,          -- AMFI numeric fund-house id (history report 'mf' param)
        amc_name    VARCHAR,          -- as it appears in NAVAll / mf_scheme.amc
        updated_at  TIMESTAMP DEFAULT now(),
        PRIMARY KEY (amc_code)
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
    """
    CREATE TABLE IF NOT EXISTS alert_keywords (
        keyword    VARCHAR,        -- lowercased phrase to watch for in exchange announcements
        added_at   TIMESTAMP DEFAULT now(),
        PRIMARY KEY (keyword)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS concall_signals (
        symbol        VARCHAR,
        filed_date    DATE,       -- when the transcript was filed with the exchange
        quarter       VARCHAR,    -- e.g. 'Q1 FY27' (best-effort label from the filing)
        tone          VARCHAR,    -- Management Tone from the words: Very Confident … Defensive (5-band)
        execution     VARCHAR,    -- Execution from OUR numbers: Firing … Struggling (5-band)
        gap           VARCHAR,    -- say-do gap: Talk > Numbers / Aligned / Numbers > Talk
        signal_score  DOUBLE,     -- 0-100, how notable the call is (drives ranking)
        summary_md    VARCHAR,    -- 3-5 takeaway bullets (markdown)
        guidance_json VARCHAR,    -- forward guidance JSON (or null), reused from extract_guidance
        source_url    VARCHAR,    -- the transcript PDF
        model         VARCHAR,    -- the LLM model string that scored it
        updated_at    TIMESTAMP DEFAULT now(),
        PRIMARY KEY (symbol, filed_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_adjustments (
        symbol    VARCHAR,
        ex_date   DATE,        -- first session at the new share count
        factor    DOUBLE,      -- multiply prices BEFORE ex_date by this (0.2 for a 1:5 split,
                               -- 0.5 for a 1:1 bonus); NULL = recorded but not applied
        share_mult DOUBLE,     -- shares after ÷ shares before (5 for a 1:5 split, 2 for 1:1 rights
                               -- if fully taken up, 1 for a demerger); NULL = unknown
        kind      VARCHAR,     -- split | bonus | consolidation | rights | demerger | unexplained
        source    VARCHAR,     -- nse (the exchange's corporate-action record) | price-gap (detected)
        detail    VARCHAR,     -- the NSE subject, or the observed opening gap
        PRIMARY KEY (symbol, ex_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS calls (
        call_id     VARCHAR,     -- hash of (source, symbol, call date): one call per engine/name/day
        made_at     TIMESTAMP,   -- when the call went out (UTC) — the only clock scoring trusts
        source      VARCHAR,     -- deep_report · tailwind · pickaxe · hotlist · screen:value · calls · …
        symbol      VARCHAR,
        stance      VARCHAR,     -- long · avoid · hold · review (verdict unreadable) · unknown
        label       VARCHAR,     -- the call as made: BUY / ACCUMULATE / HOLD / REDUCE / AVOID / PICK
        rank        INTEGER,     -- position in an engine's list (1 = top); NULL for a single verdict
        context     VARCHAR,     -- one line of why (theme, catalyst, screen) — for the reader only
        provenance  VARCHAR,     -- live (logged as it went out) · recovered (backfilled from a sent report)
        ref         VARCHAR,     -- where it came from (email subject, saved report path)
        PRIMARY KEY (call_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS theses (
        symbol        VARCHAR,     -- one thesis per company (a new one replaces the old)
        name          VARCHAR,
        created_at    TIMESTAMP,   -- when you wrote it (a trailing stop counts its peak from here)
        text          VARCHAR,     -- your words, as written
        checks_json   VARCHAR,     -- the checks and price rules they became (analysis/thesis_guard.py)
        last_json     VARCHAR,     -- statuses at the last check, to spot what changed
        last_checked  TIMESTAMP,
        active        BOOLEAN,
        PRIMARY KEY (symbol)
    )
    """,
    # equity_eod with every split / bonus / consolidation applied, so a price series is continuous
    # across the action: prices before an ex-date are scaled by the product of the factors of every
    # later action, volumes by its inverse. prev_close belongs to the session before its row, so it
    # takes the factor of the segment ending ON the row's date. Actions dated after the last session
    # on file (announced, not yet effective) are ignored. Readers that pair an old price with that
    # date's own share count / EPS (valuation history) keep using the raw equity_eod.
    """
    CREATE OR REPLACE VIEW equity_eod_adj AS
    WITH a AS (
        SELECT symbol, ex_date, factor FROM price_adjustments
        WHERE factor IS NOT NULL AND factor > 0
          AND ex_date <= (SELECT max(trade_date) FROM equity_eod)
    ), seg AS (
        SELECT symbol,
               lag(ex_date) OVER (PARTITION BY symbol ORDER BY ex_date) AS lo,
               ex_date AS hi,
               exp(sum(ln(factor)) OVER (PARTITION BY symbol ORDER BY ex_date DESC
                                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS cf
        FROM a
    )
    SELECT e.trade_date, e.symbol, e.series,
           e.prev_close * coalesce(sp.cf, 1) AS prev_close,
           e.open * coalesce(s.cf, 1) AS open,
           e.high * coalesce(s.cf, 1) AS high,
           e.low * coalesce(s.cf, 1) AS low,
           e.last * coalesce(s.cf, 1) AS last,
           e.close * coalesce(s.cf, 1) AS close,
           e.avg_price * coalesce(s.cf, 1) AS avg_price,
           CAST(round(e.ttl_trd_qnty / coalesce(s.cf, 1)) AS BIGINT) AS ttl_trd_qnty,
           e.turnover_lacs, e.no_of_trades,
           CAST(round(e.deliv_qty / coalesce(s.cf, 1)) AS BIGINT) AS deliv_qty,
           e.deliv_per
    FROM equity_eod e
    LEFT JOIN seg s ON s.symbol = e.symbol AND e.trade_date < s.hi
                   AND (s.lo IS NULL OR e.trade_date >= s.lo)
    LEFT JOIN seg sp ON sp.symbol = e.symbol AND e.trade_date <= sp.hi
                    AND (sp.lo IS NULL OR e.trade_date > sp.lo)
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
