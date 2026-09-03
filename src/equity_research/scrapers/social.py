"""The Scout — Tier ① of the 💨 Tailwind pipeline (see ``analysis/tailwind.py``).

Casts a wide net over recent, plain-HTTP, *global* signal sources for policy / supply
disruptions — export bans, quotas, tariffs, production cuts, sanctions, shortages, price
spikes — the kind of chokepoint move that hands a **tailwind** to alternative (esp. Indian
listed) producers. Two reliable workhorses:

* ``google_news(query)`` — Google News RSS search, ``when:<n>d`` recency-scoped. This is the
  breadth engine: it already surfaces official government / ministry announcements, wire
  stories and trade-press pieces for any targeted query, plain HTTP, no auth.
* ``reddit_search(query)`` — Reddit's public ``search.json`` (sort=new, t=week). The early /
  speculation layer — geopolitics & commodity chatter often flags a move before the wires.

Both are **best-effort**: any failure returns ``[]`` so the Scout degrades rather than
breaking the pipeline. Everything here is a raw *lead* carrying a source URL — the Analyst
(Tier ②) triages it and the Auditor (Tier ④) verifies any company before it reaches you.
Twitter/X is deliberately omitted (login-walled, aggressive anti-bot); Reddit + news carry
the load. The US Federal Register API is a planned Tier-① hardening (official upcoming rules).
"""

from __future__ import annotations

import html
import logging
import re
from xml.etree import ElementTree as ET

from equity_research.common.http import fetch_json, fetch_text

log = logging.getLogger("equity-research.tailwind")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# --- Google News RSS search (breadth; also carries govt/ministry announcements) ---
_GNEWS = ("https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en")
_BARE_ENTITY = re.compile(r"#(x?[0-9a-fA-F]+);")


def _clean(t: str) -> str:
    t = html.unescape(t or "")
    return _BARE_ENTITY.sub(lambda m: html.unescape("&" + m.group(0)), t).strip()


def _q(text: str) -> str:
    """URL-encode a news query (spaces → +, keep the when: operator readable)."""
    return re.sub(r"\s+", "+", text.strip())


def google_news(query: str, *, days: int = 14, limit: int = 12) -> list[dict]:
    """Recent Google-News items for ``query`` (recency-scoped to the last ``days``):
    ``[{title, url, source, published}]``. Empty on any failure. Never raises."""
    url = _GNEWS.format(q=_q(f"{query} when:{days}d"))
    try:
        root = ET.fromstring(fetch_text(url, headers=_UA, timeout=15))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        if not title:
            continue
        src = item.find("{*}source")
        out.append({
            "title": title,
            "url": (item.findtext("link") or "").strip(),
            "source": _clean(src.text) if src is not None and src.text else "Google News",
            "published": (item.findtext("pubDate") or "").strip(),
        })
        if len(out) >= limit:
            break
    return out


# --- Reddit public search (the early speculation layer) ---
# Reddit now 403s unauthenticated JSON from datacenter IPs; when we hit that we trip a
# process-lifetime circuit-breaker so the Scout doesn't burn ~15s per remaining query on a host
# that's clearly blocking us. Google News carries the load; Reddit is a best-effort bonus.
_REDDIT = "https://old.reddit.com/search.json?q={q}&sort=new&t=week&limit={n}"
_reddit_dead = False


def reddit_search(query: str, *, limit: int = 10) -> list[dict]:
    """Recent Reddit posts matching ``query`` (sort=new, this week):
    ``[{title, url, source, published, ups}]``. Empty on any failure (incl. rate-limit); trips a
    per-process circuit-breaker on the first block so later calls short-circuit instantly."""
    global _reddit_dead
    if _reddit_dead:
        return []
    url = _REDDIT.format(q=_q(query), n=limit)
    try:
        data = fetch_json(url, headers=_UA, timeout=8)
        children = data.get("data", {}).get("children", [])
    except Exception:  # noqa: BLE001
        _reddit_dead = True                                # blocked / rate-limited → stop trying
        log.info("scout: Reddit unavailable (blocked) — relying on Google News")
        return []
    out = []
    for c in children:
        d = c.get("data", {})
        title = _clean(d.get("title", ""))
        if not title:
            continue
        out.append({
            "title": title,
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
            "source": f"r/{d.get('subreddit', '?')}",
            "published": "",
            "ups": d.get("ups", 0),
        })
    return out


def scout(queries: list[str], *, days: int = 14, per_query: int = 8,
          with_reddit: bool = True, max_signals: int = 70) -> list[dict]:
    """Run the Scout: fan ``queries`` out over Google News (+ Reddit) and return a deduped,
    recency-scoped list of raw signals ``[{title, url, source, published}]``. Best-effort —
    a source that fails is simply skipped; the pipeline still runs on whatever came back."""
    seen: set[str] = set()
    out: list[dict] = []
    for q in queries:
        rows = google_news(q, days=days, limit=per_query)
        if with_reddit:
            rows += reddit_search(q, limit=max(3, per_query // 2))
        for r in rows:
            key = re.sub(r"[^a-z0-9]+", "", r["title"].lower())[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= max_signals:
                log.info("scout: hit max_signals=%d", max_signals)
                return out
    log.info("scout: %d raw signals from %d queries", len(out), len(queries))
    return out
