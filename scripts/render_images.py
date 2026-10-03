"""Render the README's designed images: every docs/media/src/img-*.html → docs/media/<name>.png.

    uv run python scripts/render_images.py                 # all of them
    uv run python scripts/render_images.py agents portfolio

Each page is 1600 px wide; the image is the #stage element at its natural height. Sources share theme.css (the
same look as the hero video, hero.html → scripts/render_video.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path("docs/media/src")
OUT = Path("docs/media")


def main(names: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    pages = sorted(SRC.glob("img-*.html"))
    if names:
        pages = [p for p in pages if p.stem.removeprefix("img-") in names]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        for src in pages:
            page.goto(src.resolve().as_uri())
            page.wait_for_function("document.fonts.status === 'loaded'")
            page.wait_for_timeout(250)
            out = OUT / f"{src.stem.removeprefix('img-')}.png"
            page.locator("#stage").screenshot(path=str(out))
            print(f"wrote {out} ({out.stat().st_size / 1e3:.0f} KB)")
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
