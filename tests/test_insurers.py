"""Insurers: IRDAI taxonomy normalisation (life & general), the filing-slip repairs, insurer metrics
& health checks, the forensic guard and the insurer-shaped deep report — synthetic insurers in a
throwaway DuckDB (no network)."""

from __future__ import annotations

import pytest

from equity_research.analysis import forensic, fundamentals as F, insurers, quant
from equity_research.common import db

CR = 1e7
EPS = "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"
_RAW = ("Ratio", "EPS", "BasicAnd")          # filed as-is (not ₹ crore)

LIFE = {  # ₹ crore unless a ratio / EPS
    "2025-03-31": dict(GrossPremiumIncome=1000, NetPremiumIncome=950, IncomeFirstYearPremium=200,
                       IncomeRenewalPremium=550, IncomeSinglePremium=250, Commission=100,
                       ProfitLossAfterTaxAndExtraordinaryItems=40, ProfitLossBeforeTax=45,
                       ShareCapital=100, ReservesAndSurplus=300, ShareholdersFunds=410,
                       SolvencyRatio=1.9, PersistencyRatio13ThMonth=0.85, PersistencyRatio61ThMonth=0.6,
                       ExpensesOfManagementRatio=0.2, Investments=9000, **{EPS: 4.0}),
    "2026-03-31": dict(GrossPremiumIncome=1150, NetPremiumIncome=1100, IncomeFirstYearPremium=220,
                       IncomeRenewalPremium=630, IncomeSinglePremium=300, Commission=120,
                       ProfitLossAfterTaxAndExtraordinaryItems=50, ProfitLossBeforeTax=56,
                       # ICICI-Pru-style slip: reserves filed elsewhere → capital + reserves is tiny
                       ShareCapital=100, ReservesAndSurplus=1, ShareholdersFunds=460,
                       # LIC-style slips: solvency and persistency filed 100x too small
                       SolvencyRatio=0.018, PersistencyRatio13ThMonth=0.0072, PersistencyRatio61ThMonth=0.62,
                       ExpensesOfManagementRatio=0.21, Investments=10000, **{EPS: 5.0}),
}
GENERAL = {
    "2025-03-31": dict(GrossPremiumsWritten=2000, NetPremiumWritten=1500, PremiumEarned=1400,
                       IncurredClaims=1000, UnderwritingProfitOrLoss=-60, IncomeFromInvestmentsNet=200,
                       ProfitOrLossBeforeTax=140, ProfitLossAfterTax=105, ShareCapital=50,
                       ReservesAndSurplus=900, ShareholdersFunds=950, CombinedRatio=1.04,
                       IncurredClaimRatio=0.71, SolvencyRatio=2.5, NetRetentionRatio=0.75, **{EPS: 21.0}),
    "2026-03-31": dict(GrossPremiumsWritten=2200, NetPremiumWritten=1650, PremiumEarned=1550,
                       IncurredClaims=1150, UnderwritingProfitOrLoss=-250, IncomeFromInvestmentsNet=210,
                       ProfitOrLossBeforeTax=-40, ProfitLossAfterTax=-30, ShareCapital=50,
                       # ICICI-Lombard-style slip: 'ShareholdersFunds' negative → capital + reserves right
                       ReservesAndSurplus=1000, ShareholdersFunds=-56, CombinedRatio=1.23,
                       IncurredClaimRatio=0.8, SolvencyRatio=0.0141, NetRetentionRatio=0.75, **{EPS: -6.0}),
}


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    rows = []
    for sym, data in (("LIFEX", LIFE), ("GENX", GENERAL)):
        for pe, els in data.items():
            for el, v in els.items():
                val = v if any(k in el for k in _RAW) else v * CR
                rows.append((sym, pe, None, "Y", False, el, val))
    # general insurer: claims ratio rising 3 quarters running
    for pe, cl in (("2025-09-30", 0.70), ("2025-12-31", 0.74), ("2026-03-31", 0.79)):
        rows += [("GENX", pe, None, "Q", False, "GrossPremiumsWritten", 550 * CR),
                 ("GENX", pe, None, "Q", False, "IncurredClaimRatio", cl)]
    c.executemany("INSERT INTO financials (symbol, period_end, period_start, period_type, "
                  "consolidated, element, value) VALUES (?,?,?,?,?,?,?)", rows)
    yield c
    c.close()


