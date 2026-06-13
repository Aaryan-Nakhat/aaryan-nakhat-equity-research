# scrapers

Thin, single-purpose scrapers for the **validated** primary-source paths (see
[`docs/SCRAPING.md`](../../../docs/SCRAPING.md)). Each returns clean structured
data (dict / `DataFrame`); no storage or analysis here.

| Module | Source | Tier | Returns |
|---|---|---|---|
| `bse` | `api.bseindia.com` | plain HTTP | `fetch_scrip_header(scripcode)` → quote/company dict |
| `nse_archives` | `nsearchives.nseindia.com` | plain HTTP | `fetch_bhavcopy(date)` → DataFrame (incl. `DELIV_PER`); `fetch_index_closes(date)` |
| `nse_api` | `www.nseindia.com/api/*` | Camoufox browser | `fetch_api(path)` → JSON (in-page XHR; reserve for endpoints with no file equivalent) |

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
