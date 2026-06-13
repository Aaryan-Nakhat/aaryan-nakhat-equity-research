"""Print a company's fundamental ratios from landed financials.

    uv run python scripts/fundamentals_report.py RELIANCE
    uv run python scripts/fundamentals_report.py RELIANCE --consolidated

Ingest first if needed:
    uv run python -c "from equity_research.common.db import connect; \
        from equity_research.ingest import ingest_financials; \
        ingest_financials('RELIANCE', connect())"
"""

from __future__ import annotations

import sys

import pandas as pd

from equity_research.analysis import fundamentals
from equity_research.common.db import connect


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: fundamentals_report.py SYMBOL [--consolidated]")
        return 2
    symbol = argv[0].upper()
    consolidated = "--consolidated" in argv

    con = connect()
    try:
        m = fundamentals.quarterly_metrics(con, symbol, consolidated)
        t = fundamentals.ttm(con, symbol, consolidated)
    finally:
        con.close()

    label = "consolidated" if consolidated else "standalone"
    if m.empty:
        print(f"No financials for {symbol} ({label}). Run ingest_financials first.")
        return 1

    pd.set_option("display.width", 140, "display.max_columns", 20)
    print(f"\n{symbol} — quarterly fundamentals ({label})\n")
    print(m.tail(6).round(2).to_string())
    print(f"\nTTM ({label}):")
    for k, v in t.items():
        print(f"  {k:<22} {v:>12,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
