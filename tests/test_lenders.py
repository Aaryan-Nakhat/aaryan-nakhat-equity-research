"""Banks: taxonomy normalisation, ratio-scale repair, bank metrics & health checks, the forensic
guard, and the bank-shaped deep report — on a synthetic bank in a throwaway DuckDB (no network)."""

from __future__ import annotations

import pandas as pd
import pytest

from equity_research.analysis import forensic, fundamentals as F, lenders, quant
from equity_research.common import db

CR = 1e7
# FY2025 → FY2026 annual bank filing (₹ values as filed; ratios as fractions)
_ANNUAL = {
    "2025-03-31": dict(InterestEarned=1000, InterestExpended=600, OtherIncome=100,
                       OperatingExpenses=200, OperatingProfitBeforeProvisionAndContingencies=300,
                       ProvisionsOtherThanTaxAndContingencies=40, ProfitLossFromOrdinaryActivitiesBeforeTax=260,
                       TaxExpense=65, ProfitLossForThePeriod=195, Advances=9000, Deposits=10000,
                       Investments=3000, Assets=14000, Borrowings=1500, Capital=100, ReservesAndSurplus=1400,
                       PaidUpValueOfEquityShareCapital=100, FaceValueOfEquityShareCapital=1,
                       GrossNonPerformingAssets=180, NonPerformingAssets=50,
                       PercentageOfGrossNpa=0.02, PercentageOfNpa=0.0055, CET1Ratio=0.15),
    "2026-03-31": dict(InterestEarned=1150, InterestExpended=700, OtherIncome=120,
                       OperatingExpenses=220, OperatingProfitBeforeProvisionAndContingencies=350,
                       ProvisionsOtherThanTaxAndContingencies=60, ProfitLossFromOrdinaryActivitiesBeforeTax=290,
                       TaxExpense=72, ProfitLossForThePeriod=218, Advances=10800, Deposits=11000,
                       Investments=3200, Assets=16000, Borrowings=1800, Capital=100, ReservesAndSurplus=1600,
                       PaidUpValueOfEquityShareCapital=100, FaceValueOfEquityShareCapital=1,
                       GrossNonPerformingAssets=210, NonPerformingAssets=80,
                       # filed 100x too small (as some banks do) — must be repaired to 1.9% / 12%
                       PercentageOfGrossNpa=0.00019, PercentageOfNpa=0.00007, CET1Ratio=0.0012),
}


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    rows = []
    for pe, els in _ANNUAL.items():
        for el, v in els.items():
            val = v if el.startswith(("Percentage", "CET1", "FaceValue")) else v * CR
            rows.append(("BANKX", pe, None, "Y", False, el, val))
    # quarterly GNPA path rising 3 quarters running → a warning
    for pe, g in (("2025-09-30", 0.018), ("2025-12-31", 0.019), ("2026-03-31", 0.0205)):
        rows += [("BANKX", pe, None, "Q", False, "InterestEarned", 280 * CR),
                 ("BANKX", pe, None, "Q", False, "InterestExpended", 170 * CR),
                 ("BANKX", pe, None, "Q", False, "PercentageOfGrossNpa", g)]
    c.executemany("INSERT INTO financials (symbol, period_end, period_start, period_type, "
                  "consolidated, element, value) VALUES (?,?,?,?,?,?,?)", rows)
    yield c
    c.close()


def test_bank_frame_is_detected_and_normalised(con):
    af = F.load_annual(con, "BANKX")
    assert F.is_bank_frame(af) and F.is_bank(con, "BANKX")
    last = af.iloc[-1]
    assert last["RevenueFromOperations"] == 1150 * CR          # interest earned = top line
    assert last["ProfitLossForPeriod"] == 218 * CR
    assert last["ProfitBeforeTax"] == 290 * CR
    assert last["Equity"] == 1700 * CR                          # capital + reserves
    assert "FinanceCosts" not in af.columns                     # interest expended is NOT a finance cost


def test_misscaled_ratios_are_repaired(con):
    last = F.load_annual(con, "BANKX").iloc[-1]
    assert last["CET1Ratio"] == pytest.approx(0.12)
    assert last["PercentageOfGrossNpa"] == pytest.approx(0.019)
    assert last["PercentageOfNpa"] == pytest.approx(0.007)


