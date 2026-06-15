# aaryan-nakhat-equity-research

A private equity-research workbench for **Indian stocks (NSE / BSE)**. Pulls
**primary, official, government-backed data only** (exchanges, SEBI, RBI, MOSPI,
company filings) — no blogs, no news aggregators, no third-party data vendors —
runs **fundamental + technical analysis**, and emails decision-grade reports to
help with actual buy/sell decisions.

Personal use. Not a hosted product.

## What it does

- **Scrape** primary sources for prices, filings, financials, corporate actions,
  and delivery/derivatives data (via `scrapling`).
- **Analyse** — fundamental (multi-year statements, ratios, quality/forensic
  scores, FCFF/FCFE, CFO-quality, valuation vs own history & sector) and
  technical (trend, momentum, volume, delivery-% conviction, relative strength).
- **Report** — Gemini reads the quant brief (+ optional filing PDF) and writes a
  forensic thesis, delivered via a **Telegram bot** (formatted inline + styled
  PDF) or email.

## Status

Working end-to-end (NSE/BSE → DuckDB → fundamentals/forensics/technicals/
valuation → Gemini report → Telegram bot, always-on). Phases 1–4 done; Phase 5
(watchlist alerts) pending. Docs:

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
