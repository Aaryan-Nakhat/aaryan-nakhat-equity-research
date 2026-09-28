"""🛡️ Insurer analysis — the numbers that actually describe an insurance company.

An insurer collects premiums today and pays claims / benefits later, earning on the money in
between, so industrial yardsticks (EBITDA, working capital, free cash flow, Altman / Piotroski /
Beneish) don't fit. Indian insurers file under IRDAI's two taxonomies, read here as:

**Life insurers** — premium engine (gross premium split into first-year / renewal / single),
**APE** (annualised premium equivalent = first-year regular premium + 10% of single premium, the
industry's new-business measure), renewal share (how much business is recurring), commission and
expense-of-management ratios, **persistency** (share of policies still paying at month 13 … 61 —
the quality of what was sold), conservation ratio, **solvency**, profit and ROE.

**General insurers** — gross / net premium written, premium earned, **claims ratio**, expense
ratio, **combined ratio** (claims + expenses as % of premium: under 100% = an underwriting profit;
most Indian general insurers run ~100–115% and earn their profit on investment income),
underwriting profit / loss, investment income, retention, **solvency**, profit and ROE.

Deterministic, from the XBRL already in ``financials`` (normalised by ``fundamentals``). Not in the
structured filing, so not here: a life insurer's **VNB margin** and **embedded value** — the value
metrics analysts lead with — appear only in investor presentations (the report's analysis reads
those filings). ``health_checks`` gives plain ✅ / ⚠️ / 🔴 lines (thresholds ``config.INS_*``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from equity_research import config
from equity_research.analysis.fundamentals import CR


def _s(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index, dtype=float)


def _avg(series: pd.Series) -> pd.Series:
    return ((series + series.shift(1)) / 2).fillna(series)


def _yoy(series: pd.Series, lag: int) -> pd.Series:
    return 100 * (series / series.shift(lag) - 1)


def _pct(df: pd.DataFrame, name: str) -> pd.Series:
    return 100 * _s(df, name)          # ratios are filed as fractions


def life_metrics(df: pd.DataFrame, *, quarterly: bool = False) -> pd.DataFrame:
    """Per period (annual, or quarterly with ``quarterly=True`` — growth then vs the same quarter
    last year): life-insurer lines in ₹ crore + ratios in % (solvency in x)."""
    if df is None or df.empty:
        return pd.DataFrame()
    lag = 4 if quarterly else 1
    gpi, fyp, single = _s(df, "GrossPremiumIncome"), _s(df, "IncomeFirstYearPremium"), _s(df, "IncomeSinglePremium")
    renewal, pat, eq = _s(df, "IncomeRenewalPremium"), _s(df, "ProfitLossForPeriod"), _s(df, "Equity")
    ape = fyp + 0.1 * single
    m = pd.DataFrame(index=df.index)
    for col, ser in (("gross_premium_cr", gpi), ("net_premium_cr", _s(df, "NetPremiumIncome")),
                     ("first_year_premium_cr", fyp), ("renewal_premium_cr", renewal),
                     ("single_premium_cr", single), ("ape_cr", ape),
                     ("commission_cr", _s(df, "Commission")),
                     ("opex_cr", _s(df, "OperatingExpensesRelatedToInsuranceBusiness")),
                     ("benefits_paid_cr", _s(df, "BenefitsPaidNet")),
                     ("investment_income_cr", _s(df, "IncomeFromInvestmentsNet")),
                     ("pbt_cr", _s(df, "ProfitBeforeTax")), ("pat_cr", pat),
                     ("networth_cr", eq), ("investments_cr", _s(df, "Investments"))):
        m[col] = ser / CR
    m["gross_premium_yoy_%"] = _yoy(gpi, lag)
    m["ape_yoy_%"] = _yoy(ape, lag)
    m["renewal_share_%"] = 100 * renewal / gpi
    m["commission_ratio_%"] = 100 * _s(df, "Commission") / gpi
    m["expense_ratio_%"] = _pct(df, "ExpensesOfManagementRatio")
    m["conservation_%"] = _pct(df, "ConservationRatio")
    for mo in ("13", "25", "37", "49", "61"):
        m[f"persistency_{mo}m_%"] = _pct(df, f"PersistencyRatio{mo}ThMonth")
    m["solvency_x"] = _s(df, "SolvencyRatio")
    m["pat_yoy_%"] = _yoy(pat, lag)
    if not quarterly:
        m["roe_%"] = 100 * pat / _avg(eq)
        m["aum_yoy_%"] = _yoy(_s(df, "Investments"), 1)
    return m.replace([np.inf, -np.inf], np.nan)


def general_metrics(df: pd.DataFrame, *, quarterly: bool = False) -> pd.DataFrame:
    """Per period: general-insurer lines in ₹ crore + ratios in % (solvency in x)."""
    if df is None or df.empty:
        return pd.DataFrame()
    lag = 4 if quarterly else 1
    gwp, earned = _s(df, "GrossPremiumsWritten"), _s(df, "PremiumEarned")
    uw, inv = _s(df, "UnderwritingProfitOrLoss"), _s(df, "IncomeFromInvestmentsNet")
    pat, pbt, eq = _s(df, "ProfitLossForPeriod"), _s(df, "ProfitBeforeTax"), _s(df, "Equity")
    m = pd.DataFrame(index=df.index)
    for col, ser in (("gross_premium_cr", gwp), ("net_premium_cr", _s(df, "NetPremiumWritten")),
                     ("premium_earned_cr", earned), ("claims_incurred_cr", _s(df, "IncurredClaims")),
                     ("commission_cr", _s(df, "CommissionsAndBrokerageNet")),
                     ("opex_cr", _s(df, "OperatingExpensesRelatedToInsuranceBusiness")),
                     ("underwriting_cr", uw), ("investment_income_cr", inv),
                     ("pbt_cr", pbt), ("pat_cr", pat), ("networth_cr", eq),
                     ("investments_cr", _s(df, "Investments"))):
        m[col] = ser / CR
    m["gross_premium_yoy_%"] = _yoy(gwp, lag)
    m["claims_ratio_%"] = _pct(df, "IncurredClaimRatio")
    m["expense_ratio_%"] = _pct(df, "ExpensesOfManagementRatio")
    m["combined_ratio_%"] = _pct(df, "CombinedRatio")
    m["retention_%"] = _pct(df, "NetRetentionRatio")
    m["underwriting_margin_%"] = 100 * uw / earned
    m["investment_share_of_pbt_%"] = 100 * inv / pbt
    m["solvency_x"] = _s(df, "SolvencyRatio")
    m["pat_yoy_%"] = _yoy(pat, lag)
    if not quarterly:
        m["roe_%"] = 100 * pat / _avg(eq)
    return m.replace([np.inf, -np.inf], np.nan)


def metrics(kind: str, df: pd.DataFrame, *, quarterly: bool = False) -> pd.DataFrame:
    return (life_metrics if kind == "life" else general_metrics)(df, quarterly=quarterly)


def _latest(frames: tuple[pd.DataFrame | None, ...], col: str) -> float | None:
    """Latest non-NaN ``col`` from the first frame that has it (pass quarterly first — fresher)."""
    for f in frames:
        if f is not None and not f.empty and col in f:
            s = f[col].dropna()
            if len(s):
                return float(s.iloc[-1])
    return None


def health_checks(kind: str, am: pd.DataFrame, qm: pd.DataFrame) -> list[tuple[str, str]]:
    """Plain-English checks on an insurer → ``[(status, text)]`` (``ok`` / ``warn`` / ``alarm``).
    Only checks with data are emitted. The insurer stand-in for industrial forensics."""
    c, out = config, []

    sol = _latest((qm, am), "solvency_x")
    if sol is not None:
        if sol < c.INS_SOLVENCY_FLOOR:
            out.append(("alarm", f"Solvency {sol:.2f}x — BELOW IRDAI's {c.INS_SOLVENCY_FLOOR:.1f}x "
                                 "minimum: the regulator can restrict new business until capital is raised."))
        elif sol < c.INS_SOLVENCY_WARN:
            out.append(("warn", f"Solvency {sol:.2f}x — thin headroom over the {c.INS_SOLVENCY_FLOOR:.1f}x "
                                "floor; fast growth may need fresh capital (dilution risk)."))
        else:
            out.append(("ok", f"Solvency {sol:.2f}x — comfortably above IRDAI's "
                              f"{c.INS_SOLVENCY_FLOOR:.1f}x floor."))

    if kind == "life":
        p13 = _latest((qm, am), "persistency_13m_%")
        if p13 is not None:
            if p13 < c.INS_PERSIST13_WARN:
                out.append(("warn", f"13th-month persistency {p13:.0f}% — ~{100 - p13:.0f}% of new "
                                    "policies lapse in year one: a sign of mis-selling or weak products."))
            elif p13 >= c.INS_PERSIST13_STRONG:
                out.append(("ok", f"13th-month persistency {p13:.0f}% — strong; what's sold stays sold."))
            else:
                out.append(("ok", f"13th-month persistency {p13:.0f}% — healthy."))
        p61 = _latest((qm, am), "persistency_61m_%")
        if p61 is not None:
            if p61 < c.INS_PERSIST61_WARN:
                out.append(("warn", f"61st-month persistency {p61:.0f}% — most policies don't reach "
                                    "year five, so long-term profit per policy is at risk."))
            else:
                out.append(("ok", f"61st-month persistency {p61:.0f}% — long-term book holding up."))
        ape = _latest((am,), "ape_yoy_%")
        if ape is not None:
            if ape < 0:
                out.append(("warn", f"New business (APE) shrank {abs(ape):.0f}% last year — the "
                                    "growth engine has stalled."))
            else:
                out.append(("ok", f"New business (APE) grew {ape:.0f}% last year."))
    else:
        cr = _latest((qm, am), "combined_ratio_%")
        if cr is not None:
            if cr > c.INS_COMBINED_ALARM:
                out.append(("alarm", f"Combined ratio {cr:.0f}% — heavy underwriting losses; profit "
                                     "leans entirely on investment income."))
            elif cr > c.INS_COMBINED_WARN:
                out.append(("warn", f"Combined ratio {cr:.0f}% — underwriting losses above the "
                                    "~100–110% Indian norm; watch pricing and claims."))
            elif cr <= 100:
                out.append(("ok", f"Combined ratio {cr:.0f}% — an underwriting profit (rare, and "
                                  "a sign of pricing discipline)."))
            else:
                out.append(("ok", f"Combined ratio {cr:.0f}% — a small underwriting loss, typical "
                                  "for Indian general insurers; investment income makes the profit."))
        g = qm["claims_ratio_%"].dropna() if qm is not None and "claims_ratio_%" in qm else pd.Series(dtype=float)
        if len(g) >= 3 and g.iloc[-1] > g.iloc[-2] > g.iloc[-3]:
            path = " → ".join(f"{v:.0f}%" for v in g.iloc[-3:])
            out.append(("warn", f"Claims ratio has risen 3 quarters running ({path}) — claims are "
                                "outpacing pricing."))
        uw, inv = _latest((am,), "underwriting_cr"), _latest((am,), "investment_income_cr")
        if uw is not None and inv is not None and uw < 0:
            if inv > -uw:
                out.append(("ok", f"Investment income (₹{inv:,.0f} cr) covers the underwriting loss "
                                  f"(₹{-uw:,.0f} cr)."))
            else:
                out.append(("warn", f"Investment income (₹{inv:,.0f} cr) no longer covers the "
                                    f"underwriting loss (₹{-uw:,.0f} cr)."))

    roe = _latest((am,), "roe_%")
    if roe is not None:
        if roe < c.INS_ROE_WEAK:
            out.append(("warn", f"ROE {roe:.1f}% — weak return on shareholders' capital."))
        else:
            out.append(("ok", f"ROE {roe:.1f}%."))
    return out
