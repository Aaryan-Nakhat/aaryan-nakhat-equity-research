"""Print forensic / quality scores for a company.

    uv run python scripts/forensic_report.py RELIANCE [--consolidated] [--mcap CR]

Ingest annual financials first if needed (ingest_annual_financials).
"""

from __future__ import annotations

import sys

from equity_research.analysis import forensic
from equity_research.common.db import connect

_ALTMAN = "Z>2.99 safe | 1.81-2.99 grey | <1.81 distress"
_PIOTROSKI = "8-9 strong | 0-2 weak"
_BENEISH = "M>-1.78 => possible manipulation"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: forensic_report.py SYMBOL [--consolidated] [--mcap CR]")
        return 2
    symbol = argv[0].upper()
    consolidated = "--consolidated" in argv
    mcap = None
    if "--mcap" in argv:
        mcap = float(argv[argv.index("--mcap") + 1]) * 1e7   # crore -> rupees

    con = connect()
    try:
        scores = [
            forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap),
            forensic.piotroski_f(con, symbol, consolidated=consolidated),
            forensic.beneish_m(con, symbol, consolidated=consolidated),
        ]
    finally:
        con.close()

    bands = {"Altman Z": _ALTMAN, "Piotroski F": _PIOTROSKI, "Beneish M": _BENEISH}
    print(f"\n{symbol} — forensic scores ({'consolidated' if consolidated else 'standalone'})")
    for s in scores:
        val = "n/a" if s.value is None else f"{s.value:.3f}"
        print(f"\n{s.name}: {val}    [{bands[s.name]}]")
        if s.note:
            print(f"  note: {s.note}")
        if s.missing:
            print(f"  missing inputs: {s.missing}")
        for k, v in s.components.items():
            print(f"    {k:<22} {v: .4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
