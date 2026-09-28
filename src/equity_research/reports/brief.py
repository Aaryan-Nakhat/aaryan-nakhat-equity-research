"""Assemble all quant signals for a symbol into one analytical brief (markdown).

This is the deterministic, primary-source layer: fundamentals, forensic scores,
technicals, valuation (own-history + sector). The brief feeds both the Claude
synthesis prompt and the emailed report. No LLM here — just the numbers.
"""

from __future__ import annotations

from datetime import date

import duckdb
import numpy as np

from equity_research.analysis import forensic, fundamentals, insurers, lenders, sector, technical, valuation


def _fmt(v, nd=2, pct=False, suffix=""):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}{suffix}"


def build_brief(con: duckdb.DuckDBPyConnection, symbol: str, *,
                consolidated: bool = False, target_shares: float | None = None) -> str:
    """Markdown brief of every quant signal we have for ``symbol``."""
    L: list[str] = [f"# {symbol} — analytical brief ({'consolidated' if consolidated else 'standalone'})",
                    f"_Report generated {date.today():%d-%b-%Y}._\n"]
    kind = fundamentals.filer_kind(con, symbol)
    if kind == "bank":
        L += _bank_lines(con, symbol, consolidated)
        return "\n".join(L + _market_lines(con, symbol, consolidated, target_shares))
    if kind in ("life", "general"):
        L += _insurer_lines(con, symbol, consolidated, kind)
        return "\n".join(L + _market_lines(con, symbol, consolidated, target_shares))

    # --- Fundamentals: TTM + annual trend ---
    t = fundamentals.ttm(con, symbol, consolidated)
    L.append("## Fundamentals (TTM)")
    if t:
        L.append(f"- Revenue: ₹{_fmt(t.get('ttm_revenue_cr'),0)} cr · "
                 f"Net profit: ₹{_fmt(t.get('ttm_net_profit_cr'),0)} cr")
        L.append(f"- Net margin: {_fmt(t.get('ttm_net_margin_%'),1,pct=True)} · "
                 f"EBITDA margin: {_fmt(t.get('ttm_ebitda_margin_%'),1,pct=True)}")
    else:
        L.append("- n/a (insufficient quarterly data)")

    qm = fundamentals.quarterly_metrics(con, symbol, consolidated)
    if not qm.empty:
        last = qm.iloc[-1]
        L.append(f"- Latest quarter ({qm.index[-1]}): rev YoY "
                 f"{_fmt(last.get('rev_yoy_%'),1,pct=True)}, net YoY "
                 f"{_fmt(last.get('net_yoy_%'),1,pct=True)}, interest cover "
                 f"{_fmt(last.get('interest_cover_x'),1,suffix='x')}")

    ao = fundamentals.annual_overview(con, symbol, consolidated)
    if not ao.empty:
        yrs = ao.tail(5)
        rev_series = " → ".join(f"{int(r)}" for r in yrs["revenue_cr"].dropna())
        L.append(f"- Annual revenue (₹cr): {rev_series}")
        latest = ao.iloc[-1]
        L.append(f"- CFO/PAT (latest yr): {_fmt(latest.get('cfo_to_pat_x'),2,suffix='x')} "
                 f"· ROA: {_fmt(latest.get('roa_%'),1,pct=True)}")

    # --- Forensic scores ---
    L.append("\n## Forensic / quality")
    mcap = valuation.market_cap(con, symbol, consolidated, shares_override=target_shares)
    z = forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap)
    f = forensic.piotroski_f(con, symbol, consolidated=consolidated)
    m = forensic.beneish_m(con, symbol, consolidated=consolidated)
    L.append(f"- Altman Z: {_fmt(z.value,2)} "
             "(>2.99 safe / 1.81-2.99 grey / <1.81 distress)"
             + (f" — {z.note}" if z.note else ""))
    L.append(f"- Piotroski F: {_fmt(f.value,0)}/9 (8-9 strong, 0-2 weak)"
             + (f" — missing {f.missing}" if f.missing else ""))
    L.append(f"- Beneish M: {_fmt(m.value,2)} (> -1.78 ⇒ possible earnings manipulation)"
             + (f" — missing {m.missing}" if m.missing else ""))
    return "\n".join(L + _market_lines(con, symbol, consolidated, target_shares))


