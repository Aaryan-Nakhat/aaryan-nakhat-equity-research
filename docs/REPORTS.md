# Reports — LLM synthesis + email (Phase 4)

The capstone: assemble every quant signal into one **analytical brief**, have
**LLM** (via Vertex AI or the Developer API) turn it (plus an optional
filing PDF) into a structured investment thesis, and **email** the result.
`src/equity_research/reports/`.

## Pipeline

```
brief.build_brief(con, symbol)        # deterministic — all primary-source signals
        │   (fundamentals · forensic · technicals · valuation · sector)
        ▼
synthesize.synthesize_thesis(brief)   # LLM (gemini-2.5-pro) — qualitative read + verdict
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

LLM via the `google-genai` SDK (`gemini-2.5-pro` by default, override with
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
- **Developer API**: just `GOOGLE_API_KEY` (from aistudio.google.com).

The client auto-selects Vertex when `GOOGLE_GENAI_USE_VERTEXAI` is truthy, else
the Developer API key.

**Shared house-style (`synthesize._FORMATTING`):** one formatting block is appended to **all
four** long-form prompts — the deep stock analysis, the IPO note, the fund note and the
growth-triggers 1-pager — so the rendering rules are identical everywhere and a fix lands in
one place. It requires **bolded key terms/figures**, a few **tasteful emojis** on headings and
signals (🎯 verdict · 📈 growth · ⚠️/🔴 flags …), and — the two real render failures it
targets — that every **table** be a standalone markdown block with a blank line before/after
(never inline in a sentence) and every **list** be a real markdown list with each item on its
own line (never `1. … 2. …` strung into one paragraph, which renders as an unreadable run-on).

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
uv run python scripts/research_report.py RELIANCE --shares 1353.2             # + LLM thesis
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
breakdowns. The LLM call uses a section-by-section forensic prompt and is
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

**Stale-share-count flag (auto-detected).** Rather than only warning generically that a
corporate action *could* have happened, the deep report now **checks**: `pipeline._detect_share_action`
pulls the symbol's NSE corporate-action feed (`nse_api.corporate_actions_symbol`) and
`valuation.detect_share_action` finds the most recent **bonus/split whose ex-date falls *after* the
FY-end of the share count** used for market cap (dividends/buybacks are ignored — they don't change
the count). If one is found, §10 leads with a **⚠️ "share count may be stale"** banner naming the
action, its ratio and ex-date, and §11 repeats a one-line DCF caveat — because the market cap, P/E,
P/B and the per-share DCF are all computed on the pre-action count until the next annual XBRL is filed.
**The numbers are still computed and shown unchanged** (the flag is an indicator, not a suppressor);
resend with an explicit share count to correct them. Self-clearing: once the annual filing that
reflects the action is ingested, the ex-date is no longer *after* the FY-end, so the banner disappears
with no false positives (verified on BAJFINANCE: flagged against FY-2025, silent against FY-2026).

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
  (`synthesize.extract_guidance`: an LLM read of the concalls → guided revenue/EBITDA/PAT →
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

**Shareholding — who actually owns it:** a `### Shareholding` block
(`deep_brief._shp_block`) renders the **holder-level shareholding pattern** from the
company's latest SEBI Reg-31 SHP XBRL (`scrapers/nse_shp.py` → `shp_holders` table):
every promoter/promoter-group account plus every public >1% holder, **sorted
highest→lowest**, each tagged with **what the holder is** — 👤 individual/HUF ·
🏛 **LISTED company (with its NSE symbol)** · 🔒 unlisted pvt company · 🏦 MF /
insurance / bank · 🌍 FPI · 🤝 trust. Listed-vs-unlisted is resolved by
normalised-name match against the full NSE listed master (`equity_master`, from
EQUITY_L.csv, ~2,400 names, monthly-refreshed). A 🎯 **"Elcid pattern"** callout
tops the section whenever a holder is itself a listed company (the way ELCIDIN held
~2.95% of Asian Paints) — the exact situation where a tiny listed vehicle can
massively re-rate on its stake value. Ingested with the quarterly ownership refresh,
plus a cooldown-free backfill for symbols that predate the feature.

