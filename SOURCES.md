# Data sources, attribution & terms of use

This project is **open-source software for personal, self-hosted, non-commercial
research**. It ships **no market data** — it only *fetches* data, on your own
machine, from the public sources below when **you** run it.

> **You are responsible for your own use.** Running this software means *you*
> access these sources, under *their* terms — not the authors. Each provider's
> Terms of Use govern what you may do with its data. Several (the exchanges in
> particular) **restrict automated access and prohibit redistribution.** Review
> them before you run this, and do not re-publish, resell, or re-serve any data
> you fetch. Nothing here is investment advice (see the in-app disclaimer).

## Please respect these boundaries

- **Do not redistribute** data you fetch (no public dumps of prices, NAVs,
  filings, holdings; no hosted API that re-serves exchange values to others).
- **Do not run this as a hosted service** that scrapes exchanges on behalf of
  many users — that shifts the terms-of-use exposure onto you, at scale. This
  tool is designed to be **self-hosted for one user's own research.**
- **Throttle and cache** — the defaults are gentle; keep them that way. Don't
  hammer any endpoint.
- **Attribute** the sources that ask for it (AMFI, PIB, government data).

## Sources

| Source | What it provides | Access | Terms — key points |
|---|---|---|---|
| **NSE** (`nseindia.com`, `nsearchives.nseindia.com`, `nseix.com`) | Filings, announcements, FII/DII, bulk/block deals, index & F&O data, GIFT Nifty | Plain-HTTP archives + a **browser tier** (opt-in) | Terms restrict automated access and **prohibit redistribution**. The browser tier is **OFF by default** — see *NSE opt-in* below |
| **BSE** (`bseindia.com`, `api.bseindia.com`) | Per-scrip quotes/headers, BSE-only names | Plain HTTP (undocumented API) | Redistribution restricted; API is not an official public product |
| **Yahoo Finance** (`query1.finance.yahoo.com`) | Global indices / cross-checks | JSON endpoint (unofficial) | Yahoo's terms prohibit commercial use and redistribution of its feeds |
| **MCX** (`mcxindia.com`) | Commodity reference prices | Browser tier | Exchange data; redistribution restricted |
| **FBIL** (`fbil.org.in`) | USD/INR reference rate | Plain HTTP | Benchmark administrator; rate redistribution may be restricted |
| **Moneycontrol** | Supplementary market context | Plain HTTP | Site terms prohibit scraping — used sparingly / best-effort only |
| **Fund AMC sites** (PPFAS, HDFC, Nippon) | Monthly scheme portfolios | Site XHRs | Portfolios are **SEBI-mandated public disclosures**; individual site terms may still restrict scraping |
| **AMFI** (`amfiindia.com`) | ~14,500 daily mutual-fund NAVs (`NAVAll.txt`), scheme master | Public download | Published free for public use — **attribute AMFI**; do not redistribute as your own dataset |
| **PIB** (`pib.gov.in`) | Government press releases (policy radar) | Plain HTTP | Government of India content — generally free to use **with attribution** |
| **US Federal Register** (`federalregister.gov`) | US rules/notices (Tailwind engine) | Official API | **Public domain** (US Government work) |
| **Google News RSS** (`news.google.com`) | Headlines / links | RSS | For headlines and linking; do **not** republish full article text |
| **Reddit / X mirrors** (`reddit.com`, nitter mirrors) | Best-effort social signal | Plain HTTP | Reddit restricts scraping; mirrors are unofficial. Off / degraded by default |
| **XBRL** (`xbrl.org`) | Taxonomy / spec for parsing filings | Plain HTTP | Open standard |

## NSE opt-in (`NSE_SCRAPING_ENABLED`)

The NSE `/api` endpoints sit behind bot protection. To read them the tool loads
a real page to clear the challenge — i.e. automated access NSE's terms restrict.
Because of that, this tier is **disabled by default**:

```env
# .env — set to true ONLY if you have reviewed NSE's Terms of Use and accept
# responsibility for complying with them (no redistribution of NSE data).
NSE_SCRAPING_ENABLED=false
```

With it left `false`, NSE `/api` calls raise `NseScrapingDisabled` and the
affected features degrade gracefully (empty result) rather than scraping. The
plain-HTTP NSE **archive files** (bhavcopy, index closes, participant OI) are the
preferred, lighter path and are used wherever a file equivalent exists.

## Disclaimer

Automated research for personal & educational use only — **not investment
advice**, a recommendation, or an offer to buy or sell any security. Data may be
delayed, incomplete, or inaccurate, and AI-generated commentary can contain
errors. Verify independently and consult a SEBI-registered investment adviser
before acting. You alone are responsible for your investment decisions.
