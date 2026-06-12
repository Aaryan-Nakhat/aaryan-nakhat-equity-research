# Data Sources

> **Initial-thinking inventory** of primary/official Indian equity data sources
> and their scrapability. This was a *first pass from reasoning*.
>
> ⚠️ **Partially validated (2026-06-13).** Key sources have now been probed with
> `scrapling` — see [`SCRAPING.md`](SCRAPING.md) for empirical results, which
> **supersede** the ratings below where they differ. Notably: NSE bhavcopy +
> delivery % is **plain-HTTP** via the archive host (easier than the 🟡 below),
> while NSE `/api/quote-equity` is currently **WAF-blocked** even in a browser.

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
| Shareholding pattern | 🟡 | Quarterly promoter/FII/DII/public split. |
| Insider trading (SAST / PIT disclosures) | 🟡 | JSON. |
| Bulk / block deals | 🟡 | |
| FII / DII daily cash activity | 🟡 | NSE-published. |
| F&O: OI, PCR, option chain, FII deriv stats | 🟡 | **Hardest** — heavily rate-limited / bot-protected. |
| ASM / GSM / surveillance, circuit limits | 🟡 | |
| Index constituents (Nifty 50/500, sector) | 🟡 | CSV. |

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

## Practical takeaways for the scraping plan

1. **BSE is the friendlier primary** for fundamentals/filings/actions; **NSE for
   NSE-only** data (delivery %, FII derivatives, option chain).
2. **The whole game is NSE's anti-bot session handling** — validate with
   `scrapling` *first*, before building anything on top.
3. **MCA is out** (login + paid + captcha); exchange XBRL filings substitute.
4. **No login needed** anywhere we actually plan to use — it's session/cookie/
   header friction and rate limits, not authentication walls.
5. **PDF parsing** (annual reports, transcripts, rating rationales) is a
   separate, heavier workstream from JSON/CSV scraping — and where Claude earns
   its keep.

## Scraping-difficulty order (de-risk easiest → hardest)

1. 🟢 BSE bhavcopy / filings, SEBI, RBI/MOSPI, rating PDFs.
2. 🟡 NSE session-gated JSON (quotes, filings, corporate actions, shareholding).
3. 🟡 NSE F&O / option chain (most bot-protected) — confirm feasibility last.
