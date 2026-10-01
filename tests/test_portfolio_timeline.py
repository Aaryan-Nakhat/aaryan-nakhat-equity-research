"""The portfolio timeline — rights not assumed, bonus shares as ₹0-cost lots dated on allotment, 31-Jan-2018
grandfathering, FIFO sells and realised gains by financial year, buyback tax by date, dividends, XIRR, and the
instruments beyond NSE main-board shares (ETFs, BSE-only). Made-up companies; throwaway DuckDB; no network."""

from __future__ import annotations

from datetime import date

import pytest

from equity_research import portfolio as pf
from equity_research.common import db
from equity_research.portfolio import income, instruments, tax

TODAY = date(2026, 10, 1)


def _price(c, sym, d, px, series="EQ"):
    c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, avg_price, "
              "ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 1, 50)", [d, sym, series] + [px] * 7)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    c = db.connect(tmp_path / "t.duckdb")
    c.execute("INSERT INTO equity_master (symbol, company_name, isin) VALUES ('ALPHA', 'Alpha Industries', 'INE0ALPHA01'),"
              " ('BETA', 'Beta Chemicals', 'INE0BETA001')")
    _price(c, "ALPHA", TODAY, 100.0)
    _price(c, "BETA", TODAY, 50.0)
    yield c
    c.close()


def _stock(p, sym):
    return next(s for s in p["stocks"] if s["symbol"] == sym)


# ------------------------------------------------------------------ 1. rights issues aren't assumed
def test_a_rights_issue_doesnt_change_your_shares_but_is_offered(con):
    con.execute("INSERT INTO financials (symbol, period_end, consolidated, period_type, element, value) VALUES "
                "('ALPHA', '2024-03-31', false, 'Y', 'FaceValueOfEquityShareCapital', 10)")
    con.execute("INSERT INTO price_adjustments VALUES ('ALPHA', '2025-02-10', 0.95, 1.2, 'rights', 'nse', "
                "'Rights 1:5 @ Premium Rs 70/-')")
    pf.add_lot(con, "ALPHA", 100, 90, "2024-06-01")
    v = _stock(pf.portfolio(con, today=TODAY), "ALPHA")["lots"][0]
    assert v["adj_qty"] == 100 and v["cost"] == 9000                  # not 120 shares
    offer = v["rights"][0]
    assert offer["entitled"] == 20 and offer["price"] == 80 and offer["ratio"] == "1 for every 5"


# ------------------------------------------------------------------ 2. bonus shares: ₹0 cost, own date
def test_bonus_shares_are_a_zero_cost_lot_dated_on_allotment(con):
    con.execute("INSERT INTO price_adjustments VALUES ('ALPHA', '2026-03-02', 0.5, 2, 'bonus', 'nse', 'Bonus 1:1')")
    pf.add_lot(con, "ALPHA", 100, 150, "2024-06-01")                  # cost 15,000
    v = _stock(pf.portfolio(con, today=TODAY), "ALPHA")["lots"][0]
    assert v["adj_qty"] == 200 and v["cost"] == 15000 and v["adj_price"] == 75   # the broker's average
    bought, bonus = sorted(v["parts"], key=lambda p: p["kind"] != "bought")
    assert (bought["shares"], bought["cost_ps"], bought["term"]) == (100, 150, "long")
    assert (bonus["shares"], bonus["cost_ps"], bonus["acquired"], bonus["term"]) == (100, 0, "2026-03-02", "short")


def test_a_split_keeps_cost_and_date(con):
    con.execute("INSERT INTO price_adjustments VALUES ('ALPHA', '2025-09-01', 0.2, 5, 'split', 'nse', 'FV 10 to 2')")
    pf.add_lot(con, "ALPHA", 100, 500, "2024-06-01")
    v = _stock(pf.portfolio(con, today=TODAY), "ALPHA")["lots"][0]
    assert [(p["shares"], p["cost_ps"], p["acquired"]) for p in v["parts"]] == [(500, 100, "2024-06-01")]


