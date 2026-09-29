# 📊 Track record

Every call the tool makes is logged the moment it goes out and scored against the market later —
including the misses. Ask for it any time with **`scorecard`** (or `track record`); it's also emailed
weekly once there's something to show. Code: `analysis/track_record.py`, `reports/scorecard_brief.py`.

← back to the [README](../README.md) · every command: [COMMANDS.md](COMMANDS.md)

## What counts as a call

| Source | Logged as |
|---|---|
| A deep report's closing verdict | **Buy / Accumulate** → long · **Reduce / Avoid** → avoid · **Hold** → hold · unreadable → `REVIEW` (logged, never scored — not silently a Hold) |
| An idea engine's list — Tailwind, Pickaxe, Hotlist, Concalls, Results Radar, every `screen:` | each name, in its ranked order, as a long pick; the whole list is also scored as an equal-weight basket |

Not calls: the "which HDFC did you mean?" menus, your own holdings (`sell`, `booking`), IPO and fund
pickers. One call per engine, company and day — a report re-sent the same day adds nothing.

The log is **append-only**: a call is never edited, and never regenerated for a past date (an LLM asked
today what it "would have said" in August knows how August went).

## How a call is scored

- **Entry** — the opening price of the first session after the call went out (a call sent before
  the 09:15 IST open uses that day's open): the first price anyone acting on it could have got.
- **Exit** — the close of the 5th / 21st / **63rd** / 126th / 252nd session after entry (1 week /
  1 month / **3 months — the primary horizon, declared in advance** / 6 months / 12 months). A horizon
  is scored only once it has fully traded; a suspended stock uses its last close.
- **Prices** — split / bonus / rights / demerger-adjusted (`equity_eod_adj`), so a 1:5 split isn't an
  80% loss.
- **Benchmark** — the **Nifty 500** over the same sessions (its open on the entry day, its close on the
  exit day). Both sides are price returns, so dividends are left out of both; costs, taxes and
  slippage are not modelled.
- **Hit** — a long call beat the Nifty 500; an Avoid / Reduce call lagged it; a Hold stayed within a
  band fixed in advance (±2 / 3 / 5 / 7 / 10 % at 1w / 1m / 3m / 6m / 12m). For Avoids the excess is
  sign-flipped, so + always means the call was right.
- **Every rate shows its sample size and a 95 % Wilson interval.** Below `TRACK_MIN_SAMPLE` (30)
  matured calls a rate is marked *too few* — 30 calls at 50/50 odds can land anywhere from ~33 % to
  ~67 % by luck.

The scorecard shows the latest verdicts, and the **best and worst** calls side by side.

## What the LLM sees: nothing from here

The "📜 last time we said Avoid at ₹210; since then −7 % vs the Nifty +2 %" line at the top of a deep
report is **rendered for you, in Python, after the analysis is written**. The model never sees past
outcomes, its hit rate or "lessons", because the research on doing so is discouraging:

- the only positive results for reflection memory in trading agents are authors' own, on a handful of
  US stocks over months inside the models' training data; an independent 20-year re-test found the
  edge disappears (FINSABER, KDD 2026);
- showing a model outcomes causes hindsight bias and pushes it toward vague "Hold" verdicts;
- short-horizon returns are mostly noise — separating a 55 % hit rate from a coin flip takes a few
  hundred independent calls;
- models already remember many past prices, so anything "replayed" flatters them.

Improving the prompts stays a human job: read the scorecard, change a prompt, and let the next months
of live calls judge it.

## Backfill

`uv run python scripts/backfill_track_record.py` (bot stopped) recovers calls that **had already gone
out** before logging began, and marks them *recovered*: the idea-engine lists the bot stored as reply
menus, deep reports still in the bot's mailbox (Sent / Bin — Gmail keeps the Bin ~30 days) and reports
saved by the web UI / `eqr`. A report whose text is gone has no recoverable verdict and is left out,
not guessed.

## Switches (`.env`)

| Variable | Default | Effect |
|---|---|---|
| `TRACK_RECORD_ENABLED` | `true` | log calls and answer `scorecard`; off → nothing is logged |
| `REPORT_MEMORY_ENABLED` | `true` | the "last time we said…" line atop a deep report |
| `ENABLE_SCORECARD_PUSH` | `true` | the weekly scorecard email |
| `TRACK_MIN_SAMPLE` | `30` | matured calls needed before a rate is shown as meaningful |

## Private by design

The calls live in your local DuckDB file (`data/`, which is gitignored) — nothing is uploaded or
published. That's deliberate: in India, a SEBI-registered research analyst may only advertise past
performance once it's verified through SEBI's PaRRVA framework (live since May 2026). This project is
personal research tooling, not a registered adviser, so the scorecard is for its user's own review —
not a performance claim — and every scorecard says so.
