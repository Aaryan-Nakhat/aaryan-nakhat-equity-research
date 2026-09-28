"""``eqr demo`` — fetch a starter set so a first-time user can explore in ~12 minutes.

Everything is downloaded **by the user's own machine** from the public sources (nothing is
redistributed): the listed-company list, sector map and ~13 months of daily prices + index
closes come from NSE's plain-HTTP archives (~2 min); financial statements and shareholding for a
dozen well-known companies come through the NSE browser tier (~45 s each), which needs the NSE
opt-in — asked once, for this run, if it isn't already on in ``.env``.

The dozen are picked to show every report shape: a services name, a conglomerate, banks, a life
insurer, an asset manager, an NBFC, capital goods, defence, consumer names and a mid-cap — and
all three HDFC companies, so typing ``hdfc`` shows a real pick list. Idempotent: re-running
skips what's already there. Then it starts the web UI and opens the browser.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from datetime import date, timedelta

DEMO_SYMBOLS = ("INFY", "RELIANCE", "HDFCBANK", "HDFCLIFE", "HDFCAMC", "SBIN", "BAJFINANCE",
                "LT", "BEL", "TITAN", "ITC", "DIXON")
PRICE_DAYS = 400              # ~13 months: enough for the 200-day average and the 52-week range
NSE_NOTICE = (
    "Financial statements and shareholding come from NSE's website. NSE's terms restrict\n"
    "automated access and forbid redistributing its data. This fetches a small set for your\n"
    "own research, on this machine only (the same as `NSE_SCRAPING_ENABLED=true` in .env).")


def _nse_on() -> bool:
    return os.environ.get("NSE_SCRAPING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _clock(t0: float) -> str:
    s = int(time.monotonic() - t0)
    return f"{s // 60}m {s % 60:02d}s"


def _step(n: int, total: int, label: str) -> None:
    print(f" {n}/{total} {label:<26}", end="", flush=True)


def _prices(con, days: int) -> int:
    """Daily bhavcopy for the last ``days`` calendar days (skips what's already stored)."""
    from equity_research.common.http import ScrapeError
    from equity_research.ingest import ingest_bhavcopy

    end, start = date.today(), date.today() - timedelta(days=days)
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM equity_eod WHERE trade_date >= ?", [start]).fetchall()}
    todo = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in todo if d.weekday() < 5 and d not in have]
    got = 0
    for i, d in enumerate(todo, 1):
        try:
            ingest_bhavcopy(d, con)
            got += 1
        except ScrapeError:
            pass                                   # a holiday — no file
        if i % 20 == 0:
            print(f"\r {'':<30}{i}/{len(todo)} days", end="", flush=True)
    return got


def build(*, assume_yes: bool = False, out=print) -> bool:
    """Fetch the starter set into the local database. Returns False if the user declines the NSE
    step (the price data is still fetched — technicals and screens work on it)."""
    from equity_research import ingest as ing
    from equity_research.common.db import connect

    t0 = time.monotonic()
    out("eqr demo — fetching a starter set to explore (≈12 min, one time)\n")
    with_nse = _nse_on()
    if not with_nse:
        out(NSE_NOTICE)
        ok = assume_yes or input("\nFetch the company data for this run? [y/N] ").strip().lower() in ("y", "yes")
        if ok:
            os.environ["NSE_SCRAPING_ENABLED"] = "true"        # this process only
            with_nse = True
        else:
            out("OK — skipping company data. Prices, technicals and screens will still work.\n")
    con = connect()
    try:
        _step(1, 5, "Company list")
        ing.ingest_equity_master(con)
        n = con.execute("SELECT count(*) FROM equity_master").fetchone()[0]
        print(f"✓ {n:,} listed names")

        _step(2, 5, "Sectors (Nifty 500)")
        try:
            ing.ingest_sector_map(con)
            print("✓")
        except Exception as e:  # noqa: BLE001 — cosmetic for the demo; keep going
            print(f"– skipped ({e.__class__.__name__})")

        _step(3, 5, "Daily prices, ~13 months")
        t = time.monotonic()
        got = _prices(con, PRICE_DAYS)
        print(f"\r {3}/5 {'Daily prices, ~13 months':<26}✓ {got} new trading days ({_clock(t)})")
        from equity_research.analysis import corporate_actions
        try:                                  # splits / bonuses / demergers → continuous prices
            corporate_actions.refresh(con)
        except Exception:  # noqa: BLE001 — charts still work, just unadjusted across an action
            pass

        _step(4, 5, "Index history")
        stats = ing.backfill_index_history(con, years=PRICE_DAYS / 365, progress_every=0)
        print(f"✓ {stats['ingested']} new days")

        _step(5, 5, "Financials + ownership")
        if not with_nse:
            print("– skipped (NSE step declined)")
        else:
            print()
            have = {r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM financials WHERE period_type = 'Y'").fetchall()}
            for i, sym in enumerate(DEMO_SYMBOLS, 1):
                if sym in have:
                    out(f"     [{i:>2}/{len(DEMO_SYMBOLS)}] {sym:<11} ✓ already here")
                    continue
                t = time.monotonic()
                try:
                    ing.ingest_financials(sym, con)
                    ing.ingest_annual_financials(sym, con)
                    try:
                        ing.ingest_shp_history(sym, con, quarters=2)
                    except Exception:  # noqa: BLE001 — ownership is a bonus section
                        pass
                    out(f"     [{i:>2}/{len(DEMO_SYMBOLS)}] {sym:<11} ✓ ({_clock(t)})")
                except Exception as e:  # noqa: BLE001 — one company never stops the demo
                    out(f"     [{i:>2}/{len(DEMO_SYMBOLS)}] {sym:<11} ✗ {str(e).splitlines()[0][:70]}")
    finally:
        con.close()
    out(f"\nDone in {_clock(t0)}.")
    return with_nse


def run(args: list[str]) -> int:
    """`eqr demo [--yes] [--no-serve]` — build the starter set, then start the web UI."""
    from equity_research import cli, config

    if cli._server_base():
        print("A server is running and owns the database — stop it (or the email bot) first, "
              "then run `eqr demo` again.", file=sys.stderr)
        return 1
    cli._setup_logging()              # library fetch logs → data/processed/cli.log, not the screen
    build(assume_yes="--yes" in args)
    print("\nTry: " + ", ".join(("infosys", "hdfc", "hdfc life", "sbin", "sector: defence",
                                 "screen: technical")))
    if "--no-serve" in args:
        print("Start the web UI any time with `eqr serve`.")
        return 0
    url = f"http://{cli._local_host()}:{config.WEB_PORT}"
    threading.Timer(3.0, webbrowser.open, args=(url,)).start()
    return cli._serve()