# ------------------------------------------------------------------ 3. grandfathering
def test_shares_held_since_jan_2018_are_grandfathered(con):
    con.execute("INSERT INTO fmv_2018 VALUES ('ALPHA', 'INE0ALPHA01', 80)")
    con.execute("INSERT INTO price_adjustments VALUES ('ALPHA', '2021-05-03', 0.5, 2, 'split', 'nse', 'FV 10 to 5')")
    pf.add_lot(con, "ALPHA", 100, 50, "2016-04-01")                   # → 200 @ 25; FMV 80 → 40 per share now
    pf.add_sell(con, "ALPHA", 200, 100, "2026-06-15")
    p = pf.portfolio(con, today=TODAY)
    row = p["realised"][0]["rows"][0]
    assert row["grandfathered"] and row["cost"] == pytest.approx(200 * 40)     # max(25, min(40, 100)) = 40
    assert row["gain"] == pytest.approx(200 * 60) and row["term"] == "long"


def test_grandfathering_rule():
    assert tax.tax_cost_ps(25, 100, date(2016, 1, 1), 40) == 40
    assert tax.tax_cost_ps(25, 30, date(2016, 1, 1), 40) == 30        # capped at the sale price
    assert tax.tax_cost_ps(60, 100, date(2016, 1, 1), 40) == 60       # actual cost higher
    assert tax.tax_cost_ps(25, 100, date(2019, 1, 1), 40) == 25       # bought after 31-Jan-2018


# ------------------------------------------------------------------ 7. sells, FIFO, realised by year
def test_sells_are_fifo_and_reduce_holdings(con):
    pf.add_lot(con, "ALPHA", 10, 60, "2024-01-10")
    pf.add_lot(con, "ALPHA", 10, 90, "2026-02-01")
    pf.add_sell(con, "ALPHA", 15, 110, "2026-08-01")
    p = pf.portfolio(con, today=TODAY)
    s = _stock(p, "ALPHA")
    assert s["qty"] == 5 and s["cost"] == pytest.approx(450)          # the newer buy's 5 left
    year = p["realised"][0]
    assert year["fy"] == "Tax year 2026-27"
    terms = [(r["shares"], r["term"], r["gain"]) for r in year["rows"]]
    assert terms == [(10, "long", 500), (5, "short", 100)]
    assert year["tax"]["lt_gain"] == 500 and year["tax"]["st_gain"] == 100


def test_selling_more_than_dated_buys_is_flagged(con):
    pf.add_lot(con, "ALPHA", 10, 60, "2024-01-10")
    pf.add_sell(con, "ALPHA", 15, 110, "2026-08-01")
    p = pf.portfolio(con, today=TODAY)
    assert p["warns"] and p["realised"][0]["unmatched"] == 1


def test_this_years_gains_feed_the_raise_plan(con):
    pf.add_lot(con, "ALPHA", 10, 60, "2024-01-10")
    pf.add_sell(con, "ALPHA", 5, 110, "2026-08-01")
    assert pf.realised_this_year(pf.portfolio(con, today=TODAY), TODAY) == [{"gain": 250, "term": "long"}]


@pytest.mark.parametrize("sold, treatment", [(date(2024, 6, 1), "exempt"), (date(2025, 6, 1), "dividend"),
                                             (date(2026, 6, 1), "capital_gains")])
def test_buyback_tax_depends_on_the_date(sold, treatment):
    assert tax.buyback_treatment(sold) == treatment


def test_a_buyback_in_the_deemed_dividend_window(con):
    pf.add_lot(con, "ALPHA", 10, 60, "2023-01-10")
    pf.add_sell(con, "ALPHA", 4, 120, "2025-06-01", kind="buyback")
    row = pf.portfolio(con, today=TODAY)["realised"][0]["rows"][0]
    assert row["deemed_dividend"] == 480 and row["gain"] == -240       # cost becomes a capital loss


