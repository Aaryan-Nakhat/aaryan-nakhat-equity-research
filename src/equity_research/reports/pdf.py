"""Render a markdown report to a styled PDF (markdown -> HTML -> Chromium PDF).

Uses the Playwright Chromium already installed for scraping — real browser
rendering gives clean multi-year tables and fonts, with no extra system deps.
Landscape A4 so the wide financial tables fit.
"""

from __future__ import annotations

import base64

import markdown as _md
from playwright.sync_api import sync_playwright

_CSS = """
* { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; }
body { font-size: 10px; color: #1b1b1b; line-height: 1.45; }
h1 { font-size: 19px; margin: 0 0 6px; border-bottom: 2px solid #222; padding-bottom: 4px; }
h2 { font-size: 14px; color: #0a6b3b; margin: 16px 0 4px; border-bottom: 1px solid #d0d0d0; padding-bottom: 2px; }
h3 { font-size: 12px; margin: 12px 0 3px; }
p, li { font-size: 10px; }
em { color: #555; }
strong { color: #111; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; font-size: 8.6px; }
th, td { border: 1px solid #bcbcbc; padding: 2px 6px; text-align: right; white-space: nowrap; }
th { background: #eef2f0; text-align: center; font-weight: 600; }
td:first-child, th:first-child { text-align: left; }
tr:nth-child(even) td { background: #fafafa; }
code, pre { font-family: 'Consolas', 'Courier New', monospace; font-size: 8.6px; }
pre { background: #f5f6f7; padding: 8px; border: 1px solid #e3e3e3; border-radius: 4px;
      white-space: pre; overflow-x: auto; }
hr { border: none; border-top: 1px solid #ccc; margin: 14px 0; }
figure.chart { margin: 8px 0 14px; page-break-inside: avoid; text-align: center; }
figure.chart img { max-width: 100%; height: auto; border: 1px solid #e3e3e3; }
figure.chart figcaption { font-size: 9px; color: #555; margin-top: 2px; }
"""


def _charts_html(images: list[tuple[str, bytes]]) -> str:
    """Embed (caption, png-bytes) charts as inline base64 <img> blocks."""
    if not images:
        return ""
    figs = []
    for caption, data in images:
        b64 = base64.b64encode(data).decode("ascii")
        figs.append(f'<figure class="chart"><img src="data:image/png;base64,{b64}"/>'
                    f'<figcaption>{caption}</figcaption></figure>')
    return '<h2>Charts</h2>' + "".join(figs)


def render_html(markdown_text: str, title: str = "",
                images: list[tuple[str, bytes]] | None = None) -> str:
    """Render a markdown report string to a full styled HTML document.

    Shared by the PDF renderer and the email body so both look identical.
    ``images`` (caption, png-bytes) are appended as a Charts section.
    """
    body = _md.markdown(markdown_text,
                        extensions=["tables", "fenced_code", "sane_lists"])
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>{title}</title><style>{_CSS}</style></head>'
            f'<body>{body}{_charts_html(images or [])}</body></html>')


def report_to_pdf(markdown_text: str, title: str = "",
                  images: list[tuple[str, bytes]] | None = None) -> bytes:
    """Render a markdown report string to PDF bytes (landscape A4)."""
    html = render_html(markdown_text, title, images)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            pdf = page.pdf(format="A4", landscape=True, print_background=True,
                           margin={"top": "12mm", "bottom": "12mm",
                                   "left": "10mm", "right": "10mm"})
        finally:
            browser.close()
    return pdf
