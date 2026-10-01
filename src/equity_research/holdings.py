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

**How to enter a buy** (the one rule that matters — repeated in the UI, the emails and the CSV template):

* **With a date** → the quantity and price **as you bought them** (your contract note). Splits / bonuses
  since that date are applied here — e.g. 100 @ ₹500 bought before a 1:5 split shows as 500 @ ₹100. Entering today's 500 @ ₹100
  with that old date would apply the split twice.
* **Without a date** → what your broker shows **today** (quantity and average price). Nothing is adjusted.

A dated buy whose price is far below that day's market price, with a split / bonus since, is flagged
(``warn``) — it's almost always today's adjusted numbers typed with the old date.

**Mergers and demergers.**

* A buy of a company that later merged into another is entered as made (the old company); it's converted at the
  swap ratio into the survivor's shares (``analysis/former_companies.py``), whose own splits / bonuses before the
  merger are not applied to them (the ``received`` date).
* A demerger after the buy splits the cost: the parent keeps the share of cost the company's filed notice gives
  (``analysis/demerger_costs.py`` — e.g. 85 %), the rest belongs to the new company's shares,
  which the UI offers to add (same buy date, received on the ex-date). With no notice filed yet, the market-price
  split is used and labelled an estimate.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from equity_research.common.db import _REPO_ROOT

log = logging.getLogger(__name__)
HOW_TO_ENTER = ("With a date → enter the quantity and price as you bought them (your contract note); splits and "
                "bonuses since then are applied for you — e.g. 100 @ ₹500 bought before a 1:5 split shows as 500 @ ₹100. "
                "Without a date → enter what your broker shows today (quantity and "
                "average price). Don't mix them: today's quantity with an old date counts the split twice.")
BENCHMARK = "Nifty 500"
LONG_TERM_DAYS = 365        # held MORE than 12 months → long-term

_HEAD = {"symbol": ("symbol", "stock", "instrument", "tradingsymbol", "scrip", "company", "name", "stockname"),
         "qty": ("qty", "quantity", "shares", "units", "quantityavailable"),
         "price": ("price", "buyprice", "avgcost", "averagecost", "avgprice", "averageprice", "cost", "buyavg",
                   "averagebuyprice"),
         "date": ("date", "buydate", "purchasedate", "tradedate", "boughton"),
         "received": ("received", "receivedon", "receiveddate", "mergerdate", "allotmentdate", "adjustfrom")}


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
    r = con.execute("SELECT symbol, name FROM former_companies WHERE symbol = ? AND name IS NOT NULL", [sym]).fetchone()
    if r:
        return r[0], r[1]
    hits = search(con, t, limit=1)                   # every word in the name — incl. companies that merged away
    if hits:
        return hits[0]["symbol"], hits[0]["name"]
    from equity_research.analysis.reality_check import _resolve as by_name

    return by_name(con, t)


def add_lot(con: duckdb.DuckDBPyConnection, stock: str, qty, price, buy_date=None, *, source: str = "ui",
            received=None) -> dict:
    """Add one lot (validated). ``received``: when these shares arrived through a merger / demerger (only this
    company's actions after it apply). The stock also lands in the watchlist as a holding."""
    hit = _resolve(con, stock)
    if not hit:
        raise ValueError(f"couldn't find {stock!r} — start typing the company name and pick it from the list")
    q, p = _num(qty), _num(price)
    if q <= 0 or p <= 0:
        raise ValueError("quantity and price must be above zero")
    d = buy_date if isinstance(buy_date, date) else parse_date(buy_date)
    if d and d > date.today():
        raise ValueError("the buy date is in the future")
    rcv = _received(received, d)
    lot = {"id": uuid.uuid4().hex[:12], "symbol": hit[0], "name": hit[1], "qty": q, "price": p, "buy_date": d,
           "source": source, "received": rcv}
    con.execute("""INSERT INTO holding_lots (id, symbol, name, qty, price, buy_date, source, added_at, received)
                   VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?)""", [lot["id"], hit[0], hit[1], q, p, d, source, rcv])
    from equity_research.analysis import former_companies as fc

    old = fc.info(con, hit[0])
    if not old:
        _ensure_watchlist(con, hit[0], hit[1])
    elif old.get("into_symbol"):
        _ensure_watchlist(con, old["into_symbol"], old["into_name"])
    return lot


def _received(received, buy_date: date | None) -> date | None:
    r = received if isinstance(received, date) or received is None else parse_date(received)
    if r and r > date.today():
        raise ValueError("the date you received the shares is in the future")
    if r and buy_date and r < buy_date:
        raise ValueError("you can't have received the shares before you bought them")
    return r


def _ensure_watchlist(con, symbol: str, name: str) -> None:
    con.execute("""INSERT INTO watchlist (symbol, company, added_at, list_type) VALUES (?, ?, now(), 'holding')
                   ON CONFLICT (symbol) DO UPDATE SET list_type = 'holding'""", [symbol, name])


