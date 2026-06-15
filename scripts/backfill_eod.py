"""Backfill daily EOD bhavcopy into equity_eod over a date range.

    uv run python scripts/backfill_eod.py 2025-01-01 2026-06-12

Idempotent — skips weekends, market holidays, and dates already ingested.
"""

from __future__ import annotations

import sys
from datetime import datetime

from equity_research.common.db import connect
from equity_research.ingest import ingest_eod_range


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: backfill_eod.py START_YYYY-MM-DD END_YYYY-MM-DD")
        return 2
    start = datetime.strptime(argv[0], "%Y-%m-%d").date()
    end = datetime.strptime(argv[1], "%Y-%m-%d").date()
    con = connect()
    try:
        summary = ingest_eod_range(start, end, con)
        n = con.execute("SELECT COUNT(DISTINCT trade_date) FROM equity_eod").fetchone()[0]
    finally:
        con.close()
    print(f"backfill {start}..{end}: {summary}")
    print(f"equity_eod now spans {n} trading days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
