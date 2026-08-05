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


# ─────────────────────────────────────────────────────────────────────────────
# Support/resistance levels, market structure, patterns & a verdict-aware setup.
# Everything here is *computed* from the daily OHLCV — no LLM, deterministic and
# reproducible. Zones come from the CONFLUENCE of several methods (swing pivots,
# moving averages, 52-week extremes, volume-by-price, round numbers): a price that
# several methods agree on is a stronger level than any one method alone.
# ─────────────────────────────────────────────────────────────────────────────

_MIN_DAYS = 60           # below this we can't say anything useful
_RELIABLE_DAYS = 180     # below this, levels are "limited" (thin history flagged)
_ZONE_LOOKBACK = 252     # ~1 trading year of history feeds the zones
_PIVOT_K = 5             # a swing pivot is an extreme over ±k bars (fractal window)

# per-source base weight in a zone's confluence score
_SRC_W = {"volume-node": 1.3, "swing": 1.0, "52w": 1.0, "200-DMA": 1.0,
          "50-DMA": 0.7, "20-DMA": 0.5, "round": 0.4}


def _swings(high: pd.Series, low: pd.Series, k: int = _PIVOT_K):
    """Swing-pivot positions: a pivot high is a bar whose high tops the ±k window;
    a pivot low bottoms it. Returns (list[(pos, price)] highs, lows) oldest→newest."""
    h, lo = high.to_numpy(), low.to_numpy()
    n = len(h)
    highs, lows = [], []
    for i in range(k, n - k):
        w_h, w_l = h[i - k:i + k + 1], lo[i - k:i + k + 1]
        if h[i] == w_h.max() and (w_h.argmax() == k):
            highs.append((i, float(h[i])))
        if lo[i] == w_l.min() and (w_l.argmin() == k):
            lows.append((i, float(lo[i])))
    return highs, lows


def _volume_by_price(df: pd.DataFrame, bins: int = 24):
    """Volume-by-price: total traded volume in each price bin over the window. Peaks are
    high-volume nodes — prices where the most shares changed hands, the stickiest S/R.
    Returns (centers, volume) arrays."""
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.array([]), np.array([])
    edges = np.linspace(lo, hi, bins + 1)
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy()
    vol = df["volume"].fillna(0).to_numpy()
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    agg = np.zeros(bins)
    for i, v in zip(idx, vol):
        agg[i] += v
    centers = (edges[:-1] + edges[1:]) / 2
    return centers, agg


def _round_levels(price: float) -> list[float]:
    """Psychological round numbers near the price. Generated at a fine (one order below
    the price) and a coarse (the price's own order) step; the caller keeps only positive
    levels within a few % of price, so far-away or zero levels never enter the zones."""
    if not price or price <= 0:
        return []
    exp = int(np.floor(np.log10(price)))
    out: set[float] = set()
    for step in (10.0 ** (exp - 1), 10.0 ** exp):
        if step <= 0:
            continue
        base = np.floor(price / step) * step
        out.update({base, base + step})
    return [float(x) for x in out if x > 0]


def _cluster(cands: list[tuple[float, float, str]], tol: float):
    """Merge nearby candidate prices into zones. Each candidate is (price, weight, source).
    A candidate joins the current zone only when it's within ``tol`` of the zone's running
    weighted **center** (not merely the previous candidate) — this bounds every zone to
    ~``tol`` wide and prevents a dense chain of candidates from drifting into one giant band.
    Returns zones sorted by price ascending: {lo, hi, mid, score, sources, touches}."""
    if not cands:
        return []
    cands = sorted(cands, key=lambda c: c[0])
    zones, cur = [], [cands[0]]

    def _center(group):
        w = sum(g[1] for g in group) or 1e-9
        return sum(g[0] * g[1] for g in group) / w

    for c in cands[1:]:
        if c[0] - _center(cur) <= tol:
            cur.append(c)
        else:
            zones.append(cur)
            cur = [c]
    zones.append(cur)
    out = []
    for z in zones:
        w = sum(c[1] for c in z) or 1e-9
        mid = sum(c[0] * c[1] for c in z) / w
        srcs = sorted({c[2] for c in z})
        out.append({"lo": float(min(c[0] for c in z)), "hi": float(max(c[0] for c in z)),
                    "mid": float(mid), "score": round(float(sum(c[1] for c in z)), 2),
                    "sources": srcs, "touches": sum(1 for c in z if c[2] == "swing")})
    return out


