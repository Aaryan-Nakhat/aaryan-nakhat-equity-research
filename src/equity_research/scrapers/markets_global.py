"""Overnight global-market context for the pre-market digest.

Three best-effort, plain-HTTP fetchers — each returns empty/None on failure so the
pre-market email degrades gracefully rather than not sending:

* ``overnight_indices()`` — US (S&P 500 / Nasdaq / Dow, closed overnight) and Asia
  (Nikkei / Hang Seng, trading into our morning) from Yahoo Finance's unauthenticated
  chart endpoint. The overnight global tape is what GIFT Nifty is reacting to.
* ``market_headlines()`` — Moneycontrol markets RSS, the raw headlines the LLM
  summarises into an overnight-drivers read.
* ``nifty_reference()`` — Nifty-50 spot **previous close** (the baseline for the
  implied-gap calc) and India VIX, from NSE's ``allIndices`` feed (plain HTTP, with a
  Camoufox fallback if Akamai blocks the plain GET that morning).
"""

from __future__ import annotations

import html
import re
from xml.etree import ElementTree as ET

from equity_research.common.http import fetch_json, fetch_text

# --- overnight global indices (Yahoo chart API, no auth needed) ---
_YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
_YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# (Yahoo symbol, display name, region) — US closed overnight, Asia live into our morning.
_INDICES = [
    ("^GSPC", "S&P 500", "US"),
    ("^IXIC", "Nasdaq", "US"),
    ("^DJI", "Dow Jones", "US"),
    ("^N225", "Nikkei 225", "Asia"),
    ("^HSI", "Hang Seng", "Asia"),
]


def _yahoo_quote(sym: str) -> dict | None:
    try:
        d = fetch_json(_YF.format(sym=sym.replace("^", "%5E")), headers=_YF_HEADERS, timeout=15)
        m = d["chart"]["result"][0]["meta"]
    except Exception:  # noqa: BLE001
        return None
    last, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose")
    if last is None or not prev:
        return None
    return {"last": float(last), "prev_close": float(prev),
            "pct": (float(last) - float(prev)) / float(prev) * 100}


def overnight_indices() -> list[dict]:
    """US + Asia index snapshots: ``[{name, region, last, prev_close, pct}, ...]``.
    Order preserved (US first, then Asia); indices that fail to fetch are skipped."""
    out = []
    for sym, name, region in _INDICES:
        q = _yahoo_quote(sym)
        if q:
            out.append({"name": name, "region": region, **q})
    return out


# --- overnight news headlines (Moneycontrol markets RSS) ---
_RSS = "https://www.moneycontrol.com/rss/marketreports.xml"
_RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# Non-article rows the feed carries (logo, self-link) — drop them.
_SKIP_TITLES = {"moneycontrol logo", "moneycontrol market reports"}
# Moneycontrol's feed is malformed: it emits numeric entities with the leading '&'
# stripped (``day#39;s`` instead of ``day&#39;s``), which html.unescape can't repair.
_BARE_ENTITY = re.compile(r"#(x?[0-9a-fA-F]+);")


def _clean_title(t: str) -> str:
    """Unescape HTML entities, repairing the feed's bare (``&``-less) numeric ones."""
    t = html.unescape(t)                                   # proper entities first
    t = _BARE_ENTITY.sub(lambda m: html.unescape("&" + m.group(0)), t)  # then bare ones
    return t.strip()


def market_headlines(limit: int = 15) -> list[dict]:
    """Recent market headlines from Moneycontrol RSS: ``[{title, link, published}]``.
    Empty list on any failure. Titles are HTML-unescaped and de-duplicated."""
    try:
        xml = fetch_text(_RSS, headers=_RSS_HEADERS, timeout=15)
        root = ET.fromstring(xml)
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for item in root.iter("item"):
        title = _clean_title(item.findtext("title") or "")
        key = title.lower()
        if not title or key in _SKIP_TITLES or key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
        })
        if len(out) >= limit:
            break
    return out


# --- Nifty-50 spot reference + India VIX (NSE allIndices) ---
_ALLIDX = "https://www.nseindia.com/api/allIndices"
_NSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/"}


def _extract_ref(data) -> dict | None:
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None

    def find(name):
        return next((r for r in rows if (r.get("index") or "").strip().upper() == name), None)

    n50, vix = find("NIFTY 50"), find("INDIA VIX")
    if not n50:
        return None

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return {
        "nifty_prev_close": num(n50.get("previousClose")),
        "nifty_last": num(n50.get("last")),
        "vix": num(vix.get("last")) if vix else None,
        "vix_pct": num(vix.get("percentChange")) if vix else None,
    }


def nifty_reference() -> dict | None:
    """Nifty-50 previous close (implied-gap baseline) + India VIX. Tries plain HTTP
    first; falls back to the Camoufox ``allIndices`` fetch if Akamai blocks the GET.
    ``None`` if both fail."""
    try:
        ref = _extract_ref(fetch_json(_ALLIDX, headers=_NSE_HEADERS, timeout=15))
        if ref:
            return ref
    except Exception:  # noqa: BLE001 — fall through to the browser tier
        pass
    try:  # heavier fallback: solve the bot challenge in a real browser
        from equity_research.scrapers import nse_api
        idx = nse_api.live_indices()  # {NAME_UPPER: (last, pct)}
        n50 = idx.get("NIFTY 50")
        vix = idx.get("INDIA VIX")
        if n50:
            # live_indices gives (last, pct); previousClose ≈ last / (1 + pct/100)
            last, pct = n50
            prev = last / (1 + pct / 100) if pct not in (None, 0) else last
            return {"nifty_prev_close": prev, "nifty_last": last,
                    "vix": vix[0] if vix else None, "vix_pct": vix[1] if vix else None}
    except Exception:  # noqa: BLE001
        pass
    return None
