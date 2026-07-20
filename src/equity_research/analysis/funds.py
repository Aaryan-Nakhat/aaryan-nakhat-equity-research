"""Mutual-fund return & risk analytics off the accumulated ``mf_nav`` series.

Pure reads of the NAV history we land daily via ``ingest.ingest_mf_navall`` (plus any
``ingest_mf_nav_history`` backfill). Everything degrades gracefully — a metric is
``None`` when the series is too short for it, never an error.

Point returns annualise (CAGR) for horizons ≥ 1y and stay absolute for < 1y. Risk
metrics (annualised vol, Sharpe, Sortino, max drawdown) come from daily NAV changes;
Sharpe/Sortino use a configurable risk-free rate (India ~6.5% default).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

_RF_DEFAULT = 0.065          # ~India 1y risk-free
_TRADING_DAYS = 252
# horizon label -> lookback in days (None = since inception)
_HORIZONS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1095, "5y": 1825, "incep": None}
_ASOF_TOL_DAYS = 12          # accept a NAV within this many days of the target date


def nav_series(con: duckdb.DuckDBPyConnection, scheme_code: int) -> pd.Series:
    """Ascending NAV series (index = date) for a scheme; empty if none stored."""
    rows = con.execute(
        "SELECT nav_date, nav FROM mf_nav WHERE scheme_code = ? ORDER BY nav_date",
        [scheme_code]).fetchall()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.Series([r[1] for r in rows], index=idx, dtype="float64")


def _asof(series: pd.Series, target: date) -> float | None:
    """NAV on/just-before ``target`` (within tolerance), else None."""
    ts = pd.Timestamp(target)
    upto = series[series.index <= ts]
    if upto.empty:
        return None
    if (ts - upto.index[-1]).days > _ASOF_TOL_DAYS:
        return None
    return float(upto.iloc[-1])


def point_returns(series: pd.Series) -> dict[str, float | None]:
    """Trailing returns per horizon: CAGR % for ≥1y, absolute % for <1y (None if
    the series doesn't reach back far enough)."""
    out: dict[str, float | None] = {}
    if series.empty:
        return {h: None for h in _HORIZONS}
    last_dt, last_nav = series.index[-1], float(series.iloc[-1])
    for label, days in _HORIZONS.items():
        base = float(series.iloc[0]) if days is None else _asof(series, (last_dt - timedelta(days=days)).date())
        if not base or base <= 0:
            out[label] = None
            continue
        yrs = ((last_dt - series.index[0]).days / 365.25) if days is None else days / 365.0
        if yrs >= 1.0:
            out[label] = round(((last_nav / base) ** (1 / yrs) - 1) * 100, 1)
        else:
            out[label] = round((last_nav / base - 1) * 100, 1)
    return out


def risk_metrics(series: pd.Series, rf: float = _RF_DEFAULT) -> dict[str, float | None]:
    """Annualised vol, Sharpe, Sortino, and max drawdown from daily NAV changes.
    Needs a reasonable run of history (≥ ~60 points) or returns all-None."""
    empty = {"vol_pct": None, "sharpe": None, "sortino": None, "max_drawdown_pct": None}
    if series.size < 60:
        return empty
    daily = series.pct_change().dropna()
    if daily.empty:
        return empty
    vol = float(daily.std() * np.sqrt(_TRADING_DAYS))
    ann_ret = float((1 + daily.mean()) ** _TRADING_DAYS - 1)
    downside = daily[daily < 0]
    dvol = float(downside.std() * np.sqrt(_TRADING_DAYS)) if not downside.empty else 0.0
    running_max = series.cummax()
    max_dd = float(((series - running_max) / running_max).min())
    return {
        "vol_pct": round(vol * 100, 1) if vol else None,
        "sharpe": round((ann_ret - rf) / vol, 2) if vol else None,
        "sortino": round((ann_ret - rf) / dvol, 2) if dvol else None,
        "max_drawdown_pct": round(max_dd * 100, 1),
    }


def rolling_returns(series: pd.Series, window_days: int = 365) -> dict[str, float | None]:
    """Distribution (min / median / max, all annualised %) of rolling ``window_days``
    returns — a consistency read. None-valued when history < ~1.5× the window."""
    empty = {"min": None, "median": None, "max": None}
    if series.empty or (series.index[-1] - series.index[0]).days < window_days * 1.5:
        return empty
    yrs = window_days / 365.0
    vals: list[float] = []
    for start_ts, start_nav in series.items():
        end_nav = _asof(series, (start_ts + timedelta(days=window_days)).date())
        if end_nav and start_nav and start_nav > 0:
            vals.append(((end_nav / start_nav) ** (1 / yrs) - 1) * 100)
    if not vals:
        return empty
    arr = np.array(vals)
    return {"min": round(float(arr.min()), 1),
            "median": round(float(np.median(arr)), 1),
            "max": round(float(arr.max()), 1)}


# ----------------- SIP / XIRR -----------------
def _xnpv(rate: float, flows: list[tuple[date, float]]) -> float:
    t0 = flows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.0) for d, cf in flows)


