"""🛡️ Thesis Guard + Exit Plan — "why did I buy this, and is that still true?"

You write why you own a stock and, optionally, your rules ("exit below 250, add below 280, trim above
450, trail 15%"). The LLM turns the words into **checks** from a fixed catalogue; every check that can
be measured is then computed here from the company's own filings and prices, and re-checked every
evening. Only reasons that can't be measured ("order book keeps growing") are judged by the LLM — from
the company's recent filings and concall notes, citing the one it relied on.

Each check reads **intact / weakening / broken** (or *unknown* when the data isn't there); the thesis is
as weak as its weakest check. A price rule reads **triggered** or not. The evening sweep emails you only
when a check's status changes or a rule triggers — so silence means nothing moved.

It judges *your* reasons against filed facts; it never tells you to buy or sell.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta

import duckdb

from equity_research.analysis import fundamentals, quant, valuation

log = logging.getLogger(__name__)
CR = 1e7
NEAR = 0.10                 # within 10% of your line (on the wrong side of comfortable) → weakening
HOLDING_DROP_BROKEN = 1.0   # percentage points — a stake falling this much in a quarter breaks "not falling"
HOLDING_DROP_WEAK = 0.25
ORDER = {"broken": 3, "weakening": 2, "unknown": 1, "intact": 0}
ICON = {"intact": "🟢", "weakening": "🟡", "broken": "🔴", "unknown": "⚪"}

# The measurable catalogue: metric → (plain name, unit, how to read it). The LLM may only use these
# names; anything else becomes a "judged" check.
METRICS = {
    "revenue_growth": ("Revenue growth (latest quarter, YoY)", "%"),
    "profit_growth": ("Net profit growth (latest quarter, YoY)", "%"),
    "ebitda_margin": ("EBITDA margin (latest quarter)", "%"),
    "net_margin": ("Net margin (latest quarter)", "%"),
    "roe": ("Return on equity (last full year)", "%"),
    "roce": ("Return on capital employed (last full year)", "%"),
    "debt_to_equity": ("Debt to equity (last full year)", "x"),
    "pe": ("P/E (trailing 12 months)", "x"),
    "promoter_holding": ("Promoter holding", "%"),
    "mf_holding": ("Mutual-fund holding (named >1% holders)", "%"),
    "fpi_holding": ("Foreign-institution holding (named >1% holders)", "%"),
    "pledge": ("Promoter shares pledged", "% of promoter holding"),
}
PRICE_RULES = {"exit_below": "Exit if it closes below", "add_below": "Add if it closes below",
               "trim_above": "Trim if it closes above", "trailing_stop": "Trailing stop — exit if it falls"}


# ------------------------------------------------------------------ measuring
def _holding_series(con, symbol: str, where: str) -> list[tuple[date, float]]:
    return con.execute(
        f"SELECT as_of, sum(pct) FROM shp_holders WHERE symbol = ? AND {where} GROUP BY as_of ORDER BY as_of",
        [symbol]).fetchall()


def measure(con: duckdb.DuckDBPyConnection, symbol: str, metric: str) -> tuple[float | None, float | None, str]:
    """(current value, previous value — for trend checks, how it's sourced)."""
    if metric in ("revenue_growth", "profit_growth", "ebitda_margin", "net_margin"):
        m = fundamentals.latest_quarters(con, symbol)
        col = {"revenue_growth": "rev_yoy_%", "profit_growth": "net_yoy_%",
               "ebitda_margin": "ebitda_margin_%", "net_margin": "net_margin_%"}[metric]
        if m.empty or col not in m or m[col].dropna().empty:
            return None, None, "no quarterly results on file"
        s = m[col].dropna()
        return float(s.iloc[-1]), (float(s.iloc[-2]) if len(s) > 1 else None), f"quarter ending {s.index[-1]:%b-%Y}"
    if metric in ("roe", "roce", "debt_to_equity"):
        r = {}
        for cons in (True, False):
            r = quant._ratios(con, symbol, cons) or {}
            if r:
                break
        key = {"roe": "ROE%", "roce": "ROCE%", "debt_to_equity": "D/E"}[metric]
        v = r.get(key)
        return (float(v) if v is not None and v == v else None), None, "last full-year filing"
    if metric == "pe":
        v = (valuation.snapshot(con, symbol) or {}).get("pe_ttm")
        return (float(v) if v is not None and v == v and v > 0 else None), None, "latest price ÷ trailing profit"
    if metric in ("promoter_holding", "mf_holding", "fpi_holding"):
        where = {"promoter_holding": "is_promoter",
                 "mf_holding": "category ILIKE '%mutual%'",
                 "fpi_holding": "(category = 'FPI' OR category ILIKE '%foreign portfolio%')"}[metric]
        s = _holding_series(con, symbol, where)
        if not s:
            return None, None, "no shareholding filing on file"
        return float(s[-1][1]), (float(s[-2][1]) if len(s) > 1 else None), f"shareholding as of {s[-1][0]:%d-%b-%Y}"
    if metric == "pledge":
        r = con.execute("SELECT pledged_pct_of_promoter, period_end FROM shareholding WHERE symbol = ? "
                        "ORDER BY period_end DESC LIMIT 2", [symbol]).fetchall()
        if not r:
            return None, None, "no pledge disclosure on file"
        return float(r[0][0] or 0), (float(r[1][0] or 0) if len(r) > 1 else None), f"as of {r[0][1]:%d-%b-%Y}"
    return None, None, "unknown metric"


def _metric_status(check: dict, v: float | None, prev: float | None) -> str:
    if v is None:
        return "unknown"
    op, t = check.get("op"), check.get("value")
    if op in (">=", ">") and t is not None:
        if v < t:
            return "broken"
        return "weakening" if v < t + abs(t) * NEAR or (prev is not None and v < prev and v < t * 1.25) else "intact"
    if op in ("<=", "<") and t is not None:
        if v > t:
            return "broken"
        return "weakening" if v > t - abs(t) * NEAR else "intact"
    if op in ("not_falling", "rising"):
        if prev is None:
            return "unknown"
        d = v - prev
        if op == "rising":
            return "intact" if d > 0 else ("weakening" if d > -HOLDING_DROP_WEAK else "broken")
        return "broken" if d <= -HOLDING_DROP_BROKEN else ("weakening" if d <= -HOLDING_DROP_WEAK else "intact")
    return "unknown"


def _fmt(v, unit: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}x" if unit == "x" else f"{v:,.1f}%"


def _describe(check: dict) -> str:
    """A check in words, e.g. 'Debt to equity ≤ 0.5x'."""
    if check["kind"] == "judged":
        return check.get("question", "")
    if check["kind"] == "price":
        rule, v = check["rule"], check.get("value")
        return f"{PRICE_RULES[rule]} {v:g}% from its peak" if rule == "trailing_stop" else f"{PRICE_RULES[rule]} ₹{v:,.2f}"
    name, unit = METRICS[check["metric"]]
    op = check.get("op")
    if op in ("not_falling", "rising"):
        return f"{name} {'not falling' if op == 'not_falling' else 'rising'}"
    return f"{name} {'≥' if op in ('>=', '>') else '≤'} {_fmt(check.get('value'), unit)}"


def _prices(con, symbol: str, since: date | None) -> tuple[float | None, date | None, float | None]:
    """(last close, its date, the highest close since ``since``) on adjusted prices."""
    row = con.execute("SELECT close, trade_date FROM equity_eod_adj WHERE symbol = ? AND series IN ('EQ','BE','BZ') "
                      "ORDER BY trade_date DESC LIMIT 1", [symbol]).fetchone()
    if not row:
        return None, None, None
    peak = con.execute("SELECT max(close) FROM equity_eod_adj WHERE symbol = ? AND series IN ('EQ','BE','BZ') "
                       "AND trade_date >= ?", [symbol, since or row[1]]).fetchone()[0]
    return float(row[0]), row[1], max(float(peak), float(row[0])) if peak else float(row[0])  # starts at today's close


def evaluate(con: duckdb.DuckDBPyConnection, thesis: dict, *, judge=None) -> dict:
    """Every check's status now. ``judge(symbol, [questions]) -> [{status, note, evidence_url}]`` handles
    the judged checks (injected so tests and the no-LLM path can skip it).
    Returns ``{overall, checks:[{…check, status, now, prev, source, note}], rules:[{…, triggered, note}],
    price, price_date}``."""
    sym = thesis["symbol"]
    out_checks, judged = [], []
    for c in thesis["checks"]:
        if c["kind"] == "metric" and c.get("metric") in METRICS:
            v, prev, src = measure(con, sym, c["metric"])
            unit = METRICS[c["metric"]][1]
            note = f"now {_fmt(v, unit)}" + (f" (was {_fmt(prev, unit)})" if prev is not None else "")
            out_checks.append({**c, "status": _metric_status(c, v, prev), "now": v, "prev": prev,
                               "source": src, "note": note})
        elif c["kind"] == "judged":
            judged.append(len(out_checks))
            out_checks.append({**c, "status": "unknown", "note": "", "source": ""})
    if judged and judge is not None:
        try:
            res = judge(sym, [out_checks[i]["question"] for i in judged])
        except Exception:  # noqa: BLE001
            log.exception("thesis guard: judging %s failed", sym)
            res = []
        for i, r in zip(judged, res):
            out_checks[i].update(status=r.get("status", "unknown"), note=r.get("note", ""),
                                 source=r.get("evidence_url", ""))

    created = thesis.get("created_at")
    since = created.date() if isinstance(created, datetime) else created
    last, last_d, peak = _prices(con, sym, since)
    rules = []
    for c in thesis["checks"]:
        if c["kind"] != "price" or last is None:
            continue
        v, hit = c.get("value"), False
        if c["rule"] == "exit_below":
            hit = last < v
        elif c["rule"] == "add_below":
            hit = last < v
        elif c["rule"] == "trim_above":
            hit = last > v
        elif c["rule"] == "trailing_stop" and peak:
            hit = last <= peak * (1 - v / 100)
        note = f"closed ₹{last:,.2f}" + (f" · peak ₹{peak:,.2f} since you started" if c["rule"] == "trailing_stop" and peak else "")
        rules.append({**c, "triggered": bool(hit), "note": note})
    worst = max((c["status"] for c in out_checks), key=lambda s: ORDER[s], default="unknown")
    overall = worst if out_checks else "unknown"
    return {"overall": overall, "checks": out_checks, "rules": rules, "price": last, "price_date": last_d}


# ------------------------------------------------------------------ storing
def save(con: duckdb.DuckDBPyConnection, symbol: str, name: str, text: str, checks: list[dict]) -> dict:
    now = datetime.now()
    con.execute("DELETE FROM theses WHERE symbol = ?", [symbol])
    con.execute("INSERT INTO theses (symbol, name, created_at, text, checks_json, last_json, active) "
                "VALUES (?, ?, ?, ?, ?, NULL, true)", [symbol, name, now, text, json.dumps(checks)])
    return {"symbol": symbol, "name": name, "created_at": now, "text": text, "checks": checks}


def load(con: duckdb.DuckDBPyConnection, symbol: str | None = None) -> list[dict]:
    q = "SELECT symbol, name, created_at, text, checks_json, last_json, last_checked FROM theses WHERE active"
    rows = con.execute(q + (" AND symbol = ?" if symbol else "") + " ORDER BY symbol",
                       [symbol] if symbol else []).fetchall()
    return [{"symbol": s, "name": n, "created_at": c, "text": t, "checks": json.loads(j or "[]"),
             "last": json.loads(lj) if lj else None, "last_checked": lc} for s, n, c, t, j, lj, lc in rows]


def remove(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    n = con.execute("SELECT count(*) FROM theses WHERE symbol = ? AND active", [symbol]).fetchone()[0]
    con.execute("UPDATE theses SET active = false WHERE symbol = ?", [symbol])
    return bool(n)


def record(con: duckdb.DuckDBPyConnection, symbol: str, result: dict) -> None:
    slim = {"overall": result["overall"],
            "checks": {_describe(c): c["status"] for c in result["checks"]},
            "rules": {_describe(r): r["triggered"] for r in result["rules"]}}
    con.execute("UPDATE theses SET last_json = ?, last_checked = now() WHERE symbol = ? AND active",
                [json.dumps(slim), symbol])


def changes(previous: dict | None, result: dict) -> list[str]:
    """What moved since the last check, in words ([] = nothing, which is the usual evening)."""
    if not previous:
        return []
    out = []
    for c in result["checks"]:
        d, was = _describe(c), previous.get("checks", {}).get(_describe(c))
        if was and was != c["status"] and not (c["status"] == "unknown"):
            out.append(f"{ICON[was]} → {ICON[c['status']]} **{d}** — {c['note']}")
    for r in result["rules"]:
        d = _describe(r)
        if r["triggered"] and not previous.get("rules", {}).get(d):
            out.append(f"🔔 **{d}** — {r['note']}")
    return out


def _clean_checks(raw: list) -> list[dict]:
    """Keep only well-formed checks from the parser (catalogue metrics, known price rules, questions)."""
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind")
        if kind == "metric" and c.get("metric") in METRICS and c.get("op") in (">=", "<=", "not_falling", "rising"):
            if c["op"] in (">=", "<=") and not isinstance(c.get("value"), (int, float)):
                continue
            out.append({"kind": "metric", "metric": c["metric"], "op": c["op"], "value": c.get("value"),
                        "because": str(c.get("because") or "")})
        elif kind == "price" and c.get("rule") in PRICE_RULES and isinstance(c.get("value"), (int, float)) \
                and c["value"] > 0 and not math.isnan(c["value"]):
            out.append({"kind": "price", "rule": c["rule"], "value": float(c["value"]),
                        "because": str(c.get("because") or "")})
        elif kind == "judged" and c.get("question"):
            out.append({"kind": "judged", "question": str(c["question"]), "because": str(c.get("because") or "")})
    return out


def due_for_sweep(last_checked: datetime | None, *, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return last_checked is None or (now - last_checked) >= timedelta(hours=20)


# ------------------------------------------------------------------ judged checks + entry points
def make_judge(con: duckdb.DuckDBPyConnection):
    """A judge for the reasons no metric measures: the company's last ~90 days of exchange filings,
    its latest concall notes and latest quarter, numbered, for ``synthesize.thesis_judge``."""
    from equity_research.analysis import reality_check
    from equity_research.reports import synthesize

    def judge(symbol: str, questions: list[str]) -> list[dict]:
        ev: dict[str, dict] = {}
        for i, f in enumerate(reality_check._filings(symbol) or [], 1):
            when = f"{f['date']:%d-%b-%Y} " if f["date"] else ""
            ev[f"F{i}"] = {"text": when + f["text"], "url": f["url"]}
        n = 0
        row = con.execute("SELECT filed_date, quarter, tone, execution, summary_md FROM concall_signals "
                          "WHERE symbol = ? ORDER BY filed_date DESC LIMIT 1", [symbol]).fetchone()
        if row:
            n += 1
            ev[f"C{n}"] = {"text": f"Earnings call {row[1] or ''} ({row[0]:%d-%b-%Y}): management tone {row[2]}, "
                                   f"execution {row[3]}. Notes: {' '.join(str(row[4] or '').split())}", "url": ""}
        q = reality_check._quarter_fact(con, symbol)
        if q:
            n += 1
            ev[f"C{n}"] = {"text": q, "url": ""}
        res = synthesize.thesis_judge(questions, {k: v["text"] for k, v in ev.items()})
        return [{**r, "evidence_url": ev.get(r["evidence"], {}).get("url", "")} for r in res]
    return judge


def create(con: duckdb.DuckDBPyConnection, symbol: str, name: str, text: str) -> tuple[dict | None, str]:
    """Parse and store a thesis. Returns (thesis, note) — thesis None when nothing usable came out."""
    from equity_research.reports import synthesize

    parsed = synthesize.thesis_parse(name, text)
    checks = _clean_checks((parsed or {}).get("checks"))
    if not checks:
        return None, (parsed or {}).get("unclear") or "couldn't turn that into checks"
    return save(con, symbol, name, text, checks), (parsed or {}).get("unclear", "")


def check(con: duckdb.DuckDBPyConnection, thesis: dict, *, with_judge: bool = True) -> dict:
    """Refresh the company's data and evaluate its thesis now."""
    from equity_research.reports.pipeline import ensure_ingested

    try:
        ensure_ingested(thesis["symbol"], con)
    except Exception:  # noqa: BLE001 — evaluate on whatever is on file
        log.info("thesis guard: couldn't refresh %s", thesis["symbol"])
    return evaluate(con, thesis, judge=make_judge(con) if with_judge else None)
