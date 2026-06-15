"""Watchlist event detectors (Phase 5).

Per-symbol detectors that compare *today's* data against stored `alert_state`
(what we knew last run) and emit an Alert only on a genuine change. Three groups:
  - technical/price  (events 1-6) — from `equity_eod`, cheap, daily
  - fundamental      (events 12-14) — from ingested `financials`
  - announcements    (events 7-11) — from the NSE per-symbol announcement feed
First sighting of a symbol seeds state silently (no day-one alert flood).
Market-wide FII/DII (15) is handled by the scan orchestrator, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import duckdb
import numpy as np

from equity_research.analysis import forensic, fundamentals, technical, valuation

# Thresholds
VOL_SPIKE = 2.0          # today vol / 20d avg
DELIV_SPIKE = 1.5        # today deliv% / 20d avg
BIG_MOVE = 0.06          # |1-day return|
RSI_OB, RSI_OS = 70, 30

EMOJI = {"red": "🔴", "green": "🟢", "warn": "⚠️", "info": "🔔", "filing": "📄"}


@dataclass
class Alert:
    symbol: str
    severity: str         # red | green | warn | info | filing
    title: str
    body: str = ""
    attach_report: bool = False     # True => orchestrator generates & attaches deep report

    def render(self) -> str:
        head = f"{EMOJI.get(self.severity, '🔔')} **{self.symbol}** — {self.title}"
        return f"{head}\n{self.body}" if self.body else head


# ---------------- state helpers ----------------
def load_state(con: duckdb.DuckDBPyConnection, symbol: str) -> dict[str, str]:
    return dict(con.execute(
        "SELECT key, value FROM alert_state WHERE symbol = ?", [symbol]).fetchall())


def save_state(con: duckdb.DuckDBPyConnection, symbol: str, updates: dict) -> None:
    for k, v in updates.items():
        con.execute(
            "INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
            "VALUES (?, ?, ?, now())", [symbol, k, str(v)])


def _num(s, default=np.nan):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


# ---------------- detectors ----------------
def _technical(con, symbol, state) -> tuple[list[Alert], dict]:
    ind = technical.indicators(con, symbol)
    if len(ind) < 2:
        return [], {}
    cur, prev = ind.iloc[-1], ind.iloc[-2]
    cur_date = str(ind.index[-1].date())
    if state.get("last_eod_date") == cur_date:        # already scanned today
        return [], {}
    up = {"last_eod_date": cur_date}
    A: list[Alert] = []
    c, pc = cur["close"], prev["close"]

    # 1) 52-week high / low (today is the extreme, and it's a NEW extreme)
    if c == c and cur["high_52w"] == cur["high_52w"]:
        if c >= cur["high_52w"] and cur["high_52w"] > _num(state.get("last_52w_high"), -1):
            A.append(Alert(symbol, "green", "New 52-week high", f"₹{c:,.2f}"))
        up["last_52w_high"] = cur["high_52w"]
        if c <= cur["low_52w"] and (state.get("last_52w_low") is None
                                    or cur["low_52w"] < _num(state.get("last_52w_low"), 1e18)):
            A.append(Alert(symbol, "red", "New 52-week low", f"₹{c:,.2f}"))
        up["last_52w_low"] = cur["low_52w"]

    # 2) golden / death cross (50 vs 200 DMA)
    if all(x == x for x in (cur["sma50"], cur["sma200"], prev["sma50"], prev["sma200"])):
        now_sign = cur["sma50"] >= cur["sma200"]
        prev_sign = prev["sma50"] >= prev["sma200"]
        if now_sign != prev_sign:
            if now_sign:
                A.append(Alert(symbol, "green", "Golden cross",
                               f"50-DMA ({cur['sma50']:,.0f}) crossed above 200-DMA ({cur['sma200']:,.0f})"))
            else:
                A.append(Alert(symbol, "red", "Death cross",
                               f"50-DMA ({cur['sma50']:,.0f}) crossed below 200-DMA ({cur['sma200']:,.0f})"))

    # 3) 200-DMA cross by price
    if all(x == x for x in (c, pc, cur["sma200"], prev["sma200"])):
        if (c >= cur["sma200"]) != (pc >= prev["sma200"]):
            above = c >= cur["sma200"]
            A.append(Alert(symbol, "info",
                           f"Price crossed {'above' if above else 'below'} 200-DMA",
                           f"₹{c:,.2f} vs 200-DMA ₹{cur['sma200']:,.0f}"))

    # 4) RSI extreme (on entry)
    if cur["rsi14"] == cur["rsi14"] and prev["rsi14"] == prev["rsi14"]:
        if cur["rsi14"] > RSI_OB and prev["rsi14"] <= RSI_OB:
            A.append(Alert(symbol, "warn", "RSI overbought", f"RSI {cur['rsi14']:.0f} (>70)"))
        elif cur["rsi14"] < RSI_OS and prev["rsi14"] >= RSI_OS:
            A.append(Alert(symbol, "warn", "RSI oversold", f"RSI {cur['rsi14']:.0f} (<30)"))

    # 5) volume spike
    if cur["vol_avg20"] == cur["vol_avg20"] and cur["vol_avg20"] > 0 and cur["volume"] > VOL_SPIKE * cur["vol_avg20"]:
        A.append(Alert(symbol, "info", "Volume spike",
                       f"{cur['volume']/cur['vol_avg20']:.1f}x the 20-day average"))

    # 6) delivery-% spike (NSE conviction)
    if cur["deliv_avg20"] == cur["deliv_avg20"] and cur["deliv_avg20"] > 0 and cur["deliv_per"] > DELIV_SPIKE * cur["deliv_avg20"]:
        A.append(Alert(symbol, "info", "Delivery% spike",
                       f"{cur['deliv_per']:.0f}% vs 20d avg {cur['deliv_avg20']:.0f}% (institutional conviction)"))

    # 6b) big single-day move
    if c == c and pc == pc and pc > 0:
        chg = c / pc - 1
        if abs(chg) > BIG_MOVE:
            A.append(Alert(symbol, "warn" if chg < 0 else "green", "Big move",
                           f"{chg*100:+.1f}% to ₹{c:,.2f}"))
    return A, up


def _fundamental(con, symbol, state) -> tuple[list[Alert], dict]:
    a = fundamentals.load_annual(con, symbol)
    if a.empty:
        return [], {}
    A, up = [], {}

    z = forensic.altman_z(con, symbol)
    f = forensic.piotroski_f(con, symbol)
    m = forensic.beneish_m(con, symbol)

    def band(zv):
        if zv is None or zv != zv:
            return None
        return "safe" if zv > 2.99 else "distress" if zv < 1.81 else "grey"

    # 12) forensic flips
    zb = band(z.value)
    if zb:
        if state.get("altman_band") and state["altman_band"] != zb and \
                ["distress", "grey", "safe"].index(zb) < ["distress", "grey", "safe"].index(state["altman_band"]):
            A.append(Alert(symbol, "red", "Altman Z deteriorated",
                           f"{state['altman_band']} → {zb} (Z {z.value:.2f})"))
        up["altman_band"] = zb
    if m.value is not None and m.value == m.value:
        flagged = m.value > -1.78
        if state.get("beneish_flag") == "0" and flagged:
            A.append(Alert(symbol, "red", "Beneish M crossed −1.78",
                           f"now {m.value:.2f} — possible earnings-manipulation flag"))
        up["beneish_flag"] = "1" if flagged else "0"
    if f.value is not None and f.value == f.value:
        if state.get("piotroski") and f.value <= _num(state["piotroski"]) - 2:
            A.append(Alert(symbol, "warn", "Piotroski F dropped",
                           f"{state['piotroski']} → {f.value:.0f}/9"))
        up["piotroski"] = f.value

    # 13) CFO-vs-PAT deterioration (latest annual)
    o = fundamentals.annual_overview(con, symbol)
    if not o.empty:
        cfo_pat = o["cfo_to_pat_x"].iloc[-1]
        if cfo_pat == cfo_pat:
            if state.get("cfo_pat_ok") == "1" and cfo_pat < 1.0:
                A.append(Alert(symbol, "red", "CFO/PAT fell below 1",
                               f"now {cfo_pat:.2f}x — cash no longer backs profit"))
            up["cfo_pat_ok"] = "1" if cfo_pat >= 1.0 else "0"

    # 14) valuation P/E breakout vs own history
    snap = valuation.snapshot(con, symbol)
    hist = valuation.valuation_history(con, symbol)
    if snap and not hist.empty and "pe" in hist:
        pe = snap.get("pe_ttm")
        pes = hist["pe"].dropna()
        if pe == pe and len(pes):
            status = "above" if pe > pes.max() else "below" if pe < pes.min() else "within"
            if state.get("pe_band") in ("within", None, "") and status == "above":
                A.append(Alert(symbol, "warn", "P/E above historical range",
                               f"P/E {pe:.1f} > prior max {pes.max():.1f}"))
            elif state.get("pe_band") in ("within", None, "") and status == "below":
                A.append(Alert(symbol, "green", "P/E below historical range",
                               f"P/E {pe:.1f} < prior min {pes.min():.1f}"))
            up["pe_band"] = status
    return A, up


def _categorise(desc: str, text: str, has_xbrl: bool) -> tuple[str, str, bool]:
    """(title, severity, is_result) from an announcement's desc/text."""
    blob = f"{desc} {text}".lower()
    if has_xbrl or "financial result" in blob or ("result" in blob and "board" in blob):
        return "Results filed", "filing", True
    for kw, title, sev in [
        ("pledge", "Promoter pledge update", "red"),
        ("encumbr", "Promoter encumbrance update", "red"),
        ("insider", "Insider trading disclosure", "red"),
        ("acquisition of shares", "SAST disclosure", "red"),
        ("rating", "Credit rating update", "warn"),
        ("dividend", "Dividend announced", "info"),
        ("bonus", "Bonus issue", "info"),
        ("split", "Stock split", "info"),
        ("buyback", "Buyback", "info"),
    ]:
        if kw in blob:
            return title, sev, False
    return "Announcement", "info", False


