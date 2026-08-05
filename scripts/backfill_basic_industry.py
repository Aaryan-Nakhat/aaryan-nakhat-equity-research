"""Backfill NSE granular ``basic_industry`` onto ``sector_map`` (one-time / occasional).

The index-constituent CSVs carry only NSE's macro-sector (e.g. 'Consumer Durables'), which
lumps jewellers with paints, ACs and footwear — so peer tables / sector percentiles / z-scores
compared unrelated names. NSE's per-symbol getSymbolData exposes the finer ``basicIndustry``
(e.g. 'Gems Jewellery And Watches'); this pulls it for every ``sector_map`` symbol still
missing a tag and stores it, so peer grouping compares like with like.

Idempotent & resumable: only fetches symbols where ``basic_industry`` is NULL/'' — safe to
re-run after a partial pass (anti-bot rate-limits can drop a session). Browser-tier and single
-writer: **stop the email bot first** (the DuckDB is single-writer).

    uv run python scripts/backfill_basic_industry.py            # all still-missing symbols
    uv run python scripts/backfill_basic_industry.py --macro "Consumer Durables"  # one bucket
    uv run python scripts/backfill_basic_industry.py --all      # re-fetch even already-tagged
"""

from __future__ import annotations

import argparse
import logging

from equity_research.common.db import connect
from equity_research.ingest import ingest_basic_industries

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")


def _ensure_column(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info('sector_map')").fetchall()}
    if "basic_industry" not in cols:
        con.execute("ALTER TABLE sector_map ADD COLUMN basic_industry VARCHAR")
        logging.info("added sector_map.basic_industry column")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill sector_map.basic_industry from NSE")
    ap.add_argument("--macro", help="only symbols in this macro industry (e.g. 'Consumer Durables')")
    ap.add_argument("--all", action="store_true", help="re-fetch even symbols already tagged")
    ap.add_argument("--batch", type=int, default=30, help="symbols per browser session")
    ap.add_argument("--cooldown", type=float, default=4.0, help="seconds between sessions")
    args = ap.parse_args()

    con = connect()
    _ensure_column(con)

    symbols = None
    if args.macro:
        q = "SELECT symbol FROM sector_map WHERE industry = ?"
        if not args.all:
            q += " AND (basic_industry IS NULL OR basic_industry = '')"
        symbols = [r[0] for r in con.execute(q + " ORDER BY symbol", [args.macro]).fetchall()]
        logging.info("macro '%s': %d symbols to fetch", args.macro, len(symbols))

    res = ingest_basic_industries(con, symbols, batch=args.batch,
                                  only_missing=not args.all, cooldown_s=args.cooldown)
    logging.info("done: %s", res)

    tagged = con.execute(
        "SELECT count(*) FROM sector_map WHERE basic_industry IS NOT NULL AND basic_industry <> ''"
    ).fetchone()[0]
    total = con.execute("SELECT count(*) FROM sector_map").fetchone()[0]
    logging.info("sector_map now: %d/%d tagged with basic_industry", tagged, total)
    con.close()


if __name__ == "__main__":
    main()
