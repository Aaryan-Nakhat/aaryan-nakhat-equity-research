"""Claude synthesis — turn the quant brief (+ optional filing PDF) into a thesis.

The deterministic brief carries the numbers; Claude's job is the qualitative
read: weigh the signals, fold in management commentary from a concall transcript
or annual report (if supplied), and produce a structured verdict with reasons.

Needs ANTHROPIC_API_KEY in the environment. See ``docs/REPORTS.md``.
"""

from __future__ import annotations

import os

import anthropic

MODEL = "claude-opus-4-8"

_SYSTEM = """You are a sober, sell-side-grade equity analyst covering Indian \
stocks. You are given a quantitative brief assembled from PRIMARY sources only \
(exchange filings, XBRL financials, EOD prices). Optionally you are also given a \
company filing (concall transcript or annual report) as a PDF.

Write a concise investment note for a retail investor who will act on it. Be \
specific and grounded in the numbers provided — cite them. Do not invent data \
not in the brief or the filing. Where the brief says a value is n/a or flags a \
caveat (e.g. stale shares), respect it.

Structure:
1. **Verdict** — one line: Buy / Accumulate / Hold / Reduce / Avoid, with a one-\
sentence rationale.
2. **Why** — 3-6 bullets tying the call to specific fundamental, forensic, \
technical, and valuation signals.
3. **Risks / red flags** — what could break the thesis (forensic flags, valuation \
stretch, technical weakness, anything from the filing).
4. **What to watch** — 2-3 concrete upcoming triggers.

Keep it under ~450 words. This is analysis for a personal decision, not advice \
for the public."""


def synthesize_thesis(brief_md: str, symbol: str, *, pdf_path: str | None = None,
                      model: str = MODEL) -> str:
    """Run the synthesis. Returns the thesis text. Streams (long output)."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    user_content: list[dict] = []
    if pdf_path:
        with open(pdf_path, "rb") as fh:
            uploaded = client.beta.files.upload(
                file=(os.path.basename(pdf_path), fh, "application/pdf"),
            )
        user_content.append({
            "type": "document",
            "source": {"type": "file", "file_id": uploaded.id},
            "title": os.path.basename(pdf_path),
        })
    user_content.append({
        "type": "text",
        "text": f"Quantitative brief for {symbol}:\n\n{brief_md}\n\n"
                "Write the investment note.",
    })

    kwargs = dict(
        model=model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    # PDF documents go through the beta Files API surface.
    create = client.beta.messages.stream if pdf_path else client.messages.stream
    if pdf_path:
        kwargs["betas"] = ["files-api-2025-04-14"]

    parts: list[str] = []
    with create(**kwargs) as stream:
        for text in stream.text_stream:
            parts.append(text)
    return "".join(parts).strip()
