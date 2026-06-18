"""Watchlist event detectors (Phase 5).

Per-symbol detectors that compare *today's* data against stored `alert_state`
(what we knew last run) and emit an Alert only on a genuine change. Groups:
  - price context    — 52-week extreme, delivery-% spike, big single-day move
    (the pure momentum signals — RSI / MA-cross / volume spike — were dropped to
    keep the focus fundamental/forensic, not trading)
  - fundamental      — forensic-score flips, CFO/PAT, P/E-vs-history
  - promoter pledge  — rise in pledged % of promoter holding (from `shareholding`)
  - announcements    — from the NSE per-symbol announcement feed
First sighting of a symbol seeds state silently (no day-one alert flood).
Market-wide FII/DII is handled by the scan orchestrator, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb
import numpy as np

from equity_research.analysis import forensic, fundamentals, technical, valuation

# Thresholds
DELIV_SPIKE = 1.5        # today deliv% / 20d avg
BIG_MOVE = 0.06          # |1-day return|
PLEDGE_RISE_PP = 1.0     # promoter-pledge rise (percentage points of promoter holding) to alert on

EMOJI = {"red": "🔴", "green": "🟢", "warn": "⚠️", "info": "🔔", "filing": "📄"}


@dataclass
class Alert:
    symbol: str
    severity: str         # red | green | warn | info | filing
    title: str
    body: str = ""
    attach_report: bool = False     # True => orchestrator generates & attaches deep report
    attachment: str = ""            # filing PDF URL (for auto doc-analysis)
    analysis: str = ""              # Gemini's inline read of the attached filing

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

    # 2) delivery-% spike (NSE conviction — kept: a fundamental-flavoured signal)
    if cur["deliv_avg20"] == cur["deliv_avg20"] and cur["deliv_avg20"] > 0 and cur["deliv_per"] > DELIV_SPIKE * cur["deliv_avg20"]:
        A.append(Alert(symbol, "info", "Delivery% spike",
                       f"{cur['deliv_per']:.0f}% vs 20d avg {cur['deliv_avg20']:.0f}% (institutional conviction)"))

    # 3) big single-day move (a 'something happened — look for news' trigger)
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
                           f"{state['altman_band']} → {zb} (Z {z.value:.2f}). Altman Z gauges "
                           "bankruptcy distance (>2.99 safe · <1.81 distress) — check leverage "
                           "& working capital."))
        up["altman_band"] = zb
    if m.value is not None and m.value == m.value:
        flagged = m.value > -1.78
        if state.get("beneish_flag") == "0" and flagged:
            A.append(Alert(symbol, "red", "Beneish M crossed −1.78",
                           f"now {m.value:.2f}. Beneish M is a statistical earnings-manipulation "
                           "screen (> −1.78 flags risk) — scrutinise receivables, margins & "
                           "accruals vs cash (a screen, not proof)."))
        up["beneish_flag"] = "1" if flagged else "0"
    if f.value is not None and f.value == f.value:
        if state.get("piotroski") and f.value <= _num(state["piotroski"]) - 2:
            A.append(Alert(symbol, "warn", "Piotroski F dropped",
                           f"{state['piotroski']} → {f.value:.0f}/9. Piotroski F is a 9-point "
                           "fundamental-strength score (8–9 strong, 0–2 weak) — weakening quality."))
        up["piotroski"] = f.value

    # 13) CFO-vs-PAT deterioration (latest annual)
    o = fundamentals.annual_overview(con, symbol)
    if not o.empty:
        cfo_pat = o["cfo_to_pat_x"].iloc[-1]
        net = o["net_profit_cr"].iloc[-1]
        # cash-backing-of-profit is only meaningful when there IS a profit; for a
        # loss year cfo/pat goes negative and would falsely "fall below 1".
        if cfo_pat == cfo_pat and net == net and net > 0:
            if state.get("cfo_pat_ok") == "1" and cfo_pat < 1.0:
                A.append(Alert(symbol, "red", "CFO/PAT fell below 1",
                               f"now {cfo_pat:.2f}x. CFO/PAT compares operating cash to reported "
                               "profit; <1 means earnings aren't backed by cash — an "
                               "earnings-quality flag (read the multi-year trend, can be lumpy)."))
            up["cfo_pat_ok"] = "1" if cfo_pat >= 1.0 else "0"

    # 14) valuation P/E breakout vs own history
    snap = valuation.snapshot(con, symbol)
    hist = valuation.valuation_history(con, symbol)
    if snap and not hist.empty and "pe" in hist:
        pe = snap.get("pe_ttm")
        pes = hist["pe"].dropna()
        pes = pes[pes > 0]                        # ignore loss-year P/Es
        if pe == pe and pe > 0 and len(pes):      # a negative P/E (loss) isn't "cheap"
            status = "above" if pe > pes.max() else "below" if pe < pes.min() else "within"
            if state.get("pe_band") in ("within", None, "") and status == "above":
                A.append(Alert(symbol, "warn", "P/E above historical range",
                               f"P/E {pe:.1f} > prior max {pes.max():.1f} — market paying more than "
                               "it ever has for this stock; justified only by stronger growth/quality."))
            elif state.get("pe_band") in ("within", None, "") and status == "below":
                A.append(Alert(symbol, "green", "P/E below historical range",
                               f"P/E {pe:.1f} < prior min {pes.min():.1f} — cheap vs its own history "
                               "(value opportunity, or the market pricing in deterioration)."))
            up["pe_band"] = status
    return A, up


# Routine/compliance noise we don't want cluttering the daily scan.
_ANN_NOISE = ("trading window", "newspaper publication", "newspaper advertisement",
              "duplicate share", "loss of share", "issue of duplicate",
              "investor complaint", "investor grievance", "compliance certificate",
              "regulation 74", "reg. 74", "regulation 7(3)", "advertisement in")

# Defined corporate-event taxonomy, checked in priority order (first match wins).
# Each: (keyword tuple, title, severity). Phrases match NSE's announcement subjects.
_ANN_EVENTS: list[tuple[tuple[str, ...], str, str]] = [
    (("rights issue",), "Rights issue", "info"),
    (("qualified institution", "qip"), "QIP / fund raising", "info"),
    (("preferential issue", "preferential allotment", "convertible warrant"), "Preferential issue", "info"),
    (("scheme of arrangement", "amalgamation", "de-merger", "demerger", "merger"), "Scheme / M&A", "warn"),
    (("open offer", "substantial acquisition", "(sast)", "takeover"), "Open offer / SAST", "warn"),
    (("buy back", "buy-back", "buyback"), "Buyback", "info"),
    (("bonus",), "Bonus issue", "info"),
    (("stock split", "sub-division", "subdivision", "face value split", "split of equity"), "Stock split", "info"),
    (("dividend",), "Dividend", "info"),
    (("raising of fund", "fund raising", "fund-raising"), "Fund raising", "info"),
    (("acquisition", "acquire", "investment in", "stake in", "disposal"), "Acquisition / disposal", "info"),
    (("pledge", "encumbr", "creation of charge", "satisfaction of charge"), "Promoter pledge / charge", "red"),
    (("insider", "prohibition of insider", "code of conduct", "(pit)"), "Insider-trading disclosure", "warn"),
    (("credit rating", "rating action", "reaffirm", "rating of"), "Credit rating update", "warn"),
    (("bagging", "receipt of order", "award of order", "awarding of order", "letter of award",
      "work order", "wins order", "secures order", "bags order", "new order", "purchase order"),
     "Order / contract win", "green"),
    (("board meeting", "outcome of board", "meeting of the board"), "Board meeting", "info"),
    (("annual general meeting", "extraordinary general", "postal ballot", "general meeting",
      " agm", " egm"), "Shareholder meeting", "info"),
    (("change in director", "key managerial", "resignation", "appointment", "cessation",
      "change in kmp", "auditor"), "Director / KMP change", "info"),
    (("delisting",), "Delisting", "warn"),
]


def _categorise(desc: str, text: str, has_xbrl: bool) -> tuple[str | None, str, bool]:
    """(title, severity, is_result) from an announcement's desc/text.

    Returns ``(None, ...)`` for routine compliance noise so the caller skips it.
    """
    blob = f"{desc} {text}".lower()
    if any(n in blob for n in _ANN_NOISE):
        return None, "info", False
    # investor-facing material first — a results-day presentation/transcript is a
    # concall, not "results" (keeps results from swallowing every XBRL filing).
    if any(kw in blob for kw in ("earnings call", "conference call", "concall", "analyst meet",
            "institutional investor", "investor meet", "investor presentation", "investor/analyst",
            "transcript", "audio recording", "schedule of analyst")):
        return "Concall / investor meet", "info", False
    if "financial result" in blob or "unaudited financial" in blob or "audited financial" in blob \
            or (has_xbrl and "result" in blob):
        return "Results filed", "filing", True
    for keywords, title, sev in _ANN_EVENTS:
        if any(kw in blob for kw in keywords):
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
    seen_titles: set[str] = set()
    for a in anns:                              # newest-first
        adt = dt(a)
        if last_dt is not None and adt <= last_dt:
            break
        title, sev, is_result = _categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                            str(a.get("hasXbrl", "")).lower() == "true")
        if title is None:                       # routine compliance noise — skip
            continue
        if title in seen_titles:                # collapse repeats of the same event type
            continue
        seen_titles.add(title)
        att = (a.get("attchmntFile") or "").strip()
        att = att if att.lower().startswith("http") and att.lower().endswith(".pdf") else ""
        body = (a.get("attchmntText") or a.get("desc") or "")[:180]
        A.append(Alert(symbol, sev, title, body, attach_report=is_result, attachment=att))
    return A, up


def _pledge(symbol, state, pledge) -> tuple[list[Alert], dict]:
    """Rise in pledged % of promoter holding (structured `shareholding` data)."""
    if not pledge:
        return [], {}
    cur = pledge.get("pledged_pct_of_promoter")
    if cur is None or cur != cur or not (0 <= cur <= 100):   # n/a or implausible (no real promoter)
        return [], {}
    up = {"pledge_pct": f"{cur:.2f}"}
    last = state.get("pledge_pct")
    if last is None:                       # first sighting: seed silently
        return [], up
    A: list[Alert] = []
    if cur > _num(last) + PLEDGE_RISE_PP:
        A.append(Alert(symbol, "red", "Promoter pledge rose",
                       f"{_num(last):.1f}% → {cur:.1f}% of promoter holding pledged. "
                       "Pledge = promoter shares posted as loan collateral; a rise can signal "
                       "promoter cash/leverage stress, and >50% is a serious red flag (forced-sale risk)."))
    return A, up


def scan_symbol(con: duckdb.DuckDBPyConnection, symbol: str, anns: list | None = None,
                pledge: dict | None = None) -> list[Alert]:
    """Run all per-symbol detectors. Seeds state silently on first sighting."""
    state = load_state(con, symbol)
    first_sight = "last_eod_date" not in state
    alerts, updates = [], {}
    for fn_alerts, fn_up in (_technical(con, symbol, state),
                             _fundamental(con, symbol, state),
                             _pledge(symbol, state, pledge),
                             _announcements(symbol, anns or [], state)):
        alerts += fn_alerts
        updates.update(fn_up)
    save_state(con, symbol, updates)
    return [] if first_sight else alerts
