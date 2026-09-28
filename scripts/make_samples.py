"""Build the README sample gallery from saved reports: a crisp PNG of each report's opening + its PDF.

    uv run python scripts/make_samples.py <outputs-dir> slug=glob [slug=glob ...]

e.g. ``adani-power=*ADANIPOWER*deep*.html``. For every pair it finds the newest matching ``.html`` under
``<outputs-dir>``, screenshots its top at 2× (``docs/samples/<slug>.png``) and copies the PDF saved beside
it, if any (``docs/samples/<slug>.pdf``). Generate the reports from a DB copy with the watchlist emptied,
so nothing personal ends up in a public sample.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "samples"
W, H = 1200, 1000     # CSS px of the shot (rendered at 2×) — wide enough for the list tables


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = Path(argv[0])
    OUT.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
        for pair in argv[1:]:
            slug, pattern = pair.split("=", 1)
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
            page.screenshot(path=str(OUT / f"{slug}.png"), clip={"x": 0, "y": 0, "width": W, "height": H})
            pdfs = [f for f in sorted(html.parent.glob(f"{html.stem}__*.pdf")) if "ratings_guide" not in f.name]
            if pdfs:
                shutil.copy2(pdfs[0], OUT / f"{slug}.pdf")
            print(f"✓ {slug}: {html.name}" + (f" + {pdfs[0].name}" if pdfs else " (no PDF)"))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
