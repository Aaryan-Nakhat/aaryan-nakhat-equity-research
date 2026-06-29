# aaryan-nakhat-equity-research

A private equity-research workbench for **Indian stocks (NSE / BSE)**. Pulls
**primary, official, government-backed data only** (exchanges, SEBI, RBI, MOSPI,
company filings) — no blogs, no news aggregators, no third-party data vendors —
runs **fundamental + technical analysis**, and emails decision-grade reports to
help with actual buy/sell decisions.

Personal use. Not a hosted product.

## What it does

- **Scrape** primary sources for prices, filings, financials, corporate actions,
  delivery/derivatives data, and **live intraday quotes** (NSE), plus the daily
  **USD/INR** reference rate (FBIL) and **gold/silver/crude** futures (MCX) — via
  `scrapling` (Camoufox browser tier for the anti-bot `/api/*`).
- **Analyse** — fundamental (multi-year statements, ratios, quality/forensic scores,
  FCFF/FCFE, CFO-quality); **sector-appropriate valuation** (P/B-on-ROE for financials,
  EV/EBITDA + mid-cycle for cyclicals, P/E elsewhere; current multiple as an own-history
  percentile; **reverse-DCF** as the centrepiece; a **forward multiple** from management's
  own guidance); and technical (trend, momentum, delivery-% conviction).
- **Signals** — FII F&O positioning (smart-money sentiment), **insider/promoter (SEBI PIT)
  trades**, promoter pledge, bulk/block deals.
- **Report** — Gemini reads the quant brief (+ filing PDFs) and writes a forensic thesis,
  delivered via an **email bot** (or Telegram, by the `CHANNELS` flag): **interactive**
  (name a stock → styled PDF + inline thesis) and **push** — a **full daily digest at
  18:00 IST** (rich market-context header: sectoral indices · VIX · FII/DII · FII futures ·
  USD/INR · commodities; movers; events with inline filing analysis; insider trades) and a
  lighter **midday "same-day" digest at 12:30** (live movers + today's filings/insider).

## Status

Working end-to-end (NSE/BSE/MCX/FBIL → DuckDB → fundamentals/forensics/technicals/
valuation + signals → Gemini report → email **or** Telegram bot, always-on). On-demand
reports + a midday (12:30) and full (18:00) watchlist digest; an email channel mirrors the
Telegram one for when Telegram is ISP-blocked. Docs:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — end-to-end diagram + component map.
- [`docs/PLAN.md`](docs/PLAN.md) — vision, scope, phase status.
- [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) / [`docs/SCRAPING.md`](docs/SCRAPING.md) — sources + scrapability findings.
- [`docs/FUNDAMENTALS.md`](docs/FUNDAMENTALS.md) — financials data path, ratios, forensic scores, valuation.
- [`docs/TECHNICAL.md`](docs/TECHNICAL.md) — indicators. [`docs/REPORTS.md`](docs/REPORTS.md) — Gemini synthesis, Telegram bot, PDF, email.

## Layout

```
src/equity_research/
  scrapers/    source-specific scrapers (NSE, BSE, SEBI, RBI, ...)
  analysis/    fundamental + technical analysis
  reports/     report generation + email delivery
  common/      config, storage, shared utilities
scripts/       pipeline entry points
data/          raw scrapes + processed artifacts (gitignored)
docs/          planning + reference docs
tests/         tests
```

## Stack

- Python 3.12, `uv`
- `scrapling` (scraping, incl. Camoufox browser tier for NSE's anti-bot `/api/`)
- DuckDB (analytics) · pandas
- Gemini (`google-genai`, via Vertex AI service account) — symbol resolution + report synthesis
- `python-telegram-bot` (delivery) · `telegramify-markdown` (formatting) ·
  Playwright Chromium + `markdown` (HTML → PDF) · SMTP email
