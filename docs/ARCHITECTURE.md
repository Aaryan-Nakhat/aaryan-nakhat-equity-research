# Architecture

End to end: primary NSE/BSE/MCX/FBIL data → DuckDB → deterministic fundamental / forensic /
technical / (sector-appropriate) valuation analysis + signals → LLM writes the thesis →
delivered by email (or Telegram) either **on demand** (you name a stock) or **pushed** as a
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
                   │  resolve.py  "name" → NSE symbol (LLM + Google Search)   │
                   │  synthesize.py → the configured LLM via the provider (a cloud SA)    │
                   │  pdf.py (HTML→Chromium PDF) · email.py (SMTP)                │
                   └───────────────────────────────┬─────────────────────────────┘
                                                   ▼
                          ┌──────────────────────────────────────────┐
                          │  DELIVERY  (CHANNELS env: telegram | email)│
                          │  telegram_bot.py  ·  email_bot.py (IMAP)   │
                          │  always-on: run_*.ps1 + Task Scheduler     │
                          └───────────────┬───────────────┬──────────┘
                                          │               │
                              PULL (you ask)        PUSH (scheduled)
```

## Flow A — Pull: you ask for a stock

```
You ▶ Telegram: "example power"  (or "Reliance consolidated")
        │
        ▼  resolve.py  → LLM+Search → NSE symbol(s)
   one match? ──run──┐        several? ──▶ buttons ──▶ you tap one ──┐
                     ▼                                               ▼
        pipeline.generate_report():  ensure financials ingested (on-demand)
              → deep_brief (full IS/BS/CF + ratios + forensic + valuation)
              → synthesize.py  → LLM forensic write-up
        │
        ▼  bot replies:  analysis inline (MarkdownV2)  +  full report as PDF
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
   digest (email | telegram), by company name, lines-only, NO PDFs:
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

## Flow G — Push: 💨 Tailwind global supply-shock radar (weekly Sat + mid-week urgent break-in)

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
              ③ MAPPER  synthesize.tailwind_beneficiaries — Google-Search-grounded → candidate
                        Indian listed beneficiaries (alternate producers / substitutes), per disruption
              ④ AUDITOR tailwind.auditor — verify each vs equity_master (reuses supply_chain._verify
                        / _implausible), drop implausible/blocklisted, flag watchlist hits, rank
        ▼  sorted: watchlist-hits → severity → #beneficiaries
        →  tailwind_brief.build_tailwind_report() → 💨 section (each catalyst + SOURCE LINK +
              verified names 🟢 curated / 🟡 AI-verified / ⭐ watchlist); catalyst keys → scan.add_tailwind_seen
        ▼
   💨 ONE "Tailwind" email (also on-demand via `tailwind`); week-marker advances only after a
        successful send (or a clean empty result). Reply with a symbol/name → that stock's deep report.

URGENT  gate: trading day Mon–Fri, ≥18:00 IST, once/day (scan.already_tailwind_urgent_today);
        Saturday skipped (weekly covers it). email_bot.maybe_tailwind_urgent
        │
        ▼  tailwind.run_tailwind_urgent() — the LIGHTER pass: Scout + Analyst only (cheap), keep just
              FRESH, high-severity, in-effect/proposed disruptions NOT in scan.tailwind_seen_keys, then
              map+audit only those; keep only catalysts with ≥1 verified beneficiary
        ▼
   💨 "Fresh supply shock" email ONLY when a big new shock lands (most days: nothing → silent). Runs
        the ~1–2 min pipeline at most once/trading-evening (marks done regardless); the seen-set
        (scan.add_tailwind_seen, ~2-wk TTL) stops it re-alerting a shock the weekly or a prior day showed.

   (An idea generator — every catalyst is source-cited; "no clean listed beneficiary" is a valid,
    un-forced answer. Reddit is best-effort; Google News carries global sourcing, Federal Register the US leg.)
```

## Flow H — Push: ⛏️ Pickaxe surging-demand radar (monthly, 1st Sat) + on-demand `pickaxe`

```
MONTHLY gate: once/calendar-month, first Saturday ≥18:00 IST (scan.pickaxe_due) — email_bot.maybe_pickaxe
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
              ③ MAPPER  synthesize.pickaxe_beneficiaries — Google-Search-grounded → Indian listed names
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
        (email_bot._pickaxe_worker, single-build lock): on-demand `pickaxe` acks instantly & delivers when
        ready; the monthly push runs off-heartbeat. `--latest` forces fresh; reply a number → deep report.

   (An idea generator — every theme is source-cited; a genuine demand theme ALWAYS has beneficiaries.
    Google Trends is best-effort — when throttled, the LLM/Search Analyst+Mapper carry the pipeline.)
```

## Component → file map

| Layer | Does | Files |
|---|---|---|
| **Scrape** | pull primary data (anti-bot handled) + Tailwind signals + Pickaxe Google-Trends demand signals | `scrapers/{bse,nse_archives,nse_api,nse_financials,nse_shp,nseix,markets_global,fbil,mcx,amfi,mf_holdings,ipo,social,fedregister,trends}.py`, `common/http.py` |
| **Ingest** | land into DuckDB, idempotent | `ingest.py` |
| **Store** | 13 tables (incl. `shareholding`, `insider_trades`, `mf_scheme`/`mf_nav`/`mf_amc`/`mf_holdings`) | `common/db.py` → `data/processed/equity.duckdb` |
| **Analyse** | deterministic Python (sector-lens valuation, MC/reverse-DCF, forensic, FII positioning, MF returns/risk, ownership-diff + **smart-money cost/booking-risk**, holdco-discount + fundamental screeners, marquee-investor tracking, **top-down sector analysis + rotation**, **supply-chain mapping**, **💨 Tailwind global supply-shock → beneficiaries**, **⛏️ Pickaxe surging-demand → indirect beneficiaries**) | `analysis/{fundamentals,forensic,valuation,sector,sector_analysis,supply_chain,technical,quant,alerts,positioning,funds,ownership,booking_risk,holdco,screener,investors,tailwind,pickaxe}.py` |
| **Report** | stock brief (+ quant + charts) → LLM → format/PDF; **fund report**; **sector report**; **pre-market digest**; **Tailwind brief**; **Pickaxe brief**; shared markdown-table helper | `reports/{brief,deep_brief,fund_brief,sector_brief,premarket,tailwind_brief,pickaxe_brief,resolve,synthesize,charts,pdf,email,inbox,pipeline,glossary,md}.py` |
| **LLM** | synthesis + filing/guidance extraction + name resolution | LLM via **the provider** (service account) |
| **Deliver** | bot(s) + pushes: pre-market (08:30), midday (12:30), full (18:00), weekly (Sat 18:00) screener-movements + sector-rotation + Tailwind, monthly (1st Sat 18:00) Pickaxe, mid-week urgent Tailwind; **mailbox housekeeping**; channel via `CHANNELS`. Commands: `fund:`/`ipo:`/`screen: value·holdco·investors·smallcap·technical·policy`/`sector: <name>·list·rotation`/`suppliers:`/`investor:`/`sell·raise·trim`/`booking`/`policy`/`tailwind`(+`--latest`)/`pickaxe`(+`--latest`)/`levels:`/`help`; opt-in growth-triggers menu | `scripts/telegram_bot.py`, `scripts/email_bot.py`, `reports/{inbox,premarket,tailwind_brief,pickaxe_brief}.py`, `scan.py`, `screen_digest.py`, `mail_cleanup.py`, `watchlist.py`, `run_bot.ps1`, `run_email_bot.ps1` |
