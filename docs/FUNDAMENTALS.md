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

## Limits / follow-ups

- Quarterly result XBRL is **P&L-heavy**; full balance-sheet / cash-flow items
  (instant contexts) are sparse. Forensic scores (Piotroski/Altman/Beneish) and
  ROE/ROCE need those → ingest **annual** filings / balance-sheet XBRL next.
- Annual figures: derive as **TTM** (sum 4 quarters) until annual ingest lands.
- The catalog step is browser-tier; cache filing lists to avoid re-warming.
