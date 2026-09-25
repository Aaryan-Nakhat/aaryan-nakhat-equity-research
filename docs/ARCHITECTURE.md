# Architecture

End to end: primary NSE/BSE/MCX/FBIL data → DuckDB → deterministic fundamental / forensic /
technical / (sector-appropriate) valuation analysis + signals → LLM writes the thesis →
delivered by email either **on demand** (you name a stock) or **pushed** as a
**pre-market (08:30)**, **midday (12:30)** and **full (18:00)** watchlist digest plus a **weekly
(Sat 18:00) screener-movements** digest, with PDF reports and holiday-aware scheduling. Per-area detail lives
in [`SCRAPING.md`](SCRAPING.md),
[`FUNDAMENTALS.md`](FUNDAMENTALS.md), [`TECHNICAL.md`](TECHNICAL.md),
[`REPORTS.md`](REPORTS.md), [`ALERTS.md`](ALERTS.md).

## Full pipeline

```
                       PRIMARY SOURCES (government / exchange only)
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │ NSE archives (plain HTTP)     NSE /api/* (Akamai → Camoufox)            MCX · FBIL   │
   │ • bhavcopy + delivery%        • corporate-announcements · corp-actions  • gold/silver│
   │ • index closes                • corporates-pit (insider) · fii/dii        /crude(MCX)│
   │ • F&O / participant OI         • NextApi live quote · pledge · holidays  • USD/INR    │
   │ NSE XBRL (nsearchives): in-bse-fin + SEBI in-capmkt (integrated filing)    (FBIL)    │
   └───────────────┬──────────────────────────────────────────────────────────┬─────────┘
                   │ scrapers/  (common/http.py decodes; .text-empty gotcha)    │
                   ▼                                                            ▼
   ┌───────────────────────────────────────┐                ┌────────────────────────────┐
   │ bse · nse_archives · nse_api ·         │                │ nse_financials.py           │
   │ fbil · mcx (batched 1-session browser) │                │ catalog (browser) + XBRL    │
   └───────────────┬───────────────────────┘                │ parse (OneD=Q, FourD=year)  │
                   │                                          └──────────────┬─────────────┘
                   ▼            ingest.py  (idempotent, date-keyed)          ▼
   ╔═══════════════════════════════════════════════════════════════════════════════════╗
   ║                          DuckDB   (common/db.py, data/processed)                    ║
   ║  equity_eod · index_close · participant_oi · financials · sector_map · watchlist    ║
   ║  shareholding · insider_trades · alert_state    (OHLCV+deliv% whole-market; XBRL)   ║
   ╚═══════════════════════════════════╤═══════════════════════════════════╤═════════════╝
                                       │  analysis/ (pure functions over DB) │
                                       ▼                                     ▼
   ┌──────────────────────────────────────────────────┐   ┌──────────────────────────────┐
   │ FUNDAMENTAL          FORENSIC        VALUATION         │   │ TECHNICAL · SIGNALS         │
   │ fundamentals.py      forensic.py     valuation·quant·  │   │ technical.py                │
   │ • IS/BS/CF, margins  • Altman Z      sector             │   │ • SMA/RSI/MACD/BB/ATR       │
   │ • ROE/ROCE/ROIC      • Piotroski F   • lens: P/B-ROE ·  │   │ • delivery% conviction      │
   │ • FCFF/FCFE, TTM     • Beneish M     EV/EBITDA · P/E    │   │ • 52w pos · rel-strength    │
   │ • CFO/PAT (3/5yr)    • CFO-vs-PAT    • own-%ile ·       │   │ positioning.py — FII F&O    │
   │                      • Sloan·Benford reverse-DCF · fwd  │   │   OI; insider (PIT) trades  │
   └───────────────────────────┬──────────────────────────────────────────┬──────────────┘
                               ▼                                            ▼
                   ┌─────────────────────────────────────────────────────────────┐
                   │  reports/                                                   │
                   │  brief.py / deep_brief.py  → one markdown brief (all signals)│
                   │  resolve.py  "name" → NSE symbol (master match → LLM) │
                   │  synthesize.py → the configured LLM (see .env)    │
                   │  pdf.py (HTML→Chromium PDF) · email.py (SMTP)                │
                   └───────────────────────────────┬─────────────────────────────┘
                                                   ▼
                          ┌──────────────────────────────────────────┐
                          │  COMMANDS   bot/app.py  handle_request()   │
                          │  one router for every command; replies via │
                          │  email.send_report(to=requester)           │
                          └──────┬───────────────────────────┬────────┘
                    email sender │                           │ local sender (you@eqr.local)
                                 ▼                           ▼
              ┌───────────────────────────────┐  ┌──────────────────────────────┐
              │ EMAIL BOT (IMAP IDLE + SMTP)   │  │ LOCAL CHANNEL  bot/local.py   │
              │ scripts/email_bot.py launcher  │  │ sink instead of SMTP →        │
              │ always-on: run_*.ps1 + Task Sch│  │ `eqr` CLI (cli.py) · web UI   │
              └───────┬───────────────┬───────┘  └──────────────────────────────┘
                      │               │
          PULL (you ask)        PUSH (scheduled)
```