def test_fiscal_year_labels():
    assert tax.fy_label(date(2026, 3, 31)) == "FY 2025-26"
    assert tax.fy_label(date(2026, 4, 1)) == "Tax year 2026-27"
    assert tax.ltcg_section(date(2026, 5, 1)).startswith("s.198")


# ------------------------------------------------------------------ 9. dividends and XIRR
def test_dividends_follow_the_shares_held_on_each_ex_date(con):
    pf.add_lot(con, "ALPHA", 10, 60, "2025-01-10")
    pf.add_lot(con, "ALPHA", 10, 80, "2025-09-01")
    income.store_actions(con, "ALPHA", [
        {"subject": "Final Dividend - Rs 3 Per Share", "exDate": "15-Jul-2025"},          # 10 shares then
        {"subject": "Interim Dividend - Rs 2 Per Share + Special Dividend - Rs 1 Per Share", "exDate": "10-Feb-2026"},
        {"subject": "Annual General Meeting", "exDate": "01-Aug-2025"}])
    s = _stock(pf.portfolio(con, today=TODAY), "ALPHA")
    assert s["dividends"] == pytest.approx(10 * 3 + 20 * 3)


def test_dividend_amounts_parse():
    assert income.parse_amount("Interim Dividend - Rs 2.50 Per Share + Special Dividend - Rs 5 Per Share") == 7.5
    assert income.parse_amount("Distribution - Rs 4.10 Per Unit") == 4.1
    assert income.parse_amount("Agm/Dividend - 40%") is None


def test_xirr():
    r = income.xirr([(date(2024, 1, 1), -1000), (date(2025, 1, 1), 1100)])
    assert r == pytest.approx(0.1, abs=1e-3)
    assert income.xirr([(date(2026, 9, 1), -1000), (date(2026, 9, 10), 1100)]) is None   # under a month


# ------------------------------------------------------------------ 5 / 6. ETFs, BSE-only shares
def test_etfs_and_bse_only_shares_can_be_found_added_and_priced(con):
    con.execute("INSERT INTO instruments VALUES ('GOLDETF', 'GOLDETF — Gold ETF', NULL, 'etf', now()), "
                "('BSE:999001', 'Gamma Textiles Limited', 'INE0GAMMA01', 'bse', now())")
    _price(con, "GOLDETF", TODAY, 70.0)
    con.execute("INSERT INTO bse_prices VALUES ('BSE:999001', ?, 12.5)", [TODAY])
    assert instruments.search(con, "gold")[0]["note"] == "ETF"
    assert pf.search(con, "gamma textiles")[0]["symbol"] == "BSE:999001"
    pf.add_lot(con, "gold etf", 10, 60, "2025-01-01")
    pf.add_lot(con, "gamma textiles", 100, 10)
    p = pf.portfolio(con, today=TODAY)
    assert _stock(p, "GOLDETF")["value"] == 700 and _stock(p, "BSE:999001")["value"] == 1250


# ------------------------------------------------------------------ csv sells
def test_csv_buys_and_sells(con, tmp_path, monkeypatch):
    f = tmp_path / "holdings.csv"
    f.write_text("symbol,qty,price,date,side\nALPHA,10,60,2024-01-10,buy\nALPHA,4,110,2026-08-01,sell\n",
                 encoding="utf-8")
    monkeypatch.setenv("HOLDINGS_CSV", str(f))
    assert pf.sync_csv(con)["imported"] == 2
    assert _stock(pf.portfolio(con, today=TODAY), "ALPHA")["qty"] == 6


def test_no_benchmark_when_index_history_starts_later(con):
    con.execute("INSERT INTO index_close (trade_date, index_name, close) VALUES ('2020-03-31', 'Nifty 500', 100), "
                "(?, 'Nifty 500', 300)", [TODAY])
    old = pf.value_lot(con, pf.add_lot(con, "ALPHA", 1, 10, "2016-05-02"), today=TODAY)
    new = pf.value_lot(con, pf.add_lot(con, "ALPHA", 1, 10, "2020-03-27"), today=TODAY)
    assert "bench_pct" not in old and new["bench_pct"] == pytest.approx(200)
