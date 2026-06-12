"""Phase 1 scraping probe.

Empirically tests how each primary source responds to scrapling, escalating
from plain HTTP to a browser-impersonating session only where needed. Prints a
compact report; writes nothing. Run:

    uv run python scripts/probe_sources.py
"""

from __future__ import annotations

import sys
import time

from scrapling.fetchers import Fetcher, FetcherSession

# Browser-like headers NSE/BSE XHR endpoints expect.
JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _line(label: str, ok: bool, detail: str) -> None:
    mark = "OK " if ok else "XX "
    print(f"  [{mark}] {label:<32} {detail}")


def probe_bse() -> None:
    print("\n== BSE (bseindia.com) ==")
    # 1. Homepage via plain stealthy Fetcher.
    try:
        r = Fetcher.get("https://www.bseindia.com/", stealthy_headers=True, timeout=30)
        _line("homepage (plain Fetcher)", r.status == 200, f"status={r.status} bytes={len(r.body)}")
    except Exception as e:  # noqa: BLE001
        _line("homepage (plain Fetcher)", False, f"ERROR {type(e).__name__}: {e}")

    # 2. Data endpoint: scrip header (Reliance = 500325). Needs Origin/Referer.
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
        "?Debtflag=&scripcode=500325&seriesid="
    )
    hdrs = {**JSON_HEADERS, "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/"}
    try:
        r = Fetcher.get(url, headers=hdrs, stealthy_headers=True, timeout=30)
        ctype = r.headers.get("content-type", "?")
        nbytes = len(r.body or b"")
        body = (r.text or "")[:120].replace("\n", " ")
        ok = r.status == 200 and nbytes > 0
        _line("scrip header API (plain)", ok,
              f"status={r.status} bytes={nbytes} ctype={ctype} body={body!r}")
    except Exception as e:  # noqa: BLE001
        _line("scrip header API (plain)", False, f"ERROR {type(e).__name__}: {e}")


def probe_nse() -> None:
    print("\n== NSE (nseindia.com) ==")
    # 1. Plain Fetcher on the API directly — expected to be blocked (no cookies).
    api = "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE"
    try:
        r = Fetcher.get(api, headers=JSON_HEADERS, stealthy_headers=True, timeout=30)
        _line("quote API (plain, no cookies)", r.status == 200,
              f"status={r.status} (block expected)")
    except Exception as e:  # noqa: BLE001
        _line("quote API (plain, no cookies)", False, f"ERROR {type(e).__name__}: {e}")

    # 2. Session: impersonate Chrome (TLS) + full NSE cookie sequence:
    #    homepage -> the actual get-quotes page (sets more cookies) -> API.
    quote_page = "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"
    try:
        with FetcherSession(impersonate="chrome", stealthy_headers=True, timeout=30) as s:
            home = s.get("https://www.nseindia.com/", headers=JSON_HEADERS)
            _line("homepage (session prime)", home.status == 200, f"status={home.status}")
            page = s.get(quote_page, headers=JSON_HEADERS)
            _line("get-quotes page (cookie step)", page.status == 200, f"status={page.status}")
            time.sleep(1.0)
            r = s.get(api, headers={**JSON_HEADERS, "Referer": quote_page})
            ok = r.status == 200
            detail = f"status={r.status}"
            if ok:
                try:
                    data = r.json()
                    price = data.get("priceInfo", {}).get("lastPrice")
                    detail += f" lastPrice={price}"
                except Exception:  # noqa: BLE001
                    detail += " (non-JSON body)"
            _line("quote API (full cookie seq)", ok, detail)
    except Exception as e:  # noqa: BLE001
        _line("quote API (full cookie seq)", False, f"ERROR {type(e).__name__}: {e}")


def main() -> int:
    print("Scraping probe — primary Indian equity sources")
    probe_bse()
    probe_nse()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
