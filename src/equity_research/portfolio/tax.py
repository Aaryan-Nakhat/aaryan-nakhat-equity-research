"""Indian capital-gains rules for listed shares, as used by the holdings page, the realised-gains table and
the ``raise ₹X`` plan. Estimates for planning — not tax advice.

* **Term.** Held more than 12 months → long-term; otherwise short-term.
* **Rates.** ``config.STCG_RATE`` / ``LTCG_RATE`` + cess, with ``LTCG_EXEMPTION`` of long-term gain tax-free per
  financial year (defaults: the rates from 23-Jul-2024 — 20 % / 12.5 % / ₹1.25 lakh / 4 %). Surcharge isn't modelled.
* **Set-off.** Short-term losses against short-term gains, then long-term gains; long-term losses against long-term
  gains only.
* **Grandfathering.** For shares acquired on or before 31-Jan-2018, the cost for long-term gains is
  ``max(actual cost, min(FMV, sale price))`` where FMV is the highest price quoted on 31-Jan-2018. Bonus shares
  allotted by then qualify too.
* **Bonus shares** cost nothing and their holding period starts on allotment; **split** shares keep the original
  cost (spread) and date. **Rights shares** are a new buy at the price paid.
* **Buybacks** (tendered shares) depend on the date: before 1-Oct-2024 the company paid the tax and the proceeds
  were exempt for you; from 1-Oct-2024 to 31-Mar-2026 the whole amount was a deemed dividend (taxed at your slab)
  and the shares' cost a capital loss; from 1-Apr-2026 ordinary capital gains again.
* **The law.** From 1-Apr-2026 the Income-tax Act 2025 replaces the 1961 Act — long-term gains on listed shares
  (s.112A) are s.198 there, and the year is called a "tax year".
"""

from __future__ import annotations

import io
import logging
import threading
import zipfile
from datetime import date

import duckdb

log = logging.getLogger(__name__)
LONG_TERM_DAYS = 365                 # held MORE than 12 months → long-term
GRANDFATHER_DATE = date(2018, 1, 31)
BUYBACK_DIVIDEND_FROM = date(2024, 10, 1)
NEW_ACT_FROM = date(2026, 4, 1)
_FMV_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/2018/JAN/cm31JAN2018bhav.csv.zip"
_fmv_lock = threading.Lock()


def term(acquired: date | None, on: date) -> str:
    """'long' / 'short' for shares acquired on ``acquired`` and sold (or valued) on ``on``; 'unknown' undated."""
    if not acquired:
        return "unknown"
    return "long" if (on - acquired).days > LONG_TERM_DAYS else "short"


def days_to_long(acquired: date | None, on: date) -> int | None:
    if not acquired or (on - acquired).days > LONG_TERM_DAYS:
        return None
    return LONG_TERM_DAYS + 1 - (on - acquired).days


