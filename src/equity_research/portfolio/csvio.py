"""``holdings.csv`` — your buys (and sells) in a file instead of the web UI. Gitignored; ``HOLDINGS_CSV`` moves it.

Columns ``symbol, qty, price, date`` (headers matched loosely — a broker's holdings export works as-is), plus
optional ``side`` (``buy`` / ``sell``; blank = buy), ``type`` (``buyback`` for shares tendered in one) and
``received``. Re-read whenever the file changes; its rows replace the previous import, UI entries stay.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path

import duckdb

from equity_research.common.db import _REPO_ROOT
from equity_research.portfolio import store

log = logging.getLogger(__name__)
_HEAD = {"symbol": ("symbol", "stock", "instrument", "tradingsymbol", "scrip", "company", "name", "stockname"),
         "qty": ("qty", "quantity", "shares", "units", "quantityavailable"),
         "price": ("price", "buyprice", "avgcost", "averagecost", "avgprice", "averageprice", "cost", "buyavg",
                   "averagebuyprice", "tradeprice"),
         "date": ("date", "buydate", "purchasedate", "tradedate", "boughton", "selldate"),
         "side": ("side", "tradetype", "buysell", "action", "transactiontype"),
         "type": ("type", "kind"),
         "received": ("received", "receivedon", "receiveddate", "allotmentdate")}


def csv_path() -> Path:
    return Path(os.environ.get("HOLDINGS_CSV") or _REPO_ROOT / "holdings.csv")


def read_csv(path: Path) -> list[dict]:
    """Rows of ``{symbol, qty, price, date, side, type, received, line}`` (``line`` = the file's line number)."""
    text = path.read_text(encoding="utf-8-sig")
    rows = [(n, r) for n, r in enumerate(csv.reader(text.splitlines()), 1)
            if any(c.strip() for c in r) and not r[0].lstrip().startswith("#")]
    if not rows:
        return []
    norm = [re.sub(r"[^a-z]", "", h.lower()) for h in rows[0][1]]
    idx = {k: next((i for i, h in enumerate(norm) if h in names), None) for k, names in _HEAD.items()}
    missing = [k for k in ("symbol", "qty", "price") if idx[k] is None]
    if missing:
        raise ValueError(f"holdings.csv needs columns symbol, qty, price (and optionally date) — missing: "
                         f"{', '.join(missing)}")
    out = []
    for n, r in rows[1:]:
        def cell(k, r=r):
            i = idx[k]
            return r[i].strip() if i is not None and i < len(r) else ""
        out.append({k: cell(k) for k in _HEAD} | {"line": n})
    return out


def sync_csv(con: duckdb.DuckDBPyConnection, *, force: bool = False) -> dict:
    """Re-import ``holdings.csv`` if it changed since the last import (or ``force``). Returns
    {status, imported, errors, path}."""
    from equity_research import scan

    path = csv_path()
    out = {"path": str(path), "imported": 0, "errors": [], "status": "none"}
    if not path.is_file():
        if scan._meta(con, "holdings_csv_mtime"):          # the file was deleted → drop what it brought
            con.execute("DELETE FROM holding_lots WHERE source = 'csv'")
            con.execute("DELETE FROM holding_sells WHERE source = 'csv'")
            scan._set_meta(con, "holdings_csv_mtime", "")
        return out
    stamp = str(path.stat().st_mtime_ns)
    if not force and scan._meta(con, "holdings_csv_mtime") == stamp:
        out["status"] = "unchanged"
        out["errors"] = [e for e in (scan._meta(con, "holdings_csv_errors") or "").split("\n") if e]
        return out
    try:
        rows = read_csv(path)
    except (OSError, ValueError, csv.Error) as e:
        out.update(status="error", errors=[str(e)])
        return out
    con.execute("DELETE FROM holding_lots WHERE source = 'csv'")
    con.execute("DELETE FROM holding_sells WHERE source = 'csv'")
    for r in rows:
        try:
            if r["side"].lower().startswith("s"):
                store.add_sell(con, r["symbol"], r["qty"], r["price"], r["date"] or None, source="csv",
                               kind="buyback" if "buyback" in r["type"].lower() else "sell")
            else:
                store.add_lot(con, r["symbol"], r["qty"], r["price"], r["date"] or None, source="csv",
                              received=r["received"] or None)
            out["imported"] += 1
        except (ValueError, TypeError) as e:
            out["errors"].append(f"line {r['line']} ({r['symbol'] or 'blank'}): {e}")
    scan._set_meta(con, "holdings_csv_mtime", stamp)
    scan._set_meta(con, "holdings_csv_errors", "\n".join(out["errors"]))
    out["status"] = "imported"
    log.info("holdings.csv: %d row(s) imported, %d problem(s)", out["imported"], len(out["errors"]))
    return out
