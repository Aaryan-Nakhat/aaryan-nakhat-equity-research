"""Derivatives positioning from the daily participant-wise OI (`participant_oi`).

NSE publishes Client / DII / FII / Pro long & short open interest each evening. The
most-watched read is the **FII index-futures net-long %** — a market-sentiment gauge
(low = FIIs defensive/short; high = aggressive/long) — plus the FII-vs-retail
(Client) divergence that often marks turning points. Pure read of data we already
ingest daily; never raises.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb


def _net_long_pct(long_, short_):
    tot = (long_ or 0) + (short_ or 0)
    return 100 * long_ / tot if tot else None


def fii_index_futures(con: duckdb.DuckDBPyConnection) -> dict:
    """FII index-futures net-long % at the latest session and ~1 week earlier, plus
    retail (Client) net-long for contrast. ``{}`` if no participant-OI data."""

    def row(client_type: str, on_or_before=None):
        if on_or_before is None:
            return con.execute(
                "SELECT trade_date, fut_idx_long, fut_idx_short FROM participant_oi "
                "WHERE client_type = ? ORDER BY trade_date DESC LIMIT 1", [client_type]).fetchone()
        return con.execute(
            "SELECT trade_date, fut_idx_long, fut_idx_short FROM participant_oi "
            "WHERE client_type = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
            [client_type, on_or_before]).fetchone()

    fii = row("FII")
    if not fii:
        return {}
    d, lo, sh = fii
    prev = row("FII", d - timedelta(days=7))
    cli = row("Client")
    return {
        "date": d,
        "net_long_pct": _net_long_pct(lo, sh),
        "long": lo, "short": sh,
        "prev_net_long_pct": _net_long_pct(prev[1], prev[2]) if prev and prev[0] != d else None,
        "retail_net_long_pct": _net_long_pct(cli[1], cli[2]) if cli else None,
    }
