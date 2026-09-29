"""Track record — every call the tool makes, logged as it goes out and scored against the market.

A **call** is a deep-report verdict (Buy / Accumulate / Hold / Reduce / Avoid) or a name an idea
engine put on its list (Tailwind, Pickaxe, Hotlist, a screen, Concalls, Results Radar). Calls are
**append-only**: logged the moment they're sent, never edited, never regenerated for a past date.

Scoring (all in Python, on split / bonus / rights / demerger-adjusted prices — ``equity_eod_adj``):

* **Entry** = the opening price of the first session *after* the call (a call made before the
  09:15 IST open uses that day's open) — the first price anyone acting on it could have got.
* **Exit** = the close of the H-th session from entry, for H = 1 week / 1 month / **3 months
  (the declared primary horizon)** / 6 months / 12 months (5 / 21 / 63 / 126 / 252 sessions).
* **Excess return** = the stock's return − the **Nifty 500**'s over the same sessions (open → close,
  both price returns, so dividends are excluded on both sides).
* **Hit**: a long call beats the Nifty 500; an Avoid / Reduce call *lags* it; a Hold stays within a
  band fixed in advance (±2 / 3 / 5 / 7 / 10 % by horizon) and is reported separately.
* An engine's list on one day is also scored as an **equal-weight basket**.
* Every rate carries its sample size and a 95 % Wilson interval; below ``MIN_SAMPLE`` matured calls a
  figure is shown as too few to read.

What the LLM sees: **nothing from here.** Past outcomes are rendered for the *reader* only (the "last
time we said…" line); they're never put into a prompt, because showing a model its hits and misses
invites hindsight bias and drifting toward safe "Hold" verdicts, and short-horizon returns are too
noisy to learn from (see docs/TRACK_RECORD.md).
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, time, timedelta, timezone

import duckdb
import pandas as pd

from equity_research import config

BENCHMARK = "Nifty 500"
HORIZONS: dict[str, int] = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "12m": 252}
PRIMARY = "3m"
HOLD_BAND = {"1w": 2.0, "1m": 3.0, "3m": 5.0, "6m": 7.0, "12m": 10.0}   # ± % vs the benchmark
MIN_SAMPLE = config.TRACK_MIN_SAMPLE
_OPEN_IST = time(9, 15)
_IST = timezone(timedelta(hours=5, minutes=30))

# engines whose numbered lists are ideas worth scoring (menus that only disambiguate a name, list
# your own holdings, or pick an IPO / fund are not calls)
IDEA_SOURCES = ("tailwind", "pickaxe", "hotlist", "calls", "results", "screen:value", "screen:volume",
                "screen:beaters", "screen:institutions", "screen:margins", "screen:deleverage",
                "screen:quality", "screen:holdco", "screen:investors", "screen:smallcap",
                "screen:technical", "screen:policy")
SOURCE_NAMES = {"deep_report": "Deep-report verdicts", "tailwind": "💨 Tailwind", "pickaxe": "⛏️ Pickaxe",
                "hotlist": "🔥 Hotlist", "calls": "🎙️ Concalls", "results": "📈 Results Radar",
                "screen:value": "Value", "screen:volume": "Volume breakouts",
                "screen:beaters": "Market beaters", "screen:institutions": "Institutional buying",
                "screen:margins": "Margin momentum", "screen:deleverage": "Debt payers",
                "screen:quality": "Compounders", "screen:holdco": "Holdcos below NAV",
                "screen:investors": "Marquee investors", "screen:smallcap": "Small-cap capex",
                "screen:technical": "Chart setups", "screen:policy": "Policy radar"}

_VERDICTS = {"buy": "long", "accumulate": "long", "hold": "hold", "reduce": "avoid", "avoid": "avoid"}


def enabled() -> bool:
    return config.TRACK_RECORD_ENABLED


# ------------------------------------------------------------------ logging
def _call_id(source: str, symbol: str, made_at: datetime) -> str:
    day = made_at.astimezone(_IST).date().isoformat()
    return hashlib.sha1(f"{source}|{symbol.upper()}|{day}".encode()).hexdigest()[:16]


def _utc(made_at: datetime | None) -> datetime:
    t = made_at or datetime.now(timezone.utc)
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def log_call(con: duckdb.DuckDBPyConnection, source: str, symbol: str, stance: str, label: str, *,
             rank: int | None = None, context: str = "", provenance: str = "live",
             made_at: datetime | None = None, ref: str = "") -> bool:
    """Record one call. The first call per (source, symbol, day) wins — a re-sent report or a cached
    list the same day adds nothing. Returns True when a row was written."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return False
    t = _utc(made_at)
    cid = _call_id(source, symbol, t)
    if con.execute("SELECT 1 FROM calls WHERE call_id = ?", [cid]).fetchone():
        return False
    con.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [cid, t.replace(tzinfo=None), source, symbol, stance, label, rank,
                 (context or "")[:300], provenance, (ref or "")[:300]])
    return True


