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

### Phase 1 — Validate scraping (de-risk first) — ✅ done
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

### Phase 2 — Fundamental analysis — ✅ done
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

**Valuation vs sector done** (`analysis/sector.py` + `sector_report.py`): peers
from the Nifty-500 `Industry` map (`sector_map`); percentile-rank P/E & P/B vs
sector. Validated — RELIANCE P/E 49.5 vs Oil & Gas median 9.8 (priciest in peer
group; Jio/Retail premium).

**Phase 2 complete.** Remaining (deferrable): auto-adjust current shares for
post-filing corporate actions; cache browser-tier catalog calls; consolidated as
the valuation default.

### Phase 3 — Technical analysis — ✅ done
**Built** (`analysis/technical.py` + `technical_report.py`, see
[`TECHNICAL.md`](TECHNICAL.md)): SMA 20/50/200, RSI, MACD, Bollinger, ATR,
volume + **delivery-% conviction**, 52-wk position, signals. Daily history
backfilled via `ingest_eod_range` (`backfill_eod.py`) — 373 days; `index_close`
also backfilled (~360 days) so relative-strength-vs-Nifty works. Validated on
RELIANCE.

**Remaining (deferrable):** derivatives positioning (OI, PCR, FII deriv stats —
data already scrapable via `nse_archives`/`nse_api`); ADX.

### Phase 4 — LLM integration + reports — ✅ done (live)
**Built + live** (`reports/` + `research_report.py`, see [`REPORTS.md`](REPORTS.md)):
`brief`/`deep_brief` assemble all quant signals → `synthesize.synthesize_thesis`
(**Gemini `gemini-2.5-pro` via Vertex AI**, service-account auth, streaming, reads
an optional concall/annual-report PDF) → delivered via:
- **Telegram bot** (`scripts/telegram_bot.py`, always-on Windows scheduled task):
  name → `resolve` (Gemini+Search) → deep report, **formatted inline (MarkdownV2)
  + styled PDF** (`reports/pdf.py`). Live-validated on RELIANCE / ADANIPOWER.
- **CLI** (`research_report.py`) and **email** (`reports/email.py`, SMTP).

(LLM provider is Gemini — reuses an existing workplace Vertex key, employer-
authorized; the brief/email layers are provider-agnostic.)

**Remaining (optional):** YoY annual-report diffing.

### Phase 5 — Watchlist alerts — ✅ done
**Built** (`analysis/alerts.py`, `scan.py`, `watchlist.py`; see [`ALERTS.md`](ALERTS.md)):
a **self-healing daily scan** (fires once per trading day at the first heartbeat
≥18:00 IST; weekend/holiday-skipped) over the 27-stock watchlist, delivered as a
**company-name digest** (email or Telegram, lines-only, **no PDFs**), with a
**market-context header** (Nifty 50 / Nifty 500 day move):
- **📅 Upcoming** — board-meeting/results dates, ex-dividend/split/bonus, AGM/fund-raising.
- **Movers** — per-stock close · day %chg · delivery% · 52-week position · **P/E vs own 5-yr median** (always present).
- **Events** — bulk/block **institutional deals**, a defined **corporate-event taxonomy**
  (results · dividend · split · rights · QIP · scheme/M&A · open offer · concall ·
  board meeting · AGM · credit rating · order win · pledge …), and **forensic/fundamental
  flips** (Altman/Beneish/Piotroski/CFO-PAT/pledge) — with `alert_state` dedup +
  first-sight seeding, and **inline Gemini analysis** of notable filing PDFs (capped 5).
Commands `/watch`, `/unwatch`, `/watchlist`, `/scan`. 27-stock watchlist populated.

### Phase 6 — depth, quant, email channel & report enrichment — ✅ done
- **Email channel** (`scripts/email_bot.py`: IMAP IDLE inbound + SMTP), via the `CHANNELS`
  flag — runs while Telegram is ISP-blocked; same brains, full report in body + PDF.
- **Quant suite** (`analysis/quant.py`): Monte-Carlo DCF (margin of safety, P(undervalued)),
  reverse DCF, scenario DCF, Benford's-law, sector z-scores.
- **Fundamental charts** in the PDF (`reports/charts.py`); **Sloan accruals** + **promoter
  pledge** forensics; **peer-comparison table**; **point-wise** §9 forensic deep-dive.
- **Self-explaining metrics** (`reports/glossary.py`): inline band tags + a standalone,
  cached **`Metric_guide.pdf`** attached separately (not in the report body/PDF).
- **Auto multi-filing read** (`pipeline._filings_for_analysis`): every report feeds Gemini
  all meaningful filings since the last FY-end + latest results; **consolidated** auto-picked
  for holding-cos (or forced via the email subject). Generic for any NSE symbol.
- **Report-integrity fixes** (post-WELCORP review): `ensure_ingested` is now **freshness-aware**
  (re-ingests when the latest stored quarter is stale, 2-day cooldown) so tables aren't frozen at
  the first-seen FY; statement tables carry a **TTM column** (`fundamentals.ttm_pl`); the DCF
  **caps beta to [0.4, 2.0]**, blends growth with **recent quarterly momentum**, and prints
  **"not meaningful"** instead of negative fair values; the peer table ingests **~6 same-sector
  peers on demand** (`_ensure_peer_financials`); §9 adds a Beneish **false-positive caveat** when
  accruals + cash conversion are clean.

### Later (deferred)
- Mutual-fund switching analytics (NAV, rolling returns, risk-adjusted,
  holdings overlap).
- Macro overlay (RBI / MOSPI) feeding sector calls.
- Screener across a broad universe to *find* ideas, not just analyse known ones.
- **Finer peer classification.** Peers are currently *same NSE macro-Industry within
  the Nifty 500* (`sector.peers` over the `sector_map`), so broad buckets lump
  unlike businesses (e.g. "Capital Goods" puts Welspun Corp's steel pipes next to
  Suzlon's wind turbines). Add a sub-industry / business-line tag (curated, or via
  the LLM) so the peer table compares true like-for-like competitors.

## 4. Where the LLM fits

The quant layer (ratios, scores, technicals) is deterministic Python. The LLM
(Gemini) adds value on the **unstructured** side:
- Digesting 200-page annual reports & concall transcripts.
- YoY diffing risk factors / accounting policy / RPTs.
- Synthesising everything into a readable thesis with a verdict and *reasons*,
  delivered via Telegram (formatted + PDF) or email.

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

Python 3.12 · `uv` · `scrapling` · DuckDB · pandas · Gemini (`google-genai`, via
Vertex AI / Gemini Developer API) · email delivery.
