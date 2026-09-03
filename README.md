# aaryan-nakhat-equity-research

A private equity-research workbench for **Indian stocks (NSE / BSE)**. Pulls
**primary, official, government-backed data only** (exchanges, SEBI, RBI, MOSPI,
company filings) — no blogs, no news aggregators, no third-party data vendors —
runs **fundamental + technical analysis**, and emails decision-grade reports to
help with actual buy/sell decisions.

Personal use. Not a hosted product.

## 📖 What you can ask it (email commands)

You drive the whole workbench by **emailing a command in the Subject line** from an allowlisted
address; the bot replies in-thread (many replies are a **numbered list — reply a number to drill
into that item**). Email **`help`** any time to get this same menu in your inbox.

**📊 Stock deep report** — the core.

| Email this | You get |
|---|---|
| `Adani Power` *(any company name or NSE symbol)* | Full deep report — filing-grounded business overview, multi-year fundamentals, forensics (Altman / Beneish / Piotroski / accruals), sector-lens valuation (reverse-DCF centrepiece), technicals, **holder-level shareholding + smart-money cost & profit-booking risk** — inline **and as a PDF**. |
| `Reliance consolidated` / `Reliance standalone` | Same, forced to that financials basis (default auto-picks). |
| reply `1` after a report | **Growth-triggers 1-pager** — forward catalysts, each with an estimated ₹cr / % business impact. |

**💰 Your portfolio & holdings** — reads your tagged watchlist holdings.

| Email this | You get |
|---|---|
| `booking` | Where the tracked institutions on **your** holdings sit on big gains → profit-booking (selling) risk, ranked. |
| `sell` *(or `raise` / `trim`)* | Ranks your holdings **weakest-hand-first** — which to sell first if you need cash. |

**🔎 Idea screeners** — find new names; each replies a numbered list → deep report.

| Email this | You get |
|---|---|
| `screen: value` *(or bare `screen`)* | Nifty-500 ranked on quality (Piotroski) + forensic + cheap-vs-own-history. |
| `screen: holdco` | The **Elcid trade, generalised** — listed holdcos trading below their listed-stake NAV. |
| `screen: investors` | Where ~25 tracked **marquee HNIs** just entered / added / trimmed / exited. |
| `screen: smallcap` | **Capex-led small-cap** hunt (structural capex boom before the P&L re-rates), traps gated out. |
| `screen: technical` | Strongest **chart setups to buy** — entry / stop / target. |
| `screen: policy` *(or `policy`)* | **Govt policy radar** — latest PIB schemes/policies → likely listed beneficiaries. |

**🧭 Sector analysis** — top-down, one sectoral index at a time.

| Email this | You get |
|---|---|
| `sector: <name>` *(e.g. `sector: defence`, `sector: pharma`)* | Trend + relative strength + valuation vs its **own history** + smart-money proxy + **best & most-undervalued names** + 🔗 supply chain. |
| `sector: list` | The ~20 sectors covered. |
| `sector: rotation` | **All** sectors ranked — leaders / laggards / turning-up-from-cheap *(also pushed weekly Sat ≥18:00)*. |

**👤 Marquee investors · 🔗 supply chain · 💨 global shocks**

| Email this | You get |
|---|---|
| `investor: <name>` *(e.g. `investor: Mukul Agrawal`)* | That HNI's disclosed book + latest-quarter moves + their **cost vs current price**. |
| `suppliers: <company>` *(e.g. `suppliers: BEL`)* | The smaller **listed** suppliers / ancillaries feeding that name. |
| `tailwind` *(or `catalysts`)* | **💨 Tailwind** — global commodity shocks (export bans / quotas / tariffs across metals, agri, pharma, chemicals, energy…) → **verified Indian beneficiaries**, with the supplier's world-share and each firm's revenue-share & production share. **Cached 24h.** |
| `tailwind --latest` *(or `fresh`)* | Same, but forces a brand-new live scan instead of the 24h-cached result. |

**📈 Levels · 🟢 IPOs · 💵 funds · ❓ help**

| Email this | You get |
|---|---|
| `levels: <name>` *(or `chart:` / `setup:`)* | Quick **computed** (no-LLM, ~30s) support/resistance zones, structure, entry/stop/target + annotated chart. |
| `ipo: ongoing` / `ipo: upcoming` / `ipo: <name>` | Live / forthcoming IPOs (band · dates · subscription) → note with **APPLY / AVOID / NEUTRAL**. |
| `fund: <name>` *(or `mf: <name>`)* | Mutual-fund deep report — returns, rolling consistency, risk, SIP/XIRR, holdings — with a PDF. |
| `help` *(or `commands` / `menu`)* | This whole menu, section by section, in your inbox. |

**📬 Arrives automatically** (no command): 🌅 **pre-market** (08:30, GIFT Nifty implied open) · 🔔
**midday** (12:30, live) · 📊 **full digest** (18:00) · 📡 **screener movements** (Sat) · 🔄 **sector
rotation** (Sat) · 💨 **Tailwind** (Sat + a mid-week urgent break-in when a big shock lands).
*Tip: add **consolidated** / **standalone** to a stock to force the basis; numbered menus stay live 24h.*

## How it works

