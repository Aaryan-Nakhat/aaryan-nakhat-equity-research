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
