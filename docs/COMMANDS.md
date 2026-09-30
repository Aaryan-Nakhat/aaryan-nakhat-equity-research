# 📖 Every command

Everything you can ask — in the web UI, with `eqr <command>` in your terminal, or as an email subject.
Plain words are fine everywhere: one match goes straight to the report, several give a numbered list.

← back to the [README](../README.md) · how each number is computed: [METHODOLOGY.md](METHODOLOGY.md)

## Three ways to ask

Every command works three ways, with the same flow:

- **In your browser** — `eqr serve` (or the running email bot) serves a local web UI at
  **http://localhost:8765**: a command bar with autocomplete of every command, live progress while a
  report builds (start another meanwhile), the report inline with its PDFs, numbered picks as
  buttons, and a history of every saved report.
- **From your terminal** with **`eqr`** — no email setup needed. Plain words are fine
  (`eqr hdfc bank`, `eqr fund: parag parikh flexi cap`); one match goes straight to the report,
  several give a **numbered list — answer with `eqr pick <n>`**. Reports print to the terminal and
  are saved (Markdown + HTML + PDF) under `data/outputs/<date>/`; add `--open` to open the HTML.
- **By email** — put the command in the **Subject line** from an allowlisted address; the always-on
  bot replies in-thread (**reply a number** to drill into a numbered list).

`eqr help` (or emailing **`help`**) returns this same menu.

```bash
eqr infosys                  # full deep report
eqr hdfc                     # several matches → numbered list
eqr pick 1                   # pick one (also answers a report's "deeper cut" menu)
eqr screen: value            # any command below, exactly as you'd email it
```

## 📊 Stock deep report

the core.

| Ask | You get |
|---|---|
| `Infosys` *(any company name or NSE symbol)* | Full deep report — filing-grounded business overview, multi-year fundamentals, forensics (Altman / Beneish / Piotroski / accruals), sector-lens valuation (reverse-DCF centrepiece), technicals, **holder-level shareholding + smart-money cost & profit-booking risk**, and a **🏢 inside view** (employee & management sentiment from AmbitionBox, graded A→E vs the company's own industry) — inline **and as a PDF**. |
| `Reliance consolidated` / `Reliance standalone` | Same, forced to that financials basis (default auto-picks). |
| reply `1` after a report | **Upside Drivers 1-pager** — forward catalysts, each with an estimated ₹cr / % business impact. |

## 💰 Your portfolio & holdings

reads your tagged watchlist holdings.

| Ask | You get |
|---|---|
| `booking` | Where the tracked institutions on **your** holdings sit on big gains → profit-booking (selling) risk, ranked. |
| `sell` *(or `raise` / `trim`)* | Ranks your holdings **weakest-hand-first** — which to sell first if you need cash. |

## 🔎 Idea screeners

find new names; each replies a numbered list → deep report.

| Ask | You get |
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

## 🧭 Sector analysis

top-down, one sectoral index at a time.

| Ask | You get |
|---|---|
| `sector: <name>` *(e.g. `sector: defence`, `sector: pharma`)* | Trend + relative strength + valuation vs its **own history** + smart-money proxy + **best & most-undervalued names** + 🔗 supply chain. |
| `sector: list` | The ~20 sectors covered. |
| `sector: rotation` | **All** sectors ranked — leaders / laggards / turning-up-from-cheap *(also pushed weekly Sat ≥18:00)*. |

## 👤 Marquee investors · 🔗 supply chain · 💨 global shocks

| Ask | You get |
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
| `scorecard` *(or `track record`)* | **📊 Track record** — every deep-report verdict and idea-engine pick, logged as it went out and scored vs the Nifty 500 from the next open (hit rate with a 95% interval, excess return, best & worst — misses included). Also emailed weekly. How it's scored: [TRACK_RECORD.md](TRACK_RECORD.md). |
| `reality check: <link or text>` *(or `reality:` · `verify:` · `tip:`)* | **🔍 Reality Check** — reads a news article, X post or Reddit post (or pasted text, e.g. a reel's caption), checks each claim against the company's exchange filings (PDFs read for amounts) and reported numbers (✅ / 🟡 / ❌ / ⚠️), sizes it against revenue and market cap, shows whether it's already in the price and any red flags, and gives a bottom line. Reply a number → deep report. Videos and reels aren't read yet — paste the caption. |
| `thesis: <company> — <reasons>; <rules>` *(· `thesis: <company>` · `theses` · `unthesis: <company>`)* | **🛡️ Thesis Guard + exit plan** — why you own it, turned into checks re-run every evening: measurable reasons (growth, margins, ROE, ROCE, debt, P/E, promoter / MF / FII stakes, pledge) computed from filings; the rest judged from recent filings and concall notes, filing cited. Rules (`exit below`, `add below`, `trim above`, `trail N%`) show 🔔 when triggered. 🟢 intact · 🟡 weakening · 🔴 broken — emailed only when something changes. |

## 📈 Levels · 🟢 IPOs · 💵 funds · ❓ help

| Ask | You get |
|---|---|
| `levels: <name>` *(or `chart:` / `setup:`)* | Quick **computed** (no-LLM, ~30s) support/resistance zones, structure, entry/stop/target + annotated chart. |
| `ipo: ongoing` / `ipo: upcoming` / `ipo: <name>` | Live / forthcoming IPOs (band · dates · subscription) → note with **APPLY / AVOID / NEUTRAL**. |
| `fund: <name>` *(or `mf: <name>`)* | Mutual-fund deep report — returns, rolling consistency, risk, SIP/XIRR, holdings — with a PDF. |
| `help` *(or `commands` / `menu`)* | This whole menu, section by section, in your inbox. |

## 📬 Arrives automatically

No command needed: 🌅 **pre-market** (08:30, GIFT Nifty implied open) · 🔔
**midday** (12:30, live) · 📊 **full digest** (18:00) · 📡 **screener movements** (Sat) · 🔄 **sector
rotation** (Sat) · 💨 **Tailwind** (Sat + urgent break-ins pre-market / midday / evening when a fresh shock lands) · ⛏️
**Pickaxe** (monthly, 1st Sat — surging demand → indirect beneficiaries) · 🎙️ **Concalls** (Sat —
the week's most notable earnings calls) · 📈 **Results Radar** (Sat — the season's strongest results).
*Tip: add **consolidated** / **standalone** to a stock to force the basis; numbered menus stay live 24h.*

## 🔎 The discovery engines in detail

Screeners that *find* ideas, not just analyse named ones.

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
  job** (~10-15 min) — acked instantly, the full report lands when ready. Google Trends via the stealth Chromium
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