def log_basket(con: duckdb.DuckDBPyConnection, source: str, symbols: list[str], *, context: str = "",
               provenance: str = "live", made_at: datetime | None = None, ref: str = "") -> int:
    """Record an engine's list (in its ranked order) as long calls. Returns rows written."""
    n = 0
    for i, sym in enumerate([s for s in symbols if s], 1):
        n += log_call(con, source, sym, "long", "PICK", rank=i, context=context,
                      provenance=provenance, made_at=made_at, ref=ref)
    return n


def parse_verdict(report_md: str) -> tuple[str, str]:
    """(label, stance) from a deep report's closing Verdict section. The prompt fixes the scale —
    Buy / Accumulate / Hold / Reduce / Avoid; anything unreadable is ('REVIEW', 'review') and never
    scored, rather than silently counted as a Hold."""
    text = report_md or ""
    heads = [m.end() for m in re.finditer(r"(?im)^#{1,4}[^\n]*\bverdict\b[^\n]*$", text)]
    if not heads:
        return "REVIEW", "review"
    section = text[heads[-1]:heads[-1] + 1500]
    m = (re.search(r"(?i)verdict\W{0,8}(buy|accumulate|hold|reduce|avoid)\b", section)
         or re.search(r"(?i)\*\*\s*(buy|accumulate|hold|reduce|avoid)\b", section)
         or re.search(r"(?i)\b(buy|accumulate|hold|reduce|avoid)\b", section[:400]))
    if not m:
        return "REVIEW", "review"
    word = m.group(1).lower()
    return word.upper(), _VERDICTS[word]


# ------------------------------------------------------------------ scoring
def _entry_session(made_at: datetime, sessions: list[date]) -> date | None:
    """The first session a reader could have traded: that day's open if the call went out before
    09:15 IST on a session day, else the next session."""
    local = made_at.replace(tzinfo=timezone.utc).astimezone(_IST)
    d = local.date()
    for s in sessions:
        if s > d or (s == d and local.time() < _OPEN_IST):
            return s
    return None


def _wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (float("nan"), float("nan"))
    p = hits / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (100 * (mid - half), 100 * (mid + half))


