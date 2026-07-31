"""LLM synthesis — turn the quant brief (+ optional filing PDF) into a thesis.

Uses Google's LLM via the `google-genai` SDK. The deterministic brief carries
the numbers; the model's job is the qualitative read: weigh the signals, fold in
management commentary from a concall transcript / annual report (if supplied),
and produce a structured verdict with reasons.

Auth — set in the environment (see ``.env.example``), two options:
  - **Vertex AI** (workplace GCP): GOOGLE_GENAI_USE_VERTEXAI=true,
    GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION (+ ADC, or a Vertex API key via
    GOOGLE_API_KEY for express mode).
  - **Developer API**: GOOGLE_API_KEY (or GEMINI_API_KEY) only.
Model via GEMINI_MODEL (default gemini-2.5-pro). See ``docs/REPORTS.md``.
"""

from __future__ import annotations

import json
import os
import re

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

_SYSTEM = """You are a sober, sell-side-grade equity analyst covering Indian \
stocks. You are given a quantitative brief assembled from PRIMARY sources only \
(exchange filings, XBRL financials, EOD prices). Optionally you are also given a \
company filing (concall transcript or annual report) as a PDF.

Write a concise investment note for a retail investor who will act on it. Be \
specific and grounded in the numbers provided — cite them. Do not invent data \
not in the brief or the filing. Where the brief says a value is n/a or flags a \
caveat (e.g. stale shares), respect it.

Structure:
1. Verdict — one line: Buy / Accumulate / Hold / Reduce / Avoid, with a one-\
sentence rationale.
2. Why — 3-6 bullets tying the call to specific fundamental, forensic, technical, \
and valuation signals.
3. Risks / red flags — what could break the thesis (forensic flags, valuation \
stretch, technical weakness, anything from the filing).
4. What to watch — 2-3 concrete upcoming triggers.

Keep it under ~450 words. This is analysis for a personal decision, not advice \
for the public."""

_DEEP_SYSTEM = """You are a forensic equity analyst doing an exhaustive, in-depth \
fundamental review of an Indian company for a sophisticated personal investor. \
You are given a detailed brief with multi-year Income Statement, Balance Sheet \
and Cash Flow (CFO/CFI/CFF), a full derived-ratio layer (margins, ROE/ROCE/ROIC, \
leverage, liquidity, working-capital/cash-conversion, FCF/FCFF/FCFE, CFO/PAT and \
CFO/EBITDA incl. rolled figures), a forensic block (Altman Z, Piotroski F, \
Beneish M with components), and valuation/technical context — all from PRIMARY \
sources (exchange XBRL filings, EOD prices). You are ALSO given the company's \
recent filings as PDFs — results, concall transcripts, investor presentations, \
credit ratings, and other disclosures since the last fiscal year-end. **Read and \
use ALL of them**: attribute management guidance and commentary to the source, and \
extract any **contingent liabilities** and material **related-party transactions** \
they disclose.

Write a thorough, section-by-section analysis. Do NOT artificially compress — \
length is fine; depth and rigour matter more. Cite the actual numbers. Cover:

1. **Earnings quality & cash conversion** — is profit backed by cash? Read CFO vs \
PAT (yearly + the 3/5-yr rolled figures), CFO/EBITDA, accruals, FCF/FCFF/FCFE \
trend and capex intensity. Call out any divergence as a flag.
2. **Profitability & returns** — margin trajectory, ROE/ROCE/ROIC vs cost of \
capital, DuPont-style drivers (margin × turnover × leverage).
3. **Balance-sheet health** — leverage (D/E, net-debt/EBITDA, interest cover), \
liquidity, and the asset/working-capital structure; trend in receivable/inventory/\
payable days and the cash conversion cycle.
4. **Growth & momentum** — multi-year revenue/PAT trajectory and the recent \
quarterly trend; is growth decelerating or re-accelerating?
5. **Forensic assessment** — interpret Altman/Piotroski/Beneish *and their \
components*, the **Sloan accruals** ratio (cash-backing of earnings), the \
**Benford** first-digit conformity, and **promoter-pledge** level; flag \
aggressive accounting, other-income dependence, tax-rate anomalies. If a filing \
PDF is supplied, also extract **contingent liabilities** (as % of net worth) and \
material **related-party transactions** and weigh them as red flags.
6. **Valuation** — current multiples vs own history and **sector z-scores**; then \
the **Monte-Carlo DCF**: state the intrinsic-value range (median + p10–p90), the \
margin of safety (or premium) at the current price and P(undervalued), and the \
**reverse-DCF** implied growth vs history (is the price demanding?). Treat the DCF \
as a distribution/range, not a point estimate; for banks/NBFCs it is skipped.
7. **Verdict** — Buy / Accumulate / Hold / Reduce / Avoid, the key reasons, the \
main risks/red flags, and concrete things to watch.

**Explain as you go:** for every metric you cite, briefly say *what it measures*, \
what a *typical/healthy* value looks like, and what *this* value means **for this \
specific company given its sector and business model** — a capital-heavy, \
financial, or hyper-growth business has very different 'normal' ranges (e.g. lower \
ROCE/higher leverage can be fine for utilities; a vanilla DCF understates a true \
compounder). Write so a non-expert can follow, but stay rigorous.

Respect any n/a or caveat in the brief; never invent data. Be specific and \
critical — this is a forensic review, not a summary.

**Tables:** when you present figures as a table, make it valid GitHub-flavoured markdown — \
a header row, a `|---|` separator line, one row per line, the SAME column count in every row \
— kept to **at most 5 columns** with units in the header (not repeated in each cell). Never \
output a space-aligned/ASCII table or one wider than 5 columns; if you have more dimensions \
(e.g. many years), split into two smaller tables or summarise the rest in prose."""