**One command handler, several channels.** `bot/app.py::handle_request` routes every command and
replies through `reports/email.py::send_report(..., to=req.sender)`. The `eqr` CLI (and the web UI)
build the same request the inbox would, from a local sender address, and register a **local sink**
for that address — so replies are handed back in-process instead of going over SMTP. Routing by
recipient keeps it correct across threads (Pickaxe delivers from a background thread) and leaves
scheduled pushes to the real inbox untouched. A numbered pick is an in-thread reply, so the
thread-scoped menus work identically; local channels only reword email phrasing for display
(`bot.local.localize`).

## Flow A — Pull: you ask for a stock

```
You ▶ email: "Infosys"  (or "Reliance consolidated")    ·   or   ▶ `eqr infosys` in a terminal
        │
        ▼  resolve.py  → exact/old symbol or name match in equity_master (no LLM)
        │               → else LLM+Search, every pick validated/renamed against the live master
   one match? ──run──┐        several (e.g. a group: hdfc, tata)? ──▶ numbered list ──▶ you pick ──┐
                     ▼                                               ▼
        pipeline.generate_report():  ensure financials ingested (on-demand)
              → deep_brief (full IS/BS/CF + ratios + forensic + valuation)
              → synthesize.py  → LLM forensic write-up
        │
        ▼  bot replies in-thread:  analysis inline (HTML)  +  full report as PDF
           (CLI: printed + saved as .md / .html / .pdf under data/outputs/<date>/;
            several matches → numbered list → `eqr pick <n>`)
```

## Flow B — Push: watchlist alerts (daily 18:00 IST)

```
self-healing gate: first heartbeat >=18:00 IST, once per trading day (already_scanned_today)
        │
        ▼  market_open_today()?  ── weekend / NSE holiday ──▶ SKIP
        │ trading day
        ▼  scan.run_watchlist_scan() → ScanResult(results, movers, upcoming, market, insider):
              1. refresh today's EOD (bhavcopy + index + participant OI)
              2. browser sessions: announcements · pledge · market_feeds (deals/board/
                 calendar/actions + fii/dii) · insider_trades (SEBI PIT)
              3. per symbol → alerts.scan_symbol(): today vs alert_state (deduped)
              4. + bulk/block deals · upcoming events · per-stock movers
              5. _enrich_event_docs(): download + LLM-read notable filings (inline)
              6. market header: sectoral indices · VIX · FII/DII · FII-futures positioning
                 (participant_oi) · USD/INR (FBIL) · gold/silver/crude (MCX)
        │   (alert_state dedup → only new events fire; insider deduped via the table)
        ▼
   digest (email), by company name, lines-only, NO PDFs:
        market header · 📅 Upcoming · Movers · Events (inline analysis) · 🔬 Insider trades
        └─ reply with a company name → full on-demand deep report
```

