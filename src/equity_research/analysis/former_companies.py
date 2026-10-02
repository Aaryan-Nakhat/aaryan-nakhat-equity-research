"""🔁 Former companies — stocks that stopped trading (merged, renamed away, delisted), so you can enter a buy
exactly as you made it ("100 shares of the old company @ ₹120 on that date") and the tool converts it.

* **Names.** NSE's company list only has what's listed today. Every symbol in our price history that stopped
  trading is looked up once on NSE's per-company corporate-action record, which carries the company name and,
  for a merger, a ``Merger`` / ``Amalgamation`` row with its record date. Runs in the
  background (``refresh``), batched in one browser session per ~80 symbols.
* **How it merged.** The record doesn't say into whom or at what ratio, so ``lookup_merger`` reads the company's
  own filings around the record date (scheme / allotment notices) with the LLM → the surviving company and
  "N new for every M held" (e.g. 3 for every 2). Kept only when the surviving company
  resolves to a listed one. Looked up lazily when you hold such a stock; a miss is retried after a week.

A merger is tax-neutral: your cost carries over whole, and the holding period counts from your original buy.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta

import duckdb

log = logging.getLogger(__name__)
BATCH = 80
RETRY_DAYS = 7
_MERGER = re.compile(r"\b(merger|amalgamation)\b", re.I)
_SCHEME = re.compile(r"amalgamat|merger|scheme|allot|record.?date|swap|share.?exchange", re.I)
_RATIO_FIRST = re.compile(r"record.?date|allot|swap|share.?exchange", re.I)   # these notices state the ratio
MAX_PDFS = 6
_lock = threading.Lock()


def _d(s) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def candidates(con: duckdb.DuckDBPyConnection) -> list[tuple[str, date]]:
    """Main-board symbols in our history that stopped trading (not listed today) and aren't looked up yet."""
    return [(r[0], r[1]) for r in con.execute(
        """SELECT symbol, max(trade_date) FROM equity_eod WHERE series IN ('EQ', 'BE', 'BZ') GROUP BY symbol
           HAVING symbol NOT IN (SELECT symbol FROM equity_master)
              AND symbol NOT IN (SELECT symbol FROM former_companies)
              AND max(trade_date) < (SELECT max(trade_date) FROM equity_eod) - INTERVAL 10 DAY
           ORDER BY 2 DESC""").fetchall()]


def parse_actions(rows) -> dict:
    """NSE's per-symbol corporate-action rows → {name, isin, merger_date}."""
    out = {"name": None, "isin": None, "merger_date": None}
    for r in rows if isinstance(rows, list) else []:
        out["name"] = out["name"] or (str(r.get("comp") or "").strip() or None)
        out["isin"] = out["isin"] or r.get("isin")
        if _MERGER.search(str(r.get("subject") or "")) and not out["merger_date"]:
            out["merger_date"] = _d(r.get("recDate")) or _d(r.get("exDate"))
    return out


def refresh(con: duckdb.DuckDBPyConnection, *, limit: int | None = None) -> int:
    """Look up names (and merger dates) for symbols that stopped trading. Returns how many were stored."""
    from equity_research.scrapers import nse_api

    todo = candidates(con)[:limit] if limit else candidates(con)
    n = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        paths = {s: f"/api/corporates-corporateActions?index=equities&symbol={nse_api.q(s)}" for s, _ in chunk}
        try:
            got = nse_api.fetch_api_multi(paths)
        except Exception:  # noqa: BLE001 — NSE access off / a failed session: try again next time
            log.exception("former companies: batch failed")
            break
        for sym, last in chunk:
            info = parse_actions(got.get(sym))
            con.execute("""INSERT OR REPLACE INTO former_companies (symbol, name, isin, last_traded, merger_date,
                           fetched_at) VALUES (?, ?, ?, ?, ?, now())""",
                        [sym, info["name"], info["isin"], last, info["merger_date"]])
            n += 1
    log.info("former companies: %d looked up", n)
    return n


def search(con: duckdb.DuckDBPyConnection, q: str, limit: int = 5) -> list[dict]:
    """Former companies whose name has every word typed (or whose old symbol is typed)."""
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if w]
    if not words:
        return []
    cond = " AND ".join(["lower(name) LIKE ?"] * len(words))
    rows = con.execute(
        f"""SELECT symbol, name, merger_date, into_name FROM former_companies
            WHERE name IS NOT NULL AND (({cond}) OR upper(symbol) = ?)
              AND (merger_date IS NOT NULL OR isin IS NULL OR isin NOT IN (   -- still listed elsewhere → that one
                   SELECT isin FROM equity_master WHERE isin IS NOT NULL
                   UNION SELECT isin FROM instruments WHERE isin IS NOT NULL))
            ORDER BY last_traded DESC LIMIT ?""", [f"%{w}%" for w in words] + [q.strip().upper(), limit]).fetchall()
    out = []
    for sym, name, md, into in rows:
        note = (f"merged into {into}" if into else "merged") + f" on {md:%d-%b-%Y}" if md else "no longer traded"
        out.append({"symbol": sym, "name": name, "note": note, "former": True})
    return out


