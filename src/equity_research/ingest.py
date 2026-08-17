"""Ingest — scrape a trade date's EOD data and land it in DuckDB.

Each function fetches via ``scrapers``, renames columns to the schema in
``common.db``, and writes idempotently (re-running a date overwrites it).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import duckdb
import pandas as pd

from equity_research.common.db import replace_for_date
from equity_research.common.http import ScrapeError, fetch_bytes
from equity_research.scrapers import amfi, nse_api, nse_archives, nse_financials

log = logging.getLogger("equity-research")

# Source-column -> schema-column maps (schema order preserved on write).
_EOD_MAP = {
    "SYMBOL": "symbol", "SERIES": "series", "PREV_CLOSE": "prev_close",
    "OPEN_PRICE": "open", "HIGH_PRICE": "high", "LOW_PRICE": "low",
    "LAST_PRICE": "last", "CLOSE_PRICE": "close", "AVG_PRICE": "avg_price",
    "TTL_TRD_QNTY": "ttl_trd_qnty", "TURNOVER_LACS": "turnover_lacs",
    "NO_OF_TRADES": "no_of_trades", "DELIV_QTY": "deliv_qty", "DELIV_PER": "deliv_per",
}
_INDEX_MAP = {
    "Index Name": "index_name", "Open Index Value": "open",
    "High Index Value": "high", "Low Index Value": "low",
    "Closing Index Value": "close", "Points Change": "points_change",
    "Change(%)": "pct_change", "Volume": "volume",
    "Turnover (Rs. Cr.)": "turnover_cr", "P/E": "pe", "P/B": "pb",
    "Div Yield": "div_yield",
}
_POI_MAP = {
    "Client Type": "client_type", "Future Index Long": "fut_idx_long",
    "Future Index Short": "fut_idx_short", "Future Stock Long": "fut_stk_long",
    "Future Stock Short": "fut_stk_short", "Option Index Call Long": "opt_idx_call_long",
    "Option Index Put Long": "opt_idx_put_long", "Option Index Call Short": "opt_idx_call_short",
    "Option Index Put Short": "opt_idx_put_short", "Option Stock Call Long": "opt_stk_call_long",
    "Option Stock Put Long": "opt_stk_put_long", "Option Stock Call Short": "opt_stk_call_short",
    "Option Stock Put Short": "opt_stk_put_short", "Total Long Contracts": "total_long",
    "Total Short Contracts": "total_short",
}


def _prepare(df: pd.DataFrame, colmap: dict[str, str], d: date,
             numeric: list[str]) -> pd.DataFrame:
    """Select+rename mapped columns, coerce numerics, prepend trade_date."""
    out = df[list(colmap)].rename(columns=colmap)
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out.insert(0, "trade_date", d)
    return out


def ingest_bhavcopy(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_bhavcopy(d)
    num = ["prev_close", "open", "high", "low", "last", "close", "avg_price",
           "ttl_trd_qnty", "turnover_lacs", "no_of_trades", "deliv_qty", "deliv_per"]
    out = _prepare(df, _EOD_MAP, d, num)
    out = out.dropna(subset=["symbol", "series"])   # some files carry junk/total rows
    return replace_for_date(con, "equity_eod", out, d)


def ingest_index_closes(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_index_closes(d)
    num = ["open", "high", "low", "close", "points_change", "pct_change",
           "volume", "turnover_cr", "pe", "pb", "div_yield"]
    return replace_for_date(con, "index_close", _prepare(df, _INDEX_MAP, d, num), d)


def backfill_index_history(con: duckdb.DuckDBPyConnection, *, years: float = 5.0,
                           only_missing: bool = True, progress_every: int = 100) -> dict:
    """Backfill daily ``index_close`` from the NSE archive so benchmark-relative fund
    metrics (alpha/beta/capture) have multi-year depth instead of ~1y.

    Walks business days from ``years`` ago to today, fetching each
    ``ind_close_all_DDMMYYYY.csv``. Non-trading days simply 404 and are skipped
    (that's how holidays are detected — no calendar needed). Idempotent: with
    ``only_missing`` it skips dates already stored, so re-runs are cheap. Returns a
    small stats dict."""
    start = (pd.Timestamp.today().normalize() - pd.DateOffset(years=years)).date()
    end = date.today()
    have: set[date] = set()
    if only_missing:
        have = {r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM index_close WHERE trade_date >= ?", [start]).fetchall()}
    days = [d.date() for d in pd.bdate_range(start=start, end=end)]  # Mon–Fri
    todo = [d for d in days if d not in have]
    ingested = holidays = failed = rows = 0
    for i, d in enumerate(todo, 1):
        try:
            rows += ingest_index_closes(d, con)
            ingested += 1
        except ScrapeError:
            holidays += 1                      # 404 = market holiday / weekend-adjacent
        except Exception:  # noqa: BLE001 — one bad file shouldn't abort a long backfill
            failed += 1
        if progress_every and i % progress_every == 0:
            print(f"  index backfill: {i}/{len(todo)} days "
                  f"({ingested} ingested, {holidays} holidays, {failed} failed)")
    return {"range": (start, end), "candidates": len(todo), "ingested": ingested,
            "holidays": holidays, "failed": failed, "rows": rows,
            "already_had": len(have)}


def ingest_participant_oi(d: date, con: duckdb.DuckDBPyConnection) -> int:
    df = nse_archives.fetch_participant_oi(d)
    num = [c for c in _POI_MAP.values() if c != "client_type"]
    return replace_for_date(con, "participant_oi", _prepare(df, _POI_MAP, d, num), d)


def ingest_financials(symbol: str, con: duckdb.DuckDBPyConnection, *,
                      period: str = "Quarterly", max_filings: int | None = None) -> int:
    """Land structured quarterly financial line items for ``symbol`` (long format).

    Lists result filings (browser), downloads + parses each XBRL (plain HTTP),
    and stores the **current-quarter** facts (the OneD context) per filing —
    giving a clean, non-overlapping quarterly series. Annual figures are derived
    downstream by summing four quarters. Returns rows written.
    """
    filings = nse_financials.list_all_result_filings(symbol, period=period)
    filings = [f for f in filings if f.xbrl_url and f.to_date]
    if max_filings:
        filings = filings[:max_filings]

    rows: list[dict] = []
    for f in filings:
        try:
            parsed = nse_financials.parse_result_xbrl(fetch_bytes(f.xbrl_url))
        except (ScrapeError, ValueError):
            continue
        facts = parsed.current_quarter()      # OneD = the reported quarter
        if not facts:
            continue
        for element, value in facts.items():
            rows.append({
                "symbol": symbol, "period_end": f.to_date, "period_start": f.from_date,
                "period_type": "Q", "consolidated": f.consolidated,
                "element": element, "value": value,
                "filing_date": f.filing_date, "source_url": f.xbrl_url,
            })
    return _write_financials(con, rows)


def _write_financials(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "period_end", "period_start",
                                     "period_type", "consolidated", "element",
                                     "value", "filing_date", "source_url"])
    con.register("_fin", df)
    try:
        con.execute("INSERT OR REPLACE INTO financials SELECT * FROM _fin")
    finally:
        con.unregister("_fin")
    return len(df)


def ingest_annual_financials(symbol: str, con: duckdb.DuckDBPyConnection, *,
                             max_filings: int | None = None) -> int:
    """Land annual full-year P&L + cash-flow + year-end balance sheet (period_type='Y').

    Per annual filing: the full-year flows live in the FourD context; the
    year-end balance sheet is the instant context dated at the filing's to_date.
    One filing = one fiscal year; N filings = N years of history.
    """
    filings = nse_financials.list_all_result_filings(symbol, period="Annual")
    filings = [f for f in filings if f.xbrl_url and f.to_date]
    if max_filings:
        filings = filings[:max_filings]

    rows: list[dict] = []
    for f in filings:
        try:
            parsed = nse_financials.parse_result_xbrl(fetch_bytes(f.xbrl_url))
        except (ScrapeError, ValueError):
            continue
        facts = dict(parsed.facts_by_context.get(nse_financials.CURRENT_YEAR_CTX, {}))
        facts.update(parsed.current_balance_sheet())     # + year-end balance sheet (OneI)
        if not facts:
            continue
        for element, value in facts.items():
            rows.append({
                "symbol": symbol, "period_end": f.to_date, "period_start": None,
                "period_type": "Y", "consolidated": f.consolidated,
                "element": element, "value": value,
                "filing_date": f.filing_date, "source_url": f.xbrl_url,
            })
    return _write_financials(con, rows)


def ingest_eod_on_or_before(d: date, con: duckdb.DuckDBPyConnection, *,
                            lookback: int = 7) -> date | None:
    """Ingest the bhavcopy for ``d`` or the nearest earlier trading day.

    Fiscal year-ends (31-Mar) are often holidays; step back up to ``lookback``
    days until a bhavcopy exists. Returns the date ingested, or None.
    """
    from datetime import timedelta
    for i in range(lookback + 1):
        day = d - timedelta(days=i)
        try:
            ingest_bhavcopy(day, con)
            return day
        except ScrapeError:
            continue
    return None


def _pledge_row(symbol: str, p: dict | None) -> dict | None:
    if not p or not p.get("as_of"):
        return None
    try:
        period_end = datetime.strptime(p["as_of"], "%d-%b-%Y").date()
    except (TypeError, ValueError):
        return None
    return {
        "symbol": symbol, "period_end": period_end,
        "promoter_holding_pct": p.get("promoter_holding_pct"),
        "pledged_pct_of_promoter": p.get("pledged_pct_of_promoter"),
        "pledged_pct_of_total": p.get("pledged_pct_of_total"),
        "num_shares_pledged": p.get("num_shares_pledged"),
        "broadcast_dt": p.get("broadcast_dt"),
        "source_url": f"nse:/api/corporate-pledgedata?symbol={symbol}",
    }


def _write_shareholding(con: duckdb.DuckDBPyConnection, rows: list[dict | None]) -> int:
    rows = [r for r in rows if r]
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "period_end", "promoter_holding_pct",
                                     "pledged_pct_of_promoter", "pledged_pct_of_total",
                                     "num_shares_pledged", "broadcast_dt", "source_url"])
    con.register("_shp", df)
    try:
        con.execute(
            "INSERT OR REPLACE INTO shareholding (symbol, period_end, promoter_holding_pct, "
            "pledged_pct_of_promoter, pledged_pct_of_total, num_shares_pledged, "
            "broadcast_dt, source_url) SELECT * FROM _shp")
    finally:
        con.unregister("_shp")
    return len(df)


def ingest_shareholding(symbol: str, con: duckdb.DuckDBPyConnection) -> int:
    """Land the latest promoter-pledge snapshot for ``symbol`` (best-effort)."""
    return _write_shareholding(con, [_pledge_row(symbol, nse_api.promoter_pledge(symbol))])


def ingest_shareholding_batch(symbols: list[str], con: duckdb.DuckDBPyConnection) -> int:
    """Land pledge snapshots for many symbols in one browser session (for the scan)."""
    data = nse_api.promoter_pledge_batch(symbols)
    return _write_shareholding(con, [_pledge_row(s, data.get(s)) for s in symbols])


def store_pledge(con: duckdb.DuckDBPyConnection, data: dict[str, dict | None]) -> int:
    """Persist already-fetched pledge data ({symbol: parsed-dict}) — avoids a
    second browser session when the scan has already fetched it."""
    return _write_shareholding(con, [_pledge_row(s, p) for s, p in data.items()])


# ----------------- holder-level shareholding (SHP XBRL) -----------------
_EQUITY_L = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def ingest_equity_master(con: duckdb.DuckDBPyConnection, *, max_age_days: int = 30) -> int:
    """Land the full NSE listed-companies master (symbol ↔ company name) from
    EQUITY_L.csv (plain HTTP). Skipped when fresher than ``max_age_days`` —
    it's the lookup that tags a shareholder as itself a LISTED company."""
    row = con.execute("SELECT max(updated_at) FROM equity_master").fetchone()
    if row and row[0] and (datetime.now() - row[0]).days < max_age_days:
        return 0
    import csv
    import io
    text = fetch_bytes(_EQUITY_L).decode("utf-8", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SYMBOL") and r.get("NAME OF COMPANY"):
            rows.append({"symbol": r["SYMBOL"], "company_name": r["NAME OF COMPANY"],
                         "isin": r.get("ISIN NUMBER") or None})
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "company_name", "isin"])
    con.register("_eqm", df)
    try:
        con.execute("INSERT OR REPLACE INTO equity_master (symbol, company_name, isin) "
                    "SELECT * FROM _eqm")
    finally:
        con.unregister("_eqm")
    return len(df)