## Flow C — Push: midday "same-day" digest (12:30 IST)

```
heartbeat gate: once/trading-day in the 12:30–14:00 IST window (already_intraday_today)
        │
        ▼  scan.run_intraday_scan() → IntradayResult(movers, filings, insider):
              • live_quotes_batch() — NSE NextApi getSymbolData (live price, %chg, deliv%)
              • today's non-routine filings · today's material insider trades
              (NO EOD ingest — bhavcopy doesn't exist midday; daily dedup untouched)
        ▼
   🔔 lighter "same-day" digest: live Movers · 📄 Filed today · 🔬 Insider (today)
        (the 18:00 digest stays the authoritative deduped record)
```

## Flow D — Push: weekly "screener-movements" digest (Sat 18:00 IST)

```
heartbeat gate: once/ISO-week, Saturday ≥18:00 IST (screen_digest.due_this_week)
        │
        ▼  screen_digest.build_screen_delta() runs all three screens and diffs vs the
              last run's fingerprint (alert_state screen_fp_*):
              • holdco — newly discounted / discount widened ≥5pp
              • fundamental — entering top-15 / climbing ≥10 ranks (deterministic sort)
              • investors — a tracked marquee name's fresh entered/added/trimmed/exited
        ▼  ONE 📡 "Screener movements" email of DELTAS ONLY (nothing crossed → no email);
           fingerprints advance only AFTER a successful send (commit_screen_state)
```

## Flow E — Push: pre-market digest (fires on first wake ≥08:30 IST)

```
heartbeat gate: once/trading-day, FIRST heartbeat in the 08:30–12:00 IST window
                (already_premarket_today), holiday/weekend-skipped. Catch-up by design: this
                machine (a laptop) is asleep at 08:30, so the digest fires whenever it wakes
                — typically the ~09:25 login. Past the 09:15 open it self-relabels to a
                "gap so far" morning snapshot; the 12:30 midday digest takes over after noon.
        │
        ▼  premarket.build_premarket() — four INDEPENDENT best-effort inputs (all plain HTTP):
              • nseix.gift_nifty() — GIFT Nifty, the overnight Nifty future (NSE IX)
              • markets_global.nifty_reference() — Nifty-50 prev close (gap baseline) + India VIX
              • markets_global.overnight_indices() — US (S&P/Nasdaq/Dow) + Asia (Nikkei/HSI), Yahoo
              • markets_global.market_headlines() — Moneycontrol markets RSS
              • positioning.fii_index_futures() — FII index-futures net-long stance
        ▼  implied gap = GIFT Nifty − Nifty prev close → bias; structured brief →
           synthesize.premarket_brief() (the LLM, uncapped) for the "overnight read"
        ▼
   🌅 ONE "Pre-market" email: implied open · gauges · global · headlines · plain-English legend
        (any input can fail without sinking the email — a setup briefing, not a trade call)
```

## Flow F — Push: weekly sector-rotation digest (Sat ≥18:00 IST)

```
heartbeat gate: once/ISO-week, Saturday ≥18:00 IST (scan.sector_rotation_due); reads latest EOD
        │
        ▼  sector_analysis.rank_all_sectors() — every catalog sector scored on relative strength
              vs Nifty + trend + valuation-vs-own-history (all from index_close; no LLM/network)
        ▼  sector_brief.build_sector_rotation() → leaders / laggards / 💎 turning-up-from-cheap
        ▼
   🔄 ONE "Sector rotation" email (also on-demand via `sector: rotation`); week-marker advances
        only after a successful send. Reply `sector: <name>` for the full read on any one.
```

## Flow G — Push: 💨 Tailwind global supply-shock radar (weekly Sat + urgent break-ins pre-market/midday/evening)