def _structure(highs, lows) -> dict:
    """Trend from swing geometry: rising highs+lows = up, falling = down, else range.
    ``bos`` flags a break of the prior structure (last higher-low undercut / lower-high
    reclaimed) — the earliest sign a trend is turning."""
    hh = [p for _, p in highs][-3:]
    ll = [p for _, p in lows][-3:]
    trend = "range"
    if len(hh) >= 2 and len(ll) >= 2:
        up = hh[-1] > hh[-2] and ll[-1] > ll[-2]
        down = hh[-1] < hh[-2] and ll[-1] < ll[-2]
        trend = "up" if up else "down" if down else "range"
    return {"trend": trend,
            "last_swing_high": hh[-1] if hh else None,
            "last_swing_low": ll[-1] if ll else None}


def _trendline(swings) -> tuple[float | None, float | None]:
    """Fit a line through the last ≤3 swing points; return (value_today, slope_per_day).
    None when too few points."""
    pts = swings[-3:]
    if len(pts) < 2:
        return None, None
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope * xs[-1] + intercept), float(slope)


def _patterns(highs, lows, price: float, atr: float,
              supports: list[dict], resistances: list[dict]) -> list[dict]:
    """Best-effort classic patterns from swing geometry. Heuristic — each carries a
    confidence and reads as context, never a standalone signal."""
    out: list[dict] = []
    tol = max(atr, 0.02 * price)
    hp = [p for _, p in highs]
    lp = [p for _, p in lows]

    # Range / consolidation — bounded between a well-touched support and resistance.
    if supports and resistances:
        s, r = supports[0], resistances[0]
        if s["touches"] >= 2 and r["touches"] >= 2:
            out.append({"name": "Range / consolidation", "direction": "neutral",
                        "confidence": "medium",
                        "note": f"coiling between ₹{s['mid']:,.0f} and ₹{r['mid']:,.0f}"})

    # Double bottom / top — two comparable swings with a counter-swing between.
    if len(lp) >= 2 and abs(lp[-1] - lp[-2]) <= tol and price > max(lp[-1], lp[-2]):
        out.append({"name": "Double bottom", "direction": "bullish", "confidence": "medium",
                    "note": f"two lows near ₹{(lp[-1] + lp[-2]) / 2:,.0f}"})
    if len(hp) >= 2 and abs(hp[-1] - hp[-2]) <= tol and price < min(hp[-1], hp[-2]):
        out.append({"name": "Double top", "direction": "bearish", "confidence": "medium",
                    "note": f"two highs near ₹{(hp[-1] + hp[-2]) / 2:,.0f}"})

    # Head & shoulders (bearish) / inverse (bullish) — 3 swings, middle the extreme.
    if len(hp) >= 3:
        a, b, c = hp[-3], hp[-2], hp[-1]
        if b > a and b > c and abs(a - c) <= tol:
            out.append({"name": "Head & shoulders", "direction": "bearish",
                        "confidence": "low", "note": f"peak ₹{b:,.0f}, shoulders ~₹{(a + c) / 2:,.0f}"})
    if len(lp) >= 3:
        a, b, c = lp[-3], lp[-2], lp[-1]
        if b < a and b < c and abs(a - c) <= tol:
            out.append({"name": "Inverse head & shoulders", "direction": "bullish",
                        "confidence": "low", "note": f"trough ₹{b:,.0f}, shoulders ~₹{(a + c) / 2:,.0f}"})

    # Triangle from converging trendlines.
    sup_v, sup_m = _trendline(lows)
    res_v, res_m = _trendline(highs)
    if sup_m is not None and res_m is not None:
        if sup_m > 0 and res_m < 0:
            out.append({"name": "Symmetrical triangle", "direction": "neutral",
                        "confidence": "low", "note": "converging — awaiting the break"})
        elif abs(res_m) < 1e-9 < sup_m:
            out.append({"name": "Ascending triangle", "direction": "bullish",
                        "confidence": "low", "note": "flat resistance, rising support"})
        elif sup_m < 0 and abs(res_m) < 1e-9:
            out.append({"name": "Descending triangle", "direction": "bearish",
                        "confidence": "low", "note": "flat support, falling resistance"})
    return out


