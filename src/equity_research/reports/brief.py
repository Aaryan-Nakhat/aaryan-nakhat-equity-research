"""Assemble all quant signals for a symbol into one analytical brief (markdown).

This is the deterministic, primary-source layer: fundamentals, forensic scores,
technicals, valuation (own-history + sector). The brief feeds both the Claude
synthesis prompt and the emailed report. No LLM here — just the numbers.
"""

from __future__ import annotations

import duckdb
import numpy as np

from equity_research.analysis import forensic, fundamentals, sector, technical, valuation


def _fmt(v, nd=2, pct=False, suffix=""):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}{suffix}"


def build_brief(con: duckdb.DuckDBPyConnection, symbol: str, *,
                consolidated: bool = False, target_shares: float | None = None) -> str:
    """Markdown brief of every quant signal we have for ``symbol``."""
    L: list[str] = [f"# {symbol} — analytical brief ({'consolidated' if consolidated else 'standalone'})\n"]

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

    return "\n".join(L)
