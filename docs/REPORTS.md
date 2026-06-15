# Reports — the LLM synthesis + email (Phase 4)

The capstone: assemble every quant signal into one **analytical brief**, have
**the LLM** (via the provider AI or the the LLM Developer API) turn it (plus an optional
filing PDF) into a structured investment thesis, and **email** the result.
`src/equity_research/reports/`.

## Pipeline

```
brief.build_brief(con, symbol)        # deterministic — all primary-source signals
        │   (fundamentals · forensic · technicals · valuation · sector)
        ▼
synthesize.synthesize_thesis(brief)   # the LLM (the-model) — qualitative read + verdict
        │   + optional concall/annual-report PDF (inline bytes)
        ▼
email.send_report(subject, report)    # SMTP
```

Orchestrated by `scripts/research_report.py`.

## The brief (`reports/brief.py`)

No LLM — pure assembly of what the analysis modules already compute: TTM +
annual fundamentals, CFO/PAT, Altman Z / Piotroski F / Beneish M, the technical
snapshot + signals, valuation (current multiples, own P/E history, sector
percentile). Renders to markdown; feeds both the prompt and the email body.
Validated on RELIANCE.

## Synthesis (`reports/synthesize.py`)

the LLM via the `the LLM SDK` SDK (`the-model` by default, override with
`LLM_MODEL`), streaming. System prompt = a sober Indian-equity analyst told to
ground every claim in the brief, respect `n/a`/caveats, and emit a 4-part note
(Verdict · Why · Risks · What to watch). An optional PDF (concall transcript /
annual report) is passed inline (`types.Part.from_bytes`) and read alongside the
brief — this is where management commentary enters the thesis.

**Auth (env, see `.env.example`) — two options:**
- **the provider AI** (a cloud GCP) via a **service account**:
  `LLM_USE_CLOUD=true`, `LLM_PROJECT`,
  `LLM_REGION`, and `LLM_CREDENTIALS_FILE=./gcp-service-account.json`.
  The key file is **gitignored** (`gcp-service-account.json` / `*service-account*.json`).
  Falls back to `LLM_CREDENTIALS`, then to `gcloud` ADC if neither
  is set.
- **the LLM Developer API**: just `GOOGLE_API_KEY` (from your provider console).

The client auto-selects the provider when `LLM_USE_CLOUD` is truthy, else
the Developer API key.

## Email (`reports/email.py`)

`send_report(subject, body)` over SMTP STARTTLS. Config via env
(`SMTP_HOST/PORT/USER/PASS`, `REPORT_FROM`, `REPORT_TO`) — see `.env.example`.
Gmail needs an App Password.

## Usage

```bash
uv run python scripts/research_report.py RELIANCE --dry-run --shares 1353.2   # brief only, no creds
uv run python scripts/research_report.py RELIANCE --shares 1353.2             # + the LLM thesis
uv run python scripts/research_report.py RELIANCE --deep --shares 1353.2      # full forensic deep-dive
uv run python scripts/research_report.py RELIANCE --pdf transcript.pdf        # + read a filing
uv run python scripts/research_report.py RELIANCE --email                     # + email it
```

### Deep mode (`--deep`)

For an exhaustive fundamental + forensic review, `--deep` swaps the one-pager for
`reports/deep_brief.build_deep_brief` — full multi-year **Income Statement /
Balance Sheet / Cash Flow (CFO·CFI·CFF)** tables plus a complete derived layer:
margins, ROE/ROCE/ROIC/ROA, leverage, liquidity, working-capital &
cash-conversion days, **FCF / FCFF / FCFE**, CFO/PAT & CFO/EBITDA (incl. 3- and
5-yr rolled), the quarterly trend, and forensic scores with full component
breakdowns. The the LLM call uses a section-by-section forensic prompt and is
**output-uncapped**. (Order book/backlog isn't in XBRL — order-driven sectors
only; needs a PDF read.)

`--shares <crore>` corrects the current share count for a post-filing
bonus/split (see [`FUNDAMENTALS.md`](FUNDAMENTALS.md)).

## Status / follow-ups

- Brief + orchestration + `--dry-run` validated end-to-end on RELIANCE.
- the LLM synthesis + email are built and import-clean; **live runs need the
  the LLM/the provider env vars + `SMTP_*`** (user-supplied, not in repo).
- Follow-ups: auto-fetch the latest concall transcript / results PDF from the
  BSE announcement feed (so `--pdf` isn't manual); HTML email formatting;
  schedule via the nightly refresh; multi-stock watchlist digest.