def _setup(price: float, atr: float, supports: list[dict], resistances: list[dict],
           structure: dict, verdict: str | None) -> dict:
    """A verdict-aware, reward:risk-framed setup. For a bearish fundamental verdict the
    levels are shown *for reference only* — never a buy setup that contradicts the thesis.
    A setup is only called actionable when reward:risk ≥ 1.5; otherwise it's 'watch'."""
    v = (verdict or "").upper()
    if any(w in v for w in ("AVOID", "REDUCE", "SELL", "EXIT")):
        near = supports[0]["mid"] if supports else None
        return {"kind": "reference-only", "bias": "no long setup",
                "note": (f"Fundamental thesis is {verdict} — levels are shown for reference, "
                         "not a buy recommendation."
                         + (f" First support ₹{near:,.0f} if already held." if near else ""))}

    trend = structure.get("trend")
    if trend == "down" and not supports:
        res = resistances[0]["mid"] if resistances else None
        return {"kind": "watch", "bias": "downtrend — no base",
                "note": ("No confirmed base yet; wait for a structure break / reclaim above "
                         + (f"₹{res:,.0f}." if res else "the nearest resistance."))}

    if not supports:
        return {"kind": "watch", "bias": "no clean support below",
                "note": "Price sits above all mapped supports — chase risk; wait for a pullback."}

    # Entry = the STRONGEST support below (highest confluence), not merely the nearest —
    # a lone swing pip at price is a weaker floor than a multi-method zone a bit lower.
    sup = max(supports, key=lambda z: z["score"])
    entry_lo, entry_hi = sup["lo"], sup["hi"]
    entry_mid = sup["mid"]
    stop = round(sup["lo"] - atr, 2)
    at_support = price <= sup["hi"] + 0.5 * atr        # trading in/near the zone now
    zone = f"₹{entry_lo:,.0f}–₹{entry_hi:,.0f}"
    where = (f"the {zone} support zone (price is in it now)" if at_support
             else f"the {zone} support zone on a pullback")

    targets = [round(r["mid"], 2) for r in resistances[:2]]
    if not targets:                                   # price near highs — no overhead mapped
        return {"kind": "hold-trail", "bias": f"{trend} — at/near highs",
                "entry_lo": round(entry_lo, 2), "entry_hi": round(entry_hi, 2),
                "stop": stop, "targets": [], "rr": None,
                "note": ("Price is near its highs with no mapped resistance overhead — trail a "
                         f"stop (below ₹{stop:,.0f}) rather than aim at a level; add at {where}.")}
    risk = entry_mid - stop
    reward = targets[0] - entry_mid
    rr = round(reward / risk, 2) if risk > 0 else None
    kind = "accumulate" if (rr and rr >= 1.5) else "watch"
    if kind == "accumulate":
        note = (f"Accumulate at {where}; invalidation (stop) below ₹{stop:,.0f}; first target "
                f"₹{targets[0]:,.0f}" + (f" (reward:risk {rr:.1f}:1)." if rr else "."))
    else:
        note = (f"Near {where}, but reward:risk is thin"
                + (f" at {rr:.1f}:1" if rr else "")
                + f" — stop ₹{stop:,.0f}, first target ₹{targets[0]:,.0f}. "
                "Wait for a better entry or a confirmed breakout.")
    return {"kind": kind, "bias": f"{trend} structure", "entry_lo": round(entry_lo, 2),
            "entry_hi": round(entry_hi, 2), "stop": stop, "targets": targets, "rr": rr,
            "note": note}


