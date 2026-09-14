"""Format the ⛏️ Pickaxe pipeline output into an email-ready markdown report + a charted PDF.

Each demand theme renders as a **Google-Trends interest line** (chart in the PDF + the % rise inline)
followed by its verified Indian listed beneficiaries — the indirect "pickaxe" plays first (⛏️), then
the direct plays (🎯). Every name carries **exact quant** (price, P/E vs its sector, support/
resistance) from our own engines and a **filing-grounded forward projection** (revenue share now →
next FY, growth, with sources) — rendered as a readable per-stock block, not a cramped table.

``build_pickaxe_report`` returns ``{markdown, picks, keys, n_themes, images}`` (``images`` = the
Trends charts for the PDF) or ``None`` when nothing surfaced. Heavy (deep per-name enrichment +
Trends charts), so it's driven from the background worker / weekly push, never the live 7-min path.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import pickaxe
from equity_research.reports import charts
from equity_research.scrapers import trends

log = logging.getLogger("equity-research.pickaxe")

_IST = ZoneInfo("Asia/Kolkata")
_DUR_EMOJI = {"structural": "🌳", "emerging": "🌱", "faddish": "⚡"}

_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **The idea:** when demand for a product booms, the maker is often the crowded, cyclical bet — "
    "so this hunts the **\"sell the pickaxes\"** play: the listed company that *supplies* the boom "
    "(the ingredient, feed/vaccine, equipment, packaging, logistics) and rides the same wave with "
    "steadier earnings, usually still off the mainstream radar. It scans India's rising **\"buy\"** "
    "searches + demand-surge news and maps each theme to the Indian names that benefit.\n"
    "- **📈 Google Trends** — each theme shows a real search-interest line (chart in the attached "
    "PDF) with the recent-quarter rise vs the prior period. Best-effort (no official API); if Google "
    "throttles it, the theme is news/LLM-sourced instead.\n"
    "- **Badges** — ⛏️ **pickaxe** (indirect, surfaced first) · 🎯 direct (makes the product) · "
    "🟢/🟡/🔁 cyclicality (low/mid/high) · 📈 accumulating / 📉 distributing (institutions' latest "
    "SHP move) · size · ⭐ on your watchlist. Every name is verified against the NSE master.\n"
    "- **Numbers** — **Price / P/E / P/B / support-resistance** are exact, from our own data "
    "(latest close + filed financials + daily OHLC). **Revenue-from-theme (now → next FY), growth "
    "and the projection** are read from the company's **own filings / concalls / investor decks**, "
    "with the **sources linked** — verify against them; a blank means not confidently found.\n"
    "- **♻️ Cached 24h** — email `pickaxe` again within a day for the same result instantly; "
    "`pickaxe --latest` forces a fresh live scan. The weekly Saturday push is always fresh.\n"
    "- _An idea **generator**, not a call — each theme carries its source; treat every name and "
    "estimate as a lead worth your own 10-minute check._"
)


def _spark(series: list[float]) -> str:
    """A tiny unicode sparkline of a 0-100 series (compressed to ~24 points)."""
    if not series:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    step = max(1, len(series) // 24)
    pts = series[::step][-24:]
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    return "".join(blocks[min(7, int((v - lo) / rng * 7))] for v in pts)


def _rs(v) -> str:
    if v is None:
        return "n/a"
    return f"₹{v:,.0f}" if v >= 100 else f"₹{v:,.1f}"


def _cr(v) -> str:
    if v is None:
        return "n/a"
    return f"₹{v/1e5:,.2f} L cr" if v >= 1e5 else f"₹{v:,.0f} cr"


def _pe(v) -> str:
    return f"{v:.1f}" if v is not None else "n/a"


def _quant_line(q: dict) -> str | None:
    """💰 valuation + 📈 technicals lines from the enrichment quant dict."""
    if not q:
        return None
    val = [f"💰 **Price** {_rs(q.get('price'))}"]
    if q.get("pe") is not None:
        pe_bit = f"**P/E** {_pe(q.get('pe'))}"
        if q.get("sector_pe") is not None:
            vs = q.get("pe_vs_sector")
            pe_bit += f" (sector ~{_pe(q.get('sector_pe'))}{', ' + vs if vs else ''})"
        val.append(pe_bit)
    if q.get("pb") is not None:
        val.append(f"**P/B** {_pe(q.get('pb'))}")
    if q.get("mcap_cr") is not None:
        val.append(f"**mcap** {_cr(q.get('mcap_cr'))}")
    lines = ["- " + " · ".join(val)]
    if q.get("support") is not None or q.get("resistance") is not None:
        tech = []
        if q.get("support") is not None:
            tech.append(f"support {_rs(q.get('support'))}")
        if q.get("resistance") is not None:
            tech.append(f"resistance {_rs(q.get('resistance'))}")
        if q.get("trend"):
            tech.append(f"trend {q['trend']}")
        lines.append("- 📈 **Technicals:** " + " · ".join(tech))
    return "\n".join(lines)


def _proj_lines(pick: dict) -> list[str]:
    """📊 revenue-from-theme (now → next) + growth, and the 📄 sources — from the filing-grounded read,
    falling back to the mapper's rough revenue_share estimate when the deep read found nothing."""
    p = pick.get("proj") or {}
    now = p.get("rev_share_now") or pick.get("revenue_share") or ""
    nxt, growth, horizon, note = (p.get("rev_share_next"), p.get("growth_pct"),
                                  p.get("horizon"), p.get("note"))
    out: list[str] = []
    rev_bits = []
    if now:
        rev_bits.append(f"**{now}** of revenue now")
    if nxt:
        has_year = bool(re.search(r"(FY|20)\d", nxt))          # value already names a year → don't double it
        rev_bits.append(f"→ **{nxt}**" + (f" by {horizon}" if horizon and not has_year else ""))
    if growth:
        rev_bits.append(f"· growth **{growth}**")
    if rev_bits:
        out.append("- 📊 **Revenue from theme:** " + " ".join(rev_bits))
    if note:
        out.append(f"- 🧭 {note}")
    srcs = [f"[{s.get('label') or 'source'}]({s['url']})" for s in (p.get("sources") or []) if s.get("url")]
    if srcs:
        out.append("- 📄 _Sources: " + " · ".join(srcs) + "_")
    return out


