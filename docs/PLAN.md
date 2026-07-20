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
  cached **`Metrics_and_ratings_guide.pdf`** attached separately (not in the report body/PDF);
  covers the metrics plus the categorical outputs (Verdict scale, P/E n/a reasons, event types).
- **Auto multi-filing read** (`pipeline._filings_for_analysis`): every report feeds Gemini
  all meaningful filings since the last FY-end + latest results; **consolidated** auto-picked
  for holding-cos (or forced via the email subject). Generic for any NSE symbol.
- **SEBI Integrated Filing source** (post-ANANTRAJ review): the legacy
  `corporates-financial-results` XBRL feed froze at the **Dec-2024 quarter** (SEBI moved
  results to "Integrated Filing"), leaving every stock's tables ~18 months stale. Added
  `nse_financials.list_integrated_filings` (the new `/api/integrated-filing-results` endpoint,
  `in-capmkt` taxonomy — same contexts/elements, so the parser only needed the namespace
  added) + `list_all_result_filings` that **merges** legacy history with Integrated Filing;
  ingest now lands quarters/annuals through the latest filed period (verified FY2026 / Q4-FY26).
- **Report-integrity fixes** (post-WELCORP review): `ensure_ingested` is now **freshness-aware**
  (re-ingests when the latest stored quarter is stale, 2-day cooldown) so tables aren't frozen at
  the first-seen FY; statement tables carry a **TTM column** (`fundamentals.ttm_pl`); the DCF
  **caps beta to [0.4, 2.0]**, blends growth with **recent quarterly momentum**, and prints
  **"not meaningful"** instead of negative fair values; the peer table ingests **~6 same-sector
  peers on demand** (`_ensure_peer_financials`); §9 adds a Beneish **false-positive caveat** when
  accruals + cash conversion are clean.

### Mutual-fund module (new track — core shipped)
Turn the workbench into a fund-aware tool: MF as an institutional-conviction signal on the
watchlist, fund research deep-reports, and forensic look-through. Sequenced so the hard
per-AMC holdings scraping is quarantined to its own phase. **Live now:** email `fund: <name>`
→ a returns/risk/rolling report with portfolio + watchlist-overlap look-through (any AMFI fund;
holdings for AMCs registered so far — **PPFAS + HDFC**). Remaining work is per-item below (mostly
broader AMC holdings coverage + report enrichment).

- **Phase 1 — NAV backbone + analytics ✅ done.** AMFI is the primary source (no bot wall):
  `scrapers/amfi.py` — `fetch_navall()` parses the daily `NAVAll.txt` (14.2k schemes: code,
  ISINs, name, AMC, category → coarse asset-class + Direct/Regular + Growth/IDCW) and
  `fetch_nav_history(amc_code, frm, to)` pulls per-AMC history (the report caps the range, so
  ingest fetches in ~180-day windows). Tables `mf_scheme` + `mf_nav` (`common/db.py`); ingest
  `ingest_mf_navall` (universe + daily point, accumulated forward → a NAV series) and
  `ingest_mf_nav_history` (chunked backfill). `ingest_mf_navall` runs in the daily 6pm scan
  (`scan.run_watchlist_scan`, best-effort). `analysis/funds.py` — point returns (CAGR ≥1y,
  absolute <1y), risk (annualised vol, Sharpe, Sortino, max drawdown), rolling-1y distribution,
  and a `summary()` bundle. Validated on Axis Large Cap (5.5y backfill).
  - *Small follow-up:* AMFI name→numeric-AMC-code map so arbitrary funds can be one-shot
    backfilled (history needs the `mf` code; the dropdown loads via JS, not static HTML —
    scrape the postback JSON once, or hardcode the ~44 codes). Forward accumulation needs no map.
