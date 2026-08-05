# Methodology & Data Lineage

> **Purpose:** a section-by-section trace of **every number** the workbench shows — for the
> stock deep report, the IPO note, the mutual-fund report, the watchlist digests and the
> screeners. For each: **where the data comes from**, **how it's transformed**, the exact
> **formula**, and whether an **LLM / model / simulation** is involved. If someone asks "how
> are you getting this?", the answer is here.
>
> Ground rule for the whole system: **primary / government-backed sources only** — NSE, BSE,
> SEBI, AMFI, FBIL, MCX, and companies' own statutory filings. No blogs, no broker research,
> no third-party data vendors, no grey-market. Every derived number is anchored to the
> company's *own* filed history — never invented.

---

## Part A — The pipeline (source → store → compute → LLM → deliver)

### A.1 Data sources & how they're scraped
Scraping uses `scrapling`, in two tiers:
- **Plain HTTP (`Fetcher`)** — archive files & JSON that aren't bot-walled.
- **Browser tier (Camoufox / `StealthyFetcher`)** — NSE's `/api/*` sits behind Akamai Bot
  Manager; we warm a real browser session and run the request as an in-page `fetch()` XHR so
  it carries the anti-bot clearance.

| Source | What we take | Access | Scraper |
|---|---|---|---|
| **NSE archives** (`nsearchives.nseindia.com`) | EOD bhavcopy (OHLCV + delivery%), index closes, participant OI, index constituents, **result XBRL**, IPO offer docs | 🟢 plain HTTP | `nse_archives.py`, `nse_financials.py` |
| **NSE `/api/*`** | live quote snapshot, corporate announcements/actions, SEBI PIT insider trades, promoter pledge, bulk/block deals, FII/DII, IPO feeds, SHP summary, granular `basicIndustry` | 🟡 browser tier | `nse_api.py` |
| **NSE result filings** | quarterly/annual financials (catalog → XBRL) | 🟡 catalog browser + 🟢 XBRL HTTP | `nse_financials.py` |
| **NSE SHP XBRL** | holder-level shareholding pattern | 🟢 plain HTTP | `nse_shp.py` |
| **BSE** | scrip header/quote (mirror) | 🟢 plain HTTP | `bse.py` |
| **AMFI** | daily NAVs for ~14k MF schemes; per-AMC NAV history | 🟢 plain HTTP | `amfi.py` |
| **AMC sites** | monthly MF portfolio holdings (per-AMC XLSX) | 🟡 mixed | `mf_holdings.py` |
| **FBIL** | USD/INR reference rate | 🟢 JSON | `fbil.py` |
| **MCX** | gold / silver / crude futures | 🟡 header-gated | `mcx.py` |

**The financials path in detail** (`nse_financials.py`, `ingest.ingest_financials`):
1. **Catalog** (browser): `/api/corporates-financial-results` lists every result filing for a
   symbol with period metadata + a direct `xbrl` URL. For post-Dec-2024 quarters SEBI moved
   results to **Integrated Filing** (`/api/integrated-filing-results`, `in-capmkt` taxonomy) —
   `list_all_result_filings` merges legacy history with the new feed so tables reach the latest
   filed period.
2. **XBRL** (plain HTTP): each filing's XBRL on `nsearchives` carries standardised
   `in-bse-fin:*` / `in-capmkt:*` facts. The **dimensionless context** whose start/end match
   the filing's declared period gives the headline numbers (BSE taxonomy: `OneD` = current
   quarter, `FourD` = YTD; `NatureOfReportStandaloneConsolidated` = standalone/consolidated).
3. Landed **long-format** into `financials(symbol, period_end, period_type, consolidated,
   element, value, filing_date, source_url)`. A taxonomy-agnostic parser gives ~6 years of P&L;
   balance sheet / cash flow are present FY2023+ (older result XBRLs omit them).

### A.2 Storage — DuckDB tables (`common/db.py`)
Single local DuckDB file. Fifteen tables:

