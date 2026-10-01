"""Your buys (``holding_lots``) and sells (``holding_sells``) — add / edit / delete, the company-name search,
and the watchlist holdings still waiting for numbers. Everything stays in the local database."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

import duckdb

from equity_research.portfolio import instruments


def parse_date(s) -> date | None:
    """A date in any common Indian form (day first); blank → None."""
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


def _date(v) -> date | None:
    return v if isinstance(v, date) or v is None else parse_date(v)


def num(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("₹", "").strip())
    except ValueError:
        raise ValueError(f"{s!r} isn't a number") from None


def _positive(qty, price) -> tuple[float, float]:
    q, p = num(qty), num(price)
    if q <= 0 or p <= 0:
        raise ValueError("quantity and price must be above zero")
    return q, p


# ------------------------------------------------------------------ finding a company
def search(con: duckdb.DuckDBPyConnection, q: str, limit: int = 8) -> list[dict]:
    """Company-name search for the add box: listed shares first, then ETFs / SME / REITs / InvITs / BSE-only,
    then companies that merged away. Every word typed must appear in the name (or the query is the symbol)."""
    from equity_research.analysis import former_companies

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
    return ([{"symbol": r[0], "name": r[1]} for r in rows] + instruments.search(con, q)
            + former_companies.search(con, q))


def resolve(con: duckdb.DuckDBPyConnection, text: str) -> tuple[str, str] | None:
    """A symbol or a company name → (symbol, name); None when nothing (or nothing unambiguous) matches."""
    t = str(text or "").strip()
    if not t:
        return None
    sym = re.sub(r"-(EQ|BE|BZ|SM|ST|RR|IV)$", "", t.upper())
    r = con.execute("SELECT symbol, company_name FROM equity_master WHERE symbol = ?", [sym]).fetchone()
    if r:
        return r[0], r[1] or r[0]
    hit = instruments.find(con, sym)
    if hit:
        return hit
    r = con.execute("SELECT symbol, name FROM former_companies WHERE symbol = ? AND name IS NOT NULL", [sym]).fetchone()
    if r:
        return r[0], r[1]
    hits = search(con, t, limit=1)
    if hits:
        return hits[0]["symbol"], hits[0]["name"]
    from equity_research.analysis.reality_check import _resolve as by_name

    return by_name(con, t)


def _ensure_watchlist(con, symbol: str, name: str) -> None:
    con.execute("""INSERT INTO watchlist (symbol, company, added_at, list_type) VALUES (?, ?, now(), 'holding')
                   ON CONFLICT (symbol) DO UPDATE SET list_type = 'holding'""", [symbol, name])


# ------------------------------------------------------------------ buys
def _received(received, buy_date: date | None) -> date | None:
    r = _date(received)
    if r and r > date.today():
        raise ValueError("the date you received the shares is in the future")
    if r and buy_date and r < buy_date:
        raise ValueError("you can't have received the shares before you bought them")
    return r


def add_lot(con: duckdb.DuckDBPyConnection, stock: str, qty, price, buy_date=None, *, source: str = "ui",
            received=None) -> dict:
    """Add one buy (validated). ``received``: when these shares arrived through a corporate event (a demerger's
    new shares) — only this company's actions after it apply, and no cash was paid. The stock also lands in
    the watchlist as a holding (for a company that merged away, the one it became)."""
    from equity_research.analysis import former_companies as fc

    hit = resolve(con, stock)
    if not hit:
        raise ValueError(f"couldn't find {stock!r} — start typing the company name and pick it from the list")
    q, p = _positive(qty, price)
    d = _date(buy_date)
    if d and d > date.today():
        raise ValueError("the buy date is in the future")
    rcv = _received(received, d)
    lot = {"id": uuid.uuid4().hex[:12], "symbol": hit[0], "name": hit[1], "qty": q, "price": p, "buy_date": d,
           "source": source, "received": rcv}
    con.execute("""INSERT INTO holding_lots (id, symbol, name, qty, price, buy_date, source, added_at, received)
                   VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?)""", [lot["id"], hit[0], hit[1], q, p, d, source, rcv])
    old = fc.info(con, hit[0])
    if not old:
        _ensure_watchlist(con, hit[0], hit[1])
    elif old.get("into_symbol"):
        _ensure_watchlist(con, old["into_symbol"], old["into_name"])
    return lot


def update_lot(con: duckdb.DuckDBPyConnection, lot_id: str, qty, price, buy_date=None, received=None) -> None:
    _editable(con, "holding_lots", lot_id)
    q, p = _positive(qty, price)
    d = _date(buy_date)
    con.execute("UPDATE holding_lots SET qty = ?, price = ?, buy_date = ?, received = ? WHERE id = ?",
                [q, p, d, _received(received, d), lot_id])


def delete_lot(con: duckdb.DuckDBPyConnection, lot_id: str) -> None:
    _editable(con, "holding_lots", lot_id)
    con.execute("DELETE FROM holding_lots WHERE id = ?", [lot_id])


def _editable(con, table: str, row_id: str) -> None:
    row = con.execute(f"SELECT source FROM {table} WHERE id = ?", [row_id]).fetchone()
    if not row:
        raise KeyError(row_id)
    if row[0] == "csv":
        raise ValueError("this comes from holdings.csv — change it there")


def lots(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = ["id", "symbol", "name", "qty", "price", "buy_date", "source", "received"]
    return [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM holding_lots ORDER BY symbol, buy_date NULLS LAST, added_at").fetchall()]


# ------------------------------------------------------------------ sells
def add_sell(con: duckdb.DuckDBPyConnection, stock: str, qty, price, sell_date, *, kind: str = "sell",
             source: str = "ui") -> dict:
    """Record a sell (as on your contract note). ``kind='buyback'`` for shares tendered in a buyback."""
    hit = resolve(con, stock)
    if not hit:
        raise ValueError(f"couldn't find {stock!r}")
    q, p = _positive(qty, price)
    d = _date(sell_date)
    if not d:
        raise ValueError("a sell needs its date — it decides the tax")
    if d > date.today():
        raise ValueError("the sell date is in the future")
    if kind not in ("sell", "buyback"):
        raise ValueError("kind must be 'sell' or 'buyback'")
    sell = {"id": uuid.uuid4().hex[:12], "symbol": hit[0], "name": hit[1], "qty": q, "price": p, "sell_date": d,
            "kind": kind, "source": source}
    con.execute("""INSERT INTO holding_sells (id, symbol, name, qty, price, sell_date, kind, source, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())""", [sell["id"], hit[0], hit[1], q, p, d, kind, source])
    return sell


def delete_sell(con: duckdb.DuckDBPyConnection, sell_id: str) -> None:
    _editable(con, "holding_sells", sell_id)
    con.execute("DELETE FROM holding_sells WHERE id = ?", [sell_id])


def sells(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = ["id", "symbol", "name", "qty", "price", "sell_date", "kind", "source"]
    return [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM holding_sells ORDER BY sell_date, added_at").fetchall()]


# ------------------------------------------------------------------ the fill-in list
def missing(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Stocks in your watchlist as holdings with no buy entered yet (also not held through a company that
    merged into them) — shown in the UI as rows to fill in."""
    rows = con.execute(
        """SELECT w.symbol, coalesce(nullif(m.company_name, ''), nullif(w.company, ''), w.symbol) FROM watchlist w
           LEFT JOIN equity_master m ON m.symbol = w.symbol
           WHERE (w.list_type = 'holding' OR w.list_type IS NULL)
             AND w.symbol NOT IN (SELECT symbol FROM holding_lots)
             AND w.symbol NOT IN (SELECT f.into_symbol FROM former_companies f
                                  JOIN holding_lots l ON l.symbol = f.symbol WHERE f.into_symbol IS NOT NULL)
           ORDER BY 2""").fetchall()
    return [{"symbol": r[0], "name": r[1]} for r in rows]