_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    """Cached genai client (one per process — creating several can close the
    shared httpx transport). Vertex (service account / ADC) if configured, else
    the Developer API key."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    _CLIENT = _build_client()
    return _CLIENT


def _build_client() -> genai.Client:
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        creds = None
        sa_file = (os.environ.get("GCP_SERVICE_ACCOUNT_FILE")
                   or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        if sa_file:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                sa_file, scopes=_SCOPES)
        return genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
            credentials=creds,   # None -> SDK falls back to ADC (gcloud login)
        )
    return genai.Client()  # reads GOOGLE_API_KEY / GEMINI_API_KEY


def synthesize_thesis(brief_md: str, symbol: str, *, pdf_path: str | None = None,
                      pdfs: list[tuple[str, bytes]] | None = None,
                      model: str = MODEL, deep: bool = False) -> str:
    """Run the synthesis. Returns the thesis text. Streams (long output).

    ``pdfs`` is a list of (label, pdf-bytes) filings to read alongside the brief —
    e.g. all of the company's results / transcripts / presentations / announcements
    since the last fiscal year-end. ``pdf_path`` (a single file) is still accepted
    and folded in. ``deep=True`` uses the exhaustive forensic prompt, uncapped.
    """
    client = _client()

    docs: list[tuple[str, bytes]] = list(pdfs or [])
    if pdf_path:
        with open(pdf_path, "rb") as fh:
            docs.append((os.path.basename(pdf_path), fh.read()))

    parts: list[types.Part] = []
    for label, data in docs:
        parts.append(types.Part.from_text(text=f"--- Company filing: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    instruction = ("Write the full forensic fundamental analysis." if deep
                   else "Write the investment note.")
    parts.append(types.Part.from_text(
        text=f"Brief for {symbol}:\n\n{brief_md}\n\n{instruction}"))

    config = types.GenerateContentConfig(
        system_instruction=_DEEP_SYSTEM if deep else _SYSTEM,
        # deep mode: leave max_output_tokens unset (uncapped — use the model max).
        **({} if deep else {"max_output_tokens": 4000}),
    )
    out: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=model, contents=parts, config=config,
    ):
        if chunk.text:
            out.append(chunk.text)
    return "".join(out).strip()


_OVERVIEW_SYS = """You are an equity analyst writing the opening "Business overview" \
section of a deep research report on an Indian listed company, for a sophisticated \
personal investor. You are given the company's own recent filings as PDFs (results, \
concall transcripts, investor presentations, annual report) and a few hard facts \
(market cap, sector). GROUND everything in these filings first.

Write in markdown, starting with the exact heading '## 🏢 Business overview', then \
these '###' subsections (omit a subsection only if you genuinely have nothing grounded \
to say):

### What the company does
2-4 plain-English sentences: the actual business, products/services, and how it makes money.

### Business segments & revenue mix
One bullet per operating segment / business line with its **approximate % of revenue** \
(latest disclosed year — from the investor presentation or annual report). If the company \
does several things, list them all with their shares (e.g. '- Retail lending — ~55% of \
AUM'). If the split isn't disclosed in the filings, list the segments qualitatively and \
say the exact mix isn't disclosed. NEVER invent percentages.

