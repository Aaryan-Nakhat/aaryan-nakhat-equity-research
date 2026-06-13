"""Map which NSE /api/ endpoints are reachable via the Camoufox in-page XHR,
and which derivatives data is available as a plain-HTTP archive file.

    uv run python scripts/probe_nse_endpoints.py
"""

from __future__ import annotations

from datetime import date

from scrapling.fetchers import Fetcher, StealthyFetcher

HOME = "https://www.nseindia.com/"

# Endpoints we care about for fundamentals/technicals (decision-grade).
API_ENDPOINTS = [
    "/api/marketStatus",
    "/api/fiidiiTradeReact",                                  # FII/DII cash activity
    "/api/corporate-announcements?index=equities",            # filings/announcements
    "/api/corporates-corporateActions?index=equities",        # dividends/splits/etc
    "/api/equity-stockIndices?index=NIFTY%2050",              # index constituents
    "/api/option-chain-indices?symbol=NIFTY",                 # index option chain (OI)
    "/api/option-chain-equities?symbol=RELIANCE",             # stock option chain
]

_BATCH = """async ({paths, retries, delay}) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const out = {};
    for (const p of paths) {
        let last = {status: 0, len: 0};
        for (let i = 0; i < retries; i++) {
            const r = await fetch(p, {headers: {'Accept': 'application/json'}});
            const body = await r.text();
            last = {status: r.status, len: body.length, attempt: i + 1};
            if (r.status === 200) break;
            await sleep(delay);
        }
        out[p] = last;
    }
    return out;
}"""


def probe_apis() -> None:
    print("== NSE /api/ endpoints (Camoufox in-page XHR, warm=homepage) ==")
    captured: dict = {}

    def action(page):
        captured.update(page.evaluate(_BATCH, {"paths": API_ENDPOINTS, "retries": 3, "delay": 1500}))
        return page

    StealthyFetcher.fetch(HOME, headless=True, network_idle=True, page_action=action)
    for ep in API_ENDPOINTS:
        r = captured.get(ep, {})
        ok = r.get("status") == 200
        print(f"  [{'OK ' if ok else 'XX '}] {ep:<52} status={r.get('status')} len={r.get('len')}")


def probe_deriv_file() -> None:
    print("\n== NSE derivatives archive files (plain HTTP) ==")
    d = date(2026, 6, 12).strftime("%d%m%Y")
    files = {
        "FII/FPI & client deriv OI": f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv",
        "F&O bhavcopy (UDiFF)": f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date(2026,6,12).strftime('%Y%m%d')}_F_0000.csv.zip",
    }
    for label, url in files.items():
        try:
            r = Fetcher.get(url, stealthy_headers=True, timeout=30)
            nbytes = len(r.body or b"")
            ok = r.status == 200 and nbytes > 0
            print(f"  [{'OK ' if ok else 'XX '}] {label:<28} status={r.status} bytes={nbytes}")
        except Exception as e:  # noqa: BLE001
            print(f"  [XX ] {label:<28} ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    probe_apis()
    probe_deriv_file()
    print("\nDone.")
