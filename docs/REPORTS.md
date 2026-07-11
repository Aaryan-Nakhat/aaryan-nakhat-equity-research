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
- **Vertex AI** (workplace GCP) via a **service account**:
  `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION`, and `GCP_SERVICE_ACCOUNT_FILE=./gcp-service-account.json`.
  The key file is **gitignored** (`gcp-service-account.json` / `*service-account*.json`).
  Falls back to `GOOGLE_APPLICATION_CREDENTIALS`, then to `gcloud` ADC if neither
  is set.
- **Gemini Developer API**: just `GOOGLE_API_KEY` (from aistudio.google.com).

The client auto-selects Vertex when `GOOGLE_GENAI_USE_VERTEXAI` is truthy, else
the Developer API key.

## Email (`reports/email.py`)

`send_report(subject, body, *, to, html, attachments, in_reply_to, references)`
over SMTP STARTTLS — supports an HTML alternative (`body_html()` reuses the PDF's
markdown→HTML so email and PDF look identical), PDF **attachments**, and
**threading** headers so replies thread under the request. Every sent message
carries an `X-EquityBot` header so the inbound reader skips the bot's own mail.
Config via env (`SMTP_HOST/PORT/USER/PASS`, `REPORT_FROM`, `REPORT_TO`) — see
`.env.example`. Gmail needs an App Password.

## Usage

```bash
uv run python scripts/research_report.py RELIANCE --dry-run --shares 1353.2   # brief only, no creds
uv run python scripts/research_report.py RELIANCE --shares 1353.2             # + Gemini thesis
uv run python scripts/research_report.py RELIANCE --deep --shares 1353.2      # full forensic deep-dive
uv run python scripts/research_report.py RELIANCE --pdf transcript.pdf        # + read a filing
uv run python scripts/research_report.py RELIANCE --deep --out reliance.pdf   # write charted PDF
uv run python scripts/research_report.py RELIANCE --email                     # full report in body + charted PDF
```

### Deep mode (`--deep`)

For an exhaustive fundamental + forensic review, `--deep` swaps the one-pager for
`reports/deep_brief.build_deep_brief` — full multi-year **Income Statement /
Balance Sheet / Cash Flow (CFO·CFI·CFF)** tables plus a complete derived layer:
margins, ROE/ROCE/ROIC/ROA, leverage, liquidity, working-capital &
cash-conversion days, **FCF / FCFF / FCFE**, CFO/PAT & CFO/EBITDA (incl. 3- and
5-yr rolled), the quarterly trend, and forensic scores with full component
breakdowns. The Gemini call uses a section-by-section forensic prompt and is
**output-uncapped**.

**Business overview (leads the report).** Before any table, the deep report opens
with a `## 🏢 Business overview` section — an LLM read of the company's own filings
(`synthesize.business_overview`, fed the same PDFs as the thesis): *what the company
does*, its *business segments with approximate revenue-mix %*, and *market cap / TAM /
penetration* (grounded in filings; market sizing kept approximate, never fabricated).
For **order-driven** sectors (`sector.is_order_driven` — EPC / capital goods / infra /
defence / IT services) it also surfaces the **order book / backlog + book-to-bill** from
the filings. The old hardcoded §14 "order book n/a" note (which fired for **every**
company regardless of type) is gone — §14 now carries a one-line order-book pointer
**only** for order-driven names. The overview is best-effort — omitted if there are no
filings to read. It also renders for symbols with **no XBRL financials** (REITs / InvITs,
newly listed / renamed): the report leads with the overview + a technical snapshot instead
of a bare "no financials" message.

`--shares <crore>` corrects the current share count for a post-filing
bonus/split (see [`FUNDAMENTALS.md`](FUNDAMENTALS.md)).

### Quant valuation & statistical forensics (`analysis/quant.py`)

