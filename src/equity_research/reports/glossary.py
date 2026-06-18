"""Plain-English interpretation for the numbers in reports and alerts.

Every metric we surface should be self-explaining: *what it is*, *typical /
benchmark values*, and *how to read the current value* — with a sector caveat
where the "normal" range shifts by business model. This module is the
deterministic backbone (consistent, no LLM); the Gemini narrative then adds the
company-specific, sector-aware reading on top.

- ``read(key, value)``  -> short inline tag, e.g. "9.5% — fair"
- ``guide(keys)``       -> a Markdown "Metric guide" appendix (what/typical/how)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metric:
    what: str                                    # one-line definition
    typical: str                                 # benchmark / typical values
    bands: list[tuple[float, str]] = field(default_factory=list)  # (threshold, label), high→low
    lower_better: bool = False                   # bands read as "below threshold is better"
    sector_note: str = ""                        # how the normal range shifts by sector


# Headline metrics we judge. Keys match how callers label values.
GLOSSARY: dict[str, Metric] = {
    "P/E": Metric(
        "Price ÷ trailing earnings — rupees paid per ₹1 of annual profit.",
        "Broad market ~20–25x; <15x cheap, >40x demands high growth. Most useful vs the company's OWN history and its sector.",
        [(40, "rich"), (25, "full"), (15, "moderate")], lower_better=True,
        sector_note="Structurally higher for fast-growers (FMCG, consumer); lower for cyclicals/commodities."),
    "P/B": Metric(
        "Price ÷ book value (net worth) per share.",
        "<1x = below book; 1–3x typical; >5x means heavy intangible/brand or very high returns.",
        [(5, "rich"), (3, "full"), (1, "moderate")], lower_better=True,
        sector_note="Banks/financials judged largely on P/B; asset-light businesses run high P/B legitimately."),
    "ROE%": Metric(
        "Return on equity — net profit ÷ shareholders' equity.",
        "≥18% strong · 12–18% decent · <10% weak.",
        [(18, "strong"), (12, "decent"), (8, "fair")],
        sector_note="Leverage flatters ROE — pair with ROCE; banks/NBFCs run high ROE on leverage."),
    "ROCE%": Metric(
        "Return on capital employed — EBIT ÷ (equity + debt); efficiency of ALL capital.",
        "≥20% excellent · 15–20% good · 10–15% fair · <10% weak.",
        [(20, "excellent"), (15, "good"), (10, "fair")],
        sector_note="Capital-heavy sectors (utilities, telecom, oil & gas, infra) structurally lower — judge vs peers, not the absolute bar."),
    "ROIC%": Metric(
        "Return on invested capital — after-tax EBIT ÷ (equity + debt − cash).",
        "Compare to WACC: ROIC > WACC = value-creating; below = destroying.",
        [(20, "excellent"), (15, "good"), (10, "fair")],
        sector_note="The cleanest cross-sector quality gauge; the spread over WACC is what matters."),
    "NetMargin%": Metric(
        "Net profit ÷ revenue — rupees of profit per ₹100 of sales.",
        "Varies hugely by industry; trend and stability matter more than the absolute level.",
        [(20, "high"), (10, "healthy"), (4, "thin")],
        sector_note="Software/branded consumer run 15–25%; retail/distribution/commodities run low-single-digit by design."),
    "EBITDA margin%": Metric(
        "Operating profit before interest/tax/depreciation ÷ revenue.",
        "Sector-bound; watch the trend — falling margins signal pricing/cost pressure.",
        [(25, "high"), (15, "healthy"), (8, "thin")]),
    "D/E": Metric(
        "Total debt ÷ equity — leverage.",
        "<0.5 conservative · 0.5–1 moderate · 1–2 high · >2 stretched.",
        [(2, "stretched"), (1, "high"), (0.5, "moderate")], lower_better=True,
        sector_note="Capital-intensive/financial businesses carry more debt normally; net-debt and interest cover matter more than the raw ratio."),
    "Net debt/EBITDA": Metric(
        "Net debt ÷ EBITDA — years of operating profit to clear net debt.",
        "<1x comfortable · 1–3x manageable · >3x stretched · >4–5x risky.",
        [(4, "risky"), (3, "stretched"), (1, "manageable")], lower_better=True),
    "Interest cover": Metric(
        "EBIT ÷ interest cost — how many times profit covers interest.",
        "≥6x comfortable · 3–6x adequate · <3x stressed · <1.5x danger.",
        [(6, "comfortable"), (3, "adequate"), (1.5, "stressed")]),
    "Current ratio": Metric(
        "Current assets ÷ current liabilities — short-term liquidity.",
        "~1.5–2x healthy; <1 means short-term obligations exceed liquid assets.",
        [(2, "strong"), (1.2, "adequate"), (1, "tight")]),
    "CCC (days)": Metric(
        "Cash conversion cycle = receivable + inventory − payable days; cash tied up in operations.",
        "Lower is better; negative (paid by suppliers before collecting) is excellent.",
        [(120, "heavy"), (60, "moderate"), (0, "lean")], lower_better=True,
        sector_note="Retail/consumer often run low or negative; capital-goods/EPC run long cycles normally."),
    "CFO/PAT": Metric(
        "Operating cash flow ÷ net profit — is reported profit backed by cash?",
        "≥1x healthy over time; persistently <1 is an earnings-quality red flag.",
        [(1.0, "cash-backed"), (0.8, "watch")],
        sector_note="Lumpy for project/working-capital-heavy businesses — read the 3/5-yr rolled figure, not one year."),
    "CFO/EBITDA%": Metric(
        "Operating cash flow ÷ EBITDA — operating-profit-to-cash conversion.",
        "≥70% healthy; low conversion points to working-capital drag.",
        [(80, "strong"), (60, "fair")]),
    "Sloan accruals%": Metric(
        "Balance-sheet accruals ÷ avg assets — the non-cash part of earnings.",
        "Near zero/negative is good; high positive (>10%) = profit not cash-backed (low-accrual firms historically outperform).",
        [(10, "aggressive"), (5, "elevated")], lower_better=True),
    "Altman Z": Metric(
        "Bankruptcy-distance score from 5 ratios.",
        ">2.99 safe · 1.81–2.99 grey · <1.81 distress.",
        [(2.99, "safe"), (1.81, "grey")],
        sector_note="Calibrated for manufacturers; asset-light/financial firms can score oddly — treat as one input."),
    "Piotroski F": Metric(
        "9-point fundamental-strength checklist (profitability, leverage, efficiency).",
        "8–9 strong · 4–7 middling · 0–2 weak.",
        [(8, "strong"), (4, "middling")]),
    "Beneish M": Metric(
        "8-variable earnings-manipulation score.",
        "> −1.78 flags possible manipulation; more negative = cleaner.",
        [(-1.78, "flag")], lower_better=True,
        sector_note="A statistical screen, not proof — corroborate with accruals, receivables and cash conversion."),
    "Benford MAD": Metric(
        "Deviation of reported figures' leading digits from Benford's law.",
        "<0.006 close · 0.006–0.012 acceptable · 0.012–0.015 marginal · >0.015 nonconformity.",
        [(0.015, "nonconformity"), (0.012, "marginal"), (0.006, "acceptable")], lower_better=True,
        sector_note="A rounding/manipulation tell on the whole filing history — a flag to dig, not a verdict."),
    "Margin of safety%": Metric(
        "Discount of price to the DCF median fair value.",
        ">30% large cushion · 10–30% some · <0% trading above fair value.",
        [(30, "large"), (10, "some"), (0, "slim")]),
    "P(undervalued)%": Metric(
        "Share of Monte-Carlo DCF runs where fair value exceeds the price.",
        ">70% odds favour value · ~50% a coin toss · <30% richly priced on DCF.",
        [(70, "favourable"), (50, "balanced"), (30, "unfavourable")]),
    "Implied growth%": Metric(
        "Revenue growth the current price implies (reverse DCF).",
        "Compare to history: at/below past growth = undemanding; well above = priced for acceleration.",
        []),
    "Promoter holding%": Metric(
        "Share of equity held by promoters (founders/controlling group).",
        "Higher promoter skin-in-the-game is generally reassuring; very low can mean dispersed control.",
        [(50, "high"), (26, "meaningful")]),
    "Pledge%": Metric(
        "Promoter shares pledged as collateral, as % of promoter holding.",
        "0% ideal · <10% minor · 10–25% watch · >50% serious red flag (forced-sale / leverage risk).",
        [(50, "severe"), (25, "elevated"), (10, "minor")], lower_better=True),
    "Sector z-score": Metric(
        "How many standard deviations a ratio sits from the peer mean.",
        "|z|<1 in line with peers · 1–2 notable · >2 an outlier (good or bad depending on the ratio).",
        []),
}


def _band(m: Metric, value: float) -> str | None:
    if not m.bands or value is None or value != value:
        return None
    if m.lower_better:
        for thr, label in m.bands:          # bands high→low; first threshold exceeded = worst
            if value >= thr:
                return label
        return "good"
    for thr, label in m.bands:
        if value >= thr:
            return label
    return "weak"


def read(key: str, value: float | None, *, nd: int = 1, pct: bool = False,
         suffix: str = "") -> str:
    """Concise inline tag for a value, e.g. ``"9.5% — fair"``. Empty if unknown."""
    if value is None or value != value:
        return ""
    m = GLOSSARY.get(key)
    txt = f"{value:,.{nd}f}{'%' if pct else ''}{suffix}"
    label = _band(m, value) if m else None
    return f"{txt} — {label}" if label else txt


def label(key: str, value: float | None) -> str:
    """Just the band word (e.g. ``"fair"``), or ""."""
    m = GLOSSARY.get(key)
    return (_band(m, value) or "") if (m and value is not None) else ""


def guide_markdown() -> str:
    """The full Metric guide (every metric) as a standalone Markdown document —
    what each is, typical values, and how to read it."""
    lines = ["# Metric guide",
             "_What each metric in the reports means, its typical/benchmark values, and how "
             "to read it — judge sector-relative where noted._\n"]
    for k, m in GLOSSARY.items():
        note = f" _Sector:_ {m.sector_note}" if m.sector_note else ""
        lines.append(f"- **{k}** — {m.what} _Typical:_ {m.typical}{note}")
    return "\n".join(lines)


_GUIDE_PDF: bytes | None = None


def guide_pdf() -> bytes:
    """The Metric guide as a constant PDF (built once, cached — it never changes)."""
    global _GUIDE_PDF
    if _GUIDE_PDF is None:
        from equity_research.reports.pdf import report_to_pdf
        _GUIDE_PDF = report_to_pdf(guide_markdown(), "Metric guide")
    return _GUIDE_PDF
