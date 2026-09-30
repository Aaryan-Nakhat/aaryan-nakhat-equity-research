"""🛡️ Thesis Guard + Exit Plan — command parsing, check validation, statuses, price rules and change
detection. No network, no LLM (judged checks are injected)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from equity_research.analysis import thesis_guard as tg
from equity_research.common import db


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


# ------------------------------------------------------------------ the command
@pytest.mark.parametrize("subject, want", [
    ("thesis: BEL — orders growing; exit below 250", ("set", "BEL", "orders growing; exit below 250")),
    ("thesis: Bharat Electronics - debt free", ("set", "Bharat Electronics", "debt free")),
    ("thesis: HAL because orders", ("set", "HAL", "orders")),
    ("thesis: BEL", ("show", "BEL", "")),
    ("theses", ("list", "", "")),
    ("Thesis", ("list", "", "")),
    ("unthesis: BEL", ("remove", "BEL", "")),
    ("reliance", None),
])
def test_thesis_query(subject, want):
    from equity_research.bot import app

    assert app._thesis_query(subject) == want


# ------------------------------------------------------------------ what the parser may produce
def test_clean_checks_keeps_only_the_catalogue():
    raw = [{"kind": "metric", "metric": "roe", "op": ">=", "value": 15, "because": "high ROE"},
           {"kind": "metric", "metric": "vibes", "op": ">=", "value": 1},              # not in the catalogue
           {"kind": "metric", "metric": "debt_to_equity", "op": "<=", "value": "low"},  # no number
           {"kind": "price", "rule": "exit_below", "value": 250},
           {"kind": "price", "rule": "moon", "value": 1},
           {"kind": "judged", "question": "Is the order book growing?"}, "junk"]
    got = tg._clean_checks(raw)
    assert [c["kind"] for c in got] == ["metric", "price", "judged"]


# ------------------------------------------------------------------ statuses
@pytest.mark.parametrize("op, value, now, prev, want", [
    (">=", 15, 25.0, None, "intact"),
    (">=", 15, 15.5, None, "weakening"),       # within 10% of your line
    (">=", 15, 12.0, None, "broken"),
    ("<=", 0.5, 0.1, None, "intact"),
    ("<=", 0.5, 0.48, None, "weakening"),
    ("<=", 0.5, 0.9, None, "broken"),
    ("not_falling", None, 51.0, 51.1, "intact"),
    ("not_falling", None, 50.5, 51.1, "weakening"),
    ("not_falling", None, 49.0, 51.1, "broken"),
    ("rising", None, 9.9, 8.5, "intact"),
    ("rising", None, 8.0, 8.5, "broken"),
    (">=", 15, None, None, "unknown"),
])
def test_metric_status(op, value, now, prev, want):
    assert tg._metric_status({"op": op, "value": value}, now, prev) == want


# ------------------------------------------------------------------ end to end on a small store
def _prices(con, sym, closes, start=date(2026, 9, 1)):
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
                    "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                    "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1000, 1, 1, 500, 50)", [d, sym, c, c, c, c, c, c, c])


def _holders(con, sym, rows):
    for as_of, pct, cat, prom in rows:
        con.execute("INSERT INTO shp_holders (symbol, as_of, holder_name, pct, category, is_promoter) "
                    "VALUES (?, ?, ?, ?, ?, ?)", [sym, as_of, f"{cat}-{as_of}", pct, cat, prom])


def test_evaluate_rules_and_holdings(con):
    _prices(con, "ACME", [300, 320, 340, 290])                 # peak 340 → a 15% trail breaks at 289
    _holders(con, "ACME", [(date(2026, 3, 31), 60.0, "promoter", True), (date(2026, 6, 30), 58.5, "promoter", True),
                           (date(2026, 3, 31), 5.0, "mutual fund", False), (date(2026, 6, 30), 6.0, "mutual fund", False)])
    checks = tg._clean_checks([
        {"kind": "metric", "metric": "promoter_holding", "op": "not_falling", "because": "promoters holding"},
        {"kind": "metric", "metric": "mf_holding", "op": "rising", "because": "MFs adding"},
        {"kind": "judged", "question": "Are orders growing?"},
        {"kind": "price", "rule": "exit_below", "value": 250},
        {"kind": "price", "rule": "trim_above", "value": 280},
        {"kind": "price", "rule": "trailing_stop", "value": 10}])
    t = tg.save(con, "ACME", "Acme", "why", checks)
    t["created_at"] = datetime(2026, 9, 1)
    judge = lambda sym, qs: [{"status": "weakening", "note": "orders flat", "evidence_url": "http://f"}]  # noqa: E731
    r = tg.evaluate(con, t, judge=judge)
    st = {tg._describe(c): c["status"] for c in r["checks"]}
    assert st["Promoter holding not falling"] == "broken"      # 60 → 58.5 = −1.5 pp
    assert st["Mutual-fund holding (named >1% holders) rising"] == "intact"
    assert st["Are orders growing?"] == "weakening"
    assert r["overall"] == "broken"
    trig = {r_["rule"]: r_["triggered"] for r_ in r["rules"]}
    assert trig == {"exit_below": False, "trim_above": True, "trailing_stop": True}   # 290 ≤ 340 × 0.9


def test_changes_only_report_what_moved(con):
    _prices(con, "ACME", [300, 300])
    t = tg.save(con, "ACME", "Acme", "why", tg._clean_checks([{"kind": "price", "rule": "exit_below", "value": 250}]))
    r1 = tg.evaluate(con, t)
    tg.record(con, "ACME", r1)
    prev = tg.load(con, "ACME")[0]["last"]
    assert tg.changes(prev, r1) == []                           # nothing moved → no email
    _prices(con, "ACME", [240], start=date(2026, 9, 3))
    r2 = tg.evaluate(con, t)
    ch = tg.changes(prev, r2)
    assert len(ch) == 1 and "Exit if it closes below" in ch[0]


def test_remove_and_list(con):
    tg.save(con, "ACME", "Acme", "why", [{"kind": "judged", "question": "q", "because": ""}])
    assert [t["symbol"] for t in tg.load(con)] == ["ACME"]
    assert tg.remove(con, "ACME") and tg.load(con) == []