```
WEEKLY  gate: once/ISO-week, Saturday ≥18:00 IST (scan.tailwind_due)
        │
        ▼  tailwind.run_tailwind() — a FOUR-TIER agent pipeline, each tier one job, chained:
              ① SCOUT   tailwind._scout_signals — GLOBAL breadth from Google News RSS
                        (scrapers/social.py, + Reddit best-effort) fanned over the chokepoint
                        catalog + generic probes, MERGED with the US Federal Register
                        (scrapers/fedregister.py — official US rules incl. PROPOSED/upcoming; US-only)
              ② ANALYST synthesize.tailwind_analyst — triage signals → genuine disruptions
                        (export ban/quota/tariff/cut/shortage); returns the source SIGNAL INDEX not a
                        free URL, so every citation is a real fetched link — the anti-hallucination gate
              ③ MAPPER  synthesize.tailwind_beneficiaries — web-search-grounded → candidate
                        Indian listed beneficiaries (alternate producers / substitutes), per disruption
              ④ AUDITOR tailwind.auditor — verify each vs equity_master (reuses supply_chain._verify
                        / _implausible), drop implausible/blocklisted, flag watchlist hits, rank
        ▼  sorted: watchlist-hits → severity → #beneficiaries
        →  tailwind_brief.build_tailwind_report() → 💨 section (each catalyst + SOURCE LINK +
              verified names 🟢 curated / 🟡 AI-verified / ⭐ watchlist); catalyst keys → scan.add_tailwind_seen
        ▼
   💨 ONE "Tailwind" email (also on-demand via `tailwind`); week-marker advances only after a
        successful send (or a clean empty result). Reply with a symbol/name → that stock's deep report.

URGENT  gate: trading day Mon–Fri, at 3 IST slots — pre-market 08:30 / midday 12:30 / evening 18:00,
        once per slot (scan.tailwind_urgent_slot_done); Saturday skipped (weekly covers it).
        bot.app.maybe_tailwind_urgent (_current_urgent_slot picks the due slot; catch-up-friendly)
        │
        ▼  tailwind.run_tailwind_urgent() — the LIGHTER pass: Scout + Analyst only (cheap), keep FRESH,
              in-effect/proposed disruptions NOT in scan.tailwind_seen_keys, map+audit a bounded set
              (highest-severity first), then keep a catalyst only if it has ≥1 verified beneficiary AND
              is high-severity OR carries a small/mid-cap name (mcap < ₹25k cr)
        ▼
   💨 "Fresh supply shock — <date> (<slot>)" email ONLY when a fresh actionable shock lands (most
        slots: nothing → silent). Runs the ~1–2 min pipeline at most once per slot (marks done
        regardless); the seen-set (scan.add_tailwind_seen, ~2-wk TTL) stops it re-alerting a shock a
        prior slot, day, or the weekly already showed.

   (An idea generator — every catalyst is source-cited; "no clean listed beneficiary" is a valid,
    un-forced answer. Reddit is best-effort; Google News carries global sourcing, Federal Register the US leg.)
```

## Flow H — Push: ⛏️ Pickaxe surging-demand radar (monthly, 1st Sat) + on-demand `pickaxe`

