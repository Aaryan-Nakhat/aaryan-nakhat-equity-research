"""One-off: run the watchlist scan now and email the digest, labelled with the
latest available trade date. Does NOT set the daily 'already scanned' marker, so
the normal evening heartbeat still fires its own scan. Used to backfill a digest
for a day the laptop was off."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from equity_research import scan  # noqa: E402
from equity_research.common.db import connect  # noqa: E402
from equity_research.reports import email as emailer  # noqa: E402


def main() -> None:
    sr = scan.run_watchlist_scan()
    con = connect()
    try:
        d = con.execute("SELECT max(trade_date) FROM equity_eod").fetchone()[0]
    finally:
        con.close()
    date_str = d.isoformat() if d else "latest"
    to = os.environ.get("REPORT_TO")
    if not to:
        allowed = [a.strip() for a in os.environ.get("EMAIL_ALLOWED_SENDERS", "").split(",") if a.strip()]
        to = min(allowed) if allowed else None
    if not to:
        print("no REPORT_TO / allowlist set", file=sys.stderr)
        sys.exit(1)
    md = scan.format_digest(date_str, sr)
    emailer.send_report(f"📊 Watchlist — {date_str} (backfill)", md, to=to,
                        html=emailer.body_html(md, "Watchlist"))
    print(f"digest sent to {to} for {date_str}: "
          f"{len(sr.movers)} movers, {len(sr.results)} event-symbols, {len(sr.upcoming)} upcoming")


if __name__ == "__main__":
    main()