def fy_label(d: date) -> str:
    """The financial (tax) year a date falls in: 'FY 2025-26' / from Apr-2026 'Tax year 2026-27'."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{'Tax year' if date(y, 4, 1) >= NEW_ACT_FROM else 'FY'} {y}-{(y + 1) % 100:02d}"


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def ltcg_section(d: date) -> str:
    return "s.198, Income-tax Act 2025" if d >= NEW_ACT_FROM else "s.112A, Income-tax Act 1961"


def buyback_treatment(sold: date) -> str:
    """'exempt' (company paid the tax) · 'dividend' (proceeds = deemed dividend, cost = capital loss) ·
    'capital_gains' (ordinary)."""
    if sold < BUYBACK_DIVIDEND_FROM:
        return "exempt"
    return "dividend" if sold < NEW_ACT_FROM else "capital_gains"


def tax_cost_ps(cost_ps: float, sale_ps: float, acquired: date | None, fmv_ps: float | None) -> float:
    """Cost per share for the gain: grandfathered for shares acquired on or before 31-Jan-2018."""
    if acquired and acquired <= GRANDFATHER_DATE and fmv_ps:
        return max(cost_ps, min(fmv_ps, sale_ps))
    return cost_ps


def tax_estimate(sales: list[dict], *, exemption: float | None = None) -> dict:
    """``sales`` = [{gain, term: short|long|unknown}] → {st_gain, lt_gain, unknown_gain, taxable_lt,
    exemption_used, tax}. Set-off: short-term losses against short-term gains, then long-term gains;
    long-term losses against long-term gains only."""
    from equity_research import config

    ex = config.LTCG_EXEMPTION if exemption is None else exemption
    st_g = sum(s["gain"] for s in sales if s["term"] == "short" and s["gain"] > 0)
    st_l = -sum(s["gain"] for s in sales if s["term"] == "short" and s["gain"] < 0)
    lt_g = sum(s["gain"] for s in sales if s["term"] == "long" and s["gain"] > 0)
    lt_l = -sum(s["gain"] for s in sales if s["term"] == "long" and s["gain"] < 0)
    unknown = sum(s["gain"] for s in sales if s["term"] == "unknown")
    net_st = max(0.0, st_g - st_l)
    net_lt = max(0.0, lt_g - lt_l - max(0.0, st_l - st_g))
    taxable_lt = max(0.0, net_lt - ex)
    tax = (config.STCG_RATE * net_st + config.LTCG_RATE * taxable_lt) * (1 + config.TAX_CESS)
    return {"st_gain": net_st, "lt_gain": net_lt, "unknown_gain": unknown, "taxable_lt": taxable_lt,
            "exemption_used": min(net_lt, ex), "tax": tax}


# ------------------------------------------------------------------ 31-Jan-2018 prices (grandfathering)
def fmv_loaded(con: duckdb.DuckDBPyConnection) -> bool:
    return con.execute("SELECT count(*) FROM fmv_2018").fetchone()[0] > 0


def load_fmv(con: duckdb.DuckDBPyConnection) -> int:
    """Store NSE's 31-Jan-2018 bhavcopy highs (equity series) once. Returns rows stored."""
    import pandas as pd

    from equity_research.common.http import fetch_bytes

    raw = fetch_bytes(_FMV_URL)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns = [c.strip().upper() for c in df.columns]
    df = df[df["SERIES"].astype(str).str.strip().isin(["EQ", "BE", "BZ", "SM", "ST"])]
    rows = [(str(s).strip(), str(i).strip(), float(h)) for s, i, h in zip(df["SYMBOL"], df["ISIN"], df["HIGH"])]
    con.execute("DELETE FROM fmv_2018")
    con.executemany("INSERT INTO fmv_2018 VALUES (?, ?, ?)", rows)
    log.info("31-Jan-2018 prices: %d stored", len(rows))
    return len(rows)


def load_fmv_async() -> None:
    """Load the 31-Jan-2018 prices in the background (once) — the page shows a note meanwhile."""
    if not _fmv_lock.acquire(blocking=False):
        return

    def run():
        from equity_research.common.db import connect

        con = connect()
        try:
            if not fmv_loaded(con):
                load_fmv(con)
        except Exception:  # noqa: BLE001 — retried on a later page load
            log.exception("31-Jan-2018 prices: load failed")
        finally:
            con.close()
            _fmv_lock.release()

    threading.Thread(target=run, name="fmv-2018", daemon=True).start()


def fmv_for(con: duckdb.DuckDBPyConnection, symbol: str) -> float | None:
    """The 31-Jan-2018 high for this share (per share as it was then): by ISIN, else by symbol (a face-value
    split changes the ISIN, not usually the symbol). None if it didn't trade on NSE that day."""
    isin = con.execute("SELECT isin FROM equity_master WHERE symbol = ?", [symbol]).fetchone()
    for sql, arg in (("SELECT max(high) FROM fmv_2018 WHERE isin = ?", isin[0] if isin else None),
                     ("SELECT max(high) FROM fmv_2018 WHERE symbol = ?", symbol)):
        if arg:
            r = con.execute(sql, [arg]).fetchone()
            if r and r[0]:
                return float(r[0])
    return None