def _announcements(symbol, anns, state) -> tuple[list[Alert], dict]:
    """anns: list of announcement dicts for this symbol (newest first)."""
    if not anns:
        return [], {}
    def dt(a):
        try:
            return datetime.strptime(a.get("an_dt", "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            return datetime.min
    anns = sorted(anns, key=dt, reverse=True)
    last_seen = state.get("last_ann_dt")
    newest = dt(anns[0])
    up = {"last_ann_dt": newest.isoformat()}
    if not last_seen:                 # first time we see this symbol's feed: seed, no alerts
        return [], up
    last_dt = datetime.fromisoformat(last_seen)
    A: list[Alert] = []
    for a in anns:
        adt = dt(a)
        if last_dt is not None and adt <= last_dt:
            break
        title, sev, is_result = _categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                            str(a.get("hasXbrl", "")).lower() == "true")
        body = (a.get("attchmntText") or a.get("desc") or "")[:180]
        A.append(Alert(symbol, sev, title, body, attach_report=is_result))
    return A, up


def scan_symbol(con: duckdb.DuckDBPyConnection, symbol: str, anns: list | None = None) -> list[Alert]:
    """Run all per-symbol detectors. Seeds state silently on first sighting."""
    state = load_state(con, symbol)
    first_sight = "last_eod_date" not in state
    alerts, updates = [], {}
    for fn_alerts, fn_up in (_technical(con, symbol, state),
                             _fundamental(con, symbol, state),
                             _announcements(symbol, anns or [], state)):
        alerts += fn_alerts
        updates.update(fn_up)
    save_state(con, symbol, updates)
    return [] if first_sight else alerts
