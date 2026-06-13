"""Ingest one trade date's EOD data into DuckDB.

    uv run python scripts/ingest_eod.py 2026-06-12
    uv run python scripts/ingest_eod.py              # defaults to the latest weekday

Re-running a date is safe — it overwrites that date's rows.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from equity_research.common.db import DEFAULT_DB_PATH, connect
from equity_research.ingest import ingest_eod


def _latest_weekday(today: date | None = None) -> date:
    d = (today or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun -> step back to Friday
        d -= timedelta(days=1)
    return d


def main(argv: list[str]) -> int:
    if argv:
        d = datetime.strptime(argv[0], "%Y-%m-%d").date()
    else:
        d = _latest_weekday()
    print(f"Ingesting EOD for {d.isoformat()} -> {DEFAULT_DB_PATH}")
    con = connect()
    try:
        counts = ingest_eod(d, con)
    finally:
        con.close()
    for table, n in counts.items():
        print(f"  {table:<16} {n:>6} rows")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