| Table | Holds | Fed by |
|---|---|---|
| `equity_eod` | daily OHLCV + delivery% + series (EQ/BE/BZ) | `ingest_eod` / `backfill_eod` |
| `index_close` | daily index closes (Nifty 50/500/Midcap/Smallcap …) | `ingest_index_closes` |
| `financials` | long-format XBRL facts (P&L/BS/CF), Q + Y | `ingest_financials` |
| `shareholding` | promoter %, pledge % (summary) | SHP ingest |
| `shp_holders` | **holder-level** SHP (name, %, category, classification) | `ingest_shp_history` |
| `insider_trades` | SEBI PIT Reg-7 disclosures | `store_insider_trades` |
| `sector_map` | symbol → company, macro industry, **basic_industry**, universe | `ingest_sector_map`, `ingest_basic_industries` |
| `equity_master` | every NSE-listed company (name, ISIN) — the listed-name resolver | `ingest_equity_master` (EQUITY_L.csv) |
| `participant_oi` | FII/DII/pro/client OI by product | `ingest_participant_oi` |
| `mf_scheme` / `mf_nav` | ~14k schemes + accumulated daily NAV | `ingest_mf_navall`, `ingest_mf_nav_history` |
| `mf_holdings` | per-scheme monthly portfolio | `ingest_mf_holdings` |
| `mf_amc` | AMC name → AMFI code | `build_mf_amc_map` |
| `watchlist` | tracked symbols + `list_type` (holding/tracking) | `watchlist.py` |
| `alert_state` | per-symbol dedup ledger + pending menus | `alerts.save_state` |

### A.3 The model layer
- **LLM: Gemini 2.5 Pro via Vertex AI** (service-account auth, streaming). It touches only the
  **unstructured** side — reading filing/RHP/concall PDFs and writing prose. **Every number in
  the structured tables is deterministic Python**, never the LLM.
- **Simulation: Monte-Carlo DCF** — numpy `default_rng(seed=42)`, 20,000 draws (pure numpy, no
  scipy). Deterministic given the same inputs.
- **Solvers:** reverse-DCF and SIP-XIRR use **bisection** (no scipy).

---

## Part B — Stock deep report (`reports/deep_brief.py` + `pipeline.generate_report`)

Section numbers match the report. Every table cell traces to `financials` XBRL elements
transformed by `analysis/fundamentals.py`, `forensic.py`, `valuation.py`, `quant.py`.

### Business overview (top of report) — 🤖 LLM
- **Source:** the company's own filings (investor PPT, concall, annual report, results) fetched
  as PDFs (`pipeline._filings_for_analysis` — everything since last FY-end + latest results).
- **Transform → model:** `synthesize.business_overview` feeds those PDFs + verified snapshot
  numbers (market cap, industry) to Gemini, which writes "what it does / segment mix / market
  size". **Grounded in the filings**, cited; the market-cap number is our computed value, not
  the LLM's.

### §1 Income statement
- **Source:** `financials` (P&L elements), consolidated preferred.
- **Transform:** `fundamentals.load_annual/load_quarters` pivots long→wide (index=period_end,
  cols=XBRL elements). Values shown in ₹cr (÷10⁷).
- **Formulas / approximations:**
  - `EBITDA = ProfitBeforeTax + FinanceCosts + Depreciation`
  - `EBIT = ProfitBeforeTax + FinanceCosts`
  - `COGS ≈ CostOfMaterialsConsumed + PurchasesOfStockInTrade + ΔInventories` (documented approx)
- **Model:** none — pure arithmetic.

### §2 Profitability, margins & growth (`fundamentals.quarterly_metrics` / annual)
- `Gross margin % = 100 × (Revenue − COGS) / Revenue`
- `EBITDA margin % = 100 × EBITDA / Revenue` · `EBIT margin %`, `PBT margin %`, `Net margin %` analogous
- `Effective tax % = 100 × TaxExpense / ProfitBeforeTax`
- `Revenue YoY % = Revenue / Revenue(4 quarters ago) − 1` (annual: vs prior FY)
- `Other income / PBT % = 100 × OtherIncome / PBT`
- **Model:** none.

