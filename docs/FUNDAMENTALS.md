# Fundamentals — data path (Phase 2)

How this project sources **structured company financials** from primary data.
Probed + validated 2026-06-13 (probe:
[`scripts/probe_bse_financials.py`](../scripts/probe_bse_financials.py)).

## The search

| Source tried | Verdict |
|---|---|
| BSE `getScripHeaderData` etc. | only live quote/header, no financials |
| BSE `AnnGetData` | **dead** ("No Record Found") — superseded |
| BSE `AnnSubCategoryGetData` (strCat=Result) | ✅ lists result filings, but financials are **PDF attachments** (no structured numbers) |
| BSE guessed financials/ratios endpoints | don't exist |
| **NSE `corporates-financial-results`** | ✅ **the source** — see below |

## The source: NSE financial-results → XBRL

Two steps (`scrapers/nse_financials.py`):

1. **Catalog** (browser tier — Camoufox):
   `/api/corporates-financial-results?index=equities&symbol=<S>&period=Quarterly`
   → list of every result filing with `consolidated`/`audited`/`financialYear`/
   `fromDate`/`toDate` and a direct **`xbrl`** URL. (130 filings for RELIANCE.)
2. **XBRL** (plain HTTP): each filing's XBRL lives on
   `nsearchives.nseindia.com/corporate/xbrl/…` — downloadable without a browser.
   Standardised `in-bse-fin:*` tags carry exact numbers (rupees).

`results-comparision?symbol=<S>` (`resCmpData`) gives NSE-pre-parsed numbers but
only ~5 periods — used only as a **validation cross-check**, not the source.

## ⚠️ The XBRL gotcha (why naïve parsing is wrong)

BSE result XBRL gives multiple contexts the **same declared start/end dates** —
`OneD` (current quarter) and `FourD` (year-to-date) both say e.g. 2024-10-01→
2024-12-31. **The period is encoded by the context ID, not its dates.** Keying
facts by date silently overwrites the quarter with the YTD figure.

Context-ID convention (in-bse-fin taxonomy):

| ID | Period |
|---|---|
| `OneD` | current quarter (3 months) |
| `TwoD` | preceding quarter |
| `ThreeD` | year-ago quarter |
| `FourD` | current year-to-date |
| `FiveD` | prior-year YTD |
| `SixD` | prior full year |

We store **`OneD` (current quarter)** per filing → a clean, non-overlapping
quarterly series. We also keep only **dimensionless** facts (headline P&L, not
by-axis breakdowns) and read the file's nature from
`NatureOfReportStandaloneConsolidated`.

## Validation

`ingest_financials("RELIANCE")` → standalone Q3 FY25 (2024-12-31):
Revenue ₹128,260cr · Net Profit ₹8,721cr · PBT ₹11,597cr — exact match to the
filing, and the quarterly net-profit series (8721 → 7713 → 7611 → 11283)
independently matches `resCmpData`.

## Storage

`financials` table (long format), PK
`(symbol, period_end, consolidated, period_type, element)`:

| col | meaning |
|---|---|
| `element` | in-bse-fin tag (e.g. `RevenueFromOperations`, `ProfitLossForPeriod`) |
| `value` | rupees | `period_type` | `Q` (we store quarters) |
| `consolidated` | from the filing's nature | `source_url` | the XBRL |

`ingest_financials(symbol, con, period="Quarterly", max_filings=N)`.

## Ratio engine (`analysis/fundamentals.py`)

Reads the quarterly series and computes — validated on RELIANCE:

- **Per quarter** (`quarterly_metrics`): net / PBT / EBIT / EBITDA margins,
  interest coverage, effective tax rate, other-income-to-PBT, and **YoY** revenue
  & net-profit growth (vs the same quarter a year earlier).
- **TTM** (`ttm`): trailing-4-quarter revenue, net profit and margins.

Report: `uv run python scripts/fundamentals_report.py RELIANCE [--consolidated]`.

## Annual data (balance sheet + cash flow)

Annual filings (`period=Annual`) carry the full year **plus** the year-end
balance sheet and cash-flow statement. Context mapping (validated on RELIANCE):

| Data | Context | Notes |
|---|---|---|
| Full-year P&L + cash flow | `FourD` (duration) | `OneD` here = Q4 quarter |
| Year-end balance sheet | instant context dated at `to_date` | matched **by date** (instants are reliable) |
| Prior year | `FiveD` + prior instant | 2 years in one filing (for YoY scores) |

`ingest_annual_financials(symbol, con)` lands these as `period_type='Y'`.
Validated FY24 standalone: Revenue ₹547,942cr · Net ₹42,042cr · **CFO ₹73,998cr**
· Assets ₹959,643cr.

`annual_overview()` adds earnings-quality signals — notably **CFO-vs-PAT**
(`cfo_to_pat_x`) and the accruals ratio: FY24 CFO/PAT = 1.76 (cash backs profit).

### History depth (taxonomy versions)

The parser is taxonomy-version-agnostic (matches any `…/xbrl/fin/<date>/in-bse-fin`).
XBRL exists from ~**FY2019** (older filings 404). Caveat: pre-FY2023 result XBRLs
are `_WEB.xml` variants that carry **P&L (+ often cash flow) but no balance
sheet** — and they reference the plain headline contexts (`FourD`, `OneI`)
without defining them, so we keep facts on those by the ID convention.

Net effect on RELIANCE: **6 years of P&L** (FY2019–24) for trend/growth, but
balance-sheet metrics + the forensic scores remain **FY2023+** (where the balance
sheet is present).

## Forensic / quality scores (`analysis/forensic.py`)

All three built and validated on RELIANCE. Each returns the score, its
components, and a list of any **missing inputs** — emitted only when every input
is present (no silent zero-proxying).

| Score | RELIANCE FY24 | Reads | Bands |
|---|---|---|---|
| **Altman Z** | 2.27 (book-equity variant) | WC, OtherEquity (RE), EBIT, equity/liab, sales, all /assets | >2.99 safe · 1.81-2.99 grey · <1.81 distress |
| **Piotroski F** | 5/9 | ROA/CFO/accruals/ΔROA, Δleverage, Δcurrent-ratio, shares, Δgross-margin, Δturnover | 8-9 strong · 0-2 weak |
| **Beneish M** | −2.81 (clean) | DSRI/GMI/AQI/SGI/DEPI/SGAI/TATA/LVGI | M > −1.78 ⇒ possible manipulation |

Report: `uv run python scripts/forensic_report.py RELIANCE [--mcap <crore>]`.
Approximations (noted in output): COGS ≈ materials + purchases + Δinventory;
SG&A ≈ employee + other expenses; Altman X4 uses book equity unless `--mcap` given.

## Limits / follow-ups

- **Deeper history**: only recent annual filings parse cleanly; older ones may
  use an earlier XBRL taxonomy (different namespace) — handle to extend history.
- Valuation: shares-outstanding (`EquityShareCapital` ÷ face value) × `equity_eod`
  price → market cap / P-E vs own history.
- The catalog step is browser-tier; cache filing lists to avoid re-warming.
