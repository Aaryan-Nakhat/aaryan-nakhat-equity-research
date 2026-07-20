"""Plain-English interpretation for the numbers in reports and alerts.

Every metric we surface should be self-explaining: *what it is*, *typical /
benchmark values*, and *how to read the current value* — with a sector caveat
where the "normal" range shifts by business model. This module is the
deterministic backbone (consistent, no LLM); the Gemini narrative then adds the
company-specific, sector-aware reading on top.

- ``read(key, value)``    -> short inline tag, e.g. "9.5% — fair"
- ``guide_markdown()``    -> the standalone "Metrics & ratings guide" (metrics +
  the categorical outputs: verdict ratings, P/E n/a reasons, event types)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metric:
    what: str                                    # one-line definition
    typical: str                                 # benchmark / typical values
    bands: list[tuple[float, str]] = field(default_factory=list)  # (threshold, label), high→low
    lower_better: bool = False                   # bands read as "below threshold is better"
    sector_note: str = ""                        # how the normal range shifts by sector


# Headline metrics we judge. Keys match how callers label values.
GLOSSARY: dict[str, Metric] = {
    "P/E": Metric(
        "Price ÷ trailing earnings — rupees paid per ₹1 of annual profit.",
        "Broad market ~20–25x; <15x cheap, >40x demands high growth. Most useful vs the company's OWN history and its sector.",
        [(40, "rich"), (25, "full"), (15, "moderate")], lower_better=True,
        sector_note="Structurally higher for fast-growers (FMCG, consumer); lower for cyclicals/commodities."),
    "P/B": Metric(
        "Price ÷ book value (net worth) per share.",
        "<1x = below book; 1–3x typical; >5x means heavy intangible/brand or very high returns.",
        [(5, "rich"), (3, "full"), (1, "moderate")], lower_better=True,
        sector_note="Banks/financials judged largely on P/B; asset-light businesses run high P/B legitimately."),
    "EV/EBITDA": Metric(
        "Enterprise value (market cap + net debt) ÷ EBITDA — capital-structure-neutral, so it "
        "compares firms with different debt and is the standard lens for cyclical/asset-heavy names "
        "where P/E swings with the cycle.",
        "~8–12x typical; <6x cheap, >15x rich. Judge cyclicals on MID-CYCLE EBITDA, not peak/trough.",
        [(15, "rich"), (12, "full"), (8, "moderate")], lower_better=True,
        sector_note="Lower for slow/cyclical (metals, oil & gas, utilities); higher for structural "
        "growth. Not meaningful for banks/financials."),
    "ROE%": Metric(
        "Return on equity — net profit ÷ shareholders' equity.",
        "≥18% strong · 12–18% decent · <10% weak.",
        [(18, "strong"), (12, "decent"), (8, "fair")],
        sector_note="Leverage flatters ROE — pair with ROCE; banks/NBFCs run high ROE on leverage."),
    "ROCE%": Metric(
        "Return on capital employed — EBIT ÷ (equity + debt); efficiency of ALL capital.",
        "≥20% excellent · 15–20% good · 10–15% fair · <10% weak.",
        [(20, "excellent"), (15, "good"), (10, "fair")],
        sector_note="Capital-heavy sectors (utilities, telecom, oil & gas, infra) structurally lower — judge vs peers, not the absolute bar."),
    "ROIC%": Metric(
        "Return on invested capital — after-tax EBIT ÷ (equity + debt − cash).",
        "Compare to WACC: ROIC > WACC = value-creating; below = destroying.",
        [(20, "excellent"), (15, "good"), (10, "fair")],
        sector_note="The cleanest cross-sector quality gauge; the spread over WACC is what matters."),
    "NetMargin%": Metric(
        "Net profit ÷ revenue — rupees of profit per ₹100 of sales.",
        "Varies hugely by industry; trend and stability matter more than the absolute level.",
        [(20, "high"), (10, "healthy"), (4, "thin")],
        sector_note="Software/branded consumer run 15–25%; retail/distribution/commodities run low-single-digit by design."),
    "EBITDA margin%": Metric(
        "Operating profit before interest/tax/depreciation ÷ revenue.",
        "Sector-bound; watch the trend — falling margins signal pricing/cost pressure.",
        [(25, "high"), (15, "healthy"), (8, "thin")]),
    "D/E": Metric(
        "Total debt ÷ equity — leverage.",
        "<0.5 conservative · 0.5–1 moderate · 1–2 high · >2 stretched.",
        [(2, "stretched"), (1, "high"), (0.5, "moderate")], lower_better=True,
        sector_note="Capital-intensive/financial businesses carry more debt normally; net-debt and interest cover matter more than the raw ratio."),
    "Net debt/EBITDA": Metric(
        "Net debt ÷ EBITDA — years of operating profit to clear net debt.",
        "<1x comfortable · 1–3x manageable · >3x stretched · >4–5x risky.",
        [(4, "risky"), (3, "stretched"), (1, "manageable")], lower_better=True),
    "Interest cover": Metric(
        "EBIT ÷ interest cost — how many times profit covers interest.",
        "≥6x comfortable · 3–6x adequate · <3x stressed · <1.5x danger.",
        [(6, "comfortable"), (3, "adequate"), (1.5, "stressed")]),
    "Current ratio": Metric(
        "Current assets ÷ current liabilities — short-term liquidity.",
        "~1.5–2x healthy; <1 means short-term obligations exceed liquid assets.",
        [(2, "strong"), (1.2, "adequate"), (1, "tight")]),
    "CCC (days)": Metric(
        "Cash conversion cycle = receivable + inventory − payable days; cash tied up in operations.",
        "Lower is better; negative (paid by suppliers before collecting) is excellent.",
        [(120, "heavy"), (60, "moderate"), (0, "lean")], lower_better=True,
        sector_note="Retail/consumer often run low or negative; capital-goods/EPC run long cycles normally."),
    "CFO/PAT": Metric(
        "Operating cash flow ÷ net profit — is reported profit backed by cash?",
        "≥1x healthy over time; persistently <1 is an earnings-quality red flag.",
        [(1.0, "cash-backed"), (0.8, "watch")],
        sector_note="Lumpy for project/working-capital-heavy businesses — read the 3/5-yr rolled figure, not one year."),
    "CFO/EBITDA%": Metric(
        "Operating cash flow ÷ EBITDA — operating-profit-to-cash conversion.",
        "≥70% healthy; low conversion points to working-capital drag.",
        [(80, "strong"), (60, "fair")]),
    "Sloan accruals%": Metric(
        "Balance-sheet accruals ÷ avg assets — the non-cash part of earnings.",
        "Near zero/negative is good; high positive (>10%) = profit not cash-backed (low-accrual firms historically outperform).",
        [(10, "aggressive"), (5, "elevated")], lower_better=True),
    "Altman Z": Metric(
        "Bankruptcy-distance score from 5 ratios.",
        ">2.99 safe · 1.81–2.99 grey · <1.81 distress.",
        [(2.99, "safe"), (1.81, "grey")],
        sector_note="Calibrated for manufacturers; asset-light/financial firms can score oddly — treat as one input."),
    "Piotroski F": Metric(
        "9-point fundamental-strength checklist (profitability, leverage, efficiency).",
        "8–9 strong · 4–7 middling · 0–2 weak.",
        [(8, "strong"), (4, "middling")]),
    "Beneish M": Metric(
        "8-variable earnings-manipulation score.",
        "> −1.78 flags possible manipulation; more negative = cleaner.",
        [(-1.78, "flag")], lower_better=True,
        sector_note="A statistical screen, not proof — corroborate with accruals, receivables and cash conversion."),
    "Benford MAD": Metric(
        "Deviation of reported figures' leading digits from Benford's law.",
        "<0.006 close · 0.006–0.012 acceptable · 0.012–0.015 marginal · >0.015 nonconformity.",
        [(0.015, "nonconformity"), (0.012, "marginal"), (0.006, "acceptable")], lower_better=True,
        sector_note="A rounding/manipulation tell on the whole filing history — a flag to dig, not a verdict."),
    "Margin of safety%": Metric(
        "Discount of price to the DCF median fair value.",
        ">30% large cushion · 10–30% some · <0% trading above fair value.",
        [(30, "large"), (10, "some"), (0, "slim")]),
    "P(undervalued)%": Metric(
        "Share of Monte-Carlo DCF runs where fair value exceeds the price.",
        ">70% odds favour value · ~50% a coin toss · <30% richly priced on DCF.",
        [(70, "favourable"), (50, "balanced"), (30, "unfavourable")]),
    "Implied growth%": Metric(
        "Revenue growth the current price implies (reverse DCF).",
        "Compare to history: at/below past growth = undemanding; well above = priced for acceleration.",
        []),
    "Multiple percentile (own history)": Metric(
        "Where today's multiple sits within the stock's OWN multi-year range (0–100th percentile).",
        "<35th = cheap vs its own history · ~50th = mid-range · >65th = rich vs its own history. "
        "A timing gauge, not a quality one.",
        []),
    "Forward multiple (guidance)": Metric(
        "A multiple (EV/EBITDA, P/E or P/S) on MANAGEMENT'S OWN forward guidance for a future fiscal "
        "year, not trailing numbers.",
        "Cheaper than the trailing multiple when growth is expected; only as reliable as the guidance — "
        "shown only when management gave explicit figures.",
        []),
    "Promoter holding%": Metric(
        "Share of equity held by promoters (founders/controlling group).",
        "Higher promoter skin-in-the-game is generally reassuring; very low can mean dispersed control.",
        [(50, "high"), (26, "meaningful")]),
    "Pledge%": Metric(
        "Promoter shares pledged as collateral, as % of promoter holding.",
        "0% ideal · <10% minor · 10–25% watch · >50% serious red flag (forced-sale / leverage risk).",
        [(50, "severe"), (25, "elevated"), (10, "minor")], lower_better=True),
    "Sector z-score": Metric(
        "How many standard deviations a ratio sits from the peer mean.",
        "|z|<1 in line with peers · 1–2 notable · >2 an outlier (good or bad depending on the ratio).",
        []),
}


def _band(m: Metric, value: float) -> str | None:
    if not m.bands or value is None or value != value:
        return None
    if m.lower_better:
        for thr, label in m.bands:          # bands high→low; first threshold exceeded = worst
            if value >= thr:
                return label
        return "good"
    for thr, label in m.bands:
        if value >= thr:
            return label
    return "weak"


def read(key: str, value: float | None, *, nd: int = 1, pct: bool = False,
         suffix: str = "") -> str:
    """Concise inline tag for a value, e.g. ``"9.5% — fair"``. Empty if unknown."""
    if value is None or value != value:
        return ""
    m = GLOSSARY.get(key)
    txt = f"{value:,.{nd}f}{'%' if pct else ''}{suffix}"
    label = _band(m, value) if m else None
    return f"{txt} — {label}" if label else txt


def label(key: str, value: float | None) -> str:
    """Just the band word (e.g. ``"fair"``), or ""."""
    m = GLOSSARY.get(key)
    return (_band(m, value) or "") if (m and value is not None) else ""


# ---- Categorical outputs (fixed value sets the reports / digest can produce) ----

# The deep report's one-line call — graded most-positive → most-negative.
VERDICTS: list[tuple[str, str]] = [
    ("Buy", "Strongest positive — start or add a position now; looks undervalued with a margin of safety."),
    ("Accumulate", "Moderately positive — build gradually / on dips; a good business but not a deep bargain (often already priced for growth)."),
    ("Hold", "Neutral — keep it if you own it, but not a fresh-buy case; roughly fairly valued."),
    ("Reduce", "Moderately negative — trim the position; deteriorating fundamentals or a stretched valuation, but not a full exit."),
    ("Avoid", "Strongest negative — don't buy / exit; overvalued, risky, or carrying red flags. (There is no separate 'Sell' — Avoid is the floor of the scale.)"),
]

# Why a Movers P/E is shown as "n/a (...)" instead of a misleading number.
PE_NA_REASONS: list[tuple[str, str]] = [
    ("loss-making", "Negative trailing-twelve-month earnings — a P/E is undefined."),
    ("earnings distorted, profit > sales", "Net profit exceeds revenue — an artifact (e.g. a demerger or one-off gain), so the P/E is meaningless."),
    ("negative net worth", "Accumulated losses exceed equity (negative book value) — earnings and P/E aren't meaningful."),
]

# How each watchlist filing is tagged in the daily digest's Events section.
EVENT_TYPES: list[tuple[str, str]] = [
    ("Results filed", "Quarterly or annual financial results."),
    ("Concall / investor meet", "Earnings call, analyst/investor meet, transcript or presentation."),
    ("Dividend", "Dividend declaration."),
    ("Stock split", "Sub-division of shares (lower face value)."),
    ("Bonus issue", "Free additional shares issued to existing holders."),
    ("Rights issue", "Discounted new shares offered pro-rata to existing holders."),
    ("QIP / fund raising", "Fresh capital raised from institutional investors."),
    ("Preferential issue", "Shares/warrants issued to select investors."),
    ("Scheme / M&A", "Merger, demerger, amalgamation or scheme of arrangement."),
    ("Open offer / SAST", "Substantial-acquisition / takeover open offer."),
    ("Buyback", "Company repurchasing its own shares."),
    ("Acquisition / disposal", "Buying or selling a business or stake."),
    ("Order / contract win", "New order or contract bagged."),
    ("Credit rating update", "Rating action by a rating agency."),
    ("Promoter pledge / charge", "Promoter shares pledged, or a charge created/satisfied."),
    ("Insider-trading disclosure", "SEBI PIT (insider-trading) disclosure."),
    ("Director / KMP change", "Board / key-management appointment, resignation or auditor change."),
    ("Shareholder meeting", "AGM / EGM, postal ballot, or its proceedings."),
    ("Board meeting", "Intimation or outcome of a board meeting."),
    ("Delisting", "Proposal to delist the shares."),
    ("Announcement", "Any other material disclosure (catch-all)."),
]


def guide_markdown() -> str:
    """The full guide as a standalone Markdown document — every metric (what it is,
    typical values, how to read it) plus the categorical outputs (verdict ratings,
    P/E n/a reasons, corporate-event types) and their possible values."""
    lines = ["# Metrics & ratings guide",
             "_What each metric and rating in the reports means, its typical values or "
             "possible categories, and how to read it — judge sector-relative where noted._\n",
             "## Financial & forensic metrics"]
    for k, m in GLOSSARY.items():
        note = f" _Sector:_ {m.sector_note}" if m.sector_note else ""
        lines.append(f"- **{k}** — {m.what} _Typical:_ {m.typical}{note}")
    lines.append("\n_The inline band words (e.g. safe / strong / rich / elevated) are just the "
                 "current value placed into each metric's thresholds listed above._")

    lines.append("\n## Verdict — the report's call")
    lines.append("_The deep report ends with exactly one of these five ratings — a graded "
                 "judgment (most positive → most negative), grounded in the quant brief and the "
                 "filings the model read:_")
    lines += [f"- **{v}** — {meaning}" for v, meaning in VERDICTS]

    lines.append("\n## Why a Movers P/E shows 'n/a'")
    lines.append("_In the daily digest, a P/E is dropped (with the reason) rather than printed "
                 "when it would be misleading:_")
    lines += [f"- **{r}** — {meaning}" for r, meaning in PE_NA_REASONS]

    lines.append("\n## Corporate-event types (daily digest)")
    lines.append("_How each watchlist filing is tagged. Emoji: 🟢 positive · 🔴 negative · "
                 "⚠️ caution · 📄 results filing · 🔔 neutral/informational._")
    lines += [f"- **{e}** — {meaning}" for e, meaning in EVENT_TYPES]
    return "\n".join(lines)


_GUIDE_PDF: bytes | None = None


def guide_pdf() -> bytes:
    """The guide as a constant PDF (built once, cached — it never changes)."""
    global _GUIDE_PDF
    if _GUIDE_PDF is None:
        from equity_research.reports.pdf import report_to_pdf
        _GUIDE_PDF = report_to_pdf(guide_markdown(), "Metrics & ratings guide")
    return _GUIDE_PDF


# --- Mutual-fund report metrics (separate guide; the fund report uses different numbers) ---
FUND_METRICS: list[tuple[str, str]] = [
    ("CAGR (compound annual growth rate)",
     "The annualised return — what the fund compounded at per year, on average, over the "
     "horizon. For periods under a year we show the plain (non-annualised) return instead. "
     "Only compare funds of the *same category and horizon*."),
    ("Sharpe ratio",
     "Return earned per unit of total risk (volatility), above a ~6.5% risk-free rate. Higher "
     "is better — >1 is good, >2 excellent. Lets you fairly compare a steady fund vs a jumpy one."),
    ("Sortino ratio",
     "Like Sharpe, but penalises only *downside* volatility (ignores upside swings) — often a "
     "fairer risk-adjusted read. Higher is better."),
    ("Annualised volatility",
     "How much the NAV swings, per year. Higher = a rougher ride. Equity funds are typically ~11–18%."),
    ("Max drawdown",
     "The worst peak-to-trough fall in NAV over the period — the deepest loss you'd have sat "
     "through. Closer to 0 is better."),
    ("Rolling 1-year returns (worst / median / best)",
     "Every possible 1-year holding period across the fund's history, summarised. The **median** "
     "is a typical year; the **worst** shows how bad one year got. A more honest consistency read "
     "than a single point-to-point number."),
    ("Category percentile / rank",
     "Where the fund's return sits among its same-category Direct-Growth peers. 'Top 10%' = it beat "
     "90% of peers over that horizon. (Shows once enough peer history is on file.)"),
    ("Top-10 concentration",
     "Share of NAV in the 10 largest holdings. Higher = more concentrated — higher conviction, but "
     "more single-stock risk."),
    ("Watchlist overlap",
     "How many of *your* tracked stocks the fund holds and their combined weight — so you can see "
     "whether a fund is actually buying the names you follow."),
]


def fund_guide_markdown() -> str:
    """Plain-English guide to every number in the mutual-fund report."""
    lines = ["# Mutual-fund metrics guide",
             "_What each number in the fund report means, and how to read it._\n",
             "## Returns, risk & portfolio"]
    lines += [f"- **{k}** — {v}" for k, v in FUND_METRICS]
    lines.append("\n_Source: NAV & returns from **AMFI** (the official industry body); holdings from "
                 "each AMC's **SEBI-mandated monthly portfolio disclosure**. Both are primary sources._")
    return "\n".join(lines)


_FUND_GUIDE_PDF: bytes | None = None


def fund_guide_pdf() -> bytes:
    """The fund-metrics guide as a constant PDF (built once, cached)."""
    global _FUND_GUIDE_PDF
    if _FUND_GUIDE_PDF is None:
        from equity_research.reports.pdf import report_to_pdf
        _FUND_GUIDE_PDF = report_to_pdf(fund_guide_markdown(), "Mutual-fund metrics guide")
    return _FUND_GUIDE_PDF


# IPO jargon, grouped as the pre-listing note presents it. A first-time applicant should
# be able to read the note end-to-end with only this sheet beside them.
IPO_METRICS: list[tuple[str, list[tuple[str, str]]]] = [
    ("The offer — how the issue is structured", [
        ("Fresh Issue",
         "**New** shares created by the company; the money goes **to the company** — for capex, "
         "debt repayment or working capital. Generally the **positive** half of an issue: the "
         "business actually gets funded."),
        ("Offer for Sale (OFS)",
         "**Existing** shareholders (promoters, PE/VC funds) selling their own shares; the money "
         "goes **to them, not the company**. A large OFS deserves caution — ask *who* is exiting "
         "and *why now*. A full PE exit reads very differently from a small promoter trim."),
        ("Price band",
         "The floor–cap range you may bid in (e.g. ₹402–₹424). Valuation is judged at the **upper "
         "band**, since that's where a well-demanded issue prices."),
        ("Lot size / minimum application",
         "The smallest number of shares you can apply for, and the rupee cost at the upper band — "
         "the minimum cheque a retail investor must write."),
        ("Promoter holding (pre → post)",
         "How much the founders own before and after the issue. A steep drop, or a low post-issue "
         "stake, means less 'skin in the game'."),
        ("Objects of the issue (use of proceeds)",
         "What the fresh-issue money will actually fund. **Concrete** uses (a named capex project, "
         "debt reduction) give far better visibility than a large 'general corporate purposes' bucket."),
    ]),
    ("Demand signals — who's actually buying", [
        ("Anchor investors",
         "Large institutions allotted shares the day **before** the issue opens, at a fixed price, "
         "with a lock-in. Marquee anchors (well-known mutual funds) are a genuine confidence signal."),
        ("QIB — Qualified Institutional Buyers",
         "Mutual funds, insurers, banks, FPIs — the professional 'smart money' that does deep "
         "diligence. **Weak QIB subscription is the single most telling warning sign** in an issue, "
         "even when retail demand looks frenzied."),
        ("NII / HNI — Non-Institutional Investors",
         "Applications above ₹2 lakh (wealthy individuals, corporates). Often leverage-funded and "
         "chasing listing gains, so high NII demand is momentum, not necessarily conviction."),
        ("RII — Retail Individual Investors",
         "Applications up to ₹2 lakh. Sentiment-driven; strong retail demand alone doesn't validate "
         "an issue's quality."),
        ("Subscription (x times)",
         "How many times each category was bid for versus shares reserved. **Read the categories "
         "separately** — a big headline number can hide a near-empty QIB book."),
        ("Grey Market Premium (GMP)",
         "An **unofficial**, unregulated off-market price rumour. It is *not* a primary source and "
         "is deliberately **excluded** from these reports — official subscription and anchor data "
         "tell the same story with far more reliability."),
    ]),
    ("Valuation at the band", [
        ("P/E at the upper band",
         "Offer price ÷ latest-year earnings per share — rupees paid for ₹1 of annual profit. "
         "Compare it to the **listed peers** in the price-band ad, not in isolation."),
        ("P/B and NAV per share",
         "NAV (net asset value) is book value per share; P/B is the offer price ÷ NAV — rupees paid "
         "for ₹1 of net worth. A high P/B needs a high RoNW to justify it."),
        ("RoNW — Return on Net Worth",
         "Profit ÷ shareholders' funds; the company's return on its own capital. Higher is better, "
         "and a RoNW well above peers can justify paying a higher multiple."),
        ("EPS (diluted)",
         "Profit per share after counting all potentially convertible instruments — the conservative "
         "earnings figure the P/E is built on."),
    ]),
    ("Financial & risk terms used in the note", [
        ("EBITDA / EBITDA margin",
         "Operating profit before interest, tax, depreciation and amortisation, and that as a % of "
         "revenue — the cleanest read on core operating profitability and its trend."),
        ("PAT and CFO",
         "Profit After Tax versus **Cash Flow from Operations**. CFO consistently tracking (or "
         "exceeding) PAT means the profit is real cash; persistently negative CFO alongside rising "
         "profit is a serious quality flag."),
        ("D/E and DSCR",
         "Debt-to-Equity measures leverage (≈1x+ is high for most sectors). The Debt Service Coverage "
         "Ratio shows how comfortably operating cash covers debt repayments — near 1.0x is tight."),
        ("Contingent liabilities",
         "Potential obligations that don't sit on the balance sheet (guarantees, disputed tax, "
         "litigation). Judge them **as a % of net worth** — a large ratio can wipe out equity if they "
         "crystallise."),
        ("Related-party transactions (RPTs)",
         "Business done with promoter-owned entities. Not wrong in itself, but material RPTs — or "
         "promoters personally guaranteeing company debt — warrant scrutiny of pricing and governance."),
        ("Customer concentration",
         "Share of revenue from the largest few clients. Very high concentration (say >50% from the "
         "top three) makes revenue fragile: losing one contract can reset the business."),
    ]),
    ("Sources & the verdict", [
        ("RHP — Red Herring Prospectus",
         "The company's own SEBI-filed offer document (often 400–600 pages): restated multi-year "
         "financials, risk factors, objects of the issue, promoter and litigation detail. **The "
         "primary source** for everything in the financials, risks and use-of-proceeds sections."),
        ("Price-band advertisement",
         "The mandatory newspaper ad carrying the KPI table — EPS, NAV, RoNW, P/E at the band — and "
         "the **listed-peer comparison**. Source of the valuation section."),
        ("APPLY / NEUTRAL / AVOID",
         "The verdict scale. **APPLY** — quality, valuation and demand line up. **NEUTRAL** — a real "
         "business at a fair price, but with risks or weak institutional demand; suitable only for a "
         "considered/speculative punt. **AVOID** — the risks, pricing or demand signals outweigh the "
         "story. It always states *who* the issue suits (listing-gain punt vs. long-term hold)."),
    ]),
]


def ipo_guide_markdown() -> str:
    """Plain-English guide to the IPO jargon used in the pre-listing note."""
    lines = ["# IPO metrics & terminology guide",
             "_What each term in the IPO analysis means, and how to read it — so the note stands "
             "on its own even if this is your first application._\n"]
    for section, items in IPO_METRICS:
        lines.append(f"## {section}")
        lines += [f"- **{k}** — {v}" for k, v in items]
        lines.append("")
    lines.append("_Source: the issuer's **RHP**, the **price-band advertisement** and the **anchor "
                 "allotment**, all filed with SEBI and published via NSE, plus NSE's official "
                 "subscription data. No grey-market or unofficial inputs are used._")
    return "\n".join(lines)


_IPO_GUIDE_PDF: bytes | None = None


def ipo_guide_pdf() -> bytes:
    """The IPO-metrics guide as a constant PDF (built once, cached)."""
    global _IPO_GUIDE_PDF
    if _IPO_GUIDE_PDF is None:
        from equity_research.reports.pdf import report_to_pdf
        _IPO_GUIDE_PDF = report_to_pdf(ipo_guide_markdown(), "IPO metrics & terminology guide")
    return _IPO_GUIDE_PDF
