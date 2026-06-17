"""Forensic / quality scores from annual financials.

Altman Z (distress), Piotroski F (fundamental strength), Beneish M (earnings
manipulation). Each reads the annual `financials` (period_type='Y') and returns
the score, its components, and a list of **missing inputs** — a score is only
emitted when every required input is present; we never silently proxy a missing
line item with zero. See ``docs/FUNDAMENTALS.md``.

Some inputs use documented approximations (COGS, SG&A) noted at each use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from equity_research.analysis.fundamentals import load_annual


@dataclass
class Score:
    name: str
    value: float | None
    components: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    note: str = ""


def _row_getter(frame, period_end):
    """Return f(element) -> value or None for a given fiscal year-end row."""
    row = frame.loc[period_end]

    def get(element: str) -> float | None:
        if element in frame.columns:
            v = row[element]
            if v == v:           # not NaN
                return float(v)
        return None
    return get


def _need(get, names: list[str], missing: list[str]) -> dict[str, float | None]:
    out = {}
    for n in names:
        v = get(n)
        if v is None:
            missing.append(n)
        out[n] = v
    return out


def _cogs(get) -> float | None:
    """Approx COGS = materials + stock purchases + change in inventories.

    Requires CostOfMaterialsConsumed; the other two default to 0 if absent.
    """
    mat = get("CostOfMaterialsConsumed")
    if mat is None:
        return None
    return mat + (get("PurchasesOfStockInTrade") or 0.0) + \
        (get("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade") or 0.0)


def _receivables(get) -> float | None:
    cur = get("TradeReceivablesCurrent")
    if cur is None:
        return None
    return cur + (get("TradeReceivablesNoncurrent") or 0.0)


# --------------------------------------------------------------------------- #
# Altman Z (single year; needs market cap for X4, else book-equity variant).
# --------------------------------------------------------------------------- #
def altman_z(con: duckdb.DuckDBPyConnection, symbol: str, *,
             consolidated: bool = False, market_cap: float | None = None) -> Score:
    a = load_annual(con, symbol, consolidated)
    if a.empty:
        return Score("Altman Z", None, missing=["<no annual data>"])
    get = _row_getter(a, a.index[-1])
    missing: list[str] = []
    f = _need(get, ["CurrentAssets", "CurrentLiabilities", "OtherEquity",
                    "ProfitBeforeTax", "FinanceCosts", "RevenueFromOperations",
                    "Assets", "Liabilities", "Equity"], missing)
    ta = f["Assets"]
    if ta in (None, 0) or missing and ta is None:
        return Score("Altman Z", None, missing=missing, note="total assets missing")
    note = ""
    x1 = (f["CurrentAssets"] - f["CurrentLiabilities"]) / ta \
        if None not in (f["CurrentAssets"], f["CurrentLiabilities"]) else None
    x2 = f["OtherEquity"] / ta if f["OtherEquity"] is not None else None   # RE proxy
    x3 = (f["ProfitBeforeTax"] + f["FinanceCosts"]) / ta \
        if None not in (f["ProfitBeforeTax"], f["FinanceCosts"]) else None
    if market_cap is not None and f["Liabilities"]:
        x4 = market_cap / f["Liabilities"]
    elif f["Equity"] is not None and f["Liabilities"]:
        x4 = f["Equity"] / f["Liabilities"]
        note = "X4 uses book equity (no market cap supplied)"
    else:
        x4 = None
    x5 = f["RevenueFromOperations"] / ta if f["RevenueFromOperations"] is not None else None

    comps = {"X1_wc/ta": x1, "X2_re/ta": x2, "X3_ebit/ta": x3,
             "X4_eq/liab": x4, "X5_sales/ta": x5}
    if any(v is None for v in comps.values()):
        return Score("Altman Z", None, comps, missing, note or "component unavailable")
    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    return Score("Altman Z", z, comps, missing, note)


# --------------------------------------------------------------------------- #
# Piotroski F (0-9); needs current + prior year.
# --------------------------------------------------------------------------- #
def piotroski_f(con: duckdb.DuckDBPyConnection, symbol: str, *,
                consolidated: bool = False) -> Score:
    a = load_annual(con, symbol, consolidated)
    if len(a) < 2:
        return Score("Piotroski F", None, missing=["<need 2 years>"])
    cur, pri = _row_getter(a, a.index[-1]), _row_getter(a, a.index[-2])
    missing: list[str] = []

    def both(name):
        c, p = cur(name), pri(name)
        if c is None or p is None:
            missing.append(name)
        return c, p

    ni_c, ni_p = both("ProfitLossForPeriod")
    ta_c, ta_p = both("Assets")
    cfo_c, _ = both("CashFlowsFromUsedInOperatingActivities")
    ca_c, ca_p = both("CurrentAssets")
    cl_c, cl_p = both("CurrentLiabilities")
    ltd_c, ltd_p = both("BorrowingsNoncurrent")
    sh_c, sh_p = both("EquityShareCapital")
    sales_c, sales_p = both("RevenueFromOperations")
    cogs_c, cogs_p = _cogs(cur), _cogs(pri)
    if cogs_c is None or cogs_p is None:
        missing.append("CostOfMaterialsConsumed")
    if missing:
        return Score("Piotroski F", None, missing=missing,
                     note="needs all 9-signal inputs across 2 years")

    roa_c, roa_p = ni_c / ta_c, ni_p / ta_p
    signals = {
        "roa_pos": ni_c > 0,
        "cfo_pos": cfo_c > 0,
        "d_roa_pos": roa_c > roa_p,
        "accrual_cfo>ni": cfo_c > ni_c,
        "d_leverage_down": (ltd_c / ta_c) < (ltd_p / ta_p),
        "d_currentratio_up": (ca_c / cl_c) > (ca_p / cl_p),
        "no_dilution": sh_c <= sh_p,
        "d_grossmargin_up": ((sales_c - cogs_c) / sales_c) > ((sales_p - cogs_p) / sales_p),
        "d_assetturnover_up": (sales_c / ta_c) > (sales_p / ta_p),
    }
    f = float(sum(1 for v in signals.values() if v))
    return Score("Piotroski F", f, {k: float(v) for k, v in signals.items()})


# --------------------------------------------------------------------------- #
# Beneish M (manipulation likelihood); needs current + prior year.
# --------------------------------------------------------------------------- #
def beneish_m(con: duckdb.DuckDBPyConnection, symbol: str, *,
              consolidated: bool = False) -> Score:
    a = load_annual(con, symbol, consolidated)
    if len(a) < 2:
        return Score("Beneish M", None, missing=["<need 2 years>"])
    cur, pri = _row_getter(a, a.index[-1]), _row_getter(a, a.index[-2])
    missing: list[str] = []

    rec_c, rec_p = _receivables(cur), _receivables(pri)
    sales_c, sales_p = cur("RevenueFromOperations"), pri("RevenueFromOperations")
    cogs_c, cogs_p = _cogs(cur), _cogs(pri)
    ca_c, ca_p = cur("CurrentAssets"), pri("CurrentAssets")
    ppe_c, ppe_p = cur("PropertyPlantAndEquipment"), pri("PropertyPlantAndEquipment")
    ta_c, ta_p = cur("Assets"), pri("Assets")
    dep_c, dep_p = cur("DepreciationDepletionAndAmortisationExpense"), \
        pri("DepreciationDepletionAndAmortisationExpense")
    ni_c = cur("ProfitLossForPeriod")
    cfo_c = cur("CashFlowsFromUsedInOperatingActivities")
    liab_c, liab_p = cur("Liabilities"), pri("Liabilities")
    # SG&A approx = employee benefit + other expenses.
    sga_c = (cur("EmployeeBenefitExpense") or 0) + (cur("OtherExpenses") or 0)
    sga_p = (pri("EmployeeBenefitExpense") or 0) + (pri("OtherExpenses") or 0)

    needed = {"receivables": (rec_c, rec_p), "sales": (sales_c, sales_p),
              "cogs": (cogs_c, cogs_p), "current_assets": (ca_c, ca_p),
              "ppe": (ppe_c, ppe_p), "assets": (ta_c, ta_p),
              "depreciation": (dep_c, dep_p), "net_income": (ni_c, ni_c),
              "cfo": (cfo_c, cfo_c), "liabilities": (liab_c, liab_p)}
    for label, (c, p) in needed.items():
        if c is None or p is None:
            missing.append(label)
    if missing:
        return Score("Beneish M", None, missing=missing,
                     note="needs all 8-variable inputs across 2 years")

    dsri = (rec_c / sales_c) / (rec_p / sales_p)
    gm_c, gm_p = (sales_c - cogs_c) / sales_c, (sales_p - cogs_p) / sales_p
    gmi = gm_p / gm_c
    aqi = (1 - (ca_c + ppe_c) / ta_c) / (1 - (ca_p + ppe_p) / ta_p)
    sgi = sales_c / sales_p
    depi = (dep_p / (dep_p + ppe_p)) / (dep_c / (dep_c + ppe_c))
    sgai = (sga_c / sales_c) / (sga_p / sales_p)
    tata = (ni_c - cfo_c) / ta_c
    lvgi = (liab_c / ta_c) / (liab_p / ta_p)

    comps = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi,
             "DEPI": depi, "SGAI": sgai, "TATA": tata, "LVGI": lvgi}
    m = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
         + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    return Score("Beneish M", m, comps,
                 note="SG&A~=employee+other; COGS~=materials+purchases+inv-change")


# --------------------------------------------------------------------------- #
# Accruals (earnings quality): balance-sheet Sloan ratio + cash-flow accruals.
# --------------------------------------------------------------------------- #
def accruals(con: duckdb.DuckDBPyConnection, symbol: str, *,
             consolidated: bool = False) -> Score:
    """Sloan balance-sheet accruals ratio (+ the cash-flow accruals for context).

    Sloan = [Δ(non-cash current assets) − Δ(non-debt current liabilities) − D&A]
    / average total assets. High positive accruals = profit not backed by cash =
    a classic earnings-quality red flag (low-accrual firms historically outperform).
    """
    a = load_annual(con, symbol, consolidated)
    if len(a) < 2:
        return Score("Sloan accruals %", None, missing=["<need 2 years>"])
    cur, pri = _row_getter(a, a.index[-1]), _row_getter(a, a.index[-2])
    missing: list[str] = []

    def both(name):
        c, p = cur(name), pri(name)
        if c is None or p is None:
            missing.append(name)
        return c, p

    ca_c, ca_p = both("CurrentAssets")
    cash_c, cash_p = both("CashAndCashEquivalents")
    cl_c, cl_p = both("CurrentLiabilities")
    dep_c, _ = both("DepreciationDepletionAndAmortisationExpense")
    ta_c, ta_p = both("Assets")
    std_c, std_p = cur("BorrowingsCurrent") or 0.0, pri("BorrowingsCurrent") or 0.0
    if missing:
        return Score("Sloan accruals %", None, missing=missing,
                     note="needs current + prior-year balance sheet")
    d_nwc = (ca_c - cash_c - (ca_p - cash_p)) - ((cl_c - std_c) - (cl_p - std_p))
    accr = d_nwc - dep_c
    avg_ta = (ta_c + ta_p) / 2
    if not avg_ta:
        return Score("Sloan accruals %", None, note="zero average assets")
    comps = {"sloan_%": 100 * accr / avg_ta, "delta_nwc": d_nwc, "depreciation": dep_c}
    ni_c, cfo_c = cur("ProfitLossForPeriod"), cur("CashFlowsFromUsedInOperatingActivities")
    if ni_c is not None and cfo_c is not None and ta_c:
        comps["cashflow_accruals_%"] = 100 * (ni_c - cfo_c) / ta_c
    return Score("Sloan accruals %", 100 * accr / avg_ta, comps,
                 note="high positive => aggressive (profit not cash-backed)")
