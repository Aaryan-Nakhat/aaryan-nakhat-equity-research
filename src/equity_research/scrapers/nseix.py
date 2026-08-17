"""NSE IX (GIFT City / IFSC) scraper — GIFT Nifty live quote.

GIFT Nifty (formerly SGX Nifty) is the Nifty-50 index future that trades on NSE
International Exchange for ~21 hours a day. Its overnight / early-morning level is
the single most-watched **lead indicator** for how the domestic Nifty will open.

The nseix.com site is a React SPA, but — unlike www.nseindia.com — its data API
answers plain HTTP (no Akamai challenge), so a simple GET is enough. The
``derivatives-watch`` feed lists every index future; we pick the **nearest-expiry
NIFTY** contract, which is the active GIFT Nifty. Best-effort: returns ``None`` on
any failure so a caller can degrade gracefully.
"""

from __future__ import annotations

from datetime import datetime

from equity_research.common.http import fetch_json

# Index-futures live watch (IDX+STK instrument classes). NIFTY FUTIDX rows are GIFT Nifty.
_URL = ("https://www.nseix.com/api/derivatives-watch"
        "?inst_type1=IDX&inst_type2=STK&type=live")
_HEADERS = {"Accept": "application/json",
            "Referer": "https://www.nseix.com/products/gift-nifty"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _expiry_key(s: str):
    """Sort key for an EXPIRYDATE like '25-Aug-2026' (earliest first)."""
    try:
        return datetime.strptime(s, "%d-%b-%Y")
    except (TypeError, ValueError):
        return datetime.max


def gift_nifty() -> dict | None:
    """Live GIFT Nifty (nearest-expiry NIFTY index future on NSE IX).

    Returns ``{last, change, pct_change, prev_close, open, high, low, expiry,
    timestamp}`` or ``None`` if the feed is unreachable / has no NIFTY row.
    ``change`` / ``pct_change`` are the contract's move vs its **own** previous
    close (``prev_close``). Never raises.
    """
    try:
        data = fetch_json(_URL, headers=_HEADERS, timeout=20)
    except Exception:  # noqa: BLE001 — best-effort lead indicator, never break the digest
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    nifty = [r for r in rows if str(r.get("SYMBOL", "")).upper() == "NIFTY"
             and str(r.get("INSTRUMENTTYPE", "")).upper() == "FUTIDX"]
    if not nifty:
        return None
    r = min(nifty, key=lambda x: _expiry_key(str(x.get("EXPIRYDATE", ""))))
    return {
        "last": _num(r.get("LASTPRICE")),
        "change": _num(r.get("CHANGE")),
        "pct_change": _num(r.get("PERCHANGE")),
        "prev_close": _num(r.get("CLOSE")),
        "open": _num(r.get("OPEN")),
        "high": _num(r.get("HIGH")),
        "low": _num(r.get("LOW")),
        "expiry": (r.get("EXPIRYDATE") or "").strip() or None,
        "timestamp": (r.get("TIMESTMP") or "").strip() or None,
    }
