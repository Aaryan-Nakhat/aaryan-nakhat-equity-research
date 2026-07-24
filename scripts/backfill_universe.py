"""One-time (re-runnable) bulk ingest to power the screeners.

For the Nifty-500 universe **plus** a curated list of known holding companies, land:
  - annual + quarterly **financials** (→ shares for market cap + the forensic inputs), and
  - the last few **SHP** quarters (→ holder tables for the holdco reverse-index and the
    quarter-over-quarter ownership diff).

Everything is idempotent (all writers upsert), so the script is **resumable** — re-run it
and already-ingested symbols are cheap re-checks. Best-effort per symbol: one failure never
aborts the run.

Usage:
  uv run python scripts/backfill_universe.py                 # full: Nifty-500 + known holdcos
  uv run python scripts/backfill_universe.py --holdcos-only  # just the curated holdcos (fast)
  uv run python scripts/backfill_universe.py --limit 20      # first 20 (a smoke test)
  uv run python scripts/backfill_universe.py --skip-financials   # only SHP (ownership/holdco)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from equity_research.common.db import connect  # noqa: E402
from equity_research.ingest import (  # noqa: E402
    ingest_annual_financials,
    ingest_equity_master,
    ingest_financials,
    ingest_shp_history,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("backfill-universe")

# Classic listed holding companies (their listed stakes drive the holdco-discount screen).
# Best-effort NSE symbols — an unknown one simply no-ops (ingest returns 0), never errors.
_KNOWN_HOLDCOS = [
    "BAJAJHLDNG", "MAHSCOOTER", "TATAINVEST", "ELCIDIN", "PILANIINVS", "SUMMITSEC",
    "KAMAHOLD", "ZUARIIND", "SILINV", "GANGESSECU", "WILLAMAGOR", "NALWASONS",
    "BBTC", "KICL", "BENGALASM", "JAYSREETEA", "MAGADSUGAR", "DHUNINV", "MAHLIFE",
]


def _universe(con, *, holdcos_only: bool, only_missing: bool) -> list[str]:
    holdcos = list(dict.fromkeys(_KNOWN_HOLDCOS))
    if holdcos_only:
        syms = holdcos
    else:
        n500 = [r[0] for r in con.execute(
            "SELECT symbol FROM sector_map WHERE universe = 'NIFTY500' ORDER BY symbol").fetchall()]
        seen = set(n500)                                    # Nifty-500 first, then extra holdcos
        syms = n500 + [h for h in holdcos if h not in seen]
    if only_missing:                                        # skip symbols that already have financials
        have = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM financials").fetchall()}
        syms = [s for s in syms if s not in have]
    return syms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdcos-only", action="store_true", help="just the curated holdco list")
    ap.add_argument("--only-missing", action="store_true",
                    help="skip symbols that already have financials (the fast gap-fill)")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N symbols")
    ap.add_argument("--skip-financials", action="store_true", help="SHP only (faster)")
    ap.add_argument("--skip-shp", action="store_true", help="financials only")
    ap.add_argument("--quarters", type=int, default=4, help="SHP quarters to backfill")
    args = ap.parse_args()

    con = connect()
    try:
        log.info("refreshing the listed master (EQUITY_L.csv)…")
        ingest_equity_master(con)
        syms = _universe(con, holdcos_only=args.holdcos_only, only_missing=args.only_missing)
        if args.limit:
            syms = syms[: args.limit]
        log.info("universe: %d symbols%s", len(syms),
                 " (holdcos only)" if args.holdcos_only else " (Nifty-500 + known holdcos)")

        fin_ok = shp_ok = fails = 0
        t0 = time.time()
        for i, sym in enumerate(syms, 1):
            if not args.skip_financials:
                try:
                    ingest_financials(sym, con, period="Quarterly", max_filings=12)
                    ingest_annual_financials(sym, con, max_filings=8)
                    fin_ok += 1
                except Exception:  # noqa: BLE001 — one symbol must not abort the run
                    fails += 1
                    log.exception("financials failed for %s", sym)
            if not args.skip_shp:
                try:
                    if ingest_shp_history(sym, con, quarters=args.quarters):
                        shp_ok += 1
                except Exception:  # noqa: BLE001
                    fails += 1
                    log.exception("SHP failed for %s", sym)
            if i % 10 == 0 or i == len(syms):
                rate = i / max(time.time() - t0, 1e-6)
                eta = (len(syms) - i) / rate / 60
                log.info("  %d/%d done — financials %d · shp %d · fails %d · ~%.0f min left",
                         i, len(syms), fin_ok, shp_ok, fails, eta)
        log.info("DONE — %d symbols · financials %d · shp %d · fails %d · %.1f min",
                 len(syms), fin_ok, shp_ok, fails, (time.time() - t0) / 60)
    finally:
        con.close()


if __name__ == "__main__":
    main()
