"""Technical snapshot for a company from the daily EOD series.

    uv run python scripts/technical_report.py RELIANCE

Backfill price history first: scripts/backfill_eod.py.
"""

from __future__ import annotations

import sys

from equity_research.analysis import technical
from equity_research.common.db import connect


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: technical_report.py SYMBOL")
        return 2
    symbol = argv[0].upper()
    con = connect()
    try:
        s = technical.snapshot(con, symbol)
    finally:
        con.close()
    if not s:
        print(f"No price history for {symbol}. Run backfill_eod.py first.")
        return 1

    print(f"\n{symbol} — technical snapshot ({s['date']}, {s['n_days']} trading days)\n")
    print(f"  close            {s['close']:,.2f}")
    for k in ("sma20", "sma50", "sma200"):
        v = s[k]
        print(f"  {k:<16} {v:,.2f}" if v == v else f"  {k:<16} n/a")
    print(f"  rsi14            {s['rsi14']:.1f}" if s['rsi14'] == s['rsi14'] else "  rsi14            n/a")
    print(f"  atr14            {s['atr14']:,.2f}" if s['atr14'] == s['atr14'] else "")
    print(f"  delivery% (20d)  {s['deliv_per']:.1f}  (avg {s['deliv_avg20']:.1f})")
    if s['pct_from_52w_high'] == s['pct_from_52w_high']:
        print(f"  52w high/low     {s['high_52w']:,.0f} / {s['low_52w']:,.0f} "
              f"({s['pct_from_52w_high']:+.1f}% from high)")
    rs = s["rel_strength_3m_vs_nifty"]
    print(f"  RS 3m vs Nifty   {rs:.3f} ({'out' if rs and rs > 1 else 'under'}performing)"
          if rs is not None else "  RS 3m vs Nifty   n/a (no index series)")
    print("\n  signals:")
    for sg in s["signals"]:
        print(f"    - {sg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
