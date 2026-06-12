# Scraping — Phase 1 findings

Empirical results from probing primary sources with `scrapling 0.4.9`. These
**supersede the initial reasoning** in [`DATA_SOURCES.md`](DATA_SOURCES.md).

Probe scripts: [`scripts/probe_sources.py`](../scripts/probe_sources.py) (HTTP
tier), [`scripts/probe_nse_browser.py`](../scripts/probe_nse_browser.py) (NSE
browser tier). Re-run anytime: `uv run python scripts/probe_sources.py`.

Date of probe: 2026-06-13 (last NSE trade date observed: 12-Jun-2026).

## scrapling tiers (what we use)

| Tier | Class | Cost | Beats |
|---|---|---|---|
| Plain HTTP | `Fetcher` / `FetcherSession` | cheap, fast | header/cookie gating, TLS fingerprint (curl_cffi `impersonate`) |
| Stealth browser | `StealthyFetcher` / `StealthySession` (Camoufox) | slow, heavy | JS challenges (Akamai `_abck`), real DOM |

Install browsers once: `uv run scrapling install`.

## Results by source

### ✅ BSE — plain HTTP works
- Homepage: `200`.
- `api.bseindia.com/.../getScripHeaderData` (Reliance `500325`): `200`,
  `application/json`, ~1.2 KB. Needs `Origin`/`Referer: https://www.bseindia.com`.
- **Verdict:** plain `Fetcher`. **Primary source for live quotes + company
  fundamentals/filings.**

### ✅ NSE archives (`nsearchives.nseindia.com`) — plain HTTP works
- `sec_bhavdata_full_DDMMYYYY.csv`: `200`, ~360 KB real CSV — **includes
  `DELIV_QTY, DELIV_PER`** (the NSE-exclusive delivery % we want).
- `ind_close_all_DDMMYYYY.csv` (index closes): `200`.
- **Verdict:** plain `Fetcher`. **This is how we get NSE bhavcopy + delivery %.**
  No browser needed.

### ⚠️ NSE APIs (`www.nseindia.com/api/...`) — browser tier, partial
- Plain HTTP (even with homepage→get-quotes cookie priming + Chrome TLS
  impersonation): **`403`**. Akamai Bot Manager needs JS-validated cookies.
- Stealth browser (Camoufox), page loads fine (`200`, ~500 KB). Direct
  top-level navigation to an `/api/` URL still `403` (`sec-fetch-mode: navigate`
  is rejected for XHR-only paths).
- **In-page `fetch()`** via `page_action` (real same-origin XHR):
  - `/api/marketStatus` → **`200`** real JSON. ✅
  - `/api/quote-equity?symbol=…` → **`403` static "Access Denied"** (path-specific
    Akamai WAF rule) — fails regardless of headers/retries, even though
    `marketStatus` passes with the *same* cookies. ❌ (hard-blocked right now)
- **Verdict:** NSE `/api/` is reachable via Camoufox **in-page fetch** for many
  endpoints, but `quote-equity` is currently WAF-blocked. **Not a problem** — we
  get bhavcopy+delivery from the archive host and live quotes from BSE, so we
  don't depend on `quote-equity`.

## Gotchas discovered

1. **`Response.text` can be empty while `Response.body` has bytes.** Happens on
   responses with no charset/BOM (NSE CSVs, the BSE JSON). Always read via
   `r.body.decode("utf-8")` or `r.json()` — never trust `.text` blindly.
2. **NSE direct `/api/` navigation ≠ in-page XHR.** Must run `fetch()` *inside*
   the page (`page_action` hook) so `sec-fetch-mode: cors` + validated `_abck`
   are sent.
3. **`scrapling[fetchers]` extra is required** for the HTTP `Fetcher`
   (`curl_cffi`) and the browser engines (Playwright/Camoufox). Base install
   only gives the parser.
4. **Akamai `_abck` validation is async** — page HTML loads before XHRs are
   authorized; budget a warm-up. (Doesn't rescue `quote-equity`, but matters
   for other `/api/` endpoints.)

## Recommended source strategy (revised)

| Need | Source | Tier |
|---|---|---|
| Live quote / company fundamentals / filings | **BSE** (`api.bseindia.com`) | plain HTTP |
| EOD bhavcopy + **delivery %** | **NSE archives** (`nsearchives…`) | plain HTTP |
| Index closes | NSE archives | plain HTTP |
| NSE `/api/` endpoints (FII derivs, OI, etc.) | NSE (in-page `fetch` via Camoufox) | browser |
| Market status | NSE `/api/marketStatus` | browser (in-page) |

**Principle:** prefer the **archive-file + BSE plain-HTTP** paths (fast, robust);
reserve the **Camoufox browser tier** only for NSE `/api/` endpoints that have no
file equivalent. Treat `quote-equity` as unavailable for now.

## Open follow-ups

- Map which NSE `/api/` endpoints we actually need (FII/DII derivs, option chain,
  corporate filings) and confirm each via in-page fetch.
- Find archive-file equivalents for NSE corporate announcements / shareholding
  (file downloads dodge the WAF entirely).
- Build a thin scraper module per confirmed path (Phase 1 deliverable).
