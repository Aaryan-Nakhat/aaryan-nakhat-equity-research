"""🏦 Bank analysis — the numbers that actually describe a lender.

Industrial yardsticks (EBITDA, working-capital days, free cash flow, Altman / Piotroski / Beneish)
don't fit a bank: its raw material is deposits, its product is loans, and its risk is bad loans. So
a bank is read on:

- **Earnings engine** — net interest income (NII = interest earned − interest expended), fee/other
  income, operating costs, pre-provision operating profit (PPOP), provisions, profit.
- **Margins & returns** — NIM (NII / average total assets — a close proxy for NIM on earning
  assets), cost-to-income, credit cost (provisions / average advances), ROA, ROE.
- **Asset quality** — gross / net NPA (₹ and % of loans) and provision coverage (PCR).
- **Capital & funding** — CET1 / Tier-1 ratios, credit-deposit (CD) ratio, loan & deposit growth.

Everything is computed from the bank-taxonomy XBRL already in ``financials`` (via the normalised
frames from ``fundamentals``) — deterministic, no LLM. ROA / ROE are computed here from profit and
average balances rather than read from the filing, because banks report the ROA field
inconsistently (some annualise the quarter, some don't).

``health_checks`` turns the metrics into plain ✅ / ⚠️ / 🔴 lines — the bank replacement for the
industrial forensic scores. Thresholds are env-tunable (``config.BANK_*``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from equity_research import config
from equity_research.analysis.fundamentals import CR


def _s(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index, dtype=float)


def _avg(series: pd.Series) -> pd.Series:
    """Average of this and the prior period's balance (the first period uses its own year-end)."""
    return ((series + series.shift(1)) / 2).fillna(series)


def _yoy(series: pd.Series, lag: int = 1) -> pd.Series:
    return 100 * (series / series.shift(lag) - 1)


def annual_metrics(af: pd.DataFrame) -> pd.DataFrame:
    """Per fiscal year (index = year-end): statement lines in ₹ crore + every bank ratio in %.
    ``af`` is a normalised annual frame (``fundamentals.load_annual``)."""
    if af is None or af.empty:
        return pd.DataFrame()
    ie, iex, oi = _s(af, "InterestEarned"), _s(af, "InterestExpended"), _s(af, "OtherIncome")
    nii = ie - iex
    opex = _s(af, "OperatingExpenses")
    ppop = _s(af, "OperatingProfitBeforeProvisionAndContingencies").fillna(nii + oi - opex)
    prov = _s(af, "ProvisionsOtherThanTaxAndContingencies")
    pat, pbt, tax = _s(af, "ProfitLossForPeriod"), _s(af, "ProfitBeforeTax"), _s(af, "TaxExpense")
    adv, dep, inv = _s(af, "Advances"), _s(af, "Deposits"), _s(af, "Investments")
    assets, borrow, networth = _s(af, "Assets"), _s(af, "Borrowings"), _s(af, "Equity")
    gnpa, nnpa = _s(af, "GrossNonPerformingAssets"), _s(af, "NonPerformingAssets")
    shares = _s(af, "EquityShareCapital") / _s(af, "FaceValueOfEquityShareCapital")

    m = pd.DataFrame(index=af.index)
    for col, ser in (("interest_earned_cr", ie), ("interest_expended_cr", iex), ("nii_cr", nii),
                     ("other_income_cr", oi), ("opex_cr", opex),
                     ("employee_cr", _s(af, "EmployeeBenefitExpense")), ("ppop_cr", ppop),
                     ("provisions_cr", prov), ("pbt_cr", pbt), ("tax_cr", tax), ("pat_cr", pat),
                     ("advances_cr", adv), ("investments_cr", inv), ("deposits_cr", dep),
                     ("borrowings_cr", borrow), ("networth_cr", networth), ("assets_cr", assets),
                     ("gnpa_cr", gnpa), ("nnpa_cr", nnpa)):
        m[col] = ser / CR
    m["nim_%"] = 100 * nii / _avg(assets)
    m["cost_to_income_%"] = 100 * opex / (nii + oi)
    m["credit_cost_%"] = 100 * prov / _avg(adv)
    m["roa_%"] = 100 * pat / _avg(assets)
    m["roe_%"] = 100 * pat / _avg(networth)
    m["fee_share_%"] = 100 * oi / (nii + oi)
    m["cd_ratio_%"] = 100 * adv / dep
    m["adv_yoy_%"], m["dep_yoy_%"] = _yoy(adv), _yoy(dep)
    m["nii_yoy_%"], m["ppop_yoy_%"], m["pat_yoy_%"] = _yoy(nii), _yoy(ppop), _yoy(pat)
    m["gnpa_%"] = 100 * _s(af, "PercentageOfGrossNpa")
    m["nnpa_%"] = 100 * _s(af, "PercentageOfNpa")
    m["pcr_%"] = 100 * (1 - nnpa / gnpa)
    m["cet1_%"] = 100 * _s(af, "CET1Ratio")
    m["tier1_%"] = 100 * (_s(af, "CET1Ratio") + _s(af, "AdditionalTier1Ratio"))
    m["bvps"] = networth / shares
    return m.replace([np.inf, -np.inf], np.nan)


