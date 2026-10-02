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

## Your holdings (`portfolio/`, `web/static/holdings.js`)

Local only — nothing is sent anywhere, and nothing personal is committed (see the last bullet).

| Module | Does |
|---|---|
| `portfolio/store.py` | buys (`holding_lots`) and sells (`holding_sells`) — add / edit / delete; the company-name search; the watchlist holdings still waiting for numbers |
| `portfolio/csvio.py` | `holdings.csv` import (buys and, with a `side` column, sells) |
| `portfolio/instruments.py` | ETFs, SME shares, REITs, InvITs and BSE-only shares (`instruments`); BSE prices (`bse_prices`) |
| `portfolio/timeline.py` | one stock's history replayed in date order → your tax lots, sells matched, dividends credited |
| `portfolio/valuation.py` | the whole portfolio: every timeline, mergers carried across, realised gains by year, XIRR |
| `portfolio/income.py` | dividend history (`dividends`), XIRR |
| `portfolio/tax.py` | Indian capital-gains rules: term, rates, set-off, 31-Jan-2018 grandfathering (`fmv_2018`), buybacks, the 2025 Act |

- **In:** the web UI (`GET/POST /api/holdings`, `PUT/DELETE /api/holdings/{id}`, `POST/DELETE /api/sells`,
  `GET /api/stocks?q=` — every word must appear in the name, or the query is the symbol) or `holdings.csv`
  (headers matched loosely — `symbol|instrument|stock`, `qty|quantity`, `price|avg cost|buy price`, `date`,
  optional `side` = buy / sell, `type` = buyback, `received`; dates day-first). The file is re-imported when its
  mtime changes; its rows replace the last import, UI entries stay; file rows are read-only in the UI.
- **How a buy is entered:** with a date, qty and price as bought; without one, the broker's numbers today
  (undated buys aren't replayed, aren't touched by sells, and have P&L only).
- **The timeline (per stock, in date order; same day: dividend, then the ex-date action, then buys, then sells):**
  each dated buy becomes a *part* (shares, cost, acquisition date).
  - *split / consolidation* (and combined split+bonus records, treated as splits): every part's shares × m; cost and
    date unchanged.
  - *bonus*: each part spawns a part of `shares × (m − 1)` at **₹0 cost, acquired on the ex-date** (s.55(2)(aa) —
    brokers average it in; the totals match theirs, the per-share split doesn't).
  - *rights*: not applied — each lot gets an offer (entitled whole shares, issue price = face value + the premium
    in NSE's subject when the face value is on file) that adds a new buy if you subscribed.
  - *demerger*: every part keeps the parent's share of cost from the company's **apportionment-of-cost notice**
    (`analysis/demerger_costs.py` — found by title or filename 60 days before to 150 after the ex-date, read by
    `synthesize.demerger_cost_split`, kept only if it adds to ~100; else the market split `price_adjustments.factor`,
    labelled an estimate; a miss is retried after 7 days). When the notice names a listed new company and the ratio,
    every part **spawns** that company's shares (shares × ratio, the moved share of cost, the same acquisition date,
    no cash flow), carried into the new company's own timeline — `valuation.py` settles parents before children in
    passes, so a sell or buy of the parent before the ex-date changes the new company's shares too, whatever order
    they were entered in. One read-only `auto` entry per parent buy; an entry typed for the same shares (received on
    that ex-date) is flagged as a duplicate and not counted.
  - *dividend*: shares held before the ex-date × the amount (NSE's per-symbol record; `Rs`/`Re` amounts summed,
    REIT / InvIT distributions included, old "% of face value" subjects skipped), refreshed weekly per held stock in
    the background when NSE access is on.
  - *sell*: FIFO across parts by acquisition date (bonus parts after bought parts of the same day). Each slice:
    proceeds, cost, term at the sell date; long-term slices of parts acquired by 31-Jan-2018 use
    `max(cost, min(FMV, sale price))` with FMV = NSE's 31-Jan-2018 high (loaded once in the background; matched by
    ISIN, else symbol) divided by the share multiplications since. A buyback is exempt before 1-Oct-2024, a deemed
    dividend (proceeds) plus capital loss (cost) to 31-Mar-2026, ordinary gains after. Selling more than the dated
    buys cover is listed as unmatched with a warning.
- **BSE's record (`analysis/bse_actions.py`):** NSE's record only covers a company's NSE life, so for each held
  stock BSE's corporate-action history is read too (scrip code by ISIN from `bse_codes`; weekly, in the background):
  splits / consolidations / bonuses sized from BSE's wording go into `price_adjustments` (`source = 'bse'`) only when
  NSE has nothing within a week of that date; dividends into `dividends`; rights (BSE rarely states the ratio) become
  an offer without a number; reductions of capital / schemes become a note on the buys before them ("check your
  share count") — never guessed. This covers shares that listed on NSE late and BSE-only shares.
- **Mergers (`analysis/former_companies.py`, table `former_companies`):** symbols that stopped trading are learned
  weekly (name + `Merger` record date from NSE's per-symbol record, batched 80 per browser session), so the add box
  finds the old company. Held ones have their record-date / scheme filings read (`synthesize.merger_terms`) for the
  survivor and swap ratio. The old company's timeline runs up to the record date; its parts × the ratio are carried
  into the survivor's timeline on that date, so the survivor's earlier bonuses or demergers don't touch them.
- **Instruments:** ETFs (NSE's ETF list), SME shares (NSE Emerge's list), REITs / InvITs (series `RR` / `IV` in the
  bhavcopy, named from NSE's per-symbol record) — priced from the same NSE bhavcopy. BSE-only shares: BSE's active
  and suspended scrips whose ISIN isn't on NSE, as `BSE:<scrip code>` (`bse` / `bse_suspended`), priced daily from
  BSE's bhavcopy — a held one missing from it (suspended) gets BSE's last traded price; their corporate actions come
  from BSE's record (above). Lists refresh weekly with the merged companies; BSE prices daily (and at once for a new one).
- **Output per stock:** remaining shares and cost (the broker-style average), value on the latest close, P&L,
  dividends received, gains booked, XIRR (dated buys, sells and dividends as cash flows plus today's value of the dated
  shares — bisection, `analysis/funds._xirr`; none under a month). Per buy: its parts (for the tax planner), term,
  yearly return once held a year, the Nifty 500 since (only when the index history reaches the buy date — within 10
  days), the history applied, bonus parts, rights offers, demerger notes, and the "today's numbers typed with an old
  date" flag (a price under 60 % of the raw close nearest the date, before the first split since).
- **Realised gains by financial year:** every sold slice, totals after set-off, the yearly exemption, the estimated
  tax (`tax.tax_estimate`), dividends received, deemed buyback dividends. The `raise ₹X` planner adds this year's
  booked gains before working out a plan's tax (so the exemption already used counts).
- **Never committed:** `holdings*.csv` (except the example) is gitignored; `.githooks/pre-commit`
  (`git config core.hooksPath .githooks`) refuses a staged holdings file, `.duckdb` or `.env`; a test fails if
  git ever tracks one.