### §3 Balance sheet
- **Source:** annual `financials` BS elements (FY2023+). Shown ₹cr. `Net debt = (BorrowingsCurrent
  + BorrowingsNoncurrent) − CashAndCashEquivalents`. **Model:** none.

### §4 Returns, leverage & liquidity
- `ROE % = 100 × PAT / Equity`
- `ROCE % = 100 × (PBT + FinanceCosts) / (Equity + Debt)`
- `ROIC % = 100 × EBIT(1 − tax_rate) / (Equity + Debt − Cash)`
- `ROA % = 100 × PAT / Assets`
- `Debt/Equity = Debt / Equity` · `Net debt / EBITDA` · `Interest coverage = EBIT / FinanceCosts`
- `Current ratio = CurrentAssets / CurrentLiabilities` · `Quick ratio = (CurrentAssets − Inventories)/CurrentLiabilities`
- **Model:** none.

### §5 Working capital & cash conversion
- `Receivable days = 365 × TradeReceivables / Revenue`
- `Inventory days = 365 × Inventories / COGS`
- `Payable days = 365 × TradePayables / COGS`
- `Cash conversion cycle = Inventory days + Receivable days − Payable days`
- `Asset turnover = Revenue / Assets`
- **Model:** none.

### §6 Cash flow statement
- **Source:** annual `financials` CF elements (`CashFlowsFromUsedInOperating/Investing/Financing`,
  capex = `PurchaseOfPropertyPlantAndEquipment…`). Straight pass-through in ₹cr. **Model:** none.

### §7 Free cash flow & earnings quality (`fundamentals.annual_overview`)
- `FCF = CFO − Capex`
- `FCFF = CFO − Capex + FinanceCosts × (1 − tax_rate)`
- `FCFE = CFO − Capex + NetBorrowing`
- `CFO/PAT`, `CFO/EBITDA` (+ 3- and 5-year rolled averages)
- `Accruals % = 100 × (PAT − CFO) / Assets` (high positive = profit not cash-backed)
- **Model:** none.

### §8 Quarterly P&L trend — last 6 quarters of §2 metrics. **Model:** none.

### §9 Forensic deep-dive (`analysis/forensic.py`) — deterministic scores
A score is emitted **only when every input is present** (missing inputs are listed; never proxied
with zero).

- **Altman Z** = `1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5`, where
  `X1 = (CurrentAssets − CurrentLiabilities)/TotalAssets`, `X2 = OtherEquity/TA` (retained-earnings
  proxy), `X3 = (PBT + FinanceCosts)/TA`, `X4 = MarketCap/TotalLiabilities` (book equity if no
  mcap), `X5 = Revenue/TA`. Bands: >2.99 safe · 1.81–2.99 grey · <1.81 distress.
- **Piotroski F (0–9)** — one point each: ROA>0, CFO>0, ΔROA>0, CFO>NI (accrual), leverage down,
  current-ratio up, no share dilution, gross-margin up, asset-turnover up. Needs current + prior FY.
- **Beneish M** = `−4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI
  + 4.679·TATA − 0.327·LVGI`. Each variable is a current-vs-prior-year ratio (days-sales-in-
  receivables, gross-margin, asset-quality, sales-growth, depreciation, SG&A, total-accruals-to-
  assets, leverage). > −1.78 flags possible manipulation. *SG&A ≈ employee + other expenses; COGS
  as above (documented approximations).* We add a **false-positive caveat** when accruals + cash
  conversion are clean.
- **Sloan accruals %** = `100 × [Δ(non-cash current assets) − Δ(non-debt current liabilities) −
  D&A] / average total assets`. Plus a cash-flow accrual `100×(PAT−CFO)/assets` for corroboration.
