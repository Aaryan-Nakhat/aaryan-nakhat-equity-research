# aaryan-nakhat-equity-research

A private equity-research workbench for **Indian stocks (NSE / BSE)**. Pulls
**primary, official, government-backed data only** (exchanges, SEBI, RBI, MOSPI,
company filings) — no blogs, no news aggregators, no third-party data vendors —
runs **fundamental + technical analysis**, and emails decision-grade reports to
help with actual buy/sell decisions.

Personal use. Not a hosted product.

## What it does (intended)

- **Scrape** primary sources for prices, filings, financials, corporate actions,
  shareholding, insider trades, and macro data (via `scrapling`).
- **Analyse** — fundamental (multi-year ratios, quality/forensic scores,
  valuation vs history & sector) and technical (trend, momentum, volume,
  delivery %, derivatives positioning).
- **Report** — Gemini reads the unstructured filings (annual reports, concall
  transcripts) and synthesises a thesis; results are **emailed** to me.

## Status

Early scaffolding. See planning docs:

- [`docs/PLAN.md`](docs/PLAN.md) — vision, scope, phases, where the LLM fits.
- [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) — detailed inventory of every
  data source and its scrapability (login / session / public).

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

## Stack (intended)

- Python 3.12, `uv`
- `scrapling` (scraping, incl. anti-bot session handling for NSE)
- DuckDB (analytics) · pandas
- Gemini (`google-genai`, via Vertex AI or the Gemini Developer API) — qualitative analysis + report synthesis
- Email delivery for reports
