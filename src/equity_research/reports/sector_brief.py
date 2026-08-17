"""Render the sectoral-analysis bundle into a markdown email + the LLM top-down read.

``build_sector_report(con, canonical)`` returns ``{markdown, picks, sector_name}`` — ``picks``
is the ordered ``[{symbol, name}]`` behind the numbered Top / Undervalued tables, so the email
bot can set the "reply a number → deep report" menu with the exact same numbering the reader sees.
"""

from __future__ import annotations

import duckdb

from equity_research.analysis import sector_analysis
from equity_research.reports import md, synthesize


def _f(v, dec=0):
    return "n/a" if v is None else f"{v:,.{dec}f}"


def _signed(v, dec=1):
    return "n/a" if v is None else f"{v:+,.{dec}f}"


_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this (plain English)**\n\n"
    "- **50 / 200-DMA** — the average price over the last 50 / 200 trading days. Price above both, "
    "with the 50 above the 200 (a **golden cross**), is a healthy uptrend; the reverse (**death "
    "cross**) is a downtrend.\n"
    "- **RSI** — momentum on a 0–100 scale. >70 overbought (stretched), <30 oversold; 55+ is strong, "
    "45− weak. **MACD** positive = momentum building.\n"
    "- **Relative strength vs Nifty 50** — how the sector has done against the broad market over ~3 "
    "months. Positive = the sector is **leading** the market; negative = lagging.\n"
    "- **PE vs its own history (percentile)** — where today's sector valuation sits against its own "
    "last ~5 years. **High percentile = expensive** vs how it usually trades (a re-rated sector can "
    "keep running, but the margin of safety is thinner); **low = cheap** vs itself. We also show it "
    "vs the Nifty-50 PE for context.\n"
    "- **Smart-money (proxy)** — NSE does **not** publish FII/DII *cash* by sector, so this is an "
    "**aggregate across the sector's stocks**: how many saw institutions (mutual funds, insurers, "
    "FPIs, banks) **add vs trim** their stake last quarter, plus mutual-fund exposure. 'Accumulating' "
    "= more adds than trims. (Quarterly data — a slow signal.) The FII/DII line is **market-wide**, "
    "shown only as backdrop.\n"
    "- **Top companies** — ranked on **quality** (Piotroski 0–9) + **forensic** safety (0–4) + "
    "**cheapness vs own history**. **Undervalued** = the ones genuinely cheap vs their own past (not "
    "just cheap-looking). **Reply with a number** to get that stock's full deep report.\n"
    "- _A read of the sector setup, not a trade call — a strong sector can still correct, and a cheap "
    "one can stay cheap._"
)


def _headline(emoji, index_name, tech, val) -> str:
    trend = "—"
    if tech:
        c, s50, s200 = tech.get("close"), tech.get("sma50"), tech.get("sma200")
        if c and s50 and s200:
            trend = "uptrend" if c > s50 > s200 else "downtrend" if c < s50 < s200 else "mixed trend"
    v = ""
    if val:
        v = f"  ·  {val['metric']} {_f(val.get('current'), 1)} ({val.get('reading')})"
    rs = tech.get("rs_vs_nifty") if tech else None
    rstxt = f"  ·  {'leading' if rs and rs > 1 else 'lagging'} Nifty" if rs else ""
    return f"## {emoji} {index_name}: **{trend}**{rstxt}{v}"


