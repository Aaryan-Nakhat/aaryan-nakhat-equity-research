"""📊 Scorecard — the track record of every call the tool has made (analysis/track_record.py), as a
report: per engine hit rates and excess returns vs the Nifty 500 with sample sizes and confidence
intervals, the latest calls, and the best and worst — nothing left out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

from equity_research.analysis import track_record as tr

_IST = timezone(timedelta(hours=5, minutes=30))

DISCLAIMER = (
    "_**Not investment advice, and not a performance claim.** This is the tool scoring its own past "
    "output for its user — every call it logged, including the bad ones. Returns are price returns "
    "(dividends excluded on both sides) from the next session's open, on split/bonus-adjusted prices, "
    "before costs, taxes and slippage; a small sample says little, and past results don't predict "
    "future ones. Calls marked *recovered* were rebuilt from reports that had already been sent, before "
    "logging began._")


def _pct(v: float | None, signed: bool = True) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.0f}%"


def _row(r: dict | None, n_min: int) -> list[str]:
    """[hit rate (CI), avg excess, median, basket avg, n] — or a 'too few' marker."""
    if not r:
        return ["—", "—", "—", "—", "0"]
    n = f"{r['n']}" + (f" ({r['recovered']} rec.)" if r["recovered"] else "")
    if not r["enough"]:
        return [f"_too few (n<{n_min})_ · {r['hit_rate']:.0f}%", _pct(r["mean_excess"]),
                _pct(r["median_excess"]), _pct(r["basket_mean"]), n]
    lo, hi = r["ci"]
    return [f"**{r['hit_rate']:.0f}%** ({lo:.0f}–{hi:.0f})", f"**{_pct(r['mean_excess'])}**",
            _pct(r["median_excess"]), _pct(r["basket_mean"]), n]


def build_scorecard(con: duckdb.DuckDBPyConnection) -> str:
    """The scorecard as markdown (always returns something — explains itself when there's no data)."""
    if not tr.enabled():
        return ("# 📊 Scorecard\n\nThe track record is switched off (`TRACK_RECORD_ENABLED=false` in "
                "`.env`). Turn it on and every deep-report verdict and idea-engine pick is logged as it "
                "goes out, then scored against the Nifty 500.")
    sc = tr.scorecard(con)
    logged = sc["logged"]
    total = sum(x["n"] for x in logged)
    today = datetime.now(_IST)
    parts = [f"# 📊 Scorecard — {today:%a %d-%b-%Y}",
             f"_Every call this tool has made, scored against the **{tr.BENCHMARK}**. A call is a "
             "deep-report verdict or a name an idea engine listed; it's logged the moment it goes out "
             "and never edited. **Hit** = a Buy/Accumulate or engine pick that beat the market, or an "
             "Avoid/Reduce that lagged it. **Excess** = the stock's return minus the market's over the "
             "same sessions (for an Avoid, the sign is flipped so + means the call was right)._"]
    if not total:
        parts.append("\nNo calls logged yet — ask for a company or run a screen, and they'll start "
                     "appearing here. Each horizon is scored once it has fully traded.")
        parts.append("\n" + DISCLAIMER)
        return "\n\n".join(parts)

    live = sum(x["n"] for x in logged if x["provenance"] == "live")
    parts.append(f"**{total:,} calls logged** · {live:,} live · {total - live:,} recovered · "
                 f"primary horizon **{tr.PRIMARY}** (declared in advance) · a rate needs **≥{tr.MIN_SAMPLE}** "
                 "matured calls before it's shown as meaningful.")

    by = {(r["source"], r["stance"], r["horizon"]): r for r in sc["rows"]}
    sources = sorted({(r["source"], r["stance"]) for r in sc["rows"]} |
                     {(x["source"], x["stance"]) for x in logged if x["stance"] in ("long", "avoid", "hold")},
                     key=lambda k: (k[0] != "deep_report", tr.SOURCE_NAMES.get(k[0], k[0]), k[1]))
    stance_word = {"long": "Buy / Accumulate", "avoid": "Avoid / Reduce", "hold": "Hold"}

    for h in (tr.PRIMARY, "1m"):
        head = "Primary — 3 months" if h == tr.PRIMARY else "1 month"
        lines = ["| Engine | Hit rate (95% CI) | Avg excess | Median | Avg basket | Scored calls |",
                 "|---|---|---|---|---|---|"]
        waiting = []
        for src, stance in sources:
            name = tr.SOURCE_NAMES.get(src, src)
            if src == "deep_report":
                name = f"{name} — {stance_word.get(stance, stance)}"
            r = by.get((src, stance, h))
            if r:
                lines.append("| " + " | ".join([name, *_row(r, tr.MIN_SAMPLE)]) + " |")
            else:
                waiting.append(name)
        body = "\n".join(lines) if len(lines) > 2 else "_No call has been out this long yet._"
        if waiting:
            body += f"\n\n_Not {h} old yet: {', '.join(waiting)}._"
        parts.append(f"## {head}\n\n" + body)

    other = [h for h in tr.HORIZONS if h not in (tr.PRIMARY, "1m")]
    extra = []
    for src, stance in sources:
        if not any(by.get((src, stance, h)) for h in other):
            continue
        cells = []
        for h in other:
            r = by.get((src, stance, h))
            cells.append(f"{_pct(r['mean_excess'])} · {r['hit_rate']:.0f}% (n={r['n']})" if r else "—")
        extra.append("| " + " | ".join([tr.SOURCE_NAMES.get(src, src)
                                         + (f" — {stance_word.get(stance, stance)}" if src == "deep_report" else ""),
                                         *cells]) + " |")
    parts.append("## Other horizons (avg excess · hit rate)\n\n| Engine | " + " | ".join(other) + " |\n|---|"
                 + "---|" * len(other) + "\n" + "\n".join(extra))

    oc = sc["outcomes"]
    todate = oc[oc["horizon"] == "to_date"].copy()
    recent = con.execute(
        "SELECT made_at, source, symbol, label, provenance, call_id FROM calls "
        "WHERE source = 'deep_report' ORDER BY made_at DESC LIMIT 15").fetchall()
    if recent:
        so = {r["call_id"]: r for _, r in todate.iterrows()}
        lines = ["| Date | Company | Verdict | Entry | Now | Stock | Market | Excess |",
                 "|---|---|---|---|---|---|---|---|"]
        for made, _src, sym, label, prov, cid in recent:
            d = pd.Timestamp(made).tz_localize("UTC").astimezone(_IST)
            r = so.get(cid)
            tag = "" if prov == "live" else " _(rec.)_"
            if r is None:
                lines.append(f"| {d:%d-%b-%Y}{tag} | {sym} | {label.title()} | — | — | — | — | not traded yet |")
                continue
            signed = -r["excess"] if r["stance"] == "avoid" else r["excess"]
            lines.append(f"| {d:%d-%b-%Y}{tag} | {sym} | {label.title()} | ₹{r['entry']:,.2f} | "
                         f"₹{r['exit']:,.2f} | {_pct(r['ret'])} | {_pct(r['bench'])} | "
                         f"{'✅' if signed > 0 else '❌'} {_pct(signed)} |")
        parts.append("## Latest deep-report verdicts (return to date — not yet a scored horizon)\n\n"
                     + "\n".join(lines))

    m1 = oc[oc["horizon"] == "1m"].copy()
    if len(m1) >= 2:
        m1["signed"] = m1.apply(lambda r: -r["excess"] if r["stance"] == "avoid" else r["excess"], axis=1)
        m1 = m1.sort_values("signed")

        def _fmt(df):
            return "\n".join(
                f"| {tr.SOURCE_NAMES.get(r['source'], r['source'])} | {r['symbol']} | {r['label'].title()} | "
                f"{pd.Timestamp(r['made_at']).tz_localize('UTC').astimezone(_IST):%d-%b-%Y} | {_pct(r['signed'])} |"
                for _, r in df.iterrows())
        head = "| Engine | Company | Call | Date | Excess (1m) |\n|---|---|---|---|---|\n"
        parts.append("## Best and worst at 1 month\n\n**Best**\n\n" + head + _fmt(m1.tail(5).iloc[::-1])
                     + "\n\n**Worst**\n\n" + head + _fmt(m1.head(5)))

    parts.append("---\n" + DISCLAIMER)
    return "\n\n".join(parts)