def levels(con: duckdb.DuckDBPyConnection, symbol: str, *,
           verdict: str | None = None) -> dict:
    """Support/resistance zones, market structure, patterns and a verdict-aware
    entry/stop/target setup — all computed from the daily OHLCV. Never raises; returns
    ``{"history_ok": False}`` when the price history is too thin to be meaningful.

    ``verdict`` (the fundamental Buy/Accumulate/…/Avoid call) makes the setup defer to the
    thesis: for Avoid/Reduce/Sell it shows levels *for reference only*."""
    ind = indicators(con, symbol)
    if ind.empty or len(ind) < _MIN_DAYS:
        return {"history_ok": False, "n_days": 0 if ind.empty else len(ind),
                "reason": "insufficient price history for reliable levels"}
    win = ind.iloc[-_ZONE_LOOKBACK:]
    last = ind.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr14"]) if last["atr14"] == last["atr14"] else 0.02 * price
    tol = max(0.75 * atr, 0.012 * price)

    highs, lows = _swings(win["high"], win["low"])
    n = len(win)

    cands: list[tuple[float, float, str]] = []
    # swing pivots — recency-weighted (older pivots matter less)
    for pos, pr in highs + lows:
        rec = 0.5 + 0.5 * (pos / max(n - 1, 1))
        cands.append((pr, _SRC_W["swing"] * rec, "swing"))
    # moving averages (dynamic S/R)
    for col, src in (("sma20", "20-DMA"), ("sma50", "50-DMA"), ("sma200", "200-DMA")):
        v = last.get(col)
        if v == v and v:
            cands.append((float(v), _SRC_W[src], src))
    # 52-week extremes
    for col in ("high_52w", "low_52w"):
        v = last.get(col)
        if v == v and v:
            cands.append((float(v), _SRC_W["52w"], "52w"))
    # volume-by-price peaks (top 3 nodes)
    centers, vol = _volume_by_price(win)
    if len(vol) and vol.max() > 0:
        order = np.argsort(vol)[::-1][:3]
        for j in order:
            cands.append((float(centers[j]), _SRC_W["volume-node"] * (vol[j] / vol.max()),
                          "volume-node"))
    # round numbers — only those within a few % of price (far ones are noise)
    for r in _round_levels(price):
        if abs(r - price) / price <= 0.12:
            cands.append((r, _SRC_W["round"], "round"))

    zones = _cluster(cands, tol)
    supports = sorted([z for z in zones if z["mid"] < price * 0.998],
                      key=lambda z: (-z["score"], price - z["mid"]))[:3]
    resistances = sorted([z for z in zones if z["mid"] > price * 1.002],
                         key=lambda z: (-z["score"], z["mid"] - price))[:3]
    # nearest-first for the setup / reader
    supports = sorted(supports, key=lambda z: price - z["mid"])
    resistances = sorted(resistances, key=lambda z: z["mid"] - price)

    structure = _structure(highs, lows)
    sup_v, sup_m = _trendline(lows)
    res_v, res_m = _trendline(highs)
    patterns = _patterns(highs, lows, price, atr, supports, resistances)
    setup = _setup(price, atr, supports, resistances, structure, verdict)

    return {
        "history_ok": True,
        "reliable": n >= _RELIABLE_DAYS,
        "n_days": len(ind),
        "date": ind.index[-1],
        "close": round(price, 2),
        "atr": round(atr, 2),
        "atr_pct": round(100 * atr / price, 1) if price else None,
        "structure": structure,
        "supports": supports,
        "resistances": resistances,
        "trendlines": {"support": sup_v, "support_slope": sup_m,
                       "resistance": res_v, "resistance_slope": res_m},
        "patterns": patterns,
        "setup": setup,
    }


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
