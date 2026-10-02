"""What you can hold beyond NSE main-board shares — so the add box finds them and the page can price them.

* **ETFs** — NSE's ETF list (names from the underlying, e.g. "Nifty 50 ETF").
* **SME shares** — NSE Emerge's list.
* **REITs / InvITs** — the ``RR`` / ``IV`` symbols in NSE's daily bhavcopy, named from NSE's per-symbol record.
* **BSE-only shares** — BSE's list of active scrips whose ISIN isn't listed on NSE, stored as ``BSE:<scrip code>``
  and priced from BSE's daily bhavcopy (``bse_prices``). NSE corporate actions don't cover them, so splits /
  bonuses aren't applied automatically — enter them as your broker shows today, or as bought if none happened.

NSE-listed instruments are priced from the same bhavcopy as everything else (``equity_eod_adj``). Refreshed
weekly in the background by the bot; BSE prices daily.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, timedelta

import duckdb
import pandas as pd

log = logging.getLogger(__name__)
SERIES = ("EQ", "BE", "BZ", "SM", "ST", "RR", "IV")      # shares, SME, ETFs (EQ), REITs (RR), InvITs (IV)
KIND_LABEL = {"etf": "ETF", "sme": "SME", "reit": "REIT", "invit": "InvIT", "bse": "BSE only"}
_SME_LIST = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
_BSE_LIST = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry="
             "&segment=Equity&status=Active")
_BSE_BHAV = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"


def is_bse(symbol: str) -> bool:
    return str(symbol).startswith("BSE:")


def _store(con, rows: list[tuple[str, str, str | None, str]]) -> int:
    if rows:
        con.executemany("INSERT OR REPLACE INTO instruments VALUES (?, ?, ?, ?, now())", rows)
    return len(rows)


def refresh_nse(con: duckdb.DuckDBPyConnection) -> int:
    """ETFs and SME shares from NSE's lists; REIT / InvIT names from NSE's per-symbol records."""
    from equity_research.common.http import fetch_bytes
    from equity_research.scrapers import nse_api, nse_archives

    rows: list[tuple] = []
    try:
        etf = nse_archives._read_csv(fetch_bytes(nse_archives._ETF_LIST))
        cols = {c.strip().lower(): c for c in etf.columns}
        for _, r in etf.iterrows():
            sym = str(r[cols["symbol"]]).strip().upper()
            under = str(r.get(cols.get("underlying asset", ""), "") or "").strip()
            name = f"{sym} — {under} ETF" if under and under.lower() != "nan" else f"{sym} ETF"
            rows.append((sym, name, str(r.get(cols.get("isinnumber", ""), "") or "") or None, "etf"))
    except Exception:  # noqa: BLE001
        log.exception("instruments: NSE ETF list failed")
    try:
        sme = nse_archives._read_csv(fetch_bytes(_SME_LIST))
        cols = {c.strip().upper(): c for c in sme.columns}
        for _, r in sme.iterrows():
            rows.append((str(r[cols["SYMBOL"]]).strip().upper(), str(r[cols["NAME_OF_COMPANY"]]).strip(),
                         str(r.get(cols.get("ISIN_NUMBER", ""), "") or "") or None, "sme"))
    except Exception:  # noqa: BLE001
        log.exception("instruments: NSE SME list failed")
    trusts = con.execute("""SELECT DISTINCT symbol, series FROM equity_eod WHERE series IN ('RR', 'IV')
                            AND trade_date >= (SELECT max(trade_date) FROM equity_eod) - INTERVAL 30 DAY""").fetchall()
    known = {r[0] for r in con.execute("SELECT symbol FROM instruments WHERE kind IN ('reit', 'invit')").fetchall()}
    todo = [(s, k) for s, k in trusts if s not in known]
    if todo:
        try:
            got = nse_api.fetch_api_multi(
                {s: f"/api/corporates-corporateActions?index=equities&symbol={nse_api.q(s)}" for s, _ in todo})
        except Exception:  # noqa: BLE001
            got = {}
        for s, series in todo:
            recs = got.get(s) if isinstance(got.get(s), list) else []
            name = next((str(r.get("comp")).strip() for r in recs if r.get("comp")), s)
            rows.append((s, name, None, "reit" if series == "RR" else "invit"))
    n = _store(con, rows)
    log.info("instruments: %d NSE ETFs / SME / REITs / InvITs", n)
    return n


def refresh_bse(con: duckdb.DuckDBPyConnection) -> int:
    """BSE's active scrips whose ISIN isn't on NSE → ``BSE:<code>``."""
    from equity_research.common.http import fetch_json
    from equity_research.scrapers.bse import _HEADERS

    data = fetch_json(_BSE_LIST, headers=_HEADERS)
    nse_isins = {r[0] for r in con.execute(
        "SELECT isin FROM equity_master WHERE isin IS NOT NULL UNION SELECT isin FROM instruments "
        "WHERE isin IS NOT NULL AND kind <> 'bse'").fetchall()}
    rows, codes = [], []
    for r in data if isinstance(data, list) else []:
        isin, code = str(r.get("ISIN_NUMBER") or "").strip(), str(r.get("SCRIP_CD") or "").strip()
        name = str(r.get("Issuer_Name") or r.get("Scrip_Name") or "").strip()
        if code and isin.startswith("INE"):
            codes.append((isin, code, name))        # every scrip — BSE's corporate-action record for any holding
        if code and name and isin.startswith("INE") and isin not in nse_isins:
            rows.append((f"BSE:{code}", name, isin, "bse"))
    if codes:
        con.executemany("INSERT OR REPLACE INTO bse_codes VALUES (?, ?, ?)", codes)
    n = _store(con, rows)
    log.info("instruments: %d BSE-only shares", n)
    return n


def refresh_bse_prices(con: duckdb.DuckDBPyConnection, *, days_back: int = 6) -> int:
    """The latest BSE bhavcopy's closes for BSE-only shares (walks back over holidays / weekends)."""
    from equity_research.common.http import fetch_bytes
    from equity_research.scrapers.bse import _HEADERS

    codes = {r[0].split(":", 1)[1]: r[0] for r in con.execute(
        "SELECT symbol FROM instruments WHERE kind = 'bse'").fetchall()}
    if not codes:
        return 0
    for back in range(days_back + 1):
        d = date.today() - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            df = pd.read_csv(io.BytesIO(fetch_bytes(_BSE_BHAV.format(d=d), headers=_HEADERS)))
        except Exception:  # noqa: BLE001 — not published (holiday / not yet) → the day before
            continue
        df = df[df["FinInstrmId"].astype(str).isin(codes)]
        rows = [(codes[str(c)], d, float(p)) for c, p in zip(df["FinInstrmId"], df["ClsPric"]) if p == p]
        con.executemany("INSERT OR REPLACE INTO bse_prices VALUES (?, ?, ?)", rows)
        log.info("BSE prices %s: %d", d, len(rows))
        return len(rows)
    return 0