**Ownership changes (quarter-over-quarter):** an `### Ownership changes` block
(`deep_brief._ownership_changes_block` → `analysis.ownership.ownership_changes`) diffs the
**two most recent SHP snapshots** and reports who **entered / added / trimmed / exited**,
with the percentage-point move — **notable holders first** (⭐ promoter / mutual fund / FPI /
insurer / listed-company holder = real conviction or distribution, above retail churn). Every
line shows the **explicit transition** — `0.00% → 3.57%` for an entrant, `2.09% → 0.00%` for
an exit, `x% → y%` for a move — never a bare "was/now". Matching across quarters is
**name-normalised** (case/punctuation-insensitive) with a conservative **token-subset** and a
**re-spelling** fallback (same promoter-flag + category, a shared ≥4-char surname/AMC token,
near-identical stake), so the *same* holder disclosed under a slightly different label —
`LIFE INSURANCE CORPORATION OF INDIA` vs `Life Insurance Corporation of India`, or a promoter
whose name the filing re-spells (`K NITYANANDA REDDY` → `KAMBAM NITHYANANDA REDDY`) — reads as
one continuous move, not a spurious exit-and-re-entry in both lists.
`ensure_ingested` keeps ≥2 quarters per symbol: a name with fewer gets a 4-quarter
**backfill** (`ingest_shp_history` → `nse_shp.all_quarters`, parsing each quarter's SHP XBRL),
so the diff works on the first report rather than after waiting a quarter. (Verified live:
Asian Paints Mar→Jun 2026 → SBI MF trimmed 5.20%→4.31%, ICICI Pru trimmed, UTI MF exited.)

**Auto multi-filing read:** `generate_report` auto-fetches **all the company's
meaningful filings since the last fiscal year-end (plus the latest results)** —
results, concall transcripts, investor presentations, ratings, M&A, etc.
(`pipeline._filings_for_analysis`, richest-first, capped ~12 docs / 15 MB to stay
under the LLM's inline limit) and feeds them all to the LLM, so every on-demand
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
data" note (the peer table still renders). The table (`deep_brief._peer_comparison`) is
**grouped into large / mid / small-cap tiers** (≥ ₹50,000 cr · ₹10,000–50,000 cr · < ₹10,000 cr,
bands shown inline), up to 5 peers per tier ranked by market cap plus a **M-cap column**, and
lists each peer by **readable company name** (from `sector_map`/`equity_master`), not ticker —
the target marked ◄ in its own tier.

**Peers are granular, not macro-sector.** Peers come from `sector.peers`, which groups on
NSE's **`basic_industry`** (e.g. `Gems Jewellery And Watches`, `Private Sector Bank`,
`Refineries & Marketing`) — the fine tier — falling back to the coarse macro `industry` only
where the granular tag isn't yet enriched. Without this, a jeweller was compared against all of
`Consumer Durables` (paints, ACs, footwear, ceramics) and a bank against all `Financial
Services` (NBFCs, insurers, AMCs) — so the peer table, the §10 sector-percentile, and the §12
z-scores were all diluted by unrelated names. The `basic_industry` column is enriched from NSE's
`getSymbolData.secInfo` via `ingest_basic_industries` (`scripts/backfill_basic_industry.py`,
one-time/idempotent); the valuation **lens** (P/E vs P/B vs EV/EBITDA) still keys off the macro
`industry` (`sector.industry_of`), whose keyword lists are macro-level.

**Trading levels & setup (computed, no LLM).** After the LLM Analysis, the deep report appends
a **Trading levels & setup** section (`technical.levels` → `deep_brief.render_levels`) — the
actionable "where", derived entirely from the daily OHLCV. It **leads with an "In plain English"
narrative** (`deep_brief._levels_narrative`) that reads the structure, where price sits vs its
floor/ceiling, and the setup in rupee terms ("risk ₹X to make ₹Y"), so a non-technical reader
gets the story before the reference tables; the confluence "built from" column uses plain words
(`the 20-day average`, `a heavy-volume price band`), not jargon. Then the detail:
- **Support/resistance zones** built from the **confluence** of several methods — swing pivots,
  the 20/50/200-DMA, 52-week extremes, **volume-by-price** nodes, and round numbers — clustered
  (center-bounded, so a dense chain never drifts into one giant band) and scored by how many
  methods agree (shown as ●○ confluence dots). A zone many methods share is a stronger level
  than any single line.
- **Market structure** (higher-highs/lows vs lower-highs/lows, last swing points), **trendlines**,
  and best-effort **patterns** (range, double top/bottom, head-&-shoulders, triangles) — heuristic
  context, each with a confidence, never a standalone signal.
- A **reward:risk-framed setup** — accumulation zone (the *strongest* support, not merely the
  nearest) · **stop** (invalidation, ~1×ATR below the zone) · **first target** (nearest resistance)
  · **RR**; called "accumulate" only when RR ≥ ~1.5, else "watch". It is **verdict-aware**: the
  section is placed *after* the Analysis and reads the thesis verdict
  (`deep_brief.verdict_from_text`), so for an **Avoid/Reduce/Sell** call it shows the levels
  **for reference only** — never a buy setup that contradicts the fundamentals. Thin/volatile or
  freshly-listed names degrade to an honest "limited history" note.
- The PDF carries an **annotated candlestick chart** (`charts.levels_chart`, the 7th chart in
  `report_charts`) — last ~180 sessions with the zones shaded (green support / red resistance) and
  the 50/200-DMA. The chart draws **zones only** (verdict-neutral facts); the verdict-aware
  entry/stop/target lives in the text section.

**`levels: <name>` command (on-demand, no LLM).** A quick technical read outside a full report:
`email_bot._send_levels` resolves the name, computes the same levels, and replies with the section
+ an annotated chart (here the chart *does* draw the entry/stop/target overlay, since no
fundamental verdict is being claimed). Aliases: `technical:`, `setup:`, `chart:`. Also surfaced as
watchlist **level alerts** in the digests (see `docs/ALERTS.md`).

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
  reverse-DCF, §12 statistical forensics and §13 technical snapshot. §10's **valuation lens**
  now prints **each multiple on its own line** (P/E · P/B · earnings yield, plus EV/EBITDA for
  cyclicals and ROE for financials) with a plain-English gloss and a cheap/dear cue right next
  to the value; the §10–§13 explainers themselves are written as **flowing prose paragraphs**
  (not bullet lists) covering own-history percentile · forward multiple; reverse-DCF ·
  Monte-Carlo range · margin of safety · WACC · terminal growth; Benford MAD · sector z-scores;
  SMA · RSI · golden/death cross — so a non-expert can read the numbers without the separate guide.
- A standalone **Metrics & ratings guide** — what each metric is (typical values,
  sector caveats) **plus the categorical outputs and their possible values**: the
  **Verdict** scale (Buy / Accumulate / Hold / Reduce / Avoid), why a Movers P/E
  shows `n/a`, and the corporate-event types. Built once and cached
  (`glossary.guide_pdf`) and attached to report emails as a **separate
  `Metrics_and_ratings_guide.pdf`** (not in the report body/PDF).
- The LLM prompt is told to explain each metric it cites and judge it **for this
  company's sector/business model** (a vanilla DCF understates a true compounder;
  utilities run lower ROCE; etc.).