The deep brief also carries a quant layer (numpy-only, assumption-driven):
- **Monte-Carlo DCF** — samples revenue growth, EBIT margin, WACC and terminal
  growth (anchored to the company's own history; WACC via CAPM with beta from
  `equity_eod` vs Nifty 50) over ~20k FCFF paths → an intrinsic-value/share
  **distribution**: median, p10–p90, **margin of safety** and **P(undervalued)**.
  Growth fades to terminal and `wacc−tg` is floored, so terminal value can't
  explode. **Skipped for banks/NBFCs** (FCFF-DCF inappropriate — flagged).
  *Guards (so a noisy stock never prints garbage):* **beta is capped to [0.4, 2.0]**
  (a raw regression beta can spike to ~3 and pin WACC at its ceiling); the growth
  assumption **blends the long-run CAGR with recent TTM momentum** so a historically
  fast grower that's currently shrinking isn't handed the +25% cap.
- **Reverse DCF** — the growth today's price implies (bisection), vs history.
- **Scenario DCF** — bear/base/bull point values; if the central case isn't a
  positive value (high-beta / cyclical / capex-heavy inputs drive modelled FCFF
  negative) the section prints **"not meaningful"** rather than a negative number.

**How the report frames valuation (§10–§11).** Because a point-estimate FCFF-DCF is
unreliable for Indian cyclicals/financials/capex-heavy names, valuation leans on
*relative + sector-appropriate + forward*, not the DCF:
- **§10 picks a lens by sector** (`sector.valuation_lens`): **financial → P/B on ROE**;
  **cyclical/asset-heavy → EV/EBITDA** (`valuation.ev_ebitda`, with a **mid-cycle** variant
  so peaks/troughs don't mislead) + P/B; **everything else → P/E**. It also shows the current
  multiple as a **percentile of the stock's own history** (`valuation.multiple_percentile`),
  and — when management gave explicit guidance — a **forward multiple**
  (`synthesize.extract_guidance`: a Gemini read of the concalls → guided revenue/EBITDA/PAT →
  forward EV/EBITDA / P/E, threaded in via `pipeline.generate_report`).
- **§11 leads with the reverse-DCF** ("what perpetual growth the price implies vs history →
  plausible/demanding") as the centrepiece; the **Monte-Carlo FCFF-DCF is only a secondary
  cross-check**, shown only when its inputs are meaningful.
- **Benford's law** — first-digit conformity (MAD) of all reported figures, a
  manipulation/rounding tell.
- **Sector z-scores** + a **peer-comparison table** — target ◄ vs sector peers on
  P/E, P/B, ROE, ROCE, net margin, D/E (all sanity-bounded in `quant._ratios`, so
  holding-co distortions like a >100% standalone net margin show `n/a`).

Plus new forensic metrics in the brief: **Sloan (balance-sheet) accruals** and
**promoter-pledge %** (NSE pledge feed → `shareholding` table; pledge-of-promoter
is `n/a` for no-promoter firms where it would be meaningless).

**Insider & promoter trades:** an `### Insider & promoter trades (recent)` block
(`deep_brief._insider_block`, after the pledge bullets) lists the latest SEBI PIT
Reg 7(2) disclosures (`insider_trades` table, ingested on demand in `ensure_ingested`)
with a one-line **net read** over the last 6 months (promoter/director + open-market
buys − sells → "net buyers ₹X cr — conviction" vs "net sellers — caution").

**Auto multi-filing read:** `generate_report` auto-fetches **all the company's
meaningful filings since the last fiscal year-end (plus the latest results)** —
results, concall transcripts, investor presentations, ratings, M&A, etc.
(`pipeline._filings_for_analysis`, richest-first, capped ~12 docs / 15 MB to stay
under Gemini's inline limit) and feeds them all to Gemini, so every on-demand
report folds in management guidance + contingent-liability / related-party notes.
Generic — works for any NSE symbol (new or famous). No manual `--pdf` needed.

**Data freshness (auto-refresh):** `pipeline.ensure_ingested` is freshness-aware —
it re-ingests the latest quarterly + annual filings whenever the newest stored
quarter is older than the quarter that should already be filed (≈75-day SEBI lag),
not just when the symbol is empty. A per-symbol 2-day cooldown (in `alert_state`)
stops repeat requests from re-hitting NSE. The promoter-pledge snapshot refreshes
the same way when older than ~80 days. (Ingests are idempotent upserts, so this
re-lands the latest and appends any new period.) This is what keeps the statement
tables current instead of frozen at the first-seen fiscal year.

**Annual P&L columns:** the §1 Income statement and §2 margins tables show the
fiscal years only. (A trailing-12-month **TTM** column was removed — with financials
current it just duplicated the latest FY, adding a column of no information.) Balance
sheet / cash flow stay annual (quarterly XBRL carries no BS/CF). The §8 quarterly-trend
label reflects the **actual** quarter count. P/E(TTM) in the valuation lens is unrelated
— that's the trailing-twelve-month earnings yield, still shown.

**Real peer table:** before building a deep report, `pipeline._ensure_peer_financials`
best-effort ingests **annual** financials for up to ~6 same-sector peers that lack
them (cached after), so §10's peer comparison shows real comparables instead of the
one-or-two stocks that happened to be ingested. When fewer than 3 peers have a
comparable P/E, the sector-percentile line is replaced by an "insufficient peer
data" note (the peer table still renders).

**Consolidated vs standalone:** `generate_report(consolidated=None)` **defaults to
consolidated whenever it exists** — the whole group (parent + subs + JVs) is the
economically complete, industry-standard primary lens. It falls back to standalone only
when consolidated is unavailable, or when consolidated's XBRL history is ≥2 years thinner
than standalone's (don't trade a complete *entity* for an incomplete *history*). This
keeps the statement tables aligned with the (consolidated-based) business overview /
thesis — e.g. EIEL, whose renewables growth sits entirely in acquired subsidiaries, now
shows consolidated tables instead of standalone ones that omit that business. Override by
putting **"consolidated"/"standalone"** in the email subject (`email_bot._basis`).

### Charts in the PDF (`reports/charts.py`)

The PDF embeds **fundamental** charts (matplotlib → PNG → base64 `<img>`):
revenue/PAT + margin, **CFO-vs-PAT** (cash quality), ROE/ROCE/ROIC, leverage +
interest cover, FCF/FCFF, and the **Monte-Carlo fair-value histogram**.
`pdf.report_to_pdf(md, images=…)` appends them as a Charts section.

### Self-explaining numbers (`reports/glossary.py`)

Every headline metric is annotated so the report stands on its own:
- **Inline band tags** on forensic/quant/pledge lines (e.g. `ROCE 9.5% — weak`,
  `pledge 2.4% — good`, `margin of safety 19% — some`) via `glossary.read/label`.
- **"How to read this" explainer blocks** close each of §10 Valuation, §11
  reverse-DCF, §12 statistical forensics and §13 technical snapshot — multi-line,
  bolded definitions of every metric shown (P/E · P/B · earnings yield · EV/EBITDA ·
  own-history percentile · forward multiple; reverse-DCF · Monte-Carlo range · margin
  of safety · WACC · terminal growth; Benford MAD · sector z-scores; SMA · RSI ·
  golden/death cross) so a non-expert can read the numbers without the separate guide.
- A standalone **Metrics & ratings guide** — what each metric is (typical values,
  sector caveats) **plus the categorical outputs and their possible values**: the
  **Verdict** scale (Buy / Accumulate / Hold / Reduce / Avoid), why a Movers P/E
  shows `n/a`, and the corporate-event types. Built once and cached
  (`glossary.guide_pdf`) and attached to report emails as a **separate
  `Metrics_and_ratings_guide.pdf`** (not in the report body/PDF).
- The Gemini prompt is told to explain each metric it cites and judge it **for this
  company's sector/business model** (a vanilla DCF understates a true compounder;
  utilities run lower ROCE; etc.).
- **Alert bodies** carry the same plain-English reading (what the number means +
  the threshold that matters).

## Telegram bot (interactive, on-demand)

`scripts/telegram_bot.py` — message a company name, get a deep report back.

```
You: "Adani Power"  ──►  resolve (Gemini + Google Search) ──►  one match? run it
                                                          └─► several? buttons → you pick
   ──►  ensure-ingested (on demand) ──►  deep brief ──►  Gemini forensic ──►  reply (formatted inline + PDF)
```

- **Resolver** (`reports/resolve.py`): Gemini + Google-Search grounding maps free
  text → exact NSE symbol(s). Returns **one** when certain, **up to 5 ranked**
  otherwise (handles small-cap / newly-listed names, not just a fixed universe).
- **Pipeline** (`reports/pipeline.py`): `generate_report(symbol, deep=…)` —
  ingests financials on demand for any NSE symbol, builds the brief, runs Gemini.
- **Reply formatting**: the analysis is sent inline as **Telegram MarkdownV2**
  (via `telegramify-markdown` — bold, bullets, emojis, tables as aligned monospace
  blocks; plain-text fallback if a chunk won't parse), and the full report is
  attached as a **styled PDF** (`reports/pdf.py`: markdown → HTML → landscape-A4
  via the installed Playwright Chromium; falls back to a `.md` file on failure).
- **Security**: only `TELEGRAM_ALLOWED_USERS` (numeric IDs) are served; the bot
  token lives in `.env`. Add `consolidated` to a message for the group view.

Setup: create a bot via **@BotFather** (`/newbot`) → token; get your ID from
**@userinfobot**; put both in `.env`; then `uv run python scripts/telegram_bot.py`
(keep it running, or schedule it). The genai client is a per-process singleton
(creating several closes the shared httpx transport).

### Always-on (Windows Task Scheduler)

`scripts/run_bot.ps1` loads `.env` and runs the bot in an auto-restart loop. It's
registered as scheduled task **`EquityResearchTelegramBot`** (trigger: at logon;
restarts on failure). The bot logs to `data/processed/telegram_bot.log`; launcher
restart markers go to `data/processed/bot_launcher.log`.

```powershell
Start-ScheduledTask  -TaskName EquityResearchTelegramBot   # start now
Stop-ScheduledTask   -TaskName EquityResearchTelegramBot   # stop
Get-ScheduledTask    -TaskName EquityResearchTelegramBot   # state
Get-Content data\processed\telegram_bot.log -Tail 20 -Wait # live log
```

Re-register from scratch: see the `Register-ScheduledTask` call in the project
history, or just run `scripts/run_bot.ps1` manually in a terminal.

## Email channel (`scripts/email_bot.py`) — Telegram-blocked fallback

When Telegram is unreachable (some Indian ISPs IP-block `api.telegram.org`), the
**email channel** delivers the exact same brains over email instead. Selected by
the **`CHANNELS`** env flag (`email` | `telegram` | `telegram,email`); the
Telegram code stays intact and revives with `CHANNELS=telegram`.

```
PULL  you email a stock name (Subject) from an allowlisted address
        │  IMAP IDLE wakes the bot (reports/inbox.py — no polling)
        ▼  resolve → one match runs; several → "which one?" reply, you reply a number
        ▼  instant ack → reply in-thread: the FULL deep report in the body
           + the same report (tables + charts) as the attached PDF
           + a "reply 1) growth triggers" deeper-cut menu (opt-in follow-ups)
PUSH  >=18:00 IST, once per trading day → run_watchlist_scan → digest email:
        Upcoming events + per-stock Movers + Events (deals / corporate events /
        forensic changes, with inline filing analysis). Lines-only, NO PDFs.
        Holiday/weekend-skipped.
```

- **Inbound** (`reports/inbox.py`): one Gmail account both sends and reads. IMAP
  **IDLE** waits for mail (no minute-by-minute polling); on arrival it fetches
  UNSEEN messages, keeps only those `From:` an address in `EMAIL_ALLOWED_SENDERS`
  (auth), and skips its own (`X-EquityBot`) replies. IDLE is re-armed each loop
  (Gmail drops it ~29 min), which also serves as the daily-scan heartbeat.
- **Disambiguation** is *ask-first*: ambiguous names get a numbered reply; your
  numeric reply is matched to the pending candidates (stored in `alert_state`
  under `__email__`, 24h TTL) and the chosen report is sent.
- **Deeper-cut menu** (opt-in follow-ups): every deep report ends with a numbered
  menu; replying with a number runs that deeper analysis *for the same stock*,
  in-thread. Today: **`1) Growth-triggers 1-pager`** —
  `pipeline.generate_growth_triggers` → `synthesize.growth_triggers`, a
  forward-looking catalysts note (5–7 concrete triggers, each quantified +
  timeline + **HIGH / MEDIUM / OPTIONALITY** conviction tag, a "what's in the
  price" read, risks, and a scoreboard table). It's **grounded in the same primary
  filings** the deep report reads (concalls / investor presentations / results —
  *not* the open web), and its Section-1 snapshot is injected from the deterministic
  numbers (`_snapshot_facts`: mcap/CMP/TTM revenue & margins/ROE/ROCE/P·E/P·B/promoter
  holding) so they're exact. Delivered as **email body + a text PDF**. The menu is
  armed via the same numbered-reply state (`_set_followup` → `GT:<SYM>` items), and
  is **extensible** — add a row + a prefix branch for the next cut (bear case, etc.).
- **Config**: `CHANNELS`, `IMAP_HOST/PORT/USER/PASS`, the existing `SMTP_*` /
  `REPORT_FROM` / `REPORT_TO`, and `EMAIL_ALLOWED_SENDERS`. Send requests *from*
  a different address you own (e.g. work) *to* the bot's Gmail, so requests never
  blur with notes-to-self.

Always-on: `scripts/run_email_bot.ps1` (auto-restart loop, mirrors the Telegram
launcher) → scheduled task **`EquityResearchEmailBot`**. Bot logs to
`data/processed/email_bot.log`; launcher markers to `email_launcher.log`.

## Fund report (`reports/fund_brief.py`) — mutual funds

The fund-side analogue of the stock deep-brief. Email **`fund: <name>`** (or `mf: <name>`)
in the subject → the bot resolves the name to an AMFI Direct-Growth scheme
(`resolve_fund`, token-AND match, disambiguation reuses the "which one?" pending UX with an
`MF:` tag), backfills its NAV history on demand via the AMC-code map
(`ingest.backfill_mf_scheme_history`), and renders a deterministic markdown report from
`analysis/funds.py`:

- **Returns** — trailing (CAGR ≥1y, absolute <1y) incl. since-inception.
- **Risk** — annualised vol, Sharpe, Sortino, max drawdown (from daily NAV).
- **Rolling 1-year** returns (worst/median/best — a consistency read).
- **Category percentile** — rank vs same-category Direct-Growth peers (best-effort; needs
  peer history, so thin until a broad backfill).
- **Portfolio** (where `mf_holdings` coverage exists — PPFAS live): # holdings, top-10
  concentration, biggest sector, top holdings, and **overlap with the user's stock
  watchlist** (which of *your* names the fund holds, by weight).

**Attachments** (mirrors the stock report): a **charted PDF** (`charts.fund_charts` — NAV
growth of ₹100 rebased + the rolling-1-year return distribution → `report_to_pdf`) and a
**mutual-fund metrics guide PDF** (`glossary.fund_guide_pdf` — plain-English on CAGR, Sharpe,
Sortino, max drawdown, rolling returns, concentration, overlap). PDF is best-effort with a hard
timeout (`_fund_pdf`); on failure the report still goes out body-only.

No LLM call yet (deterministic); a Gemini fund thesis + expense/AUM/manager lines are the
remaining enrichments (`docs/PLAN.md` → Mutual-fund module).

## Status / follow-ups

- Brief + orchestration + `--dry-run` validated end-to-end on RELIANCE.
- Gemini synthesis + email are built and import-clean; **live runs need the
  Gemini/Vertex env vars + `SMTP_*`** (user-supplied, not in repo).
- Follow-ups: auto-fetch the latest concall transcript / results PDF from the
  BSE announcement feed (so `--pdf` isn't manual); HTML email formatting;
  schedule via the nightly refresh; multi-stock watchlist digest.