def _xirr(flows: list[tuple[date, float]], lo: float = -0.95, hi: float = 10.0) -> float | None:
    """Money-weighted return for irregular cashflows, by bisection (no scipy).
    None when the flows don't bracket a root (e.g. a total loss)."""
    if len(flows) < 2:
        return None
    f_lo, f_hi = _xnpv(lo, flows), _xnpv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = _xnpv(mid, flows)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def sip_returns(series: pd.Series, monthly: float = 10_000.0,
                horizons: tuple[int, ...] = (1, 3, 5)) -> dict[str, dict]:
    """Simulate a monthly SIP of ``monthly`` into this scheme for each horizon —
    what you'd have invested, what it'd be worth, and the **XIRR** (money-weighted
    return, the number an SIP investor actually earns). Skips horizons the NAV
    history doesn't cover."""
    out: dict[str, dict] = {}
    if series.empty:
        return out
    last_ts = series.index[-1]
    last_nav = float(series.iloc[-1])
    for yrs in horizons:
        start = last_ts - pd.DateOffset(years=yrs)
        if series.index[0] > start:            # not enough history for this horizon
            continue
        units, flows = 0.0, []
        for d in pd.date_range(start=start, end=last_ts, freq="MS"):
            nav = _asof(series, d.date())
            if not nav or nav <= 0:
                continue
            units += monthly / nav
            flows.append((d.date(), -monthly))
        if not flows:
            continue
        value = units * last_nav
        invested = monthly * len(flows)
        rate = _xirr([*flows, (last_ts.date(), value)])
        out[f"{yrs}y"] = {
            "installments": len(flows),
            "invested": round(invested),
            "value": round(value),
            "gain_pct": round(100 * (value - invested) / invested, 1) if invested else None,
            "xirr_pct": round(rate * 100, 1) if rate is not None else None,
        }
    return out


# ----------------- benchmark-relative risk -----------------
# category keyword -> the fair index. Order matters ('large & mid' before 'large'/'mid').
_BENCHMARKS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("small cap", "smallcap"), "Nifty Smallcap 250"),
    (("large & mid", "large and mid"), "Nifty 100"),
    (("mid cap", "midcap"), "Nifty Midcap 150"),
    (("large cap", "largecap"), "Nifty 50"),
)


def benchmark_for(category: str | None, asset_class: str | None = None) -> str | None:
    """The fair benchmark index for an **equity** scheme's category (None for debt /
    liquid / other, where an equity index would be meaningless)."""
    if (asset_class or "").strip().lower() != "equity":
        return None
    cat = (category or "").lower()
    for kws, idx in _BENCHMARKS:
        if any(k in cat for k in kws):
            return idx
    return "Nifty 500"                          # flexi / multi / focused / value / thematic …


def index_series(con: duckdb.DuckDBPyConnection, index_name: str) -> pd.Series:
    """Ascending close series for a benchmark index; empty if not stored."""
    rows = con.execute(
        "SELECT trade_date, close FROM index_close WHERE index_name = ? AND close IS NOT NULL "
        "ORDER BY trade_date", [index_name]).fetchall()
    if not rows:
        return pd.Series(dtype="float64")
    return pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]),
                     dtype="float64")


def _dense_tail(df: pd.DataFrame, max_gap_days: int = 15) -> pd.DataFrame:
    """Trim to the contiguous recent stretch with no gap longer than ``max_gap_days``.

    Our ``index_close`` history has a few sparse early rows, and differencing across a
    multi-month hole would manufacture one enormous fake 'daily' return — which then
    poisons the mean, the variance and every metric built on them."""
    if len(df) < 2:
        return df
    gaps = df.index.to_series().diff().dt.days
    big = gaps[gaps > max_gap_days]
    return df.loc[big.index[-1]:] if len(big) else df


