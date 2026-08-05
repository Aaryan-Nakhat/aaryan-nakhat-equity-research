"""PIB (Press Information Bureau) scraper — the government's official pre-announcement channel.

PIB is where ministries publish schemes / policies / reforms — often at the cabinet-approval,
draft or consultation stage, *before* the formal launch. Primary and government-backed (fits the
project's primary-only rule), plain HTTP:

1. The **English RSS** (`RssMain.aspx?...Lang=1...`) lists the ~20 most recent releases with a
   title + a ``PRID`` link.
2. Each release's full text is on ``PressReleaseIframePage.aspx?PRID=<id>`` — ministry, headline,
   posted-date and the body.

Only the raw text is scraped here; the *interpretation* (is this an economic scheme, which sector,
which companies) is done downstream by the LLM in ``analysis/policy.py``.
"""

from __future__ import annotations

import re

from equity_research.common.http import fetch_bytes

# English national feed (Lang=1). reg=3 = English; ModId=6 = press releases.
_RSS = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3"
_BODY = "https://pib.gov.in/PressReleaseIframePage.aspx?PRID={prid}&reg=3&lang=1"

_PRID_RE = re.compile(r"PRID=(\d+)")
_POSTED_RE = re.compile(r"Posted On:\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4})")


def _strip_html(html: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def recent_releases(limit: int = 25) -> list[dict]:
    """The most recent PIB releases (English) as ``[{prid, title, link}, …]``, newest first.
    Best-effort — returns [] if the feed is unavailable."""
    try:
        xml = fetch_bytes(_RSS).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — never break the caller
        return []
    out: list[dict] = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", xml):
        title = re.search(r"(?s)<title>(.*?)</title>", item)
        link = re.search(r"(?s)<link>(.*?)</link>", item)
        if not (title and link):
            continue
        prid = _PRID_RE.search(link.group(1))
        if not prid:
            continue
        out.append({"prid": prid.group(1), "title": title.group(1).strip(),
                    "link": link.group(1).strip()})
        if len(out) >= limit:
            break
    return out


def release_text(prid: str, *, max_chars: int = 4500) -> dict | None:
    """Full text of one release: ``{prid, posted_on, body}`` (body capped for the LLM).
    ``None`` if unavailable. Ministry / classification are left to the LLM downstream."""
    try:
        html = fetch_bytes(_BODY.format(prid=prid)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    body = _strip_html(html)
    if not body:
        return None
    posted = _POSTED_RE.search(body)
    return {"prid": prid, "posted_on": posted.group(1) if posted else None,
            "body": body[:max_chars]}
