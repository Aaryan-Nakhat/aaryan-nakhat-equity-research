"""AmbitionBox employer-review scraper (browser tier) — the "inside view" data source.

AmbitionBox (Naukri / InfoEdge) is India's largest employer-review site, with far deeper coverage of
Indian listed companies than Glassdoor. Its pages are a Next.js app behind bot protection, so — like
``nse_api`` and ``trends`` — this uses the **browser tier** (Camoufox / ``StealthyFetcher``): it loads
the real company page and reads the structured ratings straight out of the embedded ``__NEXT_DATA__``
JSON (overall rating, 7 category sub-ratings, a 1-5★ distribution, review count, the **industry
average** for peer benchmarking, and the CEO name).

**Entity resolution** is the one hard part (a stock's name must map to the right AmbitionBox company,
not a same-named namesake). Strategy, most-reliable first: a verified ``_SLUG_MAP`` override → the
direct name-derived slug (trusted, since we built it from the name) → the search API with a strict
name-match guard (to avoid namesakes like example finance→a same-named giant). If nothing resolves confidently it returns
``no_coverage`` — an honest blank beats a wrong company.

Automated access to AmbitionBox is subject to its Terms of Service; this is a **personal-research**
tool and the tier is **opt-in** (``EMPLOYER_REVIEWS_ENABLED=true``), degrading to ``disabled`` when off.
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("equity-research.employer")

_HOME = "https://www.ambitionbox.com/"
_OVERVIEW = "https://www.ambitionbox.com/overview/{slug}-overview"

# Verified symbol → AmbitionBox slug, for cases the fuzzy search mis-resolves or the name-slug misses
# (namesakes, "-ltd" suffixes, brand ≠ legal name). Extend as coverage widens.
_SLUG_MAP: dict[str, str] = {
    "example energy": "example energy-energy",            # search picks a 7-review namesake; this is example energy Group
    "example financeFIN": "example financeleasing-finance",    # AmbitionBox name differs from "example finance"
    "example retail": "example retail",                     # brand slug, not "avenue-supermarts"
    "example fintech": "example fintech",                     # brand slug, not the legal "example ventures"
    "EXMOT": "tata-motors", "EXMOT": "tata-motors",
    "EXPEQ": "genus-power-infrastructures-ltd",
    "example infra": "example infra-india",
}

_LEGAL = {"ltd", "limited", "corporation", "corp", "the", "co", "company", "group", "india",
          "incorporated", "inc", "and", "&", "of", "pvt", "private"}
# generic industry words that alone don't identify a company (a shared one isn't a real match)
_INDUSTRY = {"steel", "power", "energy", "finance", "financial", "services", "motors", "pharma",
             "pharmaceuticals", "bank", "tech", "technologies", "industries", "petroleum", "tyres",
             "logistics", "beverages", "infrastructures", "infrastructure", "cement", "paper",
             "chemicals", "textiles", "auto", "green", "solar", "laboratories", "lifescience"}


def _enabled() -> bool:
    return os.environ.get("EMPLOYER_REVIEWS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _toks(s: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if t}


def _clean_slug(name: str) -> str:
    s = re.sub(r"\b(ltd|limited|corporation|corp|the)\b", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _name_match(query: str, cand_name: str) -> bool:
    """Guard for SEARCH results only: the candidate must share a real BRAND token with the query and
    cover ≥50% of the query's identifying tokens — so 'example tubes' won't accept 'example steel'."""
    q = _toks(query) - _LEGAL
    c = _toks(cand_name) - _LEGAL
    if not q:
        return False
    brand_q = q - _INDUSTRY
    if brand_q and not (brand_q & c):        # must share a non-generic brand word
        return False
    return len(q & c) / len(q) >= 0.5


# in-page fetch of AmbitionBox's company-search API (returns the authoritative slug in `url`)
_SEARCH_JS = ("async (q) => { const r = await fetch("
              "'/api/v2/search?category=company&query=' + encodeURIComponent(q), "
              "{headers:{accept:'application/json'}}); return await r.text(); }")


def _extract(nd: str) -> dict | None:
    """Pull the ratings out of a company page's ``__NEXT_DATA__`` JSON. ``None`` if it isn't a real
    rated company page."""
    try:
        pp = json.loads(nd)["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return None
    meta = pp.get("companyMetaInformation") or {}
    d = ((pp.get("aggregatedRatingsData") or {}).get("ratingDistribution") or {}).get("data") or {}
    r2 = d.get("ratingsTwoDecimal") or {}
    dist = d.get("distribution") or []
    overall = _num(r2.get("overallCompanyRating")) or _num(meta.get("rating"))
    reviews = d.get("totalCount") or 0
    if overall is None and not reviews:
        return None
    return {
        "matched_name": (meta.get("name") or pp.get("companyName") or "").strip(),
        "overall": overall,
        "industry_avg": _num(meta.get("industryRating")) or _num(d.get("industryAverage")),
        "reviews": reviews,
        "ceo": (meta.get("ceo") or "").strip(),
        "pct_positive": round(sum(x["percentage"] for x in dist if x["rating"] >= 4), 1) if dist else None,
        "pct_detractor": round(sum(x["percentage"] for x in dist if x["rating"] <= 2), 1) if dist else None,
        "work_life": _num(r2.get("workLifeRating")),
        "salary": _num(r2.get("compensationBenefitsRating")),
        "job_security": _num(r2.get("jobSecurityRating")),
        "career_growth": _num(r2.get("careerGrowthRating")),
        "skill_dev": _num(r2.get("skillDevelopmentRating")),
        "work_satisfaction": _num(r2.get("workSatisfactionRating")),
        "culture": _num(r2.get("companyCultureRating")),
    }


def fetch_company(name: str, symbol: str | None = None) -> dict:
    """Resolve a company to AmbitionBox and return its employer ratings (see ``_extract``) plus the
    ``slug`` used. Returns ``{"status": "disabled"}`` when the tier is off, ``{"status":
    "no_coverage"}`` when nothing resolves, or ``{"status": "ok", ...ratings...}``. Best-effort —
    never raises; a browser/parse failure returns ``no_coverage``."""
    if not _enabled():
        return {"status": "disabled"}
    from scrapling.fetchers import StealthyFetcher

    direct = [s for s in [(_SLUG_MAP.get((symbol or "").upper())), _clean_slug(name)] if s]
    result: dict = {"status": "no_coverage"}

    def _action(page):
        def _try(slug: str, guard_name: str | None) -> dict | None:
            try:
                page.goto(_OVERVIEW.format(slug=slug), wait_until="domcontentloaded", timeout=30000)
                nd = page.eval_on_selector("#__NEXT_DATA__", "e => e.textContent")
            except Exception:  # noqa: BLE001
                return None
            data = _extract(nd)
            if not data or not (data.get("reviews") or 0):
                return None
            if guard_name and not _name_match(guard_name, data["matched_name"]):
                return None                                  # search hit failed the name guard
            data["slug"] = slug
            return data

        # 1) direct slugs (verified map / name-derived) — trusted, no name-guard
        for slug in dict.fromkeys(direct):
            d = _try(slug, guard_name=None)
            if d:
                result.update(status="ok", **d)
                return page
        # 2) search fallback — strict name-guard against namesakes
        try:
            body = page.evaluate(_SEARCH_JS, name)
            cands = json.loads(body).get("data") or []
        except Exception:  # noqa: BLE001
            cands = []
        best, best_key = None, (-1.0, -1)
        q = _toks(name) - _LEGAL
        for c in cands[:8]:
            cn = c.get("name", "")
            if not _name_match(name, cn):
                continue
            frac = len(q & (_toks(cn) - _LEGAL)) / len(q) if q else 0
            key = (frac, c.get("reviewCount") or 0)
            if key > best_key and c.get("url"):
                best, best_key = c, key
        if best:
            d = _try(best["url"], guard_name=name)
            if d:
                result.update(status="ok", **d)
        return page

    try:
        StealthyFetcher.fetch(_HOME, headless=True, network_idle=True, page_action=_action)
    except Exception:  # noqa: BLE001 — browser launch/fetch failed → honest no-coverage
        log.info("employer: AmbitionBox browser fetch failed for %s", symbol or name)
    return result