def benchmark_metrics(fund: pd.Series, bench: pd.Series,
                      rf: float = _RF_DEFAULT) -> dict | None:
    """Benchmark-relative behaviour on the **overlapping, gap-free** history: beta,
    Jensen's alpha, up/down capture, tracking error and information ratio.
    None when fewer than ~60 usable common days (our index history is the binding limit)."""
    if fund.empty or bench.empty:
        return None
    df = _dense_tail(pd.concat([fund.rename("f"), bench.rename("b")], axis=1).dropna())
    if len(df) < 61:
        return None
    fr = df["f"].pct_change().dropna()
    br = df["b"].pct_change().dropna()
    if len(fr) < 60 or float(br.var()) == 0:
        return None
    beta = float(np.cov(fr, br)[0, 1] / br.var())
    # annualise from the endpoints (CAGR), NOT by compounding a mean daily return —
    # endpoint annualisation is robust to the odd outlier day.
    yrs = (df.index[-1] - df.index[0]).days / 365.25
    if yrs <= 0.25:
        return None
    f_ann = float((df["f"].iloc[-1] / df["f"].iloc[0]) ** (1 / yrs) - 1)
    b_ann = float((df["b"].iloc[-1] / df["b"].iloc[0]) ** (1 / yrs) - 1)
    alpha = (f_ann - rf) - beta * (b_ann - rf)
    up, down = br > 0, br < 0
    up_cap = (100 * fr[up].mean() / br[up].mean()) if up.any() and br[up].mean() else None
    dn_cap = (100 * fr[down].mean() / br[down].mean()) if down.any() and br[down].mean() else None
    te = float((fr - br).std() * np.sqrt(_TRADING_DAYS))
    return {
        "n_days": int(len(fr)),
        "years": round(yrs, 1),
        "beta": round(beta, 2),
        "alpha_pct": round(alpha * 100, 1),
        "up_capture_pct": round(float(up_cap)) if up_cap is not None else None,
        "down_capture_pct": round(float(dn_cap)) if dn_cap is not None else None,
        "tracking_error_pct": round(te * 100, 1) if te else None,
        "information_ratio": round((f_ann - b_ann) / te, 2) if te else None,
        "fund_ann_pct": round(f_ann * 100, 1),
        "bench_ann_pct": round(b_ann * 100, 1),
    }


# ----------------- month-over-month portfolio churn -----------------
# Money-market/debt lines a fund parks cash in. These MATURE and roll over every month,
# so counting them as "bought"/"exited" makes routine treasury look like frantic trading
# and buries the real equity conviction.
_MATURITY_RE = re.compile(r"\(\d{2}/\d{2}/\d{4}\)")
_NON_EQUITY = ("treps", "t-bill", "tbill", "treasury bill", "commercial paper",
               "certificate of deposit", "liquid fund", "net receivable", "net current asset",
               "cash margin", "margin deposit", "clearing corporation", "reverse repo",
               "government of india", "g-sec", "gsec", "sdl", "corporate debt", "money market",
               "debenture", "ncd", "cash & other", "cash and other")


def _is_equity_holding(instrument: str) -> bool:
    """True for a real equity position — filters the CDs / CPs / T-bills / TREPS (which
    carry a maturity date in the disclosure) and cash-equivalent lines."""
    n = (instrument or "").lower()
    if _MATURITY_RE.search(n):
        return False
    return not any(k in n for k in _NON_EQUITY)


def holdings_churn(con: duckdb.DuckDBPyConnection, scheme_code: int,
                   min_delta: float = 0.10) -> dict | None:
    """What the manager actually **did** between the two most recent monthly
    disclosures: fresh buys, full exits, and meaningful adds/trims (by % of NAV),
    **equity positions only**. None until we hold two months of portfolios."""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT as_of FROM mf_holdings WHERE scheme_code = ? "
        "ORDER BY as_of DESC LIMIT 2", [scheme_code]).fetchall()]
    if len(dates) < 2:
        return None
    cur, prev = dates[0], dates[1]

    def snap(d) -> dict[str, float]:
        return {r[0]: float(r[1]) for r in con.execute(
            "SELECT instrument, pct_nav FROM mf_holdings WHERE scheme_code = ? AND as_of = ? "
            "AND pct_nav IS NOT NULL", [scheme_code, d]).fetchall()
            if _is_equity_holding(r[0])}

    c, p = snap(cur), snap(prev)
    if not c or not p:
        return None
    new = sorted(((k, round(v, 2)) for k, v in c.items() if k not in p), key=lambda x: -x[1])
    exited = sorted(((k, round(v, 2)) for k, v in p.items() if k not in c), key=lambda x: -x[1])
    deltas = [(k, round(c[k] - p[k], 2)) for k in c if k in p]
    added = sorted((d for d in deltas if d[1] >= min_delta), key=lambda x: -x[1])
    trimmed = sorted((d for d in deltas if d[1] <= -min_delta), key=lambda x: x[1])
    return {"current": cur, "previous": prev,
            "new": new[:8], "exited": exited[:8], "added": added[:8], "trimmed": trimmed[:8],
            "n_new": len(new), "n_exited": len(exited)}