def outcomes(con: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> pd.DataFrame:
    """One row per call per horizon that has **matured** by ``as_of`` (default: the last session on
    file): entry/exit prices, stock and benchmark returns, excess, and whether it's a hit. Plus a
    ``to_date`` row per call (return so far, not a horizon — shown, never scored)."""
    calls = con.execute("SELECT * FROM calls WHERE stance IN ('long', 'avoid', 'hold') "
                        "ORDER BY made_at").df()
    cols = ["call_id", "source", "symbol", "stance", "label", "made_at", "provenance", "horizon",
            "entry_date", "entry", "exit_date", "exit", "ret", "bench", "excess", "hit"]
    if calls.empty:
        return pd.DataFrame(columns=cols)
    bench = con.execute(
        "SELECT trade_date, open, close FROM index_close WHERE index_name = ? ORDER BY trade_date",
        [BENCHMARK]).df()
    if bench.empty:
        return pd.DataFrame(columns=cols)
    bench["trade_date"] = pd.to_datetime(bench["trade_date"]).dt.date
    if as_of is not None:
        bench = bench[bench["trade_date"] <= as_of]
    sessions = list(bench["trade_date"])
    b_open = dict(zip(bench["trade_date"], bench["open"]))
    b_close = dict(zip(bench["trade_date"], bench["close"]))
    pos = {d: i for i, d in enumerate(sessions)}

    syms = sorted(set(calls["symbol"]))
    px = con.execute(
        f"""SELECT symbol, trade_date, open, close FROM equity_eod_adj
            WHERE symbol IN ({','.join('?' * len(syms))}) AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
            QUALIFY row_number() OVER (PARTITION BY symbol, trade_date
                                       ORDER BY CASE series WHEN 'EQ' THEN 0 ELSE 1 END) = 1""",
        syms).df()
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.date
    by_sym = {s: g.set_index("trade_date").sort_index() for s, g in px.groupby("symbol")}

    rows = []
    for c in calls.itertuples(index=False):
        ent = _entry_session(pd.Timestamp(c.made_at).to_pydatetime(), sessions)
        g = by_sym.get(c.symbol)
        if ent is None or g is None or ent not in g.index or not b_open.get(ent):
            continue                                   # not traded yet / no price at entry
        e_px, e_b = float(g.at[ent, "open"]), float(b_open[ent])
        if not e_px or not e_b:
            continue
        i0 = pos[ent]
        todo = [(h, i0 + n - 1) for h, n in HORIZONS.items()] + [("to_date", len(sessions) - 1)]
        for h, j in todo:
            if j >= len(sessions):
                continue                               # this horizon hasn't finished trading
            x_day = sessions[j]
            held = g.loc[:x_day]
            if held.empty:
                continue
            x_px = float(held["close"].iloc[-1])       # last close on/before exit (suspension-safe)
            ret = 100 * (x_px / e_px - 1)
            bret = 100 * (b_close[x_day] / e_b - 1)
            ex = ret - bret
            if c.stance == "long":
                hit = ex > 0
            elif c.stance == "avoid":
                hit = ex < 0
            else:
                hit = abs(ex) <= HOLD_BAND.get(h, 5.0)
            rows.append([c.call_id, c.source, c.symbol, c.stance, c.label, c.made_at, c.provenance, h,
                         ent, e_px, x_day, x_px, ret, bret, ex, hit if h != "to_date" else None])
    return pd.DataFrame(rows, columns=cols)


def scorecard(con: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> dict:
    """Per-source, per-horizon statistics over matured calls:
    ``{rows:[{source, stance, horizon, n, hits, hit_rate, ci, mean_excess, median_excess,
    basket_mean, baskets, live, recovered, enough}], totals:{…}, outcomes: DataFrame}``."""
    oc = outcomes(con, as_of=as_of)
    logged = con.execute("SELECT source, stance, provenance, count(*) FROM calls "
                         "GROUP BY ALL").fetchall()
    rows = []
    scored = oc[oc["horizon"] != "to_date"]
    if not scored.empty:
        scored = scored.assign(signed=scored.apply(
            lambda r: -r["excess"] if r["stance"] == "avoid" else r["excess"], axis=1),
            day=pd.to_datetime(scored["made_at"]).dt.date)
        for (src, stance, h), g in scored.groupby(["source", "stance", "horizon"]):
            n, hits = len(g), int(g["hit"].sum())
            baskets = g.groupby("day")["signed"].mean()
            rows.append({"source": src, "stance": stance, "horizon": h, "n": n, "hits": hits,
                         "hit_rate": 100 * hits / n, "ci": _wilson(hits, n),
                         "mean_excess": float(g["signed"].mean()),
                         "median_excess": float(g["signed"].median()),
                         "basket_mean": float(baskets.mean()), "baskets": len(baskets),
                         "live": int((g["provenance"] == "live").sum()),
                         "recovered": int((g["provenance"] != "live").sum()),
                         "enough": n >= MIN_SAMPLE})
    return {"rows": rows, "outcomes": oc,
            "logged": [{"source": s, "stance": st, "provenance": p, "n": n} for s, st, p, n in logged]}


def last_call(con: duckdb.DuckDBPyConnection, symbol: str, *, before: datetime | None = None) -> dict | None:
    """The most recent **deep-report verdict** on ``symbol`` from an earlier day, with how the stock
    has done since (vs the Nifty 500). For the reader only — never passed to the LLM."""
    t = _utc(before)
    today = t.astimezone(_IST).date()
    row = con.execute(
        """SELECT call_id, made_at, label, stance FROM calls
           WHERE symbol = ? AND source = 'deep_report' AND made_at < ?
           ORDER BY made_at DESC""", [symbol.upper(), t.replace(tzinfo=None)]).fetchall()
    row = next((r for r in row
                if pd.Timestamp(r[1]).tz_localize("UTC").astimezone(_IST).date() < today), None)
    if not row:
        return None
    oc = outcomes(con)
    mine = oc[(oc["call_id"] == row[0]) & (oc["horizon"] == "to_date")]
    out = {"made_at": pd.Timestamp(row[1]).tz_localize("UTC").astimezone(_IST), "label": row[2],
           "stance": row[3], "since": None}
    if not mine.empty:
        r = mine.iloc[0]
        out["since"] = {"entry_date": r["entry_date"], "entry": r["entry"], "exit_date": r["exit_date"],
                        "last": r["exit"], "ret": r["ret"], "bench": r["bench"], "excess": r["excess"]}
    return out


def memory_line(con: duckdb.DuckDBPyConnection, symbol: str) -> str | None:
    """The one-paragraph 'last time we said…' note for the top of a deep report, or None."""
    if not (enabled() and config.REPORT_MEMORY_ENABLED):
        return None
    lc = last_call(con, symbol)
    if not lc:
        return None
    when = f"{lc['made_at']:%d-%b-%Y}"
    s = lc["since"]
    if not s:
        return (f"> 📜 **Last time** ({when}) this tool's verdict on {symbol} was **{lc['label'].title()}** "
                "— not scored yet (no session traded since).")
    return (f"> 📜 **Last time** ({when}) this tool's verdict on {symbol} was **{lc['label'].title()}**. "
            f"From the next open (₹{s['entry']:,.2f} on {s['entry_date']:%d-%b}) to ₹{s['last']:,.2f} "
            f"({s['exit_date']:%d-%b}) it moved **{s['ret']:+.1f}%** vs the {BENCHMARK} **{s['bench']:+.1f}%** "
            f"— {abs(s['excess']):.1f} pts {'ahead of' if s['excess'] >= 0 else 'behind'} the market. "
            "_Shown for context only; the analysis below was written without it._")
