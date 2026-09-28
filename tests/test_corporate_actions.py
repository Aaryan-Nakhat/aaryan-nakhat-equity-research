"""Split / bonus / rights / demerger price adjustments (analysis/corporate_actions.py + the
``equity_eod_adj`` view) and the holiday-bhavcopy guard."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from equity_research import ingest
from equity_research.analysis import corporate_actions as ca
from equity_research.common import db
from equity_research.common.http import ScrapeError


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


def _eod(con, symbol, rows, series="EQ"):
    """rows: [(date, prev_close, open, close, volume)]"""
    for d, prev, opn, close, vol in rows:
        con.execute("INSERT INTO equity_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [d, symbol, series, prev, opn, max(opn, close), min(opn, close), close, close,
                     close, vol, close * vol / 1e5, 100, vol // 2, 50.0])


# ------------------------------------------------------------------ parsing NSE subjects
@pytest.mark.parametrize("subject, kind, factor", [
    ("Bonus 1:1", "bonus", 0.5),
    ("BONUS 4:5", "bonus", 5 / 9),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "split", 0.1),
    ("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS10/- PER SHARE TO RS 2/- PER SHARE", "split", 0.2),
    ("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share", "consolidation", 10.0),
    ("Rights 1:1 @ Premium Rs 0/-", "rights", None),
    ("Demerger", "demerger", None),
])
def test_parse_subject(subject, kind, factor):
    k, f = ca.parse_subject(subject)
    assert k == kind
    assert (f is None and factor is None) or math.isclose(f, factor)


@pytest.mark.parametrize("subject", ["Scheme Of Arrangement - Bonus Ncrps 4:1", "Dividend - Rs 5 Per Share",
                                     "Annual General Meeting", ""])
def test_parse_subject_ignores_non_equity_and_non_share_actions(subject):
    assert ca.parse_subject(subject) is None


# ------------------------------------------------------------------ sizing one ex-date
def test_rights_use_the_theoretical_ex_rights_price():
    # MAHAPEX: 1:1 rights at par (₹10 face value) against a ₹126.90 close → TERP ₹68.45
    f, mult, how = ca.size_parts([("rights", None, "Rights 1:1 @ Premium Rs 0/-")],
                                 (date(2026, 3, 20), 126.9, 60.6), 10.0)
    assert math.isclose(f, 68.45 / 126.9) and mult == 2.0 and "68.45" in how


def test_rights_above_market_move_no_price_but_still_add_shares():
    f, mult, _ = ca.size_parts([("rights", None, "Rights 1:5 @ Premium Rs 303/-")],
                               (date(2026, 2, 26), 300.0, 301.0), 10.0)
    assert f == 1.0 and math.isclose(mult, 1.2)


def test_rights_without_a_face_value_stay_unapplied():
    f, mult, how = ca.size_parts([("rights", None, "Rights 1:1 @ Premium Rs 0/-")],
                                 (date(2026, 3, 20), 126.9, 60.6), None)
    assert f is None and mult == 2.0 and "face value" in how


def test_demerger_is_sized_by_the_discovered_open_and_leaves_shares_alone():
    f, mult, _ = ca.size_parts([("demerger", None, "Demerger")], (date(2025, 4, 7), 4928.15, 2450.0), None)
    assert math.isclose(f, 2450 / 4928.15) and mult == 1.0
    f, _, how = ca.size_parts([("demerger", None, "Demerger")], (date(2025, 4, 7), 100.0, 101.0), None)
    assert f is None and "no drop" in how


def test_bonus_plus_split_on_one_day_multiply():
    f, mult, _ = ca.size_parts([("bonus", 0.5, "Bonus 1:1"), ("split", 0.2, "split 10→2")], None, None)
    assert math.isclose(f, 0.1) and math.isclose(mult, 10.0)


def test_nse_parts_files_records_under_former_symbols():
    parts = ca.nse_parts([{"symbol": "TMPV", "exDate": "14-Oct-2025", "subject": "Demerger"},
                          {"symbol": "TMPV", "exDate": "14-Oct-2025", "subject": "Demerger"}],
                         {"TMPV": ["TATAMOTORS"]})
    assert set(parts) == {("TMPV", date(2025, 10, 14)), ("TATAMOTORS", date(2025, 10, 14))}
    assert parts[("TATAMOTORS", date(2025, 10, 14))] == ("TMPV", [("demerger", None, "Demerger")])


# ------------------------------------------------------------------ detection from the bhavcopy
def test_snap_accepts_standard_ratios_and_refuses_ambiguity():
    assert ca._snap(0.204) == pytest.approx(1 / 5)
    assert ca._snap(0.101) == pytest.approx(1 / 10)
    assert ca._snap(0.343) == pytest.approx(1 / 3)
    assert ca._snap(0.605) is None                   # a demerger-sized drop fits no ratio
    assert ca._snap(0.9) is None


def test_detect_gaps_applies_etf_splits_and_skips_listing_days(con):
    d0 = date(2026, 2, 24)
    _eod(con, "GOLDETF", [(d0, 150.0, 150.0, 151.0, 1000), (d0 + timedelta(days=3), 151.0, 15.2, 15.3, 10000)])
    _eod(con, "NEWIPO", [(d0 + timedelta(days=3), 120.0, 228.0, 239.0, 5000)], series="ST")  # listing day
    _eod(con, "CRASH", [(d0, 100.0, 100.0, 100.0, 1000), (d0 + timedelta(days=3), 100.0, 60.5, 55.0, 9000)])
    out = ca.detect_gaps(con, etfs={"GOLDETF"}, nse_covered=True)
    got = {r[0]: r for r in con.execute("SELECT symbol, factor, kind FROM price_adjustments").fetchall()}
    assert got["GOLDETF"][1] == pytest.approx(0.1)
    assert "NEWIPO" not in got                         # prev_close is the issue price, not a split
    assert got["CRASH"][1] is None and got["CRASH"][2] == "unexplained"
    assert out == {"applied": 1, "unexplained": 1}


def test_stock_gap_without_an_nse_record_applies_only_when_nse_is_off(con):
    d0 = date(2026, 2, 24)
    _eod(con, "ACME", [(d0, 500.0, 500.0, 500.0, 1000), (d0 + timedelta(days=3), 500.0, 101.0, 102.0, 5000)])
    ca.detect_gaps(con, etfs=set(), nse_covered=True)
    assert con.execute("SELECT factor FROM price_adjustments").fetchone()[0] is None
    con.execute("DELETE FROM price_adjustments")
    ca.detect_gaps(con, etfs=set(), nse_covered=False)
    assert con.execute("SELECT factor FROM price_adjustments").fetchone()[0] == pytest.approx(0.2)


# ------------------------------------------------------------------ the adjusted view
def test_adjusted_view_is_continuous_across_two_actions(con):
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    _eod(con, "XYZ", [(days[0], 1000.0, 1000.0, 1000.0, 100),
                      (days[1], 1000.0, 1000.0, 1000.0, 100),
                      (days[2], 1000.0, 500.0, 500.0, 200),      # 1:1 bonus ex-date
                      (days[3], 500.0, 500.0, 500.0, 200),
                      (days[4], 500.0, 100.0, 100.0, 1000)])     # 1:5 split ex-date
    ca._upsert(con, [("XYZ", days[2], 0.5, 2.0, "bonus", "nse", "Bonus 1:1"),
                     ("XYZ", days[4], 0.2, 5.0, "split", "nse", "split")])
    rows = con.execute("SELECT close, prev_close, ttl_trd_qnty FROM equity_eod_adj ORDER BY trade_date").fetchall()
    assert [round(r[0], 6) for r in rows] == [100.0] * 5              # one flat line after adjusting
    assert [round(r[1], 6) for r in rows] == [100.0] * 5              # prev_close too, incl. on ex-dates
    assert [r[2] for r in rows] == [1000] * 5                         # volumes scaled the other way


def test_future_ex_dates_are_not_applied_yet(con):
    d = date(2026, 1, 5)
    _eod(con, "XYZ", [(d, 100.0, 100.0, 100.0, 100)])
    ca._upsert(con, [("XYZ", d + timedelta(days=30), 0.5, 2.0, "bonus", "nse", "Bonus 1:1")])
    assert con.execute("SELECT close FROM equity_eod_adj").fetchone()[0] == 100.0


def test_share_multiplier_since_the_filing(con):
    d = date(2026, 6, 1)
    _eod(con, "LICI", [(d, 900.0, 900.0, 900.0, 100)])
    ca._upsert(con, [("LICI", date(2026, 5, 29), 0.5, 2.0, "bonus", "nse", "Bonus 1:1"),
                     ("LICI", date(2025, 1, 1), 0.5, 2.0, "bonus", "nse", "before the filing"),
                     ("LICI", date(2026, 5, 30), 0.7, 1.0, "demerger", "nse", "Demerger")])
    mult, labels, exs = ca.share_multiplier_since(con, "LICI", date(2026, 3, 31))
    assert mult == 2.0 and exs == [date(2026, 5, 29)] and "×2" in labels[0]


# ------------------------------------------------------------------ data hygiene
def test_holiday_bhavcopy_is_rejected(monkeypatch, con):
    df = pd.DataFrame({"SYMBOL": ["A"], "SERIES": ["EQ"], "DATE1": ["11-Sep-2026"], "PREV_CLOSE": [1.0],
                       "OPEN_PRICE": [1.0], "HIGH_PRICE": [1.0], "LOW_PRICE": [1.0], "LAST_PRICE": [1.0],
                       "CLOSE_PRICE": [1.0], "AVG_PRICE": [1.0], "TTL_TRD_QNTY": [1], "TURNOVER_LACS": [1.0],
                       "NO_OF_TRADES": [1], "DELIV_QTY": [1], "DELIV_PER": [1.0]})
    monkeypatch.setattr(ingest.nse_archives, "fetch_bhavcopy", lambda d: df)
    with pytest.raises(ScrapeError):
        ingest.ingest_bhavcopy(date(2026, 9, 14), con)            # a holiday: NSE served the 11th's file
    assert ingest.ingest_bhavcopy(date(2026, 9, 11), con) == 1


def test_phantom_holiday_sessions_are_removed(con):
    d1, d2, d3 = date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)
    for i in range(250):
        s = f"S{i}"
        _eod(con, s, [(d1, 10.0, 10.0, 11.0, 100), (d2, 10.0, 10.0, 11.0, 100), (d3, 11.0, 11.0, 12.0, 120)])
    assert ca.repair_phantom_sessions(con) == [d2]
    assert {r[0] for r in con.execute("SELECT DISTINCT trade_date FROM equity_eod").fetchall()} == {d1, d3}
