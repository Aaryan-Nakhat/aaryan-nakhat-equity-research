"""Reusable report pipeline: ensure data → build brief → Gemini → full report.

Used by both the CLI (scripts/research_report.py) and the Telegram bot. Handles
on-demand ingestion so any NSE-listed symbol works, not just pre-ingested ones.
"""

from __future__ import annotations

import duckdb

from equity_research.common.db import connect
from equity_research.ingest import ingest_annual_financials, ingest_financials
from equity_research.reports.brief import build_brief
from equity_research.reports.deep_brief import build_deep_brief
from equity_research.reports.synthesize import synthesize_thesis


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
    thesis = synthesize_thesis(brief, symbol, pdf_path=pdf_path, deep=deep)
    return f"{brief}\n\n{'=' * 60}\n## Analysis\n\n{thesis}"
