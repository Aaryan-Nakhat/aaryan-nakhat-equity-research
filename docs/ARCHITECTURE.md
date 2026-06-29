# Architecture

End to end: primary NSE/BSE/MCX/FBIL data → DuckDB → deterministic fundamental / forensic /
technical / (sector-appropriate) valuation analysis + signals → Gemini writes the thesis →
delivered by email (or Telegram) either **on demand** (you name a stock) or **pushed** as a
**midday (12:30)** and **full (18:00)** watchlist digest, with PDF reports and holiday-aware
scheduling. Per-area detail lives in [`SCRAPING.md`](SCRAPING.md),
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
                   │  resolve.py  "name" → NSE symbol (Gemini + Google Search)   │
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
        ▼  resolve.py  → Gemini+Search → NSE symbol(s)
   one match? ──run──┐        several? ──▶ buttons ──▶ you tap one ──┐
                     ▼                                               ▼
        pipeline.generate_report():  ensure financials ingested (on-demand)
              → deep_brief (full IS/BS/CF + ratios + forensic + valuation)
              → synthesize.py  → Gemini forensic write-up
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
              5. _enrich_event_docs(): download + Gemini-read notable filings (inline)
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

## Component → file map

| Layer | Does | Files |
|---|---|---|
| **Scrape** | pull primary data (anti-bot handled) | `scrapers/{bse,nse_archives,nse_api,nse_financials,fbil,mcx}.py`, `common/http.py` |
| **Ingest** | land into DuckDB, idempotent | `ingest.py` |
| **Store** | 9 tables (incl. `shareholding`, `insider_trades`) | `common/db.py` → `data/processed/equity.duckdb` |
| **Analyse** | deterministic Python (sector-lens valuation, MC/reverse-DCF, forensic, FII positioning) | `analysis/{fundamentals,forensic,valuation,sector,technical,quant,alerts,positioning}.py` |
| **Report** | brief (+ quant + charts) → LLM → format/PDF | `reports/{brief,deep_brief,resolve,synthesize,charts,pdf,email,inbox,pipeline,glossary}.py` |
| **LLM** | synthesis + filing/guidance extraction + name resolution | Gemini 2.5 Pro via **Vertex** (service account) |
| **Deliver** | bot(s) + midday (12:30) & full (18:00) scans; channel via `CHANNELS` | `scripts/telegram_bot.py`, `scripts/email_bot.py`, `reports/inbox.py`, `scan.py`, `watchlist.py`, `run_bot.ps1`, `run_email_bot.ps1` |