### Market size & positioning
State the **market cap** (use the figure given). Then the **total addressable market / \
industry size** and the company's **penetration / market share and runway** — grounded in \
the filings where stated; you may add well-established, widely-known public industry facts \
but keep them clearly approximate ('~', 'roughly') and NEVER fabricate a precise figure. \
If unknown, say the company doesn't disclose it.

{order_line}

Rules: be specific and concise (~300-400 words total). Cite figures from the filings. \
Do NOT give a buy/sell view or valuation opinion here — this is context only; the verdict \
comes later. If the filings are thin, say what you can and note the limitation. Never \
invent data."""

_OVERVIEW_ORDER = """### Order book / backlog
This is an order-driven business, so state the **latest disclosed order book / backlog** \
value, its trend, and the **book-to-bill** or order-inflow if the filings give them. If \
the filings you were given don't disclose an order book, say so explicitly (do not guess)."""


def business_overview(pdfs: list[tuple[str, bytes]] | None, symbol: str, *,
                      market_cap_cr: float | None = None, industry: str | None = None,
                      order_driven: bool = False, model: str = MODEL) -> str | None:
    """Opening 'Business overview' markdown (what it does, segment revenue mix, market
    size / TAM / penetration, and — for order-driven names — order book) read from the
    company's own filings. Best-effort: returns None if there are no filings to read or
    the call fails, so the report simply omits the section rather than breaking."""
    docs = list(pdfs or [])
    if not docs:
        return None
    facts = [f"Company (NSE symbol): {symbol}"]
    if market_cap_cr and market_cap_cr == market_cap_cr:
        facts.append(f"Market capitalisation: ~₹{market_cap_cr:,.0f} crore")
    if industry:
        facts.append(f"NSE industry classification: {industry}")
    parts: list[types.Part] = []
    for label, data in docs:
        parts.append(types.Part.from_text(text=f"--- Company filing: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    parts.append(types.Part.from_text(
        text="Hard facts:\n" + "\n".join(facts) + "\n\nWrite the Business overview section."))
    system = _OVERVIEW_SYS.format(order_line=_OVERVIEW_ORDER if order_driven else "")
    try:
        out: list[str] = []
        for chunk in _client().models.generate_content_stream(
            model=model, contents=parts,
            config=types.GenerateContentConfig(system_instruction=system),
        ):
            if chunk.text:
                out.append(chunk.text)
        text = "".join(out).strip()
    except Exception:  # noqa: BLE001 — overview is best-effort context, never block the report
        return None
    return text or None


