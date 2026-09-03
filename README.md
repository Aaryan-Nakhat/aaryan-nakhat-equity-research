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
**Full detail per area in [`docs/`](docs/)** ([`METHODOLOGY.md`](docs/METHODOLOGY.md) traces every metric
source → formula → model).

### 📥 Data — primary / official only (`scrapers/`)

- **Market** — prices, **delivery %**, F&O + participant OI, index closes (with PE/PB per index) from
  NSE archives (plain HTTP); **live intraday quotes** via NSE NextApi.
- **Filings & ownership** — financials (XBRL; ~6y P&L, balance-sheet + cash-flow from FY23), corporate
  actions, announcements, **holder-level shareholding** (every promoter + public >1% holder from the SHP
  XBRL), **insider / promoter (SEBI PIT)** trades, promoter pledge.
- **Macro & funds** — **USD/INR** (FBIL) · near-month **gold / silver / crude** futures (MCX) ·
  **mutual-fund NAVs** (AMFI, ~14.5k schemes) · **PIB** government-policy releases.
- Anti-bot `/api/*` solved with `scrapling` (Camoufox browser tier); everything else is plain HTTP.

### 🧮 Analysis — deterministic Python (`analysis/`)

- **Fundamental** — multi-year income statement / balance sheet / cash flow, the full ratio set,
  FCFF/FCFE, **CFO-quality** (CFO vs PAT), growth & margin trends.
- **Forensic** — Piotroski F (0–9), **Altman Z**, **Beneish M**, accruals, pledge — plus statistical
  forensics (Benford / Sloan).
- **Valuation, sector-appropriate** — P/B-on-ROE for financials, EV/EBITDA + mid-cycle for cyclicals,
  P/E elsewhere; the current multiple as an **own-history percentile** (cheap/rich vs itself);
  **reverse-DCF + Monte-Carlo DCF** as the centrepiece; a **forward multiple** from management guidance.
- **Technical** — trend / momentum (SMA · RSI · MACD · BB · ATR), **delivery-% conviction**, 52-wk
  position, relative strength vs Nifty; computed support/resistance **zones** (swing pivots + MAs + 52-wk
  extremes + volume-by-price + round numbers) → a **reward:risk entry / stop / target** that defers to the
  fundamental verdict.

### 📡 Signals

- **FII F&O positioning** — net-long % in index futures vs retail (a smart-money sentiment read).
- **Insider / promoter (SEBI PIT)** trades · **promoter pledge** changes · **bulk / block deals** (each
  counterparty classified: listed co / MF / FPI / individual…).
- **💰 Smart-money cost zones** — each institution's *inferred* cost from the price range of the quarters
  it added in (~4y of SHP) vs the current price → a **profit-booking-risk** read; positions built before
  our earliest snapshot are honestly marked *cost-unknown*, not guessed.

### 🔎 Discovery — screeners that *find* ideas, not just analyse named ones

- **`screen: value`** — the Nifty-500 ranked on quality (Piotroski) + forensic
  (Altman / Beneish / accruals / no-pledge) + **cheap-vs-own-history**.
- **`screen: holdco`** — the **Elcid trade, generalised**: listed holding companies whose disclosed
  listed-stake NAV exceeds their own market cap, ranked by discount.
- **`screen: investors` / `investor: <name>`** — ~25 tracked **marquee HNIs** (Jhunjhunwala, Mukul
  Agrawal, Kedia…): each one's disclosed book + what they entered / added / trimmed / exited last quarter.
- **`screen: smallcap`** — the **capex-led small-cap hunt** (₹1,000–10,000 cr) on a capex-cycle composite
  (capex vs its 3y base · capex ÷ depreciation · self-funded · ROCE & trend · cash quality · smart-money),
  with distress / manipulation / heavy-pledge / shrinking-revenue traps **gated out** — catches a capex
  boom *before* the P&L re-rates.
- **`screen: policy`** — the **government policy radar**: scans the latest **PIB** releases (often at the
  cabinet-approved / **draft** / consultation stage, before launch) → an LLM maps each to the sector(s) it
  hits and the **listed companies** likely to benefit. Primary sources only — no news/social rumor.
- **`sector: <name>` / `sector: rotation`** — a **top-down read** on a sectoral index (trend + RS vs
  Nifty + **valuation vs its own ~5y history** + a smart-money proxy + the best & most-undervalued names +
  a **🔗 supply-chain** section). Rotation ranks **all** sectors — leaders / laggards /
  turning-up-from-cheap. Lenders (banks/NBFCs/insurers) are ranked on **ROA/ROE/NIM/P-B**.
- **`suppliers: <company>`** — the smaller **listed** suppliers / ancillaries feeding a marquee name
  (curated + AI, every name verified against the NSE master).
- **💨 `tailwind`** — the **global supply-shock radar**: a four-tier agent pipeline (Scout = Google News +
  **US Federal Register** → LLM Analyst → Google-Search-grounded Mapper → NSE-master Auditor) finds global
  **export bans / quotas / tariffs / cuts** across **all commodity categories** (metals · agri · pharma
  inputs · chemicals · fertiliser · energy) and maps them to **verified Indian beneficiaries** — with the
  supplier's world-share and each firm's revenue-share & production share. Cached 24h; `--latest` forces fresh.
- **Mutual funds — `fund: <name>`** — a deep report for any of ~14.5k schemes: returns · risk
  (Sharpe / Sortino / drawdown) · rolling consistency · **SIP/XIRR** (₹10k/mo) · **benchmark-relative
  alpha / beta / up-down-capture / tracking-error** · category percentile · an LLM verdict. Where SEBI
  monthly **holdings** are covered (PPFAS / HDFC / Nippon): concentration · **watchlist overlap** ·
  month-over-month churn.
- **IPOs — `ipo: ongoing / upcoming / <name>`** — a pre-listing note: business, **fresh-issue vs OFS** and
  what it signals, restated financials, **valuation at the band vs listed peers**, use of proceeds, RHP
  risks, demand (subscription + anchor book), and an **APPLY / AVOID / NEUTRAL** verdict. Primary NSE docs
  only — no grey-market / GMP.

### 📤 Reports & delivery

- **Deep report** — the LLM reads the quant brief + filing PDFs → a **forensic thesis + verdict**, inline
  **and as a styled PDF**. It leads with a **filing-grounded business overview** (what it does, segment
  revenue-mix %, market cap / TAM / penetration, order book for order-driven names), carries a
  **holder-level shareholding** section + a **quarter-over-quarter ownership diff** (who entered / added /
  trimmed / exited) and the **💰 smart-money cost & profit-booking-risk** block, and gives every
  valuation / forensic / technical section a plain-English **"how to read this."** Opt-in
  **growth-triggers 1-pager** (reply `1`) — forward catalysts, each with an estimated **₹cr / % impact**.
- **Pushed digests** — **pre-market** (08:30: GIFT Nifty implied open · overnight US/Asia · India VIX ·
  FII futures · headlines · an LLM overnight read), **full watchlist** (18:00: market-context header ·
  movers · events with **inline filing analysis** · insider trades), **midday** (12:30, same sections on
  live data), and the **weekly** screener-movements / sector-rotation / **💨 Tailwind** (Sat) + a
  **mid-week urgent** Tailwind break-in when a big shock lands.
- **Delivery** — email and/or Telegram via the `CHANNELS` flag; **`help`** returns the whole command menu;
  the bot **auto-tidies its own mailbox** (bins processed workbench mail ~30 min after sending — personal
  mail untouched).
- **LLM** (Gemini via Vertex) is used **only** for synthesis / filing-reading / name-resolution — every
  number above is **deterministic**.

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