def _write_shp_quarter(symbol: str, quarter: dict, listed: dict,
                       con: duckdb.DuckDBPyConnection) -> int:
    """Classify + upsert one SHP quarter's holder rows. ``quarter`` is
    ``{as_of, holders:[...]}``; ``listed`` maps norm_name → NSE symbol."""
    from equity_research.scrapers import nse_shp
    as_of = quarter.get("as_of")
    if not as_of:
        return 0
    rows = []
    for h in quarter["holders"]:
        if not h["pct"] and not h.get("shares"):
            continue                                       # empty promoter placeholder rows
        cls, matched = nse_shp.classify(h, listed)
        if matched == symbol:                              # the company itself, not a holder
            matched = None
        rows.append({"symbol": symbol, "as_of": as_of, "holder_name": h["name"],
                     "pct": round(h["pct"], 4), "shares": h.get("shares"),
                     "category": h["category"], "is_promoter": h["is_promoter"],
                     "classification": cls, "matched_symbol": matched})
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "as_of", "holder_name", "pct", "shares",
                                     "category", "is_promoter", "classification",
                                     "matched_symbol"])
    con.register("_shph", df)
    try:
        con.execute("INSERT OR REPLACE INTO shp_holders (symbol, as_of, holder_name, pct, "
                    "shares, category, is_promoter, classification, matched_symbol) "
                    "SELECT * FROM _shph")
    finally:
        con.unregister("_shph")
    return len(df)


