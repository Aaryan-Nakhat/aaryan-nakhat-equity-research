"""Deep fundamental + forensic brief — full statements and every derived ratio.

Multi-year Income Statement / Balance Sheet / Cash Flow (CFO·CFI·CFF) from the
XBRL `financials` table, plus a comprehensive derived layer: margins, returns
(ROE/ROCE/ROIC/ROA), leverage, liquidity, working-capital / cash-conversion,
FCF / FCFF / FCFE, CFO-quality (CFO/PAT, CFO/EBITDA) including 3- and 5-year
rolled figures, and the forensic scores with full component breakdowns.

History depth is data-bound (see docs/FUNDAMENTALS.md): P&L runs ~6 years; the
balance sheet and cash flow are present FY2023+ (older result XBRLs omit them).
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import forensic, fundamentals, quant, sector, technical, valuation
from equity_research.analysis.fundamentals import load_annual
from equity_research.reports import glossary

CR = 1e7


def _f(v, nd=0, pct=False, x=False, lo=None, hi=None):
    """Format a number; ``n/a`` for missing/NaN/inf or values outside the plausible
    [lo, hi] band (a data artifact — e.g. a holding-co 1,000% net margin, a ratio
    blown up by near-zero equity). Bounds are only applied where passed."""
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "n/a"
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}{'x' if x else ''}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _cover(ebit, fin) -> str:
    """Interest coverage, capped — a near-zero finance cost (debt-free) otherwise
    shows a meaningless huge multiple (e.g. 21,948x)."""
    if ebit is None or fin is None or pd.isna(ebit) or pd.isna(fin) or fin == 0:
        return "n/a"
    v = ebit / fin
    return ">500x" if v > 500 else f"{v:,.1f}x"


def build_deep_brief(con: duckdb.DuckDBPyConnection, symbol: str, *,
                     consolidated: bool = False, target_shares: float | None = None) -> str:
    af = load_annual(con, symbol, consolidated)        # index=year-end, cols=elements (₹)
    label = "consolidated" if consolidated else "standalone"
    L = [f"# {symbol} — deep fundamental & forensic brief ({label})\n",
         f"_Report generated {date.today():%d-%b-%Y}. All figures ₹ crore unless "
         "noted. History depth is data-bound: P&L is multi-year; balance sheet & "
         "cash flow are present only for years where the result XBRL carried them "
         "(typically FY2023+)._\n"]
    if af.empty:
        return "\n".join(L) + "\nNo annual financials ingested for this symbol."

    def s(el: str) -> pd.Series:
        return af[el] if el in af.columns else pd.Series(np.nan, index=af.index)

    yrs = list(af.index)

    def cells(series, nd=0, pct=False, x=False, div=CR):
        return [_f(None if pd.isna(series.get(y)) else series.get(y) / div, nd, pct, x)
                for y in years]

    # ---- raw series (₹) ----
    rev, oi, inc = s("RevenueFromOperations"), s("OtherIncome"), s("Income")
    cogs = (s("CostOfMaterialsConsumed").fillna(0) + s("PurchasesOfStockInTrade").fillna(0)
            + s("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade").fillna(0))
    cogs = cogs.where(s("CostOfMaterialsConsumed").notna())
    emp, fin, dep = s("EmployeeBenefitExpense"), s("FinanceCosts"), s("DepreciationDepletionAndAmortisationExpense")
    oexp, texp = s("OtherExpenses"), s("Expenses")
    pbeit, exc = s("ProfitBeforeExceptionalItemsAndTax"), s("ExceptionalItemsBeforeTax")
    pbt, ctax, dtax, tax = s("ProfitBeforeTax"), s("CurrentTax"), s("DeferredTax"), s("TaxExpense")
    pat, ci = s("ProfitLossForPeriod"), s("ComprehensiveIncomeForThePeriod")
    ebit, ebitda = pbt + fin, pbt + fin + dep
    gp = rev - cogs

    assets, ca, nca = s("Assets"), s("CurrentAssets"), s("NoncurrentAssets")
    ppe, inv, recv = s("PropertyPlantAndEquipment"), s("Inventories"), s("TradeReceivablesCurrent")
    cash = s("CashAndCashEquivalents")
    eq, shcap, oeq = s("Equity"), s("EquityShareCapital"), s("OtherEquity")
    liab, cl, ncl = s("Liabilities"), s("CurrentLiabilities"), s("NoncurrentLiabilities")
    debt_c, debt_nc = s("BorrowingsCurrent"), s("BorrowingsNoncurrent")
    payables = s("TradePayablesCurrent")
    debt = debt_c.add(debt_nc, fill_value=0).where(debt_c.notna() | debt_nc.notna())
    netdebt = debt - cash

    cfo, cfi, cff = (s("CashFlowsFromUsedInOperatingActivities"),
                     s("CashFlowsFromUsedInInvestingActivities"),
                     s("CashFlowsFromUsedInFinancingActivities"))
    capex = s("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities").abs()
    borrow_in = s("ProceedsFromBorrowingsClassifiedAsFinancingActivities")
    borrow_out = s("RepaymentsOfBorrowingsClassifiedAsFinancingActivities")
    net_borrow = borrow_in.fillna(0) - borrow_out.fillna(0)
    div_paid = s("DividendsPaidClassifiedAsFinancingActivities").abs()
    tax_rate = tax / pbt
    fcf = cfo - capex
    fcff = cfo - capex + fin * (1 - tax_rate)
    fcfe = cfo - capex + net_borrow

    # ---- trailing-12-month (TTM) P&L column (last 4 consecutive quarters) ----
    tpl = fundamentals.ttm_pl(con, symbol, consolidated)

    def tg(el):                       # TTM scalar for an element (₹); NaN if absent
        v = tpl.get(el)
        return float(v) if v is not None and not pd.isna(v) else np.nan

    t_rev, t_oi, t_inc = tg("RevenueFromOperations"), tg("OtherIncome"), tg("Income")
    has_ttm = not tpl.empty and not pd.isna(t_rev)
    t_cm = tg("CostOfMaterialsConsumed")
    t_cogs = (np.nan if pd.isna(t_cm) else
              np.nansum([t_cm, tg("PurchasesOfStockInTrade"),
                         tg("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade")]))
    t_emp, t_fin = tg("EmployeeBenefitExpense"), tg("FinanceCosts")
    t_dep, t_oexp, t_texp = tg("DepreciationDepletionAndAmortisationExpense"), tg("OtherExpenses"), tg("Expenses")
    t_pbeit, t_exc = tg("ProfitBeforeExceptionalItemsAndTax"), tg("ExceptionalItemsBeforeTax")
    t_pbt, t_ctax, t_dtax, t_tax = tg("ProfitBeforeTax"), tg("CurrentTax"), tg("DeferredTax"), tg("TaxExpense")
    t_pat, t_ci = tg("ProfitLossForPeriod"), tg("ComprehensiveIncomeForThePeriod")
    t_ebit, t_ebitda, t_gp = t_pbt + t_fin, t_pbt + t_fin + t_dep, t_rev - t_cogs

    def _rt(num, den, mul=100.0):     # safe ratio (no div-by-zero / NaN warnings)
        return mul * num / den if (den == den and den) else np.nan

    # ===================== INCOME STATEMENT =====================
    years = [y for y in yrs if not pd.isna(rev.get(y))]
    hdr = ["Income statement"] + [f"FY{y.year}" for y in years] + (["TTM"] if has_ttm else [])
    rows1 = [
        ["Revenue from operations"] + cells(rev),
        ["Other income"] + cells(oi),
        ["Total income"] + cells(inc),
        ["COGS (materials+purchases+Δinv)"] + cells(cogs),
        ["Employee benefit expense"] + cells(emp),
        ["Finance costs"] + cells(fin),
        ["Depreciation & amortisation"] + cells(dep),
        ["Other expenses"] + cells(oexp),
        ["Total expenses"] + cells(texp),
        ["EBITDA"] + cells(ebitda),
        ["EBIT"] + cells(ebit),
        ["Profit before excep. & tax"] + cells(pbeit),
        ["Exceptional items"] + cells(exc),
        ["Profit before tax"] + cells(pbt),
        ["  Current tax"] + cells(ctax),
        ["  Deferred tax"] + cells(dtax),
        ["Total tax"] + cells(tax),
        ["Net profit (PAT)"] + cells(pat),
        ["Comprehensive income"] + cells(ci),
    ]
    if has_ttm:
        ttm_is = [t_rev, t_oi, t_inc, t_cogs, t_emp, t_fin, t_dep, t_oexp, t_texp,
                  t_ebitda, t_ebit, t_pbeit, t_exc, t_pbt, t_ctax, t_dtax, t_tax, t_pat, t_ci]
        for row, val in zip(rows1, ttm_is):
            row.append(_f(None if pd.isna(val) else val / CR, 0))
    L += ["## 1. Income statement", _table(hdr, rows1), ""]

    # ---- margins & growth ----
    def yoy(series):
        return series / series.shift(1) - 1
    rows2 = [
        ["Gross margin"] + [_f(None if pd.isna(gp.get(y)) else 100 * gp.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["EBITDA margin"] + [_f(None if pd.isna(ebitda.get(y)) else 100 * ebitda.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["EBIT margin"] + [_f(None if pd.isna(ebit.get(y)) else 100 * ebit.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["PBT margin"] + [_f(None if pd.isna(pbt.get(y)) else 100 * pbt.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["Net margin"] + [_f(None if pd.isna(pat.get(y)) else 100 * pat.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["Effective tax rate"] + [_f(None if pd.isna(tax_rate.get(y)) else 100 * tax_rate.get(y), 1, pct=True, lo=0, hi=80) for y in years],
        ["Revenue YoY"] + [_f(None if pd.isna(yoy(rev).get(y)) else 100 * yoy(rev).get(y), 1, pct=True, lo=-100, hi=500) for y in years],
        ["PAT YoY"] + [_f(None if pd.isna(yoy(pat).get(y)) else 100 * yoy(pat).get(y), 1, pct=True, lo=-100, hi=500) for y in years],
        ["Other income / PBT"] + [_f(None if pd.isna(oi.get(y)) or pd.isna(pbt.get(y)) else 100 * oi.get(y) / pbt.get(y), 1, pct=True, lo=-200, hi=300) for y in years],
    ]
    if has_ttm:
        ttm_m = [
            _f(_rt(t_gp, t_rev), 1, pct=True, lo=-100, hi=100),
            _f(_rt(t_ebitda, t_rev), 1, pct=True, lo=-100, hi=100),
            _f(_rt(t_ebit, t_rev), 1, pct=True, lo=-100, hi=100),
            _f(_rt(t_pbt, t_rev), 1, pct=True, lo=-100, hi=100),
            _f(_rt(t_pat, t_rev), 1, pct=True, lo=-100, hi=100),
            _f(_rt(t_tax, t_pbt), 1, pct=True, lo=0, hi=80),
            "n/a",   # YoY needs the prior-year TTM (not computed)
            "n/a",
            _f(_rt(t_oi, t_pbt), 1, pct=True, lo=-200, hi=300),
        ]
        for row, val in zip(rows2, ttm_m):
            row.append(val)
    L += ["## 2. Profitability, margins & growth", _table(
        ["Metric"] + [f"FY{y.year}" for y in years] + (["TTM"] if has_ttm else []), rows2), ""]

    # ===================== BALANCE SHEET =====================
    by = [y for y in yrs if not pd.isna(assets.get(y))]
    if by:
        years = by
        hdr = ["Balance sheet"] + [f"FY{y.year}" for y in years]
        L += ["## 3. Balance sheet", _table(hdr, [
            ["Property, plant & equipment"] + cells(ppe),
            ["Non-current assets (total)"] + cells(nca),
            ["Inventories"] + cells(inv),
            ["Trade receivables (current)"] + cells(recv),
            ["Cash & equivalents"] + cells(cash),
            ["Current assets (total)"] + cells(ca),
            ["**Total assets**"] + cells(assets),
            ["Equity share capital"] + cells(shcap),
            ["Other equity (reserves)"] + cells(oeq),
            ["**Total equity**"] + cells(eq),
            ["Borrowings — non-current"] + cells(debt_nc),
            ["Borrowings — current"] + cells(debt_c),
            ["Total debt"] + cells(debt),
            ["Trade payables (current)"] + cells(payables),
            ["Current liabilities (total)"] + cells(cl),
            ["Non-current liabilities (total)"] + cells(ncl),
            ["**Total liabilities**"] + cells(liab),
            ["Net debt (debt − cash)"] + cells(netdebt),
        ]), ""]

        # returns / leverage / liquidity (balance-sheet years)
        L += ["## 4. Returns, leverage & liquidity", _table(
            ["Metric"] + [f"FY{y.year}" for y in years], [
                ["ROE (PAT/equity)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(eq.get(y)) else 100 * pat.get(y) / eq.get(y), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROCE (EBIT/(eq+debt))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) / (eq.get(y) + (debt.get(y) or 0)), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROIC (EBIT(1−t)/(eq+debt−cash))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) * (1 - (tax_rate.get(y) if not pd.isna(tax_rate.get(y)) else 0)) / (eq.get(y) + (debt.get(y) or 0) - (cash.get(y) or 0)), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROA (PAT/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(assets.get(y)) else 100 * pat.get(y) / assets.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
                ["Debt / equity"] + [_f(None if pd.isna(debt.get(y)) or pd.isna(eq.get(y)) else debt.get(y) / eq.get(y), 2, x=True, lo=0, hi=50) for y in years],
                ["Net debt / EBITDA"] + [_f(None if pd.isna(netdebt.get(y)) or pd.isna(ebitda.get(y)) else netdebt.get(y) / ebitda.get(y), 2, x=True, lo=-50, hi=50) for y in years],
                ["Interest coverage (EBIT/int)"] + [_cover(ebit.get(y), fin.get(y)) for y in years],
                ["Current ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else ca.get(y) / cl.get(y), 2, x=True, lo=0, hi=50) for y in years],
                ["Quick ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else (ca.get(y) - (inv.get(y) or 0)) / cl.get(y), 2, x=True, lo=0, hi=50) for y in years],
            ]), ""]

        # working capital / cash conversion
        L += ["## 5. Working capital & cash conversion", _table(
            ["Metric (days)"] + [f"FY{y.year}" for y in years], [
                ["Receivable days"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(rev.get(y)) else 365 * recv.get(y) / rev.get(y), 0, lo=0, hi=2000) for y in years],
                ["Inventory days"] + [_f(None if pd.isna(inv.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) else 365 * inv.get(y) / cogs.get(y), 0, lo=0, hi=2000) for y in years],
                ["Payable days"] + [_f(None if pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) else 365 * payables.get(y) / cogs.get(y), 0, lo=0, hi=2000) for y in years],
                ["Cash conversion cycle"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(inv.get(y)) or pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) or not rev.get(y) else 365 * (recv.get(y) / rev.get(y) + inv.get(y) / cogs.get(y) - payables.get(y) / cogs.get(y)), 0, lo=-1000, hi=2000) for y in years],
                ["Asset turnover (Rev/assets)"] + [_f(None if pd.isna(rev.get(y)) or pd.isna(assets.get(y)) else rev.get(y) / assets.get(y), 2, x=True, lo=0, hi=20) for y in years],
            ]), ""]

    # ===================== CASH FLOW =====================
    cy = [y for y in yrs if not pd.isna(cfo.get(y))]
    if cy:
        years = cy
        hdr = ["Cash flow"] + [f"FY{y.year}" for y in years]
        L += ["## 6. Cash flow statement", _table(hdr, [
            ["CFO — operating"] + cells(cfo),
            ["CFI — investing"] + cells(cfi),
            ["CFF — financing"] + cells(cff),
            ["  Capex (PP&E purchase)"] + cells(capex),
            ["  Borrowings raised"] + cells(borrow_in),
            ["  Borrowings repaid"] + cells(borrow_out),
            ["  Dividends paid"] + cells(div_paid),
            ["Net change in cash"] + cells(s("IncreaseDecreaseInCashAndCashEquivalents")),
        ]), ""]

        # free cash flow & cash quality
        L += ["## 7. Free cash flow & earnings quality", _table(
            ["Metric"] + [f"FY{y.year}" for y in years], [
                ["FCF (CFO−Capex)"] + cells(fcf),
                ["FCFF (CFO−Capex+Int(1−t))"] + cells(fcff),
                ["FCFE (CFO−Capex+NetBorrow)"] + cells(fcfe),
                ["CFO / PAT"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(pat.get(y)) else cfo.get(y) / pat.get(y), 2, x=True, lo=-50, hi=50) for y in years],
                ["CFO / EBITDA"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(ebitda.get(y)) else 100 * cfo.get(y) / ebitda.get(y), 0, pct=True, lo=-100, hi=200) for y in years],
                ["Accruals ((PAT−CFO)/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(cfo.get(y)) or pd.isna(assets.get(y)) else 100 * (pat.get(y) - cfo.get(y)) / assets.get(y), 1, pct=True, lo=-150, hi=150) for y in years],
            ]), ""]

        # rolling CFO quality
        v = pd.DataFrame({"cfo": cfo, "pat": pat, "ebitda": ebitda}).dropna(subset=["cfo"]).sort_index()
        def roll_ratio(num, den, n):
            if len(v) < n:
                return None
            t = v.tail(n)
            return t[num].sum() / t[den].sum() if t[den].sum() else None
        L += ["**Rolled cash quality (most recent window):**",
              f"- 3-yr CFO/PAT: {_f(roll_ratio('cfo','pat',3), 2, x=True, lo=-50, hi=50)} · "
              f"5-yr CFO/PAT: {_f(roll_ratio('cfo','pat',5), 2, x=True, lo=-50, hi=50)}",
              f"- 3-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',3) is None else 100*roll_ratio('cfo','ebitda',3), 0, pct=True, lo=-100, hi=200)} · "
              f"5-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',5) is None else 100*roll_ratio('cfo','ebitda',5), 0, pct=True, lo=-100, hi=200)}", ""]

    # ===================== QUARTERLY MOMENTUM =====================
    qm = fundamentals.quarterly_metrics(con, symbol, consolidated)
    if not qm.empty:
        q = qm.tail(8)
        hdr = ["Quarter"] + [str(i.date()) for i in q.index]
        L += [f"## 8. Quarterly P&L trend (last {len(q)}q)", _table(hdr, [
            ["Revenue (₹cr)"] + [_f(x, 0) for x in q["revenue_cr"]],
            ["Net profit (₹cr)"] + [_f(x, 0) for x in q["net_profit_cr"]],
            ["Net margin"] + [_f(x, 1, pct=True, lo=-100, hi=100) for x in q["net_margin_%"]],
            ["EBITDA margin"] + [_f(x, 1, pct=True, lo=-100, hi=100) for x in q["ebitda_margin_%"]],
            ["Rev YoY"] + [_f(x, 1, pct=True, lo=-100, hi=500) for x in q["rev_yoy_%"]],
            ["PAT YoY"] + [_f(x, 1, pct=True, lo=-100, hi=500) for x in q["net_yoy_%"]],
            ["Interest cover"] + [_f(x, 1, x=True, lo=-50, hi=500) for x in q["interest_cover_x"]],
        ]), ""]

    # ===================== FORENSIC DEEP DIVE =====================
    mcap = valuation.market_cap(con, symbol, consolidated, shares_override=target_shares)
    z = forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap)
    fsc = forensic.piotroski_f(con, symbol, consolidated=consolidated)
    m = forensic.beneish_m(con, symbol, consolidated=consolidated)
    acc = forensic.accruals(con, symbol, consolidated=consolidated)
    p = con.execute(
        "SELECT period_end, promoter_holding_pct, pledged_pct_of_promoter, pledged_pct_of_total "
        "FROM shareholding WHERE symbol = ? ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
    zband = ("n/a" if z.value is None else "safe" if z.value > 2.99
             else "distress" if z.value < 1.81 else "grey zone")
    fband = ("n/a" if fsc.value is None else "strong" if fsc.value >= 8
             else "weak" if fsc.value <= 2 else "middling")
    mflag = ("n/a" if m.value is None else
             "⚠ above −1.78 (possible manipulation)" if m.value > -1.78 else "clean (≤ −1.78)")
    # corroborate a Beneish flag against the harder cash/accrual evidence — a sharp
    # margin recovery can trip the statistical screen without any real manipulation.
    cp = (cfo / pat).replace([np.inf, -np.inf], np.nan).dropna()
    cfo_pat_latest = float(cp.iloc[-1]) if len(cp) else None
    beneish_fp = (m.value is not None and m.value > -1.78
                  and acc.value is not None and acc.value <= 10
                  and cfo_pat_latest is not None and cfo_pat_latest >= 1.0)
    mcaveat = (" — but Sloan accruals and cash conversion look clean, so likely a statistical "
               "false positive from a sharp margin recovery" if beneish_fp else "")
    L += ["## 9. Forensic deep-dive", ""]
    L.append(f"- **Altman Z = {_f(z.value, 2)} — {zband}.** Bankruptcy-distance score "
             "(>2.99 safe · 1.81–2.99 grey · <1.81 distress); calibrated for manufacturers, so "
             "asset-heavy giants can read low." + (f" _{z.note}_" if z.note else ""))
    if fsc.value is not None and fsc.components:
        passed = [k for k, v in fsc.components.items() if v]
        failed = [k for k, v in fsc.components.items() if not v]
        L.append(f"- **Piotroski F = {_f(fsc.value, 0)}/9 — {fband}.** 9-point fundamental-strength "
                 f"checklist. Passed: {', '.join(passed) or 'none'}. Failed: {', '.join(failed) or 'none'}.")
    else:
        L.append(f"- **Piotroski F:** n/a (missing {fsc.missing}).")
    L.append(f"- **Beneish M = {_f(m.value, 2)} — {mflag}{mcaveat}.** Statistical earnings-manipulation "
             "screen (a flag to dig, not proof — corroborate with accruals/receivables/cash).")
    if acc.value is not None:
        L.append(f"- **Sloan accruals = {_f(acc.value, 1, pct=True)} of avg assets — "
                 f"{glossary.label('Sloan accruals%', acc.value) or 'n/a'}.** Non-cash part of "
                 f"earnings (cash-flow accruals {_f(acc.components.get('cashflow_accruals_%'), 1, pct=True)}); "
                 "near-zero/negative = earnings cash-backed, high positive = aggressive.")
    if p and p[2] is not None:
        L.append(f"- **Promoter pledge (as of {p[0]:%d-%b-%Y}):** promoter holds "
                 f"{_f(p[1], 1, pct=True)}; **{_f(p[2], 1, pct=True)} of that is pledged** "
                 f"({glossary.label('Pledge%', p[2]) or 'n/a'}) — 0% ideal, >50% a serious red flag.")
    elif p:
        L.append(f"- **Promoter pledge (as of {p[0]:%d-%b-%Y}):** promoter holds "
                 f"{_f(p[1], 1, pct=True)} — no significant promoter; {_f(p[3], 1, pct=True)} of "
                 "total shares encumbered.")
    else:
        L.append("- **Promoter pledge:** n/a (no shareholding snapshot).")
    L.append("- **Contingent liabilities / related-party transactions:** read from the company's "
             "filings (see the Analysis section); not in the structured XBRL.")
    L.append("")

    # ===================== VALUATION + TECHNICAL (summary) =====================
    snap = valuation.snapshot(con, symbol, consolidated, shares_override=target_shares)
    hist = valuation.valuation_history(con, symbol, consolidated)
    sec = sector.sector_valuation(con, symbol, consolidated, target_shares_override=target_shares)
    L += ["## 10. Valuation"]
    if snap:
        L.append(f"- Market cap ₹{_f(snap.get('market_cap_cr'),0)} cr · P/E(TTM) "
                 f"{_f(snap.get('pe_ttm'),1,lo=0,hi=2000)} · P/B {_f(snap.get('pb'),2,lo=0,hi=200)} · "
                 f"earnings yield {_f(snap.get('earnings_yield_%'),2,pct=True,lo=-50,hi=50)}")
        if snap.get("note"):
            L.append(f"  - ⚠ {snap['note']}")
    if not hist.empty and "pe" in hist:
        pes = hist["pe"].dropna()
        pes = pes[(pes > 0) & (pes < 2000)]                 # positive, sane P/Es only
        if len(pes):
            L.append(f"- Own P/E history: min {_f(pes.min(),1)} / median "
                     f"{_f(float(pes.median()),1)} / max {_f(pes.max(),1)}")
    if sec.get("peers_with_data", 0) >= 3:
        L.append(f"- Sector ({sec['industry']}): P/E vs median {_f(sec.get('sector_median_pe'),1)} — "
                 f"cheaper than {_f(sec.get('pe_cheaper_than_%_of_peers'),0)}% of {sec['peers_with_data']} peers")
    elif sec.get("industry"):
        n_peers = sec.get("peers_with_data", 0)
        L.append(f"- Sector ({sec['industry']}): insufficient peer data "
                 f"({n_peers} peer{'s' if n_peers != 1 else ''} with comparable P/E) — "
                 "sector percentile omitted; see the peer table below")

    # peer comparison table (target ◄ vs sector peers that have data)
    pcols = ["P/E", "P/B", "ROE%", "ROCE%", "NetMargin%", "D/E"]
    prows = []
    for ps in [symbol, *sector.peers(con, symbol)]:
        r = quant._ratios(con, ps, consolidated)
        if not r:
            continue
        prows.append([ps + (" ◄" if ps == symbol else "")] + [_f(r.get(c), 1) for c in pcols])
        if len(prows) >= 8:
            break
    if len(prows) >= 2:
        # blank lines around the table so the following "## 11." renders as a heading
        # (a table glued straight to a heading makes the renderer swallow the '##')
        L += ["", "### Peer comparison", "", _table(["Company"] + pcols, prows), ""]

    # ===================== QUANT VALUATION (Monte-Carlo DCF) =====================
    inp = quant.dcf_inputs(con, symbol, consolidated, shares_override=target_shares)
    L += ["## 11. Quant valuation (Monte-Carlo DCF)"]
    if inp.is_financial:
        L.append("- FCFF-DCF is not meaningful for a lender/financial; skipped."
                 + (f" {inp.note}" if inp.note else ""))
    elif not inp.usable:
        L.append(f"- DCF inputs unavailable: {', '.join(inp.missing) or inp.note or 'n/a'}.")
    else:
        mc = quant.monte_carlo_dcf(inp)
        rev = quant.reverse_dcf(inp)
        sc = quant.scenario_dcf(inp)
        L.append(f"- Drivers: revenue ₹{_f(inp.rev0 / CR, 0)} cr · growth "
                 f"{_f(100 * inp.growth, 1, pct=True)} (σ {_f(100 * inp.growth_sigma, 1)}) · "
                 f"EBIT margin {_f(100 * inp.ebit_margin, 1, pct=True)} · WACC "
                 f"{_f(100 * inp.wacc, 1, pct=True)} (β {_f(inp.beta, 2)}) · terminal g "
                 f"{_f(100 * inp.terminal_growth, 1, pct=True)} · net debt ₹{_f(inp.net_debt / CR, 0)} cr")
        if mc.median and mc.price:
            if mc.price <= mc.median:
                mos = ("margin of safety "
                       + glossary.read("Margin of safety%", 100 * (mc.median - mc.price) / mc.median,
                                       nd=0, pct=True))
            else:
                mos = f"price is {_f(mc.price / mc.median, 1)}x the DCF median (no margin of safety)"
            L.append(f"- **Intrinsic value/share ₹{_f(mc.median, 0)} (median)**, p10–p90 "
                     f"₹{_f(mc.p10, 0)}–{_f(mc.p90, 0)}; price ₹{_f(mc.price, 0)} → {mos}; "
                     f"P(undervalued) {glossary.read('P(undervalued)%', 100 * mc.prob_undervalued, nd=0, pct=True)}.")
        if sc.get("meaningful"):
            L.append(f"- Scenario fair value — bear ₹{_f(sc.get('bear'), 0, lo=0, hi=1_000_000)} · base "
                     f"₹{_f(sc.get('base'), 0, lo=0, hi=1_000_000)} · bull "
                     f"₹{_f(sc.get('bull'), 0, lo=0, hi=1_000_000)}.")
        else:
            L.append("- Scenario fair value — not meaningful for this company "
                     "(high-beta / cyclical / capex-heavy inputs drive the modelled FCFF negative); "
                     "rely on the reverse-DCF and the relative valuation in §10 instead.")
        if rev.get("implied_growth") is not None:
            L.append(f"- Reverse-DCF: today's price implies ~{_f(100 * rev['implied_growth'], 1, pct=True)} "
                     f"revenue growth (vs {_f(100 * (rev.get('historical_growth') or 0), 1, pct=True)} "
                     f"historical) — {'plausible' if rev.get('plausible') else 'demanding'}.")
        elif rev.get("note"):
            L.append(f"- Reverse-DCF: {rev['note']}.")
        if inp.note:
            L.append(f"- _{inp.note.strip()}_")
    L.append("- _DCF is assumption-driven — read the distribution/range, not a point estimate._")
    L.append("")

    # ===================== STATISTICAL FORENSICS =====================
    L += ["## 12. Statistical forensics"]
    bf = quant.benford(con, symbol)
    if bf.get("mad") is not None:
        L.append(f"- Benford first-digit conformity: MAD {_f(bf['mad'], 4)} → **{bf['verdict']}** "
                 f"(n={bf['n']})" + (" — ⚠ possible manipulation/rounding" if bf.get("flag") else "") + ".")
    else:
        L.append(f"- Benford: {bf.get('note', 'n/a')}.")
    zs = quant.sector_zscores(con, symbol, consolidated)
    if zs.get("ratios"):
        rows = [[k, _f(v["value"], 2), _f(v["peer_mean"], 2), _f(v["z"], 2)]
                for k, v in zs["ratios"].items()]
        L += [f"- Sector-relative z-scores ({zs.get('industry', '?')}, vs {len(rows)} ratios over peers):",
              _table(["Ratio", "Value", "Peer mean", "z"], rows),
              "  _z = standard deviations from the peer mean: |z|<1 in line with peers, >2 an "
              "outlier. High z is **good** for ROE/ROCE/margins, **expensive** for P/E·P/B, "
              "**more levered** for D/E._"]
    else:
        L.append(f"- Sector z-scores: {zs.get('note', 'n/a')}.")
    L.append("")

    ts = technical.snapshot(con, symbol)
    if ts:
        L += ["## 13. Technical snapshot",
              f"- Close ₹{_f(ts['close'],2)} · SMA20/50/200 {_f(ts['sma20'],0)}/{_f(ts['sma50'],0)}/{_f(ts['sma200'],0)} · "
              f"RSI {_f(ts['rsi14'],0)} · {_f(ts['pct_from_52w_high'],1,pct=True)} from 52w high",
              f"- Signals: {', '.join(ts['signals'])}"]

    L += ["", "## 14. Notes",
          "- **Order book / backlog** is not in the structured XBRL filings and is "
          "only relevant to order-driven businesses (EPC / capital goods / IT services); "
          "n/a for this company type. It would need separate extraction from the annual "
          "report / investor presentation (a Phase-4 PDF read).",
          f"- Statements are {label}; pass the consolidated flag for group-level figures.",
          "- COGS, EBITDA and FCFF/FCFE use documented approximations "
          "(COGS=materials+purchases+Δinv; EBITDA=PBT+interest+depreciation; "
          "FCFF adds back after-tax interest; FCFE adds net borrowing)."]
    return "\n".join(L)