- **Phase 1b — AMC-code map ✅ done.** `mf_amc` table + `ingest.build_mf_amc_map` (scrapes the
  55 active AMC codes from the disclosure page's `RssNAV` links, resolves each → name via the
  history report's AMC header). **50/51** `mf_scheme` AMCs resolvable → any fund's history is
  one-shot backfillable by name (`ingest.backfill_mf_scheme_history`).
- **Phase 2 — MF-ownership signal per watchlist stock (folded into Phase 3).** Intended as a
  digest line ("MF holding 12.3% +0.8pp QoQ"). *Finding:* the NSE SHP **summary** endpoint —
  `/api/NextApi/apiClient/GetQuoteApi?functionName=getShareholdingPattern&symbol=X` (cracked,
  browser tier) — only exposes **promoter-vs-public + an `ndsid`**, not the MF/FII institutional
  breakdown (its detail call's `functionName` is unknown — variants 400). The per-stock MF% is
  therefore better derived from Phase 3's monthly holdings (monthly not quarterly, and yields
  *which* funds). The **fund report already shows the reverse** (watchlist overlap per fund); the
  stock-side digest line ("which funds hold stock X") lands once holdings coverage is broad (below).
- **Phase 3 — monthly holdings look-through ✅ done (PPFAS + HDFC live; coverage grows per-AMC).**
  No consolidated primary feed — each AMC posts its own file, so `scrapers/mf_holdings.py` is a
  **registry of per-AMC fetchers** (`REGISTRY[amc] → fetcher`) over one **generic SEBI-format
  parser** (`parse_generic`: detects the header row + columns by label, auto-scales %NAV
  fraction-vs-% and market-value ₹lakh-vs-₹cr) — so each new AMC is mostly just a URL. Two source
  patterns proven: **PPFAS** (one workbook, sheet per scheme; deterministic URL; 597 holdings) and
  **HDFC** (one direct-CDN file per scheme, listed on a JS page → `_capture_links` grabs the file
  list, then parse each; 7,210 holdings across 92 schemes). Table `mf_holdings`; ingest
  `ingest_mf_holdings` (maps each sheet/file → AMFI scheme_code; **monthly guard** skips the heavy
  fetch when the month is already stored) + `ingest_mf_holdings_all` (daily scan, best-effort).
  Needs `openpyxl`. *Next:* register more AMCs — SBI/ICICI/Kotak/Nippon/etc. (each is a small
  fetcher; **ICICI** = WAF'd JSON API at `apimf.icicipruamc.com/nms/v1/downloads/…` + CORS, so it
  needs its own crack; the AMFI disclosure page is the directory of each AMC's file host). Once
  ~15 AMCs are in, add the stock-side digest line ("which funds accumulated/exited name X, MoM").
- **Phase 4 — fund deep-report ✅ done (live).** `reports/fund_brief.py` — free-text fund resolver
  (`resolve_fund`, prefers Direct-Growth) → on-demand history backfill → returns (CAGR/absolute),
  risk (vol/Sharpe/Sortino/max-DD), rolling-1y, category percentile (best-effort), portfolio
  snapshot + watchlist overlap. Wired into the **email bot**: `fund: <name>` (or `mf: <name>`) in
  the subject → fund report; disambiguation reuses the pending "which one?" UX (`MF:` tag).
  **Attachments:** a charted PDF (NAV-growth + rolling-return distribution, `charts.fund_charts`)
  and a mutual-fund metrics guide PDF (`glossary.fund_guide_pdf`). *Later:* expense ratio (AMFI
  TER), AUM trajectory, manager/tenure, an LLM thesis.
- **Phase 5 — forensic look-through ✅ started.** Lean version live: portfolio concentration
  (top-10, biggest sector) + **watchlist overlap** (holdings ∩ the user's forensic-vetted names,
  by company-name match) in the fund report. *Deeper:* run Altman-Z / Piotroski / Beneish across
  all holdings for a portfolio-quality score — needs an ISIN→NSE-symbol map + financials ingested
  for the holdings (currently only the watchlist universe has them).

### IPO module (new track — ✅ shipped)
Pre-listing analysis for live / upcoming public issues, delivered through the email bot.
- **Data (all primary NSE) ✅ done** (`scrapers/ipo.py`): `/api/ipo-current-issue` (live + total
  subscription) · `/api/all-upcoming-issues?category=ipo` · `/api/ipo-detail` (category-wise QIB/
  NII/RII); offer documents from the predictable archive `nsearchives…/content/ipo/<DOC>_<SYM>.zip`
  — **RHP** (full prospectus) · **RATIOS** (price-band ad → KPIs / valuation-at-band / listed peers)
  · **ANCHOR** (allotment). `upcoming` is filtered to issues whose RHP is published.
- **Analysis ✅ done** (`synthesize.ipo_analysis`, `pipeline.generate_ipo_report`): Gemini reads
  the RHP + price-band ad + anchor doc, grounded on verified issue facts → snapshot · **fresh-issue
  vs OFS + what it signals** · restated financials · valuation-at-band vs peers · use of proceeds ·
  RHP risks · demand (subscription + anchor) · **APPLY / AVOID / NEUTRAL** verdict. No XBRL exists
  pre-listing, so it's RHP-driven, not the deterministic quant engine. **No grey-market/GMP.**
- **Delivery ✅ done**: `ipo: ongoing|upcoming|<name>` → list/note (body + PDF) + the same deeper-cut
  menu (growth triggers, RHP-grounded via `ipo_mode`). On-demand, no DB table.
- *Later:* verdict track-record for IPOs (listing-gain vs the call); DRHP-stage (SEBI) coverage for
  issues before the NSE RHP is posted; BSE fallback for the issue list.

### Later (deferred)
- **Verdict track record — make the tool grade itself** *(top-priority next build; the honest
  gap — we issue Buy/Accumulate/Hold/Reduce/Avoid verdicts and never check if they were right).*
  - `verdict_ledger` table: one row per verdict — `symbol, as_of_date, verdict, price_at_call,
    basis, thesis_reasons` **+ a snapshot of the signals at call time** (forensic scores,
    valuation lens + own-history percentile, reverse-DCF read, CFO/PAT) so we can later attribute
    *which signals predict*, not just whether the call worked. PK `(symbol, as_of_date)`; a
    re-rating is a new row (keeps the history).
  - *Capture:* (a) on-demand — a deterministic `parse_verdict(thesis)` pulls the one-of-five word
    from each deep report's structured verdict line, logged best-effort; (b) **a weekly systematic
    sweep** that deep-reports the whole watchlist and logs all verdicts — without this the ledger
    is too sparse to be statistical.
  - *Scoring:* `track_record()` — for each row past 1m/3m/6m/1y, stock return from `price_at_call`
    vs the **sector index** (excess/alpha; sector-relative grades stock-picking, not market timing);
    bullish call "right" if alpha > 0, bearish if alpha < 0. Aggregate by verdict bucket × horizon:
    avg return, avg alpha, hit-rate, n.
  - *Delivery:* an email **"scorecard"** command (table by bucket) + a monthly auto-email with the
    attribution slice. Label buckets with n; never over-claim on thin samples.
  - *Honest caveats:* starts from zero (can't backfill — the LLM would read today's filings, not
    the call-date's), so it's "not enough data" for ~a month, meaningful ~a quarter in, genuinely
    useful at 6–12 months. Modest statistical power (24 stocks × weekly).
- **Guidance-vs-delivery (management credibility)** — store each extracted forward guidance
  (`synthesize.extract_guidance`, already shipped); when actuals land, compare guided vs delivered
  → a habitual-over-promiser score that discounts the current guidance. Builds on today's work.
- **Ownership / stake-change tracking** — ingest NSE quarterly shareholding pattern (promoter %,
  FII %, DII %, MF %); flag promoter/institutional stake increases (conviction) vs trims + pledge
  upticks (red flag). Primary data; complements the forensic block. *(Adjacent **insider/promoter
  PIT trades** — `/api/corporates-pit` → digest alert + report section — are **done**; the
  remaining piece is the quarterly SHP stake-trend.)*

**Done (shipped):** FII F&O positioning in the digest header (`participant_oi` →
`positioning.fii_index_futures`); insider/promoter (SEBI PIT) trades — digest alerts +
deep-report section (`nse_api.insider_trades`, `insider_trades` table); **midday same-day
digest** at 12:30 IST (`scan.run_intraday_scan`/`format_intraday_digest`, `email_bot.maybe_intraday`)
— live movers + today's filings/insider via NSE's NextApi live quote (`live_quotes_batch`).
- Mutual-fund analytics — see the **Mutual-fund module** track above (Phase 1 shipped;
  holdings/overlap/reports/forensic-look-through are Phases 3–5). Personal MF portfolio
  tracking (overlap, XIRR, concentration) was scoped but deprioritised vs signal + research.
- Macro overlay (RBI / MOSPI) feeding sector calls.
- **Idea-generation screener** across a broad universe to *find* ideas, not just analyse known
  ones (the system's biggest "monitor → discover" gap). Two phases:
  - *Phase 1 — price/technical screen (feasible now, no new data):* over the full-market EOD we
    already hold (~4,100 symbols), surface 52-week breakouts/breakdowns, **delivery-% spikes**
    (institutional conviction), momentum and % from the 200-DMA → a weekly "what's moving" list.
  - *Phase 2 — fundamental + forensic screen (the real edge):* one-time bulk-ingest of the
    Nifty-500's financials (the Integrated-Filing scraper now works), then rank the universe on
    quality (ROCE, CFO/PAT, low debt) + **forensic** (clean Altman/Beneish/accruals/no pledge) +
    cheap-vs-own-history, and **auto-deep-report** the top candidates (screen finds, LLM diligences).
- **10-yr G-Sec yield** in the market header — the last deferred macro feed. FBIL's `gsec`
  endpoint returns only archive-file metadata (not inline yields); needs the archive download
  parsed, or a CCIL/RBI source. (USD/INR via FBIL and gold/silver/crude via MCX are **done**.)
  Global cues (US/Asia/Brent/DXY) would need a third-party vendor, which the primary-sources-only
  rule excludes.
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