- **Alert bodies** carry the same plain-English reading (what the number means +
  the threshold that matters).

## Telegram bot (interactive, on-demand)

`scripts/telegram_bot.py` — message a company name, get a deep report back.

```
You: "Adani Power"  ──►  resolve (LLM + Google Search) ──►  one match? run it
                                                          └─► several? buttons → you pick
   ──►  ensure-ingested (on demand) ──►  deep brief ──►  LLM forensic ──►  reply (formatted inline + PDF)
```

- **Resolver** (`reports/resolve.py`): LLM + Google-Search grounding maps free
  text → exact NSE symbol(s). Returns **one** when certain, **up to 5 ranked**
  otherwise (handles small-cap / newly-listed names, not just a fixed universe).
- **Pipeline** (`reports/pipeline.py`): `generate_report(symbol, deep=…)` —
  ingests financials on demand for any NSE symbol, builds the brief, runs the LLM.
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
           ▼  then a separate "want a deeper cut? reply 1) growth triggers" email
        │  other Subjects: `fund: <name>` · `ipo: ongoing|upcoming|<name>` ·
        │  `screen: value` (quality+forensic+cheap) · `screen: holdco` (Elcid-pattern
        │  discounts) · `screen: investors` (marquee-investor moves last quarter) ·
        │  `screen: technical` (strongest chart setups to buy — entry/stop/target) ·
        │  `sector: <name>` (top-down read on a sectoral index — trend + valuation vs
        │  own history + smart-money + best/cheapest names + supply chain; `sector: list`
        │  for options) · `sector: rotation` (all sectors ranked — leaders/laggards/
        │  value-turning; also pushed weekly Sat ≥18:00) · `suppliers: <company>`
        │  (smaller listed ancillaries feeding a marquee name) ·
        │  `investor: <name>` (one HNI's disclosed book + moves) ·
        │  `sell` | `raise` | `trim` (rank YOUR holdings weakest-hand-first — which
        │  to sell if you need cash) — each a numbered list; reply a number → deep report
PUSH  08:30–09:00 IST, once per trading day → premarket.build_premarket → ONE
        "🌅 Pre-market" email: GIFT Nifty implied open (vs Nifty-50 prev close),
        overnight US/Asia indices, India VIX + FII index-futures stance, latest
        headlines, and an LLM "overnight read". A before-the-open setup, not a
        trade call. Every input degrades independently; holiday/weekend-skipped.
PUSH  >=18:00 IST, once per trading day → run_watchlist_scan → digest email:
        Upcoming events + per-stock Movers + Events (deals / corporate events /
        forensic changes, with inline filing analysis). Lines-only, NO PDFs.
        Holiday/weekend-skipped.
PUSH  Saturday >=18:00 IST, once per ISO week → screen_digest → ONE "Screener
        movements" email: holdco / fundamental / investor **deltas only** vs the last
        run (nothing crossed a threshold → no email). Trigger-based, not a full dump.
```

- **Inbound** (`reports/inbox.py`): one Gmail account both sends and reads. IMAP
  **IDLE** waits for mail (no minute-by-minute polling); on arrival it fetches
  UNSEEN messages, keeps only those `From:` an address in `EMAIL_ALLOWED_SENDERS`
  (auth), and skips its own (`X-EquityBot`) replies. IDLE is re-armed each loop
  (Gmail drops it ~29 min), which also serves as the daily-scan heartbeat. The loop
  **drains UNSEEN every cycle, not only when IDLE fires** — IDLE just lowers latency,
  it's not the source of truth. (While a report generates for minutes the bot isn't
  idling, and Gmail's IDLE only reports mail arriving *during* its wait window; the
  unconditional drain guarantees a request sent while busy is still picked up on the
  next cycle, so you never have to resend.)
- **Disambiguation** is *ask-first*: ambiguous names get a numbered reply; your
  numeric reply is matched to the pending candidates (stored in `alert_state` under
  `__email__`, 24h TTL). Pending menus are **thread-scoped** — keyed by
  `(sender, email-thread)`, not sender alone — so several menus can be open at once
  (e.g. an `ipo: ongoing` list *and* a stock's "want a deeper cut?" prompt) and a
  numbered reply resolves against the thread it was sent in, never a stale one from
  another thread. (Thread identity = a hash of the References root; a lone live menu
  is the unambiguous fallback if a reply's threading headers are missing.)
- **One request = one Gmail thread**: every email in a flow (ack → report → deeper-cut
  menu → growth triggers) is sent with the **same subject** (`Re: <original>`) plus
  In-Reply-To/References — Gmail only groups a conversation when the subject matches,
  so the old decorated subjects ("… — growth triggers", "… — which one?") forked a new
  thread per email. The body headings carry the description instead.
- **Deeper-cut menu** (opt-in follow-ups): right **after** each deep report the bot
  sends a **separate short in-thread email** — "want a deeper cut? reply with the
  number" (`_send_followup_menu`); replying with a bare number runs that deeper
  analysis *for the same stock*, in-thread. (Kept separate rather than tacked onto the
  end of the long report so it's actually seen.) Today: **`1) Growth-triggers 1-pager`** —
  `pipeline.generate_growth_triggers` → `synthesize.growth_triggers`, a
  forward-looking catalysts note (5–7 concrete triggers, each quantified +
  timeline + **HIGH / MEDIUM / OPTIONALITY** conviction tag, a "what's in the
  price" read, risks, and a scoreboard table). Every trigger also carries an
  **estimated business impact in ₹ cr and %** — incremental annual revenue as ₹X cr
  (≈Y% of TTM revenue) and, where estimable, the potential market-cap impact %
  (with the multiple assumption stated) — and the scoreboard is **sorted by ₹ impact,
  not conviction**, with a closing "Priority read" that flags any MEDIUM/OPTIONALITY
  trigger whose impact ranks top-3, so a big medium-conviction trigger is never
  buried under small high-conviction ones. It's **grounded in the same primary
  filings** the deep report reads (concalls / investor presentations / results —
  *not* the open web), and its Section-1 snapshot is injected from the deterministic
  numbers (`_snapshot_facts`: mcap/CMP/TTM revenue & margins/ROE/ROCE/P·E/P·B/promoter
  holding) so they're exact. Delivered as **email body + a text PDF**. The menu is
  armed via the same numbered-reply state (`_set_followup` → `GT:<SYM>` items), and
  is **extensible** — add a row + a prefix branch for the next cut (bear case, etc.).
- **Phone-readable HTML**: the email body and the PDF share one renderer
  (`pdf.render_html`), which was print-tuned and therefore unreadable on a phone
  (no viewport → desktop-width render → zoom-out + sideways scroll). It now emits a
  **viewport tag** plus an `@media only screen and (max-width: 600px)` block that wraps
  table cells and fenced blocks and lets a genuinely wide table scroll inside its own
  `.tablewrap` box. `only screen` keeps all of it out of the **PDF**, which still prints
  A4-landscape with `nowrap` financial tables.
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
- **SIP / XIRR** (`funds.sip_returns`) — simulate ₹10k/month for 1/3/5y: invested vs value
  today and the **money-weighted XIRR** (what an SIP investor actually earns, ≠ lump-sum CAGR).
  Bisection XIRR, no scipy.
- **Versus its benchmark** (`funds.benchmark_metrics`, index chosen by category via
  `benchmark_for` — Nifty 50 / Midcap 150 / Smallcap 250 / 500): **alpha, beta, up/down
  capture, tracking error, information ratio**, on the gap-free overlap of daily NAV and
  `index_close`. Annualised endpoint-to-endpoint and trimmed to the dense recent stretch
  (`_dense_tail`) so a stray sparse index row can't manufacture a fake outlier day. **Index
  history is backfilled ~5y** via `ingest.backfill_index_history` (walks business days over the
  NSE `ind_close_all` archive, 404 = holiday, idempotent `only_missing`), so alpha/beta is a
  multi-cycle read; the daily scan keeps it current.
- **Category percentile** — rank vs same-category Direct-Growth peers (best-effort).
- **Portfolio** (where `mf_holdings` coverage exists — PPFAS · HDFC · Nippon India live): # holdings, top-10
  concentration, biggest sector, top holdings, **watchlist overlap**, and **month-over-month
  churn** (`funds.holdings_churn`) — what the manager **bought / exited / added / trimmed**
  between the two latest SEBI monthly disclosures, **equity-only** (CDs / T-bills / TREPS that
  roll over monthly are filtered via `_is_equity_holding`, else routine treasury drowns the signal).
- **Analysis** (`synthesize.fund_thesis`) — a **thorough, section-by-section** LLM note over the
  deterministic report, written in the same depth and *teach-as-you-go* style as the deep stock
  report (uncapped — no word limit): **verdict** (Buy / Accumulate / Hold / Switch / Avoid + who it
  suits) · what the fund/category actually is · returns-vs-category · risk (vol/Sharpe/Sortino/
  drawdown) · consistency (rolling-1y) · SIP-XIRR-vs-CAGR · benchmark (alpha-vs-beta, up/down
  capture, TE/IR) · portfolio (concentration, churn, watchlist overlap — flagging duplicated risk
  when the fund largely holds names you own directly) · risks to watch. For **every metric** it
  says what it measures, a healthy range, and what *this* value means for *this* fund's category.
  Best-effort — the report still ships numbers-only if it fails.

**Attachments** (mirrors the stock report): a **charted PDF** (`charts.fund_charts` — NAV
growth of ₹100 rebased + the rolling-1-year return distribution → `report_to_pdf`) and a
**mutual-fund metrics guide PDF** (`glossary.fund_guide_pdf` — plain-English on CAGR, Sharpe,
Sortino, drawdown, rolling returns, **SIP XIRR, alpha/beta, up/down capture, tracking error/IR,
churn**, concentration, overlap). PDF is best-effort with a hard timeout (`_fund_pdf`).

Still to add (`docs/PLAN.md` → Mutual-fund module): **expense ratio / AUM / manager** (needs new
scraping) and the **forensic look-through** (Altman/Piotroski across holdings — needs a bulk
financials ingest for the holdings' symbols).

## IPO pre-listing analysis (`scrapers/ipo.py` + `synthesize.ipo_analysis`)

Email **`ipo: ongoing`** or **`ipo: upcoming`** → a numbered list of live / forthcoming
issues (name · price band · dates · live subscription); reply with a number (or email
`ipo: <name>`) → a **pre-listing IPO note**. All **primary NSE sources** (no grey-market/GMP):

- **List + live subscription**: `/api/ipo-current-issue`, `/api/all-upcoming-issues?category=ipo`,
  and category-wise `/api/ipo-detail` (QIB/NII/RII), via the browser tier. `upcoming` is
  filtered to issues whose **RHP is already published** (`has_prospectus`, a cheap Range request).
- **Offer documents** (predictable per-symbol archive `nsearchives…/content/ipo/<DOC>_<SYM>.zip`):
  **RHP** (full prospectus), **RATIOS** (price-band ad → KPIs / valuation-at-band / listed-peer
  table), **ANCHOR** (anchor allotment). `ipo.documents` picks the right PDF from each zip,
  prioritised within an ~18 MB budget (RHP always kept) since a pre-listing company has **no XBRL**
  — the analysis is RHP-driven, not the deterministic quant engine.

`synthesize.ipo_analysis` (LLM reads the RHP + price-band ad + anchor doc, grounded on the
verified issue facts) produces a **thorough, section-by-section, teach-as-you-go note** (same
depth/readability as the deep stock report, uncapped): what the company does & how it makes money ·
**offer structure — a fresh-issue vs OFS split table** (₹ cr + % of offer, summing to 100%) then
what it signals (fresh → company funded = positive; heavy OFS → insiders exiting = caution) ·
restated financials *with the trajectory read* ·
**valuation at the band vs listed peers** (P/E · P/B · RoNW each explained) · use of proceeds ·
key risks · demand (subscription + anchor book) · **APPLY / AVOID / NEUTRAL verdict**. For every
metric it says what it measures and a healthy/normal range so a first-time applicant can follow. Delivered as **email body +
PDF**, alongside an **IPO metrics & terminology guide PDF** (`glossary.ipo_guide_pdf`, cached —
fresh-issue vs OFS, QIB/NII/RII, anchor investors, price band/lot, P/E-at-band · P/B · RoNW · NAV,
EBITDA/PAT/CFO, D/E · DSCR, contingent liabilities, RPTs, concentration, and the verdict scale;
it also states plainly that **GMP is excluded** as non-primary) — mirroring the stock and fund
reports' guides. Then the same **deeper-cut menu** (reply `1` → growth-triggers, here grounded in
the RHP via `IGT:` items + `generate_growth_triggers(ipo_mode=True)`). On-demand, no DB table.

> **Gotcha (fixed):** the RHP archive's folder is itself named `RHP_<SYM>/`, so matching the
> preferred keyword against the **full zip path** selected the first PDF in that folder — the
> boilerplate *General Information Document* — instead of the real prospectus. `_pick_pdf` now
> matches the **file name** (largest match wins), which is the difference between "not disclosed"
> and a full 3-year restated financial table.

## Screeners — idea generation (`analysis/screener.py`, `analysis/holdco.py`)

Email commands that **find** ideas (not just analyse named ones); each replies a **numbered list**
and a **number-reply runs that name's full deep report** (reusing the pending-menu UX — plain
`SYM` candidates → `_send_report`). On-demand **and** pushed weekly (see the digest below). Every
list is a **real markdown table** (`reports/md.table` → styled `<table>`), not the old code-fenced
text that collapsed on phones.

- **`screen: value`** (aliases `screen: quality`, bare `screen`) → `screener.fundamental_screen`
  ranks the Nifty-500 (symbols with ingested financials) on a composite: **40% quality**
  (Piotroski F), **35% forensic** (Altman-safe + Beneish-clean + low Sloan accruals + no pledge),
  **25% cheapness** (current P/E — P/B for financials via `sector.valuation_lens` — as a *low
  percentile of its own history*). Each pillar is rank-normalised across the scored set; a missing
  pillar maps to the median so one gap doesn't sink a name. Reuses `forensic.*` / `valuation.*` /
  `sector.*` — nothing re-derives numbers.
- **`screen: holdco`** → `holdco.holdco_discounts` generalises the **Elcid trade**: it inverts
  `shp_holders` into `holder → [(investee, pct)]` for holders that are themselves NSE-listed,
  values each stake (`pct% × investee market cap`, `valuation.market_cap`), sums to a **stake NAV**,
  and compares to the holder's **own market cap** → a **discount %**, deepest first. Counts only
  **disclosed listed** stakes (SHP promoter + public >1%); unlisted subsidiaries aren't valued (the
  email says so). Coverage grows with SHP ingested.
- **`screen: investors`** / **`investor: <name>`** → `analysis/investors.py` tracks a curated
  **25 marquee investors** (Rekha Jhunjhunwala, Mukul Agrawal, Vijay Kedia, Dolly Khanna, Akash
  Bhanshali, Sunil Singhania/Abakkus, …). Matching is **curated, not fuzzy** — each name carries
  hand-verified alias token-sets and a holder matches only when an alias is a *subset* of its name
  tokens (so 'Vijay Kedia' hits 'VIJAY KISHANLAL KEDIA' but 'damani' never hits 'CHOODAMANI').
  `screen: investors` lists **what every tracked name did last disclosed quarter** (entered / added /
  trimmed / exited, ≥0.5pp/0.5% floor); `investor: <name>` shows **one person's disclosed book +
  moves**. Only holders **named** in the SHP (≥~1%) are visible; an exit can be a full sale or a trim
  below the disclosure floor; coverage = SHP universe ingested.
- **`screen: smallcap`** (aliases `screen: capex`, `small-cap`) → `analysis/smallcap.py` finds **strong
  small-caps early, led by the capex cycle** — the thesis being that committed **capex leads revenue by
  1-2 years**, so a capex boom is the earliest structural tell before the P&L re-rates. Universe = every
  symbol with financials whose **market cap sits in ₹1,000–10,000 cr** and that discloses ≥2y of capex
  (the band, not an index tag, defines eligibility, so backfilled small-caps auto-appear). It **hard-gates
  the traps out first** (Altman near-distress · Beneish manipulator · promoter pledge >25% · revenue
  shrinking vs 2y ago), then ranks a composite: **30% capex cycle** (capex vs its own 3y base · capex ÷
  depreciation · rising capex/sales · self-funded = CFO covers capex) · **25% capital efficiency** (ROCE
  level *and* trend · EBITDA-margin expansion) · **20% cash & balance sheet** (CFO/PAT · low D/E · interest
  cover) · **15% forensic** (reuses `screener._forensic_raw`) · **10% smart money** (promoter holding · net
  institutional accumulation from the QoQ ownership diff). Each metric is rank-normalised within the cohort;
  **valuation is deliberately *not* weighted** (shown for context only — forcing "cheap" filters out the
  quality compounders this screen exists to catch early). Reply a number → that name's deep report.
  **Coverage note:** the real edge needs a small-cap universe — `backfill_universe.py --seed-smallcaps`
  lands **Nifty Smallcap 250 + Microcap 250** into `sector_map` and backfills them; until then the band
  is only the small end of the Nifty-500.
- **`screen: technical`** (aliases `setups`, `momentum`, `buys`, `chart`) → `analysis/technical_screen.py`
  ranks on **price action** instead of fundamentals — the strongest chart setups to *buy*, market-wide.
  Two stages so it fits the time budget: **(1)** score every **liquid** name (avg turnover ≥ ₹2 cr/day,
  20-session window) that has financials on a technical composite — **30% trend** (>200-DMA · 50>200) ·
  **25% relative strength vs Nifty** · **15% MACD** · **10% RSI-health** (rewards a constructive 55-60
  zone, penalises overbought — don't chase) · **10% breakout proximity** (near the 52w high) · **10%
  delivery** confirmation, each rank-normalised across the set; **(2)** walk the ranked list top-down,
  apply the **same trap gate as the small-cap screen** (Altman near-distress · Beneish manipulator ·
  pledge >25% — dropped) and build a **momentum-appropriate setup** from `technical.levels()`: entry at
  the **nearest** support (a shallow pullback — *not* `_setup`'s deepest zone, which for names near their
  highs reads as an impossible "buy 40% lower"), **stop** below it, **target** = next resistance, with
  **reward:risk**. Kinds: `accumulate` (R:R ≥ 1.5) · `breakout` (near highs, no overhead — trail) ·
  `watch` (thin R:R). **Universe is bounded to symbols with financials ingested** so the safety gate is
  real for every row (spans micro→large incl. Microcap-250; the liquidity floor drops the un-tradeable
  long tail anyway). Reply a number → deep report. **Honest caveat (in the email):** a candidate finder
  with *defined risk*, **not** a back-tested edge — short-term timing is the tool's least-proven area.

### Sell-priority advisor — `sell` / `raise` / `trim` (your holdings)

**`sell`** (aliases `raise`, `trim`) → `analysis/sell_advisor.sell_ranking` answers a different
question from the discovery screens: *of the stocks **I own**, which should I sell first if I need
cash?* It reads the `holding`-tagged watchlist entries and ranks them **weakest hand first** on a
**keep score 0-100** (higher = stronger hold), each signal rank-normalised **within your own book**
(so it's relative to your holdings, not the Nifty-500):

- **35% valuation headroom** — `upside` (DCF margin to fair value, `quant.monte_carlo_dcf`, measured
  vs *price* so it reads as a sane move-to-fair-value) **+** `cheapness` (current multiple as a low
  percentile of the stock's own history, reused from `screener._cheapness_raw`). Little upside /
  richly valued ⇒ sell first.
- **25% quality** — Piotroski F (`screener._quality_raw`).
- **20% forensic** — Altman Z · Beneish M · Sloan accruals · no pledge (`screener._forensic_raw`).
- **10% momentum** — 3-month relative strength vs Nifty (`technical.relative_strength`, converted
  from its ratio to an out/under-performance %). A laggard is easier to let go.
- **10% smart-money flow** — net institutional (MF/insurer/FPI/bank) QoQ accumulation from
  `ownership.ownership_changes`; institutions exiting ⇒ sell first.

Names bucket into **🔴 Sell candidate / 🟡 Trim if needed / 🟢 Keep** (weakest / middle / top third
of the scored book); holdings with **no ingested data** land last as **⚪ No data** (email the symbol
once to build its report). Reply a number → that holding's **full deep report** before you act (same
pending-menu UX). Reuses the deterministic analysis layer — nothing re-derives numbers.

**Version A (shipped) is merit only** — it deliberately knows nothing about your cost, P&L or tax.
**Version B (planned)** layers on **LTCG/STCG** and **"raise ₹X" sizing** once holdings carry
quantity + average cost + buy date. Framed as decision *support*; the call is the user's.

**Weekly digest** (`src/equity_research/screen_digest.py`, `email_bot.maybe_screen_digest`): once per
ISO week (Saturday ≥18:00 IST) the bot runs all three screens and emails **ONE "Screener movements"**
message with **only the deltas** vs the last run — a holdco newly discounted / widening ≥5pp, a stock
entering the top-15 or climbing ≥10 ranks, a tracked investor's fresh moves. Fingerprints live in
`alert_state` (`screen_fp_*`) and advance **only after a successful send** (so a delivery failure
re-surfaces the deltas); if nothing crossed a threshold, **no email**. The fundamental screen sorts
`(-composite, symbol)` so ranks are deterministic and the deltas don't jitter on ties.

**Seeding** (`scripts/backfill_universe.py`, one-time, idempotent/resumable): for **every universe
tagged in `sector_map`** + a curated holdco list it lands **financials** (`ingest_financials`/`_annual`
→ shares for market cap + forensic inputs) and **4 quarters of SHP** (`ingest_shp_history` → holder
tables for the holdco reverse-index and the ownership diff). Run it once (bot **stopped** first —
single-writer DuckDB): `uv run python scripts/backfill_universe.py` (`--holdcos-only` for a fast
holdco-only seed). **`--seed-smallcaps`** first lands **Nifty Smallcap 250 + Microcap 250** (via
`ingest_sector_map`; NSE's microcap CSV uses an underscore filename, handled by a fallback in
`nse_archives.fetch_constituents`) into `sector_map`, then backfills them — the genuine "before anyone
else" universe for `screen: smallcap` (Nifty-500 is all large/mid). Pair with **`--only-missing`** to
skip the already-ingested Nifty-500 and do just the ~500 new small-cap names. This is a multi-hour
browser-tier run — launch it as a Windows scheduled task with the bot stopped, per the ingest note.

## Pre-market digest (`reports/premarket.py`, `email_bot.maybe_premarket`)

A **before-the-open** read pushed once per trading day in the **08:30–09:00 IST** window (fires after
market open ~09:15 is pointless, so it's cut off at 09:00; `last_premarket_date` in `alert_state`
guarantees once/day; holiday/weekend-skipped via `scan.market_open_today`). It answers "what does the
overnight tape imply for how Nifty opens?".

Four **independent, best-effort** inputs (any can fail without sinking the email):
- **GIFT Nifty** (`scrapers/nseix.py::gift_nifty`) — the Nifty-50 future that trades ~21h/day on NSE IX
  (GIFT City), the market's best lead indicator for the domestic open. Plain-HTTP JSON
  (`nseix.com/api/derivatives-watch`, **no browser needed**); we take the nearest-expiry NIFTY FUTIDX.
- **Nifty-50 spot prev close + India VIX** (`scrapers/markets_global.py::nifty_reference`) — from NSE
  `allIndices` (plain HTTP, Camoufox fallback if Akamai blocks). Prev close is the **implied-gap baseline**.
- **Overnight US/Asia indices** (`markets_global.overnight_indices`) — S&P/Nasdaq/Dow + Nikkei/Hang Seng
  from Yahoo's unauthenticated `v8/finance/chart` endpoint.
- **Headlines** (`markets_global.market_headlines`) — Moneycontrol markets RSS (the feed emits malformed
  `&`-less numeric entities like `day#39;s`; `_clean_title` repairs them).

