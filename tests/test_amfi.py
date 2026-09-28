"""AMFI NAV parsing — header-driven, so a column change can't silently empty the NAV series again."""

from __future__ import annotations

from datetime import date

import pytest

from equity_research.scrapers import amfi

NAVALL_2026 = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122639;INF879O01027;-;Parag Parikh Flexi Cap Fund;Direct Plan;Growth Option;82.4800;28-Sep-2026
122640;-;INF879O01035;Parag Parikh Flexi Cap Fund;Regular Plan;IDCW Option;70.1000;28-Sep-2026
"""

NAVALL_OLD = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122639;INF879O01027;-;Parag Parikh Flexi Cap Fund - Direct Plan - Growth;80.1000;18-Aug-2026
"""

HISTORY_2026 = """Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date
Open Ended Schemes ( Equity Scheme - Large Cap Fund )
PPFAS Mutual Fund
154155;Parag Parikh Large Cap Fund - Direct Plan - Growth;Direct Plan;Growth;INF879O01332;;9.2867;24-Sep-2026
"""

HISTORY_OLD = """Scheme Code;Scheme Name;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Repurchase Price;Sale Price;Date
PPFAS Mutual Fund
154155;Parag Parikh Large Cap Fund - Direct Plan - Growth;INF879O01332;;9.1000;;;18-Aug-2026
"""


def test_navall_with_plan_and_option_columns():
    rows = amfi._parse_navall(NAVALL_2026)
    assert [(r.scheme_code, r.nav, r.nav_date, r.plan, r.option) for r in rows] == [
        (122639, 82.48, date(2026, 9, 28), "Direct", "Growth"),
        (122640, 70.10, date(2026, 9, 28), "Regular", "IDCW"),
    ]
    assert rows[0].isin_growth == "INF879O01027" and rows[1].isin_growth is None
    assert rows[0].amc == "PPFAS Mutual Fund" and "Flexi Cap" in rows[0].category


def test_navall_old_layout_still_parses():
    (r,) = amfi._parse_navall(NAVALL_OLD)
    assert (r.nav, r.nav_date, r.plan, r.option) == (80.10, date(2026, 8, 18), "Direct", "Growth")


@pytest.mark.parametrize("text, nav, day", [(HISTORY_2026, 9.2867, date(2026, 9, 24)),
                                            (HISTORY_OLD, 9.1, date(2026, 8, 18))])
def test_history_both_layouts(text, nav, day):
    assert amfi._parse_history(text) == [(154155, day, nav)]


def test_a_header_without_the_nav_column_fails_loudly():
    with pytest.raises(amfi.AmfiFormatChanged):
        amfi._parse_navall("Scheme Code;Scheme Name;Price;When\n1;X;2;3\n")
