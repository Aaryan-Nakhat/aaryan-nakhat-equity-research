"""💼 Your holdings with quantity, buy price and (optionally) buy date — kept on this machine only.

Two ways in:

* the web UI's **My holdings** table (add / edit / delete a lot), and
* a ``holdings.csv`` in the repo folder (``HOLDINGS_CSV`` to move it) — columns ``symbol, qty, price, date``;
  broker-style headers (``Instrument, Qty., Avg. cost``) work too. It's re-read whenever the file changes, and
  its rows replace the previous import (lots added in the UI are left alone). The file is gitignored.

A **lot** is one buy: one stock can have several, each with or without a date. Every lot gets value and
profit / loss on the latest close; a lot **with a date** also gets: split / bonus adjustment since the buy,
short- vs long-term (held more than 12 months), the yearly return (once held a year) and the Nifty 500 over
the same days. A lot without a date gets profit / loss only — nothing is guessed.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path

import duckdb

from equity_research.common.db import _REPO_ROOT

log = logging.getLogger(__name__)
BENCHMARK = "Nifty 500"
LONG_TERM_DAYS = 365        # held MORE than 12 months → long-term

_HEAD = {"symbol": ("symbol", "stock", "instrument", "tradingsymbol", "scrip", "company", "name", "stockname"),
         "qty": ("qty", "quantity", "shares", "units", "quantityavailable"),
         "price": ("price", "buyprice", "avgcost", "averagecost", "avgprice", "averageprice", "cost", "buyavg",
                   "averagebuyprice"),
         "date": ("date", "buydate", "purchasedate", "tradedate", "boughton")}


def csv_path() -> Path:
    return Path(os.environ.get("HOLDINGS_CSV") or _REPO_ROOT / "holdings.csv")


def parse_date(s) -> date | None:
    """A buy date in any common Indian form (day first); blank → None."""
    s = str(s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d/%m/%y",
                "%b %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"can't read the date {s!r} — use e.g. 2025-03-12 or 12-03-2025")


def _num(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("₹", "").strip())
    except ValueError:
        raise ValueError(f"{s!r} isn't a number") from None


# ------------------------------------------------------------------ store
def _resolve(con: duckdb.DuckDBPyConnection, text: str) -> tuple[str, str] | None:
    t = str(text or "").strip()
    if not t:
        return None
    sym = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", t.upper())
    r = con.execute("SELECT symbol, company_name FROM equity_master WHERE symbol = ?", [sym]).fetchone()
    if r:
        return r[0], r[1] or r[0]
    from equity_research.analysis.reality_check import _resolve as by_name

    return by_name(con, t)


def add_lot(con: duckdb.DuckDBPyConnection, stock: str, qty, price, buy_date=None, *, source: str = "ui") -> dict:
    """Add one lot (validated). The stock also lands in the watchlist as a holding."""
    hit = _resolve(con, stock)
    if not hit:
        raise ValueError(f"couldn't find {stock!r} — start typing the company name and pick it from the list")
    q, p = _num(qty), _num(price)
    if q <= 0 or p <= 0:
        raise ValueError("quantity and price must be above zero")
    d = buy_date if isinstance(buy_date, date) else parse_date(buy_date)
    if d and d > date.today():
        raise ValueError("the buy date is in the future")
    lot = {"id": uuid.uuid4().hex[:12], "symbol": hit[0], "name": hit[1], "qty": q, "price": p, "buy_date": d,
           "source": source}
    con.execute("INSERT INTO holding_lots VALUES (?, ?, ?, ?, ?, ?, ?, now())",
                [lot["id"], hit[0], hit[1], q, p, d, source])
    _ensure_watchlist(con, hit[0], hit[1])
    return lot


def _ensure_watchlist(con, symbol: str, name: str) -> None:
    con.execute("""INSERT INTO watchlist (symbol, company, added_at, list_type) VALUES (?, ?, now(), 'holding')
                   ON CONFLICT (symbol) DO UPDATE SET list_type = 'holding'""", [symbol, name])


def update_lot(con: duckdb.DuckDBPyConnection, lot_id: str, qty, price, buy_date=None) -> None:
    row = con.execute("SELECT source FROM holding_lots WHERE id = ?", [lot_id]).fetchone()
    if not row:
        raise KeyError(lot_id)
    if row[0] == "csv":
        raise ValueError("this lot comes from holdings.csv — edit it there")
    q, p = _num(qty), _num(price)
    if q <= 0 or p <= 0:
        raise ValueError("quantity and price must be above zero")
    d = buy_date if isinstance(buy_date, date) else parse_date(buy_date)
    con.execute("UPDATE holding_lots SET qty = ?, price = ?, buy_date = ? WHERE id = ?", [q, p, d, lot_id])


def delete_lot(con: duckdb.DuckDBPyConnection, lot_id: str) -> None:
    row = con.execute("SELECT source FROM holding_lots WHERE id = ?", [lot_id]).fetchone()
    if not row:
        raise KeyError(lot_id)
    if row[0] == "csv":
        raise ValueError("this lot comes from holdings.csv — remove it there")
    con.execute("DELETE FROM holding_lots WHERE id = ?", [lot_id])


def lots(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = ["id", "symbol", "name", "qty", "price", "buy_date", "source"]
    return [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM holding_lots ORDER BY symbol, buy_date NULLS LAST, added_at").fetchall()]


def missing(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Stocks already in your watchlist as holdings but with no quantity yet — shown in the UI as rows to
    fill in (qty, price, date)."""
    rows = con.execute(
        """SELECT w.symbol, coalesce(nullif(m.company_name, ''), nullif(w.company, ''), w.symbol) FROM watchlist w
           LEFT JOIN equity_master m ON m.symbol = w.symbol
           WHERE (w.list_type = 'holding' OR w.list_type IS NULL)
             AND w.symbol NOT IN (SELECT symbol FROM holding_lots)
           ORDER BY 2""").fetchall()
    return [{"symbol": r[0], "name": r[1]} for r in rows]