- **Promoter pledge** — `shareholding.pledged_pct_of_promoter` (from NSE `/api/corporate-pledgedata`).
- **Insider & promoter trades** — `insider_trades` (SEBI PIT Reg 7(2), via `/api/corporates-pit`):
  each buy/sell with ₹ size and holding % before→after; routine ESOP/off-market filtered; net
  6-month promoter direction summarised. **Model:** none.
- **Shareholding — holder-level (SHP)** — `shp_holders` from the SHP XBRL (`nse_shp.py`): every
  promoter account + public >0.5% holder, each **classified** (individual/HUF · **LISTED company**
  with its symbol, matched against `equity_master` — the Elcid pattern · unlisted pvt · trust ·
  mutual fund · FPI). **Model:** none.
- **Ownership changes (QoQ)** (`analysis/ownership.py`) — diff of the two most recent SHP snapshots,
  holders matched across quarters by normalised name (+ token-subset fallback for scheme relabels)
  → entered / exited / added / trimmed with the pp delta; notable holders (promoter/MF/FPI/listed)
  sorted first. **Model:** none.
- *Contingent liabilities & RPTs* live only in the notes to accounts (not the XBRL), so the
  **Analysis section (LLM)** extracts them from the filing PDFs and flags anything material.

### §10 Valuation (`analysis/valuation.py`, `sector.py`)
- **Shares** = `EquityShareCapital / FaceValue` (both ₹, from the latest annual filing).
- **Market cap** = shares × latest EOD close. *(A bonus/split since the FY-end makes this stale —
  auto-detected from NSE corporate actions and flagged; `detect_share_action`.)*
- `P/E (TTM) = MarketCap / TTM net profit` · `P/B = MarketCap / Equity` · `Earnings yield % =
  100 × TTM net / MarketCap`.
