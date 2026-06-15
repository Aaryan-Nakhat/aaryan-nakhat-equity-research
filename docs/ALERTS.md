# Watchlist alerts (Phase 5)

Daily *push*: a scan walks the watchlist, compares today's data against stored
`alert_state`, and pushes a Telegram alert only on a genuine change. Runs inside
the always-on bot via `JobQueue.run_daily` at **18:00 IST** (+ a startup
catch-up if the laptop was off at 18:00). Manual trigger: `/scan`.

## Pieces

| Piece | Where |
|---|---|
| Tables `watchlist`, `alert_state` | `common/db.py` |
| Watchlist CRUD + ensure-data | `watchlist.py` |
| Per-symbol detectors | `analysis/alerts.py` |
| Orchestrator (refresh EOD → detect → assemble) | `scan.py` |
| Bot commands + schedule + push | `scripts/telegram_bot.py` |
| Bulk-add the initial list | `scripts/populate_watchlist.py` |

## Bot commands

- `/watch <name>` — resolve (Gemini+search) → add → ingest financials → seed state
  (buttons if several match).
- `/unwatch <SYMBOL>` · `/watchlist` · `/scan` (run now).

## Events (all 15)

**Technical / price (1–6, from `equity_eod`, every symbol, cheap):** 52-week
high/low · golden/death cross · price-vs-200-DMA cross · RSI overbought/oversold
(on entry) · volume spike (>2× 20d) · **delivery-% spike** (>1.5× 20d) · big
single-day move (>6%).

**Announcements (7–11, NSE per-symbol feed, one batched browser session):** new
**results filed** (→ triggers a full deep report + PDF) · dividend/bonus/split/
buyback · credit-rating update · promoter pledge / insider (SAST) · other
disclosures. Categorised from the announcement `desc`/`hasXbrl`.

**Fundamental / forensic (12–14, from ingested `financials`):** forensic flip
(Altman band drop, Beneish crossing −1.78, Piotroski drop ≥2) · CFO/PAT falling
below 1 · P/E breaking out of its own historical range.

**Flows (15, market-wide):** daily FII/DII cash net note.

## How "new" is decided (no spam)

`alert_state` stores last-seen values per `(symbol, key)`: `last_eod_date`,
`last_52w_high/low`, `altman_band`, `beneish_flag`, `piotroski`, `cfo_pat_ok`,
`pe_band`, `last_ann_dt`. Transition events (crosses, RSI entry, band changes)
fire once on change; the `last_eod_date` guard prevents re-firing technicals
twice the same day; **first sighting of a symbol seeds state silently** (no
day-one flood). State-based dedup also makes a double-run (e.g. startup catch-up)
harmless.

## Cost / limits

- Technical + fundamental detection is DB-only (fast, all symbols, seconds).
- Announcement detection uses **one** Camoufox session for the whole watchlist
  (batched in-page XHR per symbol) — the slow part of the daily scan.
- The bot only runs while the laptop is on; a missed 18:00 slot is covered by the
  startup catch-up. True 24/7 needs a small server.
- Fundamental/valuation events need financials ingested; symbols without NSE
  financials still get price/announcement alerts.
