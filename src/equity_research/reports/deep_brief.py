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

from equity_research.analysis import forensic, fundamentals, sector, technical, valuation
from equity_research.analysis.fundamentals import load_annual

CR = 1e7


def _f(v, nd=0, pct=False, x=False):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}{'x' if x else ''}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


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

    # ===================== INCOME STATEMENT =====================
    years = [y for y in yrs if not pd.isna(rev.get(y))]
    hdr = ["Income statement"] + [f"FY{y.year}" for y in years]
    L += ["## 1. Income statement", _table(hdr, [
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
    ]), ""]

    # ---- margins & growth ----
    def yoy(series):
        return series / series.shift(1) - 1
    L += ["## 2. Profitability, margins & growth", _table(
        ["Metric"] + [f"FY{y.year}" for y in years], [
            ["Gross margin"] + [_f(None if pd.isna(gp.get(y)) else 100 * gp.get(y) / rev.get(y), 1, pct=True) for y in years],
            ["EBITDA margin"] + [_f(None if pd.isna(ebitda.get(y)) else 100 * ebitda.get(y) / rev.get(y), 1, pct=True) for y in years],
            ["EBIT margin"] + [_f(None if pd.isna(ebit.get(y)) else 100 * ebit.get(y) / rev.get(y), 1, pct=True) for y in years],
            ["PBT margin"] + [_f(None if pd.isna(pbt.get(y)) else 100 * pbt.get(y) / rev.get(y), 1, pct=True) for y in years],
            ["Net margin"] + [_f(None if pd.isna(pat.get(y)) else 100 * pat.get(y) / rev.get(y), 1, pct=True) for y in years],
            ["Effective tax rate"] + [_f(None if pd.isna(tax_rate.get(y)) else 100 * tax_rate.get(y), 1, pct=True) for y in years],
            ["Revenue YoY"] + [_f(None if pd.isna(yoy(rev).get(y)) else 100 * yoy(rev).get(y), 1, pct=True) for y in years],
            ["PAT YoY"] + [_f(None if pd.isna(yoy(pat).get(y)) else 100 * yoy(pat).get(y), 1, pct=True) for y in years],
            ["Other income / PBT"] + [_f(None if pd.isna(oi.get(y)) or pd.isna(pbt.get(y)) else 100 * oi.get(y) / pbt.get(y), 1, pct=True) for y in years],
        ]), ""]

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
                ["ROE (PAT/equity)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(eq.get(y)) else 100 * pat.get(y) / eq.get(y), 1, pct=True) for y in years],
                ["ROCE (EBIT/(eq+debt))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) / (eq.get(y) + (debt.get(y) or 0)), 1, pct=True) for y in years],
                ["ROIC (EBIT(1−t)/(eq+debt−cash))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) * (1 - (tax_rate.get(y) if not pd.isna(tax_rate.get(y)) else 0)) / (eq.get(y) + (debt.get(y) or 0) - (cash.get(y) or 0)), 1, pct=True) for y in years],
                ["ROA (PAT/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(assets.get(y)) else 100 * pat.get(y) / assets.get(y), 1, pct=True) for y in years],
                ["Debt / equity"] + [_f(None if pd.isna(debt.get(y)) or pd.isna(eq.get(y)) else debt.get(y) / eq.get(y), 2, x=True) for y in years],
                ["Net debt / EBITDA"] + [_f(None if pd.isna(netdebt.get(y)) or pd.isna(ebitda.get(y)) else netdebt.get(y) / ebitda.get(y), 2, x=True) for y in years],
                ["Interest coverage (EBIT/int)"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(fin.get(y)) else ebit.get(y) / fin.get(y), 1, x=True) for y in years],
                ["Current ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else ca.get(y) / cl.get(y), 2, x=True) for y in years],
                ["Quick ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else (ca.get(y) - (inv.get(y) or 0)) / cl.get(y), 2, x=True) for y in years],
            ]), ""]

        # working capital / cash conversion
        L += ["## 5. Working capital & cash conversion", _table(
            ["Metric (days)"] + [f"FY{y.year}" for y in years], [
                ["Receivable days"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(rev.get(y)) else 365 * recv.get(y) / rev.get(y), 0) for y in years],
                ["Inventory days"] + [_f(None if pd.isna(inv.get(y)) or pd.isna(cogs.get(y)) else 365 * inv.get(y) / cogs.get(y), 0) for y in years],
                ["Payable days"] + [_f(None if pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) else 365 * payables.get(y) / cogs.get(y), 0) for y in years],
                ["Cash conversion cycle"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(inv.get(y)) or pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) else 365 * (recv.get(y) / rev.get(y) + inv.get(y) / cogs.get(y) - payables.get(y) / cogs.get(y)), 0) for y in years],
                ["Asset turnover (Rev/assets)"] + [_f(None if pd.isna(rev.get(y)) or pd.isna(assets.get(y)) else rev.get(y) / assets.get(y), 2, x=True) for y in years],
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
                ["CFO / PAT"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(pat.get(y)) else cfo.get(y) / pat.get(y), 2, x=True) for y in years],
                ["CFO / EBITDA"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(ebitda.get(y)) else 100 * cfo.get(y) / ebitda.get(y), 0, pct=True) for y in years],
                ["Accruals ((PAT−CFO)/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(cfo.get(y)) or pd.isna(assets.get(y)) else 100 * (pat.get(y) - cfo.get(y)) / assets.get(y), 1, pct=True) for y in years],
            ]), ""]

        # rolling CFO quality
        v = pd.DataFrame({"cfo": cfo, "pat": pat, "ebitda": ebitda}).dropna(subset=["cfo"]).sort_index()
        def roll_ratio(num, den, n):
            if len(v) < n:
                return None
            t = v.tail(n)
            return t[num].sum() / t[den].sum() if t[den].sum() else None
        L += ["**Rolled cash quality (most recent window):**",
              f"- 3-yr CFO/PAT: {_f(roll_ratio('cfo','pat',3), 2, x=True)} · "
              f"5-yr CFO/PAT: {_f(roll_ratio('cfo','pat',5), 2, x=True)}",
              f"- 3-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',3) is None else 100*roll_ratio('cfo','ebitda',3), 0, pct=True)} · "
              f"5-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',5) is None else 100*roll_ratio('cfo','ebitda',5), 0, pct=True)}", ""]

    # ===================== QUARTERLY MOMENTUM =====================
    qm = fundamentals.quarterly_metrics(con, symbol, consolidated)
    if not qm.empty:
        q = qm.tail(8)
        hdr = ["Quarter"] + [str(i.date()) for i in q.index]
        L += ["## 8. Quarterly P&L trend (last 8q)", _table(hdr, [
            ["Revenue (₹cr)"] + [_f(x, 0) for x in q["revenue_cr"]],
            ["Net profit (₹cr)"] + [_f(x, 0) for x in q["net_profit_cr"]],
            ["Net margin"] + [_f(x, 1, pct=True) for x in q["net_margin_%"]],
            ["EBITDA margin"] + [_f(x, 1, pct=True) for x in q["ebitda_margin_%"]],
            ["Rev YoY"] + [_f(x, 1, pct=True) for x in q["rev_yoy_%"]],
            ["PAT YoY"] + [_f(x, 1, pct=True) for x in q["net_yoy_%"]],
            ["Interest cover"] + [_f(x, 1, x=True) for x in q["interest_cover_x"]],
        ]), ""]

    # ===================== FORENSIC DEEP DIVE =====================
    mcap = valuation.market_cap(con, symbol, consolidated, shares_override=target_shares)
    z = forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap)
    fsc = forensic.piotroski_f(con, symbol, consolidated=consolidated)
    m = forensic.beneish_m(con, symbol, consolidated=consolidated)
    L += ["## 9. Forensic deep-dive"]
    L.append(f"**Altman Z = {_f(z.value,2)}** (>2.99 safe · 1.81–2.99 grey · <1.81 distress)"
             + (f" — {z.note}" if z.note else ""))
    if z.components:
        L.append("  - components: " + ", ".join(f"{k}={_f(v,3)}" for k, v in z.components.items()))
    L.append(f"**Piotroski F = {_f(fsc.value,0)}/9** (8–9 strong · 0–2 weak)"
             + (f" — missing {fsc.missing}" if fsc.missing else ""))
    if fsc.components:
        passed = [k for k, v in fsc.components.items() if v]
        L.append(f"  - passed ({len(passed)}/9): {', '.join(passed) or 'none'}")
        L.append(f"  - failed: {', '.join(k for k,v in fsc.components.items() if not v) or 'none'}")
    L.append(f"**Beneish M = {_f(m.value,2)}** (> −1.78 ⇒ possible earnings manipulation)"
             + (f" — missing {m.missing}" if m.missing else ""))
    if m.components:
        L.append("  - components: " + ", ".join(f"{k}={_f(v,3)}" for k, v in m.components.items()))
        if m.note:
            L.append(f"  - note: {m.note}")
    L.append("")

    # ===================== VALUATION + TECHNICAL (summary) =====================
    snap = valuation.snapshot(con, symbol, consolidated, shares_override=target_shares)
    hist = valuation.valuation_history(con, symbol, consolidated)
    sec = sector.sector_valuation(con, symbol, consolidated, target_shares_override=target_shares)
    L += ["## 10. Valuation"]
    if snap:
        L.append(f"- Market cap ₹{_f(snap.get('market_cap_cr'),0)} cr · P/E(TTM) {_f(snap.get('pe_ttm'),1)} · "
                 f"P/B {_f(snap.get('pb'),2)} · earnings yield {_f(snap.get('earnings_yield_%'),2,pct=True)}")
        if snap.get("note"):
            L.append(f"  - ⚠ {snap['note']}")
    if not hist.empty and "pe" in hist:
        pes = hist["pe"].dropna()
        if len(pes):
            L.append(f"- Own P/E history: min {_f(pes.min(),1)} / median {_f(float(pes.median()),1)} / max {_f(pes.max(),1)}")
    if sec.get("peers_with_data"):
        L.append(f"- Sector ({sec['industry']}): P/E vs median {_f(sec.get('sector_median_pe'),1)} — "
                 f"cheaper than {_f(sec.get('pe_cheaper_than_%_of_peers'),0)}% of {sec['peers_with_data']} peers")
    ts = technical.snapshot(con, symbol)
    if ts:
        L += ["## 11. Technical snapshot",
              f"- Close ₹{_f(ts['close'],2)} · SMA20/50/200 {_f(ts['sma20'],0)}/{_f(ts['sma50'],0)}/{_f(ts['sma200'],0)} · "
              f"RSI {_f(ts['rsi14'],0)} · {_f(ts['pct_from_52w_high'],1,pct=True)} from 52w high",
              f"- Signals: {', '.join(ts['signals'])}"]

    L += ["", "## 12. Notes",
          "- **Order book / backlog** is not in the structured XBRL filings and is "
          "only relevant to order-driven businesses (EPC / capital goods / IT services); "
          "n/a for this company type. It would need separate extraction from the annual "
          "report / investor presentation (a Phase-4 PDF read).",
          f"- Statements are {label}; pass the consolidated flag for group-level figures.",
          "- COGS, EBITDA and FCFF/FCFE use documented approximations "
          "(COGS=materials+purchases+Δinv; EBITDA=PBT+interest+depreciation; "
          "FCFF adds back after-tax interest; FCFE adds net borrowing)."]
    return "\n".join(L)
