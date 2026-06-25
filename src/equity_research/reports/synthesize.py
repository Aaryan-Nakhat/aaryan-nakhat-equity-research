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


_FILING_SYS = """You are a forensic equity analyst. You are given ONE company \
filing/disclosure for an Indian listed company — e.g. quarterly results, a concall \
transcript, an investor presentation, an annual report, an order/contract win, an \
acquisition, a credit-rating action, or another corporate-action document.

Summarise it as a **point-wise markdown bullet list** ('- ' bullets) for an investor.
Be COMPREHENSIVE — capture every material specific the document contains; do NOT \
generalise or omit detail. In particular, always pull out the concrete specifics:
- amounts & values — order/contract size, deal/acquisition value, fund-raise amount, \
credit rating (and prior rating / outlook);
- the counterparties — client/customer, acquirer/target, lender, rating agency;
- quantities, capacities, dates, timelines, and completion/execution periods;
- ownership / stake %s, and any voting or approval outcomes (with the % for/against);
- guidance, outlook, and margin / cash / order-book trends (transcripts/presentations);
- risks, red flags, **contingent liabilities** and **related-party transactions**.

Cite the exact figures from the document. One fact per bullet; keep each bullet tight. \
Do NOT impose a length limit — include as many bullets as the document warrants, and \
never trail off mid-thought. If the filing is genuinely routine/administrative with no \
investor-relevant detail, say so in a single bullet. Never invent anything not in it."""


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
