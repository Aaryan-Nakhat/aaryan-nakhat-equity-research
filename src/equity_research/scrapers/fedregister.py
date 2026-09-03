"""US Federal Register — the hard-primary anchor for the US leg of the 💨 Tailwind Scout.

The Federal Register is the official daily journal of the US federal government. Its public
API (``federalregister.gov/api/v1``) is free, JSON, no auth — and crucially exposes **proposed
rules** (PRORULE) with their comment periods, i.e. US policy *before* it takes effect and often
before it's in the news (the "coming in the next few days" the user originally asked for).

Scope note: this is **US-only** — it does NOT see China's MOFCOM export bans, the EU, or other
countries. Those come from Google News (``scrapers/social.py``). Federal Register is the
*complement*: an authoritative source + link for US export-control / tariff / critical-minerals
actions, and a forward look at proposed ones. Best-effort: ``[]`` on any failure.

Returned rows share the Scout's signal shape ``{title, url, source, published}`` so they merge
straight into the signal stream the Analyst triages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from equity_research.common.http import fetch_json

log = logging.getLogger("equity-research.tailwind")

_BASE = "https://www.federalregister.gov/api/v1/documents.json"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
_FIELDS = ["title", "abstract", "html_url", "publication_date", "type"]
# Full-text terms aimed at the supply-chokepoint surface — export controls, tariffs, the materials.
_TERMS = [
    "critical minerals export",
    "export control critical",
    "rare earth",
    "tungsten",
    "gallium germanium",
    "antimony OR graphite OR cobalt",
    "tariff critical minerals",
]


def _url(term: str, gte: str, per_page: int) -> str:
    parts = [f"per_page={per_page}", "order=newest",
             f"conditions[term]={quote(term)}",
             f"conditions[publication_date][gte]={gte}",
             "conditions[type][]=RULE", "conditions[type][]=PRORULE", "conditions[type][]=NOTICE"]
    parts += [f"fields[]={f}" for f in _FIELDS]
    return _BASE + "?" + "&".join(parts)


def recent_rules(*, days: int = 21, per_term: int = 6, max_items: int = 30) -> list[dict]:
    """Recent + proposed US Federal Register documents on the critical-material / export-control
    surface. ``[{title, url, source, published}]`` — ``source`` carries the document type (e.g.
    'US Federal Register (Proposed Rule)') so the Analyst can read status from it; a trimmed
    abstract is folded into the title for context. Deduped by URL. Best-effort; [] on failure."""
    gte = (datetime.utcnow().date() - timedelta(days=days)).isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for term in _TERMS:
        try:
            data = fetch_json(_url(term, gte, per_term), headers=_UA, timeout=15)
            results = data.get("results", []) if isinstance(data, dict) else []
        except Exception:  # noqa: BLE001 — official anchor is best-effort; news carries the rest
            continue
        for r in results:
            url = (r.get("html_url") or "").strip()
            title = (r.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            abstract = " ".join((r.get("abstract") or "").split())[:220]
            out.append({
                "title": f"{title} — {abstract}" if abstract else title,
                "url": url,
                "source": f"US Federal Register ({r.get('type', 'Document')})",
                "published": (r.get("publication_date") or "").strip(),
            })
            if len(out) >= max_items:
                log.info("fedregister: hit max_items=%d", max_items)
                return out
    log.info("fedregister: %d US official signals", len(out))
    return out