def _listed_master(con: duckdb.DuckDBPyConnection) -> dict:
    """norm_name → NSE symbol over the whole listed master (the holder classifier)."""
    from equity_research.scrapers import nse_shp
    ingest_equity_master(con)                              # staleness-guarded
    listed = {nse_shp.norm_name(n): s for s, n in
              con.execute("SELECT symbol, company_name FROM equity_master").fetchall()}
    listed.pop("", None)
    return listed


def ingest_shp_holders(symbol: str, con: duckdb.DuckDBPyConnection) -> int:
    """Land the latest holder-level shareholding pattern for ``symbol`` — every
    promoter/promoter-group account + every public >1% holder, each classified
    (individual / listed company / unlisted pvt / MF / FPI / …). Best-effort."""
    from equity_research.scrapers import nse_shp
    data = nse_shp.holders(symbol)
    if not data or not data.get("as_of"):
        return 0
    return _write_shp_quarter(symbol, data, _listed_master(con), con)


def ingest_shp_history(symbol: str, con: duckdb.DuckDBPyConnection, quarters: int = 12) -> int:
    """Land the most recent ``quarters`` SHP filings for ``symbol`` (newest first) so
    quarter-over-quarter ownership diffs and multi-year holder **cost zones** work. Idempotent —
    each quarter is a distinct ``as_of`` snapshot (re-running just adds any newly-filed quarters).
    Default 12 (~3 years) so ``ownership.institutional_cost`` can reach back to real entry prices;
    the NSE share-holdings-master catalog carries many quarters. Returns holder rows written."""
    from equity_research.scrapers import nse_shp
    quarters_data = nse_shp.all_quarters(symbol, n=quarters)
    if not quarters_data:
        return 0
    listed = _listed_master(con)
    return sum(_write_shp_quarter(symbol, qd, listed, con) for qd in quarters_data)