def _bank_lines(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool) -> list[str]:
    """A bank's fundamentals + health checks (industrial scores don't apply — see analysis/lenders)."""
    af = fundamentals.load_annual(con, symbol, consolidated)
    am = lenders.annual_metrics(af)
    qm = lenders.quarterly_metrics(fundamentals.load_quarters(con, symbol, consolidated))
    if consolidated:                      # regulatory ratios are bank-level (standalone) figures
        am, _ = lenders.with_regulatory_fallback(
            am, lenders.annual_metrics(fundamentals.load_annual(con, symbol, False)))
        qm, _ = lenders.with_regulatory_fallback(
            qm, lenders.quarterly_metrics(fundamentals.load_quarters(con, symbol, False)))
    L = ["## Fundamentals (bank)"]
    if not am.empty:
        a = am.iloc[-1]
        L.append(f"- FY{am.index[-1].year}: NII ₹{_fmt(a.get('nii_cr'),0)} cr · PPOP "
                 f"₹{_fmt(a.get('ppop_cr'),0)} cr · net profit ₹{_fmt(a.get('pat_cr'),0)} cr")
        L.append(f"- NIM (on avg assets) {_fmt(a.get('nim_%'),2,pct=True)} · cost-to-income "
                 f"{_fmt(a.get('cost_to_income_%'),1,pct=True)} · credit cost "
                 f"{_fmt(a.get('credit_cost_%'),2,pct=True)} · ROA {_fmt(a.get('roa_%'),2,pct=True)} "
                 f"· ROE {_fmt(a.get('roe_%'),1,pct=True)}")
        L.append(f"- CD ratio {_fmt(a.get('cd_ratio_%'),0,pct=True)} · loans YoY "
                 f"{_fmt(a.get('adv_yoy_%'),1,pct=True)} · deposits YoY {_fmt(a.get('dep_yoy_%'),1,pct=True)}")
    if not qm.empty:
        q = qm.iloc[-1]
        L.append(f"- Latest quarter ({qm.index[-1]:%d-%b-%Y}): NII YoY {_fmt(q.get('nii_yoy_%'),1,pct=True)}, "
                 f"PAT YoY {_fmt(q.get('pat_yoy_%'),1,pct=True)} · gross NPA {_fmt(q.get('gnpa_%'),2,pct=True)} "
                 f"· net NPA {_fmt(q.get('nnpa_%'),2,pct=True)} · CET1 {_fmt(q.get('cet1_%'),1,pct=True)}")
    L.append("\n## Bank health checks")
    L.append("- _Altman Z / Piotroski F / Beneish M don't apply to banks (built for industrial "
             "balance sheets); these checks replace them._")
    icon = {"ok": "✅", "warn": "⚠️", "alarm": "🔴"}
    L += [f"- {icon[s]} {t}" for s, t in lenders.health_checks(am, qm)] or ["- n/a (insufficient data)"]
    return L


