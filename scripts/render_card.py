"""Render a fixed-size HTML card (e.g. ``docs/media/tailwind-agents.html``) to a PNG beside it, at 2×.

    uv run python scripts/render_card.py docs/media/tailwind-agents.html [--size 1200x675]
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("html")
    ap.add_argument("--size", default="1200x675", help="card size in CSS px (default: a social-post card)")
    args = ap.parse_args()
    w, h = (int(v) for v in args.size.lower().split("x"))
    src = Path(args.html).resolve()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
        page.goto(src.as_uri())
        page.wait_for_load_state("networkidle")
        out = src.with_suffix(".png")
        page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": w, "height": h})
        browser.close()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
