"""The numbers the README promises are computed, checked against hand-worked values: Altman Z,
Piotroski F, Beneish M, Sloan accruals, the valuation snapshot / history, and smart-money cost zones.

Every fixture is a tiny synthetic company in a throw-away database; amounts are rupees, scaled by
``CR`` so they look like real filings (₹ crore)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from equity_research.analysis import forensic, ownership, valuation
from equity_research.analysis import corporate_actions as ca
from equity_research.common import db

CR = 1e7
FY25, FY26 = date(2025, 3, 31), date(2026, 3, 31)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


def _annual(con, symbol, period_end, **elements):
    """One fiscal year of annual financials (values in ₹ crore unless the element is a per-share
    or face-value figure)."""
    per_share = {"FaceValueOfEquityShareCapital"}
    con.executemany(
        "INSERT INTO financials (symbol, period_end, period_start, period_type, consolidated, element, "
        "value) VALUES (?, ?, ?, 'Y', false, ?, ?)",
        [(symbol, period_end, date(period_end.year - 1, 4, 1), k, v if k in per_share else v * CR)
         for k, v in elements.items()])


def _close(con, symbol, d, price):
    con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, "
                "last, close, avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1000, 1, 1, 500, 50)",
                [d, symbol, price, price, price, price, price, price, price])


# ------------------------------------------------------------------ Altman Z
def test_altman_z_book_and_market_variants(con):
    _annual(con, "ACME", FY26, CurrentAssets=500, CurrentLiabilities=300, OtherEquity=200,
            ProfitBeforeTax=80, FinanceCosts=20, RevenueFromOperations=1000, Assets=1000,
            Liabilities=400, Equity=600)
    # X1 .2 · X2 .2 · X3 .1 · X4 1.5 (book) · X5 1.0 → 0.24 + 0.28 + 0.33 + 0.9 + 1.0
    z = forensic.altman_z(con, "ACME")
    assert z.value == pytest.approx(2.75) and "book equity" in z.note
    # with a ₹2,000 cr market cap X4 = 5 → 0.24 + 0.28 + 0.33 + 3.0 + 1.0
    assert forensic.altman_z(con, "ACME", market_cap=2000 * CR).value == pytest.approx(4.85)


def test_altman_z_refuses_to_guess_a_missing_input(con):
    _annual(con, "ACME", FY26, CurrentAssets=500, CurrentLiabilities=300, OtherEquity=200,
            ProfitBeforeTax=80, RevenueFromOperations=1000, Assets=1000, Liabilities=400, Equity=600)
    z = forensic.altman_z(con, "ACME")                          # no FinanceCosts filed
    assert z.value is None and "FinanceCosts" in z.missing


# ------------------------------------------------------------------ Piotroski F
_PRIOR = dict(ProfitLossForPeriod=50, Assets=1000, CashFlowsFromUsedInOperatingActivities=40,
              CurrentAssets=400, CurrentLiabilities=200, BorrowingsNoncurrent=300,
              EquityShareCapital=100, RevenueFromOperations=800, CostOfMaterialsConsumed=500)


def test_piotroski_all_nine_signals(con):
    _annual(con, "ACME", FY25, **_PRIOR)
    _annual(con, "ACME", FY26, ProfitLossForPeriod=100, Assets=1100,
            CashFlowsFromUsedInOperatingActivities=150, CurrentAssets=500, CurrentLiabilities=200,
            BorrowingsNoncurrent=250, EquityShareCapital=100, RevenueFromOperations=1000,
            CostOfMaterialsConsumed=550)
    f = forensic.piotroski_f(con, "ACME")
    assert f.value == 9 and all(v == 1.0 for v in f.components.values())


def test_piotroski_counts_only_the_signals_that_pass(con):
    _annual(con, "ACME", FY25, **_PRIOR)
    # loss-making, cash burn, more debt, a share issue, margins down, turnover down — only the
    # current ratio improves
    _annual(con, "ACME", FY26, ProfitLossForPeriod=-20, Assets=1200,
            CashFlowsFromUsedInOperatingActivities=-30, CurrentAssets=500, CurrentLiabilities=200,
            BorrowingsNoncurrent=500, EquityShareCapital=120, RevenueFromOperations=800,
            CostOfMaterialsConsumed=560)
    f = forensic.piotroski_f(con, "ACME")
    passed = {k for k, v in f.components.items() if v}
    assert passed == {"d_currentratio_up"} and f.value == 1


# ------------------------------------------------------------------ Beneish M
def test_beneish_m_eight_variables(con):
    _annual(con, "ACME", FY25, TradeReceivablesCurrent=100, RevenueFromOperations=800,
            CostOfMaterialsConsumed=500, CurrentAssets=400, PropertyPlantAndEquipment=280, Assets=900,
            DepreciationDepletionAndAmortisationExpense=45, Liabilities=450, EmployeeBenefitExpense=90,
            OtherExpenses=40, ProfitLossForPeriod=60, CashFlowsFromUsedInOperatingActivities=55)
    _annual(con, "ACME", FY26, TradeReceivablesCurrent=150, RevenueFromOperations=1000,
            CostOfMaterialsConsumed=600, CurrentAssets=500, PropertyPlantAndEquipment=300, Assets=1000,
            DepreciationDepletionAndAmortisationExpense=50, Liabilities=500, EmployeeBenefitExpense=100,
            OtherExpenses=50, ProfitLossForPeriod=80, CashFlowsFromUsedInOperatingActivities=60)
    m = forensic.beneish_m(con, "ACME")
    c = m.components
    assert c["DSRI"] == pytest.approx(1.2)                     # (150/1000) / (100/800)
    assert c["GMI"] == pytest.approx(0.375 / 0.4)
    assert c["AQI"] == pytest.approx(0.2 / (1 - 680 / 900))
    assert c["SGI"] == pytest.approx(1.25)
    assert c["DEPI"] == pytest.approx((45 / 325) / (50 / 350))
    assert c["SGAI"] == pytest.approx(0.15 / 0.1625)
    assert c["TATA"] == pytest.approx(0.02)
    assert c["LVGI"] == pytest.approx(1.0)
    assert m.value == pytest.approx(-2.0762, abs=1e-3)          # worked by hand from the eight above


# ------------------------------------------------------------------ Sloan accruals
def test_sloan_accruals(con):
    _annual(con, "ACME", FY25, CurrentAssets=400, CashAndCashEquivalents=80, CurrentLiabilities=250,
            BorrowingsCurrent=40, DepreciationDepletionAndAmortisationExpense=45, Assets=900)
    _annual(con, "ACME", FY26, CurrentAssets=500, CashAndCashEquivalents=100, CurrentLiabilities=300,
            BorrowingsCurrent=50, DepreciationDepletionAndAmortisationExpense=50, Assets=1000)
    # Δ non-cash CA = 400 − 320 = 80 · Δ non-debt CL = 250 − 210 = 40 · accruals = 80 − 40 − 50 = −10
    s = forensic.accruals(con, "ACME")
    assert s.value == pytest.approx(100 * -10 / 950)


def test_industrial_scores_are_not_applied_to_a_bank(con):
    _annual(con, "BANK", FY25, InterestEarned=100, InterestExpended=60, Assets=1000)
    _annual(con, "BANK", FY26, InterestEarned=120, InterestExpended=70, Assets=1100)
    for fn in (forensic.altman_z, forensic.piotroski_f, forensic.beneish_m, forensic.accruals):
        s = fn(con, "BANK")
        assert s.value is None and s.note == forensic.NOT_FOR_BANKS


# ------------------------------------------------------------------ valuation
def test_snapshot_market_cap_and_bonus_since_the_filing(con):
    # ₹10 cr of ₹10 shares = 1 crore shares at FY26; price ₹100 → market cap ₹100 cr
    _annual(con, "ACME", FY26, EquityShareCapital=10, FaceValueOfEquityShareCapital=10,
            Equity=50, ProfitLossForPeriod=5)
    _close(con, "ACME", date(2026, 6, 1), 100.0)
    s = valuation.snapshot(con, "ACME")
    assert s["shares_cr"] == pytest.approx(1.0) and s["market_cap_cr"] == pytest.approx(100.0)
    assert s["pb"] == pytest.approx(2.0)
    # a 1:1 bonus after the FY-end doubles the share count: same price, twice the market cap
    ca._upsert(con, [("ACME", date(2026, 5, 29), 0.5, 2.0, "bonus", "nse", "Bonus 1:1")])
    s = valuation.snapshot(con, "ACME")
    assert s["shares_cr"] == pytest.approx(2.0) and s["market_cap_cr"] == pytest.approx(200.0)
    assert s["shares_adjusted_for"] and "bonus" in s["note"]


def test_valuation_history_uses_each_years_own_shares_and_raw_price(con):
    _annual(con, "ACME", FY25, EquityShareCapital=10, FaceValueOfEquityShareCapital=10,
            Equity=40, ProfitLossForPeriod=4)
    _annual(con, "ACME", FY26, EquityShareCapital=10, FaceValueOfEquityShareCapital=2,   # 1:5 split
            Equity=50, ProfitLossForPeriod=5)
    _close(con, "ACME", FY25, 500.0)               # pre-split price
    _close(con, "ACME", FY26, 120.0)               # post-split price
    ca._upsert(con, [("ACME", date(2025, 9, 1), 0.2, 5.0, "split", "nse", "split 10→2")])
    h = valuation.valuation_history(con, "ACME").rename(index=lambda t: t.date())
    assert h.loc[FY25, "pe"] == pytest.approx(125.0)    # 1 cr shares × ₹500 = ₹500 cr ÷ ₹4 cr profit
    assert h.loc[FY26, "pe"] == pytest.approx(120.0)    # 5 cr shares × ₹120 = ₹600 cr ÷ ₹5 cr profit
    assert h.loc[FY25, "price"] == 500.0                # raw, not split-adjusted: pairs with 1 cr shares


# ------------------------------------------------------------------ smart-money cost zones
def test_institutional_cost_zone_uses_split_adjusted_prices(con):
    q1, q2 = date(2025, 12, 31), date(2026, 3, 31)
    d = q1 + timedelta(days=10)
    while d <= q2:                                  # the fund adds in Q4, pre-split at ₹1,000
        _close(con, "ACME", d, 1000.0)
        d += timedelta(days=7)
    ca._upsert(con, [("ACME", date(2026, 4, 1), 0.2, 5.0, "split", "nse", "split 10→2")])
    _close(con, "ACME", date(2026, 6, 1), 260.0)    # today: ₹260 after a 1:5 split = ₹1,300 old
    con.executemany("INSERT INTO shp_holders (symbol, as_of, holder_name, pct, category, is_promoter, "
                    "classification) VALUES (?, ?, ?, ?, 'mutual fund', false, '')",
                    [("ACME", q1, "SOME MF", 1.0), ("ACME", q2, "SOME MF", 3.0)])
    got = ownership.institutional_cost(con, "ACME")
    (h,) = got["holders"]
    assert h["avg_cost"] == pytest.approx(200.0)   # ₹1,000 pre-split = ₹200 today
    assert h["gain_pct"] == pytest.approx(30.0)    # not "−74% below cost"
    assert h["emoji"] == "🟡"


@pytest.mark.parametrize("gain, emoji", [(None, "⚪"), (-20, "🔵"), (5, "🟢"), (30, "🟡"),
                                         (60, "🟠"), (120, "🔴")])
def test_booking_flag_bands(gain, emoji):
    assert ownership.booking_flag(gain)[0] == emoji
