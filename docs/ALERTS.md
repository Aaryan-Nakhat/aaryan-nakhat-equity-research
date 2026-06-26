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

## The digest

Sent by **company name** (ticker in parens — symbols like `RAMASTEEL` are
cryptic), lines-only (no PDFs), with a **point-wise market-context header** (one
emoji-tagged bullet per item) — then three parts. The header (all primary-source):
- **Indices** — Nifty 50 + the key **sectoral Nifty indices** (Bank · Fin Services ·
  IT · Auto · Pharma · FMCG · Metal · Energy · Realty) + **India VIX**
  (`scan.market_context`, all from `index_close`).
- **FII/DII** net-cash (`scan._fii_dii_line` off the `fiidiiTradeReact` feed, batched
  into `market_feeds`).
- **USD/INR + commodities** (`scan._money_line`): USD/INR from **FBIL** (`scrapers/fbil`)
  + near-month **gold / silver / crude** futures from **MCX** (`scrapers/mcx`). Each line
  is best-effort — a failing feed is simply omitted. The three parts:
- **📅 Upcoming:** the watchlist's events in the next ~35 days — board-meeting /
  results dates, ex-dividend / split / bonus dates, AGM / fund-raising
  (`scan.watchlist_upcoming` from NSE's board-meetings + event-calendar +
  corporate-actions feeds, fetched with a date range). Board-meeting **purposes are
  LLM-labelled** in one batched Gemini call (`synthesize.label_events`) into clean
  plain-English (e.g. "Q1 results & dividend"); a keyword heuristic
  (`_bm_purpose`, after-"consider", case-insensitive) is the per-item fallback.
  Every record is parsed best-effort and each digest section is built
  independently, so one malformed filing can never abort the whole scan.
- **Movers (always present):** a per-stock daily snapshot for the whole watchlist
  — close, day %change, delivery%, 52-week position, and **P/E vs the stock's own
  5-yr median** (cheap/rich lens) — sorted biggest-move first
  (`scan.watchlist_movers`). The only data that changes *every* day, so it keeps
  the digest substantive even on quiet event days.
- **Events (when they happen):** the alerts below, grouped under each company. For
  notable **document-bearing** events (results / concall / scheme / rights / QIP),
  the attached filing PDF is **auto-downloaded and read by Gemini** — a concise
  investor analysis (guidance, key numbers, contingent-liability / related-party
  flags) is shown **inline** as a quote block (`scan._enrich_event_docs` →
  `synthesize.analyze_filing`; capped at 5/scan). No PDFs attached.

Built by `scan.format_digest`; all market-wide feeds come from `nse_api.market_feeds`
in one browser session. Shared by the email + Telegram channels.

## Events

The pure momentum/trading signals were **dropped** (golden/death cross,
price-vs-200-DMA, RSI overbought/oversold, volume spike) to keep the watchlist
fundamental/forensic rather than trading-oriented.

**Institutional deals (per stock, daily):** today's **bulk + block deals** from
NSE's large-deal snapshot (`nse_api.large_deals` → one market-wide fetch, filtered
to the watchlist) — names the counterparty (FIIs / MFs / insurers / HNIs) with
BUY/SELL, qty and VWAP. Green for BUY, red for SELL. (No daily *per-stock* FII/DII
cash figure exists anywhere — bulk/block deals are the real daily signal.)

**Price context (from `equity_eod`, cheap):** 52-week high/low · **delivery-%
spike** (>1.5× 20d — institutional conviction) · big single-day move (>6% — a
"something happened, look for news" trigger).

**Promoter pledge (from `shareholding`, batched browser session):** a rise of
> 1 percentage point in **pledged % of promoter holding** (NSE pledge feed); the
text-based pledge *announcement* alert is kept too.

**Corporate events — a defined taxonomy** (NSE per-symbol announcement feed, one
batched browser session; `alerts._categorise`): results · dividend · bonus · stock
split · **rights issue** · buyback · **QIP / fund-raising / preferential** ·
**scheme / M&A / demerger** · **open offer / SAST** · acquisition/disposal ·
**concall / investor meet** · board meeting · AGM/EGM · credit rating · director/
KMP change · **order / contract win** · pledge/charge · delisting. Routine
compliance **noise is skipped** (trading-window, newspaper publication, duplicate
share, investor complaints, Reg-74). Results no longer auto-attach a PDF in the
email digest — request the full report by replying with the stock name.

**Fundamental / forensic (from ingested `financials`):** forensic flip
(Altman band drop, Beneish crossing −1.78, Piotroski drop ≥2) · CFO/PAT falling
below 1 · P/E breaking out of its own historical range. Each line carries a
plain-English read of what the number means.

The market-wide FII/DII daily note was **dropped** — per-stock institutional
activity now comes from the bulk/block deals above.

## How "new" is decided (no spam)

`alert_state` stores last-seen values per `(symbol, key)`: `last_eod_date`,
`last_52w_high/low`, `altman_band`, `beneish_flag`, `piotroski`, `cfo_pat_ok`,
`pe_band`, `pledge_pct`, `last_ann_dt`. Transition events (band changes, pledge
rise) fire once on change; the `last_eod_date` guard prevents re-firing price
events twice the same day; **first sighting of a symbol seeds state silently** (no
day-one flood). State-based dedup also makes a double-run harmless.

**At-least-once delivery:** the scan computes each stock's state advance but does
**not** persist it inline — `scan_symbol(commit=False)` returns the updates, the scan
carries them in `ScanResult.pending_state`, and they're saved by `commit_scan_state`
**only after the digest is actually delivered**. So a scan that crashes (or whose
send fails) leaves the markers untouched and the events resurface on the next run,
instead of being silently consumed. (Seeding callers — `/watch`, `populate_watchlist`
— still use `commit=True`.)

## Cost / limits

- Price + fundamental + accruals detection is DB-only (fast, all symbols, seconds).
- Announcements + promoter-pledge each use **one** Camoufox session for the whole
  watchlist (batched in-page XHR per symbol); bulk/block deals are one more single
  market-wide fetch — these browser calls are the slow part of the scan.
- The **email digest is lines-only (no PDFs)** → sends in seconds even on heavy
  results days; grouped by symbol. Telegram still attaches PDFs (parked channel).
- The bot only runs while the laptop is on; a missed 18:00 slot is covered by the
  self-healing heartbeat (fires once the bot is next up after 18:00). True 24/7
  needs a small server. If the laptop was **off the whole day**, that day can't be
  recovered by the heartbeat — run `scripts/run_scan_once.py` the next morning
  (before market close, so the latest bhavcopy is still the missed day's) to
  backfill that digest; it doesn't set the daily marker, so the evening scan still
  fires normally.
- Fundamental/valuation events need financials ingested; symbols without NSE
  financials still get price/announcement alerts.