def _pick_block(pick: dict, no: int) -> str:
    """One beneficiary → a readable per-stock block (not a table row)."""
    head = f"**{no}. {pick['name']} ({pick['symbol']})** — {pick.get('tier', '')}"
    lines = [head]
    if pick.get("role"):
        lines.append(f"_{pick['role']}_")
    ql = _quant_line(pick.get("quant") or {})
    if ql:
        lines.append(ql)
    lines.extend(_proj_lines(pick))
    if pick.get("why"):
        lines.append(f"- 🔗 **Why it rides this theme:** {pick['why']}")
    return "\n".join(lines)


def _trend_term(theme: dict) -> str:
    """Pick a clean Google-Trends search term for a theme: the shortest co-trend, else the theme
    name stripped of parentheticals."""
    cts = [c for c in (theme.get("cotrends") or []) if 1 <= len(c.split()) <= 3]
    if cts:
        return min(cts, key=len)
    base = re.sub(r"\(.*?\)", "", theme.get("theme", "")).strip()
    return " ".join(base.split()[:3])


def _theme_block(t: dict, start_no: int, detail: dict | None) -> tuple[str, list]:
    """One theme (header + Trends metric + beneficiary blocks) → (markdown, picks)."""
    dur = _DUR_EMOJI.get(t.get("durability", ""), "")
    title = f"## {dur} {t.get('theme', '').title()}"
    if t.get("category"):
        title += f" — {t['category']}"
    parts = [title]
    if t.get("headline"):
        parts.append(f"**{t['headline']}**")
    meta = []
    if t.get("driver"):
        meta.append(t["driver"])
    if t.get("cotrends"):
        meta.append("**co-trending:** " + ", ".join(t["cotrends"]))
    if t.get("india_supply"):
        meta.append(f"**supply:** {t['india_supply']}")
    if meta:
        parts.append(" · ".join(meta))
    if detail and detail.get("series"):
        slope = detail.get("slope_pct")
        arrow = "📈" if (slope or 0) >= 0 else "📉"
        parts.append(f"{arrow} **Google Trends — “{detail['term']}” (India, 12mo):** "
                     f"`{_spark(detail['series'])}` recent quarter **"
                     f"{'+' if (slope or 0) >= 0 else ''}{slope}%** vs prior "
                     f"(latest {detail.get('latest')}/100, avg {detail.get('avg')}) "
                     f"— _chart in the attached PDF_")
    if t.get("source_url"):
        parts.append(f"_news source: [{t.get('source_name') or 'source'}]({t['source_url']})_")

    picks: list = []
    bens = t.get("beneficiaries") or []
    for b in bens:
        parts.append(_pick_block(b, start_no + len(picks)))
        picks.append(b)
    return "\n\n".join(parts), picks


