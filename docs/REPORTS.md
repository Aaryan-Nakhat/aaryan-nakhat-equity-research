# Reports — Gemini synthesis + email (Phase 4)

The capstone: assemble every quant signal into one **analytical brief**, have
**Gemini** (via Vertex AI or the Gemini Developer API) turn it (plus an optional
filing PDF) into a structured investment thesis, and **email** the result.
`src/equity_research/reports/`.

## Pipeline

```
brief.build_brief(con, symbol)        # deterministic — all primary-source signals
        │   (fundamentals · forensic · technicals · valuation · sector)
        ▼
synthesize.synthesize_thesis(brief)   # Gemini (gemini-2.5-pro) — qualitative read + verdict
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

Gemini via the `google-genai` SDK (`gemini-2.5-pro` by default, override with
`GEMINI_MODEL`), streaming. System prompt = a sober Indian-equity analyst told to
ground every claim in the brief, respect `n/a`/caveats, and emit a 4-part note
(Verdict · Why · Risks · What to watch). An optional PDF (concall transcript /
annual report) is passed inline (`types.Part.from_bytes`) and read alongside the
brief — this is where management commentary enters the thesis.

**Auth (env, see `.env.example`) — two options:**
- **Vertex AI** (workplace GCP): `GOOGLE_GENAI_USE_VERTEXAI=true`,
  `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, plus ADC
  (`gcloud auth application-default login`) or a Vertex express API key in
  `GOOGLE_API_KEY`.
- **Gemini Developer API**: just `GOOGLE_API_KEY` (from aistudio.google.com).

The client auto-selects Vertex when `GOOGLE_GENAI_USE_VERTEXAI` is truthy, else
the Developer API key.

## Email (`reports/email.py`)

`send_report(subject, body)` over SMTP STARTTLS. Config via env
(`SMTP_HOST/PORT/USER/PASS`, `REPORT_FROM`, `REPORT_TO`) — see `.env.example`.
Gmail needs an App Password.

## Usage

```bash
uv run python scripts/research_report.py RELIANCE --dry-run --shares 1353.2   # brief only, no creds
uv run python scripts/research_report.py RELIANCE --shares 1353.2             # + Gemini thesis
uv run python scripts/research_report.py RELIANCE --pdf transcript.pdf        # + read a filing
uv run python scripts/research_report.py RELIANCE --email                     # + email it
```

`--shares <crore>` corrects the current share count for a post-filing
bonus/split (see [`FUNDAMENTALS.md`](FUNDAMENTALS.md)).

## Status / follow-ups

- Brief + orchestration + `--dry-run` validated end-to-end on RELIANCE.
- Gemini synthesis + email are built and import-clean; **live runs need the
  Gemini/Vertex env vars + `SMTP_*`** (user-supplied, not in repo).
- Follow-ups: auto-fetch the latest concall transcript / results PDF from the
  BSE announcement feed (so `--pdf` isn't manual); HTML email formatting;
  schedule via the nightly refresh; multi-stock watchlist digest.
