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

### ⚠️ SEBI Integrated Filing cutover (Dec-2024 quarter onward)
From the **Dec-2024 quarter**, SEBI's **Integrated Filing** framework replaced the
old result submission, and the legacy `corporates-financial-results` catalog
**stopped getting new entries** — so it now ends at `2024-12-31` for *every* symbol.
The new results live at **`/api/integrated-filing-results?index=equities&symbol=<S>&period=Quarterly`**
(rows carry `qe_Date`, `consolidated`, a `type` of `"Integrated Filing- Financials"`
vs `"…Governance"`, and a direct `xbrl` URL). The XBRL uses SEBI's new **`in-capmkt`**
taxonomy — but with the **same `OneD`/`FourD`/`OneI` contexts and identical element
local-names**, so the same parser handles it. `nse_financials.list_all_result_filings`
**merges** the legacy feed (history ≤ Dec-2024) with the Integrated Filing feed
(≥ the new regime); the annual side reads the full year from each fiscal-year-end
(31-Mar) integrated filing's `FourD`/`OneI`. The ingest path is otherwise unchanged.

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
- **TTM P&L** (`ttm_pl`): the trailing-4-quarter **sum of every P&L element** (for a
  "TTM" column beside the annual statements in the deep brief). Returns empty unless
  4 *consecutive* quarters exist (≈9–13 months end-to-end), so a missing quarter
  never silently understates the total.

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

The parser is taxonomy-version-agnostic (matches any `…/xbrl/fin/<date>/in-bse-fin`
**and** the Integrated Filing `…/sebi.gov.in/xbrl/<date>/in-capmkt`).
XBRL exists from ~**FY2019** (older filings 404). Caveat: pre-FY2023 result XBRLs
are `_WEB.xml` variants that carry **P&L (+ often cash flow) but no balance
sheet** — and they reference the plain headline contexts (`FourD`, `OneI`)
without defining them, so we keep facts on those by the ID convention.

Net effect on RELIANCE: **6 years of P&L** (FY2019–24) for trend/growth, but
balance-sheet metrics + the forensic scores remain **FY2023+** (where the balance
sheet is present).

## 🏦 Banks — a different XBRL taxonomy (`analysis/lenders.py`)

Banks file results under the **RBI banking taxonomy**, not Ind-AS corporate: `InterestEarned` /
`InterestExpended` / `OperatingProfitBeforeProvisionAndContingencies` /
`ProvisionsOtherThanTaxAndContingencies` / `ProfitLossForThePeriod`, a balance sheet of `Advances`
/ `Deposits` / `Investments` / `Capital` + `ReservesAndSurplus`, and regulatory ratios
(`PercentageOfGrossNpa`, `PercentageOfNpa`, `CET1Ratio`, `AdditionalTier1Ratio`). Before this
existed every one of the ~34 listed banks read as empty (no `RevenueFromOperations`).

- **Normalisation at load** (`fundamentals._normalise`, applied by `load_quarters` / `load_annual`):
  same-meaning tags are copied onto their corporate names so every consumer works — revenue ←
  interest earned (the usual Indian-screener convention; fees stay in `OtherIncome`), profit, PBT,
  employee cost, equity share capital (`PaidUpValueOfEquityShareCapital` first — a bank's
  `Capital` can include more than equity: ICICI's is ₹4,114 cr vs ₹1,432 cr of equity), and total
  equity = capital + reserves. **Deliberately not mapped:** interest expended → `FinanceCosts` (it
  would give every bank a ~1.3× "interest cover"), deposits → debt.
- **Ratio repair:** some banks file certain periods' ratios 100× too small (CET1 `0.0012` for 12%,
  GNPA `0.0002` for 2% — YESBANK, EQUITASBNK, JSFB, UJJIVANSFB); consolidated filings put 0 for NPA
  and CET1 ("not reported"). Zeros → missing; CET1 < 3% and GNPA < 0.1% → ×100 (net NPA only in the
  same row); CET1 > 60% after repair → missing.
- **Bank metrics** (`lenders.annual_metrics` / `quarterly_metrics`): NII, PPOP, NIM (NII ÷ average
  total assets — a proxy that reads slightly below the bank's own NIM on interest-earning assets),
  cost-to-income, credit cost (provisions ÷ average advances), ROA / ROE on average balances
  (computed — the filed quarterly ROA is annualised by some banks and not others), CD ratio, loan &
  deposit growth, GNPA / NNPA (₹ and %), provision coverage (1 − NNPA ÷ GNPA), CET1 / Tier-1, book
  value per share. Validated against HDFC Bank's published FY25 figures (NII ₹1,22,670 cr, PAT
  ₹67,347 cr, GNPA 1.33%, NNPA 0.43%).
