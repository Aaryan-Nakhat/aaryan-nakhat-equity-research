"""Employee & management sentiment from employer reviews (AmbitionBox) — the "inside view".

Turns ``scrapers.ambitionbox`` ratings into two banded scores on a fixed A→E scale, **leaning on the
gap vs the company's own industry** (a 3.9 in a 4.1 industry is worse than a 3.7 in a 3.4 industry):

* **Employee Sentiment** (morale) — overall rating + %positive/%detractor, 45% absolute / 55% peer-relative.
* **Management & Leadership Quality** — a weighted composite of culture, job-security, career-growth and
  work-satisfaction, nudged by the same peer-relative premium.

A **confidence gate** on review count means thin samples aren't over-read (<20 reviews → not scored).
Results are cached per symbol (~30 days) in ``alert_state`` so a deep report doesn't re-scrape every
time. A culture/governance *signal*, not a financial metric — shown as such in the report.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import duckdb

from equity_research import config
from equity_research.scrapers import ambitionbox

log = logging.getLogger("equity-research.employer")

_CACHE_KEY = "employer_sentiment"
_CACHE_TTL_DAYS = config.EMPLOYER_CACHE_TTL_DAYS


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


# ── the fixed scale ──
def _band(score: float | None) -> dict:
    """Map a 0-100 score to the A→E band (letter, emoji, label)."""
    if score is None:
        return {"letter": "—", "emoji": "", "label": "n/a"}
    if score >= 72:
        return {"letter": "A", "emoji": "🟢🟢", "label": "Excellent"}
    if score >= 60:
        return {"letter": "B", "emoji": "🟢", "label": "Strong"}
    if score >= 48:
        return {"letter": "C", "emoji": "🟡", "label": "Average"}
    if score >= 36:
        return {"letter": "D", "emoji": "🟠", "label": "Below-par"}
    return {"letter": "E", "emoji": "🔴", "label": "Poor"}


def _confidence(n: int) -> str:
    n = n or 0
    return ("High" if n >= 500 else "Medium" if n >= 100 else "Low" if n >= 20 else "Insufficient")


def _sentiment_score(overall: float, ind: float | None, det: float | None) -> float:
    absolute = _clamp((overall - 2.8) / 1.5 * 100)          # real range ~2.8-4.3 → 0-100
    gap = (overall - ind) if ind else 0.0
    relative = _clamp(50 + gap * 100)                        # +0.2 vs peers → 70; -0.2 → 30
    s = 0.45 * absolute + 0.55 * relative                   # lean on peer-relative
    if det and det > 25:                                    # a heavy detractor tail drags it down
        s -= 8
    return round(_clamp(s), 1)


def _mgmt_score(culture, job_sec, career, satisfaction, overall, ind) -> float | None:
    parts = [(culture, 0.40), (job_sec, 0.30), (career, 0.15), (satisfaction, 0.15)]
    parts = [(v, w) for v, w in parts if v]
    if not parts or overall is None:
        return None
    m = sum(v * w for v, w in parts) / sum(w for _, w in parts)
    absolute = _clamp((m - 2.8) / 1.5 * 100)
    gap = (overall - ind) if ind else 0.0
    relative = _clamp(50 + gap * 100)
    return round(_clamp(0.45 * absolute + 0.55 * relative), 1)


def _read(sent: str, mgmt: str, gap: float | None) -> str:
    """One plain-English interpretive line."""
    strong, weak = {"A", "B"}, {"D", "E"}
    if sent in strong and mgmt in strong:
        return ("A **well-regarded employer** — strong internal morale and leadership; a quiet quality "
                "signal that tends to travel with execution.")
    if sent in weak or mgmt in weak:
        g = (f" ({gap:+.2f} vs its industry)" if gap is not None else "")
        return ("**Weak internal sentiment**" + g + " — employees rate it below peers; watch for "
                "attrition, culture or execution risk behind the numbers.")
    return "**Middling internal sentiment** — broadly in line with its industry; neither a flag nor an edge."


def _score_from_raw(raw: dict) -> dict:
    """Compute the banded result from a raw AmbitionBox ``fetch_company`` dict (status 'ok')."""
    n = raw.get("reviews") or 0
    conf = _confidence(n)
    overall, ind = raw.get("overall"), raw.get("industry_avg")
    gap = (overall - ind) if (overall and ind) else None
    if n < 20 or overall is None:
        return {"status": "insufficient", "reviews": n, "overall": overall,
                "matched_name": raw.get("matched_name"), "confidence": conf,
                "as_of": datetime.now(timezone.utc).isoformat()}
    s = _sentiment_score(overall, ind, raw.get("pct_detractor"))
    m = _mgmt_score(raw.get("culture"), raw.get("job_security"), raw.get("career_growth"),
                    raw.get("work_satisfaction"), overall, ind)
    sb, mb = _band(s), _band(m)
    return {
        "status": "ok", "overall": overall, "industry_avg": ind, "gap": gap, "reviews": n,
        "confidence": conf, "ceo": raw.get("ceo"), "matched_name": raw.get("matched_name"),
        "sentiment_score": s, "sentiment": sb, "mgmt_score": m, "mgmt": mb,
        "pct_positive": raw.get("pct_positive"), "pct_detractor": raw.get("pct_detractor"),
        "subratings": {k: raw.get(k) for k in ("work_life", "salary", "job_security",
                       "career_growth", "skill_dev", "work_satisfaction", "culture")},
        "read": _read(sb["letter"], mb["letter"], gap),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ── cache (per-symbol, in alert_state) ──
def _cache_get(con: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    try:
        row = con.execute("SELECT value, updated_at FROM alert_state WHERE symbol=? AND key=?",
                          [symbol, _CACHE_KEY]).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    try:
        age_days = (datetime.now(timezone.utc) - row[1].replace(tzinfo=timezone.utc)).days
    except Exception:  # noqa: BLE001
        age_days = 999
    if age_days > _CACHE_TTL_DAYS:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _cache_put(con: duckdb.DuckDBPyConnection, symbol: str, result: dict) -> None:
    try:
        con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                    "VALUES (?, ?, ?, now())", [symbol, _CACHE_KEY, json.dumps(result)])
    except Exception:  # noqa: BLE001 — cache is a nicety, never break the report
        pass


def _resolve_name(con: duckdb.DuckDBPyConnection, symbol: str) -> str:
    """Readable company name for a symbol (equity_master, then sector_map), else the symbol itself."""
    for sql in ("SELECT company_name FROM equity_master WHERE symbol=?",
                "SELECT company FROM sector_map WHERE symbol=?"):
        try:
            r = con.execute(sql, [symbol]).fetchone()
            if r and r[0]:
                return str(r[0]).strip()
        except Exception:  # noqa: BLE001
            continue
    return symbol


def score(con: duckdb.DuckDBPyConnection, symbol: str, name: str | None = None, *,
          refresh: bool = False) -> dict:
    """Employer-sentiment result for ``symbol`` — cached (~30d) in ``alert_state``, else scraped fresh
    via AmbitionBox and scored. Returns ``{status: ok|insufficient|no_coverage|disabled, ...}``. Never
    raises."""
    if not refresh:
        hit = _cache_get(con, symbol)
        if hit:
            return hit
    raw = ambitionbox.fetch_company(name or _resolve_name(con, symbol), symbol)
    status = raw.get("status")
    if status == "disabled":
        return raw                                          # not cached — flag may flip on
    result = _score_from_raw(raw) if status == "ok" else {
        "status": "no_coverage", "as_of": datetime.now(timezone.utc).isoformat()}
    _cache_put(con, symbol, result)
    return result


# ── deep-report section ──
def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def _rev(n: int) -> str:
    return f"{n/1000:.1f}k" if n and n >= 1000 else str(n or 0)


def section_lines(con: duckdb.DuckDBPyConnection, symbol: str, name: str | None = None) -> list[str]:
    """The '🏢 Inside view' section for the deep report as markdown lines ([] when the tier is off)."""
    r = score(con, symbol, name)
    st = r.get("status")
    if st == "disabled":
        return []                                            # tier off → no section at all
    head = ["", "## 🏢 Inside view — employee & management sentiment", ""]
    if st == "no_coverage":
        return head + ["_n/a — no employer reviews found for this company on AmbitionBox._", ""]
    if st == "insufficient":
        nm = f" ({r.get('matched_name')})" if r.get("matched_name") else ""
        return head + [f"_n/a — too few employee reviews to score{nm} "
                       f"({r.get('reviews', 0)} review(s), need ≥20)._", ""]
    sb, mb = r["sentiment"], r["mgmt"]
    gap = r.get("gap")
    gap_txt = (f"**{gap:+.2f} vs industry** (peers {_fmt(r.get('industry_avg'))})" if gap is not None
               else "industry n/a")
    sub = r.get("subratings") or {}
    lines = head + [
        f"**Employee sentiment: {sb['emoji']} {sb['letter']} ({sb['label']})** · "
        f"**Management: {mb['emoji']} {mb['letter']} ({mb['label']})** — from employee reviews.",
        "",
        f"- Overall **{_fmt(r.get('overall'))}/5** · {gap_txt} · **{_rev(r.get('reviews'))} reviews** "
        f"({r.get('confidence','').lower()} confidence)"
        + (f" · CEO {r['ceo']}" if r.get("ceo") else ""),
        f"- Sub-ratings: work-life {_fmt(sub.get('work_life'))} · salary {_fmt(sub.get('salary'))} · "
        f"job-security {_fmt(sub.get('job_security'))} · career-growth {_fmt(sub.get('career_growth'))} · "
        f"culture {_fmt(sub.get('culture'))} · work-satisfaction {_fmt(sub.get('work_satisfaction'))} · "
        f"skill-dev {_fmt(sub.get('skill_dev'))}",
    ]
    if r.get("pct_positive") is not None:
        lines.append(f"- **{r['pct_positive']:.0f}% rated 4★+** · {r.get('pct_detractor', 0):.0f}% rated 1–2★")
    lines += [
        f"- {r.get('read','')}",
        "",
        "_Employee-reported (AmbitionBox) — a **culture/management signal, not a financial metric**. "
        "Each of the two scores is graded A→E, leaning on the gap vs the company's **own industry** "
        "(so beating/lagging peers matters more than the raw star rating). Verify against the source._",
        "",
    ]
    return lines