_INSIDER_COLS = ["symbol", "did", "disclosure_dt", "trade_to_dt", "acq_name", "category",
                 "mode", "txn_type", "qty", "value_cr", "hold_before_pct",
                 "hold_after_pct", "regulation"]


def _insider_rows(symbol: str, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        if not r.get("did"):
            continue
        out.append({"symbol": symbol, **{c: r.get(c) for c in _INSIDER_COLS if c != "symbol"}})
    return out


def _write_insider(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    rows = [r for r in rows if r]
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=_INSIDER_COLS)
    con.register("_pit", df)
    try:
        con.execute(f"INSERT OR REPLACE INTO insider_trades ({', '.join(_INSIDER_COLS)}) "
                    "SELECT * FROM _pit")
    finally:
        con.unregister("_pit")
    return len(df)


def ingest_insider_trades(symbol: str, con: duckdb.DuckDBPyConnection) -> int:
    """Land recent SEBI PIT (insider/promoter) trade disclosures for ``symbol`` (best-effort)."""
    return _write_insider(con, _insider_rows(symbol, nse_api.insider_trades(symbol)))


def store_insider_trades(con: duckdb.DuckDBPyConnection, data: dict[str, list[dict]]) -> int:
    """Persist already-fetched insider data ({symbol: [rows]}) — no extra browser session."""
    n = 0
    for s, rows in (data or {}).items():
        n += _write_insider(con, _insider_rows(s, rows))
    return n


_MF_SCHEME_COLS = ["scheme_code", "isin_growth", "isin_reinvest", "scheme_name", "amc",
                   "category", "asset_class", "plan", "option"]


def ingest_mf_navall(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Land the full AMFI daily NAV universe: refresh the ``mf_scheme`` master and
    append today's point to ``mf_nav`` (accumulated forward → a NAV time series).

    Idempotent per day: re-running overwrites the scheme master and the day's NAV.
    Returns ``{"schemes": n, "navs": m}`` (both 0 if the feed was unreachable).
    """
    rows = amfi.fetch_navall()
    if not rows:
        return {"schemes": 0, "navs": 0}

    sch = pd.DataFrame(
        [{c: getattr(r, c) for c in _MF_SCHEME_COLS} for r in rows],
        columns=_MF_SCHEME_COLS,
    ).drop_duplicates(subset=["scheme_code"], keep="last")
    con.register("_mfsch", sch)
    try:
        con.execute(f"INSERT OR REPLACE INTO mf_scheme ({', '.join(_MF_SCHEME_COLS)}) "
                    "SELECT * FROM _mfsch")
    finally:
        con.unregister("_mfsch")

    nav = pd.DataFrame(
        [{"scheme_code": r.scheme_code, "nav_date": r.nav_date, "nav": r.nav}
         for r in rows if r.nav is not None and r.nav_date is not None],
        columns=["scheme_code", "nav_date", "nav"],
    ).drop_duplicates(subset=["scheme_code", "nav_date"], keep="last")
    con.register("_mfnav", nav)
    try:
        con.execute("INSERT OR REPLACE INTO mf_nav (scheme_code, nav_date, nav) "
                    "SELECT * FROM _mfnav")
    finally:
        con.unregister("_mfnav")
    return {"schemes": len(sch), "navs": len(nav)}


_MF_HIST_WINDOW = 180        # AMFI's history report caps the range; fetch in windows


def ingest_mf_nav_history(amc_code: int, frm: date, to: date,
                          con: duckdb.DuckDBPyConnection) -> int:
    """Backfill ``mf_nav`` for one AMC over [frm, to] from AMFI's history report.

    The report silently returns nothing for wide ranges, so we fetch in ~180-day
    windows and upsert each. ``amc_code`` is AMFI's numeric fund-house id.
    Idempotent (upsert on ``(scheme_code, nav_date)``). Returns points written.
    """
    from datetime import timedelta
    written = 0
    start = frm
    while start <= to:
        end = min(start + timedelta(days=_MF_HIST_WINDOW), to)
        hist = amfi.fetch_nav_history(amc_code, start, end)
        if hist:
            df = pd.DataFrame(hist, columns=["scheme_code", "nav_date", "nav"]) \
                .drop_duplicates(subset=["scheme_code", "nav_date"], keep="last")
            con.register("_mfhist", df)
            try:
                con.execute("INSERT OR REPLACE INTO mf_nav (scheme_code, nav_date, nav) "
                            "SELECT * FROM _mfhist")
            finally:
                con.unregister("_mfhist")
            written += len(df)
        start = end + timedelta(days=1)
    return written


def build_mf_amc_map(con: duckdb.DuckDBPyConnection, on: date | None = None) -> int:
    """Resolve every active AMFI AMC code → name (one-time, cached in ``mf_amc``).

    Needed to backfill an arbitrary fund's history (the report needs the numeric
    ``mf`` code, which NAVAll doesn't carry). Idempotent. ``on`` is the probe date
    (defaults to a recent weekday). Returns the number of AMCs mapped.
    """
    from datetime import timedelta
    if on is None:
        on = date.today() - timedelta(days=1)
        while on.weekday() >= 5:
            on -= timedelta(days=1)
    rows = []
    for code in amfi.amc_codes():
        name = amfi.amc_name(code, on)
        if name:
            rows.append({"amc_code": code, "amc_name": name})
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["amc_code", "amc_name"])
    con.register("_mfamc", df)
    try:
        con.execute("INSERT OR REPLACE INTO mf_amc (amc_code, amc_name) SELECT * FROM _mfamc")
    finally:
        con.unregister("_mfamc")
    return len(df)


def amc_code_for(con: duckdb.DuckDBPyConnection, amc_name: str) -> int | None:
    """AMFI numeric AMC code for a fund-house name (from ``mf_amc``), or None."""
    row = con.execute("SELECT amc_code FROM mf_amc WHERE amc_name = ?", [amc_name]).fetchone()
    return row[0] if row else None


def backfill_mf_scheme_history(con: duckdb.DuckDBPyConnection, scheme_code: int,
                               years: float = 5.5) -> int:
    """Backfill one scheme's NAV history by resolving its AMC → code and pulling that
    AMC's history (then keeping just this scheme's rows land via the shared upsert).

    Returns points written for the AMC over the window (0 if the AMC is unmapped).
    """
    from datetime import timedelta
    row = con.execute("SELECT amc FROM mf_scheme WHERE scheme_code = ?", [scheme_code]).fetchone()
    if not row or not row[0]:
        return 0
    code = amc_code_for(con, row[0])
    if code is None:
        return 0
    to = date.today()
    frm = date(to.year - int(years), to.month, to.day) - timedelta(days=int((years % 1) * 365))
    return ingest_mf_nav_history(code, frm, to, con)


_MF_HOLD_COLS = ["scheme_code", "as_of", "isin", "instrument", "industry",
                 "quantity", "market_value_cr", "pct_nav", "source_url"]


def _match_scheme(con: duckdb.DuckDBPyConnection, amc_name: str, fund_name: str) -> int | None:
    """Map a disclosure sheet's fund name → the AMFI Direct-Growth scheme_code for it.
    Token-AND match within the AMC, preferring the tightest Direct-Growth name."""
    import re as _re
    base = fund_name.split("(")[0]               # drop the parenthetical scheme description
    _STOP = {"fund", "plan", "the", "of", "scheme", "an", "open", "ended", "equity",
             "growth", "direct", "regular", "investing", "predominantly", "a"}
    toks = [t for t in _re.findall(r"[a-z0-9]+", base.lower()) if t not in _STOP]
    if not toks:
        return None
    where = " AND ".join(["scheme_name ILIKE ?"] * len(toks))
    params = [amc_name] + [f"%{t}%" for t in toks]
    for extra in ("AND plan = 'Direct' AND (option = 'Growth' OR option IS NULL)", ""):
        row = con.execute(
            f"SELECT scheme_code FROM mf_scheme WHERE amc = ? AND {where} {extra} "
            "ORDER BY length(scheme_name) LIMIT 1", params).fetchone()
        if row:
            return row[0]
    return None


def ingest_mf_holdings(con: duckdb.DuckDBPyConnection, amc_name: str,
                       as_of: date | None = None) -> int:
    """Land a registered AMC's month-end scheme holdings into ``mf_holdings``.

    Maps each disclosure sheet to its AMFI scheme_code and upserts (idempotent per
    scheme/month/holding). Returns rows written (0 if the AMC is uncovered or the
    month isn't published yet).
    """
    import calendar as _cal
    from datetime import timedelta
    from equity_research.scrapers import mf_holdings as mfh
    if as_of is None:                            # default: most recent published month-end
        as_of = date.today().replace(day=1) - timedelta(days=1)
    me = date(as_of.year, as_of.month, _cal.monthrange(as_of.year, as_of.month)[1])
    # monthly guard: if we already hold this AMC's month, skip the (heavy) fetch entirely
    have = con.execute(
        "SELECT count(*) FROM mf_holdings h JOIN mf_scheme s USING(scheme_code) "
        "WHERE s.amc = ? AND h.as_of = ?", [amc_name, me]).fetchone()[0]
    if have:
        return 0
    url, rows = mfh.fetch_amc_holdings(amc_name, as_of)
    if not rows:
        return 0
    code_cache: dict[str, int | None] = {}
    out = []
    for r in rows:
        fn = r["fund_name"]
        if fn not in code_cache:
            code_cache[fn] = _match_scheme(con, amc_name, fn)
        code = code_cache[fn]
        if code is None:
            continue
        out.append({"scheme_code": code, "as_of": me, "isin": r["isin"],
                    "instrument": r["instrument"], "industry": r["industry"],
                    "quantity": r["quantity"], "market_value_cr": r["market_value_cr"],
                    "pct_nav": r["pct_nav"], "source_url": url})
    if not out:
        return 0
    df = pd.DataFrame(out, columns=_MF_HOLD_COLS).drop_duplicates(
        subset=["scheme_code", "as_of", "isin", "instrument"], keep="last")
    con.register("_mfh", df)
    try:
        con.execute(f"INSERT OR REPLACE INTO mf_holdings ({', '.join(_MF_HOLD_COLS)}) "
                    "SELECT * FROM _mfh")
    finally:
        con.unregister("_mfh")
    return len(df)


def ingest_mf_holdings_all(con: duckdb.DuckDBPyConnection, as_of: date | None = None) -> int:
    """Land month-end holdings for every AMC with a registered parser (idempotent).
    Cheap to run daily (one fetch per covered AMC; upsert is a no-op mid-month)."""
    from equity_research.scrapers import mf_holdings as mfh
    return sum(ingest_mf_holdings(con, amc, as_of) for amc in mfh.REGISTRY)


def ingest_sector_map(con: duckdb.DuckDBPyConnection, index: str = "nifty500") -> int:
    """Land the symbol -> industry map from an NSE index constituent list.

    Upserts the CSV columns (company / macro industry / universe) but **preserves** any
    ``basic_industry`` already enriched via ``ingest_basic_industries`` — the granular tag
    is expensive to fetch and must survive the daily universe refresh."""
    df = nse_archives.fetch_constituents(index)
    out = df.rename(columns={"Company Name": "company", "Industry": "industry",
                             "Symbol": "symbol"})[["symbol", "company", "industry"]].copy()
    out["universe"] = index.upper()
    con.register("_sec", out)
    try:
        con.execute(
            "INSERT INTO sector_map (symbol, company, industry, universe) "
            "SELECT symbol, company, industry, universe FROM _sec "
            "ON CONFLICT (symbol) DO UPDATE SET company = excluded.company, "
            "industry = excluded.industry, universe = excluded.universe")
    finally:
        con.unregister("_sec")
    return len(out)


def ingest_basic_industries(con: duckdb.DuckDBPyConnection, symbols: list[str] | None = None,
                            *, batch: int = 40, only_missing: bool = True,
                            cooldown_s: float = 0.0) -> dict[str, int]:
    """Enrich ``sector_map`` with NSE's granular ``basic_industry`` (e.g. 'Gems Jewellery And
    Watches') — the finer tier the index-constituent CSVs omit, so peer grouping compares a
    jeweller to jewellers, not to all of 'Consumer Durables'. Static data; run once, re-run
    only picks up symbols still missing a tag. Browser-tier — batched to warm the session once
    per ``batch`` symbols. Returns {'updated', 'missing', 'symbols'}."""
    if symbols is None:
        q = "SELECT symbol FROM sector_map"
        if only_missing:
            q += " WHERE basic_industry IS NULL OR basic_industry = ''"
        symbols = [r[0] for r in con.execute(q + " ORDER BY symbol").fetchall()]
    updated = 0
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        info = nse_api.sec_info_batch(chunk)
        for sym, d in info.items():
            bi = (d or {}).get("basic_industry")
            if bi:
                con.execute("UPDATE sector_map SET basic_industry = ? WHERE symbol = ?",
                            [bi, sym])
                updated += 1
        log.info("basic_industry: %d/%d done (+%d tagged)",
                 min(i + batch, len(symbols)), len(symbols), updated)
        if cooldown_s and i + batch < len(symbols):
            import time
            time.sleep(cooldown_s)
    return {"updated": updated, "symbols": len(symbols),
            "missing": len(symbols) - updated}


def ingest_eod_range(start: date, end: date, con: duckdb.DuckDBPyConnection, *,
                     skip_existing: bool = True) -> dict[str, int]:
    """Backfill daily bhavcopy for every trading day in [start, end] (inclusive).

    Idempotent: weekends/holidays 404 and are skipped; dates already in
    ``equity_eod`` are skipped when ``skip_existing``. Returns a small summary.
    """
    from datetime import timedelta
    have: set = set()
    if skip_existing:
        have = {r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM equity_eod WHERE trade_date BETWEEN ? AND ?",
            [start, end]).fetchall()}
    ingested = skipped = holidays = 0
    d = start
    while d <= end:
        if d.weekday() >= 5 or d in have:
            skipped += 1
        else:
            try:
                ingest_bhavcopy(d, con)
                ingested += 1
            except ScrapeError:
                holidays += 1            # no file = market holiday
        d += timedelta(days=1)
    return {"ingested": ingested, "skipped": skipped, "holidays_or_missing": holidays}


def ingest_eod(d: date, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Ingest the full daily EOD set for trade date ``d``."""
    return {
        "equity_eod": ingest_bhavcopy(d, con),
        "index_close": ingest_index_closes(d, con),
        "participant_oi": ingest_participant_oi(d, con),
    }
