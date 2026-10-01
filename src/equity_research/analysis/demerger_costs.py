"""✂️ Demerger cost split — how much of your original cost stays with the parent and how much moves to each
new company, from the company's own **"apportionment of cost of acquisition"** notice.

Indian tax law (s.49(2C)/(2D)) splits the cost by *net book value*, and every company publishes the result —
e.g. "85 % stays with the parent, 15 % moves to the new company (1 share for 1)". That can be far from what the
market prices implied (in one real case the market said ~31 % stays, the notice ~88 %), so the filed notice is
used whenever it exists;
the market-based split (the parent's opening gap, ``price_adjustments.factor``) is only a labelled fallback.

``lookup`` finds the notice among the company's filings around the ex-date (the title is often just
"General Updates" — the filename says "Cost_of_acquisition"), has the LLM read the PDF, and stores the
result in ``demerger_costs``: one row per new company (``new_symbol`` resolved from its name), or a single
``source = 'none'`` row when nothing was found (retried after ``RETRY_DAYS``).
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta

import duckdb

log = logging.getLogger(__name__)
RETRY_DAYS = 7
_NOTICE = re.compile(r"cost.{0,3}of.{0,3}a(c)?qu?isition|apportion|[_\W]coa[_\W]", re.I)
_lock = threading.Lock()


def known(con: duckdb.DuckDBPyConnection, symbol: str, ex_date: date) -> list[dict] | None:
    """The stored split for this demerger: [{new_symbol, new_name, cost_pct, ratio_new, ratio_old, url}] (empty
    when the lookup found nothing), or None when it hasn't been looked up (or is due a retry)."""
    rows = con.execute("""SELECT new_symbol, new_name, cost_pct, ratio_new, ratio_old, source, url, fetched_at
                          FROM demerger_costs WHERE symbol = ? AND ex_date = ?""", [symbol, ex_date]).fetchall()
    if not rows:
        return None
    if all(r[5] == "none" for r in rows):
        if rows[0][7] and datetime.now() - rows[0][7] > timedelta(days=RETRY_DAYS):
            return None
        return []
    return [{"new_symbol": r[0], "new_name": r[1], "cost_pct": r[2], "ratio_new": r[3], "ratio_old": r[4],
             "url": r[6]} for r in rows if r[5] != "none"]


def _notices(symbol: str, ex_date: date) -> list[dict]:
    from equity_research.scrapers import nse_api

    rows = nse_api.corporate_announcements(symbol=symbol, from_date=f"{ex_date - timedelta(days=60):%d-%m-%Y}",
                                           to_date=f"{ex_date + timedelta(days=150):%d-%m-%Y}")
    out = []
    for r in rows if isinstance(rows, list) else []:
        text = " ".join(str(x) for x in (r.get("desc"), r.get("attchmntText"), r.get("attchmntFile")) if x)
        if _NOTICE.search(text) and str(r.get("attchmntFile") or "").lower().endswith(".pdf"):
            out.append({"url": r["attchmntFile"], "text": " ".join(text.split())})
    return out


def lookup(con: duckdb.DuckDBPyConnection, symbol: str, name: str, ex_date: date) -> list[dict]:
    """Find, read and store the company's cost-split notice for this demerger. Never raises."""
    from equity_research.common.http import fetch_bytes
    from equity_research.reports import synthesize

    got: dict = {}
    try:
        notes = _notices(symbol, ex_date)
        files, ev = [], {}
        for i, n in enumerate(notes[:3], 1):
            ev[f"F{i}"] = n
            try:
                files.append((f"F{i}.pdf", fetch_bytes(n["url"])))
            except Exception:  # noqa: BLE001
                pass
        if files:
            got = synthesize.demerger_cost_split(name, ex_date, {k: v["text"] for k, v in ev.items()},
                                                files=files) or {}
            got["url"] = (ev.get(got.get("source_id") or "") or ev.get("F1") or {}).get("url")
    except Exception:  # noqa: BLE001 — NSE access off, a fetch or LLM failure: fall back to the estimate
        log.exception("demerger cost lookup failed for %s %s", symbol, ex_date)
    rows = []
    for c in got.get("resulting") or []:
        hit = _resolve(con, c.get("name") or "")
        rows.append([symbol, ex_date, hit[0] if hit else None, hit[1] if hit else c.get("name"), c.get("pct"),
                     c.get("ratio_new"), c.get("ratio_old"), "filing", got.get("url")])
    con.execute("DELETE FROM demerger_costs WHERE symbol = ? AND ex_date = ?", [symbol, ex_date])
    if rows:
        con.executemany("INSERT INTO demerger_costs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())", rows)
    else:
        con.execute("INSERT INTO demerger_costs VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, 'none', NULL, now())",
                    [symbol, ex_date])
    log.info("demerger cost split %s %s: %s", symbol, ex_date, rows or "no notice found")
    return known(con, symbol, ex_date) or []


def _resolve(con, name: str) -> tuple[str, str] | None:
    from equity_research.portfolio import store as holdings

    hits = holdings.search(con, re.sub(r"\b(limited|ltd\.?|formerly.*)$", "", name, flags=re.I).strip())
    return (hits[0]["symbol"], hits[0]["name"]) if hits else None


def lookup_missing_async(pairs: list[tuple[str, str, date]]) -> None:
    """Look up any not-yet-known splits in a background thread (the page shows the estimate meanwhile)."""
    from equity_research.common import llm

    if not pairs or not llm.configured() or not _lock.acquire(blocking=False):
        return

    def run():
        from equity_research.common.db import connect

        con = connect()
        try:
            for sym, name, ex in pairs:
                if known(con, sym, ex) is None:
                    lookup(con, sym, name, ex)
        finally:
            con.close()
            _lock.release()

    threading.Thread(target=run, name="demerger-costs", daemon=True).start()