def build_sector_report(con: duckdb.DuckDBPyConnection, canonical: str) -> dict | None:
    """Full sector report markdown + ordered picks. ``None`` if the sector has no data."""
    data = sector_analysis.build_sector_analysis(con, canonical)
    if not data:
        return None
    emoji, index_name = data["emoji"], data["index_name"]
    tech, val, sm, rk = (data["technicals"], data["valuation"], data["smart_money"], data["ranking"])
    sector_name = index_name.replace("Nifty ", "").replace(" Index", "")

    # ── ordered picks for the numbered reply-menu (top, then undervalued/cheapest not already in) ──
    top = rk.get("top", [])
    uv_list = rk.get("undervalued") or rk.get("cheapest") or []
    picks: list[dict] = [{"symbol": r["symbol"], "name": r["name"]} for r in top]
    seen = {r["symbol"] for r in top}
    for r in uv_list:
        if r["symbol"] not in seen:
            picks.append({"symbol": r["symbol"], "name": r["name"]})
            seen.add(r["symbol"])
    num_of = {p["symbol"]: i + 1 for i, p in enumerate(picks)}

    parts = [f"# {emoji} Sector analysis — {sector_name}",
             "_Top-down read: where the sector is in its trend and valuation cycle, who's "
             "accumulating it, and the best / cheapest names inside it. Reply a number for a deep "
             "report on that stock. Not a trade call — see the legend._",
             _headline(emoji, index_name, tech, val)]

    # LLM top-down read
    thesis = synthesize.sector_thesis(_brief_for_llm(sector_name, data), sector_name)
    if thesis:
        parts.append("### 🧭 Top-down read\n\n" + thesis)

    # technicals & valuation
    if tech or val:
        rows = []
        if tech:
            rows.append(["Trend / momentum", " · ".join(tech.get("signals", [])[:3]) or "—"])
            if tech.get("rs_vs_nifty") is not None:
                rows.append(["Relative strength vs Nifty", f"{(tech['rs_vs_nifty'] - 1) * 100:+.1f}% "
                             f"over ~3m ({'leading' if tech['rs_vs_nifty'] > 1 else 'lagging'})"])
        if val:
            rows.append([f"Valuation ({val['metric']})", f"{_f(val.get('current'), 1)} — "
                         f"{val.get('reading')} (richer than {_f(val.get('own_history_pctile'))}% of its "
                         f"own {val.get('years')}y); Nifty {val['metric']} "
                         f"{_f(val.get('nifty_pe') if val['metric'] == 'PE' else val.get('nifty_pb'), 1)}"])
            if val.get("div_yield") is not None:
                rows.append(["Dividend yield", f"{_f(val.get('div_yield'), 2)}%"])
        parts += ["## 📈 Sector index — technicals & valuation",
                  md.table(["Gauge", "Reading"], rows, "ll")]

    # smart money
    if sm:
        net_emoji = {"accumulating": "🟢", "distributing": "🔴", "mixed": "🟡"}.get(sm.get("net"), "⚪")
        lines = [f"{net_emoji} **Institutions look {sm.get('net')}** across the sector last quarter "
                 f"— {sm.get('adds', 0)} stakes added/entered vs {sm.get('reduces', 0)} trimmed/exited "
                 f"(mutual funds, insurers, FPIs, banks)."]
        mf = sm.get("mf") or {}
        if mf.get("schemes"):
            lines.append(f"- 📦 **Mutual funds:** {mf['schemes']} schemes hold names here "
                         f"(~₹{_f(mf.get('exposure_cr'))} cr aggregate exposure).")
        if sm.get("add_detail"):
            adds = ", ".join(f"{d['symbol']} ({d['holder'][:20]})" for d in sm["add_detail"][:4])
            lines.append(f"- 🟢 **Notable adds:** {adds}")
        if sm.get("reduce_detail"):
            red = ", ".join(f"{d['symbol']} ({d['holder'][:20]})" for d in sm["reduce_detail"][:4])
            lines.append(f"- 🔴 **Notable trims:** {red}")
        if sm.get("marquee"):
            mq = ", ".join(f"{m['investor']} {m['kind']} {m['symbol']}" for m in sm["marquee"][:4])
            lines.append(f"- 👤 **Marquee investors:** {mq}")
        fd = data.get("fii_dii_backdrop") or {}
        if fd:
            lines.append(f"- 🌐 _Market-wide backdrop (not sector-specific): FII cash "
                         f"₹{_f(fd.get('fii_net_cr'))} cr · DII ₹{_f(fd.get('dii_net_cr'))} cr._")
        parts += ["## 💰 Smart money in the sector (proxy)", "\n".join(lines)]

    # news
    if data.get("news"):
        parts += ["## 📰 Sector headlines",
                  "\n".join(f"- {h['title']}" for h in data["news"])]

    # top companies
    if top:
        rows = [[num_of[r["symbol"]], r["symbol"], (r["name"] or "")[:24], _f(r.get("composite"), 1),
                 r.get("why", "")] for r in top]
        parts += ["## 🏆 Top companies in the sector",
                  md.table(["#", "Symbol", "Company", "Score", "Why"], rows, "rllrl")]

    # undervalued / least-expensive
    if uv_list:
        label = ("## 💎 Undervalued names (cheap vs their own history)" if rk.get("genuinely_cheap")
                 else "## 💎 Least-expensive names (the sector has re-rated — none are outright cheap)")
        rows = [[num_of[r["symbol"]], r["symbol"], (r["name"] or "")[:24],
                 f"cheaper than {_f(r.get('cheapness'))}% of own history", r.get("why", "")]
                for r in uv_list]
        parts += [label, md.table(["#", "Symbol", "Company", "Cheapness", "Why"], rows, "rllll")]

    if rk.get("total") and rk.get("scored"):
        parts.append(f"_Ranked {rk.get('scored')} of {rk.get('total')} constituents with ingested "
                     f"financials._")
    elif rk.get("total"):
        # scored 0 — banks/financials aren't rank-scored (Piotroski/Altman/Beneish assume a
        # non-financial balance sheet). The index read + smart-money above are the sector call.
        parts.append("_Per-stock quality/forensic ranking isn't shown here — this sector's names use "
                     "**financial-company accounting** (banks/NBFCs/insurers), which the quality + "
                     "forensic screen doesn't apply to. Use the index trend, valuation and smart-money "
                     "read above as the sector call; ask for any name directly for its deep report._")

    return {"markdown": "\n\n".join(parts) + _LEGEND, "picks": picks, "sector_name": sector_name}