_GROWTH_TRIGGERS_SYS = """You are a senior equity research analyst at a top-tier \
Indian institutional brokerage (Kotak Institutional / Motilal Oswal / Ambit Capital \
caliber). Produce a single, high-density **growth-triggers document** for the company \
that a fund manager can glance at and immediately grasp why this business could re-rate \
over the next 12–36 months. You are given the company's own recent filings as PDFs \
(concall transcripts, investor presentations, results, annual report) plus a block of \
verified snapshot numbers. GROUND every quantified claim in those filings.

Structure it exactly as follows (markdown):

## 🚀 Growth triggers — [Company] (NSE: [Ticker])

### 1. Company snapshot (4–5 lines)
- What the company does, in one jargon-free sentence a non-sector analyst understands.
- Current market cap, CMP, TTM revenue, TTM EBITDA margin, TTM ROE/ROCE — **use the \
verified numbers supplied; do not recompute or estimate them**.
- Promoter holding % and any recent change.
- Where it sits in the value chain (upstream/midstream/downstream) and who the end customers are.
- **Business uniqueness:** what is genuinely unique/moated vs. whether it competes in a \
commoditized industry — say which, plainly.

### 2. Core growth triggers
5–7 specific, concrete triggers (never generic tailwinds), each in this exact shape:

**[Trigger name — crisp 5–7 words]**
- **What's happening:** 2–3 sentences — the specific capex, order win, capacity addition, \
product launch, policy change, or structural shift.
- **Quantified impact:** numbers wherever the filings allow — incremental revenue (₹ cr), \
margin expansion (bps), volume growth (%), capacity/utilisation, addressable market, \
order-book/bid-pipeline value.
- **Estimated business impact (₹ cr and %):** REQUIRED for every trigger — translate the \
trigger into money so its magnitude can be judged independently of conviction:
  - **Incremental annual revenue: ₹X cr (≈Y% of TTM revenue)** — use the verified TTM \
revenue supplied as the denominator. When the filings give a range, show the range.
  - Where estimable, the incremental EBITDA/earnings (₹ cr, using the segment's or \
company's margin — state which) and the **potential market-cap impact: ≈Z%** (incremental \
earnings × the company's current multiple, against the verified market cap — state the \
multiple used). If a step is genuinely not estimable from the filings, write \
"*not estimable from filings*" for that step only — never invent, but never skip the \
whole line when the revenue leg IS estimable.
- **Timeline:** when it starts flowing into the P&L (e.g. "H2 FY27", "commissioning Q1 FY28").
- **Conviction:** **HIGH CONVICTION** (already visible in order book / capex / policy), \
**MEDIUM CONVICTION** (management-guided, not yet contracted), or **OPTIONALITY** \
(asymmetric upside, not in consensus).

Order the triggers by this priority: (1) capacity/capex-led volume, (2) new \
product/segment/geography, (3) margin-expansion drivers, (4) policy/regulatory catalysts, \
(5) industry-structure shifts (consolidation, competitor exit, import substitution, China+1), \
(6) balance-sheet triggers (deleveraging, asset monetisation, subsidiary value unlock), \
(7) management/governance upgrades.

### 3. What's already in the price? (2–3 lines)
What is consensus already discounting, and where is the incremental surprise vs. street.

### 4. Key risks to the trigger thesis (3–4 bullets)
What can delay or derail each high-conviction trigger — execution, regulatory, input-cost, \
demand-cyclicality, or balance-sheet risk.

### 5. Trigger scoreboard
A markdown table:
| # | Trigger | Est. impact (₹ cr) | % of TTM revenue | Potential mcap impact | Timeline | Conviction |
**Sort the scoreboard by estimated ₹ cr impact (largest first) — NOT by conviction** — so a \
big MEDIUM-conviction trigger is never buried under small HIGH-conviction ones. After the \
table add one line — **Priority read:** — naming any MEDIUM/OPTIONALITY trigger whose \
estimated impact ranks in the top 3, e.g. "Trigger #2 is only MEDIUM conviction but carries \
the largest ₹ impact — size it on probability, don't ignore it."

**Quality & sourcing rules (strict):**
- Every trigger must be **company-specific and verifiable** from the filings — cite the \
source inline (e.g. "(Q4FY26 concall)", "(May-2026 investor presentation, p.17)"). NO filler \
like "India's growing economy" or "rising middle class".
- Every ₹cr / bps / % figure must be **sourced to a filing** OR flagged as an explicit \
estimate with the assumption stated. If the data for a trigger isn't disclosed, write \
"*awaiting disclosure*" — never guess a number.
- Use the model's own knowledge only for industry framing, and label it as context — never \
present it as a company-specific fact.
- Write like a conviction note briefing a PM before a position-sizing meeting: dense, \
specific, no fluff. Length is fine — cover every real trigger; do not artificially compress \
or truncate. If the filings are thin, say so and give what is grounded.
- Output ONLY the finished document — do NOT echo these instructions, the section \
descriptions, or the bracketed placeholders; start directly at the '## 🚀 Growth triggers' heading."""


