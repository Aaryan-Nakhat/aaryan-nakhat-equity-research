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

## What it does

- **Scrape** primary sources for prices, filings, financials, corporate actions,
  delivery/derivatives data, and **live intraday quotes** (NSE), plus the daily
  **USD/INR** reference rate (FBIL), **gold/silver/crude** futures (MCX), and **mutual-fund
  NAVs** (AMFI) — via `scrapling` (Camoufox browser tier for the anti-bot `/api/*`).
- **Analyse** — fundamental (multi-year statements, ratios, quality/forensic scores,
  FCFF/FCFE, CFO-quality); **sector-appropriate valuation** (P/B-on-ROE for financials,
  EV/EBITDA + mid-cycle for cyclicals, P/E elsewhere; current multiple as an own-history
  percentile; **reverse-DCF** as the centrepiece; a **forward multiple** from management's
  own guidance); and technical (trend, momentum, delivery-% conviction).
- **Signals** — FII F&O positioning (smart-money sentiment), **insider/promoter (SEBI PIT)
  trades**, promoter pledge, bulk/block deals.
- **Mutual funds** *(new track)* — AMFI NAV backbone (`mf_scheme`/`mf_nav`, accumulated daily).
  **Email `fund: <name>`** → a fund deep-report for any of ~14,500 schemes: returns · risk
  (Sharpe/Sortino/drawdown) · rolling consistency · **SIP/XIRR** (₹10k/mo simulation) ·
  **benchmark-relative alpha/beta/up-down-capture/tracking-error** vs the category's index
  (~5y history backfilled from NSE archives) · category percentile · and a **LLM verdict**
  (Buy/Accumulate/Hold/Switch/Avoid). Where the AMC's SEBI monthly **holdings** are covered
  (`mf_holdings`, a generic SEBI-format parser over a per-AMC fetch registry — **PPFAS, HDFC, Nippon India**):
  portfolio concentration · **watchlist overlap** · and **month-over-month churn** (what the
  manager actually bought/exited). Broader AMC coverage + forensic look-through are next
  (see [`docs/PLAN.md`](docs/PLAN.md)).
- **IPOs** *(new track)* — **email `ipo: ongoing` / `ipo: upcoming`** → a numbered list of
  live / forthcoming issues (band · dates · live subscription) → reply a number (or
  `ipo: <name>`) → a **pre-listing note**: business, **fresh-issue vs OFS and what it signals**,
  restated financials, **valuation at the band vs listed peers**, use of proceeds, RHP risks,
  demand (subscription + anchor book), and an **APPLY / AVOID / NEUTRAL** verdict. All primary
  NSE sources (the RHP / price-band KPIs / anchor docs) — **no grey-market/GMP**. `scrapers/ipo.py`.