- `EV/EBITDA = (MarketCap + NetDebt) / TTM EBITDA`; **mid-cycle** variant uses the average historical
  EBITDA margin × TTM revenue (so peak/trough cyclicals aren't mis-valued).
- **Valuation lens** (`sector.valuation_lens`, keyword on macro industry): `financial` → P/B-on-ROE,
  `cyclical` → EV/EBITDA mid-cycle, else `earnings` → P/E.
- **Own-history percentile** = % of the stock's own historical multiples ≤ today's
  (`valuation_history` computes per-FY P/E & P/B with **contemporaneous shares**, so it's
  bonus/split-invariant).
- **Peer comparison** (`sector.peers`) — peers share the **granular `basic_industry`** (from NSE
  `getSymbolData.secInfo`, `ingest_basic_industries`), grouped by market-cap tier. Sector percentile =
  `_pctile` (% of peers more expensive). **Model:** none.

### §11 Reverse-DCF + Monte-Carlo DCF (`analysis/quant.py`) — 🎲 simulation
- **Inputs (`dcf_inputs`, all from the company's own history):** base revenue = TTM (else latest
  annual); growth = long-run revenue CAGR **blended 50/50 with recent TTM-vs-prior-TTM momentum**,
  clipped [−5%, +25%]; EBIT margin, D&A%, capex%, NWC% = 5-year means of the ratio to revenue; tax
  rate = 5-yr mean clipped [10%, 35%]; net debt = latest.
- **WACC = CAPM:** `cost_equity = rf + β·ERP` (rf = 7% default, ERP = 5.5%; **β = cov(stock,
  Nifty)/var(Nifty)** over ~2y daily returns, clipped [0.4, 2.0]); `cost_debt = (FinanceCosts/Debt)
  ×(1−tax)`; `WACC = we·cost_equity + wd·cost_debt` (weights by mcap vs debt), clipped [8%, 18%].
  `Terminal growth = min(5%, WACC − 2%)`.
- **The DCF engine (`_value_per_share`):** a **2-stage FCFF model** — revenue growth *fades*
  linearly from `g` (year 1) to `tg` (year 10); `FCFF_t = NOPAT + D&A − Capex − ΔNWC`; discount at
  WACC; terminal value `= FCFF_N·(1+tg)/max(WACC−tg, 0.03)`; `equity = ΣPV − net debt`; per share.
- **Reverse-DCF (`reverse_dcf`):** **bisection** solves for the constant revenue growth that makes
  the DCF equal today's price — "what growth is the price assuming?" Compared to history for
  plausibility. This is the **centrepiece** read.
- **Monte-Carlo (`monte_carlo_dcf`):** **20,000 draws** sampling g, EBIT margin, WACC, terminal g
  from normals around the base (with clips) → distribution of intrinsic value/share. Reports
  **median, p10, p90**, `Margin of safety = (median − price)/median`, `P(undervalued) =
  mean(value > price)`.
- **Financials (banks/NBFCs)** are flagged `is_financial` and the FCFF-DCF is **skipped** (a caveat
  is shown instead of a bogus number). A non-positive base case prints "not meaningful".

### §12 Statistical forensics (`analysis/quant.py`)
- **Benford's law** — over **all** reported financial values for the symbol: observed first-digit
  frequencies vs expected `log10(1 + 1/d)`; **Nigrini MAD** = mean|obs − expected|; bands: <0.006
  close · <0.012 acceptable · <0.015 marginal · else nonconformity (soft flag). **Model:** none.
- **Sector z-scores** — for P/E, P/B, ROE, ROCE, NetMargin, D/E: `z = (target − peer mean)/peer
  std` over the granular-industry peers (≥3 needed; pathological peers bounded out). **Model:** none.

### §13 Technical snapshot (`analysis/technical.py`)
- **Source:** `equity_eod` daily series, **EQ + BE + BZ** (trade-for-trade) series, one row/date.
- `SMA 20/50/200` = rolling means. `EMA 12/26`; `MACD = EMA12 − EMA26`, signal = EMA9(MACD).
  `RSI(14)` = Wilder's smoothing of gains/losses. `Bollinger = SMA20 ± 2·σ20`. `ATR(14)` = Wilder
  average true range. `52-week high/low` = 252-day rolling max/min. `Relative strength` = stock
  return ÷ Nifty return over 63 days. **Delivery %** conviction (spike vs 20-day avg). **Model:** none.

### Trading levels & setup (`technical.levels`, rendered by `deep_brief.render_levels`)
- **Support/resistance zones** from the **confluence** of: swing pivots (±5-bar fractal extremes),
  20/50/200-DMA, 52-week extremes, **volume-by-price** peaks (24 price bins, most-traded = stickiest),
  and round numbers within ±12% of price. Candidates **clustered** (center-bounded, tolerance =
  max(0.75·ATR, 1.2% of price)) → each zone scored by summed source weights (shown as ●○ dots).
- **Market structure** = higher-highs/lows vs lower-highs/lows; **trendlines** = least-squares through
  the last ≤3 swings; **patterns** (range, double top/bottom, head-&-shoulders, triangles) from swing
  geometry (heuristic, each with a confidence).
- **Setup** = the *strongest* support as the accumulation zone; `stop = zone low − 1·ATR`; first
  target = nearest resistance; `reward:risk = (target − entry)/(entry − stop)`; "accumulate" only at
  RR ≥ 1.5, else "watch". **Verdict-aware:** for an Avoid/Reduce verdict it's shown *for reference
  only*. Needs **≥60 trading days** in the store (else omitted). Plus an **annotated candlestick
  chart** (`charts.levels_chart`, matplotlib → PNG in the PDF). **Model:** none (deterministic).

### Analysis (the closing narrative) — 🤖 LLM
- **Source:** the full deterministic brief above **+** the filing PDFs.
- **Model:** `synthesize.synthesize_thesis` → Gemini writes earnings-quality / profitability /
  balance-sheet / growth / **forensic (RPTs, contingent liabilities read from the notes)** /
  valuation sections and a **Verdict (Buy/Accumulate/Hold/Reduce/Avoid)** with reasons. It's told
  to respect the brief's numbers and caveats (never invent). The verdict is parsed back out
  (`verdict_from_text`) to make the Trading-levels setup defer to it.

### Growth-triggers 1-pager (opt-in, reply `1`) — 🤖 LLM
- `synthesize.growth_triggers` reads the filings + verified snapshot facts and writes 5–7 quantified,
  timeline-tagged, conviction-rated catalysts (₹cr & % impact), each cited to a filing. Numbers it's
  given (mcap, TTM revenue) are ours; the ₹-impact estimates are the LLM's, explicitly labelled.

---

## Part C — IPO note (`scrapers/ipo.py`, `pipeline.generate_ipo_report`, `synthesize.ipo_analysis`)

- **Source (all primary NSE):** `/api/ipo-current-issue` (live + subscription), `/api/all-upcoming-
  issues`, `/api/ipo-detail` (QIB/NII/RII); **offer documents** from the archive
  `nsearchives…/content/ipo/<DOC>_<SYM>.zip` — **RHP** (prospectus), **RATIOS** (price-band ad KPIs),
  **ANCHOR** (allotment). No XBRL exists pre-listing, so it's **RHP-driven**, not the quant engine.
- **Analysis — 🤖 LLM:** `ipo_analysis` feeds the RHP + price-band ad + anchor doc + verified issue
  facts to Gemini: snapshot · **offer structure (a Fresh-Issue vs OFS split table, ₹cr + %) and what
  it signals** · restated financials with the trajectory read · valuation-at-band vs listed peers ·
  use of proceeds · RHP risks · demand (subscription + anchor) · **APPLY / AVOID / NEUTRAL** verdict.
  Subscription %, band, lot size are structured facts; the interpretation is the LLM's. **No
  grey-market/GMP** (against the primary-only rule).

---

## Part D — Mutual-fund report (`analysis/funds.py`, `reports/fund_brief.py`)

- **Source:** `mf_nav` (accumulated daily from AMFI `NAVAll.txt`, + `ingest_mf_nav_history` backfill),
  `mf_scheme` (identity), `mf_holdings` (per-AMC monthly XLSX), `index_close` (benchmark).
- **Point returns (`point_returns`):** for each horizon, base NAV = NAV as-of (last date − horizon);
  **CAGR** `((last/base)^(1/years) − 1)` for ≥1y, **absolute** `(last/base − 1)` for <1y.
- **Risk (`risk_metrics`)** from daily NAV % changes: `Vol = std(daily)·√252`; annualised return
  `(1+mean)^252 − 1`; `Sharpe = (annret − rf)/vol`; `Sortino = (annret − rf)/downside-vol`;
  `Max drawdown = min((NAV − running-max)/running-max)`. rf = 6.5% default.
- **Rolling 1-year (`rolling_returns`)** — min/median/max of every 365-day annualised return
  (consistency read).
- **SIP / XIRR (`sip_returns`)** — simulate ₹10k/month; units bought at each month's NAV; **XIRR =
  money-weighted return via bisection** on the dated cashflows (`_xirr`/`_xnpv`).
- **Benchmark-relative (`benchmark_metrics`)** on the overlapping gap-free history vs the category's
  fair index: **β = cov(fund, bench)/var(bench)**; **Jensen's α = (fund_ann − rf) − β·(bench_ann −
  rf)** (endpoint-annualised); **up/down capture** = mean fund return ÷ mean bench return on up/down
  days; **tracking error = std(fund − bench)·√252**; information ratio.
