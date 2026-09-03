"""One-off: resolve a list of company names, add them to the watchlist, ingest
financials, and seed alert state. Prints what each name resolved to.

Your personal portfolio is **not** hardcoded here — the names come from your
(git-ignored) ``.env`` so the code stays generic and public-safe:

    WATCHLIST_HOLDINGS=adani power, bpcl, itc, ...      # stocks you own
    WATCHLIST_TRACKING=jio financial services, srf, ... # watching, not owned

Each var is a comma-separated list of company names or NSE symbols. Then:

    set -a; . ./.env; set +a
    uv run python scripts/populate_watchlist.py
"""

from __future__ import annotations

import os
import sys

from equity_research import watchlist
from equity_research.analysis import alerts
from equity_research.common.db import connect
from equity_research.reports import resolve as resolver


def _names_from_env(var: str) -> list[str]:
    """Comma-separated company names/symbols from an env var (blank → [])."""
    return [n.strip() for n in os.environ.get(var, "").split(",") if n.strip()]


def _seed(con, names: list[str], list_type: str, added: list, unresolved: list) -> None:
    for name in names:
        cands = resolver.resolve(name)
        if not cands:
            unresolved.append(name)
            print(f"  UNRESOLVED  {name}")
            continue
        c = cands[0]                          # top-ranked match
        watchlist.add(con, c.symbol, c.name, list_type=list_type)
        has_fin = watchlist.ensure_data(con, c.symbol)
        alerts.scan_symbol(con, c.symbol, [])  # seed state silently
        added.append((name, c.symbol, has_fin))
        print(f"  {c.symbol:<14} <- {name}  ({list_type}; financials: {'yes' if has_fin else 'NO'})")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    holdings = _names_from_env("WATCHLIST_HOLDINGS")
    tracking = _names_from_env("WATCHLIST_TRACKING")
    if not holdings and not tracking:
        print("Nothing to seed. Set WATCHLIST_HOLDINGS (and optionally "
              "WATCHLIST_TRACKING) in your .env — comma-separated company names "
              "or NSE symbols — then re-run. See this script's docstring.")
        return 1

    con = connect()
    added, unresolved = [], []
    try:
        _seed(con, holdings, "holding", added, unresolved)
        _seed(con, tracking, "tracking", added, unresolved)
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
