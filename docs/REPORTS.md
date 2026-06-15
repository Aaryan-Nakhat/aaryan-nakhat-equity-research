# Reports — Claude synthesis + email (Phase 4)

The capstone: assemble every quant signal into one **analytical brief**, have
**Claude** turn it (plus an optional filing PDF) into a structured investment
thesis, and **email** the result. `src/equity_research/reports/`.

## Pipeline

```
brief.build_brief(con, symbol)        # deterministic — all primary-source signals
        │   (fundamentals · forensic · technicals · valuation · sector)
        ▼
synthesize.synthesize_thesis(brief)   # Claude (opus-4-8) — qualitative read + verdict
        │   + optional concall/annual-report PDF (Files API)
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

`claude-opus-4-8`, adaptive thinking, streaming. System prompt = a sober Indian-
equity analyst told to ground every claim in the brief, respect `n/a`/caveats,
and emit a 4-part note (Verdict · Why · Risks · What to watch). An optional PDF
(concall transcript / annual report) is uploaded via the Files API and read
alongside the brief — this is where management commentary enters the thesis.

Needs `ANTHROPIC_API_KEY`.

## Email (`reports/email.py`)

`send_report(subject, body)` over SMTP STARTTLS. Config via env
(`SMTP_HOST/PORT/USER/PASS`, `REPORT_FROM`, `REPORT_TO`) — see `.env.example`.
Gmail needs an App Password.

## Usage

```bash
uv run python scripts/research_report.py RELIANCE --dry-run --shares 1353.2   # brief only, no creds
uv run python scripts/research_report.py RELIANCE --shares 1353.2             # + Claude thesis
uv run python scripts/research_report.py RELIANCE --pdf transcript.pdf        # + read a filing
uv run python scripts/research_report.py RELIANCE --email                     # + email it
```

`--shares <crore>` corrects the current share count for a post-filing
bonus/split (see [`FUNDAMENTALS.md`](FUNDAMENTALS.md)).

## Status / follow-ups

- Brief + orchestration + `--dry-run` validated end-to-end on RELIANCE.
- Claude synthesis + email are built and import-clean; **live runs need
  `ANTHROPIC_API_KEY` + `SMTP_*`** (user-supplied, not in repo).
- Follow-ups: auto-fetch the latest concall transcript / results PDF from the
  BSE announcement feed (so `--pdf` isn't manual); HTML email formatting;
  schedule via the nightly refresh; multi-stock watchlist digest.