**Implied gap** = GIFT Nifty − Nifty-50 prev close → pts/% → bias bucket (`strong/mild gap-up`, `flat`,
`mild/strong gap-down` at ±0.15% / ±0.5%). The structured brief (global + gap + VIX + FII stance + the
headline list) is handed to `synthesize.premarket_brief` (Gemini, **uncapped** output) for a tight
"overnight drivers → likely open → what to watch" paragraph — grounded strictly in the numbers given,
never a trade call. The email closes with a plain-English **legend** (GIFT Nifty, implied gap, VIX, FII
net-long, overnight global) so no term is assumed. FII positioning reuses `analysis/positioning.py`.

## Sector analysis — `sector: <name>` (`analysis/sector_analysis.py`, `reports/sector_brief.py`)

A **top-down** read on a whole NSE sectoral index — the workbench's missing "is this sector one to
**enter / add to** now, and which names inside it are strongest / most **undervalued**?" lens. On-demand
email command (`sector: defence`, `sector: pharma`, `sector: realty`, … ; `sector: list` shows the ~20
covered sectors). A `_CATALOG` maps each canonical sector → its `index_close` index name + constituent
CSV slug + valuation lens + aliases (`resolve_sector` fuzzy-matches "defense"→defence etc.).

The report (all from data we already refresh daily):
- **Sector index technicals** — `index_technicals` computes SMA 20/50/200, RSI, MACD, ATR, **relative
  strength vs Nifty 50**, % from 52-wk high on the index's `index_close` OHLC series, reusing
  `technical.indicators_from_prices` (the shared stock/index indicator core).
