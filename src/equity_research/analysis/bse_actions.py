"""BSE's corporate-action record — what NSE's record doesn't have, for the stocks you hold.

NSE's record only covers a company's NSE life. A share that traded on BSE first (listed on NSE later) or only on
BSE has its earlier splits, bonuses and dividends only in BSE's record (``CorporateAction`` API, plain HTTP).
For each held stock (matched to its BSE scrip code by ISIN, ``bse_codes``):

* **split / consolidation / bonus** → ``price_adjustments`` (``source = 'bse'``) — only when NSE has no record
  within a week of that date (so nothing is counted twice), sized from BSE's wording ("Bonus issue 1:2",
  "Stock Split From Rs.10/- to Rs.2/-");
* **rights issue** → a ``rights`` record (BSE rarely states the ratio — the holdings page then asks whether you
  subscribed, without a number);
* **dividend** → ``dividends`` (amount per share);
* **anything that changes the share count but can't be sized from the record** (reduction of capital,
  scheme / arrangement, amalgamation) → an ``unsized`` record: the holdings page notes it on the buys it affects,
  so the share count can be checked against the broker.

Refreshed weekly per held stock, in the background.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta

import duckdb

log = logging.getLogger(__name__)
REFRESH_DAYS = 7
_URL = "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w?scripcode={code}"
_BONUS = re.compile(r"bonus\s*(?:issue)?\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)
_FV = re.compile(r"from\s*r[se]\.?\s*(\d+(?:\.\d+)?).*?to\s*r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_RIGHTS_RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
_UNSIZED = re.compile(r"reduction of capital|capital reduction|scheme|arrangement|amalgamation|demerger", re.I)
_lock = threading.Lock()


def parse(purpose: str, details) -> tuple[str, float | None, str] | None:
    """BSE purpose → (kind, share multiplier or ₹ amount, label); None when it isn't something we track."""
    p = " ".join(str(purpose or "").split())
    low = p.lower()
    if any(w in low for w in ("debenture", "preference", "ncd", "warrant", "bond")):
        return None
    if "bonus" in low:
        m = _BONUS.search(p)
        return ("bonus", (float(m.group(1)) + float(m.group(2))) / float(m.group(2)), p) if m else ("unsized", None, p)
    if "split" in low or "sub-division" in low or "consolidation" in low:
        m = _FV.search(p)
        return ("split" if "consolidation" not in low else "consolidation",
                float(m.group(1)) / float(m.group(2)), p) if m else ("unsized", None, p)
    if "right" in low:
        m = _RIGHTS_RATIO.search(p)
        return ("rights", (float(m.group(1)) + float(m.group(2))) / float(m.group(2)) if m else None, p)
    if "dividend" in low:
        try:
            amt = float(str(details).strip())
        except (TypeError, ValueError):
            return None
        return ("dividend", amt, p) if amt > 0 else None
    if _UNSIZED.search(p):
        return ("unsized", None, p)
    return None


def code_for(con: duckdb.DuckDBPyConnection, symbol: str) -> str | None:
    """The BSE scrip code: from a ``BSE:<code>`` symbol, else by the NSE symbol's ISIN."""
    if symbol.startswith("BSE:"):
        return symbol.split(":", 1)[1]
    r = con.execute("""SELECT b.code FROM equity_master m JOIN bse_codes b ON b.isin = m.isin
                       WHERE m.symbol = ? UNION ALL
                       SELECT b.code FROM instruments i JOIN bse_codes b ON b.isin = i.isin WHERE i.symbol = ?""",
                    [symbol, symbol]).fetchone()
    return r[0] if r else None


def _d(s) -> date | None:
    try:
        return datetime.strptime(str(s).strip(), "%d %b %Y").date()
    except (TypeError, ValueError):
        return None


def store(con: duckdb.DuckDBPyConnection, symbol: str, rows) -> int:
    """Keep what NSE's record lacks from BSE's ``Table2`` rows. Returns how many records were added."""
    n = 0
    for r in rows if isinstance(rows, list) else []:
        ex, got = _d(r.get("Ex_date")), parse(r.get("purpose"), r.get("Details"))
        if not ex or not got:
            continue
        kind, value, label = got
        if kind == "dividend":
            if not con.execute("SELECT count(*) FROM dividends WHERE symbol = ? AND ex_date = ?",
                               [symbol, ex]).fetchone()[0]:
                con.execute("INSERT INTO dividends VALUES (?, ?, ?, ?)", [symbol, ex, value, f"{label} (BSE)"])
                n += 1
            continue
        near = con.execute("""SELECT count(*) FROM price_adjustments WHERE symbol = ? AND source <> 'bse'
                              AND ex_date BETWEEN ? - INTERVAL 7 DAY AND ? + INTERVAL 7 DAY""",
                           [symbol, ex, ex]).fetchone()[0]
        if near:
            continue                                   # NSE has it — don't count it twice
        factor = 1 / value if kind in ("bonus", "split", "consolidation") and value else None
        share_mult = value if kind in ("bonus", "split", "consolidation") else None
        con.execute("INSERT OR IGNORE INTO price_adjustments VALUES (?, ?, ?, ?, ?, 'bse', ?)",
                    [symbol, ex, factor, share_mult, kind, f"{label} (BSE)"])
        n += 1
    con.execute("INSERT OR REPLACE INTO bse_action_fetch VALUES (?, now())", [symbol])
    return n


def stale(con: duckdb.DuckDBPyConnection, symbols: list[str]) -> list[str]:
    fresh = {r[0] for r in con.execute("SELECT symbol FROM bse_action_fetch WHERE fetched_at > ?",
                                       [datetime.now() - timedelta(days=REFRESH_DAYS)]).fetchall()}
    return sorted(s for s in set(symbols) if s not in fresh)


def refresh(con: duckdb.DuckDBPyConnection, symbols: list[str]) -> int:
    from equity_research.common.http import fetch_json
    from equity_research.scrapers.bse import _HEADERS

    n = 0
    for sym in symbols:
        code = code_for(con, sym)
        if not code:
            con.execute("INSERT OR REPLACE INTO bse_action_fetch VALUES (?, now())", [sym])
            continue
        try:
            data = fetch_json(_URL.format(code=code), headers=_HEADERS)
        except Exception:  # noqa: BLE001 — retried next time
            log.warning("BSE corporate actions: fetch failed for %s", sym)
            continue
        n += store(con, sym, (data or {}).get("Table2"))
    log.info("BSE corporate actions: %d record(s) added for %d stock(s)", n, len(symbols))
    return n


def refresh_async(symbols: list[str]) -> None:
    if not symbols or not _lock.acquire(blocking=False):
        return

    def run():
        from equity_research.common.db import connect

        con = connect()
        try:
            refresh(con, symbols)
        except Exception:  # noqa: BLE001
            log.exception("BSE corporate actions: refresh failed")
        finally:
            con.close()
            _lock.release()

    threading.Thread(target=run, name="bse-actions", daemon=True).start()
