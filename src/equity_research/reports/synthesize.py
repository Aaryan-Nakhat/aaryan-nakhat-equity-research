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


def _client() -> genai.Client:
    """Vertex (project+location / ADC) if configured, else the Developer API key."""
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        return genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    return genai.Client()  # reads GOOGLE_API_KEY / GEMINI_API_KEY


def synthesize_thesis(brief_md: str, symbol: str, *, pdf_path: str | None = None,
                      model: str = MODEL) -> str:
    """Run the synthesis. Returns the thesis text. Streams (long output)."""
    client = _client()

    parts: list[types.Part] = []
    if pdf_path:
        with open(pdf_path, "rb") as fh:
            parts.append(types.Part.from_bytes(data=fh.read(), mime_type="application/pdf"))
    parts.append(types.Part.from_text(
        text=f"Quantitative brief for {symbol}:\n\n{brief_md}\n\nWrite the investment note."))

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        max_output_tokens=4000,
    )
    out: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=model, contents=parts, config=config,
    ):
        if chunk.text:
            out.append(chunk.text)
    return "".join(out).strip()
