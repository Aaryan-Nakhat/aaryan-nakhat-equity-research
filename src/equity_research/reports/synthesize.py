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
sources (exchange XBRL filings, EOD prices). Optionally also a filing PDF.

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
components*; flag aggressive accounting, other-income dependence, tax-rate \
anomalies, related-party / receivables concerns the numbers hint at.
6. **Valuation** — current multiples vs own history and sector; what is priced in.
7. **Verdict** — Buy / Accumulate / Hold / Reduce / Avoid, the key reasons, the \
main risks/red flags, and concrete things to watch.

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
                      model: str = MODEL, deep: bool = False) -> str:
    """Run the synthesis. Returns the thesis text. Streams (long output).

    ``deep=True`` switches to the exhaustive forensic prompt and leaves the output
    length uncapped (the model's own default max).
    """
    client = _client()

    parts: list[types.Part] = []
    if pdf_path:
        with open(pdf_path, "rb") as fh:
            parts.append(types.Part.from_bytes(data=fh.read(), mime_type="application/pdf"))
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
