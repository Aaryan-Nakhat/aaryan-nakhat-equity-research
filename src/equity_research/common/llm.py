"""Provider-agnostic LLM access — the single place the project talks to a language model.

Everything else calls :func:`generate`. The model and provider are chosen **entirely from the
environment** (via LiteLLM), so the workbench runs on any LLM without touching code. Configure it
in your ``.env``::

    LLM_MODEL=<model>          # LiteLLM model string; the provider is inferred from it
    LLM_API_KEY=<key>          # your provider key (omit if the backend uses ambient credentials)
    LLM_BASE_URL=<endpoint>    # optional: a custom API base URL (self-hosted, gateway, router…)
    LLM_SEARCH_TOOL=<json>     # optional: the web-search tool spec your model accepts, to enable the
                               # grounded discovery features; leave unset to run un-grounded

See LiteLLM's provider docs for the model strings, keys, and any extra provider env vars a given
backend needs. Two capabilities are model-dependent and degrade gracefully: **document reading**
(PDF filings for the deep report) and **web-grounded search** (the discovery engines) — a backend
that lacks either simply gets a text-only / un-grounded call, never an error.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import os

import litellm

log = logging.getLogger("equity-research.llm")
litellm.drop_params = True          # silently drop a param a given model doesn't support
litellm.suppress_debug_info = True


def model() -> str:
    """The configured model string (``LLM_MODEL``). Empty if unset — callers degrade gracefully."""
    return (os.environ.get("LLM_MODEL") or "").strip()


def _messages(system: str, user_text: str, files) -> list[dict]:
    if files:
        content: list | str = [{"type": "text", "text": user_text}]
        for label, data in files:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "file", "file": {
                "file_data": f"data:application/pdf;base64,{b64}", "filename": label}})
    else:
        content = user_text
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


_DOC_ERR_HINTS = ("no pages", "has no pages", "unable to process", "failed to process",
                  "cannot process", "invalid pdf", "corrupt", "unable to read",
                  "could not process", "unsupported document")


def _is_document_error(exc: Exception) -> bool:
    """True if a provider error looks like it was caused by an attached document the
    backend couldn't read (so the call can be retried without the files)."""
    return any(h in str(exc).lower() for h in _DOC_ERR_HINTS)


def _search_tool() -> list | None:
    """The web-search tool spec to attach for grounded calls, taken verbatim from the environment
    (``LLM_SEARCH_TOOL``, a JSON object). ``None`` when unset — grounded calls then run un-grounded."""
    raw = os.environ.get("LLM_SEARCH_TOOL")
    if not raw:
        return None
    try:
        return [_json.loads(raw)]
    except (ValueError, TypeError):
        return None


def generate(system: str, user_text: str, *, files: list[tuple[str, bytes]] | None = None,
             json: bool = False, max_tokens: int | None = None, grounded: bool = False,
             model_name: str | None = None) -> str:
    """Run a single completion and return the text. ``files`` are ``(label, pdf-bytes)`` documents to
    read alongside the prompt (used where the model supports it); ``json`` asks for a JSON reply;
    ``grounded`` attaches the configured web-search tool. Raises on a hard provider error — callers
    wrap this in try/except and degrade (their existing behaviour)."""
    kwargs: dict = {"model": model_name or model(),
                    "messages": _messages(system, user_text, files)}
    if os.environ.get("LLM_API_KEY"):
        kwargs["api_key"] = os.environ["LLM_API_KEY"]
    if os.environ.get("LLM_BASE_URL"):
        kwargs["api_base"] = os.environ["LLM_BASE_URL"]
    if json:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if grounded and (tool := _search_tool()):
        kwargs["tools"] = tool
    try:
        resp = litellm.completion(**kwargs)
    except Exception as e:  # noqa: BLE001
        if files and _is_document_error(e):   # a bad attachment shouldn't sink the whole call —
            log.warning("LLM rejected an attached document (%s); retrying text-only",
                        str(e).splitlines()[0][:200])
            kwargs["messages"] = _messages(system, user_text, None)
            resp = litellm.completion(**kwargs)
        else:
            raise
    choices = getattr(resp, "choices", None) or []         # empty when the provider returns no
    if not choices:                                        # candidate (safety block / empty reply)
        return ""
    return (choices[0].message.content or "").strip()