def search(con: duckdb.DuckDBPyConnection, q: str, limit: int = 6) -> list[dict]:
    """ETFs, SME, REITs, InvITs and BSE-only shares whose name has every word typed (or whose symbol is typed)."""
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if w]
    if not words:
        return []
    cond = " AND ".join(["lower(name) LIKE ?"] * len(words))
    rows = con.execute(
        f"""SELECT symbol, name, kind FROM instruments WHERE ({cond}) OR upper(symbol) = ?
            ORDER BY (upper(symbol) = ?) DESC, (lower(name) LIKE ?) DESC, length(name) LIMIT ?""",
        [f"%{w}%" for w in words] + [q.strip().upper(), q.strip().upper(), f"{q.strip().lower()}%", limit]).fetchall()
    return [{"symbol": s, "name": n, "kind": k, "note": KIND_LABEL.get(k, k)} for s, n, k in rows]


def find(con: duckdb.DuckDBPyConnection, symbol: str) -> tuple[str, str] | None:
    r = con.execute("SELECT symbol, name FROM instruments WHERE symbol = ?", [symbol]).fetchone()
    return (r[0], r[1]) if r else None


def last_close(con: duckdb.DuckDBPyConnection, symbol: str) -> tuple[float, date] | None:
    """The latest close: BSE-only shares from ``bse_prices``, everything else from NSE (split-adjusted)."""
    if is_bse(symbol):
        r = con.execute("SELECT close, trade_date FROM bse_prices WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1",
                        [symbol]).fetchone()
    else:
        r = con.execute(f"""SELECT close, trade_date FROM equity_eod_adj WHERE symbol = ?
                            AND series IN {SERIES}
                            ORDER BY trade_date DESC, CASE series WHEN 'EQ' THEN 0 ELSE 1 END LIMIT 1""",
                        [symbol]).fetchone()
    return (float(r[0]), r[1]) if r else None