def update_lot(con: duckdb.DuckDBPyConnection, lot_id: str, qty, price, buy_date=None, received=None) -> None:
    row = con.execute("SELECT source FROM holding_lots WHERE id = ?", [lot_id]).fetchone()
    if not row:
        raise KeyError(lot_id)
    if row[0] == "csv":
        raise ValueError("this lot comes from holdings.csv — edit it there")
    q, p = _num(qty), _num(price)
    if q <= 0 or p <= 0:
        raise ValueError("quantity and price must be above zero")
    d = buy_date if isinstance(buy_date, date) else parse_date(buy_date)
    con.execute("UPDATE holding_lots SET qty = ?, price = ?, buy_date = ?, received = ? WHERE id = ?",
                [q, p, d, _received(received, d), lot_id])


def delete_lot(con: duckdb.DuckDBPyConnection, lot_id: str) -> None:
    row = con.execute("SELECT source FROM holding_lots WHERE id = ?", [lot_id]).fetchone()
    if not row:
        raise KeyError(lot_id)
    if row[0] == "csv":
        raise ValueError("this lot comes from holdings.csv — remove it there")
    con.execute("DELETE FROM holding_lots WHERE id = ?", [lot_id])


def lots(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = ["id", "symbol", "name", "qty", "price", "buy_date", "source", "received"]
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
             AND w.symbol NOT IN (SELECT f.into_symbol FROM former_companies f       -- held via a merged company
                                  JOIN holding_lots l ON l.symbol = f.symbol WHERE f.into_symbol IS NOT NULL)
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
    from equity_research.analysis import former_companies

    return [{"symbol": r[0], "name": r[1]} for r in rows] + former_companies.search(con, q)


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
                    "received": cell("received"), "line": n})
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
            add_lot(con, r["symbol"], r["qty"], r["price"], r["date"] or None, source="csv",
                    received=r.get("received") or None)
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


SUSPECT_BELOW = 0.6     # a dated buy price under 60% of that day's raw close, with a split since → flagged


def _raw_close_on(con, symbol: str, d: date, before: date) -> float | None:
    """The unadjusted close nearest the buy date (within ~2 months, history can have gaps) and before the first
    split / bonus since (``before``) — so it's on the same share basis as what was paid then."""
    r = con.execute("""SELECT close FROM equity_eod WHERE symbol = ? AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
                       AND trade_date BETWEEN ? - INTERVAL 60 DAY AND ? + INTERVAL 60 DAY AND trade_date < ?
                       ORDER BY abs(trade_date - ?), CASE series WHEN 'EQ' THEN 0 ELSE 1 END LIMIT 1""",
                    [symbol, d, d, before, d]).fetchone()
    return float(r[0]) if r and r[0] else None


def _bench_since(con, since: date) -> float | None:
    rows = con.execute("""SELECT
            (SELECT close FROM index_close WHERE index_name = ? AND trade_date >= ? ORDER BY trade_date LIMIT 1),
            (SELECT close FROM index_close WHERE index_name = ? ORDER BY trade_date DESC LIMIT 1)""",
                       [BENCHMARK, since, BENCHMARK]).fetchone()
    return (rows[1] / rows[0] - 1) if rows and rows[0] and rows[1] else None


def _demergers(con, lot: dict, start: date, today: date, lookups: list | None) -> tuple[float, list[dict]]:
    """Demergers of this stock after ``start``: (share of the lot's cost the parent keeps, details). The filed
    split wins; otherwise the market-price split (an estimate); otherwise the cost isn't split (unknown)."""
    from equity_research.analysis import corporate_actions as ca
    from equity_research.analysis import demerger_costs as dc

    rows = con.execute("""SELECT ex_date, factor FROM price_adjustments WHERE symbol = ? AND kind = 'demerger'
                          AND ex_date > ? AND ex_date <= ? ORDER BY ex_date""", [lot["symbol"], start, today]).fetchall()
    keep, out = 1.0, []
    cost0 = lot["qty"] * lot["price"]
    for ex, factor in rows:
        split = dc.known(con, lot["symbol"], ex)
        if split is None and lookups is not None:
            lookups.append((lot["symbol"], lot["name"], ex))
        qty_then = lot["qty"] * (ca.share_multiplier_since(con, lot["symbol"], start, ex - timedelta(days=1))[0] or 1)
        item = {"ex_date": ex.isoformat(), "children": []}
        if split:
            moved = sum(c["cost_pct"] for c in split) / 100
            item.update(basis="filing", parent_pct=100 * (1 - moved), url=split[0]["url"])
            for c in split:
                n = qty_then * c["ratio_new"] / c["ratio_old"] if c["ratio_new"] and c["ratio_old"] else None
                c_cost = cost0 * keep * c["cost_pct"] / 100
                item["children"].append({"symbol": c["new_symbol"], "name": c["new_name"], "pct": c["cost_pct"],
                                         "qty": n, "cost": c_cost, "price": c_cost / n if n else None})
            keep *= 1 - moved
        elif factor:
            item.update(basis="looking" if split is None else "estimate", parent_pct=100 * factor)
            keep *= factor
        else:
            item.update(basis="looking" if split is None else "unknown", parent_pct=None)
        out.append(item)
    return keep, out


