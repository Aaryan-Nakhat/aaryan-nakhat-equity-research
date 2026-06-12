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

### Phase 0 — Scaffolding (this commit)
Repo, structure, planning docs. No code yet.

### Phase 1 — Validate scraping (de-risk first)
Prove `scrapling` can reliably pull from the hardest source before building
anything on top. Order, easiest → hardest:
1. BSE bhavcopy / filings (friendlier).
2. NSE session-gated JSON (quotes, filings, corporate actions).
3. NSE F&O / option chain (most bot-protected) — confirm feasibility.

Output: a thin scraper per source that returns clean structured data.

### Phase 2 — Fundamental analysis
- Ingest financials (XBRL + PDF results) into DuckDB.
- Multi-year (5–10 yr) series: P&L, balance sheet, cash flow.
- Computed ratios: ROE / ROCE / ROIC, leverage, liquidity, cash conversion.
- Quality / forensic scores: Piotroski F, Altman Z, Beneish M; CFO-vs-PAT
  divergence; promoter pledge; related-party creep; receivables vs revenue.
- Valuation vs **own history** and **sector**, not absolutes.

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