- **Category percentile (`category_percentile`)** — the scheme's horizon return ranked among
  same-category Direct-Growth peers (≥5 needed).
- **Holdings look-through:** `holdings_snapshot` (top-10 concentration, biggest sector),
  `watchlist_overlap` (fund holdings ∩ your watchlist by name), `holdings_churn` (MoM buys/exits/
  adds/trims, **equity positions only** — CDs/CPs/T-bills/TREPS filtered). **Coverage:** holdings only
  for AMCs registered (PPFAS/HDFC/Nippon…); NAV/returns/risk work for **all** ~14k schemes.
- **Model:** none (all deterministic; XIRR is a bisection solver). *Optional LLM fund thesis is a
  later add.*

---

## Part E — Watchlist digests (`scan.py`) — 6 PM full + 12:30 midday

- **Market header:** sectoral indices + India VIX (`index_close` / live `/api/allIndices`), FII/DII
  cash (`/api/fiidiiTradeReact`), **FII index-futures positioning** (`participant_oi` →
  `positioning.fii_index_futures`), USD/INR (FBIL), gold/silver/crude (MCX). Pass-through. **Model:** none.
- **📅 Upcoming (`watchlist_upcoming`):** board-meeting / results / ex-div / split / bonus / AGM dates
  from `/api/corporate-board-meetings` + `/api/event-calendar` + `/api/corporates-corporateActions`
  (date-ranged). Board-meeting **purposes are LLM-labelled** (`synthesize.label_events`) into clean
  English, keyword-heuristic fallback.
