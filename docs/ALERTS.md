# Watchlist alerts (Phase 5)

Daily *push*: a scan walks the watchlist, compares today's data against stored
`alert_state`, and pushes an alert (Telegram **or email**, per the `CHANNELS`
flag) only on a genuine change. Runs inside the always-on bot via a **self-healing
gate** — a repeating job fires the scan once per trading day the first time the
bot is up at/after **18:00 IST** (robust to the laptop sleeping through the exact
tick), tracked by `scan.already_scanned_today()`/`mark_scanned()`. **Skips
weekends and NSE trading holidays** — `scan.market_open_today()` checks the equity
(CM) holiday calendar fetched from NSE's `holiday-master` and cached in
`alert_state` (refreshed monthly). Manual `/scan` ignores the gate (always runs).

## Pieces

| Piece | Where |
|---|---|
| Tables `watchlist`, `alert_state`, `shareholding` | `common/db.py` |
| Watchlist CRUD + ensure-data | `watchlist.py` |
| Per-symbol detectors (incl. promoter-pledge) | `analysis/alerts.py` |
| Orchestrator (refresh EOD → announcements + pledge → detect) | `scan.py` |
| Bot commands + self-healing schedule + push | `scripts/telegram_bot.py`, `scripts/email_bot.py` |
| Bulk-add the initial list | `scripts/populate_watchlist.py` |

## Bot commands

- `/watch <name>` — resolve (Gemini+search) → add → ingest financials → seed state
  (buttons if several match).
- `/unwatch <SYMBOL>` · `/watchlist` · `/scan` (run now).

## Events

The pure momentum/trading signals were **dropped** (golden/death cross,
price-vs-200-DMA, RSI overbought/oversold, volume spike) to keep the watchlist
fundamental/forensic rather than trading-oriented.

**Price context (from `equity_eod`, cheap):** 52-week high/low · **delivery-%
spike** (>1.5× 20d — institutional conviction) · big single-day move (>6% — a
"something happened, look for news" trigger).

**Promoter pledge (from `shareholding`, batched browser session):** a rise of
> 1 percentage point in **pledged % of promoter holding** (NSE pledge feed); the
text-based pledge *announcement* alert is kept too.

**Announcements (NSE per-symbol feed, one batched browser session):** new
**results filed** (→ triggers a full deep report + PDF) · dividend/bonus/split/
buyback · credit-rating update · promoter pledge / insider (SAST) · other
disclosures. Categorised from the announcement `desc`/`hasXbrl`.

**Fundamental / forensic (from ingested `financials`):** forensic flip
(Altman band drop, Beneish crossing −1.78, Piotroski drop ≥2) · CFO/PAT falling
below 1 · P/E breaking out of its own historical range.

**Flows (market-wide):** daily FII/DII cash net note.

## How "new" is decided (no spam)

`alert_state` stores last-seen values per `(symbol, key)`: `last_eod_date`,
`last_52w_high/low`, `altman_band`, `beneish_flag`, `piotroski`, `cfo_pat_ok`,
`pe_band`, `pledge_pct`, `last_ann_dt`. Transition events (band changes, pledge
rise) fire once on change; the `last_eod_date` guard prevents re-firing price
events twice the same day; **first sighting of a symbol seeds state silently** (no
day-one flood). State-based dedup also makes a double-run harmless.

## Cost / limits

- Price + fundamental + accruals detection is DB-only (fast, all symbols, seconds).
- Announcement and promoter-pledge detection each use **one** Camoufox session for
  the whole watchlist (batched in-page XHR per symbol) — the slow part of the scan.
- The bot only runs while the laptop is on; a missed 18:00 slot is covered by the
  startup catch-up. True 24/7 needs a small server.
- Fundamental/valuation events need financials ingested; symbols without NSE
  financials still get price/announcement alerts.