def growth_triggers(pdfs: list[tuple[str, bytes]] | None, symbol: str, *,
                    facts: list[str] | None = None, model: str = MODEL) -> str | None:
    """Forward-looking **growth-triggers 1-pager** for ``symbol`` — catalysts, quantified
    and conviction-tagged — read from the company's own filings and grounded on a block of
    verified snapshot numbers (``facts``). Best-effort: returns None if there are no filings
    to read or the call fails, so the caller can fall back gracefully. Never invents data."""
    docs = list(pdfs or [])
    if not docs:
        return None
    parts: list[types.Part] = []
    for label, data in docs:
        parts.append(types.Part.from_text(text=f"--- Company filing: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    facts_block = ("Verified snapshot numbers (use these exact figures in Section 1; do not "
                   "recompute):\n" + "\n".join(f"- {f}" for f in (facts or []))) if facts else ""
    parts.append(types.Part.from_text(
        text=f"Company: {symbol}\n\n{facts_block}\n\nProduce the growth-triggers document, "
             "grounded in the attached filings."))
    try:
        out: list[str] = []
        for chunk in _client().models.generate_content_stream(
            model=model, contents=parts,
            config=types.GenerateContentConfig(system_instruction=_GROWTH_TRIGGERS_SYS),
        ):
            if chunk.text:
                out.append(chunk.text)
        text = "".join(out).strip()
    except Exception:  # noqa: BLE001 — best-effort, never block on the follow-up
        return None
    return text or None


_IPO_SYS = """You are a senior equity research analyst at a top-tier Indian institutional \
brokerage, writing a **pre-listing IPO note** for a personal investor deciding whether to \
apply. You are given the company's official offer documents as PDFs — the **Red Herring \
Prospectus (RHP)**, the **price-band advertisement** (which carries the KPIs, valuation at \
the band and the listed-peer comparison), and the **anchor allotment** — plus a block of \
verified issue facts (dates, price band, size, live subscription). GROUND every number in \
these documents; this is a company with NO listed trading history, so the RHP is your truth.

Write a thorough, section-by-section note that a non-expert can actually follow and enjoy \
reading — the same depth and teaching style as a deep single-stock report. Do NOT artificially \
compress; length is fine — depth, clarity and *explanation* matter more than brevity. Cite the \
actual numbers. Use markdown with these sections:

## 🧾 IPO analysis — [Company] (NSE: [Ticker])

### 1. What the company does
3-5 plain-English sentences: the actual business, its products/services, **how it makes money**, \
who its customers are, and any edge/moat or scale it claims in the RHP. Then the sector and the \
headline of the issue (size, price band, dates) — use the verified facts supplied. Set the scene \
so a reader new to the name understands it before the numbers start.

### 2. The offer — structure & what it signals
Break the issue into **Fresh Issue vs Offer for Sale (OFS)** with the ₹ amounts and %s, and \
interpret it plainly:
- **Fresh issue** — new shares; the money goes **to the company** (capex / debt repayment / \
working capital). Generally a **positive** — the business is being funded.
- **Offer for Sale (OFS)** — existing holders (promoters / PE-VC investors) selling their \
stake; the money goes **to them, not the company**. A large OFS warrants **caution** — ask \
*who* is exiting and *why now* (a full PE exit reads differently from a small promoter trim).
State promoter holding **pre → post** issue, and the lot size / minimum retail application \
(and what one lot costs at the upper band, in ₹).

### 3. Financials (restated, from the RHP)
The last 3 years (+ any stub period) of revenue, EBITDA/EBIT margin, PAT, net worth, debt, \
and operating cash flow. Don't just list them — **read the trajectory**: is growth real and \
durable or lumpy, are margins expanding or thinning, is PAT backed by operating cash (or is \
CFO weak vs PAT)? Flag red flags (falling margins, negative/erratic CFO, related-party \
dependence, customer or geographic concentration, a debt spike).

### 4. Valuation at the band
The **P/E, P/B and RoNW at the upper price band** (from the price-band ad's KPI table). Briefly \
say what each means (P/E = the years of current earnings you're paying for; RoNW = how hard the \
company's equity works) and then judge: is the issue cheap, fair, or richly priced vs. the \
**listed peers** in the ad and vs. its own growth? Cite the peer multiples. A high multiple can \
be fine for a fast, clean compounder and dangerous for a slow or cyclical one — say which this is.

### 5. Use of proceeds (objects of the issue)
What the fresh-issue money will actually fund — debt repayment / capex / acquisition / \
general corporate purposes — and whether it's value-accretive. (Concrete growth/deleveraging \
use > vague "general corporate purposes", which is a mild negative.)

### 6. Key risks (from the RHP risk factors)
The 4-6 most material, investor-relevant risks — not boilerplate. Concentration, litigation/ \
contingent liabilities, regulatory dependence, promoter/governance, working-capital stress. \
Say why each one matters for *this* business.

### 7. Demand signals
**Subscription** so far (overall and QIB / NII / Retail, from the verified facts) and the \
**anchor book** (who anchored and how much, from the anchor document). Explain the read: strong \
QIB demand and marquee institutional anchors (long-only funds, insurers, sovereign funds) are \
confidence signals; a weak QIB book, or a book stuffed with short-term money, is a warning.

### 8. Verdict — APPLY / AVOID / NEUTRAL
A clear call with the 3-4 reasons that drive it (quality × valuation × issue structure × \
demand), plus who it suits (listing-gain punt vs. long-term hold) and the main risk to the call.

**Explain as you go:** for every metric you cite (P/E, RoNW, margins, subscription multiples), \
briefly say *what it measures* and what a *healthy/normal* value looks like for a business like \
this, so a first-time IPO applicant can follow the reasoning — but stay rigorous.

**Rules:** Ground every figure in the documents; cite them ("RHP p.X", "price-band ad"). If \
something isn't disclosed, say "*not disclosed*" — never invent. Do **NOT** cite or rely on \
**grey-market premium (GMP)** or any unofficial/grey-market chatter — it is not a primary \
source. Be specific and decisive; length is fine, but no filler. Any table must be valid \
markdown (a header, a `|---|` separator, equal columns per row), ≤5 columns, never a \
space-aligned/ASCII table. Output ONLY the finished note — do not echo these instructions, \
and start at the '## 🧾 IPO analysis' heading."""


def ipo_analysis(pdfs: list[tuple[str, bytes]] | None, symbol: str, *,
                 facts: list[str] | None = None, model: str = MODEL) -> str | None:
    """Pre-listing IPO note for ``symbol`` — offer structure (fresh vs OFS), restated
    financials, valuation-at-band vs peers, use of proceeds, risks, demand, and an
    APPLY / AVOID / NEUTRAL verdict — read from the official RHP + price-band ad + anchor
    docs and grounded on verified issue facts. None if the documents aren't available."""
    docs = list(pdfs or [])
    if not docs:
        return None
    parts: list[types.Part] = []
    for label, data in docs:
        parts.append(types.Part.from_text(text=f"--- Offer document: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    facts_block = ("Verified issue facts (use these exact figures):\n"
                   + "\n".join(f"- {f}" for f in (facts or []))) if facts else ""
    parts.append(types.Part.from_text(
        text=f"Company (NSE symbol): {symbol}\n\n{facts_block}\n\nWrite the pre-listing IPO note."))
    try:
        out: list[str] = []
        for chunk in _client().models.generate_content_stream(
            model=model, contents=parts,
            config=types.GenerateContentConfig(system_instruction=_IPO_SYS),
        ):
            if chunk.text:
                out.append(chunk.text)
        text = "".join(out).strip()
    except Exception:  # noqa: BLE001 — best-effort
        return None
    return text or None


_FUND_SYS = """You are a seasoned mutual-fund analyst advising a sophisticated Indian \
personal investor who already holds direct stocks. You are given a **deterministic fund \
report** built entirely from primary data: AMFI NAV history (trailing returns, category \
rank/percentile, rolling 1-year consistency, volatility, Sharpe/Sortino, max drawdown), an \
**SIP/XIRR** simulation, **benchmark-relative** behaviour (alpha, beta, up/down capture, \
tracking error, information ratio), and — where the AMC's SEBI monthly portfolio disclosure \
is covered — the holdings, concentration, sector tilt, overlap with the user's own watchlist, \
and **what the manager bought/exited last month**.

Write a thorough, section-by-section note that a non-expert can actually follow and enjoy \
reading — the same depth and teaching style as a deep single-stock report. Do NOT artificially \
compress; length is fine — depth, clarity and *explanation* matter more than brevity. Use \
markdown with these sections (omit one only if the underlying data is genuinely absent):

### 🎯 Verdict
One line: **Buy / Accumulate / Hold / Switch / Avoid**, then 2-3 sentences on who this fund \
actually suits — core holding vs satellite, SIP vs lump-sum, and the risk profile it fits.

### 🧭 What this fund is
Plain-English: its **category** and what that mandate actually means (e.g. a flexi-cap can roam \
across market caps; a mid-cap must stay ~65% in mid-caps — so expect higher highs and deeper \
drawdowns), what the fund is trying to do, and where it sits on the risk/return spectrum. \
Ground the category/AMC in the report; do NOT invent a manager name, AUM, expense ratio or \
mandate detail that isn't given.

### 📈 Returns — and what they mean
Walk through the trailing returns and the **category rank/percentile**. Explain the difference \
between a point-to-point CAGR and lived experience, and whether the fund is beating or lagging \
its peer set — and by how much. Is the track record long enough to trust?

### ⚖️ Risk — how bumpy the ride is
Interpret **volatility**, **Sharpe** and **Sortino** (return per unit of *total* vs *downside* \
risk — say what a good number looks like), and **max drawdown** (the worst peak-to-trough fall — \
frame it as "you'd have needed the stomach to sit through a X% drop"). Tie the risk back to the \
mandate.

### 🔁 Consistency
Read the **rolling 1-year worst / median / best**: consistency across many start dates beats a \
flattering single trailing number. How bad was a bad year, how good a good one?

### 💰 What an SIP actually earned
Interpret the **SIP XIRR vs the lump-sum CAGR** — the money-weighted return a monthly investor \
actually got, and why it differs. Which way has this fund historically rewarded investors?

### 🎯 Versus its benchmark
The real test of active management. Explain **alpha vs beta** (is outperformance genuine skill \
or just dialled-up market risk? — a high beta in a bull run is NOT alpha), **up/down capture** \
(capturing most of the upside while taking *less* of the downside is the hallmark of a good \
fund), and **tracking error / information ratio** (excess return per unit of active risk). Say \
plainly whether the manager has actually added value.

### 🧬 What the portfolio says
If holdings are present, read the **concentration** (top-10 %, single-name and sector bets — \
conviction or fragility?), the **churn** (fresh buys / exits / adds — what the manager is \
actually doing with conviction, not what they say), and the **overlap with the user's own \
watchlist**: if the fund largely holds names they already own directly, say so plainly — that \
is duplicated risk, not diversification.

### 🔍 Risks & what to watch
3-5 concrete, fund-specific things that would change the call (style going out of favour, \
concentration, a manager-dependent record, a short history, a benchmark-lagging phase).

**Explain as you go:** for every metric you cite, briefly say *what it measures*, what a \
*typical/healthy* value looks like, and what *this* value means for *this* fund given its \
category — a mid-cap equity fund and a liquid fund have completely different 'normal' ranges. \
Write so a newcomer can follow, but stay rigorous.

**Rules:** ground every claim in a number that is in the report — cite it. Where the report \
says a figure is unavailable or based on a short history, respect that and say so; never invent \
returns, expense ratios, AUM or manager names (they are NOT in the data). Be direct and \
critical — this is a personal decision, not marketing copy. Any table must be valid markdown (a \
header, a `|---|` separator, equal columns per row), ≤5 columns, never a space-aligned/ASCII \
table. Output ONLY the note — no preamble, do not echo these instructions, and start directly \
at the '### 🎯 Verdict' heading."""


def fund_thesis(brief_md: str, fund_name: str, *, model: str = MODEL) -> str | None:
    """Qualitative read + verdict over the deterministic fund report. Best-effort —
    returns None on any failure so the report still goes out numbers-only."""
    try:
        out: list[str] = []
        for chunk in _client().models.generate_content_stream(
            model=model,
            contents=[types.Part.from_text(
                text=f"Fund report for {fund_name}:\n\n{brief_md}\n\nWrite the note.")],
            config=types.GenerateContentConfig(system_instruction=_FUND_SYS),
        ):
            if chunk.text:
                out.append(chunk.text)
        text = "".join(out).strip()
    except Exception:  # noqa: BLE001 — thesis is a bonus, never block the fund report
        return None
    return text or None


_FILING_SYS = """You are a forensic equity analyst. You are given ONE company \
filing/disclosure for an Indian listed company — e.g. quarterly results, a concall \
transcript, an investor presentation, an annual report, an order/contract win, an \
acquisition, a credit-rating action, or another corporate-action document.

Reply with ONE flat markdown bullet list — every line starts with '- ', one fact per \
line. Do NOT use section headings, bold titles, numbered lists, or nested/indented \
sub-bullets; fold any grouping into the bullet text itself (e.g. '- Resolution 3 \
(re-appoint Sagar Adani): passed with 99.77% for; 0.72% of institutions against').

Be COMPREHENSIVE — capture every material specific; do not generalise or omit. Always \
pull out the concrete numbers: amounts/values (order or deal size, fund-raise amount, \
rating + prior rating/outlook), the counterparties (client, acquirer/target, agency), \
quantities/capacities, dates and timelines, stake %s, voting/approval outcomes (with the \
% for and against), guidance and outlook, margin/cash/order-book trends, and any risks, \
**contingent liabilities** or **related-party transactions**. Cite exact figures.

No length limit — as many bullets as the document warrants; never trail off mid-thought. \
If the filing is genuinely routine/administrative with no investor-relevant detail, say \
so in a single bullet. Never invent anything not in it."""


def analyze_filing(pdf_bytes: bytes, symbol: str, event_title: str,
                   *, model: str = MODEL) -> str:
    """Focused investor read of a single filing PDF (for inline digest analysis)."""
    client = _client()
    parts = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        types.Part.from_text(text=f"Filing for {symbol} — event: {event_title}. "
                             "Give the investor takeaways."),
    ]
    # no max_output_tokens — let the model finish; length is controlled by the prompt
    # (~180 words), so the analysis is never guillotined mid-sentence.
    config = types.GenerateContentConfig(system_instruction=_FILING_SYS)
    out: list[str] = []
    for chunk in client.models.generate_content_stream(model=model, contents=parts, config=config):
        if chunk.text:
            out.append(chunk.text)
    return "".join(out).strip()


_LABEL_SYS = """You label Indian-exchange (NSE/BSE) corporate filings and board-meeting \
notices. You are given a numbered list of raw announcement texts. Reply with the SAME \
numbers, one label per line, each the concise plain-English SUBJECT/purpose of AT MOST \
~8 words — e.g. "Q1 results & dividend", "Fund raising via QIP", "Stock split 1:5", \
"Bonus issue", "Scheme of amalgamation", "Buyback of shares". Give ONLY the subject — do \
NOT include the words "board meeting", "intimation", "outcome", or "announcement". No extra \
commentary, no blank lines, no markdown. One line per input number, in order."""


def label_events(texts: list[str], *, model: str = MODEL) -> list[str]:
    """Concise plain-English labels for a batch of NSE filing / board-meeting texts in ONE
    LLM call (cheap — one call per scan). Returns a list aligned to ``texts`` ("" where
    the model gave nothing for that item); returns all-"" on any failure so the caller falls
    back to its heuristic. Never raises."""
    items = [" ".join((t or "").split())[:400] for t in texts]
    blank = ["" for _ in items]
    if not any(items):
        return blank
    numbered = "\n".join(f"{i + 1}. {t or '(no text)'}" for i, t in enumerate(items))
    try:
        resp = _client().models.generate_content(
            model=model,
            contents=[types.Part.from_text(text=numbered)],
            config=types.GenerateContentConfig(system_instruction=_LABEL_SYS, max_output_tokens=1200),
        )
        text = resp.text or ""
    except Exception:  # noqa: BLE001 — labeling is best-effort
        return blank
    out = list(blank)
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(out):
                out[idx] = m.group(2).strip().strip("*").strip().rstrip(".")[:60]
    return out


_GUIDANCE_SYS = """You read Indian-listed companies' filings (concall transcripts, \
investor presentations, results, outlook statements). Extract ONLY explicit FORWARD \
guidance that MANAGEMENT itself gave for a FUTURE full fiscal year — e.g. "we expect \
FY27 revenue of ~₹5,000 cr", "targeting 18-20% EBITDA margin next year", "guiding to \
₹1,200 cr EBITDA in FY27".

Reply with ONLY a JSON object (money figures in ₹ crore, numbers only):
{"fy_label":"FY27","revenue_cr":5000,"ebitda_cr":1200,"ebit_margin":19.0,"pat_cr":800,"source":"Q4FY26 concall"}
Use null for any field management did not give. If a RANGE is given, use the midpoint. \
If management gave NO explicit forward guidance for a future year, reply exactly \
{"guidance":null}. Never infer or invent a number management did not state."""


def extract_guidance(pdfs: list[tuple[str, bytes]] | None, *, model: str = MODEL) -> dict | None:
    """Pull management's **explicit** forward guidance (a future FY's revenue / EBITDA /
    margin / PAT) from the filing PDFs, as a dict; ``None`` if none was given. Best-effort —
    forces JSON output, never raises, and never invents numbers (so the report's forward
    multiple is only shown when management actually guided)."""
    docs = list(pdfs or [])
    if not docs:
        return None
    parts: list[types.Part] = []
    for label, data in docs:
        parts.append(types.Part.from_text(text=f"--- Filing: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    parts.append(types.Part.from_text(text="Extract management's forward guidance as JSON."))
    try:
        resp = _client().models.generate_content(
            model=model, contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=_GUIDANCE_SYS, response_mime_type="application/json"),
        )
        text = (resp.text or "").strip()
    except Exception:  # noqa: BLE001 — guidance is best-effort
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("guidance", "x") is None:
        return None
    # need at least one usable forward number to be worth showing
    if not any(d.get(k) is not None for k in ("revenue_cr", "ebitda_cr", "ebit_margin", "pat_cr")):
        return None
    return d
