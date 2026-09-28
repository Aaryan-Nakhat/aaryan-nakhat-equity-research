"""Build the README gallery from saved reports: crisp PNGs of whole reports or single sections,
plus the report's PDF.

    uv run python scripts/make_samples.py <outputs-dir> SPEC [SPEC ...]

Each SPEC is ``slug=glob`` optionally followed by what to capture:

* ``slug=*adani*.html``                      — the top of the report
* ``slug=*adani*.html#Forensic deep-dive``   — one section: from the first heading containing that text
                                               to the next heading of the same or a higher level
* ``slug=*adani*.html@Monte-Carlo``          — one chart: the figure whose caption contains that text
* ``slug=*adani*.html#Verdict>9``            — ``>N`` also includes N more headings' sections
* ``slug=*adani*.html#Forensic deep-dive!``  — ``!`` stops at the very next heading of any level

The newest matching ``.html`` under ``<outputs-dir>`` is used; a whole-report capture also copies its
PDF (``docs/samples/<slug>.pdf``). Generate the reports from a database copy with the watchlist
emptied, so nothing personal ends up in a public sample.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "samples"
W = 1200                 # CSS px wide — enough for the list tables (rendered at 2×)
TOP_H = 1000             # height of a "top of the report" capture
MAX_H = 1600             # a section taller than this is cut here (the full report is one click away)

_SECTION_JS = """([needle, extra, maxH, tight]) => {
  const hs = [...document.querySelectorAll('h1,h2,h3,h4')];
  const i = hs.findIndex(h => h.textContent.toLowerCase().includes(needle.toLowerCase()));
  if (i < 0) return null;
  const lvl = h => Number(h.tagName[1]);
  let end = null, skipped = 0;
  for (let j = i + 1; j < hs.length; j++) {
    if (tight || lvl(hs[j]) <= lvl(hs[i])) { if (skipped++ >= extra) { end = hs[j]; break; } }
  }
  const top = hs[i].getBoundingClientRect().top + window.scrollY - 12;
  const bottom = end ? end.getBoundingClientRect().top + window.scrollY - 8
                     : document.documentElement.scrollHeight;
  return {y: Math.max(0, top), h: Math.min(bottom - top, maxH)};
}"""

_FIGURE_JS = """(needle) => {
  const f = [...document.querySelectorAll('figure')].find(
      x => x.textContent.toLowerCase().includes(needle.toLowerCase()));
  if (!f) return null;
  const r = f.getBoundingClientRect();
  return {y: Math.max(0, r.top + window.scrollY - 8), h: r.height + 16};
}"""


def _parse(spec: str) -> tuple[str, str, str, str, int, bool]:
    slug, rest = spec.split("=", 1)
    m = re.match(r"^(.*?\.html)(?:([#@])(.+?))?(!)?(?:>(\d+))?$", rest)
    if not m:
        raise SystemExit(f"bad spec: {spec}")
    return slug, m.group(1), m.group(2) or "", m.group(3) or "", int(m.group(5) or 0), bool(m.group(4))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = Path(argv[0])
    OUT.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": TOP_H}, device_scale_factor=2)
        for spec in argv[1:]:
            slug, pattern, kind, needle, extra, tight = _parse(spec)
            hits = sorted(src.rglob(pattern), key=lambda f: f.stat().st_mtime)
            if not hits:
                print(f"✗ {slug}: nothing matches {pattern}")
                continue
            html = hits[-1]
            page.goto(html.resolve().as_uri())
            page.wait_for_load_state("networkidle")
            # the "♻️ cached scan" note is about the run, not the report — leave it out of the picture
            page.evaluate("""() => document.querySelectorAll('blockquote').forEach(b => {
                if (/Cached (scan|run)/.test(b.textContent)) b.remove(); })""")
            if kind == "#":
                box = page.evaluate(_SECTION_JS, [needle, extra, MAX_H, tight])
            elif kind == "@":
                box = page.evaluate(_FIGURE_JS, needle)
            else:
                box = {"y": 0, "h": TOP_H}
            if not box:
                print(f"✗ {slug}: no {'heading' if kind == '#' else 'chart'} matching {needle!r} in {html.name}")
                continue
            page.screenshot(path=str(OUT / f"{slug}.png"), full_page=True,
                            clip={"x": 0, "y": box["y"], "width": W, "height": box["h"]})
            note = ""
            if not kind:
                pdfs = [f for f in sorted(html.parent.glob(f"{html.stem}__*.pdf")) if "guide" not in f.name.lower()]
                if pdfs:
                    shutil.copy2(pdfs[0], OUT / f"{slug}.pdf")
                    note = f" + {pdfs[0].name}"
            print(f"✓ {slug}: {html.name}{(' ' + kind + needle) if kind else ''} ({box['h']:.0f}px){note}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