```
MONTHLY gate: once/calendar-month, first Saturday ≥18:00 IST (scan.pickaxe_due) — bot.app.maybe_pickaxe
        │
        ▼  pickaxe.run_pickaxe() — the DEMAND-SIDE mirror of Tailwind, a FOUR-TIER agent pipeline:
              ① SCOUT   pickaxe._scout_signals — Google Trends rising "buy" queries by consumer
                        category (scrapers/trends.py — the BROWSER TIER: Camoufox loads the real
                        explore page and INTERCEPTS the widgetdata XHRs it fires, like nse_api;
                        BEST-EFFORT + circuit-broken, Google rate-limits by IP → LLM path carries it),
                        MERGED with demand-surge / price-hike Google News (+ Reddit) via scrapers/social.py
              ② ANALYST synthesize.pickaxe_analyst — triage signals → genuine, DURABLE demand THEMES
                        (reject fads/seasonal); returns the source SIGNAL INDEX not a free URL (anti-
                        hallucination), with co-trending confirmation terms + india_supply + durability
              ③ MAPPER  synthesize.pickaxe_beneficiaries — web-search-grounded → Indian listed names
                        in TWO layers: direct (makes the product) + INDIRECT "pickaxe" (the arms-dealer
                        to the boom — feed/vaccine/ingredient/equipment/packaging/logistics)
              ④ AUDITOR pickaxe.auditor — verify each vs equity_master (reuses supply_chain._verify /
                        _implausible), attach market-cap tier + a smart-money read (ownership.ownership_
                        changes) + cyclicality tag, flag watchlist, RANK so indirect pickaxes surface first
                        (the Mapper is required to ALWAYS return names — an empty theme is never surfaced)
              ⑤ ENRICH  pickaxe.enrich_report — for EVERY name: pipeline.ensure_ingested → exact quant
                        (valuation.snapshot price/PE/PB, sector.sector_valuation PE-vs-sector,
                        technical.levels support/resistance) + synthesize.pickaxe_projection (grounded
                        filing/concall read → revenue-share now→next-FY, growth, SOURCES)
              ⑥ CHARTS  trends.interest_details (one browser session) → charts.pickaxe_trend_chart per
                        theme (12-mo Google-Trends interest line) → the attached PDF
        ▼  sorted: watchlist → indirect-before-direct → lower-cyclicality → accumulating → smaller-cap
        →  pickaxe_brief.build_pickaxe_report() → ⛏️ per-stock BLOCKS (quant · rev-from-theme now→next ·
              growth · sources · why; ⛏️ pickaxe / 🎯 direct badges) + Trends charts. Cached 24h (no PNGs).
        ▼
   ⛏️ ONE "Pickaxe" email + charted PDF. The full build is ~10-15 min, so it runs in a BACKGROUND thread
        (bot.app._pickaxe_worker, single-build lock): on-demand `pickaxe` acks instantly & delivers when
        ready; the monthly push runs off-heartbeat. `--latest` forces fresh; reply a number → deep report.

   (An idea generator — every theme is source-cited; a genuine demand theme ALWAYS has beneficiaries.
    Google Trends is best-effort — when throttled, the LLM/Search Analyst+Mapper carry the pipeline.)
```

## Flow I — 🎙️ Concalls: market-wide earnings-call scan (background ingest + weekly Sat push + on-demand `concalls`)

```
INGEST  gate: ~hourly, background (scan.concall_ingest_due) — bot.app.maybe_concall_ingest (off-heartbeat
        thread, _concall_lock). Cost-bounded: persist once, read cheap.
        ▼  call_radar.pending_transcripts — ONE market-wide date-ranged sweep
              (nse_api.corporate_announcements(from_date,to_date)) → new TRANSCRIPT filings
              (alerts._categorise + "transcript") for symbols WITH FINANCIALS, deduped vs concall_signals
        ▼  call_radar.ingest_new — score a BOUNDED BATCH (~8/pass; a results-season backlog drains over runs):
              • Management Tone (WORDS)   synthesize.concall_signal → 5-band Very Confident..Defensive (LLM)
              • Execution      (NUMBERS)  call_radar._execution_band → 5-band Firing..Struggling, from
                                          fundamentals.quarterly_metrics (YoY growth + margin trend) — NOT the LLM
              • Say-Do Gap + Signal(0-100) call_radar._gap / _signal_score — divergence weighted most
        →  upsert into concall_signals (tone, execution, gap, signal_score, takeaways, guidance, url)

SERVE   on-demand `concalls` (bot.app._send_call_radar) and the WEEKLY Sat ≥18:00 push
        (scan.call_radar_due / bot.app.maybe_call_radar) both just READ + RANK the table
        (call_radar.radar → call_radar_brief.build_call_radar) — NO LLM at request time.
        ▼
   🎙️ ranked list of the most notable calls (biggest tone-vs-execution gaps first); reply a number → deep report.
   (A discovery screen, not a call — every read is cited to the transcript.)
```