def info(con: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    r = con.execute("""SELECT symbol, name, merger_date, into_symbol, into_name, ratio_new, ratio_old, source, url,
                              merger_checked_at, isin FROM former_companies WHERE symbol = ?""", [symbol]).fetchone()
    if not r:
        return None
    return dict(zip(["symbol", "name", "merger_date", "into_symbol", "into_name", "ratio_new", "ratio_old",
                     "source", "url", "merger_checked_at", "isin"], r))


_LIVE_BY_ISIN = """SELECT symbol, company_name FROM equity_master WHERE isin = ?
                   UNION ALL SELECT symbol, name FROM instruments WHERE isin = ?"""


def live_listing(con: duckdb.DuckDBPyConnection, fc: dict) -> tuple[str, str] | None:
    """The same security still listed under another symbol (same ISIN) — e.g. a share that left NSE but trades
    on BSE, or a stray old NSE symbol. Not for a merger (the merged company's ISIN is extinguished)."""
    if fc.get("merger_date") or not fc.get("isin"):
        return None
    r = con.execute(_LIVE_BY_ISIN, [fc["isin"], fc["isin"]]).fetchone()
    return (r[0], r[1]) if r else None


def needs_lookup(fc: dict) -> bool:
    """A merger whose target / ratio isn't known yet (and wasn't just tried)."""
    if not fc.get("merger_date") or (fc.get("into_symbol") and fc.get("ratio_new")):
        return False
    at = fc.get("merger_checked_at")
    return not at or datetime.now() - at > timedelta(days=RETRY_DAYS)


def lookup_merger(con: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    """Read the company's scheme / allotment filings → into whom it merged and at what ratio. Never raises."""
    from equity_research.portfolio import store as holdings
    from equity_research.common.http import fetch_bytes
    from equity_research.reports import synthesize
    from equity_research.scrapers import nse_api

    fc = info(con, symbol)
    if not fc or not fc["merger_date"]:
        return fc
    md = fc["merger_date"]
    got: dict = {}
    try:
        rows = nse_api.corporate_announcements(symbol=symbol, from_date=f"{md - timedelta(days=150):%d-%m-%Y}",
                                               to_date=f"{md + timedelta(days=10):%d-%m-%Y}")
        notes = []
        for r in rows if isinstance(rows, list) else []:
            text = " ".join(str(x) for x in (r.get("desc"), r.get("attchmntText"), r.get("attchmntFile")) if x)
            if _SCHEME.search(text):
                notes.append({"text": " ".join(text.split()), "url": r.get("attchmntFile") or ""})
        notes.sort(key=lambda n: not _RATIO_FIRST.search(n["text"]))     # record-date / allotment notices first
        ev, files = {}, []
        for i, n in enumerate(notes[:10], 1):
            ev[f"F{i}"] = n
            if n["url"].lower().endswith(".pdf") and len(files) < MAX_PDFS:
                try:
                    files.append((f"F{i}.pdf", fetch_bytes(n["url"])))
                except Exception:  # noqa: BLE001
                    pass
        if ev:
            got = synthesize.merger_terms(fc["name"] or symbol, md, {k: v["text"] for k, v in ev.items()},
                                          files=files) or {}
            got["url"] = (ev.get(got.get("source_id") or "") or {}).get("url")
    except Exception:  # noqa: BLE001
        log.exception("merger lookup failed for %s", symbol)
    hit = None
    if got.get("into_name"):
        hits = holdings.search(con, re.sub(r"\b(limited|ltd\.?)\s*$", "", got["into_name"], flags=re.I).strip())
        hit = hits[0] if hits else None
    if hit and got.get("ratio_new") and got.get("ratio_old"):
        con.execute("""UPDATE former_companies SET into_symbol = ?, into_name = ?, ratio_new = ?, ratio_old = ?,
                       source = 'filing', url = ?, merger_checked_at = now() WHERE symbol = ?""",
                    [hit["symbol"], hit["name"], got["ratio_new"], got["ratio_old"], got.get("url"), symbol])
    else:
        con.execute("UPDATE former_companies SET merger_checked_at = now() WHERE symbol = ?", [symbol])
    log.info("merger terms %s: %s", symbol, got or "not found")
    return info(con, symbol)


def lookup_missing_async(symbols: list[str]) -> None:
    """Read merger terms in a background thread (the page says 'looking up…' meanwhile)."""
    from equity_research.common import llm

    if not symbols or not llm.configured() or not _lock.acquire(blocking=False):
        return

    def run():
        from equity_research.common.db import connect

        con = connect()
        try:
            for s in symbols:
                fc = info(con, s)
                if fc and needs_lookup(fc):
                    lookup_merger(con, s)
        finally:
            con.close()
            _lock.release()

    threading.Thread(target=run, name="merger-terms", daemon=True).start()
