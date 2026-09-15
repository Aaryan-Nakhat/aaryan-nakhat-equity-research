"""Format the 📈 Results Radar output into an email-ready markdown section.

Renders the just-reported companies — each with its YoY revenue & profit growth, whether growth is
**accelerating**, and the **Execution** band — ranked by a 0-100 Results Score, as a numbered table
that drives the reply-a-number → deep-report flow. Returns ``{markdown, picks}``; ``None`` when nothing
has reported in the window.
"""

from __future__ import annotations

import duckdb

from equity_research.analysis import results_radar
from equity_research.reports import md

_ACCEL_LABEL = {"Accelerating": "⏫ Accelerating", "Steady": "▶ Steady",
                "Decelerating": "⏬ Decelerating"}


def _pct(v) -> str:
    return f"{v:+.0f}%" if v is not None else "n/a"


_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **The idea:** during results season ~1,500 companies report in a few weeks. This surfaces the "
    "ones whose **just-reported quarter** was strongest — so you see who delivered before the coverage "
    "catches up.\n"
    "- **Rev / PAT YoY** — the latest quarter vs the same quarter a year ago.\n"
    "- **Execution** — how the quarter landed overall, from the numbers: **Firing → Delivering → "
    "Holding → Slipping → Struggling** (YoY growth + margin trend vs the company's own run-rate).\n"
    "- **Trend** — is growth *accelerating*? ⏫ **Accelerating** = this quarter's profit growth is well "
    "above the prior few quarters' · ▶ **Steady** · ⏬ **Decelerating**.\n"
    "- **Score (0-100)** ranks results strength (growth magnitude + acceleration + margin inflection).\n"
    "- **No analyst consensus is used** (primary data only) — this is *biggest / accelerating growth vs "
    "the company's own history*, **not** 'beat vs street'. Watch base effects on tiny year-ago numbers.\n"
    "- ⭐ = a name you hold/track. **A discovery screen, not a call** — reply a number for that name's "
    "full deep report."
)


def build_results(con: duckdb.DuckDBPyConnection, *, limit: int = 25) -> dict | None:
    """The 📈 Results Radar email section. Returns ``{markdown, picks}`` (``picks`` = ``[{symbol,
    name}]`` for the numbered reply), or ``None`` when nothing reported in the recent window."""
    rows = results_radar.radar(con, limit=limit)
    if not rows:
        return None
    tbl = []
    for i, r in enumerate(rows, 1):
        star = "⭐ " if r["watchlist"] else ""
        tbl.append([
            i, r["symbol"], (star + r["name"])[:22], r["quarter"],
            _pct(r["rev_yoy"]), _pct(r["net_yoy"]), r["execution"],
            _ACCEL_LABEL.get(r["accel"], r["accel"]), r["score"],
        ])
    table = md.table(
        ["#", "Symbol", "Company", "Qtr end", "Rev YoY", "PAT YoY", "Execution", "Trend", "Score"],
        tbl, align="rlllrrllr")
    head = ("**📈 Results Radar — the strongest just-reported quarters**\n\n"
            "Companies that **just reported**, ranked by how strong the quarter was (YoY growth + "
            "acceleration + margin inflection), all from the numbers. **Reply a number for that name's "
            "full deep report.**\n\n")
    md_text = head + table + _LEGEND
    picks = [{"symbol": r["symbol"], "name": r["name"]} for r in rows]
    return {"markdown": md_text, "picks": picks}
