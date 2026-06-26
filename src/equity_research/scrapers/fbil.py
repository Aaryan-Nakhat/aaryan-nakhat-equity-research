"""FBIL (Financial Benchmarks India Pvt Ltd) — official daily benchmark rates.

FBIL is the RBI-recognised administrator for Indian financial benchmarks. Its
``/wasdm/`` endpoints serve plain-HTTP JSON (no browser tier needed). We use the
reference-rate feed for **USD/INR** (published ~1:30pm IST daily).

(The G-Sec par-yield feed only returns archive-file metadata, not inline yields,
so the 10-yr benchmark isn't wired yet — see ``docs/PLAN.md``.)
"""

from __future__ import annotations

import json

from equity_research.common.http import fetch_bytes

_REFRATES = "https://www.fbil.org.in/wasdm/refrates/fetch?authenticated=false"


def usd_inr() -> float | None:
    """Latest FBIL USD/INR reference rate (INR per 1 USD); None on any failure."""
    try:
        rows = json.loads(fetch_bytes(_REFRATES).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — best-effort, never break the scan
        return None
    for r in rows if isinstance(rows, list) else []:
        if "USD" in (r.get("subProdName") or "").upper():
            try:
                return round(float(r["rate"]), 2)
            except (TypeError, ValueError, KeyError):
                return None
    return None
