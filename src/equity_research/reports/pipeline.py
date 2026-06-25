"""Reusable report pipeline: ensure data → build brief → Gemini → full report.

Used by both the CLI (scripts/research_report.py) and the Telegram bot. Handles
on-demand ingestion so any NSE-listed symbol works, not just pre-ingested ones.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from equity_research.analysis import forensic, fundamentals, quant, sector, valuation
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


def _last_fy_end() -> date:
    """Most recent fiscal year-end (31-Mar) on or before today."""
    today = date.today()
    yr = today.year if today >= date(today.year, 3, 31) else today.year - 1
    return date(yr, 3, 31)


def _doc_score(title: str, blob: str, is_result: bool) -> int:
    """Content richness 1-5 — used to prioritise which filings to read under the cap."""
    if "transcript" in blob:
        return 5
    if any(k in blob for k in ("investor presentation", "earnings presentation",
                               "results presentation", "analyst presentation")):
        return 4
    if "annual report" in blob:
        return 4
    if is_result or "financial result" in blob:
        return 3
    if title in ("Scheme / M&A", "Open offer / SAST", "Rights issue", "QIP / fund raising",
                 "Credit rating update", "Order / contract win", "Acquisition / disposal"):
        return 2
    return 1


def _filings_for_analysis(symbol: str, *, max_docs: int = 12,
                          max_bytes: int = 15_000_000) -> list[tuple[str, bytes]]:
    """All meaningful filing PDFs for ``symbol`` since the last fiscal year-end
    (plus the latest results, even if older), richest-first, capped by count and
    total size. Returns [(label, pdf-bytes)] for the report's Gemini call. Generic
    — works for any NSE-listed symbol; never raises."""
    try:
        anns = nse_api.corporate_announcements_batch([symbol]).get(symbol) or []
    except Exception:  # noqa: BLE001
        return []

    def _dt(a):
        try:
            return datetime.strptime(a.get("an_dt", "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
        except (ValueError, TypeError):
            return datetime.min

    anns = sorted(anns, key=_dt, reverse=True)          # newest first
    fy = _last_fy_end()
    cands: list[tuple[int, datetime, str, str]] = []    # (score, dt, label, url)
    latest_results = None
    for a in anns:
        att = (a.get("attchmntFile") or "").strip()
        if not att.lower().endswith(".pdf"):
            continue
        title, _, is_result = _categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                          str(a.get("hasXbrl", "")).lower() == "true")
        if title is None:                                # routine noise — skip
            continue
        adt = _dt(a)
        blob = f"{a.get('desc', '')} {a.get('attchmntText', '')}".lower()
        label = f"{title} · {adt:%d-%b-%Y}"
        if is_result and latest_results is None:
            latest_results = (_doc_score(title, blob, is_result), adt, label, att)
        if adt.date() >= fy:
            cands.append((_doc_score(title, blob, is_result), adt, label, att))
    if latest_results and not any(c[3] == latest_results[3] for c in cands):
        cands.append(latest_results)                     # ensure the latest results doc is in
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)  # richest, then newest

    out: list[tuple[str, bytes]] = []
    total = 0
    seen: set[str] = set()
    for _score, _adt, label, url in cands:
        if url in seen or len(out) >= max_docs:
            continue
        seen.add(url)
        try:
            data = fetch_bytes(url)
        except Exception:  # noqa: BLE001
            continue
        if total + len(data) > max_bytes:                 # stay under Gemini's inline request limit
            continue
        out.append((label, data))
        total += len(data)
    return out


def _f(v, nd=0, pct=False) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}"


def _expected_latest_quarter_end(today: date) -> date:
    """Most recent Mar/Jun/Sep/Dec quarter-end that should already be *filed* — i.e.
    at least ~75 days old (SEBI's 45-day quarterly / 60-day annual deadline + slack)."""
    cutoff = today - timedelta(days=75)
    ends = [date(cutoff.year, 3, 31), date(cutoff.year, 6, 30),
            date(cutoff.year, 9, 30), date(cutoff.year, 12, 31),
            date(cutoff.year - 1, 12, 31)]
    return max(d for d in ends if d <= cutoff)


def _financials_stale(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """True if we hold no quarterly rows, or the newest one predates the quarter that
    should already have been filed by now (the staleness that left FY2024 in a 2026 report)."""
    row = con.execute("SELECT max(period_end) FROM financials "
                      "WHERE symbol = ? AND period_type = 'Q'", [symbol]).fetchone()
    latest = row[0] if row else None
    return latest is None or latest < _expected_latest_quarter_end(date.today())


def _shareholding_stale(con: duckdb.DuckDBPyConnection, symbol: str, days: int = 80) -> bool:
    """True if there's no pledge snapshot, or it's older than ~a quarter."""
    row = con.execute("SELECT max(updated_at) FROM shareholding WHERE symbol = ?", [symbol]).fetchone()
    ts = row[0] if row else None
    if ts is None:
        return True
    try:
        return (datetime.now() - ts).days >= days
    except TypeError:
        return True


def _refresh_attempted_recently(con: duckdb.DuckDBPyConnection, symbol: str, days: int = 2) -> bool:
    """Cooldown: did we already try to refresh this symbol within ``days``? Avoids
    re-hitting NSE on every report when a newer filing genuinely isn't out yet."""
    row = con.execute("SELECT value FROM alert_state WHERE symbol = ? AND key = 'fin_refresh'",
                      [symbol]).fetchone()
    if not row:
        return False
    try:
        return (date.today() - date.fromisoformat(row[0])).days < days
    except (ValueError, TypeError):
        return False


def _mark_refresh_attempt(con: duckdb.DuckDBPyConnection, symbol: str) -> None:
    con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                "VALUES (?, 'fin_refresh', ?, now())", [symbol, date.today().isoformat()])


def ensure_ingested(symbol: str, con: duckdb.DuckDBPyConnection) -> bool:
    """Ensure ``symbol`` has *fresh* financials + a pledge snapshot. Re-ingests when
    our latest filing is stale (not just when empty), behind a per-symbol cooldown so
    repeat requests don't hammer NSE. Returns True if financial data is available."""
    have = con.execute("SELECT COUNT(*) FROM financials WHERE symbol = ?", [symbol]).fetchone()[0] > 0
    need_fin = not have or _financials_stale(con, symbol)
    need_sh = _shareholding_stale(con, symbol)
    if (need_fin or need_sh) and not _refresh_attempted_recently(con, symbol):
        if need_fin:
            try:  # idempotent upsert — re-lands the latest filings, appends any new period
                ingest_financials(symbol, con, period="Quarterly", max_filings=12)
                ingest_annual_financials(symbol, con, max_filings=8)
            except Exception:  # noqa: BLE001
                pass
        if need_sh:
            try:  # one browser fetch, cached
                ingest_shareholding(symbol, con)
            except Exception:  # noqa: BLE001
                pass
        _mark_refresh_attempt(con, symbol)
        have = con.execute("SELECT COUNT(*) FROM financials WHERE symbol = ?", [symbol]).fetchone()[0] > 0
    return have


def _ensure_peer_financials(con: duckdb.DuckDBPyConnection, symbol: str, cap: int = 6) -> None:
    """Best-effort: ingest ANNUAL financials for up to ``cap`` same-sector peers that
    have none yet, so the peer-comparison table has real comparables (peer P/B, ROE,
    ROCE, net-margin, D/E come from annual statements + the market-wide EOD price we
    already hold). Annual-only keeps it bounded; cached for future reports; never raises."""
    try:
        peers = sector.peers(con, symbol)
    except Exception:  # noqa: BLE001
        return
    done = 0
    for ps in peers:
        if done >= cap:
            break
        try:
            if con.execute("SELECT COUNT(*) FROM financials WHERE symbol = ? AND period_type = 'Y'",
                           [ps]).fetchone()[0]:
                continue                       # already have annual data for this peer
            if ingest_annual_financials(ps, con, max_filings=8):
                done += 1
        except Exception:  # noqa: BLE001 — one bad peer shouldn't break the report
            continue


def _prefer_consolidated(con: duckdb.DuckDBPyConnection, symbol: str) -> bool:
    """Auto-pick consolidated when it exists AND subsidiaries add materially — i.e.
    consolidated revenue or PAT is ≥25% larger than standalone (RIL's Jio/Retail,
    Tata Motors' JLR, etc.). Else standalone (where the two are ~equal)."""
    cons = fundamentals.load_annual(con, symbol, consolidated=True)
    if cons.empty:
        return False
    std = fundamentals.load_annual(con, symbol, consolidated=False)
    if std.empty:
        return True

    def latest(df, k):
        if k not in df.columns:
            return None
        s = df[k].dropna()
        return float(s.iloc[-1]) if len(s) else None

    for k in ("ProfitLossForPeriod", "RevenueFromOperations"):
        c, s = latest(cons, k), latest(std, k)
        if c and s and s > 0 and c / s >= 1.25:        # consolidated ≥25% larger
            return True
    return False


def generate_report(symbol: str, *, deep: bool = True, consolidated: bool | None = None,
                    pdf_path: str | None = None, target_shares: float | None = None,
                    synthesize: bool = True) -> str:
    """Full report (brief + Gemini analysis) for ``symbol``. Ingests on demand.

    ``consolidated=None`` (default) auto-picks consolidated for holding-cos.
    """
    symbol = symbol.upper()
    con = connect()
    try:
        have = ensure_ingested(symbol, con)
        if deep:
            _ensure_peer_financials(con, symbol)   # populate peers so §10's table is real
        basis = consolidated if consolidated is not None else _prefer_consolidated(con, symbol)
        builder = build_deep_brief if deep else build_brief
        brief = builder(con, symbol, consolidated=basis, target_shares=target_shares)
    finally:
        con.close()
    if not have:
        return (f"No structured financials could be ingested for **{symbol}** — NSE may not "
                "publish result XBRL for it (newly listed / recently renamed), or the lookup "
                "returned nothing. Price/technical data may still be available.\n\n" + brief)
    if not synthesize:
        return brief
    if pdf_path:                               # explicit filing supplied (CLI --pdf)
        thesis = synthesize_thesis(brief, symbol, pdf_path=pdf_path, deep=deep)
    else:                                      # auto: all filings since last FY-end + latest results
        thesis = synthesize_thesis(brief, symbol, pdfs=_filings_for_analysis(symbol), deep=deep)
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
                 "and charts — is in the attached PDF. A separate **Metrics & ratings guide** "
                 "explains what each number and rating means, its typical range or possible "
                 "values, and how to read it._")
        return "\n".join(L)
    finally:
        con.close()
