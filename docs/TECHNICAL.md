# Technical analysis (Phase 3)

Indicators computed from the daily EOD series (NSE bhavcopy, incl. delivery %), read through
the **`equity_eod_adj` view — adjusted for splits, bonuses, consolidations, rights issues and
demergers** (see *Corporate-action adjustment* below). All in `analysis/technical.py`; pure
functions over the price history.

## Corporate-action adjustment (`analysis/corporate_actions.py`)

NSE's bhavcopy is **unadjusted**: on Adani Power's 1:5 split (ex 22-Sep-2025) the close simply
went ₹709 → ₹170. Uncorrected, that bends every moving average, fakes a 52-week high, wrecks
3/6/12-month returns and beta, and mis-places smart-money cost zones. So each action gets a
**price factor** in `price_adjustments`, and the `equity_eod_adj` view scales every earlier price by
the product of later factors (volumes by the inverse; `prev_close` by the factor of the session it
belongs to). Actions dated after the last session on file aren't applied yet.

| Action | Factor (new ÷ old price) | Source |
|---|---|---|
| Split F1 → F2 | F2 / F1 | NSE corporate-action subject |
| Bonus a:b (a new per b held) | b / (a + b) | NSE subject |
| Consolidation F1 → F2 | F2 / F1 (> 1) | NSE subject |
| Rights a:b at face value + premium | theoretical ex-rights price ÷ previous close = (b·P + a·issue) / ((a+b)·P); no adjustment when the issue price ≥ market | NSE subject + face value (NSE equity lists, else filings) + the ex-date's previous close |
| Demerger | the ex-date's **discovered open** (NSE's special pre-open session) ÷ previous close | NSE record + bhavcopy |
| ETF unit split / any split-shaped gap with no NSE record | the standard ratio the opening gap matches (1/2, 1/3, 1/5, 1/10 …) | bhavcopy (`prev_close` in the same file) |

Rules that keep it honest: NSE's record wins where it exists (and is filed under a company's
former symbols too, but only where that name traded at the ex-date). With NSE's record available,
a **stock** gap it doesn't explain is recorded as `unexplained` and **not** applied — never guessed;
with NSE access off (the default), a gap that unambiguously matches a standard ratio is applied
and labelled "could also be a demerger". Listing days (the previous close is the issue price) and
rights entitlements (`…-RE`, which collapse at expiry) are excluded. Validated on the live store:
the rights formula lands within ~5% of the actual opening prices (Calsoft 0.578 vs 0.596,
Captrust 0.656 vs 0.689, Genesys 0.682 vs 0.654).

Runs after each daily EOD ingest (`scan.refresh_eod` → `maybe_refresh`, the last ~10 days; a full
history pass the first time) and in `eqr demo`. **Before 2025 the store holds only fiscal-year-end
sessions**, so demergers/rights from those years can't be sized from the bhavcopy and stay recorded
but unapplied; splits and bonuses (exact ratios) apply regardless.

**Holiday files.** On a market holiday NSE serves the *previous* session's bhavcopy under the
holiday's URL; stored as-is it became a duplicate candle dated on the holiday (14 such dates since
2025). `ingest_bhavcopy` now checks the file's own `DATE1` and treats a mismatch as "no session";
`repair_phantom_sessions` removed the ones already stored.

`load_prices` reads the **`EQ` + trade-for-trade (`BE`/`BZ`) series** (one row per date,
`EQ` preferred) — small / surveillance names trade in `BE`, so an `EQ`-only read left them
with almost no history and no technicals/levels. The levels engine still needs **≥60 trading
days** in the store before it maps support/resistance (below that it shows nothing rather than
draw lines on too little data); genuinely thin/suspended names (e.g. a distressed stock with
only a few stale rows) therefore carry no levels until enough history accumulates.

## Data dependency — continuous daily history

Indicators like the 200-DMA need a *continuous* daily series, but normal use
ingests only sparse dates. Backfill a range first (idempotent — skips weekends,
holidays, and dates already present):

```
uv run python scripts/backfill_eod.py 2025-01-01 2026-06-12   # ~350 trading days
```

`ingest_eod_range(start, end, con)` is the library entry point.

## Indicators (`indicators(con, symbol)` → DataFrame)

- **Trend**: SMA 20 / 50 / 200; golden/death-cross regime (50 vs 200).
- **Momentum**: RSI(14) (Wilder), MACD (12/26/9) line / signal / histogram.
- **Volatility**: Bollinger Bands (20, ±2σ), ATR(14).
- **Volume / conviction**: 20-day avg volume; **delivery %** + its 20-day average
  (an NSE-exclusive conviction signal — delivery spikes flag institutional intent).
- **Position**: 52-week high / low and % from high.
- **Relative strength** vs an index (`relative_strength`, default Nifty 50,
  63-day window): stock return ÷ index return; >1 = outperforming. Needs the
  index series in `index_close` (backfill alongside the EOD range).

## Snapshot (`snapshot(con, symbol)` → dict)

Latest values + plain-language **signals** (trend vs 200-DMA, cross regime, RSI
zone, MACD direction, delivery-% spike). Report:

```
uv run python scripts/technical_report.py RELIANCE
```

Validated on RELIANCE (2026-06-12, 373 trading days): close 1,293 below SMA20/50/
200 → downtrend + death-cross regime, RSI 41 (neutral), MACD bearish, −18.8% from
52-week high — all internally consistent.

## Notes

- Rolling windows operate on row order; the few sparse pre-backfill dates sit at
  the series start and don't affect the latest snapshot. Backfill a clean
  contiguous range for trustworthy early-period indicators.
- Relative strength needs `index_close` populated for the same dates (the EOD
  backfill covers `equity_eod` only; index closes are backfilled separately).

## Your holdings (`holdings.py`, `web/static/holdings.js`)

- **Store:** `holding_lots` (one row per buy: symbol, qty, price, optional `buy_date`, `source` = `ui` | `csv`).
  Adding a lot also marks the stock a *holding* in the watchlist. Local DuckDB only; nothing is sent anywhere.
- **In:** the web UI (`GET/POST /api/holdings`, `PUT/DELETE /api/holdings/{id}`; watchlist holdings with no
  lot yet come back as `missing` rows to fill in; `GET /api/stocks?q=` is the company-name search — every word
  must appear in the name, or the query is the symbol) or `holdings.csv` (headers
  matched loosely — `symbol|instrument|stock`, `qty|quantity`, `price|avg cost|buy price`, `date|buy date`; dates
  day-first). The file is re-imported when its mtime changes; its rows replace the last import, UI lots stay.
  Lots from the file are read-only in the UI. Unmatched stocks / bad numbers are listed by file line.
- **Valuation:** the latest close from `equity_eod_adj`. A dated lot is brought through every split / bonus /
  consolidation / rights since its date (`corporate_actions.share_multiplier_since`: qty × m, price ÷ m) — so
  enter a dated lot as you bought it. Term: long when held more than 365 days (else days left). Yearly return
  = (close ÷ adjusted price)^(365 ÷ days) − 1, shown once held a year. Benchmark = Nifty 500 from the first close
  on/after the buy date. Undated lots: profit / loss only.
- **Never committed:** `holdings*.csv` (except the example) is gitignored; `.githooks/pre-commit`
  (`git config core.hooksPath .githooks`) refuses a staged holdings file, `.duckdb` or `.env`; a test fails if
  git ever tracks one.
