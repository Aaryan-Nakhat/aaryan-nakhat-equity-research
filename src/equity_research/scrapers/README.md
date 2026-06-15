# scrapers

Thin, single-purpose scrapers for the **validated** primary-source paths (see
[`docs/SCRAPING.md`](../../../docs/SCRAPING.md)). Each returns clean structured
data (dict / `DataFrame`); no storage or analysis here.

| Module | Source | Tier | Returns |
|---|---|---|---|
| `bse` | `api.bseindia.com` | plain HTTP | `fetch_scrip_header(scripcode)` → quote/company dict |
| `nse_archives` | `nsearchives.nseindia.com` | plain HTTP | `fetch_bhavcopy(date)` (incl. `DELIV_PER`); `fetch_index_closes(date)`; `fetch_participant_oi(date)`; `fetch_fo_bhavcopy(date)` |
| `nse_api` | `www.nseindia.com/api/*` | Camoufox browser | `fetch_api(path)` (generic in-page XHR); `fii_dii_activity()`; `corporate_announcements()`; `corporate_actions()`; `option_chain_equity(symbol)` |
| `nse_financials` | NSE results API (catalog) + XBRL on `nsearchives` | browser (catalog) + plain HTTP (XBRL) | `list_result_filings(symbol, period)`; `parse_result_xbrl(bytes)` → structured `in-bse-fin` financials (see [`docs/FUNDAMENTALS.md`](../../../docs/FUNDAMENTALS.md)) |

Prefer the plain-HTTP `nse_archives` files wherever they exist (fast, robust);
the browser tier is only for `/api/` endpoints with no file equivalent.
`equity-stockIndices` and `option-chain-indices` currently 404 (paths moved) —
not yet wrapped.

Shared fetch helpers live in `equity_research.common.http` (`fetch_bytes` /
`fetch_text` / `fetch_json`), which work around the `Response.text`-empty gotcha
by decoding `Response.body` directly.

```python
from datetime import date
from equity_research.scrapers import bse, nse_archives, nse_api

bse.fetch_scrip_header(500325)              # Reliance quote
nse_archives.fetch_bhavcopy(date(2026, 6, 12))   # EOD + delivery %
nse_api.fetch_api("/api/marketStatus")      # browser tier
```
