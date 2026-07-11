"""LLM synthesis — turn the quant brief (+ optional filing PDF) into a thesis.

Uses Google's Gemini via the `google-genai` SDK. The deterministic brief carries
the numbers; the model's job is the qualitative read: weigh the signals, fold in
management commentary from a concall transcript / annual report (if supplied),
and produce a structured verdict with reasons.

Auth — set in the environment (see ``.env.example``), two options:
  - **Vertex AI** (workplace GCP): GOOGLE_GENAI_USE_VERTEXAI=true,
    GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION (+ ADC, or a Vertex API key via
    GOOGLE_API_KEY for express mode).
  - **Gemini Developer API**: GOOGLE_API_KEY (or GEMINI_API_KEY) only.
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
critical — this is a forensic review, not a summary."""


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
A markdown table: | # | Trigger | Revenue/earnings impact | Timeline | Conviction |

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
    Gemini call (cheap — one call per scan). Returns a list aligned to ``texts`` ("" where
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