def test_zero_regulatory_ratios_mean_unreported():
    df = pd.DataFrame({"InterestEarned": [1.0], "InterestExpended": [0.5], "CET1Ratio": [0.0],
                       "PercentageOfGrossNpa": [0.0], "PercentageOfNpa": [0.0],
                       "GrossNonPerformingAssets": [0.0]})
    out = F._normalise(df)
    assert out[["CET1Ratio", "PercentageOfGrossNpa", "PercentageOfNpa",
                "GrossNonPerformingAssets"]].isna().all(axis=None)


def test_bank_metrics(con):
    am = lenders.annual_metrics(F.load_annual(con, "BANKX")).iloc[-1]
    assert am["nii_cr"] == pytest.approx(450)
    assert am["cost_to_income_%"] == pytest.approx(100 * 220 / 570)
    assert am["nim_%"] == pytest.approx(100 * 450 / 15000)                  # avg assets
    assert am["credit_cost_%"] == pytest.approx(100 * 60 / 9900)            # avg advances
    assert am["roe_%"] == pytest.approx(100 * 218 / 1600)                   # avg net worth
    assert am["cd_ratio_%"] == pytest.approx(100 * 10800 / 11000)
    assert am["pcr_%"] == pytest.approx(100 * (1 - 80 / 210))
    assert am["cet1_%"] == pytest.approx(12.0)
    assert am["bvps"] == pytest.approx(17.0)          # ₹1,700 cr net worth / 100 cr shares (FV ₹1)


def test_health_checks_flag_the_right_things(con):
    am = lenders.annual_metrics(F.load_annual(con, "BANKX"))
    qm = lenders.quarterly_metrics(F.load_quarters(con, "BANKX"))
    checks = dict((t.split(" ")[0] + " " + t.split(" ")[1], s) for s, t in lenders.health_checks(am, qm))
    text = " | ".join(t for _, t in lenders.health_checks(am, qm))
    assert "risen 3 quarters running" in text                               # 1.8 → 1.9 → 2.05
    assert checks.get("Credit-deposit ratio") == "warn"                      # 98% > 90%
    assert any(s == "ok" and "CET1 12.0%" in t for s, t in lenders.health_checks(am, qm))


def test_industrial_scores_say_not_applicable_for_a_bank(con):
    for score in (forensic.altman_z(con, "BANKX"), forensic.piotroski_f(con, "BANKX"),
                  forensic.beneish_m(con, "BANKX"), forensic.accruals(con, "BANKX")):
        assert score.value is None and score.note == forensic.NOT_FOR_BANKS


def test_bank_peer_ratios_have_no_fake_leverage(con):
    r = quant._ratios(con, "BANKX", False)
    assert "D/E" not in r and r["ROA%"] == pytest.approx(100 * 218 / 16000)
    assert r["GNPA%"] == pytest.approx(1.9)


def test_consolidated_borrows_regulatory_ratios_from_standalone():
    idx = pd.to_datetime(["2026-03-31"])
    cons = pd.DataFrame({"gnpa_%": [float("nan")], "cet1_%": [float("nan")], "pat_cr": [250.0]}, index=idx)
    std = pd.DataFrame({"gnpa_%": [1.9], "cet1_%": [12.0], "pat_cr": [218.0]}, index=idx)
    out, borrowed = lenders.with_regulatory_fallback(cons, std)
    assert borrowed and out.iloc[0]["gnpa_%"] == 1.9 and out.iloc[0]["cet1_%"] == 12.0
    assert out.iloc[0]["pat_cr"] == 250.0                       # group P&L is never replaced


def test_deep_report_is_bank_shaped(con):
    from equity_research.reports.deep_brief import build_deep_brief
    md = build_deep_brief(con, "BANKX")
    assert "Income statement (bank)" in md and "Net interest income (NII)" in md
    assert "Asset quality — bad loans" in md and "Bank health checks" in md
    assert "not applicable to banks" in md
    assert "EBITDA margin" not in md and "Receivable days" not in md      # no industrial tables
