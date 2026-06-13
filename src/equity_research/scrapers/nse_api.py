"""NSE ``/api/`` scraper (browser tier).

``www.nseindia.com/api/*`` sits behind Akamai Bot Manager: plain HTTP gets 403
even with primed cookies. The working pattern (validated in ``docs/SCRAPING.md``)
is to load a real page in Camoufox — which solves the JS challenge — then run
``fetch()`` **inside the page** (a same-origin XHR carrying the validated
``_abck`` cookie) via scrapling's ``page_action`` hook.

Heavy (launches a browser), so reserve this for ``/api/`` endpoints that have no
plain-HTTP archive-file equivalent. Note ``/api/quote-equity`` is currently
WAF-blocked even here; use BSE for per-scrip quotes instead.
"""

from __future__ import annotations

import json
from typing import Any

from scrapling.fetchers import StealthyFetcher

from equity_research.common.http import ScrapeError

_HOME = "https://www.nseindia.com/"

# Run inside the page: retry the XHR because Akamai validates _abck asynchronously.
_IN_PAGE_FETCH = """async ({path, retries, delay}) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let last = {status: 0, body: ''};
    for (let i = 0; i < retries; i++) {
        const r = await fetch(path, {headers: {'Accept': 'application/json'}});
        last = {status: r.status, body: await r.text(), attempt: i + 1};
        if (r.status === 200) break;
        await sleep(delay);
    }
    return last;
}"""


def fetch_api(
    path: str,
    *,
    warm_url: str = _HOME,
    retries: int = 4,
    retry_delay_ms: int = 1500,
    headless: bool = True,
) -> Any:
    """Fetch an NSE ``/api/`` endpoint via Camoufox in-page XHR.

    ``path`` is the API path (e.g. ``"/api/marketStatus"``). ``warm_url`` is the
    page loaded first to solve the bot challenge — some endpoints need a matching
    page (e.g. a get-quotes page) rather than the homepage. Raises ``ScrapeError``
    on non-200.
    """
    if not path.startswith("/"):
        path = "/" + path
    captured: dict[str, Any] = {}

    def _action(page):
        captured.update(
            page.evaluate(
                _IN_PAGE_FETCH,
                {"path": path, "retries": retries, "delay": retry_delay_ms},
            )
        )
        return page

    StealthyFetcher.fetch(warm_url, headless=headless, network_idle=True, page_action=_action)

    if captured.get("status") != 200:
        raise ScrapeError(warm_url.rstrip("/") + path, captured.get("status"))
    return json.loads(captured.get("body") or "{}")
