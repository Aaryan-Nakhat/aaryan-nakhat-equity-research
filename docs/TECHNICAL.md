# Technical analysis (Phase 3)

Indicators computed from the daily EOD series in `equity_eod` (NSE bhavcopy,
incl. delivery %). All in `analysis/technical.py`; pure functions over the price
history.

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