def test_kinds_are_detected(con):
    assert F.filer_kind(con, "LIFEX") == "life" and F.filer_kind(con, "GENX") == "general"
    assert F.taxonomy(F.load_annual(con, "LIFEX")) == "life"


def test_life_normalisation_and_repairs(con):
    last = F.load_annual(con, "LIFEX").iloc[-1]
    assert last["RevenueFromOperations"] == 1150 * CR                   # gross premium = top line
    assert last["ProfitLossForPeriod"] == 50 * CR
    assert last["Equity"] == 460 * CR                                   # the larger positive net worth
    assert last["FaceValueOfEquityShareCapital"] == 10                  # ₹100 cr ÷ (₹50 cr ÷ ₹5 EPS)
    assert last["SolvencyRatio"] == pytest.approx(1.8)                  # 0.018 → 1.8x
    assert last["PersistencyRatio13ThMonth"] == pytest.approx(0.72)     # 0.0072 → 72%
    assert last["PersistencyRatio61ThMonth"] == pytest.approx(0.62)     # a real value is untouched


def test_general_normalisation_and_repairs(con):
    last = F.load_annual(con, "GENX").iloc[-1]
    assert last["RevenueFromOperations"] == 2200 * CR
    assert last["Equity"] == 1050 * CR                                  # capital + reserves, not -56
    assert last["SolvencyRatio"] == pytest.approx(1.41)


def test_life_metrics(con):
    am = insurers.life_metrics(F.load_annual(con, "LIFEX")).iloc[-1]
    assert am["ape_cr"] == pytest.approx(220 + 30)                      # first-year + 10% of single
    assert am["ape_yoy_%"] == pytest.approx(100 * (250 / 225 - 1))
    assert am["renewal_share_%"] == pytest.approx(100 * 630 / 1150)
    assert am["roe_%"] == pytest.approx(100 * 50 / ((410 + 460) / 2))


def test_general_metrics(con):
    am = insurers.general_metrics(F.load_annual(con, "GENX")).iloc[-1]
    assert am["combined_ratio_%"] == pytest.approx(123)
    assert am["underwriting_margin_%"] == pytest.approx(100 * -250 / 1550)


def test_life_health_checks(con):
    am = insurers.life_metrics(F.load_annual(con, "LIFEX"))
    checks = insurers.health_checks("life", am, am.iloc[0:0])
    by_word = {t.split(" ")[0]: s for s, t in checks}
    assert by_word["13th-month"] == "warn"                              # 72% < 75%
    assert by_word["Solvency"] == "ok"                                  # 1.8x


def test_general_health_checks(con):
    am = insurers.general_metrics(F.load_annual(con, "GENX"))
    qm = insurers.general_metrics(F.load_quarters(con, "GENX"), quarterly=True)
    checks = insurers.health_checks("general", am, qm)
    text = " | ".join(t for _, t in checks)
    status = {t.split(" ")[0]: s for s, t in checks}
    assert status["Combined"] == "alarm"                                # 123% > 120%
    assert status["Solvency"] == "alarm"                                # 1.41x is below the 1.5x floor
    assert "risen 3 quarters running" in text
    assert "no longer covers" in text                                   # 210 < 250


def test_solvency_below_the_floor_is_an_alarm():
    import pandas as pd
    am = pd.DataFrame({"solvency_x": [1.4]})
    assert insurers.health_checks("life", am, pd.DataFrame())[0][0] == "alarm"


def test_industrial_scores_say_not_applicable(con):
    for s in (forensic.altman_z(con, "GENX"), forensic.piotroski_f(con, "LIFEX"),
              forensic.beneish_m(con, "LIFEX"), forensic.accruals(con, "GENX")):
        assert s.value is None and s.note == forensic.NOT_FOR_INSURERS


def test_insurer_peer_ratios(con):
    r = quant._ratios(con, "GENX", False)
    assert "D/E" not in r and r["Combined%"] == pytest.approx(123) and r["Solvency(x)"] == pytest.approx(1.41)


def test_deep_report_is_insurer_shaped(con):
    from equity_research.reports.deep_brief import build_deep_brief
    life, gen = build_deep_brief(con, "LIFEX"), build_deep_brief(con, "GENX")
    assert "life insurer" in life and "APE" in life and "Persistency" in life
    assert "Combined ratio" in gen and "Where the profit comes from" in gen
    for md in (life, gen):
        assert "Insurer health checks" in md and "not applicable to insurers" in md
        assert "EBITDA margin" not in md and "Receivable days" not in md