def quarterly_metrics(qf: pd.DataFrame) -> pd.DataFrame:
    """Per quarter: the earnings engine + asset quality + capital. YoY needs the same quarter a
    year back (5+ quarters of history)."""
    if qf is None or qf.empty:
        return pd.DataFrame()
    ie, iex, oi = _s(qf, "InterestEarned"), _s(qf, "InterestExpended"), _s(qf, "OtherIncome")
    nii = ie - iex
    ppop = _s(qf, "OperatingProfitBeforeProvisionAndContingencies").fillna(
        nii + oi - _s(qf, "OperatingExpenses"))
    pat = _s(qf, "ProfitLossForPeriod")
    gnpa, nnpa = _s(qf, "GrossNonPerformingAssets"), _s(qf, "NonPerformingAssets")
    m = pd.DataFrame(index=qf.index)
    m["interest_earned_cr"] = ie / CR
    m["nii_cr"] = nii / CR
    m["other_income_cr"] = oi / CR
    m["ppop_cr"] = ppop / CR
    m["provisions_cr"] = _s(qf, "ProvisionsOtherThanTaxAndContingencies") / CR
    m["pat_cr"] = pat / CR
    m["nii_yoy_%"], m["pat_yoy_%"] = _yoy(nii, 4), _yoy(pat, 4)
    m["gnpa_%"] = 100 * _s(qf, "PercentageOfGrossNpa")
    m["nnpa_%"] = 100 * _s(qf, "PercentageOfNpa")
    m["pcr_%"] = 100 * (1 - nnpa / gnpa)
    m["cet1_%"] = 100 * _s(qf, "CET1Ratio")
    return m.replace([np.inf, -np.inf], np.nan)


REGULATORY_COLS = ("gnpa_%", "nnpa_%", "gnpa_cr", "nnpa_cr", "pcr_%", "cet1_%", "tier1_%")