def value_lot(con: duckdb.DuckDBPyConnection, lot: dict, *, today: date | None = None,
              lookups: list | None = None) -> dict:
    """One lot on the latest close. Dated lots are brought through splits / bonuses since the buy (or since the
    shares were received, for a merger / demerger) and their cost is split at any demerger since."""
    from equity_research.analysis import corporate_actions as ca
    from equity_research.analysis import former_companies

    today = today or date.today()
    fc = former_companies.info(con, lot["symbol"])
    if fc:
        return _value_former(con, lot, fc, today, lookups)
    v = {**lot, "buy_date": lot["buy_date"].isoformat() if lot.get("buy_date") else None,
         "received": lot["received"].isoformat() if lot.get("received") else None,
         "adj_qty": lot["qty"], "adj_price": lot["price"], "adjusted": [], "demergers": []}
    px = _last_close(con, lot["symbol"])
    d = lot.get("buy_date")
    start = lot.get("received") or d
    if start:
        mult, labels, ex_dates = ca.share_multiplier_since(con, lot["symbol"], start)
        keep, v["demergers"] = _demergers(con, lot, start, today, lookups)
        if mult and abs(mult - 1) > 1e-9:
            v.update(adj_qty=lot["qty"] * mult, adj_price=lot["price"] / mult, adjusted=labels)
            then = None if lot.get("received") else _raw_close_on(con, lot["symbol"], d, min(ex_dates))
            if then and lot["price"] < SUSPECT_BELOW * then:
                v["warn"] = (f"₹{lot['price']:,.2f} is far below the price around {d:%d-%b-%Y} (≈₹{then:,.2f}), and there's "
                             f"been a split / bonus since — this looks like today's numbers. Enter the quantity and "
                             f"price as you bought them (before the split), or remove the date.")
        if keep != 1.0:
            v["adj_price"] *= keep                   # the rest of the cost now sits with the demerged shares
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


def _value_former(con, lot: dict, fc: dict, today: date, lookups: list | None) -> dict:
    """A buy of a company that later merged: converted at the swap ratio into the surviving company's shares
    (the old company's own splits / bonuses before the merger applied first), same total cost and buy date,
    then valued as those shares — whose own actions count only from the merger. Not yet known → a note."""
    from equity_research.analysis import corporate_actions as ca
    from equity_research.analysis import former_companies

    base = {**lot, "buy_date": lot["buy_date"].isoformat() if lot.get("buy_date") else None,
            "received": None, "adj_qty": lot["qty"], "adj_price": lot["price"], "adjusted": [], "demergers": [],
            "cost": lot["qty"] * lot["price"]}
    md = fc.get("merger_date")
    if md and fc.get("into_symbol") and fc.get("ratio_new") and fc.get("ratio_old"):
        start = lot.get("received") or lot.get("buy_date") or md
        m_old = (ca.share_multiplier_since(con, lot["symbol"], start, md)[0] or 1) if start < md else 1
        new_qty = lot["qty"] * m_old * fc["ratio_new"] / fc["ratio_old"]
        cost = lot["qty"] * lot["price"]
        v = value_lot(con, {**lot, "symbol": fc["into_symbol"], "name": fc["into_name"], "qty": new_qty,
                            "price": cost / new_qty, "received": md}, today=today, lookups=lookups)
        v.update(id=lot["id"], qty=lot["qty"], price=lot["price"], source=lot["source"], received=None,
                 via={"symbol": lot["symbol"], "name": fc["name"], "date": md.isoformat(), "url": fc.get("url"),
                      "ratio": f"{fc['ratio_new']:g} for every {fc['ratio_old']:g}", "shares": new_qty})
        return v
    if md and former_companies.needs_lookup(fc):
        if lookups is not None:
            lookups.append(("merger", lot["symbol"]))
        base["note"] = f"{fc['name']} merged on {md:%d-%b-%Y} — reading its filings for what you got in exchange…"
    elif md:
        base["note"] = (f"{fc['name']} merged on {md:%d-%b-%Y}, but its filings don't say the swap ratio — enter the "
                        f"shares you received under the new company instead.")
    else:
        base["note"] = f"{fc['name']} is no longer traded (last traded {fc.get('last_traded')})."
    return base


def portfolio(con: duckdb.DuckDBPyConnection, *, today: date | None = None) -> dict:
    """Every lot valued, grouped per stock, with totals. Stocks without a price are listed, not valued.
    ``lookups`` lists demergers whose filed cost split hasn't been looked up yet (the caller fetches them)."""
    need: list = []
    vals = [value_lot(con, lt, today=today, lookups=need) for lt in lots(con)]
    held = {v["symbol"] for v in vals}
    for v in vals:
        for dm in v["demergers"]:
            for c in dm["children"]:
                c["have"] = c["symbol"] in held
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
            "lookups": sorted({x for x in need if x[0] != "merger"}),
            "merger_lookups": sorted({x[1] for x in need if x[0] == "merger"}),
            "total": {"cost": cost, "value": value, "pnl": value - cost,
                      "pnl_pct": 100 * (value / cost - 1) if cost else None, "n_stocks": len(stocks),
                      "n_lots": len(vals), "unpriced": [s["symbol"] for s in stocks.values() if not s["priced"]]}}
