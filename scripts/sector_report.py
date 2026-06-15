"""Valuation vs sector peers.

    uv run python scripts/sector_report.py RELIANCE [--consolidated] [--shares CR]

Needs `ingest_sector_map` + peers' financials ingested. --shares overrides the
target's current shares (bonus/split correction).
"""

from __future__ import annotations

import sys

import pandas as pd

from equity_research.analysis import sector
from equity_research.common.db import connect


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: sector_report.py SYMBOL [--consolidated] [--shares CR]")
        return 2
    symbol = argv[0].upper()
    consolidated = "--consolidated" in argv
    override = None
    if "--shares" in argv:
        override = float(argv[argv.index("--shares") + 1]) * 1e7

    con = connect()
    try:
        r = sector.sector_valuation(con, symbol, consolidated,
                                    target_shares_override=override)
    finally:
        con.close()

    if r.get("note"):
        print(r["note"])
        if "table" not in r:
            return 1
    print(f"\n{symbol} — valuation vs sector: {r['industry']}")
    print(f"  peers with data : {r['peers_with_data']}")
    print(f"  P/E  {r['target_pe']:.1f}  vs sector median {r['sector_median_pe']:.1f}"
          f"  (cheaper than {r['pe_cheaper_than_%_of_peers']:.0f}% of peers)")
    if r["target_pb"] == r["target_pb"]:
        print(f"  P/B  {r['target_pb']:.2f}  vs sector median {r['sector_median_pb']:.2f}"
              f"  (cheaper than {r['pb_cheaper_than_%_of_peers']:.0f}% of peers)")
    pd.set_option("display.width", 120)
    print("\n  peer table (by P/E):")
    print(r["table"].round(2).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