## Flow J — 📈 Results Radar just-reported strength (background refresh + weekly Sat push + on-demand `results`)

```
REFRESH gate: ~hourly, background (scan.results_ingest_due) — bot.app.maybe_results_ingest (off-heartbeat,
        _results_lock). No LLM, no new table — financials.filing_date IS the "just reported" marker.
        ▼  results_radar.recent_filers — the SAME market-wide date-ranged sweep Concalls uses
              (nse_api.corporate_announcements(from_date,to_date)), filtered to "Results filed"
              (alerts._categorise is_result) for symbols WITH FINANCIALS
        ▼  results_radar.refresh_new — for a BOUNDED batch (~6) whose stored latest quarter is STALE:
              ingest.ingest_financials(symbol, max_filings=2) → lands the fresh quarter

SERVE   on-demand `results` (bot.app._send_results) and the WEEKLY Sat ≥18:00 push
        (scan.results_radar_due / bot.app.maybe_results) both COMPUTE + RANK on-the-fly:
        ▼  results_radar.radar — names whose latest quarter filed within ~35d, scored by
              magnitude (YoY growth) + acceleration (vs prior ~3 quarters) + margin inflection,
              with the shared fundamentals.execution_band (Firing..Struggling) — ranked best-first
        →  results_brief.build_results
        ▼
   📈 ranked list of the strongest just-reported quarters; reply a number → deep report.
   (No analyst consensus — growth vs the company's OWN history, not beat-vs-street. A discovery screen.)
```

## Component → file map

| Layer | Does | Files |
|---|---|---|
| **Scrape** | pull primary data (anti-bot handled) + Tailwind signals + Pickaxe Google-Trends demand signals + AmbitionBox employer reviews | `scrapers/{bse,nse_archives,nse_api,nse_financials,nse_shp,nseix,markets_global,fbil,mcx,amfi,mf_holdings,ipo,social,fedregister,trends,ambitionbox}.py`, `common/http.py` |
| **Ingest** | land into DuckDB, idempotent | `ingest.py` |
| **Store** | 17 tables (incl. `shareholding`, `insider_trades`, `mf_scheme`/`mf_nav`/`mf_amc`/`mf_holdings`, `concall_signals`, `alert_keywords`) | `common/db.py` → `data/processed/equity.duckdb` |
| **Analyse** | deterministic Python (sector-lens valuation, MC/reverse-DCF, forensic, FII positioning, MF returns/risk, ownership-diff + **smart-money cost/booking-risk**, holdco-discount + fundamental screeners, **volume-breakout / market-beaters / institutional-buying discovery + 🔥 multi-signal Hotlist**, marquee-investor tracking, **top-down sector analysis + rotation**, **supply-chain mapping**, **💨 Tailwind global supply-shock → beneficiaries**, **⛏️ Pickaxe surging-demand → indirect beneficiaries**, **🎙️ Concalls earnings-call tone-vs-execution**, **📈 Results Radar just-reported strength**, **🏢 employer sentiment**, **🏦 bank analysis — NII / NIM / NPAs / capital + health checks**) | `analysis/{fundamentals,lenders,forensic,valuation,sector,sector_analysis,supply_chain,technical,technical_screen,quant,alerts,positioning,funds,ownership,booking_risk,holdco,screener,fundamental_screens,momentum,leaders,accumulation,hotlist,investors,tailwind,pickaxe,call_radar,results_radar,keyword_alerts,employer_sentiment}.py` |
| **Report** | stock brief (+ quant + charts) → LLM → format/PDF; **fund report**; **sector report**; **pre-market digest**; **Tailwind brief**; **Pickaxe brief**; **Concalls brief**; **Results Radar brief**; shared markdown-table helper | `reports/{brief,deep_brief,fund_brief,sector_brief,premarket,tailwind_brief,pickaxe_brief,call_radar_brief,results_brief,resolve,synthesize,charts,pdf,email,inbox,pipeline,glossary,md}.py` |
| **Config** | single source of truth for every tunable knob — timezone, schedule, feature toggles, cache TTLs, timeouts, market-cap bands, analytical thresholds; read from env with defaults = original behaviour | `config.py`, `.env.example` |
| **LLM** | synthesis + filing/guidance extraction + concall-tone read + name resolution | the configured LLM (any provider, via .env) |
| **Deliver** | bot(s) + pushes: pre-market (08:30), midday (12:30), full (18:00), weekly (Sat 18:00) screener-movements + sector-rotation + Tailwind + Concalls + Results Radar, monthly (1st Sat 18:00) Pickaxe, urgent Tailwind break-ins (pre-market/midday/evening on trading days), ~hourly background Concalls + Results ingest; **mailbox housekeeping**. Commands: `fund:`/`ipo:`/`screen: value·volume·beaters·institutions·margins·deleverage·quality·holdco·investors·smallcap·technical·policy`/`hotlist`/`concalls`/`results`/`sector: <name>·list·rotation`/`suppliers:`/`investor:`/`sell·raise·trim`/`booking`/`policy`/`tailwind`(+`--latest`)/`pickaxe`(+`--latest`)/`alert: <kw>·alerts·unalert:`/`levels:`/`help`; opt-in upside-drivers menu. The same commands run from the terminal via the `eqr` CLI (local channel, no SMTP) | `bot/app.py` (command router + email bot; launched by `bot/app.py` (launched by `scripts/email_bot.py`)), `bot/local.py` (local channel), `cli.py` (`eqr`), `common/env.py` (`.env` loader), `reports/{inbox,premarket,tailwind_brief,pickaxe_brief,call_radar_brief,results_brief}.py`, `scan.py`, `screen_digest.py`, `mail_cleanup.py`, `watchlist.py`, `run_email_bot.ps1` |