def build_pickaxe_report(con: duckdb.DuckDBPyConnection, *, days: int = 21,
                         max_themes: int = 6, use_cache: bool = False) -> dict | None:
    """Run the pipeline, deep-enrich every name, fetch a Google-Trends chart per theme, and format
    it all. Returns ``{markdown, picks, keys, n_themes, images}`` (``images`` = (caption, png) for the
    PDF) or ``None`` when nothing surfaced. 24h-cached; ``use_cache=False`` always runs fresh."""
    from equity_research import scan

    if use_cache:
        hit = scan.pickaxe_cache_get(con)
        if hit and hit.get("report"):
            rep = dict(hit["report"])
            rep["from_cache"] = True
            rep["cached_at"] = hit.get("cached_at")
            rep.setdefault("images", [])                       # charts aren't cached; PDF is skipped on a hit
            return rep

    res = pickaxe.run_pickaxe(con, days=days, max_themes=max_themes)
    themes = [t for t in (res.get("themes") or []) if t.get("beneficiaries")]  # never surface an empty theme
    if not themes:
        return None

    pickaxe.enrich_report(con, themes)                         # heavy: exact quant + filing-grounded projections

    # one Google-Trends browser session for every theme's key term
    terms = {t["theme"]: _trend_term(t) for t in themes}
    details = trends.interest_details(list(terms.values()))
    images: list = []

    today = datetime.now(_IST).date()
    parts = [f"# ⛏️ Pickaxe — {today:%a %d-%b-%Y}",
             "_Surging Indian demand → the indirect **'sell the pickaxes'** listed beneficiary. "
             "Reply with a stock's symbol or name for its full deep report. Not a call — see the "
             "legend._"]
    picks: list = []
    for t in themes:
        detail = details.get(terms[t["theme"]])
        if detail:
            chart = charts.pickaxe_trend_chart(detail["term"], detail)
            if chart:
                images.append(chart)
        block, block_picks = _theme_block(t, len(picks) + 1, detail)
        parts.append(block)
        picks.extend(block_picks)

    n_watch = sum(1 for p in picks if p.get("on_watchlist"))
    n_pick = sum(1 for p in picks if p.get("layer") == "indirect")
    header = (f"**{len(themes)} theme(s)** · **{len(picks)} verified name(s)**"
              + (f" · **{n_pick} ⛏️ pickaxe play(s)**" if n_pick else "")
              + (f" · **{n_watch} on your watchlist** ⭐" if n_watch else ""))
    parts.insert(2, header)

    report = {"markdown": "\n\n".join(parts) + _LEGEND, "picks": picks,
              "keys": res.get("keys", []), "n_themes": len(themes), "images": images}
    cacheable = {k: v for k, v in report.items() if k != "images"}  # PNGs are bulky; don't cache them
    scan.pickaxe_cache_put(cacheable, con)
    return report
