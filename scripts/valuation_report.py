"""Valuation report — current multiples vs the company's own history.

    uv run python scripts/valuation_report.py RELIANCE [--consolidated] [--shares CR]

--shares overrides current shares outstanding (crore) to correct for a
bonus/split since the last annual filing. Needs annual financials + at least the
latest EOD price ingested.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from equity_research.analysis import valuation
from equity_research.common.db import connect


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: valuation_report.py SYMBOL [--consolidated] [--shares CR]")
        return 2
    symbol = argv[0].upper()
    consolidated = "--consolidated" in argv
    shares = None
    if "--shares" in argv:
        shares = float(argv[argv.index("--shares") + 1]) * 1e7   # crore -> count

    con = connect()
    try:
        snap = valuation.snapshot(con, symbol, consolidated, shares_override=shares)
        hist = valuation.valuation_history(con, symbol, consolidated)
    finally:
        con.close()

    if not snap:
        print(f"No valuation for {symbol} (need annual financials + a price).")
        return 1

    print(f"\n{symbol} — valuation ({'consolidated' if consolidated else 'standalone'})")
    print(f"\nCurrent (price {snap['price']} on {snap['price_date']}):")
    for k in ("shares_cr", "market_cap_cr", "pe_ttm", "pb", "earnings_yield_%"):
        print(f"  {k:<16} {snap[k]:,.2f}")
    if snap.get("note"):
        print(f"  ! {snap['note']}")

    if not hist.empty:
        pd.set_option("display.width", 150)
        print("\nP/E & P/B at each fiscal year-end (own history):")
        print(hist[["price", "pe", "pb"]].round(2).to_string())
        pe_hist = hist["pe"].dropna()
        if len(pe_hist) and snap["pe_ttm"] == snap["pe_ttm"]:
            lo, hi, med = pe_hist.min(), pe_hist.max(), float(np.median(pe_hist))
            cur = snap["pe_ttm"]
            pos = "above" if cur > hi else "below" if cur < lo else "within"
            print(f"\nCurrent P/E {cur:.1f} is {pos} its {len(pe_hist)}-yr history "
                  f"(min {lo:.1f} / median {med:.1f} / max {hi:.1f}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
