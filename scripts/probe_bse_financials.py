"""Phase 2 probe — find BSE's structured company-financials path.

Tries candidate BSE endpoints for Reliance (scripcode 500325): result-filing
feed (AnnGetData) and a few financial-data endpoints. Reports status + a shape
hint so we can pick the cleanest primary source. Run:

    uv run python scripts/probe_bse_financials.py
"""

from __future__ import annotations

import json

from scrapling.fetchers import Fetcher

SCRIP = "500325"  # Reliance Industries

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}

# Candidate endpoints (some may 404 — that's the point of probing).
CANDIDATES = {
    "result filings (AnnGetData)":
        ("https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
         f"?pageno=1&strCat=Result&strPrevDate=20240101&strScrip={SCRIP}"
         "&strSearch=P&strToDate=20260613&strType=C"),
    "financials - quarterly (Comonqtrcalc)":
        f"https://api.bseindia.com/BseIndiaAPI/api/Comonqtrcalc/w?scripcode={SCRIP}",
    "financials - data (CompanyFinancialData)":
        f"https://api.bseindia.com/BseIndiaAPI/api/CompanyFinancialData/w?scripcode={SCRIP}",
    "financials - results (FinancialResultData)":
        f"https://api.bseindia.com/BseIndiaAPI/api/FinancialResultData/w?scripcode={SCRIP}",
    "ratios (CompanyRatio)":
        f"https://api.bseindia.com/BseIndiaAPI/api/CompanyRatio/w?scripcode={SCRIP}",
    "peer / industry (ComphdrData)":
        f"https://api.bseindia.com/BseIndiaAPI/api/ComphdrData/w?scripcode={SCRIP}&seriesid=",
}


def _shape(text: str) -> str:
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return f"non-JSON ({len(text)} chars): {text[:80]!r}"
    if isinstance(data, dict):
        keys = list(data)
        hint = ""
        # AnnGetData nests rows under 'Table'.
        for k in ("Table", "Table1", "data", "Data"):
            if isinstance(data.get(k), list):
                hint = f" | {k}[{len(data[k])}] sample-keys={list(data[k][0])[:6] if data[k] else []}"
                break
        return f"dict keys={keys[:8]}{hint}"
    if isinstance(data, list):
        return f"list[{len(data)}] sample-keys={list(data[0])[:8] if data else []}"
    return f"scalar: {data!r}"


def main() -> int:
    print(f"BSE financials probe — Reliance ({SCRIP})\n")
    for label, url in CANDIDATES.items():
        try:
            r = Fetcher.get(url, headers=_HEADERS, stealthy_headers=True, timeout=30)
            body = (r.body or b"").decode("utf-8", "replace")
            ok = r.status == 200 and len(body) > 2
            print(f"  [{'OK ' if ok else 'XX '}] {label}")
            print(f"        status={r.status} bytes={len(body)}")
            if ok:
                print(f"        {_shape(body)}")
        except Exception as e:  # noqa: BLE001
            print(f"  [XX ] {label}: ERROR {type(e).__name__}: {e}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
