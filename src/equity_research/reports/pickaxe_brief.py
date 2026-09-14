"""Format the ⛏️ Pickaxe pipeline output into an email-ready markdown section.

Renders each demand theme with its **source link**, the co-trending terms that corroborate it,
and a numbered table of verified Indian listed beneficiaries — the **indirect "pickaxe" plays
first** (⛏️), then the direct plays (🎯), each tagged with cyclicality and a smart-money read.
Returns ``{markdown, picks}`` where ``picks`` drives the numbered-reply → deep-report flow.
``None`` when nothing surfaced. Mirrors ``reports/tailwind_brief.py``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import pickaxe
from equity_research.reports import md

_IST = ZoneInfo("Asia/Kolkata")

_DUR_EMOJI = {"structural": "🌳", "emerging": "🌱", "faddish": "⚡"}

_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **The idea:** when demand for a product is booming, the maker of the product is often the "
    "crowded, cyclical bet — so this hunts the **'sell the pickaxes'** play instead: the listed "
    "company that supplies the boom (the ingredient, the feed/vaccine, the equipment, the "
    "packaging, the logistics) and rides the same wave with steadier earnings, usually still off "
    "the mainstream radar. It scans India's rising **'buy'** searches + demand-surge news and maps "
    "each theme to the Indian names that benefit.\n"
    "- **Tier badges** — ⛏️ **pickaxe** (indirect beneficiary — surfaced first) · 🎯 direct (makes "
    "the product itself) · 🟢/🟡/🔁 cyclicality (low / mid / high — how volatile the earnings) · "
    "📈 accumulating / 📉 distributing (institutions' latest SHP move) · size (small/mid/large-cap) "
    "· ⭐ on your watchlist. Every name is checked against the NSE master; implausible ones dropped.\n"
    "- **Rev** — estimated **% of that company's revenue** from this theme (its materiality — a "
    "pure-play benefits far more than a conglomerate at ~2%). An **LLM/grounded estimate — verify "
    "against the annual report**; 'n/a' means not confidently known (an honest blank, not zero).\n"
    "- **Durability** — 🌳 structural · 🌱 emerging · ⚡ faddish (pure fads are dropped upstream).\n"
    "- **♻️ Fresh vs cached** — a scan is **cached for 24 hours**: email `pickaxe` again within a "
    "day for the *same* result instantly (no re-fetch, no token cost). Email **`pickaxe --latest`** "
    "to force a fresh live scan. The weekly Saturday push is always fresh.\n"
    "- **⚠️ Google Trends is best-effort** (no official API); when it's blocked the demand read is "
    "LLM/Search-driven (qualitative, no hard % change) — still useful, just less quantified.\n"
    "- _An idea **generator**, not a call — each theme carries its source; treat every name (and "
    "every estimated %) as a lead worth 10 minutes of your own check. No clean listed beneficiary → "
    "it says so._"
)


def _theme_block(t: dict, start_no: int) -> tuple[str, list]:
    """One theme → (markdown, picks). ``start_no`` is the running number for the pick menu."""
    dur = _DUR_EMOJI.get(t.get("durability", ""), "")
    title = f"### {dur} {t.get('theme', '').title()}"
    if t.get("category"):
        title += f" — {t['category']}"
    bits = []
    if t.get("headline"):
        bits.append(t["headline"])
    meta = []
    if t.get("driver"):
        meta.append(f"**why:** {t['driver']}")
    if t.get("cotrends"):
        meta.append("**co-trending:** " + ", ".join(t["cotrends"]))
    if t.get("india_supply"):
        meta.append(f"**supply:** {t['india_supply']}")
    if t.get("durability"):
        meta.append(f"**{t['durability']}**")
    if meta:
        bits.append(" · ".join(meta))
    if t.get("source_url"):
        bits.append(f"_source: [{t.get('source_name') or 'source'}]({t['source_url']})_")

    bens = t.get("beneficiaries") or []
    picks: list = []
    if bens:
        rows = []
        for b in bens:
            company = f"{b['name']} ({b['symbol']})"
            rows.append([start_no + len(picks), company, b.get("role", ""),
                         b.get("revenue_share") or "n/a", b.get("tier", "")])
            picks.append(b)
        body = md.table(["#", "Company (NSE)", "What it makes / why", "Rev", "Tier"],
                        rows, "rllll")
    else:
        body = "_No clean **listed** Indian beneficiary surfaced for this one — noted, not forced._"
    return title + "\n\n" + "\n\n".join(bits) + "\n\n" + body, picks


def build_pickaxe_report(con: duckdb.DuckDBPyConnection, *, days: int = 21,
                         max_themes: int = 6, use_cache: bool = False) -> dict | None:
    """Run the pipeline and format it. Returns ``{markdown, picks, keys, n_themes}`` (plus
    ``from_cache``/``cached_at`` when served from cache) or ``None`` when nothing surfaced. The scan
    is expensive (live fetch + grounded LLM calls) and demand barely moves hour to hour, so a fresh
    run is **cached for 24h**: ``use_cache=True`` reuses a run within that window; ``use_cache=False``
    (weekly push, or `--latest`) always runs fresh and refreshes the cache."""
    from equity_research import scan

    if use_cache:
        hit = scan.pickaxe_cache_get(con)
        if hit and hit.get("report"):
            rep = dict(hit["report"])
            rep["from_cache"] = True
            rep["cached_at"] = hit.get("cached_at")
            return rep

    res = pickaxe.run_pickaxe(con, days=days, max_themes=max_themes)
    themes = res.get("themes") or []
    if not themes:
        return None

    today = datetime.now(_IST).date()
    parts = [f"# ⛏️ Pickaxe — {today:%a %d-%b-%Y}",
             "_Surging Indian demand → the indirect **'sell the pickaxes'** listed beneficiary. "
             "Reply with a stock's symbol or name for its full deep report. Not a call — see the "
             "legend._"]
    picks: list = []
    for t in themes:
        block, block_picks = _theme_block(t, len(picks) + 1)
        parts.append(block)
        picks.extend(block_picks)

    n_watch = sum(1 for p in picks if p.get("on_watchlist"))
    n_pick = sum(1 for p in picks if p.get("layer") == "indirect")
    header_note = (f"**{len(themes)} theme(s)** · **{len(picks)} verified name(s)**"
                   + (f" · **{n_pick} ⛏️ pickaxe play(s)**" if n_pick else "")
                   + (f" · **{n_watch} on your watchlist** ⭐" if n_watch else ""))
    parts.insert(2, header_note)

    report = {"markdown": "\n\n".join(parts) + _LEGEND, "picks": picks,
              "keys": res.get("keys", []), "n_themes": len(themes)}
    scan.pickaxe_cache_put(report, con)                        # refresh the 24h cache on every fresh run
    return report
