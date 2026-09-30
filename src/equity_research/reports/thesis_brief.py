"""🛡️ Thesis Guard reports — one thesis, the list of all of them, and the evening change alert."""

from __future__ import annotations

from equity_research.analysis.thesis_guard import ICON, METRICS, _describe, _fmt

DISCLAIMER = ("_This re-checks **your own** reasons and rules against the company's filings and prices — "
              "it isn't advice to buy or sell. Numbers come from filings (named >1% holders only for "
              "institutional stakes); judged reasons cite the filing used._")
_WORD = {"intact": "intact", "weakening": "weakening", "broken": "broken", "unknown": "no data"}


def _status(s: str) -> str:
    return f"{ICON[s]} {_WORD[s]}"


def render_one(thesis: dict, result: dict, *, note: str = "") -> str:
    head = f"# 🛡️ Thesis Guard — {thesis['name']} ({thesis['symbol']})"
    lines = [head, f"> _“{thesis['text']}”_ — written {thesis['created_at']:%d-%b-%Y}"]
    if note:
        lines.append(note)
    lines.append(f"## Your thesis is **{_status(result['overall'])}**")
    if result["checks"]:
        rows = ["| Your reason | What's checked | Now | Status |", "|---|---|---|---|"]
        for c in result["checks"]:
            now = c.get("note") or "—"
            if c.get("source", "").startswith("http"):
                now += f" ([filing]({c['source']}))"
            elif c.get("source"):
                now += f" · _{c['source']}_"
            rows.append(f"| {c.get('because') or '—'} | {_describe(c)} | {now} | {_status(c['status'])} |")
        lines.append("\n".join(rows))
    if result["rules"]:
        rows = ["| Your rule | Status |", "|---|---|"]
        for r in result["rules"]:
            rows.append(f"| {_describe(r)} | {'🔔 **triggered** — ' if r['triggered'] else '⏳ not yet — '}{r['note']} |")
        lines.append("## 🚪 Your exit plan\n\n" + "\n".join(rows))
    lines.append("_Re-checked every evening; you'll get an email only when a reason changes status or a rule "
                 "triggers. `thesis: " + thesis["symbol"] + "` shows this again, `unthesis: " + thesis["symbol"]
                 + "` stops it._")
    lines.append("---\n" + DISCLAIMER)
    return "\n\n".join(lines)


def render_list(items: list[tuple[dict, dict]]) -> str:
    if not items:
        return ("# 🛡️ Thesis Guard\n\nNo theses yet. Write why you own a stock and your rules, e.g.\n\n"
                "`thesis: BEL — order book keeps growing, debt-free, promoters not selling; exit below 250, "
                "trail 15%`\n\nIt's re-checked every evening against filings and prices.")
    rows = ["| Company | Thesis | Weakest reason | Rules triggered |", "|---|---|---|---|"]
    for t, r in items:
        worst = max(r["checks"], key=lambda c: {"broken": 3, "weakening": 2, "unknown": 1, "intact": 0}[c["status"]],
                    default=None)
        trig = ", ".join(_describe(x) for x in r["rules"] if x["triggered"]) or "—"
        rows.append(f"| {t['name']} ({t['symbol']}) | {_status(r['overall'])} | "
                    f"{_describe(worst) + ' — ' + worst['note'] if worst and worst['status'] != 'intact' else '—'} | {trig} |")
    return ("# 🛡️ Thesis Guard — all your theses\n\n" + "\n".join(rows) +
            "\n\n_`thesis: <company>` for the full check · `unthesis: <company>` to stop tracking one._\n\n---\n"
            + DISCLAIMER)


def render_changes(moved: list[tuple[dict, dict, list[str]]]) -> str:
    parts = ["# 🛡️ Thesis Guard — what changed tonight"]
    for t, r, ch in moved:
        parts.append(f"## {t['name']} ({t['symbol']}) — thesis {_status(r['overall'])}\n\n" + "\n".join(f"- {c}" for c in ch))
    parts.append("_Reply `thesis: <company>` for the full check._\n\n---\n" + DISCLAIMER)
    return "\n\n".join(parts)


__all__ = ["render_one", "render_list", "render_changes", "METRICS", "_fmt"]
