"""Format the 💨 Tailwind pipeline output into an email-ready markdown section.

Renders each catalyst (a global supply/policy disruption) with its **source link**, the
downstream sectors it squeezes, and a numbered table of verified Indian listed beneficiaries
— curated 🟢 / AI-verified 🟡 / already-on-your-watchlist ⭐. Returns ``{markdown, picks}``
where ``picks`` drives the numbered-reply → deep-report flow. ``None`` when nothing surfaced.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import tailwind
from equity_research.reports import md

_IST = ZoneInfo("Asia/Kolkata")

_STATUS_EMOJI = {"in-effect": "🔴", "proposed": "🟠", "rumored": "🟡"}
_SEV_EMOJI = {"high": "🔥", "medium": "▲", "low": "•"}

_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **The idea:** when one country dominates a critical material and restricts it (export ban, "
    "quota, tariff, production cut), the sectors that *must* keep buying (defence, semiconductors, "
    "EV batteries…) hunt for other suppliers — and listed players elsewhere who make that thing get "
    "a **tailwind**. This scans the world for those moves and finds the Indian names that benefit.\n"
    "- **Status** — 🔴 in effect · 🟠 proposed / imminent · 🟡 rumored. **Severity** — 🔥 high · ▲ "
    "medium · • low.\n"
    "- **Beneficiary tiers** — 🟢 hand-curated · 🟡 **AI-suggested, verify** the link before acting · "
    "⭐ already on your watchlist. Every name is checked against the NSE master; implausible ones are "
    "dropped, but the AI link is still yours to confirm.\n"
    "- _An idea **generator**, not a call — each catalyst carries its source; treat every name as a "
    "lead worth 10 minutes of your own check. Where there's no clean listed beneficiary, it says so._"
)


def _catalyst_block(c: dict, start_no: int) -> tuple[str, list]:
    """One catalyst → (markdown, picks). ``start_no`` is the running number for the pick menu."""
    st = _STATUS_EMOJI.get(c.get("status", ""), "")
    sev = _SEV_EMOJI.get(c.get("severity", ""), "")
    title = (f"### {sev} {c.get('material', '').title()} — {c.get('imposer', '')} "
             f"{c.get('action', '').replace('-', ' ')} {st}")
    bits = []
    if c.get("headline"):
        bits.append(c["headline"])
    meta = []
    if c.get("sectors"):
        meta.append("**needs it:** " + ", ".join(c["sectors"]))
    if c.get("status"):
        meta.append(f"**status:** {c['status']}")
    if c.get("date"):
        meta.append(f"**{c['date']}**")
    if meta:
        bits.append(" · ".join(meta))
    src = f"[{c.get('source_name') or 'source'}]({c['source_url']})" if c.get("source_url") else ""
    if src:
        bits.append(f"_source: {src}_")

    bens = c.get("beneficiaries") or []
    picks: list = []
    if bens:
        rows = []
        for b in bens:
            rows.append([start_no + len(picks), b["symbol"], b["name"],
                         b.get("role", ""), b.get("tier", "")])
            picks.append(b)
        body = md.table(["#", "Symbol", "Company", "What it makes / why it benefits", "Tier"],
                        rows, "rllll")
    else:
        body = "_No clean **listed** Indian beneficiary surfaced for this one — noted, not forced._"
    return title + "\n\n" + "\n\n".join(bits) + "\n\n" + body, picks


def build_tailwind_report(con: duckdb.DuckDBPyConnection, *, days: int = 14,
                          max_catalysts: int = 8) -> dict | None:
    """Run the pipeline and format it. Returns ``{markdown, picks, n_catalysts}`` or ``None``
    when nothing surfaced (so the caller can stay silent / say so)."""
    res = tailwind.run_tailwind(con, days=days, max_catalysts=max_catalysts)
    cats = res.get("catalysts") or []
    if not cats:
        return None

    today = datetime.now(_IST).date()
    parts = [f"# 💨 Tailwind — {today:%a %d-%b-%Y}",
             "_Global supply / policy moves that hand a tailwind to Indian listed producers. "
             "Reply with a stock's symbol or name for its full deep report. Not a call — see the "
             "legend._"]
    picks: list = []
    for c in cats:
        block, block_picks = _catalyst_block(c, len(picks) + 1)
        parts.append(block)
        picks.extend(block_picks)

    n_watch = sum(1 for p in picks if p.get("on_watchlist"))
    header_note = (f"**{len(cats)} catalyst(s)** · **{len(picks)} verified name(s)**"
                   + (f" · **{n_watch} on your watchlist** ⭐" if n_watch else ""))
    parts.insert(2, header_note)

    return {"markdown": "\n\n".join(parts) + _LEGEND, "picks": picks,
            "keys": res.get("keys", []), "n_catalysts": len(cats)}


def build_tailwind_urgent(con: duckdb.DuckDBPyConnection, seen_keys: set[str], *,
                          days: int = 7) -> dict | None:
    """Compact mid-week break-in for the daily digest: only a FRESH, high-severity shock (not in
    ``seen_keys``) with a verified beneficiary. Returns ``{markdown, picks, keys, n_catalysts}`` or
    ``None`` (the usual quiet-day result). The markdown is a short block, not a full report."""
    res = tailwind.run_tailwind_urgent(con, seen_keys=seen_keys, days=days)
    cats = res.get("catalysts") or []
    if not cats:
        return None

    parts = ["## 💨 Fresh supply shock (mid-week)",
             "_A big global supply/policy move just landed — with Indian names that benefit. "
             "Reply with a stock's symbol or name for its deep report. The full weekly 💨 Tailwind "
             "still runs Saturday._"]
    picks: list = []
    for c in cats:
        block, block_picks = _catalyst_block(c, len(picks) + 1)
        parts.append(block)
        picks.extend(block_picks)

    return {"markdown": "\n\n".join(parts) + _LEGEND, "picks": picks,
            "keys": res.get("keys", []), "n_catalysts": len(cats)}
