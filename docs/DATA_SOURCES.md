# Data Sources

> **Initial-thinking inventory** of primary/official Indian equity data sources
> and their scrapability. This was a *first pass from reasoning*.
>
> ⚠️ **Validated & extended (updated 2026-06-29).** Sources have been probed with
> `scrapling` — see [`SCRAPING.md`](SCRAPING.md) for empirical results, which
> **supersede** the ratings below. Notably since cracked: NSE bhavcopy + delivery % is
> **plain-HTTP** via the archive host; the old `/api/quote-equity` (now 403) and
> `/api/equity-stockIndices` (404) moved to NSE's **NextApi** live-quote endpoint;
> **insider/promoter trades** via `/api/corporates-pit`; **post-Dec-2024 financials** via
> SEBI's **integrated-filing** feed (`in-capmkt` XBRL). And beyond NSE — **USD/INR** (FBIL
> JSON, no browser) and **gold/silver/crude** futures (MCX, via an `X-Requested-With`
> header) are now wired into the digest header. (The 10-yr G-Sec yield — RBI/FBIL/CCIL —
> remains the one deferred macro feed.)

## Ground rules

- **Primary / government-backed only** — exchanges (NSE/BSE), regulators
  (SEBI/RBI), statistics (MOSPI), and companies' own statutory filings.
- **Excluded by design:** blogs, news, broker reports, screeners, aggregators,
  and any paid third-party data vendor.

## Legend (access reality)

