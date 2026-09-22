# 📈 aaryan-nakhat-equity-research

> A self-hosted equity-research workbench for **Indian stocks (NSE / BSE)** — you drive the whole
> thing by **emailing a command**, and an always-on bot emails back decision-grade reports.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Data](https://img.shields.io/badge/data-primary%20%2F%20official%20only-2E7D32)
![LLM](https://img.shields.io/badge/LLM-provider--agnostic%20(BYO)-6E56CF)
![Store](https://img.shields.io/badge/store-DuckDB-FFF000?logo=duckdb&logoColor=black)
![Delivery](https://img.shields.io/badge/delivery-email%20bot-0088CC)
![Self-hosted](https://img.shields.io/badge/self--hosted-yes-informational)

Pulls **primary, official, government-backed data only** (exchanges, SEBI, RBI, MOSPI, company
filings) — no blogs, no news aggregators, no third-party data vendors — runs **fundamental +
technical + forensic analysis**, and turns it into readable reports + a set of **idea-discovery
engines** that surface stocks you didn't name. Bring your own LLM (any provider, via `.env`).

Personal use. Not a hosted product.

## 📖 What you can ask it (email commands)

You drive the whole workbench by **emailing a command in the Subject line** from an allowlisted
address; the bot replies in-thread (many replies are a **numbered list — reply a number to drill
into that item**). Email **`help`** any time to get this same menu in your inbox.

**📊 Stock deep report** — the core.

| Email this | You get |
|---|---|
| `Infosys` *(any company name or NSE symbol)* | Full deep report — filing-grounded business overview, multi-year fundamentals, forensics (Altman / Beneish / Piotroski / accruals), sector-lens valuation (reverse-DCF centrepiece), technicals, **holder-level shareholding + smart-money cost & profit-booking risk**, and a **🏢 inside view** (employee & management sentiment from AmbitionBox, graded A→E vs the company's own industry) — inline **and as a PDF**. |
| `Reliance consolidated` / `Reliance standalone` | Same, forced to that financials basis (default auto-picks). |
| reply `1` after a report | **Upside Drivers 1-pager** — forward catalysts, each with an estimated ₹cr / % business impact. |

**💰 Your portfolio & holdings** — reads your tagged watchlist holdings.

| Email this | You get |
|---|---|
| `booking` | Where the tracked institutions on **your** holdings sit on big gains → profit-booking (selling) risk, ranked. |
| `sell` *(or `raise` / `trim`)* | Ranks your holdings **weakest-hand-first** — which to sell first if you need cash. |

**🔎 Idea screeners** — find new names; each replies a numbered list → deep report.

| Email this | You get |
|---|---|
| `screen: value` *(or bare `screen`)* | Nifty-500 ranked on quality (Piotroski) + forensic + cheap-vs-own-history. |
| `screen: volume` *(or `breakout`)* | **Volume Breakouts** — names near 52-week highs, uptrend intact, with a volume surge (full liquid universe). |
| `screen: beaters` *(or `rs`)* | **Market Beaters** — beating the Nifty-500 over 3 / 6 / 12 months, still trending up. |
| `screen: institutions` | **Institutional Buying** — where promoters raised their own stake last quarter, with the institution adding alongside. |
| `screen: margins` | **Margin Momentum** — net margin expanding vs the prior quarters, on growing revenue. |
| `screen: deleverage` | **Debt Payers** — cut total debt over 3-4 yrs while staying profitable (healthy, not distress). |
| `screen: quality` | **Compounders** — high ROCE + low debt + steady multi-year growth + clean books. |
| `screen: holdco` | The **Elcid trade, generalised** — listed holdcos trading below their listed-stake NAV. |
| `screen: investors` | Where ~25 tracked **marquee HNIs** just entered / added / trimmed / exited. |
| `screen: smallcap` | **Capex-led small-cap** hunt (structural capex boom before the P&L re-rates), traps gated out. |
| `screen: technical` | Strongest **chart setups to buy** — entry / stop / target. |
| `screen: policy` *(or `policy`)* | **Govt policy radar** — latest PIB schemes/policies → likely listed beneficiaries. |
| `hotlist` *(`--latest` forces fresh)* | **🔥 Hotlist** — the names lighting up across **several** screeners at once (volume · beaters · institutions · value · small-cap); highest-conviction confluence first. **Cached 24h.** |

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
| `pickaxe` *(or `demand`)* | **⛏️ Pickaxe** — surging Indian demand (Google-Trends 'buy' searches + demand-surge news) → the indirect **"sell the pickaxes"** listed beneficiaries + the direct plays. Each name carries **exact price / P/E-vs-sector / support-resistance** and a **filing-grounded revenue-share now → next-FY + growth (with sources)**, plus a **Google-Trends chart** per theme in a PDF. A deep build (~10-15 min): acked instantly, lands when ready. **Cached 24h.** |
| `pickaxe --latest` | Same, but forces a brand-new live scan instead of the 24h-cached result. |
| `concalls` *(or `calls`)* | **🎙️ Concalls** — the most notable recent earnings calls market-wide: **Management Tone** (from the transcript) vs the quarter's **Execution** (from our numbers), ranked by how far the two diverge (upbeat talk on soft numbers = caution; quiet talk on strong numbers = under-radar). Reply a number → deep report. *(Also pushed weekly.)* |
| `results` *(or `movers`)* | **📈 Results Radar** — companies that **just reported**, ranked by how strong the quarter was (YoY growth + whether it's **accelerating** + margin inflection, from the numbers). No analyst consensus → growth-vs-own-history, not beat-vs-street. Reply a number → deep report. *(Also pushed weekly.)* |
| `alert: <keyword>` *(· `alerts` · `unalert: <keyword>`)* | **🔔 Announcements** — watch every company's exchange filings for a phrase (e.g. `alert: order win`, `alert: QIP`); get an email within ~20 min of any match (8am-11pm IST). Forward-looking; `alerts` lists them, `unalert:` removes. |

**📈 Levels · 🟢 IPOs · 💵 funds · ❓ help**

| Email this | You get |
|---|---|
| `levels: <name>` *(or `chart:` / `setup:`)* | Quick **computed** (no-LLM, ~30s) support/resistance zones, structure, entry/stop/target + annotated chart. |
| `ipo: ongoing` / `ipo: upcoming` / `ipo: <name>` | Live / forthcoming IPOs (band · dates · subscription) → note with **APPLY / AVOID / NEUTRAL**. |
| `fund: <name>` *(or `mf: <name>`)* | Mutual-fund deep report — returns, rolling consistency, risk, SIP/XIRR, holdings — with a PDF. |
| `help` *(or `commands` / `menu`)* | This whole menu, section by section, in your inbox. |

**📬 Arrives automatically** (no command): 🌅 **pre-market** (08:30, GIFT Nifty implied open) · 🔔
**midday** (12:30, live) · 📊 **full digest** (18:00) · 📡 **screener movements** (Sat) · 🔄 **sector
rotation** (Sat) · 💨 **Tailwind** (Sat + urgent break-ins pre-market / midday / evening when a fresh shock lands) · ⛏️
**Pickaxe** (monthly, 1st Sat — surging demand → indirect beneficiaries) · 🎙️ **Concalls** (Sat —
the week's most notable earnings calls) · 📈 **Results Radar** (Sat — the season's strongest results).
*Tip: add **consolidated** / **standalone** to a stock to force the basis; numbered menus stay live 24h.*

## How it works

Primary data → DuckDB → deterministic analysis + signals → an LLM writes the thesis → email.
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
- **`screen: volume`** — **Volume Breakouts** across the **full liquid equity universe**: names
  within ~4% of their **52-week high**, in an uptrend (>200-DMA · 50>200), with a **volume surge**
  confirming the move (ranked on high-proximity + volume + delivery, forensic-trap-gated).
- **`screen: beaters`** — **Market Beaters**: names **beating the Nifty-500** over 3 / 6 /
  12 months and still above their 200-DMA (leadership persists), restricted to liquid, tradeable names.
- **`screen: institutions`** — **Institutional Buying**: names whose **promoter raised their own stake**
  quarter-on-quarter, annotated with the largest **institution adding alongside** — where the smart,
  informed money is going in.
- **`screen: margins`** — **Margin Momentum**: names whose **net margin is expanding** vs the prior four
  quarters **on growing revenue** (margin gains on a growing base, not a shrinking one).
- **`screen: deleverage`** — **Debt Payers**: names that have **cut total borrowings** over the last
  3-4 years **while staying profitable** (ROCE > 0) — deleveraging from strength, not distress.
- **`screen: quality`** — **Compounders**: **high ROCE + low debt + steady multi-year growth + clean
  forensics** — leads with *business quality* (unlike `screen: value`, which leads with cheapness).
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
  **US Federal Register** → LLM Analyst → web-search-grounded Mapper → NSE-master Auditor) finds global
  **export bans / quotas / tariffs / cuts** across **all commodity categories** (metals · agri · pharma
  inputs · chemicals · fertiliser · energy) and maps them to **verified Indian beneficiaries** — with the
  supplier's world-share and each firm's revenue-share & production share. Cached 24h; `--latest` forces fresh.
- **⛏️ `pickaxe`** — the **demand-side mirror of Tailwind**: a four-tier agent pipeline (Scout = Google
  Trends rising 'buy' searches + demand-surge news → LLM Demand-Analyst → web-search-grounded Value-Chain
  Mapper → NSE-master Auditor) finds **surging Indian consumer/industrial demand** and maps each theme to the
  **indirect "sell the pickaxes" beneficiaries** — the feed/vaccine/ingredient/equipment/packaging names that
  ride a boom with **less cyclicality** than the crowded end-product — plus the direct plays. Every name is
  then **deep-enriched**: exact **price / P/E-vs-sector / P/B / support-resistance** from our own engines, and
  a **filing/concall-grounded revenue-share (now → next FY) + growth projection with sources**, plus a
  **Google-Trends interest chart** per theme in an attached PDF. Because that's heavy it runs as a **background
  job** (~10-15 min) — acked instantly, the full report lands when ready. Google Trends via the Camoufox
  browser tier is best-effort (no official API); when throttled the demand read is news/LLM-driven. Cached
  24h; `--latest` forces fresh.
- **🔥 `hotlist`** — the **multi-signal confluence** screen: it runs every discovery engine (momentum ·
  leaders · accumulation · value+forensic · small-cap capex) and ranks names by **how many engines flag
  the same stock** — confluence being a higher-conviction lead than any single screen. A heavier
  multi-engine run, so it's built once and **cached 24h**; `--latest` forces fresh.
- **🎙️ `concalls`** — scores recent earnings calls **market-wide** and surfaces the notable ones by
  comparing **Management Tone** (an LLM read of the transcript's forward words — *Very Confident →
  Defensive*) against the quarter's **Execution** (computed from our own financials, not the words —
  *Firing → Struggling*). The **say-do gap** is the edge — *Talk > Numbers* flags over-promising,
  *Numbers > Talk* flags a quiet compounder. Transcripts are scored **incrementally in the background**
  and persisted, so the command reads a ranked table instantly. Reply a number → deep report; also a
  weekly Saturday digest.
- **📈 `results`** — the **Results Radar**, the companion to Concalls (what management *said* vs what
  they *delivered*): ranks the companies that **just reported** by the strength of the quarter —
  YoY revenue & profit growth, whether growth is **accelerating** vs the prior quarters, and margin
  inflection — all from our numbers. A background pass lands the fresh quarter for names that just filed;
  the command computes + ranks instantly. **No analyst consensus** (primary data only), so it's
  growth-vs-own-history, not beat-vs-street. Reply a number → deep report; also a weekly Saturday digest.
- **🔔 `alert: <keyword>`** — **standing announcement alerts**: register a phrase (`alert: order win`,
  `alert: QIP`, `alert: capacity expansion`) and get an email the moment **any** listed company files
  an announcement that matches — front-running the news. A background sweep (~every 20 min, 8am-11pm
  IST) matches new filings against your keywords; **forward-looking** (a high-watermark means a new
  keyword only catches *future* filings, never a backlog). `alerts` lists them, `unalert: <keyword>`
  removes, `alert clear` wipes all.
- **Mutual funds — `fund: <name>`** — a deep report for any of ~14.5k schemes: returns · risk
  (Sharpe / Sortino / drawdown) · rolling consistency · **SIP/XIRR** (₹10k/mo) · **benchmark-relative
  alpha / beta / up-down-capture / tracking-error** · category percentile · an LLM verdict. Where SEBI
  monthly **holdings** are covered (PPFAS / HDFC / Nippon): concentration · **watchlist overlap** ·
  month-over-month churn.
- **IPOs — `ipo: ongoing / upcoming / <name>`** — a pre-listing note: business, **fresh-issue vs OFS** and
  what it signals, restated financials, **valuation at the band vs listed peers**, an **accounting &
  governance flag** (🟢 Clean / 🟡 Watch / 🟠 Concern / 🔴 Red flag, read from the RHP), use of proceeds,
  RHP risks, demand (subscription by category + **retail allotment odds** + anchor book), and an
  **APPLY / AVOID / NEUTRAL** verdict. Primary NSE docs only — no grey-market / GMP.

### 📤 Reports & delivery

- **Deep report** — the LLM reads the quant brief + filing PDFs → a **forensic thesis + verdict**, inline
  **and as a styled PDF**. It leads with a **filing-grounded business overview** (what it does, segment
  revenue-mix %, market cap / TAM / penetration, order book for order-driven names), carries a
  **holder-level shareholding** section + a **quarter-over-quarter ownership diff** (who entered / added /
  trimmed / exited) and the **💰 smart-money cost & profit-booking-risk** block, and gives every
  valuation / forensic / technical section a plain-English **"how to read this."** Opt-in
  **Upside Drivers 1-pager** (reply `1`) — forward catalysts, each with an estimated **₹cr / % impact**.
- **Pushed digests** — **pre-market** (08:30: GIFT Nifty implied open · overnight US/Asia · India VIX ·
  FII futures · headlines · an LLM overnight read), **full watchlist** (18:00: market-context header ·
  movers · events with **inline filing analysis** · insider trades), **midday** (12:30, same sections on
  live data), the **weekly** screener-movements / sector-rotation / **💨 Tailwind** (Sat), the **monthly**
  **⛏️ Pickaxe** (1st Sat) + **urgent** Tailwind break-ins (pre-market / midday / evening) when a fresh shock lands.
- **Delivery** — email; **`help`** returns the whole command menu;
  the bot **auto-tidies its own mailbox** (bins processed workbench mail ~30 min after sending — personal
  mail untouched).
- **LLM** (your configured provider) is used **only** for synthesis / filing-reading / name-resolution — every
  number above is **deterministic**.

## Status

Working end-to-end (NSE/BSE/MCX/FBIL → DuckDB → fundamentals/forensics/technicals/
valuation + signals → LLM report → email bot, always-on). On-demand
reports + a pre-market (08:30), midday (12:30) and full (18:00) watchlist digest, all over email. Docs:

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) — **every metric traced source → transform → formula → model** (the "how are you getting this?" reference).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — end-to-end diagram + component map.
- [`docs/PLAN.md`](docs/PLAN.md) — vision, scope, phase status.
- [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) / [`docs/SCRAPING.md`](docs/SCRAPING.md) — sources + scrapability findings.
- [`docs/FUNDAMENTALS.md`](docs/FUNDAMENTALS.md) — financials data path, ratios, forensic scores, valuation.
- [`docs/TECHNICAL.md`](docs/TECHNICAL.md) — indicators. [`docs/REPORTS.md`](docs/REPORTS.md) — LLM synthesis, PDF, email.
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
- the LLM (configured via .env — any provider) — symbol resolution + report synthesis
  Playwright Chromium + `markdown` (HTML → PDF) · SMTP email

## Setup

```bash
uv sync                                   # install deps (Python 3.12)
uv run playwright install chromium        # for HTML → PDF
cp .env.example .env                       # then fill in your own credentials
```

Configure `.env` (all secrets are read from the environment; `.env` is gitignored — see
[`.env.example`](.env.example) for every variable):
- **LLM** — bring your own: set `LLM_MODEL` (the provider is inferred from it) and `LLM_API_KEY`
  (or `LLM_BASE_URL` for a custom endpoint). Runs on any provider via [LiteLLM](https://docs.litellm.ai).
- **Delivery** — one Gmail for both sending and reading requests (SMTP/IMAP app password), plus
  `EMAIL_ALLOWED_SENDERS` (who may request reports) and `REPORT_TO` (where pushes go).
- **Schedule, toggles & tuning** — every delivery time, the weekly-push day, per-feature on/off
  switches, the timezone, cache TTLs, timeouts and analytical thresholds are read from `.env` by
  [`src/equity_research/config.py`](src/equity_research/config.py) — **all optional, each defaulting
  to the built-in behaviour**, so nothing here is required to get started. `.env.example` documents
  them in two tiers (headline vs advanced). E.g. `TAILWIND_URGENT_SLOTS=` disables urgent alerts;
  `ENABLE_PICKAXE=false` skips that push.

Bootstrap the local data store, then run a report or the bot:

```bash
uv run python scripts/populate_watchlist.py               # seed the watchlist
uv run python scripts/backfill_eod.py                     # ingest market EOD history
uv run python scripts/research_report.py RELIANCE --deep  # one-off deep report
uv run python scripts/email_bot.py                        # the always-on bot (or run_email_bot.ps1)
```

The DuckDB file and all scrapes under `data/` are built locally and gitignored — bring your
own data store.

## Data sources & terms

This tool ships **no data** — it fetches from public sources **on your own machine** when you
run it, under **their** terms. Several (the exchanges especially) restrict automated access and
**prohibit redistribution**. Please review **[SOURCES.md](SOURCES.md)** before running: it lists
every source, its terms, attribution, and the **NSE browser-tier opt-in** (`NSE_SCRAPING_ENABLED`,
off by default). Intended for **personal, self-hosted, non-commercial** use — don't run it as a
hosted service that scrapes exchanges for others, and don't redistribute fetched data.

## Disclaimer

Personal research tooling, **not investment advice**. It reads only primary/official sources
and can still be wrong; verify anything before you act on it. No warranty — see the license.
Every emailed report and PDF carries the same disclaimer (`REPORT_DISCLAIMER`).

## License

[MIT](LICENSE) © Aaryan Nakhat.
