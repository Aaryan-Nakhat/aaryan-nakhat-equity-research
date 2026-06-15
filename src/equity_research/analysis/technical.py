"""Technical analysis from the daily EOD series (`equity_eod`).

Trend / momentum / volatility / volume indicators, plus delivery-% conviction
(NSE-exclusive) and relative strength vs an index. Pure functions over the price
history; needs a continuous daily series (backfill via `ingest_eod_range`).
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd


def load_prices(con: duckdb.DuckDBPyConnection, symbol: str) -> pd.DataFrame:
    """Daily OHLCV + delivery% for ``symbol`` (EQ series), indexed by date asc."""
    df = con.execute(
        """SELECT trade_date, open, high, low, close, ttl_trd_qnty AS volume,
                  deliv_per
           FROM equity_eod
           WHERE symbol = ? AND series = 'EQ'
           ORDER BY trade_date""",
        [symbol],
    ).df()
    if df.empty:
        return df
    return df.set_index("trade_date")


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing.
    avg_gain = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def indicators(con: duckdb.DuckDBPyConnection, symbol: str) -> pd.DataFrame:
    """Full indicator frame (DMA/EMA/MACD/RSI/Bollinger/ATR/volume/delivery)."""
    p = load_prices(con, symbol)
    if p.empty:
        return p
    c = p["close"]
    out = p.copy()
    out["sma20"] = c.rolling(20).mean()
    out["sma50"] = c.rolling(50).mean()
    out["sma200"] = c.rolling(200).mean()
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["rsi14"] = _rsi(c)
    std20 = c.rolling(20).std()
    out["bb_mid"] = out["sma20"]
    out["bb_upper"] = out["sma20"] + 2 * std20
    out["bb_lower"] = out["sma20"] - 2 * std20
    out["atr14"] = _atr(p)
    out["vol_avg20"] = p["volume"].rolling(20).mean()
    out["deliv_avg20"] = p["deliv_per"].rolling(20).mean()
    out["high_52w"] = c.rolling(252, min_periods=20).max()
    out["low_52w"] = c.rolling(252, min_periods=20).min()
    return out


def relative_strength(con: duckdb.DuckDBPyConnection, symbol: str, *,
                      index_name: str = "Nifty 50", window: int = 63) -> float | None:
    """Stock return ÷ index return over ``window`` trading days (>1 = outperform).

    Returns None if the index series isn't available for the period.
    """
    p = load_prices(con, symbol)
    idx = con.execute(
        "SELECT trade_date, close FROM index_close WHERE index_name = ? ORDER BY trade_date",
        [index_name],
    ).df()
    if len(p) <= window or len(idx) <= window:
        return None
    idx = idx.set_index("trade_date")["close"]
    common = p.index.intersection(idx.index)
    if len(common) <= window:
        return None
    s = p["close"].reindex(common)
    i = idx.reindex(common)
    stock_ret = s.iloc[-1] / s.iloc[-window] - 1
    index_ret = i.iloc[-1] / i.iloc[-window] - 1
    if index_ret == -1:
        return None
    return (1 + stock_ret) / (1 + index_ret)


def snapshot(con: duckdb.DuckDBPyConnection, symbol: str) -> dict:
    """Latest indicator values + plain-language signals."""
    ind = indicators(con, symbol)
    if ind.empty:
        return {}
    last = ind.iloc[-1]
    c = last["close"]

    def sig(cond, yes, no):
        return yes if cond else no

    signals = []
    if c == c and last["sma200"] == last["sma200"]:
        signals.append(sig(c > last["sma200"], "above 200-DMA (uptrend)",
                           "below 200-DMA (downtrend)"))
    if last["sma50"] == last["sma50"] and last["sma200"] == last["sma200"]:
        signals.append(sig(last["sma50"] > last["sma200"],
                           "50>200 (golden-cross regime)", "50<200 (death-cross regime)"))
    if last["rsi14"] == last["rsi14"]:
        r = last["rsi14"]
        signals.append("RSI overbought (>70)" if r > 70 else
                       "RSI oversold (<30)" if r < 30 else f"RSI neutral ({r:.0f})")
    if last["macd"] == last["macd"]:
        signals.append(sig(last["macd"] > last["macd_signal"],
                           "MACD bullish", "MACD bearish"))
    if last["deliv_per"] == last["deliv_per"] and last["deliv_avg20"] == last["deliv_avg20"]:
        signals.append(sig(last["deliv_per"] > 1.5 * last["deliv_avg20"],
                           "delivery% spike (conviction)", "delivery% normal"))

    pct_from_high = (100 * (c / last["high_52w"] - 1)
                     if last["high_52w"] == last["high_52w"] else np.nan)
    rs = relative_strength(con, symbol)
    return {
        "date": ind.index[-1],
        "close": c,
        "sma20": last["sma20"], "sma50": last["sma50"], "sma200": last["sma200"],
        "rsi14": last["rsi14"], "macd_hist": last["macd_hist"], "atr14": last["atr14"],
        "deliv_per": last["deliv_per"], "deliv_avg20": last["deliv_avg20"],
        "high_52w": last["high_52w"], "low_52w": last["low_52w"],
        "pct_from_52w_high": pct_from_high,
        "rel_strength_3m_vs_nifty": rs,
        "n_days": len(ind),
        "signals": signals,
    }
