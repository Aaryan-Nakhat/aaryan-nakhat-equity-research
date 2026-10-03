# 📈 aaryan-nakhat-equity-research

> **Type a company name. Get the report an analyst would write** — for Indian stocks (NSE / BSE), self-hosted,
> built on the exchanges' own filings. Ask in a **local web UI**, your **terminal** or by **email**.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Data](https://img.shields.io/badge/numbers-from%20exchange%20filings-2E7D32)
![LLM](https://img.shields.io/badge/LLM-provider--agnostic%20(BYO)-6E56CF)
![Store](https://img.shields.io/badge/store-DuckDB-FFF000?logo=duckdb&logoColor=black)
![Delivery](https://img.shields.io/badge/UI-web%20·%20CLI%20·%20email-0088CC)
![License](https://img.shields.io/badge/license-MIT-informational)
[![CI](https://github.com/Aaryan-Nakhat/aaryan-nakhat-equity-research/actions/workflows/ci.yml/badge.svg)](https://github.com/Aaryan-Nakhat/aaryan-nakhat-equity-research/actions/workflows/ci.yml)

<p align="center"><a href="docs/media/hero.mp4"><img src="docs/media/hero.gif" alt="A 48-second tour: ask for a company in plain words and the report builds itself — forensics, a Monte-Carlo fair value, every claim cited to a filing; agents turning a shock abroad into an Indian beneficiary; a tax-aware portfolio; a scorecard" width="100%"></a><br><sub>▶ <a href="docs/media/hero.mp4">Watch in full quality (MP4, 48 s)</a></sub></p>

**[Deep report](#-the-deep-report)** · **[Find ideas](#-find-ideas-you-didnt-ask-for)** ·
**[Global & demand engines](#-agents-that-read-the-world)** · **[Sectors, investors, earnings](#-sectors-investors-and-earnings)** ·
**[Funds, IPOs, levels, alerts](#-funds-ipos-levels-and-alerts)** · **[Track record](#-it-keeps-score-of-itself)** ·
**[Reality Check](#-saw-a-tip-in-a-reel-a-post-or-the-news-check-it)** · **[Thesis Guard](#%EF%B8%8F-you-bought-it-for-a-reason-is-it-still-true)** · **[Portfolio](#-your-portfolio--every-corporate-action-every-tax-rule)** · **[How it thinks](#-how-it-thinks)** ·
**[Quickstart](#-quickstart)** · **[Every command](docs/COMMANDS.md)**

## Why it's different

- **The analysis is computed, not generated.** Financials come straight from exchange XBRL filings; ratios,
  forensics, valuation, ownership and technicals are deterministic Python, and with no LLM configured every report
  still builds with all of those numbers. The LLM reads the filings and writes the argument. Where it does estimate
  something — an Upside Driver's ₹ impact, a Pickaxe revenue-share projection, a Tailwind company's market share —
  the report labels it as an estimate, with its source, to verify.
- **Official data for the numbers** — NSE / BSE, SEBI, RBI, AMFI, PIB and company filings. News, Google Trends and
  employee reviews feed only the idea engines and the inside view, and are shown as such
  ([every source and its terms](SOURCES.md)).
- **Shaped to the business** — a bank is read on NIM, NPAs and capital; an insurer on combined ratio, persistency
  and solvency; everyone else on the industrial statements. Not one template with "n/a" everywhere.
- **It finds ideas, not just analyses them** — 12 screeners, a hotlist of names several screens agree on, agent
  pipelines that turn a foreign export ban or a demand surge into verified Indian beneficiaries, and a
  management-says vs numbers-show read of every earnings call.
- **Yours** — runs on your machine, any LLM provider (or none), MIT-licensed.

**Compared with the typical open-source AI stock analyst:**

| | Typical open-source AI analyst | This project |
|---|---|---|
| **Where the numbers come from** | The LLM reads a price / news API and writes figures into its answer | Computed in Python from exchange filings; the few LLM estimates are labelled as such |
| **Indian market depth** | A `.NS` ticker on a global price API | NSE / BSE XBRL financials, SEBI shareholding (every >1% holder), insider trades, pledges, concall transcripts, corporate actions |
| **Forensic checks** | Rarely | Altman Z, Beneish M, Piotroski F, accruals, Benford — tested against hand-worked values |
| **Banks and insurers** | The same template as a manufacturer | Their own reports: NIM, NPAs, CET1 · combined ratio, persistency, solvency |
| **Finding ideas** | You bring the ticker | 12 screens, a hotlist, Tailwind, Pickaxe, earnings-call and results radars, a policy radar |
| **Splits, bonuses, rights, demergers** | Left to a global data vendor | Adjusted from NSE's own corporate-action records, including rights (ex-rights price) and demergers |
| **Without an LLM key** | Nothing to show | Every report still builds with every number |
| **How you use it** | A script or a terminal | A web UI, a terminal command and email, plus scheduled digests |

## 🔬 The deep report

Ask for any listed company in plain words (`adani power`, `hdfc` → pick from a list, `INFY`). A few minutes later:
a report of ~15 sections plus a PDF with charts. **[Full Adani Power PDF](docs/samples/adani-power.pdf)** ·
[HDFC Bank](docs/samples/hdfc-bank.pdf) · [ICICI Lombard](docs/samples/icici-lombard.pdf) — unedited output of 29-Sep-2026.

<p align="center"><img src="docs/media/deep-report.png" alt="The deep report at a glance" width="100%"></p>

**It starts from the company's own filings** — what the business does, its revenue mix, market position — read
from the annual report, investor presentations and concall transcripts, with the page it came from.

**Forensics** — Altman Z (distress), Beneish M (earnings manipulation), Piotroski F (fundamental strength),
accruals and promoter pledge, each with what the number means.

**Who owns it, and who just moved** — every promoter and >1% holder from the SEBI shareholding filings, and the
quarter-on-quarter diff: who entered, added, trimmed or exited.

**💰 Smart-money cost** — what each institution likely paid (from the price range of the quarters it bought in),
so you can see who's sitting on gains and might book profit. Holders who bought before the data starts are
marked unknown, not guessed.

**🏢 The inside view** — what employees say about the company and its management, graded A→E against its own
industry.

**What the price already assumes** — a reverse-DCF (the growth today's price implies), a Monte-Carlo DCF, and
the multiple against the company's own history and its peers.

**The verdict, argued** — earnings quality, returns, balance sheet, growth, forensics and valuation, each read by
the LLM from the computed brief and the filings, ending in a verdict it has to justify.

**Trading levels** — support and resistance zones computed from swing pivots, moving averages, 52-week extremes,
volume-by-price and round numbers, and a reward-to-risk entry / stop / target that defers to the fundamental verdict.

<details>
<summary><b>Banks and insurers get their own report</b> — NIM, NPAs, CET1 · combined ratio, solvency, persistency</summary>

A lender has no EBITDA and an insurer has no working capital, so they're read on the numbers that matter for them,
with their own health checks — and the industrial forensics (Altman, Beneish, Piotroski) are left out rather than
misapplied.

</details>

Reply **`1`** after any report for the **Upside Drivers 1-pager**: forward catalysts from the concalls and investor
presentations, each with an estimated ₹ cr / % impact and how certain it is.

<details><summary><b>📸 The real report, section by section</b></summary>

![Business overview read from the filings](docs/samples/dr-overview.png)

![Forensic deep-dive](docs/samples/dr-forensic.png)

![Ownership changes](docs/samples/dr-ownership.png)

![Smart-money cost and profit-booking risk](docs/samples/dr-smart-money.png)

![Employee and management sentiment](docs/samples/dr-inside-view.png)

![Reverse-DCF](docs/samples/dr-reverse-dcf.png)

![Monte-Carlo DCF fair-value distribution](docs/samples/dr-chart-montecarlo.png)

![Verdict](docs/samples/dr-verdict.png)

![Support and resistance chart](docs/samples/dr-chart-levels.png)

![HDFC Bank: asset quality](docs/samples/bank-asset-quality.png)

![ICICI Lombard: underwriting ratios](docs/samples/insurer-underwriting.png)

</details>

## 🔎 Find ideas you didn't ask for

Every screen replies with a ranked, numbered list — pick a number for that company's deep report.

<p align="center"><img src="docs/media/ideas.png" alt="12 screens and the hotlist" width="100%"></p>

**🔥 `hotlist`** — the names that several screens flag at once (volume breakouts, market beaters, institutional
buying, value + clean books, small-cap capex). Agreement between independent screens is a stronger lead than any one.

**`screen: value`** — the Nifty 500 ranked on quality (Piotroski) + clean forensics (Altman, Beneish, accruals, no pledge)
+ cheap against its own history.

**`screen: smallcap`** — ₹1,000–10,000 cr companies in a capex cycle (capex vs its 3-year base and depreciation,
self-funded, rising ROCE, smart money in), with distress, manipulation, pledge and shrinking-revenue traps gated out.

<details>
<summary><b>9 more screens</b> — volume breakouts · market beaters · institutional buying · margin momentum · debt payers · compounders · holdcos below NAV · marquee investors · chart setups</summary>

**`screen: volume`** — near a 52-week high, in an uptrend, on a volume surge.

**`screen: beaters`** — beating the Nifty 500 over 3, 6 and 12 months and still trending.

**`screen: institutions`** — promoters raising their own stake, with the institution adding alongside.

**`screen: margins`** — net margin expanding on growing revenue.

**`screen: deleverage`** — cut debt over 3–4 years while staying profitable.

**`screen: quality`** — high ROCE, low debt, steady growth, clean books.

**`screen: holdco`** — listed holding companies trading below the value of their listed stakes.

**`screen: investors`** — where ~25 tracked marquee investors entered, added, trimmed or exited.

**`screen: technical`** — the strongest chart setups, each with entry, stop and target.

</details>

<details><summary><b>📸 Real screen output</b></summary>

![Hotlist](docs/samples/hotlist.png)

![Value screen](docs/samples/screen-value.png)

![Small-cap capex screen](docs/samples/screen-smallcap.png)

![Volume breakouts](docs/samples/screen-volume.png)

![Market beaters](docs/samples/screen-beaters.png)

![Institutional buying](docs/samples/screen-institutions.png)

![Margin momentum](docs/samples/screen-margins.png)

![Debt payers](docs/samples/screen-deleverage.png)

![Compounders](docs/samples/screen-quality.png)

![Holding companies below NAV](docs/samples/screen-holdco.png)

![Marquee investors](docs/samples/screen-investors.png)

![Technical setups](docs/samples/screen-technical.png)

</details>

## 🌍 Agents that read the world

**💨 `tailwind`** — when a country restricts exports of something (a ban, a quota, a tariff), buyers need another
supplier. Four agents find the Indian listed companies that *are* that supplier: a scout reads ~30 commodity
chokepoints in the news and the US Federal Register, an analyst keeps only real disruptions (citing its evidence by
number), a web-search mapper finds existing producers, and a Python auditor drops any name that isn't a real,
plausible NSE listing. Pushed weekly, plus same-day alerts when a fresh shock lands.

<p align="center"><img src="docs/media/agents.png" alt="Tailwind and Pickaxe agent pipelines" width="100%"></p>

**⛏️ `pickaxe`** — the demand-side mirror: rising "buy" searches on Google Trends and demand news → the durable theme →
not the crowded producer but the supplier to it (the feed, vaccine, packaging or equipment maker), each with its
price, P/E vs sector, support/resistance and a revenue-share projection read from its filings, with sources.

**🏛️ `policy`** — the latest government press releases (PIB), often at the cabinet-approval or draft stage, mapped
to the sectors and listed companies they help.

<details><summary><b>📸 Real Tailwind and Pickaxe output</b></summary>

![Tailwind](docs/samples/tailwind.png)

![Pickaxe](docs/samples/pickaxe.png)

</details>

## 🧭 Sectors, investors and earnings

**`sector: defence`** (or pharma, banks, IT…) — the sector index's trend, relative strength and valuation against its
own history, who's accumulating, and the best and cheapest names inside it, plus its listed supply chain.

<p align="center"><img src="docs/media/sectors.png" alt="Sector rotation and the concall say-do gap" width="100%"></p>

**`sector: rotation`** — every sector ranked: leaders, laggards, and the ones turning up from cheap.

**🎙️ `concalls`** — every recent earnings call scored twice: the **tone** of what management said (from the
transcript) and the **execution** in the quarter's numbers (from the filings). Upbeat talk on soft numbers is a
caution; quiet talk on strong numbers is an under-the-radar lead.

**📈 `results`** — companies that just reported, ranked by growth, whether it's accelerating, and margin inflection
(during results season; it's empty between seasons).

**👤 `investor: <name>`** — a marquee investor's disclosed holdings, last-quarter moves and their cost vs today.

**🔗 `suppliers: <company>`** — the smaller listed suppliers feeding a big name.

<details><summary><b>📸 Real sector, concall, investor and supplier output</b></summary>

![Sector: defence](docs/samples/sector-defence.png)

![Sector rotation](docs/samples/sector-rotation.png)

![Concalls: say-do gap](docs/samples/concalls.png)

![Investor: Mukul Agrawal](docs/samples/investor.png)

![Suppliers: BEL](docs/samples/suppliers.png)

</details>

## 💵 Funds, IPOs, levels and alerts

**`fund: <name>`** — any of ~14,000 mutual-fund schemes: returns, rolling consistency, risk, SIP/XIRR, alpha / beta /
capture vs its benchmark, category rank and, where the AMC publishes it, the portfolio.

<p align="center"><img src="docs/media/funds.png" alt="Funds, IPOs, levels and alerts" width="100%"></p>

**`ipo: ongoing` / `ipo: upcoming` / `ipo: <name>`** — a pre-listing note from the offer documents: fresh issue vs
offer-for-sale, valuation at the band vs listed peers, an accounting and governance flag, risks, subscription, and
apply / avoid / neutral.

**`levels: <name>`** — the quick one: support/resistance zones, structure and a setup with a chart, in ~30 seconds and
without the LLM.

**🔔 `alert: order win`** — watch every company's exchange filings for a phrase and get an email within ~20 minutes of
a match. **💰 `booking` / `sell`** rank your own holdings by profit-booking risk and which to trim first — and
**`raise 50000`** (or `take out 2 lakh`) says exactly what to sell for that amount: a least-tax plan vs a
weakest-holdings-first plan, with shares, money in hand and the estimated capital-gains tax.
**📬 Arrives by itself** (with email set up): a pre-market brief at 08:30 (GIFT Nifty implied open, overnight
markets, FII positioning), midday and evening watchlist digests, and weekly screen, sector, Tailwind, concall and
results pushes.

<details><summary><b>📸 Real fund, IPO and levels output</b></summary>

![Mutual fund report](docs/samples/fund.png)

![Growth of ₹100 invested in the fund](docs/samples/fund-chart.png)

![Live IPOs](docs/samples/ipo.png)

![Levels](docs/samples/levels.png)

</details>

## 🔍 Saw a tip in a reel, a post or the news? Check it

`reality check: <link or text>` reads a **news article, an X post or a Reddit post** (or text you paste — a
reel's caption, a WhatsApp forward) and pulls out every claim. Each one is checked against the company's
**own exchange filings** — the filing PDFs are read for the amounts — and its reported numbers: ✅ confirmed ·
🟡 partly true · ❌ contradicted · ⚠️ no filing found. Then: **how big** it is next to the company's revenue and
market cap, whether the stock has **already moved**, and **red flags** (micro-cap, thin trading, a run-up,
pledged or selling promoters, "multibagger" language) — ending in a bottom line like *Real and material*,
*Real, but small* or *Looks like hype*. Tried on a post saying HDFC Bank's profit "jumped 60%": ❌ contradicted —
the reported quarter shows +19%.

<p align="center"><img src="docs/media/reality-check.png" alt="Reality Check on a social post" width="100%"></p>

## 🛡️ You bought it for a reason. Is it still true?

`thesis: BEL — order book keeps growing, debt-free, ROE above 20%, promoters not selling; exit below 250,
trim above 450, trail 15%` — your reasons become checks: the measurable ones (growth, margins, ROE, debt,
promoter / mutual-fund / FII stakes, pledge, P/E) computed from filings, the rest ("order book growing")
judged from the company's recent filings and concall notes with the filing cited. Your rules become an exit
plan. Every evening it's all re-checked, and you get an email **only when something changes** — a reason
slipping from 🟢 intact to 🟡 weakening or 🔴 broken, or a rule triggering. It answers the question forums
won't ("should I still hold this?") with your own reasons, not a tip.

<p align="center"><img src="docs/media/thesis-guard.png" alt="Thesis Guard checks and exit plan" width="100%"></p>

## 💼 Your portfolio — every corporate action, every tax rule

Add what you own in the web UI's **💼 My holdings** (your watchlist is already listed — type quantity and price) or a
`holdings.csv`; it never leaves your computer. Then it's worked out for you: splits and bonuses (bonus shares as their
own ₹0-cost lot), rights offers, demergers split by the company's own cost notice, companies that merged away entered
as you bought them, 31-Jan-2018 grandfathering, sells matched oldest-first into realised gains by tax year, dividends,
XIRR — and **`raise 50000`** says exactly what to sell, least tax first. ETFs, SME, REITs, InvITs and BSE-only shares too.

<p align="center"><img src="docs/media/portfolio.png" alt="A sample portfolio: tax lots, bonus and demerger handling, realised gains by tax year and a raise-cash plan" width="100%"></p>

## 📊 It keeps score of itself

Every call — each deep-report verdict and every name an idea engine lists — is logged the moment it goes
out and later scored against the Nifty 500 from the next session's open, on split-adjusted prices: hit
rate with a confidence interval, excess return, best and worst, misses included. Ask **`scorecard`** any time
(it's also emailed weekly), and a report on a company you've asked about before opens with what the tool
said last time and how that played out. The model never sees those outcomes — they're for you, not for
it to "learn" from ([why](docs/TRACK_RECORD.md#what-the-llm-sees-nothing-from-here)). The record stays in
your local database; it's never published.

## 🧠 How it thinks

<p align="center"><img src="docs/media/architecture.png" alt="How it works: official data → a local DuckDB → deterministic Python → an LLM that reads, writes and is checked → web, terminal, email" width="100%"></p>

The LLMs read, triage and write; **Python computes every number and checks every name they propose.**

<p align="center"><img src="docs/media/tailwind-flow.png" alt="The Tailwind pipeline: scout, analyst (LLM, must cite evidence), mapper (LLM + web), Python auditor (real NSE listing?) → verified names; unsourced and hallucinated names are dropped" width="100%"></p>

**A real case, including the part that didn't work.** On Saturday 19-Sep-2026 the weekly Tailwind digest carried a
low-severity catalyst: the Philippines restricting exports of ube (purple yam). There's no "purple-yam stock", so the
mapper went one layer out, to natural colours and food ingredients, and three small-caps passed the auditor.

| From Friday 18-Sep's close | Tue 22 | Mon 28 |
|---|---:|---:|
| AVT Natural Products | **+14.7%** | +1.1% |
| Vidhi Specialty Food Ingredients | +4.5% | +2.7% |
| Dynemic Products | +0.7% | −2.3% |
| *Nifty 50* | *−0.1%* | *−2.4%* |

One of three ran and gave most of it back within a week — an idea generator, not a call. And the alert came late:
the mid-week check only fired for high-severity shocks, so this waited for Saturday. It now runs three times each
trading day and also breaks in for a fresh shock of any severity with a small- or mid-cap beneficiary.

**→ [How it thinks, in full](docs/HOW_IT_THINKS.md)** — the deep-report flow, the Tailwind and Pickaxe pipelines,
and the one-process architecture, as diagrams.

## 🚀 Quickstart

```bash
git clone https://github.com/Aaryan-Nakhat/aaryan-nakhat-equity-research && cd aaryan-nakhat-equity-research
uv sync && uv run playwright install chromium     # Python 3.12 via uv; Chromium renders the PDFs
cp .env.example .env                              # optional: add an LLM key for the AI write-ups
uv run eqr demo                                   # ≈12 min, one time: fetch a starter set, open the web UI
```

`eqr demo` downloads ~13 months of prices and the financials of a dozen well-known companies **on your
machine** (it asks once before using NSE's site — see [Data sources & terms](#data-sources--terms)), then
opens **http://localhost:8765**. `eqr doctor` shows what's working and the one-line fix for what isn't.
No LLM key yet? Every report still builds with all its numbers; the AI write-up is what the key adds.

**Docker** instead: `cp .env.example .env && docker compose up -d` (web UI on http://localhost:8765), then once
`docker compose run --rm eqr eqr demo --yes --no-serve` (≈12 min) for the starter set. Data lives in a volume.

**On a VPS** (Openship, Coolify, Railway, a plain server): deploy the `Dockerfile`, mount a volume at
`/data`, and **set `WEB_PASSWORD`** — the server refuses to listen publicly without one, so strangers
can't run reports on your LLM key.

**Three ways to ask, one flow.** The browser UI has autocomplete for every command, live progress, reports with
their charts and PDFs, and a history. In a terminal it's `eqr <anything>` (`eqr pick 2` answers a list). By email,
the command is the subject line. **[Every command →](docs/COMMANDS.md)**

```bash
eqr adani power              # deep report in the terminal (saved as Markdown + HTML + PDF)
eqr hdfc                     # several matches → numbered list
eqr pick 1                   # pick one
eqr screen: value --open     # any command; --open opens the HTML
```

## How it works

Primary data → DuckDB → deterministic analysis + signals → an LLM writes the thesis → web UI / terminal / email.
**Full detail per area in [`docs/`](docs/)** ([`METHODOLOGY.md`](docs/METHODOLOGY.md) traces every metric
source → formula → model).

### 📥 Data (`scrapers/`) — every source and its terms in [SOURCES.md](SOURCES.md)

- **Market** — prices, **delivery %**, F&O + participant OI, index closes (with PE/PB per index) from
  NSE archives (plain HTTP); **live intraday quotes** via NSE NextApi.
- **Filings & ownership** — financials (XBRL; ~6y P&L, balance-sheet + cash-flow from FY23), corporate
  actions, announcements, **holder-level shareholding** (every promoter + public >1% holder from the SHP
  XBRL), **insider / promoter (SEBI PIT)** trades, promoter pledge.
- **Macro & funds** — **USD/INR** (FBIL) · near-month **gold / silver / crude** futures (MCX) ·
  **mutual-fund NAVs** (AMFI, ~14.5k schemes) · **PIB** government-policy releases.
- Most data is plain-HTTP archive files. NSE's `/api/*` endpoints only answer a real browser session, so they're
  read through a headless Chromium (`scrapling`) — **off by default**, opt-in via `NSE_SCRAPING_ENABLED`
  after reading NSE's terms.

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

### 🔎 Discovery

Screeners (`screen: …`), the 🔥 hotlist, sector analysis, 💨 Tailwind and ⛏️ Pickaxe (four-tier agent
pipelines: scout → analyst → web-grounded mapper → auditor against the NSE master), 🎙️ Concalls,
📈 Results Radar, 🔔 announcement alerts, mutual funds and IPOs — **each explained in
[docs/COMMANDS.md](docs/COMMANDS.md#-the-discovery-engines-in-detail)**.

### 📤 Reports & delivery

- **Deep report** — the LLM reads the quant brief + filing PDFs → a **forensic thesis + verdict**, inline
  **and as a styled PDF**. It leads with a **filing-grounded business overview** (what it does, segment
  revenue-mix %, market cap / TAM / penetration, order book for order-driven names), carries a
  **holder-level shareholding** section + a **quarter-over-quarter ownership diff** (who entered / added /
  trimmed / exited) and the **💰 smart-money cost & profit-booking-risk** block, and gives every
  valuation / forensic / technical section a plain-English **"how to read this."** **Banks get a
  bank-shaped report** — NII, NIM, cost-to-income, credit cost, gross/net NPAs & provision coverage,
  CET1, CD ratio and ✅/⚠️ bank health checks in place of the industrial statements and Altman /
  Piotroski / Beneish (which don't apply to lenders); **insurers get an insurer report** — premiums &
  APE, persistency, claims / combined ratio, underwriting vs investment profit, solvency. Opt-in **Upside Drivers 1-pager** (reply `1`) —
  forward catalysts, each with an estimated **₹cr / % impact**.
- **Pushed digests** — **pre-market** (08:30: GIFT Nifty implied open · overnight US/Asia · India VIX ·
  FII futures · headlines · an LLM overnight read), **full watchlist** (18:00: market-context header ·
  movers · events with **inline filing analysis** · insider trades), **midday** (12:30, same sections on
  live data), the **weekly** screener-movements / sector-rotation / **💨 Tailwind** (Sat), the **monthly**
  **⛏️ Pickaxe** (1st Sat) + **urgent** Tailwind break-ins (pre-market / midday / evening) when a fresh shock lands.
- **Delivery** — the **`eqr` terminal CLI** (saves Markdown + HTML + PDF locally) or **email**; both
  run the same command handler, so name resolution, numbered menus and follow-ups behave identically.
  **`help`** returns the whole command menu; the email bot **auto-tidies its own mailbox** (bins processed workbench mail ~30 min after sending — personal
  mail untouched).
- **LLM** (your configured provider) is used **only** for synthesis / filing-reading / name-resolution — every
  number above is **deterministic**.

## Status

Working end-to-end (NSE/BSE/MCX/FBIL → DuckDB → fundamentals/forensics/technicals/
valuation + signals → LLM report → email bot, always-on). On-demand
reports from the terminal (`eqr`) or email, + a pre-market (08:30), midday (12:30) and full (18:00)
watchlist digest over email. Docs:

- [`docs/COMMANDS.md`](docs/COMMANDS.md) — every command, with details.
- [`docs/HOW_IT_THINKS.md`](docs/HOW_IT_THINKS.md) — the agent pipelines and request flow as diagrams, plus a real case.
- [`docs/TRACK_RECORD.md`](docs/TRACK_RECORD.md) — how every call is logged and scored, and why the model never sees the results.
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
  bot/         commands: app.py (routing + main loop) · queries · core · help · deliver · screens ·
               personal · pushes · local.py (the channel shared by email, web and CLI)
  web/         the local web UI (FastAPI + plain JS, no build step), incl. 💼 My holdings
  portfolio/   your buys / sells (web UI + holdings.csv) → tax lots, realised gains, dividends, XIRR
  cli.py       the `eqr` terminal command
  common/      config, storage, shared utilities
scripts/       pipeline entry points (email_bot.py launches bot/app.py; render_video.py / render_images.py /
               make_samples.py rebuild the README media)
data/          raw scrapes + processed artifacts (gitignored)
docs/          reference docs · docs/samples/ real sample reports (specs.txt rebuilds them) · docs/media/ the README visuals (src/ rebuilds them)
tests/         tests
```

## Stack

- Python 3.12, `uv`
- `scrapling` (scraping, incl. stealth Chromium browser tier for NSE's anti-bot `/api/`)
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
- **LLM** *(optional to start — without one every report still builds with all its numbers; the
  AI write-up and the discovery engines like Tailwind / Pickaxe need it)* — bring your own: set `LLM_MODEL` (the provider is inferred from it) and `LLM_API_KEY`
  (or `LLM_BASE_URL` for a custom endpoint). Runs on any provider via [LiteLLM](https://docs.litellm.ai).
- **Email delivery** *(optional — skip it if you only use `eqr` in the terminal)* — one Gmail for
  both sending and reading requests (SMTP/IMAP app password), plus `EMAIL_ALLOWED_SENDERS` (who may
  request reports) and `REPORT_TO` (where pushes go).
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
uv run eqr reliance                                       # a deep report in the terminal
uv run eqr doctor                                         # what works here, and the one-line fix for what doesn't
uv run eqr serve                                          # web UI at http://localhost:8765 (+ the email bot if configured)
uv run eqr bot                                            # the always-on email bot — it also serves the web UI
```

**One process owns the database.** DuckDB allows one writing process at a time, so the server
(`eqr serve`, or the email bot) runs everything — the web UI, the email loop and CLI jobs — and a
`eqr` command sent while it's running is forwarded to it (`--direct` forces a local run). The web
server binds to localhost only (`WEB_HOST` / `WEB_PORT`; `WEB_UI_ENABLED=false` turns it off). Set
`WEB_PASSWORD` to require a sign-in — the server refuses to listen beyond localhost without one, so an
instance on a VPS can't be used by strangers to run reports on your LLM key.

The DuckDB file and all scrapes under `data/` are built locally and gitignored — bring your
own data store.

**Your holdings (quantity, buy price, optional date)** stay on your computer. Add them in the web UI's
**💼 My holdings** page — the stocks already in your watchlist are listed there by company name, so you just
type how many you hold and what you paid; a new one is found by typing any part of its name — or copy [`holdings.example.csv`](holdings.example.csv) to `holdings.csv`
(gitignored; `HOLDINGS_CSV` moves it) — columns `symbol, qty, price, date`, and a broker's holdings export
works as-is. One row per buy. **With a date, enter the quantity and price as you bought them** (splits / bonuses since
are applied — 100 @ ₹500 bought before a 1:5 split shows as 500 @ ₹100); **without one, what your broker shows
today**. **Mergers and demergers are handled:** a company that merged into another is entered as you bought it
(type the old company's name — the tool reads from the filings what it merged into and at what ratio, and
converts it, keeping your cost and date), and after a demerger your cost is split the way the
company's own cost-of-acquisition notice says — read from its filing — with the new company's shares offered
to add in one click. **Sells** are recorded with one click (oldest shares go first, as the tax rules say) and land in a **realised
gains by year** table with the estimated tax; **bonus shares** are their own ₹0-cost lots dated on allotment;
**rights issues** are offered, never assumed; buys before **Feb-2018** are grandfathered; **dividends** you received
and your **XIRR** are worked out from your buys. **ETFs, SME shares, REITs, InvITs and BSE-only shares** can be
added too. The date is optional *per buy*: with it you also get short- / long-term
(held over 12 months), the yearly return, split / bonus adjustment and the Nifty 500 over the same days;
without it, profit / loss only. The file is re-read whenever it changes. To make sure it — or the
database or `.env` — can never be committed, turn on the repo's guard once: `git config core.hooksPath .githooks`.

**Tests.** `uv sync --extra dev && uv run pytest` — ~150 test cases, no network, no `.env`, no data needed
(every fetch is mocked, every database is a throw-away). They include the forensic scores, valuation and
smart-money cost zones checked against hand-worked numbers, split/bonus/rights/demerger price adjustment,
bank and insurer taxonomies, name resolution and the web UI. [CI](.github/workflows/ci.yml) runs lint and the
suite on Linux and Windows for every push.

## Data sources & terms

This tool ships **no data** — it fetches from public sources **on your own machine** when you
run it, under **their** terms. Several (the exchanges especially) restrict automated access and
**prohibit redistribution**. Please review **[SOURCES.md](SOURCES.md)** before running: it lists
every source, its terms, attribution, and the **NSE browser-tier opt-in** (`NSE_SCRAPING_ENABLED`,
off by default). Intended for **personal, self-hosted, non-commercial** use — don't run it as a
hosted service that scrapes exchanges for others, and don't redistribute fetched data.

## Disclaimer

Personal research tooling, **not investment advice**. It builds its numbers from official filings
and can still be wrong; verify anything before you act on it. No warranty — see the license.
Every emailed report and PDF carries the same disclaimer (`REPORT_DISCLAIMER`).

## License

[MIT](LICENSE) © Aaryan Nakhat.
