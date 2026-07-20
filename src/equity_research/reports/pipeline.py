"""Reusable report pipeline: ensure data → build brief → Gemini → full report.

Used by both the CLI (scripts/research_report.py) and the Telegram bot. Handles
on-demand ingestion so any NSE-listed symbol works, not just pre-ingested ones.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import duckdb

from equity_research.analysis import forensic, fundamentals, quant, sector, valuation
from equity_research.analysis.alerts import _categorise
from equity_research.common.db import connect
from equity_research.common.http import fetch_bytes
from equity_research.reports import glossary
from equity_research.ingest import (ingest_annual_financials, ingest_financials,
                                    ingest_insider_trades, ingest_shareholding)
from equity_research.reports.brief import build_brief
from equity_research.reports.deep_brief import build_deep_brief
from equity_research.reports.synthesize import (business_overview, extract_guidance,
                                                growth_triggers, ipo_analysis,
                                                synthesize_thesis)
from equity_research.scrapers import ipo, nse_api

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
            try:  # insider/promoter (SEBI PIT) disclosures — same ownership cadence
                ingest_insider_trades(symbol, con)
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
    """Default to **consolidated whenever it exists** — it's the whole group (parent +
    subsidiaries + JVs) and the economically complete, industry-standard primary lens.
    Standalone deliberately excludes subsidiaries, so it only wins when consolidated is
    unavailable — or when consolidated's XBRL history is materially thinner than
    standalone's (don't trade a complete *entity* for an incomplete *history*)."""
    cons = fundamentals.load_annual(con, symbol, consolidated=True)
    if cons.empty:
        return False                                    # no group statements → standalone
    std = fundamentals.load_annual(con, symbol, consolidated=False)
    if std.empty:
        return True

    def usable_years(df) -> int:                        # years with a real revenue figure
        if "RevenueFromOperations" not in df.columns:
            return 0
        return int(df["RevenueFromOperations"].dropna().shape[0])

    # Prefer consolidated unless it's ≥2 years shallower than standalone (consolidated
    # commonly starts a year later, so tolerate a 1-year gap).
    return usable_years(cons) >= usable_years(std) - 1


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
        # Gather filings up-front (deep, auto-synthesize): reused for the leading business
        # overview, the forward-guidance multiple, and the thesis below (no double fetch).
        # Fetched even when financials are missing (REIT/InvIT/newly listed) so the report
        # still leads with a real business overview + technicals instead of "not found".
        pdfs = guidance = overview = None
        if deep and synthesize and not pdf_path:
            pdfs = _filings_for_analysis(symbol)
            if have:
                guidance = extract_guidance(pdfs)
            industry = sector.industry_of(con, symbol)
            mc = valuation.market_cap(con, symbol, False)      # rupees
            overview = business_overview(
                pdfs, symbol, market_cap_cr=(mc / CR if mc else None),
                industry=industry, order_driven=sector.is_order_driven(industry))
        if deep:
            _ensure_peer_financials(con, symbol)   # populate peers so §10's table is real
        basis = consolidated if consolidated is not None else _prefer_consolidated(con, symbol)
        if deep:
            brief = build_deep_brief(con, symbol, consolidated=basis,
                                     target_shares=target_shares, guidance=guidance,
                                     overview=overview)
        else:
            brief = build_brief(con, symbol, consolidated=basis, target_shares=target_shares)
    finally:
        con.close()
    if not have:
        # No XBRL statements (REIT/InvIT, newly listed/renamed). The brief already leads
        # with the business overview + an honest note + the technical snapshot, so return it
        # as-is rather than a bare "couldn't find" message.
        return brief
    if not synthesize:
        return brief
    if pdf_path:                               # explicit filing supplied (CLI --pdf)
        thesis = synthesize_thesis(brief, symbol, pdf_path=pdf_path, deep=deep)
    else:                                      # auto: all filings since last FY-end + latest results
        thesis = synthesize_thesis(brief, symbol,
                                   pdfs=pdfs if pdfs is not None else _filings_for_analysis(symbol),
                                   deep=deep)
    return f"{brief}\n\n{'=' * 60}\n## Analysis\n\n{thesis}"


def _ok(v) -> bool:
    return v is not None and v == v            # not None and not NaN


def _snapshot_facts(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool) -> list[str]:
    """Verified, deterministic snapshot numbers to ground the growth-triggers Section 1 —
    market cap / CMP / TTM revenue & EBITDA margin / ROE / ROCE / P/E / P/B / promoter
    holding (+ recent change). The LLM must use these verbatim instead of estimating."""
    facts: list[str] = []
    ind = sector.industry_of(con, symbol)
    if ind:
        facts.append(f"NSE industry: {ind}")
    snap = valuation.snapshot(con, symbol, consolidated)
    if _ok(snap.get("price")):
        facts.append(f"CMP: ₹{snap['price']:,.2f}")
    if _ok(snap.get("market_cap_cr")):
        facts.append(f"Market cap: ₹{snap['market_cap_cr']:,.0f} cr")
    t = fundamentals.ttm(con, symbol, consolidated)
    if _ok(t.get("ttm_revenue_cr")):
        facts.append(f"TTM revenue: ₹{t['ttm_revenue_cr']:,.0f} cr")
    if _ok(t.get("ttm_ebitda_margin_%")):
        facts.append(f"TTM EBITDA margin: {t['ttm_ebitda_margin_%']:.1f}%")
    if _ok(t.get("ttm_net_margin_%")):
        facts.append(f"TTM net margin: {t['ttm_net_margin_%']:.1f}%")
    r = quant._ratios(con, symbol, consolidated)
    for key, label, nd in (("ROE%", "ROE", 1), ("ROCE%", "ROCE", 1),
                           ("P/E", "P/E (TTM)", 1), ("P/B", "P/B", 2)):
        if _ok(r.get(key)):
            facts.append(f"{label}: {r[key]:.{nd}f}{'%' if key.endswith('%') else ''}")
    rows = con.execute(
        "SELECT promoter_holding_pct FROM shareholding WHERE symbol = ? "
        "AND promoter_holding_pct IS NOT NULL ORDER BY period_end DESC LIMIT 2", [symbol]).fetchall()
    if rows and rows[0][0] is not None:
        line = f"Promoter holding: {rows[0][0]:.2f}%"
        if len(rows) > 1 and rows[1][0] is not None:
            d = rows[0][0] - rows[1][0]
            if abs(d) >= 0.01:
                line += f" ({d:+.2f} pp vs prior quarter)"
        facts.append(line)
    return facts


def generate_growth_triggers(symbol: str, *, consolidated: bool | None = None,
                             ipo_mode: bool = False) -> str | None:
    """Forward-looking **growth-triggers 1-pager** for ``symbol`` — an opt-in deeper cut
    offered after a deep report. Grounded in the primary filings plus the verified snapshot.
    ``ipo_mode`` grounds it in the IPO offer documents (RHP etc.) for a pre-listing company
    instead of listed filings. Returns markdown, or ``None`` if there's nothing to ground it."""
    symbol = symbol.upper()
    if ipo_mode:
        meta, live = _ipo_meta(symbol)
        facts = _ipo_facts(symbol, meta, live)
        pdfs = ipo.documents(symbol)
    else:
        con = connect()
        try:
            ensure_ingested(symbol, con)           # fresh financials + shareholding (cooldown-guarded)
            basis = consolidated if consolidated is not None else _prefer_consolidated(con, symbol)
            facts = _snapshot_facts(con, symbol, basis)
        finally:
            con.close()
        pdfs = _filings_for_analysis(symbol)
    if not pdfs:
        return None
    return growth_triggers(pdfs, symbol, facts=facts)


