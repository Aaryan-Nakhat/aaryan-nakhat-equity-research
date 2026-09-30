"""📋 Corporate-Action Desk — classifying NSE's record, what's decided vs automatic, rights maths, closed
windows, the push's dedup, the LLM extractor's citation rule and the command. No network, no LLM."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from equity_research.analysis import ca_desk
from equity_research.common import db
from equity_research.reports import ca_brief

TODAY = date.today()


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
              "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
              "VALUES (?, 'ACME', 'EQ', 100, 100, 100, 100, 100, 100, 100, 1000, 1, 1, 500, 50)", [TODAY])
    yield c
    c.close()


def _row(subject, days, rec=None):
    ex = TODAY + timedelta(days=days)
    return {"subject": subject, "exDate": f"{ex:%d-%b-%Y}", "recDate": f"{rec or ex:%d-%b-%Y}"}


@pytest.mark.parametrize("subject, kind", [
    ("Buy Back", "buyback"), ("Rights 2:21 @ Premium Rs 748/-", "rights"), ("Demerger", "demerger"),
    ("Bonus 1:1", "bonus"), ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "split"),
    ("Interim Dividend - Rs 1.50 Per Share", "dividend"), ("Rights - 7 Ccps And 7 Warrants:40", "other"),
    ("Bonus Issue Of NCRPS", "other"), ("Annual General Meeting", "other"),
])
def test_kind(subject, kind):
    assert ca_desk._kind(subject) == kind


def test_actions_for_window_and_details(con):
    rows = [_row("Bonus 1:1", 5), _row("Dividend - Rs 3 Per Share", -10), _row("Buy Back", 2),
            _row("Bonus 1:1", 5),                               # duplicate row → once
            _row("Dividend - Rs 9 Per Share", -200),            # outside the window
            _row("Rights - 7 Ccps And 7 Warrants:40", 3)]       # not equity
    got = ca_desk.actions_for(con, "ACME", "Acme", rows=rows)
    assert [a.kind for a in got] == ["buyback", "dividend", "bonus"]   # decisions first, then by date
    bonus = got[2]
    assert bonus.details["share_multiplier"] == 2 and not bonus.needs_action
    assert got[1].details == {"per_share": 3.0, "yield_pct": 3.0}
    assert got[0].needs_action


def test_rights_maths_and_citations(con, monkeypatch):
    from equity_research.analysis import reality_check
    from equity_research.reports import synthesize

    monkeypatch.setattr(reality_check, "_filings", lambda s: [
        {"date": None, "text": "Outcome of rights issue committee — letter of offer", "url": "http://x/f1.htm"}])
    monkeypatch.setattr(synthesize, "ca_details", lambda *a, **k: {
        "close_date": f"{TODAY + timedelta(days=20):%d-%b-%Y}", "cite": {"close_date": "F1"}})
    a = ca_desk.actions_for(con, "ACME", "Acme", rows=[_row("Rights 1:4 @ Premium Rs 70/-", 3)])[0]
    ca_desk.enrich(con, a, {"ACME": 10.0})
    # issue = 10 + 70 = 80; TERP = (4×100 + 1×80) / 5 = 96
    assert a.details["issue_price"] == 80 and a.details["value_per_right"] == 20
    assert a.details["terp"] == 96 and a.details["dilution_if_ignored_pct"] == 4.0
    assert a.needs_action
    md = ca_brief.render([a])
    assert "Needs your decision" in md and "[F1](http://x/f1.htm)" in md and "₹96.00" in md


def test_a_closed_window_needs_nothing(con):
    a = ca_desk.actions_for(con, "ACME", "Acme", rows=[_row("Buy Back", -20)])[0]
    a.details.update(close_date=f"{TODAY - timedelta(days=3):%d-%b-%Y}", offer_price=150.0)
    assert a.closed_on and not a.needs_action
    assert "Nothing left to do" in ca_brief.render([a])
    b = ca_desk.actions_for(con, "ACME", "Acme", rows=[_row("Buy Back", -40)])[0]   # no close date, long past
    assert not b.needs_action


def test_fresh_emails_once_and_reminds_near_the_deadline(con):
    far = ca_desk.actions_for(con, "ACME", "Acme", rows=[_row("Buy Back", 10), _row("Bonus 1:1", 2)])
    assert len(ca_desk.fresh(con, far)) == 2
    assert ca_desk.fresh(con, far) == []                        # already sent
    near = ca_desk.actions_for(con, "ACME", "Acme", rows=[_row("Buy Back", 10)])
    assert ca_desk.fresh(con, near, today=TODAY + timedelta(days=8)) == near   # 2 days out → one reminder
    assert ca_desk.fresh(con, near, today=TODAY + timedelta(days=9)) == []


def test_extractor_drops_uncited_values(monkeypatch):
    from equity_research.common import llm
    from equity_research.reports import synthesize

    monkeypatch.setattr(llm, "generate", lambda *a, **k: json.dumps({
        "values": {"offer_price": "1,450", "size_cr": 300, "open_date": "10-Sep-2026"},
        "cite": {"offer_price": "F1", "size_cr": "F9"}, "note": ""}))
    got = synthesize.ca_details("buyback", "Acme", "Buy Back", {"F1": "public announcement"})
    assert got == {"offer_price": 1450.0, "cite": {"offer_price": "F1"}}   # F9 doesn't exist; open_date uncited


@pytest.mark.parametrize("subject, want", [
    ("actions", ("all", "")), ("Corporate Actions", ("all", "")), ("Re: actions: BEL", ("one", "BEL")),
    ("corporate action: Natco Pharma", ("one", "Natco Pharma")), ("reactions", None), ("actions list", None),
])
def test_ca_query(subject, want):
    from equity_research.bot import app

    assert app._ca_query(subject) == want


def test_holdings_are_owned_stocks_and_theses(con):
    con.execute("INSERT INTO watchlist (symbol, company, list_type) VALUES ('ACME', 'Acme', 'holding'), "
                "('WATCH', 'Watch', 'tracking')")
    con.execute("INSERT INTO theses (symbol, name, active) VALUES ('BEL', 'Bharat Electronics', true)")
    assert [s for s, _ in ca_desk.holdings(con)] == ["ACME", "BEL"]
