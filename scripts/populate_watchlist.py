"""One-off: resolve a list of company names, add to the watchlist, ingest
financials, and seed alert state. Prints what each name resolved to.

    set -a; . ./.env; set +a
    uv run python scripts/populate_watchlist.py
"""

from __future__ import annotations

import sys

from equity_research import watchlist
from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.reports import resolve as resolver

NAMES = [
    "adani power", "bpcl", "suzlon energy", "varun beverages", "bank of maharashtra",
    "avenue supermarkets", "groww", "tata motors commercial vehicles",
    "tata motors passenger vehicles", "adani green energy", "allcargo logistics",
    "IRFC", "tata power", "NBCC India", "vodafone idea", "ITC",
    "venus pipes and tubes", "apollo tyres", "premier explosives",
    "baid leasing and finance", "tata steel", "mercury metals", "rama steel tubes",
    "spicejet", "filatex fashions", "ajcon global solutions", "sunshine capital",
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    con = connect()
    added, unresolved = [], []
    try:
        for name in NAMES:
            cands = resolver.resolve(name)
            if not cands:
                unresolved.append(name)
                print(f"  UNRESOLVED  {name}")
                continue
            c = cands[0]                          # top-ranked match
            watchlist.add(con, c.symbol, c.name)
            has_fin = watchlist.ensure_data(con, c.symbol)
            alerts.scan_symbol(con, c.symbol, [])  # seed state silently
            added.append((name, c.symbol, has_fin))
            print(f"  {c.symbol:<14} <- {name}  (financials: {'yes' if has_fin else 'NO'})")
    finally:
        con.close()
    print(f"\nadded {len(added)}, unresolved {len(unresolved)}")
    if unresolved:
        print("unresolved:", unresolved)
    no_fin = [s for _, s, f in added if not f]
    if no_fin:
        print("no NSE financials (price/announcement alerts only):", no_fin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