- **Movers (`watchlist_movers`):** per stock — close, day %chg, delivery%, **52-week position**, **P/E
  vs own 5-yr median**, and **nearest support/resistance** `(S ₹.. / R ₹..)` from `technical.levels`
  (`_annotate_mover_levels`; computed once per symbol, shared with the alerts). **Model:** none.
- **🎯 Level alerts (`_level_alerts`):** **transition** events (fire once) — 6 PM compares yesterday→
  today close, midday prior-close→live: Tracking = breakout above nearest resistance / pullback into
  strongest support; Holdings = lost 50/200-DMA / broke support. Bucketed Holdings vs Tracking. **Model:** none.
- **Events:** the corporate-event taxonomy (`analysis/alerts.py`) + **institutional bulk/block deals**
  — `watchlist_deals` **dedups across the bulk & block feeds** (one trade reported in both) and
  **classifies the counterparty** (`_classify_client`: listed co · SYMBOL / MF / insurer / FPI / LLP /
  trust / HUF / unlisted / individual) from its name + `equity_master`. Forensic/valuation **flips**
  (Altman/Beneish/Piotroski/CFO-PAT/pledge crossing a band) also fire here, deduped via `alert_state`.
  For notable **document-bearing** events the filing PDF is **auto-read by the LLM** inline
  (`synthesize.analyze_filing` — 🤖). 
- **🔬 Insider & promoter trades:** `insider_trades`, deduped, 5-day recency guard.

---

## Part F — Screeners (discovery)

- **`screen: value` (`analysis/screener.py`)** — over symbols with financials in the universe.
  Composite = `0.40·Quality + 0.35·Forensic + 0.25·Cheapness`, each **rank-normalised to [0,1]**
  cross-sectionally:
  - Quality = Piotroski F (0–9).
  - Forensic (0–4) = +1 each for Altman-safe, Beneish-clean, low accruals, no pledge.
  - Cheapness = `100 − own-history percentile` of the current P/E (P/B for financials).
  A missing pillar maps to 0.5 (doesn't sink the name). Ranked list; reply a number → full deep report.
- **`screen: holdco` (`analysis/holdco.py`)** — the Elcid trade generalised. From `shp_holders`
  rows classified `LISTED company`, invert to `holder → [(investee, pct)]`; **stake NAV = Σ pct% ×
  investee market cap**; **discount = 1 − own market cap / stake NAV**; ranked deepest-first. Only
  *disclosed listed* stakes count (unlisted subs not valued).
- **`screen: investors` / `investor: <name>` (`analysis/investors.py`)** — a curated 25-name roster
  with hand-verified alias token-sets; scan `shp_holders`, sum each investor's vehicles (self+HUF+
  trust) per stock per quarter, diff QoQ (entered/added/trimmed/exited, ≥0.5pp).