- **Discover** *(new track)* — screeners that *find* ideas, not just analyse named ones.
  **Email `screen: value`** → the Nifty-500 ranked on quality (Piotroski) + forensic
  (Altman/Beneish/accruals/no-pledge) + cheap-vs-own-history → a numbered list; reply a number →
  that name's full deep report. **Email `screen: holdco`** → the **Elcid trade, generalised**:
  listed holding companies whose disclosed listed-stake NAV exceeds their own market cap, ranked by
  discount. **Email `screen: investors`** / **`investor: <name>`** → track ~25 **marquee investors**
  (Jhunjhunwala, Mukul Agrawal, Kedia, …) across the shareholding data — each one's disclosed book
  and what they entered / added / trimmed / exited last quarter. **Email `screen: smallcap`** → the
  **capex-led small-cap hunt**: names in ₹1,000–10,000 cr ranked on a capex-cycle-first composite
  (capex vs its 3y base · capex ÷ depreciation · self-funded · ROCE & trend · cash quality · smart-money),
  with near-distress / manipulation / heavy-pledge / shrinking-revenue traps **gated out** — built to
  catch a structural capex boom *before* the P&L re-rates. **Email `screen: policy`** (or `policy:`) →
  the **government policy radar**: scans the latest **PIB press releases** (primary, official) for new
  schemes / policies / reforms — often at the announced / cabinet-approved / **draft** / consultation
  stage, *before* formal launch — and an LLM maps each to the **sector(s)** it hits and the **listed
  companies** likely to benefit (resolved to NSE symbols, watchlist names flagged). Primary sources
  only — **no news/social-media rumor**. **Email `sector: <name>`** (e.g. `sector: defence`;
  `sector: list` for the ~20 covered) → a **top-down read on a whole sectoral index**: its trend +
  momentum + relative strength vs Nifty, its **valuation vs its own ~5-yr history** (cheap/expensive vs
  itself), a smart-money proxy (institutional ownership Δ + MF exposure + marquee moves across the
  sector's stocks), sector headlines, an LLM enter/accumulate/hold verdict, the **best + most
  undervalued names inside it**, and a **🔗 supply-chain** section — the smaller **listed ancillaries**
  feeding the sector's marquee names (curated + AI, every name verified against the NSE master; also
  **`suppliers: <company>`**, e.g. `suppliers: BEL`). **Email `sector: rotation`** (also pushed **weekly,
  Sat ≥18:00**) ranks **all** sectors — leaders / laggards / **turning-up-from-cheap**. Financial sectors
  (banks/NBFCs/insurers) are ranked on **ROA/ROE/NIM/P-B** (Piotroski/Altman don't apply to lenders). Every list is a
  real table; a **weekly "Screener movements" email** (Sat ≥18:00 IST) pushes only the deltas.
  `analysis/screener.py`, `analysis/holdco.py`, `analysis/investors.py`, `analysis/smallcap.py`,
  `analysis/policy.py` (+ `scrapers/pib.py`), `screen_digest.py`; seed once with `scripts/backfill_universe.py` (`--seed-smallcaps` for the
  Nifty Smallcap 250 + Microcap 250 universe that gives `screen: smallcap` its edge).
- **Trading levels** *(new track)* — a **computed** (no-LLM) technical layer: support/resistance
  **zones** from the confluence of swing pivots + moving averages + 52-week extremes + volume-by-price
  + round numbers, market structure, chart patterns, and a **reward:risk-framed entry / stop / target**
  that **defers to the fundamental verdict** (Avoid names show levels for reference, not a buy). In the
  deep report (a "Trading levels & setup" section + an **annotated candlestick chart**), on demand via
  **email `levels: <name>`** (aliases `technical:` / `setup:` / `chart:`), and as watchlist **level
  alerts** in the digests — 🚀 breakouts / 🎯 pullbacks-to-support for the tracking list, ⚠️ 50/200-DMA
  losses / 🔻 support breaks for holdings. `analysis/technical.py`, `reports/charts.py`.
- **Report** — LLM reads the quant brief (+ filing PDFs) and writes a forensic thesis,
  delivered via an **email bot** (or Telegram, by the `CHANNELS` flag): **interactive**
  (name a stock → styled PDF + inline thesis). The deep report now **leads with a
  filing-grounded business overview** (what it does, segment revenue-mix %, market cap /
  TAM / penetration, order book for order-driven names), carries a **holder-level
  shareholding section** (every promoter account + public >1% holder from the SHP XBRL,
  sorted, each classified individual / **LISTED company** (the Elcid pattern, with its
  symbol) / unlisted pvt / MF / FPI) plus a **quarter-over-quarter ownership diff** (who
  entered / added / trimmed / exited, notable holders first) and a **💰 smart-money cost &
  profit-booking-risk** block — each institution's *inferred cost zone* (from the price range of
  the quarters they added in, ~4 yr of SHP history) vs the current price → a booking-risk flag
  (near cost / big gains ⚠️), with pre-our-data holdings honestly marked cost-unknown; the same
  read appears in the `sector:` and `investor:` views, and **email `booking`** ranks *your*
  holdings by how much institutions are up (elevated selling risk). Every
  valuation/forensic/technical section has a
  plain-English "how to read this"; it also offers an opt-in **growth-triggers 1-pager**
  (reply `1`) — forward-looking catalysts, each with an **estimated ₹ cr / % business
  impact** and an impact-sorted scoreboard so big medium-conviction triggers aren't buried.
  And **push** — a **pre-market digest at 08:30 IST** (GIFT Nifty implied Nifty open vs
  yesterday's close · overnight US/Asia · India VIX · FII futures stance · headlines · an LLM
  "overnight read" — a setup briefing, not a trade call), a **full daily digest at
  18:00 IST** (rich market-context header: sectoral indices · VIX · FII/DII · FII futures ·
  USD/INR · commodities; movers; events with inline filing analysis; insider trades) and a
  **midday "same-day" digest at 12:30** with the same sections on **live** intraday data
  (live indices/VIX/commodities, Upcoming, live movers, today's filings/insider), and a
  **💨 Tailwind global supply-shock radar pushed weekly (Sat ≥18:00 IST**, plus a **mid-week
  urgent break-in** when a big shock lands, and on-demand **`tailwind`** — **cached 24h**,
  `--latest` forces fresh) — a **four-tier agent pipeline** (Scout: Google News + **US Federal
  Register** → LLM Analyst → Google-Search-grounded Mapper → NSE-master Auditor) that finds global
  policy/supply moves across **all commodity categories** (metals, **agri**, **pharma inputs**,
  chemicals, fertiliser, energy) and maps them to **verified Indian listed beneficiaries** —
  alternate producers or export-share gainers — with the **supplier's world-share** and each firm's
  **revenue-share & production share**, each catalyst **source-cited**, each name checked against
  the master (reply → deep report). An idea generator, not a call; "no clean listed beneficiary" is
  a valid answer.
- **Housekeeping** — the bot keeps its own (server) mailbox tidy: processed **workbench** mail
  (requests it handled + reports it sent, matched by the `X-EquityBot` header) is moved to Trash
  **~30 min after sending**, scoped strictly to the client address so personal mail is untouched.
  Email **`help`** any time for the full command menu.

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
