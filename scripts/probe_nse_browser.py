"""NSE browser-tier probe — does Camoufox (StealthyFetcher) beat Akamai?

NSE's /api/ is 403 to any plain HTTP client (Akamai Bot Manager needs JS cookies).
This tests the browser tier. Run:

    uv run python scripts/probe_nse_browser.py
"""

from __future__ import annotations

import sys

from scrapling.fetchers import StealthySession, StealthyFetcher

API = "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE"
PAGE = "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"


def _report(label: str, r) -> None:
    ok = getattr(r, "status", None) == 200
    nbytes = len(getattr(r, "body", b"") or b"")
    print(f"  [{'OK ' if ok else 'XX '}] {label:<34} status={getattr(r,'status',None)} bytes={nbytes}")


def main() -> int:
    print("NSE browser-tier probe (Camoufox / StealthyFetcher)\n")

    # 1. Load the real quote page — proves Camoufox launches + solves Akamai.
    try:
        page = StealthyFetcher.fetch(PAGE, headless=True, network_idle=True)
        _report("get-quotes page (browser)", page)
    except Exception as e:  # noqa: BLE001
        print(f"  [XX ] get-quotes page (browser)          ERROR {type(e).__name__}: {e}")

    # 2. Hit the API URL directly in the browser (carries JS-challenge cookies).
    try:
        api = StealthyFetcher.fetch(API, headless=True, network_idle=True)
        _report("quote API (browser direct)", api)
    except Exception as e:  # noqa: BLE001
        print(f"  [XX ] quote API (browser direct)         ERROR {type(e).__name__}: {e}")

    # 3. Load page, then run fetch() INSIDE the page (real XHR, valid cookies).
    captured: dict = {}

    def grab_api(page):
        # Probe several NSE APIs in-page with retries to see which (if any) pass.
        result = page.evaluate(
            """async () => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                // [label, url, headers] — vary headers to isolate the WAF trigger.
                const cases = [
                    ['quote default-hdrs', '/api/quote-equity?symbol=RELIANCE', {}],
                    ['quote trade_info',   '/api/quote-equity?symbol=RELIANCE&section=trade_info', {}],
                    ['quote XHR hdr',      '/api/quote-equity?symbol=RELIANCE',
                        {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}],
                ];
                const out = {};
                for (const [label, url, headers] of cases) {
                    let last = {status: 0, body: ''};
                    for (let i = 0; i < 3; i++) {
                        const r = await fetch(url, {headers});
                        last = {status: r.status, body: await r.text(), attempt: i + 1};
                        if (r.status === 200) break;
                        await sleep(1500);
                    }
                    out[label] = last;
                }
                return out;
            }"""
        )
        captured.update(result)
        return page

    try:
        StealthyFetcher.fetch(PAGE, headless=True, network_idle=True, page_action=grab_api)
        for ep, res in captured.items():
            status = res.get("status")
            body = res.get("body", "") or ""
            ok = status == 200
            print(f"  [{'OK ' if ok else 'XX '}] in-page {ep:<34} "
                  f"status={status} bytes={len(body)} attempt={res.get('attempt')}")
            print(f"        body[:200]={body[:200].replace(chr(10),' ')!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [XX ] in-page fetch                     ERROR {type(e).__name__}: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