def _brief_for_llm(sector_name: str, data: dict) -> str:
    """Compact structured brief handed to the LLM (numbers only — it never re-derives)."""
    tech, val, sm, rk = (data["technicals"], data["valuation"], data["smart_money"], data["ranking"])
    L = [f"SECTOR: {sector_name} (index {data['index_name']})", "", "TECHNICALS:"]
    L += [f"- {s}" for s in (tech.get("signals", []) if tech else [])] or ["- (insufficient history)"]
    L.append("")
    if val:
        L.append(f"VALUATION: {val['metric']} {val.get('current'):.1f} — {val.get('reading')} "
                 f"(richer than {val.get('own_history_pctile'):.0f}% of its own {val.get('years')}y "
                 f"history); Nifty-50 {val['metric']} "
                 f"{(val.get('nifty_pe') if val['metric'] == 'PE' else val.get('nifty_pb')) or float('nan'):.1f}; "
                 f"div yield {val.get('div_yield')}.")
    if sm:
        L.append(f"SMART MONEY (proxy): institutions {sm.get('net')} — {sm.get('adds')} adds vs "
                 f"{sm.get('reduces')} trims across constituents; "
                 f"{(sm.get('mf') or {}).get('schemes', 0)} MF schemes hold the sector.")
    if data.get("news"):
        L.append("HEADLINES:")
        L += [f"- {h['title']}" for h in data["news"]]
    if rk.get("top"):
        L.append("STRONGEST NAMES: " + ", ".join(f"{r['symbol']} (score {r.get('composite')})"
                                                  for r in rk["top"][:6]))
    uv = rk.get("undervalued") or []
    if uv:
        L.append("CHEAP NAMES: " + ", ".join(r["symbol"] for r in uv[:6]))
    return "\n".join(L)
