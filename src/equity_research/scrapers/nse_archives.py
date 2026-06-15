"""NSE archive-file scraper (``nsearchives.nseindia.com``).

These are static CSV files served over plain HTTP — they bypass the Akamai bot
wall on ``www.nseindia.com/api/`` entirely. This is how we get the EOD bhavcopy
*with delivery %* (the NSE-exclusive datum) and daily index closes.

See ``docs/SCRAPING.md`` for the probe that validated this path.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd

from equity_research.common.http import fetch_bytes

_BHAVCOPY = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
_INDEX_CLOSE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d}.csv"
_PARTICIPANT_OI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"
_FO_BHAVCOPY = ("https://nsearchives.nseindia.com/content/fo/"
                "BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d%m%Y")


def _read_csv(raw: bytes) -> pd.DataFrame:
    # NSE CSVs pad every field with a leading space after each comma, so both
    # headers (" SERIES") and values (" 51.55") need trimming. skipinitialspace
    # handles the values (and lets numerics parse as numbers); strip the headers.
    df = pd.read_csv(io.BytesIO(raw), skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df


def fetch_bhavcopy(d: date) -> pd.DataFrame:
    """Full EOD bhavcopy for trade date ``d`` (incl. ``DELIV_QTY``/``DELIV_PER``).

    Columns: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE,
    LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,
    NO_OF_TRADES, DELIV_QTY, DELIV_PER.
    """
    raw = fetch_bytes(_BHAVCOPY.format(d=_ddmmyyyy(d)))
    df = _read_csv(raw)
    # Some series (e.g. govt securities) report '-' for delivery; coerce to NaN
    # so the columns stay numeric for analysis.
    for col in ("DELIV_QTY", "DELIV_PER"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_index_closes(d: date) -> pd.DataFrame:
    """Daily close values for all NSE indices on trade date ``d``."""
    raw = fetch_bytes(_INDEX_CLOSE.format(d=_ddmmyyyy(d)))
    return _read_csv(raw)


_CONSTITUENTS = "https://nsearchives.nseindia.com/content/indices/ind_{index}list.csv"


def fetch_constituents(index: str = "nifty500") -> pd.DataFrame:
    """Index constituents with industry classification (Company Name, Industry,
    Symbol, Series, ISIN Code). ``index`` e.g. 'nifty500', 'nifty50'."""
    raw = fetch_bytes(_CONSTITUENTS.format(index=index))
    return pd.read_csv(io.BytesIO(raw))


def fetch_participant_oi(d: date) -> pd.DataFrame:
    """Participant-wise F&O open interest (Client / DII / FII / Pro) for ``d``.

    The file's first line is a title; the real header is the second line, with
    trailing-space column names. Columns cover Future/Option Index/Stock
    Long/Short OI by participant — the raw input for FII derivatives positioning.
    """
    raw = fetch_bytes(_PARTICIPANT_OI.format(d=_ddmmyyyy(d)))
    df = pd.read_csv(io.BytesIO(raw), skiprows=1, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    return df


def fetch_fo_bhavcopy(d: date) -> pd.DataFrame:
    """Full F&O (derivatives) bhavcopy for ``d`` — the UDiFF zipped CSV.

    One row per contract (futures + options): TckrSymb, XpryDt, StrkPric,
    OptnTp, OHLC, settlement, OpnIntrst, ChngInOpnIntrst, etc.
    """
    raw = fetch_bytes(_FO_BHAVCOPY.format(ymd=d.strftime("%Y%m%d")))
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        inner = zf.namelist()[0]
        data = zf.read(inner)
    return pd.read_csv(io.BytesIO(data))