def search(con: duckdb.DuckDBPyConnection, q: str, limit: int = 8) -> list[dict]:
    """Company-name search for the add box: 'bharat ele' → Bharat Electronics. Every word must appear in
    the name (or the query is the symbol); names starting with the query rank first."""
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if w]
    if not words:
        return []
    cond = " AND ".join(["lower(company_name) LIKE ?"] * len(words))
    rows = con.execute(
        f"""SELECT symbol, company_name FROM equity_master
            WHERE ({cond}) OR upper(symbol) = ?
            ORDER BY (upper(symbol) = ?) DESC, (lower(company_name) LIKE ?) DESC, length(company_name)
            LIMIT ?""",
        [f"%{w}%" for w in words] + [q.strip().upper(), q.strip().upper(), f"{q.strip().lower()}%", limit]).fetchall()
    return [{"symbol": r[0], "name": r[1]} for r in rows]


# ------------------------------------------------------------------ holdings.csv
def read_csv(path: Path) -> list[dict]:
    """Rows of ``{symbol, qty, price, date, line}`` from a holdings file (headers matched loosely)."""
    text = path.read_text(encoding="utf-8-sig")
    rows = [(n, r) for n, r in enumerate(csv.reader(text.splitlines()), 1)      # n = the file's line number
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
        out.append({"symbol": cell("symbol"), "qty": cell("qty"), "price": cell("price"), "date": cell("date"),
                    "line": n})
    return out


def sync_csv(con: duckdb.DuckDBPyConnection, *, force: bool = False) -> dict:
    """Re-import ``holdings.csv`` if it changed since the last import (or ``force``). Its rows replace
    the previous import; UI lots are untouched. Returns {status, imported, errors, path}."""
    from equity_research import scan

    path = csv_path()
    out = {"path": str(path), "imported": 0, "errors": [], "status": "none"}
    if not path.is_file():
        if scan._meta(con, "holdings_csv_mtime"):          # the file was deleted → drop what it brought
            con.execute("DELETE FROM holding_lots WHERE source = 'csv'")
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
    for r in rows:
        try:
            add_lot(con, r["symbol"], r["qty"], r["price"], r["date"] or None, source="csv")
            out["imported"] += 1
        except (ValueError, TypeError) as e:
            out["errors"].append(f"line {r['line']} ({r['symbol'] or 'blank'}): {e}")
    scan._set_meta(con, "holdings_csv_mtime", stamp)
    scan._set_meta(con, "holdings_csv_errors", "\n".join(out["errors"]))
    out["status"] = "imported"
    log.info("holdings.csv: %d lot(s) imported, %d problem(s)", out["imported"], len(out["errors"]))
    return out


# ------------------------------------------------------------------ valuation
def _last_close(con, symbol: str) -> tuple[float, date] | None:
    r = con.execute("""SELECT close, trade_date FROM equity_eod_adj WHERE symbol = ?
                       AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
                       ORDER BY trade_date DESC, CASE series WHEN 'EQ' THEN 0 ELSE 1 END LIMIT 1""",
                    [symbol]).fetchone()
    return (float(r[0]), r[1]) if r else None


def _bench_since(con, since: date) -> float | None:
    rows = con.execute("""SELECT
            (SELECT close FROM index_close WHERE index_name = ? AND trade_date >= ? ORDER BY trade_date LIMIT 1),
            (SELECT close FROM index_close WHERE index_name = ? ORDER BY trade_date DESC LIMIT 1)""",
                       [BENCHMARK, since, BENCHMARK]).fetchone()
    return (rows[1] / rows[0] - 1) if rows and rows[0] and rows[1] else None


def value_lot(con: duckdb.DuckDBPyConnection, lot: dict, *, today: date | None = None) -> dict:
    """One lot on the latest close. Dated lots are brought through splits / bonuses since the buy."""
    from equity_research.analysis import corporate_actions as ca

    today = today or date.today()
    v = {**lot, "buy_date": lot["buy_date"].isoformat() if lot.get("buy_date") else None,
         "adj_qty": lot["qty"], "adj_price": lot["price"], "adjusted": []}
    px = _last_close(con, lot["symbol"])
    d = lot.get("buy_date")
    if d:
        mult, labels, _ = ca.share_multiplier_since(con, lot["symbol"], d)
        if mult and abs(mult - 1) > 1e-9:
            v.update(adj_qty=lot["qty"] * mult, adj_price=lot["price"] / mult, adjusted=labels)
    cost = v["adj_qty"] * v["adj_price"]
    v["cost"] = cost
    if px:
        v.update(ltp=px[0], ltp_date=px[1].isoformat(), value=v["adj_qty"] * px[0])
        v.update(pnl=v["value"] - cost, pnl_pct=100 * (v["value"] / cost - 1))
    if d:
        days = (today - d).days
        v.update(days_held=days, term="long" if days > LONG_TERM_DAYS else "short",
                 days_to_long=None if days > LONG_TERM_DAYS else LONG_TERM_DAYS + 1 - days)
        if px and days >= LONG_TERM_DAYS:
            v["yearly_pct"] = 100 * ((px[0] / v["adj_price"]) ** (365 / days) - 1)
        b = _bench_since(con, d)
        if b is not None:
            v["bench_pct"] = 100 * b
    return v


def portfolio(con: duckdb.DuckDBPyConnection, *, today: date | None = None) -> dict:
    """Every lot valued, grouped per stock, with totals. Stocks without a price are listed, not valued."""
    vals = [value_lot(con, lt, today=today) for lt in lots(con)]
    stocks: dict[str, dict] = {}
    for v in vals:
        s = stocks.setdefault(v["symbol"], {"symbol": v["symbol"], "name": v["name"], "lots": [], "qty": 0.0,
                                           "cost": 0.0, "value": 0.0, "priced": True, "ltp": v.get("ltp")})
        s["lots"].append(v)
        s["qty"] += v["adj_qty"]
        s["cost"] += v["cost"]
        if "value" in v:
            s["value"] += v["value"]
        else:
            s["priced"] = False
    for s in stocks.values():
        s["avg_price"] = s["cost"] / s["qty"] if s["qty"] else None
        if s["priced"]:
            s["pnl"] = s["value"] - s["cost"]
            s["pnl_pct"] = 100 * (s["value"] / s["cost"] - 1) if s["cost"] else None
    priced = [s for s in stocks.values() if s["priced"]]
    cost = sum(s["cost"] for s in priced)
    value = sum(s["value"] for s in priced)
    for s in priced:
        s["weight_pct"] = 100 * s["value"] / value if value else None
    return {"stocks": sorted(stocks.values(), key=lambda s: -(s["value"] or 0)),
            "missing": missing(con),
            "total": {"cost": cost, "value": value, "pnl": value - cost,
                      "pnl_pct": 100 * (value / cost - 1) if cost else None, "n_stocks": len(stocks),
                      "n_lots": len(vals), "unpriced": [s["symbol"] for s in stocks.values() if not s["priced"]]}}
