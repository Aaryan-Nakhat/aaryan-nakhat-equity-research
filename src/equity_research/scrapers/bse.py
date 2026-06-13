"""BSE scraper (``api.bseindia.com``).

BSE serves quote/company data as JSON over plain HTTP — no bot wall, just an
``Origin``/``Referer`` requirement. This is our primary source for live quotes
and company fundamentals (BSE is friendlier than NSE for per-scrip lookups).

Scrips are addressed by BSE numeric ``scripcode`` (e.g. Reliance = 500325).
See ``docs/SCRAPING.md``.
"""

from __future__ import annotations

from typing import Any

from equity_research.common.http import fetch_json

_BASE = "https://api.bseindia.com/BseIndiaAPI/api"

# BSE's API rejects requests without a matching Origin/Referer.
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


def fetch_scrip_header(scripcode: str | int) -> dict[str, Any]:
    """Header/quote block for a scrip (name, group, face value, last price…)."""
    url = (f"{_BASE}/getScripHeaderData/w"
           f"?Debtflag=&scripcode={scripcode}&seriesid=")
    return fetch_json(url, headers=_HEADERS)
