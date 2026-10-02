"""BSE's corporate-action record filling what NSE's lacks — a stock that listed on NSE late (or BSE-only):
splits / bonuses sized from BSE's wording, NSE records never duplicated, dividends, rights without a stated ratio,
and unsized events noted on the buys. Made-up companies; throwaway DuckDB; no network."""

from __future__ import annotations

from datetime import date

import pytest

from equity_research import portfolio as pf
from equity_research.analysis import bse_actions as ba
from equity_research.common import db

TODAY = date(2026, 10, 1)


@pytest.mark.parametrize("purpose, details, want", [
    ("Bonus issue 1:2", None, ("bonus", 1.5)),
    ("Stock  Split From Rs.10/- to Rs.2/-", None, ("split", 5.0)),
    ("Consolidation of Shares From Re.1/- to Rs.10/-", None, ("consolidation", 0.1)),
    ("Right Issue of Equity Shares", None, ("rights", None)),
    ("Final Dividend", "2.50", ("dividend", 2.5)),
    ("Reduction of Capital", None, ("unsized", None)),
    ("Redemption of Debentures", None, None),
    ("Annual General Meeting", None, None),
])
def test_parse(purpose, details, want):
    got = ba.parse(purpose, details)
    assert (got[:2] if got else None) == want


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    c = db.connect(tmp_path / "t.duckdb")
    c.execute("INSERT INTO equity_master (symbol, company_name, isin) VALUES ('LATECO', 'Lateco Motors', 'INE0LATE001')")
    # on NSE only since mid-2026 (it traded on BSE before)
    c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, avg_price, "
              "ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
              "VALUES (?, 'LATECO', 'EQ', 20, 20, 20, 20, 20, 20, 20, 1, 1, 1, 1, 50)", [TODAY])
    yield c
    c.close()


def test_pre_nse_bonus_and_split_apply_to_an_old_buy(con):
    ba.store(con, "LATECO", [
        {"Ex_date": "10 Jan 2022", "purpose": "Stock  Split From Rs.10/- to Rs.1/-", "Details": None},
        {"Ex_date": "05 Sep 2023", "purpose": "Bonus issue 1:1", "Details": None},
        {"Ex_date": "12 Aug 2024", "purpose": "Final Dividend", "Details": "0.50"}])
    pf.add_lot(con, "LATECO", 10, 400, "2021-06-01")                 # entered as bought, years before NSE
    v = next(s for s in pf.portfolio(con, today=TODAY)["stocks"] if s["symbol"] == "LATECO")["lots"][0]
    assert v["adj_qty"] == 200 and v["cost"] == 4000                  # ×10 split, then 1:1 bonus (₹0-cost lot)
    assert sorted(p["kind"] for p in v["parts"]) == ["bonus", "bought"]
    assert v["dividends"] == pytest.approx(200 * 0.5)


def test_nse_records_are_never_duplicated(con):
    con.execute("INSERT INTO price_adjustments VALUES ('LATECO', '2023-09-06', 0.5, 2, 'bonus', 'nse', 'Bonus 1:1')")
    assert ba.store(con, "LATECO", [{"Ex_date": "05 Sep 2023", "purpose": "Bonus issue 1:1", "Details": None}]) == 0


def test_unsized_events_and_rights_without_a_ratio_are_shown(con):
    ba.store(con, "LATECO", [{"Ex_date": "23 Nov 2015", "purpose": "Reduction of Capital ", "Details": None},
                             {"Ex_date": "04 Aug 2022", "purpose": "Right Issue of Equity Shares ", "Details": None}])
    pf.add_lot(con, "LATECO", 10, 400, "2014-06-01")
    v = next(s for s in pf.portfolio(con, today=TODAY)["stocks"] if s["symbol"] == "LATECO")["lots"][0]
    assert v["adj_qty"] == 10                                         # nothing guessed
    assert "Reduction of Capital" in v["events"][0]
    assert v["rights"][0]["entitled"] is None and v["rights"][0]["ratio"] == "ratio not stated"


def test_bse_code_by_isin(con):
    con.execute("INSERT INTO bse_codes VALUES ('INE0LATE001', '599001', 'Lateco')")
    assert ba.code_for(con, "LATECO") == "599001" and ba.code_for(con, "BSE:123456") == "123456"
