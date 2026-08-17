"""Render the sectoral-analysis bundle into a markdown email + the LLM top-down read.

``build_sector_report(con, canonical)`` returns ``{markdown, picks, sector_name}`` — ``picks``
is the ordered ``[{symbol, name}]`` behind the numbered Top / Undervalued tables, so the email
bot can set the "reply a number → deep report" menu with the exact same numbering the reader sees.
"""

from __future__ import annotations

import duckdb

from equity_research.analysis import sector_analysis, supply_chain
from equity_research.reports import md, synthesize


def _f(v, dec=0):
    return "n/a" if v is None else f"{v:,.{dec}f}"


def _signed(v, dec=1):
    return "n/a" if v is None else f"{v:+,.{dec}f}"


def cheap_band(cheapness) -> str:
    """Plain-English valuation-vs-own-history band from the 0-100 cheapness score (higher =
    cheaper). Avoids the confusing 'cheaper than 0% of its own history' phrasing."""
    if cheapness is None:
        return ""
    c = cheapness
    if c >= 75:
        return "very cheap vs its own 5-yr history"
    if c >= 55:
        return "cheap vs its own 5-yr history"
    if c >= 40:
        return "around its own 5-yr average"
    if c >= 25:
        return "a bit pricey vs its own 5-yr history"
    return "expensive vs its own 5-yr history"


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

    # ── ordered picks for the numbered reply-menu (top, then genuinely-undervalued not already in) ──
    top = rk.get("top", [])
    picks: list[dict] = [{"symbol": r["symbol"], "name": r["name"]} for r in top]
    seen = {r["symbol"] for r in top}
    for r in rk.get("undervalued") or []:
        if r["symbol"] not in seen:
            picks.append({"symbol": r["symbol"], "name": r["name"]})
            seen.add(r["symbol"])

    # supply chain: the smaller LISTED ancillaries feeding the sector's marquee names (Phase-2,
    # best-effort — curated seed + AI, all verified against the NSE master). Excludes the index's
    # own constituents (those are direct, not indirect).
    supply: list[dict] = []
    try:
        members = sector_analysis.constituents(con, canonical)
        csyms = {m["symbol"] for m in members}
        primes = [r["symbol"] for r in top] or [m["symbol"] for m in members[:6]]
        supply = supply_chain.sector_supply_chain(con, canonical, index_name, primes, csyms)
    except Exception:  # noqa: BLE001 — supply-chain is a bonus, never break the sector report
        supply = []
    for r in supply:
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
            nifty_val = val.get("nifty_pe") if val["metric"] == "PE" else val.get("nifty_pb")
            pct = val.get("own_history_pctile")
            hist = (f" (richer than {pct:.0f}% of its own {val.get('years')}y)"
                    if pct is not None else "")
            rows.append([f"Valuation ({val['metric']})",
                         f"{_f(val.get('current'), 1)} — {val.get('reading')}{hist}; "
                         f"Nifty {val['metric']} {_f(nifty_val, 1)}"])
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
        bullets = []
        mf = sm.get("mf") or {}
        if mf.get("schemes"):
            bullets.append(f"- 📦 **Mutual funds:** {mf['schemes']} schemes hold names here "
                           f"(~₹{_f(mf.get('exposure_cr'))} cr aggregate exposure).")
        if sm.get("add_detail"):
            def _add(d):
                g = d.get("zone_gain")
                tag = f", now {g:+.0f}% vs add-zone" if g is not None else ""
                return f"{d['symbol']} ({d['holder']}{tag})"
            adds = ", ".join(_add(d) for d in sm["add_detail"][:4])
            bullets.append(f"- 🟢 **Notable adds:** {adds}")
        if sm.get("reduce_detail"):
            red = ", ".join(f"{d['symbol']} ({d['holder']})" for d in sm["reduce_detail"][:4])
            bullets.append(f"- 🔴 **Notable trims:** {red}")
        if sm.get("marquee"):
            mq = ", ".join(f"{m['investor']} {m['kind']} {m['symbol']}" for m in sm["marquee"][:4])
            bullets.append(f"- 👤 **Marquee investors:** {mq}")
        fd = data.get("fii_dii_backdrop") or {}
        if fd:
            bullets.append(f"- 🌐 _Market-wide backdrop (not sector-specific): FII cash "
                           f"₹{_f(fd.get('fii_net_cr'))} cr · DII ₹{_f(fd.get('dii_net_cr'))} cr._")
        # intro sentence and the bullet list must be SEPARATE blocks (blank line between) or
        # markdown lumps them onto one line.
        parts += ["## 💰 Smart money in the sector (proxy)", lines[0]]
        if bullets:
            parts.append("\n".join(bullets))

    # news
    if data.get("news"):
        parts += ["## 📰 Sector headlines",
                  "\n".join(f"- {h['title']}" for h in data["news"])]

    # top companies
    if top:
        rows = [[num_of[r["symbol"]], r["symbol"], r["name"] or "", _f(r.get("composite"), 1),
                 r.get("why", "")] for r in top]
        parts += ["## 🏆 Top companies in the sector",
                  md.table(["#", "Symbol", "Company", "Score", "Why"], rows, "rllrl")]

    # undervalued — only genuinely-cheap names; else an honest one-liner (no confusing table)
    if rk.get("undervalued"):
        rows = [[num_of[r["symbol"]], r["symbol"], r["name"] or "",
                 cheap_band(r.get("cheapness")), r.get("why", "")] for r in rk["undervalued"]]
        parts += ["## 💎 Undervalued names (cheap vs their own history)",
                  md.table(["#", "Symbol", "Company", "Valuation", "Why"], rows, "rllll")]
    elif top:
        parts.append("## 💎 Undervalued names\n\n_**None right now** — no constituent is cheap vs its "
                     "own history; the sector has broadly re-rated. The **Top** list above is the "
                     "best-quality entry set — wait for a pullback for better value._")

    # supply chain / indirect contributors
    if supply:
        rows = [[num_of[r["symbol"]], r["symbol"], r["name"] or "", r.get("role") or "",
                 "🖐️ curated" if r["source"] == "curated" else "🤖 AI"] for r in supply]
        parts += ["## 🔗 Supply chain — smaller listed ancillaries",
                  "_The **indirect** beneficiaries: smaller listed companies that supply / make "
                  "components for the sector's marquee names (not index members themselves). "
                  "🖐️ = hand-curated; 🤖 = **AI-suggested, verify** the link before acting. Reply a "
                  "number for a deep report._",
                  md.table(["#", "Symbol", "Company", "Supplies", "Source"], rows, "rllll")]

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