- **Sector valuation** — `index_valuation` takes the index's own PE (PB for financial lenses) and its
  **percentile vs its own ~5-yr history** (cheap/expensive vs itself) + vs Nifty 50. This is the key
  "fairly valued / overvalued" read.
- **Smart-money proxy** — `smart_money` aggregates, across the sector's constituents: institutional
  ownership Δ (`ownership.ownership_changes`), marquee-investor moves (`investors.all_moves`), and
  mutual-fund exposure (`mf_holdings` by ISIN) → net **accumulating / distributing**. NSE publishes no
  FII/DII *cash* by sector, so this is an honest aggregate-of-constituents proxy (market-wide FII/DII is
  shown only as backdrop).
- **News** — sector-keyword-filtered market headlines.
- **LLM verdict** — `synthesize.sector_thesis` (uncapped) → an enter / add-&-accumulate / hold-&-watch /
  avoid read grounded strictly in the above (weighs momentum *and* valuation — a strong-but-expensive
  sector reads differently from a strong-and-cheap one).
- **Within-sector picks** — `within_sector_ranking` runs `screener.fundamental_screen(symbols=…)` over the
  constituents → a **numbered** Top (composite) and Undervalued (cheap-vs-own-history) list; **reply a
  number → that stock's deep report** (the `screen:` pending-menu pattern). Constituents fetched live from
  the NSE archive CSV (plain HTTP), macro-industry fallback on a 404.