def category_percentile(con: duckdb.DuckDBPyConnection, scheme_code: int,
                        horizon: str = "3y") -> dict | None:
    """Where this scheme's ``horizon`` return ranks among its same-category
    Direct-Growth peers that have enough NAV history. Best-effort: None if the
    category is too thinly covered (< 5 peers with a computable return)."""
    cat = con.execute("SELECT category, plan, option FROM mf_scheme WHERE scheme_code = ?",
                      [scheme_code]).fetchone()
    if not cat or not cat[0]:
        return None
    peers = con.execute(
        "SELECT scheme_code FROM mf_scheme WHERE category = ? "
        "AND plan = 'Direct' AND (option = 'Growth' OR option IS NULL)", [cat[0]]).fetchall()
    rets: list[tuple[int, float]] = []
    for (pc,) in peers:
        r = point_returns(nav_series(con, pc)).get(horizon)
        if r is not None:
            rets.append((pc, r))
    if len(rets) < 5:
        return None
    mine = dict(rets).get(scheme_code)
    if mine is None:
        return None
    below = sum(1 for _, r in rets if r < mine)
    return {"horizon": horizon, "percentile": round(100 * below / len(rets)),
            "rank": sum(1 for _, r in rets if r > mine) + 1, "n": len(rets),
            "category_median": round(float(np.median([r for _, r in rets])), 1)}


def _norm_name(s: str) -> str:
    """Normalise a company name for matching ('HDFC Bank Limited' ~ 'HDFC Bank Ltd')."""
    n = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    for junk in (" limited", " ltd", " india", " corporation", " corp", " company", " co"):
        n = n.replace(junk, " ")
    return re.sub(r"\s+", " ", n).strip()


def holdings_snapshot(con: duckdb.DuckDBPyConnection, scheme_code: int) -> dict | None:
    """Latest month's portfolio shape: size, top-10 concentration, biggest sector,
    and the top holdings. None if no ``mf_holdings`` for this scheme."""
    asof = con.execute("SELECT max(as_of) FROM mf_holdings WHERE scheme_code = ?",
                       [scheme_code]).fetchone()[0]
    if asof is None:
        return None
    rows = con.execute(
        "SELECT instrument, industry, pct_nav FROM mf_holdings "
        "WHERE scheme_code = ? AND as_of = ? AND pct_nav IS NOT NULL "
        "ORDER BY pct_nav DESC", [scheme_code, asof]).fetchall()
    if not rows:
        return None
    top10 = sum(r[2] for r in rows[:10])
    sectors: dict[str, float] = {}
    for _, ind, pct in rows:
        if ind:
            sectors[ind] = sectors.get(ind, 0.0) + pct
    top_sec = max(sectors.items(), key=lambda kv: kv[1]) if sectors else (None, None)
    return {
        "as_of": asof, "n_holdings": len(rows),
        "top10_pct": round(top10, 1),
        "top_sector": top_sec[0], "top_sector_pct": round(top_sec[1], 1) if top_sec[1] else None,
        "top": [(r[0], round(r[2], 2)) for r in rows[:10]],
    }


def watchlist_overlap(con: duckdb.DuckDBPyConnection, scheme_code: int) -> dict | None:
    """Where the fund's holdings intersect the user's stock watchlist (matched by
    company name) — a 'does smart money hold my names' read. None if no holdings."""
    asof = con.execute("SELECT max(as_of) FROM mf_holdings WHERE scheme_code = ?",
                       [scheme_code]).fetchone()[0]
    if asof is None:
        return None
    wl = con.execute("SELECT symbol, company FROM watchlist").fetchall()
    by_name = {_norm_name(c): s for s, c in wl if c}
    hits = []
    for instr, pct in con.execute(
            "SELECT instrument, pct_nav FROM mf_holdings WHERE scheme_code = ? AND as_of = ?",
            [scheme_code, asof]).fetchall():
        sym = by_name.get(_norm_name(instr))
        if sym and pct is not None:
            hits.append((sym, round(pct, 2)))
    hits.sort(key=lambda x: -x[1])
    return {"as_of": asof, "n_watchlist": len(wl), "hits": hits,
            "weight_pct": round(sum(p for _, p in hits), 1)}


def summary(con: duckdb.DuckDBPyConnection, scheme_code: int,
            rf: float = _RF_DEFAULT) -> dict | None:
    """Full analytics bundle for a scheme (identity + returns + risk), or None if
    the scheme/NAV is unknown."""
    ident = con.execute(
        "SELECT scheme_name, amc, category, asset_class, plan, option "
        "FROM mf_scheme WHERE scheme_code = ?", [scheme_code]).fetchone()
    series = nav_series(con, scheme_code)
    if ident is None or series.empty:
        return None
    return {
        "scheme_code": scheme_code,
        "scheme_name": ident[0], "amc": ident[1], "category": ident[2],
        "asset_class": ident[3], "plan": ident[4], "option": ident[5],
        "nav": round(float(series.iloc[-1]), 2),
        "nav_date": series.index[-1].date(),
        "history_days": (series.index[-1] - series.index[0]).days,
        "returns": point_returns(series),
        "risk": risk_metrics(series, rf),
        "rolling_1y": rolling_returns(series, 365),
    }