- **Health checks** (`lenders.health_checks`, thresholds `BANK_*` in `config.py`): GNPA rising 3
  quarters running, net NPA level, thin provision coverage, credit-cost spike vs its own history, thin
  CET1, CD ratio > 90%, NII growth lagging loan growth (margin squeeze), ROA — ✅ / ⚠️ / 🔴.
- **Altman / Piotroski / Beneish / Sloan** return `forensic.NOT_FOR_BANKS` for a bank instead of a
  number; the health checks replace them. CFO/PAT and accruals are blanked (a bank's cash flow is
  deposit and loan movement). Peer tables compare banks on ROA and GNPA % instead of ROCE and D/E.
- **Basis:** banks default to **standalone** — NIM, cost-to-income, NPAs and capital are bank-level
  concepts, and consolidation folds in insurance / AMC subsidiaries (HDFC Bank's consolidated
  cost-to-income reads ~60% vs the bank's ~40%). With "consolidated" requested, regulatory ratios are
  taken from the standalone filing and labelled as such.
- **Not yet covered:** insurers file yet another taxonomy (premiums, claims) and still read thinly.

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

### Accruals & promoter pledge (added)

- **Sloan (balance-sheet) accruals** — `forensic.accruals()`:
  `[Δ(non-cash current assets) − Δ(non-debt current liabilities) − D&A] / avg
  assets`. High positive ⇒ profit not cash-backed (classic earnings-quality
  flag). Reported alongside the existing cash-flow accruals `(PAT−CFO)/assets`.
- **Promoter pledge** — NSE `/api/corporate-pledgedata` (browser tier;
  `nse_api.promoter_pledge[_batch]`) → `shareholding` table
  (`ingest.ingest_shareholding`). Surfaces **pledged % of promoter holding**
  (the investor-relevant figure) + promoter holding %, and feeds a watchlist
  pledge-rise alert. Degrades to `n/a` if the feed is unavailable.
- **Contingent liabilities & related-party transactions** are **not** in the
  structured XBRL (no such tags) — they live in annual-report notes; supply a
  filing PDF and the LLM step extracts them.

For the **Monte-Carlo DCF / reverse-DCF / Benford / sector z-scores** quant layer
(`analysis/quant.py`), see [`REPORTS.md`](REPORTS.md).

## Valuation (`analysis/valuation.py`)

Joins annual financials × `equity_eod` prices. Shares = `EquityShareCapital` ÷
`FaceValueOfEquityShareCapital`. Market cap is computed **per period from
contemporaneous shares × that period's price**, which makes P/E and P/B
bonus/split-invariant and comparable across time.

- `valuation_history(symbol)` — P/E & P/B at each fiscal year-end (price from the
  nearest trading day ≤ year-end; `ingest_eod_on_or_before` backfills them).
- `snapshot(symbol, shares_override=…)` — current P/E (TTM), P/B, earnings yield.
- `market_cap(symbol, shares_override=…)` — feeds **Altman X4** (RELIANCE Z goes
  2.27 book-equity → **3.94 safe** with real market cap).

Report: `uv run python scripts/valuation_report.py RELIANCE [--shares <crore>]`
→ e.g. RELIANCE current P/E 49.5 flagged *above* its 2-yr history (35.7–47.8).

**Bonus/split caveat (important):** current shares come from the latest annual
filing, so a corporate action since then makes the live snapshot stale (RELIANCE
1:1 bonus Oct-2024 → pass `--shares 1353.2`). The output surfaces this; history
rows are each internally consistent (contemporaneous shares) and unaffected.

## Valuation vs sector (`analysis/sector.py`)

Peers come from `sector_map` — the NSE Nifty-500 constituent list's `Industry`
tag (one plain-HTTP file; `ingest_sector_map`). RELIANCE → "Oil Gas & Consumable
Fuels" (17 peers). `sector_valuation(symbol)` computes the target's current P/E &
P/B (via `valuation.snapshot`) and percentile-ranks it against peers that have
financials ingested — "cheaper than X% of peers".

Report: `uv run python scripts/sector_report.py RELIANCE [--shares <crore>]`.

Caveats: a peer participates only once its financials are ingested (browser-tier
catalog per peer); peers assume no corporate action since their last annual
(the target can be corrected with `--shares`/`target_shares_override`).

## Limits / follow-ups

- **Valuation history depth** follows balance-sheet availability (FY2023+), since
  per-year shares come from `EquityShareCapital`. P&L trend is 6 years regardless.
- Auto-adjust current shares for post-filing corporate actions (from
  `corporate_actions()`), so the live snapshot needs no manual `--shares`.
- The catalog step is browser-tier; cache filing lists to avoid re-warming.