**Supply chain — indirect contributors (`analysis/supply_chain.py`):** the sector report ends with a
**🔗 Supply chain** section — the smaller **listed** ancillaries feeding the sector's marquee names (the
*indirect* beneficiaries, excluding index members). Also a standalone **`suppliers: <company>`** command
(`suppliers: BEL`). No structured supplier data exists, so it's a **hybrid**: a hand-curated seed
(`_CURATED_SECTOR`/`_CURATED_COMPANY`, flagship sectors like defence) + LLM suggestions
(`synthesize.supply_chain_suppliers`, Google-Search-grounded) — **every** name verified against
`equity_master` (dropped if it doesn't resolve to a real NSE symbol; a name-consistency guard rejects a
hallucinated ticker like "PNC"→Pritish Nandy). Rows labelled 🖐️ curated vs 🤖 **AI-suggested (verify)**.
A discovery aid, not a confirmed supplier ledger.

**Sector rotation (`sector_brief.build_sector_rotation`, `email_bot.maybe_sector_rotation`):** ranks
**every** sector by relative strength vs Nifty + trend + valuation vs its own history →
**leaders / laggards / 💎 turning-up-from-cheap** (value+momentum inflection). On-demand
**`sector: rotation`**, and **pushed weekly (Saturday ≥18:00 IST**, once/ISO-week via
`scan.sector_rotation_due`/`mark_sector_rotation`). Deterministic — no LLM/network.

