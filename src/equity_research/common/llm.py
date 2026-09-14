"""Provider-agnostic LLM access — the single place the project talks to a language model.

Everything else calls :func:`generate`; the provider is chosen entirely from the environment, so
you can run this on **any** LLM without touching code. Configure it in your ``.env``::

    LLM_PROVIDER=openai            # openai | openai_compatible | google | anthropic
    LLM_MODEL=<model-name>
    LLM_API_KEY=<your key>         # for openai / openai_compatible / anthropic
    LLM_BASE_URL=<endpoint>        # optional — any OpenAI-compatible endpoint (self-hosted, gateway…)

``openai_compatible`` (an OpenAI-style ``base_url`` + key) covers the large majority of hosted and
local models through one interface. The ``google`` and ``anthropic`` backends use their own SDKs.

Two capabilities are provider-dependent and used only by some features:
* **File reading** (PDF filings for the deep report) — supported on backends that accept documents;
  others receive a text-only prompt and simply have less to read.
* **Web-grounded search** (the discovery engines) — used on backends that expose a search tool;
  others answer un-grounded. Both degrade gracefully, never error.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("equity-research.llm")


def provider() -> str:
    return (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()


def model() -> str:
    """The configured model name (from ``LLM_MODEL``). Empty if unset — callers degrade gracefully."""
    return (os.environ.get("LLM_MODEL") or "").strip()


# ── cached client (one per process) ──
_client = None
_client_provider: str | None = None


def _get_client():
    global _client, _client_provider
    p = provider()
    if _client is not None and _client_provider == p:
        return _client
    _client_provider = p
    if p in ("openai", "openai_compatible"):
        from openai import OpenAI
        _client = OpenAI(api_key=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
                         base_url=os.environ.get("LLM_BASE_URL") or None)
    elif p == "anthropic":
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    elif p == "google":
        _client = _build_google_client()
    else:
        raise ValueError(f"Unknown LLM_PROVIDER '{p}' (use openai | openai_compatible | google | anthropic)")
    return _client


def _build_google_client():
    """Google backend — service-account/ADC project mode when configured, else an API key. Reads the
    provider's own standard env vars (only relevant when LLM_PROVIDER=google)."""
    from google import llm_sdk
    if os.environ.get("LLM_USE_CLOUD", "").lower() in ("1", "true", "yes"):
        creds = None
        sa_file = (os.environ.get("LLM_CREDENTIALS_FILE")
                   or os.environ.get("LLM_CREDENTIALS"))
        if sa_file:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                sa_file, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return llm_sdk.Client(cloud mode=True, project=os.environ.get("LLM_PROJECT"),
                            location=os.environ.get("LLM_REGION", "global"), credentials=creds)
    return llm_sdk.Client(api_key=os.environ.get("LLM_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


# ── the one entry point ──
def generate(system: str, user_text: str, *, files: list[tuple[str, bytes]] | None = None,
             json: bool = False, max_tokens: int | None = None, grounded: bool = False,
             model_name: str | None = None) -> str:
    """Run a single completion and return the text. ``files`` are ``(label, pdf-bytes)`` documents to
    read alongside the prompt (used where the backend supports it); ``json`` asks for a JSON reply;
    ``grounded`` requests web-search grounding (used where the backend supports it). Raises on a hard
    provider error — callers wrap this in try/except and degrade (their existing behaviour)."""
    m = model_name or model()
    p = provider()
    if p in ("openai", "openai_compatible"):
        return _openai_generate(system, user_text, files, json, max_tokens, m)
    if p == "anthropic":
        return _anthropic_generate(system, user_text, files, json, max_tokens, m)
    return _google_generate(system, user_text, files, json, max_tokens, grounded, m)


def _files_note(user_text: str, files) -> str:
    """For text-only backends: fold a short note that documents were provided but not readable here."""
    if not files:
        return user_text
    labels = ", ".join(lbl for lbl, _ in files)
    return (f"{user_text}\n\n[Note: {len(files)} source document(s) were provided ({labels}) but this "
            "model backend reads text only — base your answer on the text above.]")


def _openai_generate(system, user_text, files, json_mode, max_tokens, m) -> str:
    client = _get_client()
    kwargs = {"model": m, "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": _files_note(user_text, files)}]}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _anthropic_generate(system, user_text, files, json_mode, max_tokens, m) -> str:
    client = _get_client()
    hint = "\n\nReply with ONLY valid JSON." if json_mode else ""
    resp = client.messages.create(
        model=m, system=system, max_tokens=max_tokens or 4096,
        messages=[{"role": "user", "content": _files_note(user_text, files) + hint}])
    return "".join(getattr(b, "text", "") for b in resp.content).strip()


def _google_generate(system, user_text, files, json_mode, max_tokens, grounded, m) -> str:
    from the LLM SDK import types
    client = _get_client()
    parts: list = []
    for label, data in files or []:
        parts.append(types.Part.from_text(text=f"--- Source document: {label} ---"))
        parts.append(types.Part.from_bytes(data=data, mime_type="application/pdf"))
    parts.append(types.Part.from_text(text=user_text))
    cfg: dict = {"system_instruction": system}
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    if max_tokens:
        cfg["max_output_tokens"] = max_tokens
    if grounded:
        cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    config = types.GenerateContentConfig(**cfg)
    out: list[str] = []
    for chunk in client.models.generate_content_stream(model=m, contents=parts, config=config):
        if chunk.text:
            out.append(chunk.text)
    return "".join(out).strip()