def _insurer_lines(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool,
                   kind: str) -> list[str]:
    """An insurer's fundamentals + health checks (industrial scores don't apply — see
    analysis/insurers)."""
    am = insurers.metrics(kind, fundamentals.load_annual(con, symbol, consolidated))
    qm = insurers.metrics(kind, fundamentals.load_quarters(con, symbol, consolidated), quarterly=True)
    L = [f"## Fundamentals ({'life' if kind == 'life' else 'general'} insurer)"]
    if not am.empty:
        a, fy = am.iloc[-1], am.index[-1].year
        if kind == "life":
            L.append(f"- FY{fy}: gross premium ₹{_fmt(a.get('gross_premium_cr'),0)} cr "
                     f"({_fmt(a.get('gross_premium_yoy_%'),1,pct=True)} YoY) · APE ₹{_fmt(a.get('ape_cr'),0)} cr "
                     f"({_fmt(a.get('ape_yoy_%'),1,pct=True)}) · net profit ₹{_fmt(a.get('pat_cr'),0)} cr")
            L.append(f"- Renewal share {_fmt(a.get('renewal_share_%'),1,pct=True)} · commission "
                     f"{_fmt(a.get('commission_ratio_%'),1,pct=True)} of premium · expense ratio "
                     f"{_fmt(a.get('expense_ratio_%'),1,pct=True)} · ROE {_fmt(a.get('roe_%'),1,pct=True)}")
        else:
            L.append(f"- FY{fy}: GWP ₹{_fmt(a.get('gross_premium_cr'),0)} cr "
                     f"({_fmt(a.get('gross_premium_yoy_%'),1,pct=True)} YoY) · underwriting "
                     f"₹{_fmt(a.get('underwriting_cr'),0)} cr + investment income "
                     f"₹{_fmt(a.get('investment_income_cr'),0)} cr → net profit ₹{_fmt(a.get('pat_cr'),0)} cr")
            L.append(f"- Claims ratio {_fmt(a.get('claims_ratio_%'),1,pct=True)} · combined ratio "
                     f"{_fmt(a.get('combined_ratio_%'),1,pct=True)} · retention "
                     f"{_fmt(a.get('retention_%'),1,pct=True)} · ROE {_fmt(a.get('roe_%'),1,pct=True)}")
    L.append("\n## Insurer health checks")
    L.append("- _Altman Z / Piotroski F / Beneish M don't apply to insurers (built for industrial "
             "balance sheets); these checks replace them._")
    icon = {"ok": "✅", "warn": "⚠️", "alarm": "🔴"}
    L += ([f"- {icon[s]} {t}" for s, t in insurers.health_checks(kind, am, qm)]
          or ["- n/a (insufficient data)"])
    return L


def _market_lines(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool,
                  target_shares: float | None) -> list[str]:
    """Technicals + valuation — shared by every company type."""
    L: list[str] = []

    # --- Technicals ---
    L.append("\n## Technicals")
    ts = technical.snapshot(con, symbol)
    if ts:
        L.append(f"- Close ₹{_fmt(ts['close'],2)} on {ts['date']} ({ts['n_days']} sessions)")
        L.append(f"- SMA 20/50/200: {_fmt(ts['sma20'],0)} / {_fmt(ts['sma50'],0)} / "
                 f"{_fmt(ts['sma200'],0)} · RSI {_fmt(ts['rsi14'],0)}")
        L.append(f"- Delivery% (20d): {_fmt(ts['deliv_per'],1)} (avg {_fmt(ts['deliv_avg20'],1)}) "
                 f"· {_fmt(ts['pct_from_52w_high'],1,pct=True)} from 52w high")
        rs = ts.get("rel_strength_3m_vs_nifty")
        L.append(f"- Rel. strength 3m vs Nifty: {_fmt(rs,3)} "
                 f"({'out' if rs and rs > 1 else 'under'}performing)")
        L.append(f"- Signals: {', '.join(ts['signals'])}")
    else:
        L.append("- n/a (no price history — run backfill_eod)")

    # --- Valuation ---
    L.append("\n## Valuation")
    snap = valuation.snapshot(con, symbol, consolidated, shares_override=target_shares)
    if snap:
        L.append(f"- Market cap ₹{_fmt(snap.get('market_cap_cr'),0)} cr · "
                 f"P/E (TTM) {_fmt(snap.get('pe_ttm'),1)} · P/B {_fmt(snap.get('pb'),2)} · "
                 f"earnings yield {_fmt(snap.get('earnings_yield_%'),2,pct=True)}")
        if snap.get("note"):
            L.append(f"  - ⚠ {snap['note']}")
    hist = valuation.valuation_history(con, symbol, consolidated)
    if not hist.empty and "pe" in hist:
        pes = hist["pe"].dropna()
        if len(pes):
            L.append(f"- Own P/E history: min {_fmt(pes.min(),1)} / median "
                     f"{_fmt(float(pes.median()),1)} / max {_fmt(pes.max(),1)}")
    sec = sector.sector_valuation(con, symbol, consolidated,
                                  target_shares_override=target_shares)
    if sec.get("peers_with_data"):
        L.append(f"- Sector ({sec['industry']}): P/E vs median "
                 f"{_fmt(sec.get('sector_median_pe'),1)} — cheaper than "
                 f"{_fmt(sec.get('pe_cheaper_than_%_of_peers'),0)}% of "
                 f"{sec['peers_with_data']} peers")
    return L
