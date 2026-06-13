"""Plain-HTTP fetch helpers built on scrapling's ``Fetcher``.

Centralises the Phase-1 gotcha: ``Response.text`` can be empty on responses
with no declared charset (NSE CSVs, some BSE JSON), so we always read
``Response.body`` and decode ourselves. See ``docs/SCRAPING.md``.
"""

from __future__ import annotations

import json
from typing import Any

from scrapling.fetchers import Fetcher

DEFAULT_TIMEOUT = 30


class ScrapeError(RuntimeError):
    """Raised when a fetch returns a non-200 status."""

    def __init__(self, url: str, status: int | None) -> None:
        self.url = url
        self.status = status
        super().__init__(f"fetch failed [{status}]: {url}")


def fetch_bytes(url: str, *, headers: dict[str, str] | None = None,
                timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """GET ``url`` and return the raw response body, raising on non-200."""
    r = Fetcher.get(url, headers=headers or {}, stealthy_headers=True, timeout=timeout)
    if r.status != 200:
        raise ScrapeError(url, r.status)
    return r.body or b""


def fetch_text(url: str, *, headers: dict[str, str] | None = None,
               timeout: int = DEFAULT_TIMEOUT, encoding: str = "utf-8") -> str:
    """GET ``url`` and return the body decoded as text."""
    return fetch_bytes(url, headers=headers, timeout=timeout).decode(encoding, "replace")


def fetch_json(url: str, *, headers: dict[str, str] | None = None,
               timeout: int = DEFAULT_TIMEOUT) -> Any:
    """GET ``url`` and parse the body as JSON."""
    return json.loads(fetch_text(url, headers=headers, timeout=timeout))
