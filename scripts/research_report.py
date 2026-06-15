"""End-to-end research report: assemble brief -> Claude thesis -> email.

    # just the quant brief (no API/email needed):
    uv run python scripts/research_report.py RELIANCE --dry-run --shares 1353.2

    # brief + Gemini synthesis, printed (needs Gemini/Vertex env — see .env.example):
    uv run python scripts/research_report.py RELIANCE --shares 1353.2

    # + attach a concall transcript / annual report PDF for the model to read:
    uv run python scripts/research_report.py RELIANCE --pdf transcript.pdf

    # + email it (needs SMTP_* env, see .env.example):
    uv run python scripts/research_report.py RELIANCE --email
"""

from __future__ import annotations

import sys

from equity_research.common.db import connect
from equity_research.reports.brief import build_brief
from equity_research.reports.deep_brief import build_deep_brief


def _arg(argv, flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ / arrows on Windows console
    if not argv:
        print("usage: research_report.py SYMBOL [--dry-run] [--consolidated] "
              "[--shares CR] [--pdf PATH] [--email]")
        return 2

    symbol = argv[0].upper()
    consolidated = "--consolidated" in argv
    dry_run = "--dry-run" in argv
    do_email = "--email" in argv
    deep = "--deep" in argv
    pdf = _arg(argv, "--pdf")
    shares = float(_arg(argv, "--shares")) * 1e7 if "--shares" in argv else None

    con = connect()
    try:
        builder = build_deep_brief if deep else build_brief
        brief = builder(con, symbol, consolidated=consolidated, target_shares=shares)
    finally:
        con.close()

    print(brief)
    if dry_run:
        print("\n[--dry-run: skipping Gemini synthesis and email]")
        return 0

    # Synthesis (needs Gemini/Vertex env — see .env.example).
    from equity_research.reports.synthesize import synthesize_thesis
    mode = "forensic deep-dive" if deep else "thesis"
    print("\n" + "=" * 60 + f"\nSynthesising {mode} with Gemini...\n")
    thesis = synthesize_thesis(brief, symbol, pdf_path=pdf, deep=deep)
    print(thesis)

    report = f"{brief}\n\n{'=' * 60}\n## Thesis\n\n{thesis}"
    if do_email:
        from equity_research.reports.email import send_report
        send_report(f"Research: {symbol}", report)
        print(f"\n[emailed report for {symbol}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