- 🟢 **Public** — open download/endpoint, scrape directly.
- 🟡 **Session-gated** — public but needs browser-like session / cookies /
  headers (NSE's anti-bot). No login. `scrapling` stealth/browser mode.
- 🔴 **Blocked** — login + paid + captcha; not practically scrapable.

---

## 1. NSE — nseindia.com

> No login anywhere, but **everything is behind anti-bot session/cookie/header
> gating**. This is the #1 scraping challenge of the whole project.

| Data | Access | Notes |
|---|---|---|
| EOD bhavcopy (OHLCV + delivery %) | 🟡 | Archive file download; needs session first. |
| Live quote + market depth (top-5 order book) | 🟡 | JSON endpoint; cookie + referer. |
| Corporate announcements / filings (results, transcripts, PPTs) | 🟡 | JSON + PDF links. |
| Corporate actions (dividend/bonus/split/buyback) | 🟡 | JSON. |
| Shareholding pattern | 🟡 | Quarterly promoter/FII/DII/public split. **Holder-level tables** (every promoter account + public >1% holder, with names) come from the filing's **SHP XBRL on nsearchives (plain HTTP)** via the share-holdings-master catalog — `scrapers/nse_shp.py`; classified listed/unlisted via the EQUITY_L.csv master. |
| Insider trading (SAST / PIT disclosures) | 🟡 | JSON. |
| Bulk / block deals | 🟡 | |
| FII / DII daily cash activity | 🟡 | NSE-published. |
| F&O: OI, PCR, option chain, FII deriv stats | 🟡 | **Hardest** — heavily rate-limited / bot-protected. |
| ASM / GSM / surveillance, circuit limits | 🟡 | |
| Index constituents (Nifty 50/500, sector) | 🟡 | CSV. Carries only the **macro-sector** (e.g. 'Consumer Durables'). |
| Sector-index constituents (Pharma / Bank / Defence …) | 🟢 | Per-index archive CSV `ind_<slug>list.csv` (Symbol + Company + **ISIN** + industry) — powers the within-sector ranking + supply-chain. Plain HTTP; macro-industry fallback on 404. |
| **GIFT Nifty** (overnight Nifty future, NSE IX) | 🟢 | `nseix.com/api/derivatives-watch` — clean JSON over **plain HTTP** (NSE IX isn't behind Akamai, unlike nseindia.com). Nearest-expiry NIFTY FUTIDX = the pre-market lead indicator. `scrapers/nseix.py`. |
| Granular industry (basic_industry) | 🟡 | `getSymbolData.secInfo.basicIndustry` (e.g. 'Gems Jewellery And Watches') — the fine tier the constituent CSVs omit. Enriched onto `sector_map` via `ingest_basic_industries` so peers group jeweller-with-jewellers, not all of 'Consumer Durables'. |
| **IPOs** — live / upcoming issues + subscription | 🟡 | `/api/ipo-current-issue`, `/api/all-upcoming-issues?category=ipo`, `/api/ipo-detail` (category-wise). Browser tier. |
| **IPO offer documents** — RHP / price-band KPIs / anchor | 🟢 | Archive `nsearchives…/content/ipo/<DOC>_<SYM>.zip`, predictable per symbol. Plain HTTP. The **primary source** for pre-listing financials, fresh/OFS, risks. |

**Delivery %** and **FII derivatives** are NSE-exclusive — worth the gating pain.

---

## 2. BSE — bseindia.com

> Generally **friendlier to scrape than NSE** — lighter protection. Use as the
> primary mirror for overlapping data; fall back to NSE only for NSE-only feeds.

| Data | Access | Notes |
|---|---|---|
| EOD bhavcopy | 🟢 | Often the easier mirror. |
| Announcements / filings, results (XBRL + PDF) | 🟢 | API-ish endpoints, lighter protection. |
| Corporate actions | 🟢 | |
| Shareholding pattern | 🟢 | |
| Insider trading | 🟢 | |
| Company master / scrip codes | 🟢 | |

---

## 3. MCA — Ministry of Corporate Affairs (mca.gov.in)

| Data | Access | Notes |
|---|---|---|
| Company master data (basic) | 🟢 | Free lookup. |
| AOC-4 / MGT-7 financials (XBRL) | 🔴 | Login + **pay-per-document** + captcha. Not bulk-scrapable. |

**Verdict:** effectively **out of scope**. Exchange XBRL filings substitute for
most fundamentals.

---

## 4. SEBI — sebi.gov.in

| Data | Access | Notes |
|---|---|---|
| FPI / FII flow statistics | 🟢 | |
| Orders, regulatory / settlement actions | 🟢 | Useful red-flag signal. |
| Circulars / disclosures | 🟢 | |

Mostly **clean public scraping**, no login.

---

## 5. Credit rating agencies (CRISIL / ICRA / CARE / India Ratings)

| Data | Access | Notes |
|---|---|---|
| Rating rationale PDFs | 🟢 | Public on each agency site; **fragmented** across 4 layouts. |

**Easier path:** pull via the exchange "credit rating" announcement filings
(single normalized entry point) rather than scraping each agency.

---

## 6. Macro — RBI (rbi.org.in) / MOSPI (mospi.gov.in)

| Data | Access | Notes |
|---|---|---|
| RBI DBIE — repo rate, inflation, forex, sectoral credit | 🟢 | Downloads + clunky query forms. |
| MOSPI — GDP, IIP, CPI / WPI | 🟢 | Public downloads. |

Clean, no login. **Secondary priority** for stock picking (macro overlay).

---

## 7. Company investor-relations sites & official channels

| Data | Access | Notes |
|---|---|---|
| Annual reports, concall transcripts, investor PPTs | 🟢 | Also filed on BSE/NSE → prefer exchange filings as unified source. |
| Concall audio / AGM video | 🟢 | Company's own official YouTube channel. |

Use exchange filings as the **single normalized entry point**; go to company
sites only for gaps.

---

## 8. AMFI — Association of Mutual Funds in India (amfiindia.com)

The primary, authoritative source for Indian mutual-fund NAVs. Plain-HTTP text, **no
browser tier** (the `www` host 302-redirects to `portal.amfiindia.com`). Backbone of the
mutual-fund module (`scrapers/amfi.py`).

| Data | Access | Notes |
|---|---|---|
| Daily NAV — all schemes (`/spages/NAVAll.txt`) | 🟢 | Semicolon-delimited, grouped by category then AMC; ~14k schemes/day (code, ISINs, name, NAV, date). Universe + daily NAV point. |
| Historical NAV (`DownloadNAVHistoryReport_Po.aspx?frmdt=&todt=&mf=`) | 🟢 | Per-AMC over a date range; **caps the range** (wide ranges return empty) → fetch in ~180-day windows. `mf` = AMFI numeric AMC code. |
| AMC name → numeric code map | 🟢 | 55 active AMCs from the disclosure page's `RssNAV.aspx?mf=N` links; resolved to names via the history report header. Needed to backfill a fund by name (`mf_amc`). |
| Scheme TER / expense ratio | 🟢 | Monthly AMFI disclosure (for fund-report enrichment — later). |
| Monthly scheme portfolio holdings | 🟡 | **Not** consolidated on AMFI — SEBI-mandated but published **per-AMC** as XLSX. A **generic SEBI-format parser** (header-detecting) + a per-AMC fetcher registry (`scrapers/mf_holdings.py`); coverage grows one AMC at a time. Live: **PPFAS** (one workbook, sheet/scheme) · **HDFC** (direct-CDN file per scheme, listed on a JS page) · **Nippon India** (one consolidated workbook; its file server 503s a plain client, so the link is captured and the file is fetched **in-browser** — `_browser_pick_and_fetch` — which carries the tab's anti-bot clearance). Scheme sheets map to AMFI Direct-Growth `scheme_code` via `ingest._match_scheme`. |

*MF-holding-per-stock note:* NSE's SHP **summary**
(`/api/NextApi/apiClient/GetQuoteApi?functionName=getShareholdingPattern&symbol=X`) only
exposes promoter-vs-public (+ an `ndsid`), **not** the MF/FII institutional breakdown — so
per-stock MF ownership is derived from the Phase-3 monthly holdings, not the SHP summary.

---

## 9. PIB — Press Information Bureau (pib.gov.in)

The government's **official press-release channel** — where ministries announce schemes / policies /
reforms / allocations, often at the cabinet-approved / **draft** / consultation stage *before* formal
launch. Primary and government-backed (fits the primary-only rule), plain HTTP (`scrapers/pib.py`).

| Data | Access | Notes |
|---|---|---|
| Recent releases (English) | 🟢 | RSS `RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3` — ~20 latest, title + `PRID` link. |
| Full release text | 🟢 | `PressReleaseIframePage.aspx?PRID=<id>` — ministry, headline, date, body. |

Powers the **policy radar** screen (`analysis/policy.py`, email `screen: policy`): keyword-gate the
bodies → LLM classifies the economic ones into sector + mechanism + likely listed beneficiaries →
resolved to NSE symbols against `equity_master`/`sector_map`, watchlist names flagged. **No
news-portal / social-media rumor** — that would break the primary-only rule.

## 10. Global supply-shock sources — the 💨 Tailwind Scout (`scrapers/social.py`, `scrapers/fedregister.py`)

> **Analysis/LLM-grounded layer, NOT the primary DB.** These feed the Tailwind radar (global export
> bans / quotas / tariffs / crop failures → Indian beneficiaries). Unlike everything above, this is
> *news/signal* input, not government-primary data landed in DuckDB — so every catalyst it produces
> **carries its source link** and every company is verified against `equity_master` downstream. It's a
> lead generator to check, not authoritative data. See [`REPORTS.md`](REPORTS.md) → 💨 Tailwind.

| Data | Access | Notes |
|---|---|---|
| **Google News RSS** (global breadth) | 🟢 | `news.google.com/rss/search?q=<query>+when:<N>d` — recency-scoped, plain HTTP, no auth. The workhorse: catches China/EU/DRC/global commodity & policy moves and government/ministry announcements for any targeted query. Fanned over the `_CHOKEPOINTS` catalog + generic probes. |
| **US Federal Register** (official US) | 🟢 | `federalregister.gov/api/v1/documents.json` — free JSON, no auth; RULE + **PRORULE (proposed/upcoming rules)** + NOTICE, date/agency filtered. The authoritative **US leg** and a forward look at rules *before* they're news. **US-only** (does not see China/EU) — complements Google News, doesn't replace it. |
| **Reddit** (early speculation) | 🟡→🔴 | `old.reddit.com/search.json` — now 403s unauthenticated from datacenter IPs; a per-process circuit-breaker skips it fast. Best-effort bonus; News carries the load. |
| **Twitter / X** (fastest chatter) | 🔴 | No usable no-auth API; tried via nitter RSS mirrors (`xcancel.com`, `nitter.poast.org`) which are largely down → returns nothing gracefully (circuit-breaker). A paid X API key would make it reliable (deferred). |

## Practical takeaways for the scraping plan

1. **BSE is the friendlier primary** for fundamentals/filings/actions; **NSE for
   NSE-only** data (delivery %, FII derivatives, option chain).
2. **The whole game is NSE's anti-bot session handling** — validate with
   `scrapling` *first*, before building anything on top.
3. **MCA is out** (login + paid + captcha); exchange XBRL filings substitute.
4. **No login needed** anywhere we actually plan to use — it's session/cookie/
   header friction and rate limits, not authentication walls.
5. **PDF parsing** (annual reports, transcripts, rating rationales) is a
   separate, heavier workstream from JSON/CSV scraping — and where the LLM earns
   its keep.

## Scraping-difficulty order (de-risk easiest → hardest)

1. 🟢 BSE bhavcopy / filings, SEBI, RBI/MOSPI, rating PDFs.
2. 🟡 NSE session-gated JSON (quotes, filings, corporate actions, shareholding).
3. 🟡 NSE F&O / option chain (most bot-protected) — confirm feasibility last.
