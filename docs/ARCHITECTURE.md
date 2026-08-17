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
                   │  synthesize.py → GEMINI 2.5 Pro via Vertex (workplace SA)    │
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
You ▶ Telegram: "Adani Power"  (or "Reliance consolidated")
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

## Flow E — Push: pre-market digest (08:30 IST)

```
heartbeat gate: once/trading-day in the 08:30–09:00 IST window (already_premarket_today),
                holiday/weekend-skipped; cut off at 09:00 (open ~09:15)
        │
        ▼  premarket.build_premarket() — four INDEPENDENT best-effort inputs (all plain HTTP):
              • nseix.gift_nifty() — GIFT Nifty, the overnight Nifty future (NSE IX)
              • markets_global.nifty_reference() — Nifty-50 prev close (gap baseline) + India VIX
              • markets_global.overnight_indices() — US (S&P/Nasdaq/Dow) + Asia (Nikkei/HSI), Yahoo
              • markets_global.market_headlines() — Moneycontrol markets RSS
              • positioning.fii_index_futures() — FII index-futures net-long stance
        ▼  implied gap = GIFT Nifty − Nifty prev close → bias; structured brief →
           synthesize.premarket_brief() (Gemini, uncapped) for the "overnight read"
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

## Component → file map

| Layer | Does | Files |
|---|---|---|
| **Scrape** | pull primary data (anti-bot handled) | `scrapers/{bse,nse_archives,nse_api,nse_financials,nse_shp,nseix,markets_global,fbil,mcx,amfi,mf_holdings,ipo}.py`, `common/http.py` |
| **Ingest** | land into DuckDB, idempotent | `ingest.py` |
| **Store** | 13 tables (incl. `shareholding`, `insider_trades`, `mf_scheme`/`mf_nav`/`mf_amc`/`mf_holdings`) | `common/db.py` → `data/processed/equity.duckdb` |
| **Analyse** | deterministic Python (sector-lens valuation, MC/reverse-DCF, forensic, FII positioning, MF returns/risk, ownership-diff, holdco-discount + fundamental screeners, marquee-investor tracking, **top-down sector analysis + rotation**, **supply-chain mapping**) | `analysis/{fundamentals,forensic,valuation,sector,sector_analysis,supply_chain,technical,quant,alerts,positioning,funds,ownership,holdco,screener,investors}.py` |
| **Report** | stock brief (+ quant + charts) → LLM → format/PDF; **fund report**; **sector report** (index tech/valuation + smart-money + picks); **pre-market digest**; shared markdown-table helper | `reports/{brief,deep_brief,fund_brief,sector_brief,premarket,resolve,synthesize,charts,pdf,email,inbox,pipeline,glossary,md}.py` |
| **LLM** | synthesis + filing/guidance extraction + name resolution | LLM via **Vertex** (service account) |
| **Deliver** | bot(s) + pre-market (08:30), midday (12:30) & full (18:00) scans + weekly (Sat 18:00) screener-movements digest; channel via `CHANNELS`; `fund: <name>` → fund report; `ipo: ongoing/upcoming` → IPO note; `screen: value/holdco/investors/technical`, `sector: <name>`/`sector: rotation`, `suppliers: <company>`, `investor: <name>`, `sell`/`raise`/`trim` → screeners; opt-in deeper-cut menu (growth triggers) | `scripts/telegram_bot.py`, `scripts/email_bot.py`, `reports/{inbox,premarket}.py`, `scan.py`, `screen_digest.py`, `watchlist.py`, `run_bot.ps1`, `run_email_bot.ps1` |
