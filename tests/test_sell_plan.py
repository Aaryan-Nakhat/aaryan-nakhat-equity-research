"""💰 `raise ₹X` — capital-gains estimate (set-off, exemption, cess), FIFO, the least-tax vs weakest-first
plans, wait-for-long-term tips and the command parser. Throwaway DuckDB; no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from equity_research import holdings as h
from equity_research.analysis import sell_advisor as sa
from equity_research.common import db

TODAY = date.today()
CESS = 1.04


def _tax(**kw):
    sales = [{"gain": g, "term": t} for t, gs in kw.items() for g in gs]
    return sa.tax_estimate(sales)


@pytest.mark.parametrize("kw, want", [
    ({"short": [10000]}, 0.20 * 10000 * CESS),
    ({"long": [100000]}, 0.0),                                   # inside the ₹1.25 lakh exemption
    ({"long": [200000]}, 0.125 * 75000 * CESS),
    ({"short": [10000, -4000]}, 0.20 * 6000 * CESS),             # short loss offsets short gain
    ({"short": [-50000], "long": [200000]}, 0.125 * 25000 * CESS),   # leftover short loss → long gain
    ({"long": [-50000], "short": [10000]}, 0.20 * 10000 * CESS),     # a long loss can't touch short gains
])
def test_tax_estimate(kw, want):
    assert _tax(**kw)["tax"] == pytest.approx(want)


def test_unknown_term_is_reported_not_taxed():
    t = _tax(unknown=[5000], short=[1000])
    assert t["unknown_gain"] == 5000 and t["tax"] == pytest.approx(0.20 * 1000 * CESS)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    c = db.connect(tmp_path / "t.duckdb")
    c.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('WIN', 'Winner'), ('LOSS', 'Loser'), "
              "('NEW', 'Newbie')")
    for sym, px in (("WIN", 200.0), ("LOSS", 50.0), ("NEW", 120.0)):
        c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
                  "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                  "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1000, 1, 1, 500, 50)", [TODAY, sym] + [px] * 7)
    yield c
    c.close()


def test_fifo_oldest_shares_go_first(con):
    h.add_lot(con, "WIN", 10, 150, TODAY - timedelta(days=100))      # newer, short-term
    h.add_lot(con, "WIN", 10, 100, TODAY - timedelta(days=800))      # older, long-term → sold first
    res = sa.raise_plan(con, 1000, [])
    part = res["plans"]["merit"]["rows"][0]["parts"][0]
    assert part["term"] == "long" and part["shares"] == 5 and part["gain"] == 500


def test_least_tax_vs_weakest_first(con):
    h.add_lot(con, "WIN", 100, 100, TODAY - timedelta(days=100))     # big short-term gain (strong stock)
    h.add_lot(con, "LOSS", 100, 80, TODAY - timedelta(days=100))     # short-term loss
    ranking = [{"symbol": "WIN", "keep_score": 20.0}, {"symbol": "LOSS", "keep_score": 80.0}]
    res = sa.raise_plan(con, 4000, ranking)
    tax_p, merit_p = res["plans"]["tax"], res["plans"]["merit"]
    assert [r["symbol"] for r in tax_p["rows"]] == ["LOSS"] and tax_p["tax"]["tax"] == 0
    assert [r["symbol"] for r in merit_p["rows"]] == ["WIN"]          # lowest keep score first
    assert merit_p["rows"][0]["shares"] == 20 and merit_p["tax"]["tax"] == pytest.approx(0.2 * 2000 * CESS)
    assert not res["same"]


def test_more_than_you_hold_and_holdings_without_quantity(con):
    h.add_lot(con, "LOSS", 10, 40)
    con.execute("INSERT INTO watchlist (symbol, company, list_type) VALUES ('NEW', 'Newbie', 'holding')")
    res = sa.raise_plan(con, 10000, [])
    p = res["plans"]["tax"]
    assert p["proceeds"] == 500 and p["short_by"] == 9500 and p["has_unknown"]
    assert res["no_qty"] == ["NEW"]


def test_wait_for_long_term_tip(con):
    h.add_lot(con, "NEW", 100, 100, TODAY - timedelta(days=340))     # long-term in 26 days
    tips = sa.raise_plan(con, 1200, [])["plans"]["tax"]["tips"]
    assert tips and tips[0]["days"] == 26
    assert tips[0]["save"] == pytest.approx(200 * (0.20 - 0.125) * CESS)


@pytest.mark.parametrize("subject, want", [
    ("raise 50000", 50000), ("sell ₹1.5 lakh", 150000), ("take out 2L", 200000), ("I wanna take out 50k", 50000),
    ("need 3 lakh", 300000), ("Re: raise: 1,20,000", 120000), ("withdraw 1cr", 1e7),
    ("sell", None), ("sell: need cash", None), ("need cash", None), ("reliance", None),
])
def test_raise_amount(subject, want):
    from equity_research.bot import app

    assert app._raise_amount(subject) == want


def test_inr():
    from equity_research.bot import app

    assert (app._inr(1234567), app._inr(-905.4), app._inr(999)) == ("₹12,34,567", "−₹905", "₹999")
