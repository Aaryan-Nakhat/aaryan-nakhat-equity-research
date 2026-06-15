# Plan

> Initial planning doc. Captures the vision, scope, and phasing as discussed.
> This is a living document — expect it to change as we validate scraping and
> data quality.

## 1. Purpose

A **personal** research workbench for **Indian equities**. The end goal is
decision-grade output that helps me **buy/sell individual stocks**. Mutual-fund
analysis is explicitly **deferred** (added in a later phase).

I will use this to make real money decisions, so the bar is *decision-grade
data, not just raw feeds* — multi-year, cross-checked, and forensic where it
matters.

## 2. Hard constraints

- **Primary / government-backed sources only.** NSE, BSE, SEBI, RBI, MOSPI,
  MCA, and companies' own statutory filings. **No** blogs, news sites, broker
  research, screeners, or third-party data vendors.
- **Personal use.** No hosting, no static site, no public surface. (This is a
  deliberate departure from the `cricdex` snapshot/static-site pattern.)
- **Scraping via `scrapling`.** Including the anti-bot session handling that
  NSE requires.

## 3. Scope (Phase-by-phase)

### Phase 0 — Scaffolding ✅ done
Repo, structure, planning docs.

### Phase 1 — Validate scraping (de-risk first) — 🟡 in progress
Prove `scrapling` can reliably pull before building on top.

**Probe results (2026-06-13) — see [`SCRAPING.md`](SCRAPING.md):**
- ✅ **BSE** quotes/fundamentals: plain HTTP (`Fetcher`).
- ✅ **NSE bhavcopy + delivery %**: plain HTTP via `nsearchives.nseindia.com`
  (archive files dodge the WAF entirely — easier than expected).
- ⚠️ **NSE `/api/`**: browser tier (Camoufox in-page `fetch`); works for
  `marketStatus` etc., but `quote-equity` is currently WAF-blocked. Not a
  blocker — BSE + archives cover our needs.

**Scrapers built** (`src/equity_research/scrapers/`, smoke-tested live):
- `bse.fetch_scrip_header(scripcode)` — quote/company JSON.
- `nse_archives` — `fetch_bhavcopy` (+numeric `DELIV_PER`), `fetch_index_closes`,
  `fetch_participant_oi`, `fetch_fo_bhavcopy` (all plain HTTP).
- `nse_api` — `fetch_api` + wrappers `fii_dii_activity`,
  `corporate_announcements`, `corporate_actions`, `option_chain_equity`
  (Camoufox in-page XHR). NSE endpoint map in [`SCRAPING.md`](SCRAPING.md).
- Shared `common.http` helpers (work around the `.text`-empty gotcha).

**Storage built** (`common/db.py` + `ingest.py` + `scripts/ingest_eod.py`):
DuckDB landing tables `equity_eod` / `index_close` / `participant_oi` with a
date-idempotent writer. `ingest_eod(date)` lands a full day (3246/147/5 rows
verified, re-runs overwrite cleanly).

**Phase 1 essentially complete.** Remaining (minor, deferrable): re-find the two
moved NSE paths (index constituents, index option chain); land `fo_bhavcopy` at
contract grain when Phase 3 needs OI.

### Phase 2 — Fundamental analysis — 🟡 in progress
**Data path built + validated** (see [`FUNDAMENTALS.md`](FUNDAMENTALS.md)):
NSE `corporates-financial-results` (catalog, browser) → XBRL on `nsearchives`
(plain HTTP) → `in-bse-fin` tags. `scrapers/nse_financials.py` +
`ingest.ingest_financials(symbol)` land a clean quarterly P&L series into the
`financials` table (long format). Validated on RELIANCE (Q3 FY25 rev ₹128,260cr
/ net ₹8,721cr, exact). Solved the BSE-XBRL context-ID gotcha (OneD=quarter).

**P&L ratio engine built** (`analysis/fundamentals.py` + `fundamentals_report.py`):
per-quarter margins (net/PBT/EBIT/EBITDA), interest cover, effective tax rate,
YoY growth, and TTM aggregates. Validated on RELIANCE.

**Annual + forensic engine built** (`analysis/forensic.py` + `forensic_report.py`):
annual balance-sheet/cash-flow ingested; **Altman Z, Piotroski F, Beneish M** and
CFO-vs-PAT all computed + validated on RELIANCE (Z 2.27 / F 5 / M −2.81 clean).
Scores emit only when every input is present (missing inputs reported).

**Deeper history done**: taxonomy-agnostic parser → 6 years of P&L (FY2019-24);
balance sheet + forensic/valuation stay FY2023+ (older result XBRLs omit the
balance sheet).

**Valuation done** (`analysis/valuation.py` + `valuation_report.py`): P/E & P/B vs
own history (contemporaneous, bonus-invariant); current snapshot + market cap
feeding Altman X4 (RELIANCE Z 2.27→3.94). Bonus/split staleness surfaced.

**Phase 2 essentially complete.** Remaining (deferrable): valuation **vs sector**
(needs a peer universe); auto-adjust current shares for post-filing corporate
actions; cache browser-tier catalog calls.

### Phase 3 — Technical analysis
Computed from EOD OHLCV + delivery %:
- Trend/momentum (DMAs, MACD, RSI, ADX), volatility (BBands, ATR),
  volume/delivery conviction, relative strength vs index.
- Derivatives positioning (OI, PCR, FII deriv stats) where scrapable.

### Phase 4 — Claude integration + email reports
- Claude reads unstructured filings (annual reports, concall transcripts) →
  extracts guidance, tone, risk-factor changes, red flags.
- Year-over-year diffing of annual reports (high-signal, tedious for a human).
- Synthesises quant signals + qualitative read into a structured thesis.
- **Emails** the result (the "Claude sends me the results" piece).

### Phase 5 — Triggers / alerting
Event-driven emails: results day, ratio breaches, pledge increases, rating
downgrades, technical breakouts.

### Later (deferred)
- Mutual-fund switching analytics (NAV, rolling returns, risk-adjusted,
  holdings overlap).
- Macro overlay (RBI / MOSPI) feeding sector calls.
- Screener across a broad universe to *find* ideas, not just analyse known ones.

## 4. Where Claude fits

The quant layer (ratios, scores, technicals) is deterministic Python. Claude's
value is on the **unstructured** side:
- Digesting 200-page annual reports & concall transcripts.
- YoY diffing risk factors / accounting policy / RPTs.
- Synthesising everything into a readable thesis with a verdict and *reasons*,
  delivered by email.

## 5. Known risks / open questions

- **NSE anti-bot gating** is the single biggest technical risk — validate in
  Phase 1 before committing to architecture.
- **XBRL / PDF financial normalization** is where most engineering effort (and
  silent data-error risk) lives. Garbage here corrupts every downstream score.
- **MCA** financials are login + pay-per-doc + captcha → effectively out of
  scope; exchange XBRL filings substitute.
- **Point-in-time discipline** — if we ever backtest theses, store data as it
  was known then to avoid look-ahead bias.

## 6. Stack

Python 3.12 · `uv` · `scrapling` · DuckDB · pandas · Anthropic Claude API ·
email delivery. (Choices to be firmed up as we build.)
