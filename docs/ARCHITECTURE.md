# Architecture

End to end: primary NSE/BSE data → DuckDB → deterministic fundamental / forensic /
technical / valuation analysis → Gemini writes the thesis → delivered to Telegram
either **on demand** (you name a stock) or **pushed daily** (watchlist events),
with PDF reports and holiday-aware scheduling. Per-area detail lives in
[`SCRAPING.md`](SCRAPING.md), [`FUNDAMENTALS.md`](FUNDAMENTALS.md),
[`TECHNICAL.md`](TECHNICAL.md), [`REPORTS.md`](REPORTS.md), [`ALERTS.md`](ALERTS.md).

## Full pipeline

```
                          PRIMARY SOURCES (government / exchange only)
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │  NSE archives (plain HTTP)   NSE /api/* (Akamai → Camoufox)   BSE api   NSE XBRL    │
   │  • sec_bhavdata (delivery%)  • corporate-announcements        • scrip   • in-bse-fin │
   │  • index closes              • corporate-actions              quote     financials  │
   │  • F&O / participant OI       • fii/dii, holiday-master                  (nsearchives)│
   └───────────────┬──────────────────────────────────────────────────────────┬─────────┘
                   │ scrapers/  (common/http.py decodes; .text-empty gotcha)    │
                   ▼                                                            ▼
   ┌───────────────────────────────────────┐                ┌────────────────────────────┐
   │ bse.py · nse_archives.py · nse_api.py  │                │ nse_financials.py           │
   │ (batched 1-session browser for /api)   │                │ catalog (browser) + XBRL    │
   └───────────────┬───────────────────────┘                │ parse (OneD=Q, FourD=year)  │
                   │                                          └──────────────┬─────────────┘
                   ▼            ingest.py  (idempotent, date-keyed)          ▼
   ╔═══════════════════════════════════════════════════════════════════════════════════╗
   ║                          DuckDB   (common/db.py, data/processed)                    ║
   ║  equity_eod   index_close   participant_oi │ financials │ sector_map │ watchlist     ║
   ║  (OHLCV+deliv%, whole market, ~373 days)   │ (XBRL,long)│ (Nifty500) │ alert_state   ║
   ╚═══════════════════════════════════╤═══════════════════════════════════╤═════════════╝
                                       │  analysis/ (pure functions over DB) │
                                       ▼                                     ▼
   ┌──────────────────────────────────────────────────┐   ┌──────────────────────────────┐
   │ FUNDAMENTAL          FORENSIC        VALUATION     │   │ TECHNICAL                     │
   │ fundamentals.py      forensic.py     valuation.py  │   │ technical.py                  │
   │ • IS/BS/CF, margins  • Altman Z      • P/E,P/B vs   │   │ • SMA/RSI/MACD/Bollinger/ATR  │
   │ • ROE/ROCE/ROIC      • Piotroski F     own history  │   │ • delivery% conviction        │
   │ • FCFF/FCFE, TTM     • Beneish M     sector.py      │   │ • 52w pos · rel-strength      │
   │ • CFO/PAT (3/5yr)    • CFO-vs-PAT    • peer %ile     │   │                               │
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
                          │  TELEGRAM BOT  (scripts/telegram_bot.py)  │
                          │  always-on: run_bot.ps1 + Task Scheduler  │
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
JobQueue 18:00 IST  (+ startup catch-up)
        │
        ▼  market_open_today()?  ── weekend / NSE holiday ──▶ SKIP (no ping)
        │ trading day
        ▼  scan.run_watchlist_scan():
              1. refresh today's EOD (bhavcopy + index + OI)
              2. batched 1-session browser → each symbol's announcements
              3. per symbol → alerts.scan_symbol():  compare today vs alert_state
                    technical(1-6) · fundamental/forensic(12-14) · announcements(7-11)
              4. + FII/DII market note (15)
        │   (alert_state dedup → only real changes fire; 1st-sight seeds silently)
        ▼
   push to Telegram:  🔴/🟢/⚠️/🔔 one line per event
        └─ "results filed" 📄 ──▶ auto-generate full deep report + PDF
```

## Component → file map

| Layer | Does | Files |
|---|---|---|
| **Scrape** | pull primary data (anti-bot handled) | `scrapers/{bse,nse_archives,nse_api,nse_financials}.py`, `common/http.py` |
| **Ingest** | land into DuckDB, idempotent | `ingest.py` |
| **Store** | 7 tables | `common/db.py` → `data/processed/equity.duckdb` |
| **Analyse** | deterministic Python | `analysis/{fundamentals,forensic,valuation,sector,technical,alerts}.py` |
| **Report** | brief → LLM → format | `reports/{brief,deep_brief,resolve,synthesize,pdf,email,pipeline}.py` |
| **LLM** | synthesis + name resolution | Gemini 2.5 Pro via **Vertex** (service account) |
| **Deliver** | bot + daily scan | `scripts/telegram_bot.py`, `scan.py`, `watchlist.py`, `run_bot.ps1` |
