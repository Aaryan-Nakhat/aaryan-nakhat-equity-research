"""Indian capital-gains rules for listed shares, as used by the holdings page, the realised-gains table and
the ``raise ₹X`` plan. Estimates for planning — not tax advice.

* **Term.** Held for more than 12 calendar months → long-term (bought 1-Mar-2023, sold 1-Mar-2024 is exactly 12
  months: short-term); otherwise short-term.
* **Rates, by sale date.** From 23-Jul-2024: ``config.STCG_RATE`` / ``LTCG_RATE`` (defaults 20 % / 12.5 %); before it:
  15 % / 10 %. Plus cess. Long-term gains tax-free per financial year: ₹1 lakh up to FY 2023-24, then
  ``config.LTCG_EXEMPTION`` (₹1.25 lakh). Surcharge isn't modelled.
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
LONG_TERM_DAYS = 365                 # ≈ a year, for annualising returns (the tax term uses calendar months)
RATE_CHANGE = date(2024, 7, 23)      # Finance (No. 2) Act 2024: 15 → 20 % short-term, 10 → 12.5 % long-term
OLD_STCG, OLD_LTCG, OLD_EXEMPTION = 0.15, 0.10, 100000.0
GRANDFATHER_DATE = date(2018, 1, 31)
BUYBACK_DIVIDEND_FROM = date(2024, 10, 1)
NEW_ACT_FROM = date(2026, 4, 1)
_FMV_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/2018/JAN/cm31JAN2018bhav.csv.zip"
_fmv_lock = threading.Lock()


def _twelve_months(acquired: date) -> date:
    from dateutil.relativedelta import relativedelta

    return acquired + relativedelta(months=12)


def term(acquired: date | None, on: date) -> str:
    """'long' / 'short' for shares acquired on ``acquired`` and sold (or valued) on ``on``; 'unknown' undated.
    Long-term means held for *more than* 12 months: sold after the same date a year later."""
    if not acquired:
        return "unknown"
    return "long" if on > _twelve_months(acquired) else "short"


def days_to_long(acquired: date | None, on: date) -> int | None:
    """Days until a sale would be long-term (the day after the 12-month anniversary); None once it is."""
    if not acquired or on > _twelve_months(acquired):
        return None
    return (_twelve_months(acquired) - on).days + 1


def rates_on(sold: date | None) -> tuple[float, float]:
    """(short-term rate, long-term rate) for a sale on that date — before 23-Jul-2024 the older 15 % / 10 %."""
    from equity_research import config

    if sold and sold < RATE_CHANGE:
        return OLD_STCG, OLD_LTCG
    return config.STCG_RATE, config.LTCG_RATE


def exemption_for(sold: date | None) -> float:
    """Long-term gain tax-free in that sale's financial year: ₹1 lakh up to FY 2023-24, then the current figure."""
    from equity_research import config

    return OLD_EXEMPTION if sold and fy_start(sold) < date(2024, 4, 1) else config.LTCG_EXEMPTION


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
    """``sales`` = [{gain, term: short|long|unknown, date?}] → {st_gain, lt_gain, unknown_gain, taxable_lt,
    exemption_used, tax}. Set-off: short-term losses against short-term gains, then long-term gains;
    long-term losses against long-term gains only. Each sale is taxed at the rates in force on its date (no date:
    today's); what's left after set-off and the exemption is spread over the gains in proportion."""
    from equity_research import config

    dates = [s.get("date") for s in sales if s.get("date")]
    ex = (exemption if exemption is not None
          else exemption_for(max(dates)) if dates else config.LTCG_EXEMPTION)
    st_g = sum(s["gain"] for s in sales if s["term"] == "short" and s["gain"] > 0)
    st_l = -sum(s["gain"] for s in sales if s["term"] == "short" and s["gain"] < 0)
    lt_g = sum(s["gain"] for s in sales if s["term"] == "long" and s["gain"] > 0)
    lt_l = -sum(s["gain"] for s in sales if s["term"] == "long" and s["gain"] < 0)
    unknown = sum(s["gain"] for s in sales if s["term"] == "unknown")
    net_st = max(0.0, st_g - st_l)
    net_lt = max(0.0, lt_g - lt_l - max(0.0, st_l - st_g))
    taxable_lt = max(0.0, net_lt - ex)

    def blended(kind: str, idx: int) -> float:
        """The rate on this term's positive gains, weighted by their size (rates follow each sale's date)."""
        pos = [(s["gain"], rates_on(s.get("date"))[idx]) for s in sales if s["term"] == kind and s["gain"] > 0]
        tot = sum(g for g, _ in pos)
        return sum(g * r for g, r in pos) / tot if tot else rates_on(None)[idx]

    tax = (blended("short", 0) * net_st + blended("long", 1) * taxable_lt) * (1 + config.TAX_CESS)
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