## Configuration (`config.py`)

Every tunable knob lives in one place — **`src/equity_research/config.py`** — read from the
environment (a gitignored `.env`) via small typed helpers (`env_str/int/float/bool/csv/time/times/
weekday`). **Every value has a default equal to the original behaviour**, so a fresh clone runs
identically with zero configuration; modules `from equity_research import config` and read
`config.EOD_HOUR`, `config.TZ`, etc. rather than hardcoding. Two documentation tiers in
[`.env.example`](../.env.example) (the split is only how prominently a var appears — all are equally
overridable):

- **Tier 1 (headline).** `TIMEZONE`; the delivery schedule (`PUSH_PREMARKET`, `PUSH_MIDDAY`,
  `PUSH_EOD_HOUR`, `WEEKLY_PUSH_DAY`, `TAILWIND_URGENT_SLOTS` — empty disables urgent alerts,
  `HEARTBEAT_SECONDS`, `MENU_TTL_HOURS`); per-push **feature toggles** (`ENABLE_PREMARKET … ENABLE_
  MAIL_HOUSEKEEPING`, all default on, gated at the heartbeat call sites so on-demand email commands
  still work when a push is off); cache TTLs; render/screen timeouts; market-cap bands
  (`SMALLCAP/MIDCAP/LARGECAP_MAX_CR`); `RISK_FREE_RATE`; `BIG_MOVE_PCT`.
- **Tier 2 (advanced).** Deep analytical thresholds — background-pass cadence gaps, result caps,
  screen liquidity/turnover floors, ownership deltas, radar windows, projection horizon, fund knobs.

**India-market structural assumptions are NOT config** — Nifty benchmarks, the ₹-crore scale, the
data sources (NSE/BSE/PIB/AMFI/FBIL/MCX), `geo=IN` and SEBI-disclosure concepts are baked in; this is
an India (NSE/BSE) build, and `TIMEZONE` mainly shifts *delivery* times rather than making it a
different market's tool. Secrets/identity (SMTP/IMAP, `LLM_*`, allowlist, watchlist) are read directly
by their own modules; the ToS opt-ins (`NSE_SCRAPING_ENABLED`, `EMPLOYER_REVIEWS_ENABLED`) stay where
they are enforced.
