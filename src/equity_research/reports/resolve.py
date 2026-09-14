"""Resolve a free-text company name/query to NSE trading symbol candidates.

Uses LLM + web-search grounding so it works for any listed name, including
small-cap and recently-listed companies (not limited to a local universe).
Returns up to 5 ranked candidates, or exactly one when the model is certain.
The caller disambiguates when there's more than one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from equity_research.common import llm

_RESOLVER_SYS = (
    "You map an Indian company name/query to its stock trading symbol(s) on NSE "
    "(preferred) or BSE. Use web search to find the correct tickers, including "
    "small-cap and recently-listed companies. Return the best matches, RANKED by "
    "relevance, as a JSON array of objects {\"symbol\":..., \"name\":..., "
    "\"exchange\":\"NSE\"|\"BSE\"}. The symbol must be the exact NSE trading symbol "
    "(e.g. RELIANCE, TCS, INFY). If you are fully sure of a single "
    "match, return exactly one element; otherwise return up to 5 plausible "
    "candidates. Reply with ONLY the JSON array, no prose."
)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    exchange: str = "NSE"


def resolve(query: str) -> list[Candidate]:
    """NSE symbol candidates for ``query`` via the LLM + web search (≤5)."""
    try:
        text = llm.generate(_RESOLVER_SYS, query, grounded=True)
    except Exception:  # noqa: BLE001
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)   # strip ```json fences / prose
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Candidate] = []
    for d in data if isinstance(data, list) else []:
        sym, name = d.get("symbol"), d.get("name")
        if sym and str(d.get("exchange", "NSE")).upper() == "NSE":   # analysis = NSE only
            out.append(Candidate(str(sym).upper(), name or str(sym), "NSE"))
    return out[:5]