def with_regulatory_fallback(m: pd.DataFrame, standalone: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """Asset-quality and capital ratios are reported for the **bank** (standalone), not the group —
    consolidated filings leave them blank. Fill ``m``'s missing regulatory columns from the matching
    standalone periods. Returns ``(frame, borrowed)`` — ``borrowed`` True if anything was filled,
    so the report can say where the figures come from."""
    if m is None or m.empty or standalone is None or standalone.empty:
        return m, False
    m = m.copy()
    borrowed = False
    for col in REGULATORY_COLS:
        if col not in standalone:
            continue
        src = standalone[col].reindex(m.index)
        gap = (m[col].isna() if col in m else pd.Series(True, index=m.index)) & src.notna()
        if gap.any():
            m.loc[gap, col] = src[gap]
            borrowed = True
    return m, borrowed


def _last(series: pd.Series | None) -> tuple[object, float] | None:
    """(period, value) of the latest non-NaN point, or None."""
    if series is None:
        return None
    s = series.dropna()
    return (s.index[-1], float(s.iloc[-1])) if len(s) else None


def _latest(qm: pd.DataFrame, am: pd.DataFrame, col: str) -> float | None:
    """Latest value of ``col``, preferring the (fresher) quarterly series."""
    for frame in (qm, am):
        if frame is not None and not frame.empty and col in frame:
            hit = _last(frame[col])
            if hit:
                return hit[1]
    return None


def health_checks(am: pd.DataFrame, qm: pd.DataFrame) -> list[tuple[str, str]]:
    """Plain-English checks on a bank → ``[(status, text)]`` with status ``ok`` / ``warn`` /
    ``alarm``. Only checks with data are emitted. The bank stand-in for industrial forensics."""
    out: list[tuple[str, str]] = []
    c = config

    # 1) asset-quality direction (quarterly GNPA trend)
    g = qm["gnpa_%"].dropna() if qm is not None and "gnpa_%" in qm else pd.Series(dtype=float)
    if len(g) >= 3:
        a, b, d = g.iloc[-3], g.iloc[-2], g.iloc[-1]
        path = " → ".join(f"{v:.2f}%" for v in g.iloc[-3:])
        if d > b > a:
            out.append(("warn", f"Gross NPA has risen 3 quarters running ({path}) — bad loans are "
                                "building up; watch the next quarter's slippages."))
        elif d < g.iloc[0]:
            out.append(("ok", f"Gross NPA is easing ({g.iloc[0]:.2f}% → {d:.2f}% over the last "
                              f"{len(g)} quarters) — asset quality improving."))
        else:
            out.append(("ok", f"Gross NPA broadly stable ({path})."))

    # 2) net NPA level
    nn = _latest(qm, am, "nnpa_%")
    if nn is not None:
        if nn > c.BANK_NNPA_ALARM:
            out.append(("alarm", f"Net NPA {nn:.2f}% — high: a large slice of loans is bad even "
                                 f"after provisions (above ~{c.BANK_NNPA_ALARM:.0f}%)."))
        elif nn > c.BANK_NNPA_WARN:
            out.append(("warn", f"Net NPA {nn:.2f}% — elevated (above ~{c.BANK_NNPA_WARN:.0f}%)."))
        else:
            out.append(("ok", f"Net NPA {nn:.2f}% — low; unprovided bad loans are small."))

    # 3) provision coverage
    pcr = _latest(qm, am, "pcr_%")
    if pcr is not None:
        if pcr < c.BANK_PCR_WARN:
            out.append(("warn", f"Provision coverage {pcr:.0f}% — thin cushion (below "
                                f"{c.BANK_PCR_WARN:.0f}%): more bad-loan losses may still hit profit."))
        else:
            out.append(("ok", f"Provision coverage {pcr:.0f}% — bad loans are well provided for."))

    # 4) credit-cost spike vs its own history
    cc = am["credit_cost_%"].dropna() if am is not None and "credit_cost_%" in am else pd.Series(dtype=float)
    if len(cc) >= 3:
        cur, prior = cc.iloc[-1], cc.iloc[-4:-1].mean()
        if prior > 0 and cur > c.BANK_CREDIT_COST_SPIKE_X * prior and cur > 0.5:
            out.append(("warn", f"Credit cost jumped to {cur:.2f}% of loans (vs {prior:.2f}% on "
                                "average before) — provisions are rising faster than the book."))
        else:
            out.append(("ok", f"Credit cost {cur:.2f}% of loans — in line with its recent "
                              f"history ({prior:.2f}% avg)."))

    # 5) capital
    cet1 = _latest(qm, am, "cet1_%")
    if cet1 is not None:
        if cet1 < c.BANK_CET1_WARN:
            out.append(("warn", f"CET1 {cet1:.1f}% — thin capital buffer (RBI's floor incl. the "
                                "conservation buffer is 8%); growth may need fresh equity."))
        else:
            out.append(("ok", f"CET1 {cet1:.1f}% — comfortably capitalised (RBI floor ~8%)."))

    # 6) funding: credit-deposit ratio
    cd = _latest(None, am, "cd_ratio_%")
    if cd is not None:
        if cd > c.BANK_CD_RATIO_WARN:
            out.append(("warn", f"Credit-deposit ratio {cd:.0f}% — loans are outrunning deposits; "
                                "growth leans on costlier borrowings (margin pressure)."))
        else:
            out.append(("ok", f"Credit-deposit ratio {cd:.0f}% — loans funded by deposits."))

    # 7) margin squeeze: NII growth vs loan growth (latest year)
    if am is not None and not am.empty:
        nii_g, adv_g = _last(am.get("nii_yoy_%")), _last(am.get("adv_yoy_%"))
        if nii_g and adv_g and nii_g[0] == adv_g[0]:
            if nii_g[1] < adv_g[1] - c.BANK_NII_LAG_PP:
                out.append(("warn", f"NII grew {nii_g[1]:.0f}% vs loans {adv_g[1]:.0f}% — margins "
                                    "are being squeezed (funding costs rising faster than yields)."))
            else:
                out.append(("ok", f"NII grew {nii_g[1]:.0f}% vs loans {adv_g[1]:.0f}% — margins "
                                  "holding up."))

    # 8) profitability
    roa = _latest(None, am, "roa_%")
    if roa is not None:
        if roa < c.BANK_ROA_WEAK:
            out.append(("warn", f"ROA {roa:.2f}% — weak profitability for a bank."))
        elif roa >= c.BANK_ROA_STRONG:
            out.append(("ok", f"ROA {roa:.2f}% — strong (≥{c.BANK_ROA_STRONG:.1f}% is top-tier "
                              "for an Indian bank)."))
        else:
            out.append(("ok", f"ROA {roa:.2f}% — healthy."))
    return out