**Financial-sector ranking (`screener.financial_screen`):** banks / NBFCs / insurers can't be scored on
Piotroski/Altman/Beneish (those assume a non-financial balance sheet), so `within_sector_ranking` routes
lender sectors to a lender-appropriate composite — **ROA + ROE + NIM (proxy) + cheap P/B**, each
rank-normalised within the set, tried on both standalone & consolidated. So a `sector: bank` /
`sector: nbfc` report still gets a proper Top (and Undervalued where P/B is available; some banks don't
report a usable equity element, so they rank on ROA/NIM).

**Known limits:** constituent depth = names with financials ingested. Supply-chain covers **listed**
vendors only (many suppliers are private). Newer sub-indices (NBFC / Insurance / Capital Goods) have
<60 days of `index_close` history, so no technicals yet (valuation still works).

## Status / follow-ups

- Brief + orchestration + `--dry-run` validated end-to-end on RELIANCE.
- LLM synthesis + email are built and import-clean; **live runs need the
  LLM/Vertex env vars + `SMTP_*`** (user-supplied, not in repo).
- Follow-ups: auto-fetch the latest concall transcript / results PDF from the
  BSE announcement feed (so `--pdf` isn't manual); HTML email formatting;
  schedule via the nightly refresh; multi-stock watchlist digest.
