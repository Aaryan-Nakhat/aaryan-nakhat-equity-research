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
body { font-size: 12.5px; color: #161616; line-height: 1.5; }
h1 { font-size: 23px; margin: 0 0 8px; border-bottom: 3px solid #0a5c34; padding-bottom: 5px; color: #0a3d24; }
h2 { font-size: 17px; color: #0a5c34; margin: 20px 0 6px; border-bottom: 1px solid #b8b8b8; padding-bottom: 3px; }
h3 { font-size: 14px; margin: 14px 0 4px; color: #111; }
p, li { font-size: 12.5px; color: #161616; }
em { color: #3a3a3a; }
strong { color: #000; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 18px; font-size: 11.5px;
        border: 1px solid #b7c7c0; }
th, td { border: 1px solid #dbe5e0; padding: 6px 10px; text-align: right; white-space: nowrap;
         color: #161616; vertical-align: top; }
th { background: #0a5c34; text-align: center; font-weight: 700; color: #ffffff;
     border-color: #0a5c34; white-space: nowrap; }
td:first-child, th:first-child { text-align: left; font-weight: 600; color: #0a3d24; }
tr:nth-child(even) td { background: #eef4f1; }
tr:hover td { background: #e2efe9; }
code, pre { font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; }
pre { background: #f3f5f4; padding: 9px; border: 1px solid #d2d2d2; border-radius: 4px;
      white-space: pre; overflow-x: auto; }
hr { border: none; border-top: 1px solid #b0b0b0; margin: 16px 0; }
blockquote { border-left: 4px solid #0a6b3b; margin: 8px 0; padding: 4px 12px;
             background: #f4f8f6; color: #2a2a2a; }
figure.chart { margin: 10px 0 16px; page-break-inside: avoid; text-align: center; }
figure.chart img { max-width: 100%; height: auto; border: 1px solid #cfcfcf; }
figure.chart figcaption { font-size: 11px; color: #3a3a3a; margin-top: 3px; font-weight: 600; }
img { max-width: 100%; height: auto; }
p, li, h1, h2, h3, blockquote { overflow-wrap: break-word; }

/* --- Phone (email) only. `only screen` keeps this out of the PDF, which is printed
   at A4-landscape width and still wants nowrap tables. Without this the email body
   renders at desktop width on a phone, forcing zoom-out + sideways scrolling. --- */
@media only screen and (max-width: 600px) {
  body { font-size: 15px; line-height: 1.55; margin: 0; padding: 12px; }
  h1 { font-size: 20px; } h2 { font-size: 17px; } h3 { font-size: 15px; }
  p, li { font-size: 15px; }
  /* let cells wrap instead of forcing the page wide */
  th, td { white-space: normal; overflow-wrap: break-word; padding: 5px 6px; font-size: 12px; }
  /* a genuinely wide table scrolls inside its own box, not the whole page */
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  /* fenced blocks (IPO / disambiguation menus) must wrap, not scroll */
  pre { white-space: pre-wrap; overflow-wrap: break-word; font-size: 12px; }
  code { overflow-wrap: break-word; }
}
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


def _wrap_tables(html: str) -> str:
    """Put each table in a ``.tablewrap`` div so a wide financial table scrolls inside
    its own box on a phone (see the media query) instead of widening the whole page.
    Inert in the PDF, where ``.tablewrap`` carries no styling."""
    return html.replace("<table>", '<div class="tablewrap"><table>') \
               .replace("</table>", "</table></div>")


def render_html(markdown_text: str, title: str = "",
                images: list[tuple[str, bytes]] | None = None) -> str:
    """Render a markdown report string to a full styled HTML document.

    Shared by the PDF renderer and the email body so both look identical. The
    viewport tag + phone media query make the same HTML readable on a phone
    (wrapped text, no zoom-out) without changing the printed PDF.
    ``images`` (caption, png-bytes) are appended as a Charts section.
    """
    body = _wrap_tables(_md.markdown(markdown_text,
                                     extensions=["tables", "fenced_code", "sane_lists"]))
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
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
            page.set_default_timeout(60_000)         # bound every op so a busy box can't hang us
            page.set_content(html, wait_until="load", timeout=60_000)
            pdf = page.pdf(format="A4", landscape=True, print_background=True,
                           margin={"top": "12mm", "bottom": "12mm",
                                   "left": "10mm", "right": "10mm"})
        finally:
            browser.close()
    return pdf
