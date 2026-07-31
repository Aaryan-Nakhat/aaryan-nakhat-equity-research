"""Tiny markdown helpers shared by the interactive screens and the proactive digest.

A real markdown pipe-table renders as a styled ``<table>`` in both the email body and
the PDF (via ``pdf.render_html``), unlike the old code-fenced space-aligned text which
collapsed into unreadable mush on phones.
"""

from __future__ import annotations

_SEP = {"l": ":--", "r": "--:", "c": ":-:"}


def table(headers: list[str], rows: list[list], align: str) -> str:
    """A GitHub-flavoured markdown pipe-table. ``align`` is one char per column:
    'l' left · 'r' right · 'c' centre — carried in the separator so numeric columns
    right-align. Pipes inside cells are escaped so a stray '|' can't break the table."""
    def _row(cells: list) -> str:
        return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"
    out = [_row(headers), "| " + " | ".join(_SEP.get(a, ":--") for a in align) + " |"]
    out += [_row(r) for r in rows]
    return "\n".join(out)