- **`screen: smallcap` (`analysis/smallcap.py`)** — capex-led hunt in ₹1,000–10,000 cr. Composite
  weights **capex 0.30 · efficiency 0.25 · cash/balance-sheet 0.20 · forensic 0.15 · smart-money
  0.10**, rank-normalised. Capex metrics: capex vs its 3-yr base, capex ÷ depreciation, capex-
  intensity delta, self-funded (CFO/capex). **Hard gates** exclude traps (revenue shrinking vs 2y
  ago, Altman Z<1.81, Beneish M>−1.78, pledge>25%). **Model:** none for all four.
- **Weekly "Screener movements"** (`screen_digest.py`) — Saturday email with **trigger-based deltas
  only** across the three screens (fingerprints in `alert_state`).

---

## Part G — Every place the LLM is used (exhaustive)

The LLM (**Gemini 2.5 Pro / Vertex AI**) is used **only** here — everything else is deterministic:

| Where | Function | Input | Output |
|---|---|---|---|
| Deep-report Analysis + Verdict | `synthesize.synthesize_thesis` | quant brief + filing PDFs | forensic thesis, Buy→Avoid verdict |
| Business overview | `synthesize.business_overview` | filing PDFs + snapshot facts | "what it does / segments / TAM" |
| Growth-triggers 1-pager | `synthesize.growth_triggers` | filings + snapshot facts | quantified catalysts |
| Forward multiple | `synthesize.extract_guidance` | filings | management's next-year guidance |
| IPO note | `synthesize.ipo_analysis` | RHP + band ad + anchor + facts | APPLY/AVOID note |
| Digest event analysis | `synthesize.analyze_filing` | one filing PDF | inline point-wise read |
| Board-meeting labels | `synthesize.label_events` | event subjects | clean purpose text |
| Symbol resolution | `reports/resolve.py` | free-text name | NSE symbol (LLM + search) |

**Simulation:** Monte-Carlo DCF (§B.11). **Solvers:** reverse-DCF & XIRR (bisection).

---

## Part H — Honest caveats & documented approximations

- **COGS ≈** materials + stock purchases + Δinventories; **EBITDA =** PBT + interest + D&A;
  **SG&A ≈** employee + other expenses (used in Beneish). These are noted at every use.
- **Share count** for market cap comes from the latest annual filing → a bonus/split since then makes
  the current P/E/P/B/mcap slightly stale (auto-detected & flagged).
- **Balance-sheet & cash-flow history** starts FY2023 (older result XBRLs omit them); P&L runs ~6y.
- **Consolidated vs standalone:** consolidated is the default when it exists (falls back to standalone
  if consolidated's XBRL history is ≥2y thinner).
- **DCF is assumption-driven** — the value is the *distribution/sensitivity*, not a point; financials
  (lenders) skip it entirely.
- **Coverage is bounded by what's ingested:** ~750–800 stocks have financials; SHP/holdco/investor
  signals only light up where SHP is ingested; MF holdings cover a handful of AMCs (NAV/returns cover
  all ~14k); levels need ≥60 trading days.
- **Peer set** is the granular `basic_industry`; a name outside the ingested universe is skipped, not
  wrong.
- **The LLM can be wrong** — it reads primary filings but is a language model; the verdict is a
  starting point, and the deterministic numbers above are the ground truth to check it against.

---

*Cross-references: [`DATA_SOURCES.md`](DATA_SOURCES.md) (source scrapability), [`FUNDAMENTALS.md`]
(FUNDAMENTALS.md) (XBRL path + ratios), [`TECHNICAL.md`](TECHNICAL.md) (indicators),
[`REPORTS.md`](REPORTS.md) (report assembly + LLM), [`ALERTS.md`](ALERTS.md) (digests), [`PLAN.md`]
(PLAN.md) (scope & status).*
