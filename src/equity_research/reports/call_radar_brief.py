"""Format the 🎙️ Concalls output into an email-ready markdown section.

Renders the most notable recent earnings calls — each with its **Management Tone** (from the words),
**Execution** (from the numbers), and the **Say-Do Gap** between them — as a numbered table that drives
the reply-a-number → deep-report flow. Returns ``{markdown, picks}``; ``None`` when nothing has been
scored yet.
"""

from __future__ import annotations

import duckdb

from equity_research.analysis import call_radar
from equity_research.reports import md

_GAP_LABEL = {"Talk > Numbers": "⚠️ Talk > Numbers", "Numbers > Talk": "💎 Numbers > Talk",
              "Aligned": "✓ Aligned", "Numbers n/a": "· —"}

_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **The idea:** an earnings call is management *talking about the future*. This reads the tone "
    "from **what they said** and cross-checks it against **what the numbers actually did** — and "
    "surfaces the calls where the two diverge. That gap is the signal.\n"
    "- **Management Tone** (from the words): the confidence of the forward outlook — **Very Confident → "
    "Confident → Balanced → Cautious → Defensive**.\n"
    "- **Execution** (from *our* numbers, not the words): how the reported quarter actually landed on "
    "YoY growth + margin trend vs the company's own run-rate — **Firing → Delivering → Holding → "
    "Slipping → Struggling**.\n"
    "- **Say-Do Gap:** ⚠️ **Talk > Numbers** = more upbeat than the results justify (a caution flag) · "
    "💎 **Numbers > Talk** = quietly delivering more than management is talking up (under-the-radar) · "
    "✓ **Aligned** = tone matches execution.\n"
    "- **Signal** ranks how notable the call is — the widest gaps and strongest calls first.\n"
    "- ⭐ = a name you hold/track. **A discovery screen, not a call** — reply a number for that name's "
    "full deep report before acting."
)


def build_call_radar(con: duckdb.DuckDBPyConnection, *, limit: int = 25) -> dict | None:
    """The 🎙️ Concalls email section from the scored ``concall_signals`` table. Returns
    ``{markdown, picks}`` (``picks`` = ``[{symbol, name}]`` for the numbered reply), or ``None`` when
    no calls have been scored in the recent window yet."""
    rows = call_radar.radar(con, limit=limit)
    if not rows:
        return None
    tbl = []
    for i, r in enumerate(rows, 1):
        star = "⭐ " if r["watchlist"] else ""
        tbl.append([
            i, r["symbol"], (star + r["name"])[:22], r["quarter"],
            r["tone"], r["execution"], _GAP_LABEL.get(r["gap"], r["gap"]),
            r["signal_score"], r["filed_date"],
        ])
    table = md.table(
        ["#", "Symbol", "Company", "Qtr", "Management Tone", "Execution", "Say-Do Gap", "Signal", "Filed"],
        tbl, align="rllllllrl")
    head = ("**🎙️ Concalls — the most notable earnings calls right now**\n\n"
            "Management's **forward tone** (from the transcript) vs the quarter's **Execution** (from our "
            "numbers) — ranked by how far the two diverge. **Reply a number for that name's full deep "
            "report.**\n\n")
    md_text = head + table + _LEGEND
    picks = [{"symbol": r["symbol"], "name": r["name"]} for r in rows]
    return {"markdown": md_text, "picks": picks}
