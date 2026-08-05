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
# Date-paged "all releases" listing — carries title + ministry per release, GET-parameterised
# by day/month/year, so we can walk back a window (the RSS only holds the latest ~20).
_LISTING = "https://pib.gov.in/allRel.aspx?reg=3&lang=1"

_PRID_RE = re.compile(r"PRID=(\d+)")
_POSTED_RE = re.compile(r"Posted On:\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4})")
# each listing row: <h3>Ministry</h3> … <a title='<title>' … PRID=<n>
_LISTING_RE = re.compile(r"(?:<h3[^>]*>(.*?)</h3>)|(?:<a\s+title='([^']*)'[^>]*PRID=(\d+))", re.S)


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


def latest_releases(limit: int = 120) -> list[dict]:
    """The latest releases from the all-releases listing as ``[{prid, title, ministry}, …]`` —
    richer than the RSS (which holds only ~20): ~100+ recent releases, each with the **ministry**
    it sits under and its **title**, so the caller can filter cheaply before fetching bodies.

    Note: PIB's public listing exposes only the *latest* ~100 releases (its date filter is a
    server-side control that doesn't page reliably), so this is "recent", not a fixed N-day
    archive — de-duped, newest first."""
    try:
        html = fetch_bytes(_LISTING).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    seen: set[str] = set()
    ministry: str | None = None
    for m in _LISTING_RE.finditer(html):
        if m.group(1) is not None:                       # an <h3> ministry header
            ministry = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        elif m.group(3) and m.group(3) not in seen:      # a release anchor
            title = re.sub(r"\s+", " ", m.group(2)).strip()
            if title:
                seen.add(m.group(3))
                out.append({"prid": m.group(3), "title": title, "ministry": ministry})
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
