"""Track record (analysis/track_record.py + reports/scorecard_brief.py): calls are logged once, scored
from the next open against the Nifty 500, Avoids score when the stock lags, and the switches work."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from equity_research import config
from equity_research.analysis import track_record as tr
from equity_research.common import db
from equity_research.reports import scorecard_brief

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


def _sessions(n: int, start=date(2026, 6, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _market(con, days, stock_path: dict[str, list[float]], bench_path: list[float]):
    """Daily bars: each stock and the Nifty 500 open at the previous close and close on the path."""
    for i, d in enumerate(days):
        b_prev = bench_path[i - 1] if i else bench_path[0]
        con.execute("INSERT INTO index_close (trade_date, index_name, open, high, low, close) "
                    "VALUES (?, 'Nifty 500', ?, ?, ?, ?)", [d, b_prev, bench_path[i], b_prev, bench_path[i]])
        for sym, path in stock_path.items():
            prev = path[i - 1] if i else path[0]
            con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, "
                        "last, close, avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, "
                        "deliv_per) VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1000, 1, 1, 500, 50)",
                        [d, sym, prev, prev, max(prev, path[i]), min(prev, path[i]), path[i], path[i], path[i]])


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# ------------------------------------------------------------------ verdicts
@pytest.mark.parametrize("text, label, stance", [
    ("### 7. Verdict 🎯\n**Verdict: Accumulate** — steady compounder", "ACCUMULATE", "long"),
    ("## Verdict\n\n**AVOID.** Leverage is rising.", "AVOID", "avoid"),
    ("### Verdict (the closing section)\nWe would **Reduce** into strength.", "REDUCE", "avoid"),
    ("### 7. Verdict\nHold — fairly valued; wait for a better entry.", "HOLD", "hold"),
    ("### 7. Verdict\nIt's complicated.", "REVIEW", "review"),
    ("no verdict heading at all", "REVIEW", "review"),
])
def test_parse_verdict(text, label, stance):
    assert tr.parse_verdict(text) == (label, stance)


# ------------------------------------------------------------------ logging
def test_one_call_per_engine_name_day(con):
    d = date(2026, 6, 3)
    assert tr.log_call(con, "deep_report", "acme", "long", "BUY", made_at=_at(d, 11))
    assert not tr.log_call(con, "deep_report", "ACME", "avoid", "AVOID", made_at=_at(d, 16))   # same day
    assert tr.log_call(con, "deep_report", "ACME", "avoid", "AVOID", made_at=_at(d + timedelta(days=1), 11))
    assert tr.log_basket(con, "hotlist", ["ACME", "BETA", ""], made_at=_at(d, 11)) == 2
    assert con.execute("SELECT rank FROM calls WHERE source='hotlist' AND symbol='BETA'").fetchone()[0] == 2


# ------------------------------------------------------------------ scoring
def test_entry_is_next_open_and_scores_vs_benchmark(con, monkeypatch):
    days = _sessions(30)
    stock = [100.0] * 2 + [110.0] * 28          # jumps on day 3 (index 2), after the call
    bench = [1000.0] * 2 + [1050.0] * 28
    _market(con, days, {"ACME": stock, "DUD": [100.0] * 30}, bench)
    # called on day 1 after the close → entry = day 2's open (100); a 1-week exit at day 6's close
    tr.log_call(con, "deep_report", "ACME", "long", "BUY", made_at=_at(days[0], 16))
    tr.log_call(con, "deep_report", "DUD", "avoid", "AVOID", made_at=_at(days[0], 16))
    oc = tr.outcomes(con)
    a = oc[(oc.symbol == "ACME") & (oc.horizon == "1w")].iloc[0]
    assert a.entry_date == days[1] and a.entry == 100.0 and a.exit_date == days[5]
    assert a.ret == pytest.approx(10.0) and a.bench == pytest.approx(5.0) and a.excess == pytest.approx(5.0)
    assert a.hit
    d = oc[(oc.symbol == "DUD") & (oc.horizon == "1w")].iloc[0]
    assert d.excess == pytest.approx(-5.0) and d.hit        # an Avoid that lagged the market is a hit
    assert set(oc[oc.symbol == "ACME"].horizon) == {"1w", "1m", "to_date"}   # 3m hasn't traded yet


def test_a_call_before_the_open_uses_that_days_open(con):
    days = _sessions(10)
    _market(con, days, {"ACME": [100.0 + i for i in range(10)]}, [1000.0] * 10)
    tr.log_call(con, "hotlist", "ACME", "long", "PICK", made_at=_at(days[3], 8, 30))
    oc = tr.outcomes(con)
    assert oc.iloc[0].entry_date == days[3]


def test_hold_is_scored_against_a_band(con):
    days = _sessions(10)
    _market(con, days, {"FLAT": [100.0] * 5 + [101.0] * 5, "RUN": [100.0] * 5 + [120.0] * 5},
            [1000.0] * 10)
    for s in ("FLAT", "RUN"):
        tr.log_call(con, "deep_report", s, "hold", "HOLD", made_at=_at(days[0], 16))
    oc = tr.outcomes(con).set_index(["symbol", "horizon"])
    assert oc.loc[("FLAT", "1w"), "hit"] and not oc.loc[("RUN", "1w"), "hit"]    # ±2% band at 1 week


def test_scorecard_marks_small_samples(con, monkeypatch):
    monkeypatch.setattr(tr, "MIN_SAMPLE", 3)
    days = _sessions(25)
    _market(con, days, {s: [100.0] * 3 + [110.0] * 22 for s in "ABCD"}, [1000.0] * 25)
    tr.log_basket(con, "tailwind", ["A", "B"], made_at=_at(days[0], 16))
    tr.log_basket(con, "hotlist", ["A", "B", "C", "D"], made_at=_at(days[0], 16))
    rows = {(r["source"], r["horizon"]): r for r in tr.scorecard(con)["rows"]}
    assert not rows[("tailwind", "1w")]["enough"] and rows[("hotlist", "1w")]["enough"]
    assert rows[("hotlist", "1w")]["hit_rate"] == 100.0 and rows[("hotlist", "1w")]["baskets"] == 1
    md = scorecard_brief.build_scorecard(con)
    assert "too few (n<3)" in md and "Not investment advice" in md


def test_memory_line_is_for_the_reader_and_can_be_switched_off(con, monkeypatch):
    days = _sessions(8)
    _market(con, days, {"ACME": [100.0] * 4 + [90.0] * 4}, [1000.0] * 8)
    tr.log_call(con, "deep_report", "ACME", "avoid", "AVOID", made_at=_at(days[0], 16))
    line = tr.memory_line(con, "ACME")
    assert "Last time" in line and "Avoid" in line and "written without it" in line
    monkeypatch.setattr(config, "REPORT_MEMORY_ENABLED", False)
    assert tr.memory_line(con, "ACME") is None


def test_track_record_switch(con, monkeypatch):
    monkeypatch.setattr(config, "TRACK_RECORD_ENABLED", False)
    assert "switched off" in scorecard_brief.build_scorecard(con)


def test_only_idea_menus_are_logged(monkeypatch, tmp_path):
    """The bot's list hook logs engine picks, never name-matching / holdings / fund menus."""
    from equity_research.bot import app

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "bot.duckdb")
    monkeypatch.setattr(app, "connect", lambda: db.connect(tmp_path / "bot.duckdb"))
    c = db.connect(tmp_path / "bot.duckdb")
    try:
        app._track_basket(c, "hotlist", ["ACME", "BETA"])
        app._track_basket(c, "hdfc", ["HDFCBANK", "HDFCLIFE"])        # a "which HDFC?" menu
        app._track_basket(c, "sell", ["ACME"])                        # your own holdings
        got = c.execute("SELECT source, symbol FROM calls ORDER BY symbol").fetchall()
    finally:
        c.close()
    assert got == [("hotlist", "ACME"), ("hotlist", "BETA")]