Primary data → DuckDB → deterministic analysis + signals → an LLM writes the thesis → email/Telegram.
Short version below; **full detail per area in [`docs/`](docs/)**.

**📥 Data** *(all primary/official, `scrapers/`)*
- Prices, delivery %, F&O/participant OI, index closes — NSE archives (plain HTTP) + live intraday quotes.
- Financials (XBRL), corporate actions, announcements, shareholding (holder-level SHP), insider/PIT, pledge.
- **USD/INR** (FBIL) · **gold/silver/crude** (MCX) · **mutual-fund NAVs** (AMFI) · **PIB** policy releases.
- Anti-bot `/api/*` handled via `scrapling` (Camoufox browser tier).

**🧮 Analysis** *(deterministic Python, `analysis/`)*
- **Fundamental** — multi-year statements, ratios, FCFF/FCFE, CFO-quality; **forensic** scores (Piotroski, Altman Z, Beneish M, accruals).
- **Valuation** — sector-appropriate lens (P/B-on-ROE for lenders, EV/EBITDA for cyclicals, P/E else), current multiple as an **own-history percentile**, **reverse-DCF + Monte-Carlo** centrepiece, a forward multiple from management guidance.
- **Technicals** — trend/momentum, delivery-% conviction, and computed support/resistance **levels** with an R:R entry/stop/target.

**📡 Signals** — FII F&O positioning (smart-money sentiment) · insider/promoter (SEBI PIT) trades · promoter pledge · bulk/block deals · **smart-money cost zones** (each institution's inferred cost from SHP add-quarters → profit-booking risk).

**🔎 Discovery** — screeners that *find* ideas, not just analyse named ones: value/quality+forensic, holdco-discount (Elcid), marquee-investor moves, capex-led small-caps, policy radar, top-down **sector** analysis + rotation, supply-chain mapping, and the **💨 Tailwind** global supply-shock → Indian-beneficiary radar. *(See the command table above for how to call each.)*

**📤 Reports & delivery**
- **On-demand** deep reports (styled PDF + inline forensic thesis; every section has a plain-English "how to read this").
- **Pushed** digests — pre-market (08:30), midday (12:30), full (18:00), weekly screener-movements / sector-rotation / Tailwind (Sat), + a mid-week urgent Tailwind break-in.
- Channel via the `CHANNELS` flag (email and/or Telegram); the bot also **auto-tidies its own mailbox** (bins processed workbench mail ~30 min after sending, personal mail untouched).
- **LLM** (Gemini via Vertex) is used *only* for synthesis / filing-reading / name-resolution — every number above is deterministic.

## Status

Working end-to-end (NSE/BSE/MCX/FBIL → DuckDB → fundamentals/forensics/technicals/
valuation + signals → LLM report → email **or** Telegram bot, always-on). On-demand
reports + a pre-market (08:30), midday (12:30) and full (18:00) watchlist digest; an email channel mirrors the
Telegram one for when Telegram is ISP-blocked. Docs:

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — **every metric traced source → transform → formula → model** (the "how are you getting this?" reference).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — end-to-end diagram + component map.
- [`docs/PLAN.md`](docs/PLAN.md) — vision, scope, phase status.
- [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) / [`docs/SCRAPING.md`](docs/SCRAPING.md) — sources + scrapability findings.
- [`docs/FUNDAMENTALS.md`](docs/FUNDAMENTALS.md) — financials data path, ratios, forensic scores, valuation.
- [`docs/TECHNICAL.md`](docs/TECHNICAL.md) — indicators. [`docs/REPORTS.md`](docs/REPORTS.md) — LLM synthesis, Telegram bot, PDF, email.
- [`docs/ALERTS.md`](docs/ALERTS.md) — watchlist alerts + the full push schedule.

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
- the LLM (`google-genai`, via Vertex AI service account) — symbol resolution + report synthesis
- `python-telegram-bot` (delivery) · `telegramify-markdown` (formatting) ·
  Playwright Chromium + `markdown` (HTML → PDF) · SMTP email

## Setup

```bash
uv sync                                   # install deps (Python 3.12)
uv run playwright install chromium        # for HTML → PDF
cp .env.example .env                       # then fill in your own credentials
```

Configure `.env` (all secrets are read from the environment; `.env` is gitignored — see
[`.env.example`](.env.example) for every variable):
- **LLM** — either Vertex AI (a GCP service-account JSON, also gitignored) or a
  Developer API key.
- **Delivery** — `CHANNELS=email` (Gmail SMTP/IMAP app password) and/or `telegram`
  (a BotFather token + your allowed user IDs).

Bootstrap the local data store, then run a report or the bot:

```bash
uv run python scripts/populate_watchlist.py               # seed the watchlist
uv run python scripts/backfill_eod.py                     # ingest market EOD history
uv run python scripts/research_report.py RELIANCE --deep  # one-off deep report
uv run python scripts/email_bot.py                        # the always-on bot (or run_email_bot.ps1)
```

The DuckDB file and all scrapes under `data/` are built locally and gitignored — bring your
own data store.

## Disclaimer

Personal research tooling, **not investment advice**. It reads only primary/official sources
and can still be wrong; verify anything before you act on it. No warranty — see the license.

## License

[MIT](LICENSE) © Aaryan Nakhat.
