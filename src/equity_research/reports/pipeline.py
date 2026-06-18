"""Reusable report pipeline: ensure data → build brief → Gemini → full report.

Used by both the CLI (scripts/research_report.py) and the Telegram bot. Handles
on-demand ingestion so any NSE-listed symbol works, not just pre-ingested ones.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import duckdb

from equity_research.analysis import forensic, fundamentals, quant, valuation
from equity_research.analysis.alerts import _categorise
from equity_research.common.db import connect
from equity_research.common.http import fetch_bytes
from equity_research.reports import glossary
from equity_research.ingest import (ingest_annual_financials, ingest_financials,
                                    ingest_shareholding)
from equity_research.reports.brief import build_brief
from equity_research.reports.deep_brief import build_deep_brief
from equity_research.reports.synthesize import synthesize_thesis
from equity_research.scrapers import nse_api

CR = 1e7


def _latest_filing_pdf(symbol: str) -> str | None:
    """Newest results/concall filing PDF for ``symbol``, downloaded to a temp file
    so the report's Gemini call can read management commentary, guidance, and the
    contingent-liability / related-party notes. Returns the path, or None."""
    try:
        anns = nse_api.corporate_announcements_batch([symbol]).get(symbol) or []
    except Exception:  # noqa: BLE001
        return None

    def _dt(a):
        try:
            return datetime.strptime(a.get("an_dt", "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            return datetime.min

    for a in sorted(anns, key=_dt, reverse=True):
        att = (a.get("attchmntFile") or "").strip()
        title, _, _ = _categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                  str(a.get("hasXbrl", "")).lower() == "true")
        if att.lower().endswith(".pdf") and title in ("Results filed", "Concall / investor meet"):
            try:
                data = fetch_bytes(att)
            except Exception:  # noqa: BLE001
                return None
            fd, path = tempfile.mkstemp(suffix=".pdf")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return path
    return None


def _f(v, nd=0, pct=False) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}"


def ensure_ingested(symbol: str, con: duckdb.DuckDBPyConnection) -> bool:
    """Ingest financials for ``symbol`` if we don't have any yet. Returns True if
    data is available afterwards."""
    n = con.execute("SELECT COUNT(*) FROM financials WHERE symbol = ?", [symbol]).fetchone()[0]
    if n == 0:
        try:
            ingest_financials(symbol, con, period="Quarterly", max_filings=12)
            ingest_annual_financials(symbol, con, max_filings=8)
        except Exception:  # noqa: BLE001
            pass
        n = con.execute("SELECT COUNT(*) FROM financials WHERE symbol = ?", [symbol]).fetchone()[0]
    # best-effort: ensure a promoter-pledge snapshot exists (one browser fetch, cached)
    try:
        if con.execute("SELECT COUNT(*) FROM shareholding WHERE symbol = ?",
                       [symbol]).fetchone()[0] == 0:
            ingest_shareholding(symbol, con)
    except Exception:  # noqa: BLE001
        pass
    return n > 0


def generate_report(symbol: str, *, deep: bool = True, consolidated: bool = False,
                    pdf_path: str | None = None, target_shares: float | None = None,
                    synthesize: bool = True) -> str:
    """Full report (brief + Gemini analysis) for ``symbol``. Ingests on demand."""
    symbol = symbol.upper()
    con = connect()
    try:
        have = ensure_ingested(symbol, con)
        builder = build_deep_brief if deep else build_brief
        brief = builder(con, symbol, consolidated=consolidated, target_shares=target_shares)
    finally:
        con.close()
    if not have:
        return (f"No financial data could be ingested for **{symbol}** — it may not be "
                "NSE-listed, or the symbol is wrong.\n\n" + brief)
    if not synthesize:
        return brief
    tmp = None
    if pdf_path is None:                       # auto-fetch the latest results/concall filing
        pdf_path = tmp = _latest_filing_pdf(symbol)
    try:
        thesis = synthesize_thesis(brief, symbol, pdf_path=pdf_path, deep=deep)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return f"{brief}\n\n{'=' * 60}\n## Analysis\n\n{thesis}"


def report_summary(symbol: str, *, consolidated: bool = False) -> str:
    """A concise, deterministic (no-LLM) executive summary for the email body.

    Headline price/valuation, the Monte-Carlo DCF fair value + margin of safety,
    quality/forensic snapshot, and an at-a-glance red-flag list. The full deep
    report (tables + charts + Gemini analysis) goes in the attached PDF.
    """
    symbol = symbol.upper()
    con = connect()
    try:
        snap = valuation.snapshot(con, symbol, consolidated)
        t = fundamentals.ttm(con, symbol, consolidated)
        ov = fundamentals.annual_overview(con, symbol, consolidated)
        mcap = valuation.market_cap(con, symbol, consolidated)
        z = forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap)
        fsc = forensic.piotroski_f(con, symbol, consolidated=consolidated)
        m = forensic.beneish_m(con, symbol, consolidated=consolidated)
        acc = forensic.accruals(con, symbol, consolidated=consolidated)
        bf = quant.benford(con, symbol)
        inp = quant.dcf_inputs(con, symbol, consolidated)
        mc = quant.monte_carlo_dcf(inp) if inp.usable else None
        pl = con.execute(
            "SELECT pledged_pct_of_promoter FROM shareholding WHERE symbol = ? "
            "ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
        pledge = pl[0] if pl else None
        cfo_pat = float(ov["cfo_to_pat_x"].iloc[-1]) if not ov.empty else None

        L = [f"# {symbol} — summary"]
        if snap.get("price"):
            L.append(f"- **Price ₹{_f(snap.get('price'), 2)}** · mcap ₹{_f(snap.get('market_cap_cr'), 0)} cr "
                     f"· P/E(TTM) {_f(snap.get('pe_ttm'), 1)} · P/B {_f(snap.get('pb'), 2)}")
        if mc and mc.median and mc.price:
            if mc.price <= mc.median:
                mos = f"margin of safety **{_f(100 * (mc.median - mc.price) / mc.median, 0)}%**"
            else:
                mos = f"**{_f(mc.price / mc.median, 1)}x** the DCF median (no margin of safety)"
            L.append(f"- **DCF fair value ₹{_f(mc.median, 0)}** (p10–p90 ₹{_f(mc.p10, 0)}–{_f(mc.p90, 0)}) "
                     f"→ {mos}; P(undervalued) {_f(100 * mc.prob_undervalued, 0)}%")
        elif inp.is_financial:
            L.append("- DCF: skipped (financial/lender)")
        L.append(f"- Quality: ROA {_f(ov['roa_%'].iloc[-1] if not ov.empty else None, 1, pct=True)} "
                 f"· net margin {_f(t.get('ttm_net_margin_%'), 1, pct=True)} · CFO/PAT {_f(cfo_pat, 2)}x")
        L.append(f"- Forensic: Altman Z {_f(z.value, 2)} ({glossary.label('Altman Z', z.value) or 'n/a'}) · "
                 f"Piotroski {_f(fsc.value, 0)}/9 · Beneish M {_f(m.value, 2)} · "
                 f"Sloan accruals {_f(acc.value, 1, pct=True)} · "
                 f"pledge {_f(pledge, 1, pct=True)} of promoter "
                 f"({glossary.label('Pledge%', pledge) or 'n/a'})")

        flags = []
        if m.value is not None and m.value > -1.78:
            flags.append("Beneish M flags possible earnings manipulation")
        if z.value is not None and z.value < 1.81:
            flags.append("Altman Z in distress zone")
        if cfo_pat is not None and cfo_pat < 1.0:
            flags.append("CFO/PAT < 1 (profit not cash-backed)")
        if acc.value is not None and acc.value > 10:
            flags.append("high Sloan accruals")
        if pledge is not None and pledge > 20:
            flags.append(f"{_f(pledge, 0)}% of promoter holding pledged")
        if bf.get("flag"):
            flags.append("Benford nonconformity in reported figures")
        L.append("- **Red flags:** " + ("; ".join(flags) if flags else "none from the quant screens"))
        L.append("")
        L.append("_Full analysis — multi-year statements, forensic deep-dive, quant valuation "
                 "and charts — is in the attached PDF, which ends with a **Metric guide** "
                 "explaining what each number means, its typical range, and how to read it._")
        return "\n".join(L)
    finally:
        con.close()