_ROTATION_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this**\n\n"
    "- **RS vs Nifty** — the sector index's return vs the Nifty 50 over ~3 months. Positive = the "
    "sector is **leading** the market (money rotating in); negative = lagging.\n"
    "- **Trend** — price vs its 50 & 200-day averages (up / mixed / down).\n"
    "- **Valuation** — where the sector's PE/PB sits vs its **own ~5-yr history** (cheap = room; "
    "expensive = stretched).\n"
    "- **💎 Turning up from cheap** — sectors that are still **cheap vs their own history** yet "
    "**not in a downtrend and starting to outperform** — the classic value+momentum rotation setup "
    "(early, higher-conviction).\n"
    "- _Reply `sector: <name>` (e.g. `sector: pharma`) for the full read on any of these — trend, "
    "valuation, smart-money, best/cheapest names and supply chain. A momentum map, not a trade call._"
)


def _rot_rows(items):
    out = []
    for o in items:
        val = (f"{o['metric']} {_f(o['current'], 1)} ({o['val_reading']})"
               if o.get("current") is not None else "n/a")
        out.append([f"{o['emoji']} {o['index_name'].replace('Nifty ', '')}",
                    _signed(o["rs_pct"], 1) + "%", o["trend"], val])
    return out


def build_sector_rotation(con: duckdb.DuckDBPyConnection) -> str | None:
    """Weekly (and on-demand) sector-rotation digest: leaders / laggards / value-turning across all
    sectors. ``None`` if nothing ranked. Deterministic — no LLM, no network."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = sector_analysis.rank_all_sectors(con)
    if not r["all"]:
        return None
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    parts = [f"# 🔄 Sector rotation — week of {today:%d-%b-%Y}",
             "_Where money is rotating (relative strength vs Nifty) and where value is starting to "
             "turn. Reply `sector: <name>` for the full read on any one._"]
    if r["leaders"]:
        parts += ["## 🚀 Leaders — strongest vs Nifty",
                  md.table(["Sector", "RS vs Nifty", "Trend", "Valuation"], _rot_rows(r["leaders"]),
                           "lrll")]
    if r["turning"]:
        parts += ["## 💎 Turning up from cheap (value + momentum)",
                  md.table(["Sector", "RS vs Nifty", "Trend", "Valuation"], _rot_rows(r["turning"]),
                           "lrll")]
    if r["laggards"]:
        parts += ["## 🐌 Laggards — weakest vs Nifty",
                  md.table(["Sector", "RS vs Nifty", "Trend", "Valuation"], _rot_rows(r["laggards"]),
                           "lrll")]
    return "\n\n".join(parts) + _ROTATION_LEGEND


def _brief_for_llm(sector_name: str, data: dict) -> str:
    """Compact structured brief handed to the LLM (numbers only — it never re-derives)."""
    tech, val, sm, rk = (data["technicals"], data["valuation"], data["smart_money"], data["ranking"])
    L = [f"SECTOR: {sector_name} (index {data['index_name']})", "", "TECHNICALS:"]
    L += [f"- {s}" for s in (tech.get("signals", []) if tech else [])] or ["- (insufficient history)"]
    L.append("")
    if val:
        pct = val.get("own_history_pctile")
        hist = (f" (richer than {pct:.0f}% of its own {val.get('years')}y history)"
                if pct is not None else "")
        nifty_val = val.get("nifty_pe") if val["metric"] == "PE" else val.get("nifty_pb")
        L.append(f"VALUATION: {val['metric']} {val.get('current'):.1f} — {val.get('reading')}{hist}; "
                 f"Nifty-50 {val['metric']} {nifty_val if nifty_val is not None else float('nan'):.1f}; "
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