# ----------------- IPO (pre-listing) -----------------
def _upper_band(band: str | None) -> float | None:
    """Upper end of a price band string like 'Rs.402 to Rs.424' → 424.0."""
    if not band:
        return None
    nums = [float(n) for n in re.findall(r"[0-9]+(?:\.[0-9]+)?", band.replace(",", ""))]
    return max(nums) if nums else None


def _ipo_meta(symbol: str) -> tuple[dict | None, bool]:
    """(meta, live) for an IPO symbol from the live then upcoming lists; (None, False) if
    unknown (e.g. just closed) — the report still works off the archived documents."""
    for x in ipo.list_current():
        if x["symbol"] == symbol:
            return x, True
    for x in ipo.list_upcoming():
        if x["symbol"] == symbol:
            return x, False
    return None, False


def _ipo_facts(symbol: str, meta: dict | None, live: bool) -> list[str]:
    """Verified issue facts to ground the IPO note — band, size, dates, subscription."""
    if not meta:
        return []
    facts = [f"Company: {meta['company']}"] if meta.get("company") else []
    if meta.get("price_band"):
        facts.append(f"Price band: {meta['price_band']}")
    shares, upper = meta.get("issue_size_shares"), _upper_band(meta.get("price_band"))
    if shares and upper:
        facts.append(f"Issue size (offered): ~{shares:,.0f} shares "
                     f"(~₹{shares * upper / CR:,.0f} cr at the upper band)")
    elif shares:
        facts.append(f"Issue size (offered): ~{shares:,.0f} shares")
    if meta.get("start") and meta.get("end"):
        facts.append(f"Open–close: {meta['start']} to {meta['end']}")
    if meta.get("status"):
        facts.append(f"Status: {meta['status']}")
    if live:
        if meta.get("subscription_x") is not None:
            facts.append(f"Total subscription so far: {meta['subscription_x']:.2f}x")
        for row in ipo.subscription_detail(symbol):
            facts.append(f"  {row['category']}: {row['times']:.2f}x")
    return facts


def generate_ipo_report(symbol: str) -> str | None:
    """Pre-listing IPO note for ``symbol`` — offer structure (fresh vs OFS), restated
    financials, valuation-at-band vs peers, use of proceeds, risks, live demand, and an
    APPLY / AVOID / NEUTRAL verdict — from the official RHP + price-band ad + anchor docs.
    ``None`` if the offer documents aren't published yet (so the caller replies gracefully)."""
    symbol = symbol.upper()
    meta, live = _ipo_meta(symbol)
    facts = _ipo_facts(symbol, meta, live)
    docs = ipo.documents(symbol)
    if not docs:
        return None
    return ipo_analysis(docs, symbol, facts=facts)


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
