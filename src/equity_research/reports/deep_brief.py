"""Deep fundamental + forensic brief — full statements and every derived ratio.

Multi-year Income Statement / Balance Sheet / Cash Flow (CFO·CFI·CFF) from the
XBRL `financials` table, plus a comprehensive derived layer: margins, returns
(ROE/ROCE/ROIC/ROA), leverage, liquidity, working-capital / cash-conversion,
FCF / FCFF / FCFE, CFO-quality (CFO/PAT, CFO/EBITDA) including 3- and 5-year
rolled figures, and the forensic scores with full component breakdowns.

History depth is data-bound (see docs/FUNDAMENTALS.md): P&L runs ~6 years; the
balance sheet and cash flow are present FY2023+ (older result XBRLs omit them).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

from equity_research.analysis import (forensic, fundamentals, lenders, ownership, quant, sector,
                                      technical, valuation)
from equity_research.analysis.fundamentals import load_annual
from equity_research.reports import glossary

log = logging.getLogger("equity-research.deep_brief")
CR = 1e7


def _f(v, nd=0, pct=False, x=False, lo=None, hi=None):
    """Format a number; ``n/a`` for missing/NaN/inf or values outside the plausible
    [lo, hi] band (a data artifact — e.g. a holding-co 1,000% net margin, a ratio
    blown up by near-zero equity). Bounds are only applied where passed."""
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "n/a"
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return "n/a"
    return f"{v:,.{nd}f}{'%' if pct else ''}{'x' if x else ''}"


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _forward_line(guidance: dict | None, snap: dict | None, evd: dict, lens: str) -> str | None:
    """A forward-multiple bullet from **explicit** management guidance (else None).
    Forward EV/EBITDA / P/E / P/S as the guided figures allow; clearly attributed."""
    if not guidance:
        return None
    fy = guidance.get("fy_label") or "next FY"
    src = guidance.get("source")
    mcap = (snap or {}).get("market_cap_cr")
    ev = (evd or {}).get("ev_cr")
    g_rev, g_ebitda = _num(guidance.get("revenue_cr")), _num(guidance.get("ebitda_cr"))
    g_margin, g_pat = _num(guidance.get("ebit_margin")), _num(guidance.get("pat_cr"))
    fwd_ebitda = g_ebitda if g_ebitda else (g_rev * g_margin / 100 if g_rev and g_margin else None)
    bits = []
    if lens != "financial" and ev and fwd_ebitda and fwd_ebitda > 0:
        bits.append(f"forward EV/EBITDA ~{ev / fwd_ebitda:.1f}x (EBITDA ₹{fwd_ebitda:,.0f} cr)")
    if mcap and g_pat and g_pat > 0:
        bits.append(f"forward P/E ~{mcap / g_pat:.1f}x (PAT ₹{g_pat:,.0f} cr)")
    if mcap and g_rev and g_rev > 0 and not fwd_ebitda and not (g_pat and g_pat > 0):
        bits.append(f"forward P/S ~{mcap / g_rev:.1f}x (revenue ₹{g_rev:,.0f} cr)")
    if not bits:
        return None
    return (f"- **Forward — on management's {fy} guidance{f' ({src})' if src else ''}:** "
            + " · ".join(bits))


def _insider_block(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    """Recent SEBI PIT insider/promoter trades (from `insider_trades`) + a net read.
    Returns [] if none stored."""
    rows = con.execute(
        "SELECT disclosure_dt, acq_name, category, mode, txn_type, qty, value_cr, "
        "hold_before_pct, hold_after_pct FROM insider_trades WHERE symbol = ?", [symbol]).fetchall()
    if not rows:
        return []

    def pdt(s):
        try:
            return datetime.strptime((s or "").strip(), "%d-%b-%Y %H:%M")
        except (ValueError, TypeError):
            return None

    keys = ("dt_s", "name", "cat", "mode", "txn", "qty", "val", "hb", "ha")
    recs = [dict(zip(keys, r)) for r in rows]
    for r in recs:
        r["dt"] = pdt(r["dt_s"])
    recs = sorted((r for r in recs if r["dt"]), key=lambda r: r["dt"], reverse=True)
    if not recs:
        return []

    def sig(r):                                      # promoter/director or open-market
        c, m = (r["cat"] or "").lower(), (r["mode"] or "").lower()
        return "promoter" in c or "director" in c or ("market" in m and "off" not in m)

    cutoff = datetime.now() - timedelta(days=183)
    net, have = 0.0, False
    for r in recs:
        if r["dt"] >= cutoff and sig(r) and r["val"]:
            have = True
            t = (r["txn"] or "").lower()
            net += r["val"] if "buy" in t else -r["val"] if "sell" in t else 0
    L = ["### Insider & promoter trades (recent)"]
    if have:
        dirn = "net buyers" if net > 0 else "net sellers" if net < 0 else "roughly flat"
        tag = " — conviction" if net > 0 else " — caution" if net < 0 else ""
        L.append(f"- **Net (last 6 mo, promoter/director + open-market): {dirn} "
                 f"₹{abs(net):,.1f} cr{tag}.**")
    else:
        L.append("- _Recent activity is routine (off-market / designated-person ESOP); "
                 "no material promoter/open-market signal._")
    for r in recs:                                   # all disclosures, newest-first — no cap
        t = (r["txn"] or "").lower()
        emo = "🟢" if "buy" in t else "🔴" if "sell" in t else "🔹"
        size = (f"₹{r['val']:,.2f} cr" if r["val"] and r["val"] >= 0.01
                else f"{r['qty']:,.0f} sh" if r["qty"] else "—")
        hb, ha = r["hb"], r["ha"]
        hold = f"; {hb:.2f}%→{ha:.2f}%" if hb is not None and ha is not None and (hb or ha) else ""
        L.append(f"- {emo} {r['cat'] or 'Insider'} {(r['name'] or '').title()} — "
                 f"{r['txn'] or 'traded'} {size} ({r['mode'] or 'n/a'}){hold} · {r['dt']:%d-%b-%Y}")
    L.append("")
    return L


def _shp_block(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    """Holder-level shareholding (latest SHP filing) — every promoter account + every
    public >1% holder, sorted highest→lowest, each tagged with what the holder IS
    (individual / LISTED company / unlisted pvt / MF / FPI / …). Listed-company holders
    are the *Elcid pattern*: a small listed vehicle sitting on a big stake. [] if none."""
    rows = con.execute(
        "SELECT holder_name, pct, category, is_promoter, classification, matched_symbol "
        "FROM shp_holders WHERE symbol = ? AND as_of = "
        "(SELECT max(as_of) FROM shp_holders WHERE symbol = ?) ORDER BY pct DESC",
        [symbol, symbol]).fetchall()
    if not rows:
        return []
    as_of = con.execute("SELECT max(as_of) FROM shp_holders WHERE symbol = ?", [symbol]).fetchone()[0]

    def tag(cls, matched):
        if cls == "LISTED company":
            return f"🏛 **LISTED company ({matched})**" if matched else "🏛 **LISTED company**"
        return {"unlisted pvt company": "🔒 unlisted pvt company",
                "unlisted company": "🔒 unlisted company",
                "individual / HUF": "👤 individual / HUF",
                "NRI / foreign individual": "👤 NRI / foreign individual",
                "mutual fund": "🏦 mutual fund", "insurance company": "🏦 insurance company",
                "FPI": "🌍 FPI", "bank / FI": "🏦 bank / FI", "trust": "🤝 trust",
                "LLP": "🔒 LLP", "employee trust": "🤝 employee trust",
                "government": "🏛 government"}.get(cls, cls)

    def fmt(r):
        name, pct, _cat, _prom, cls, matched = r
        return f"| {name} | {pct:.2f}% | {tag(cls, matched)} |"

    prom = [r for r in rows if r[3]]
    pub = [r for r in rows if not r[3]]
    L = [f"### Shareholding — who actually owns it (as of {as_of:%d-%b-%Y})", ""]
    listed_holders = [r for r in rows if r[4] == "LISTED company"]
    if listed_holders:
        names = " · ".join(f"**{r[0]}**{f' ({r[5]})' if r[5] else ''} at {r[1]:.2f}%"
                           for r in listed_holders)
        L += [f"🎯 **Listed-company holders (the Elcid pattern):** {names} — a listed vehicle "
              "holding a meaningful stake; check whether ITS market cap prices in this holding.", ""]
    if prom:
        total = sum(r[1] for r in prom)
        L += [f"**Promoter & promoter group** ({total:.2f}% across {len(prom)} accounts, "
              "highest→lowest):", "", "| Holder | % | What it is |", "|---|---|---|"]
        L += [fmt(r) for r in prom]
        L.append("")
    if pub:
        L += [f"**Public holders above 1%** ({len(pub)} disclosed, highest→lowest):", "",
              "| Holder | % | What it is |", "|---|---|---|"]
        L += [fmt(r) for r in pub]
        L.append("")
    L.append("_From the company's SEBI Reg-31 shareholding-pattern XBRL on NSE (primary). "
             "'LISTED company' means the holder itself trades on NSE — the way Elcid "
             "Investments held ~3% of Asian Paints._")
    L.append("")
    return L


def _ownership_changes_block(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    """Quarter-over-quarter ownership diff (needs ≥2 SHP snapshots) — who entered, exited,
    added or trimmed, notable holders (promoter / MF / FPI / listed) first. [] if <2 quarters."""
    ch = ownership.ownership_changes(con, symbol)
    if not ch:
        return []
    L = [f"### Ownership changes ({ch['prev_as_of']:%b-%Y} → {ch['as_of']:%b-%Y})", ""]
    z, cur = ch.get("action_zone"), ch.get("current_price")
    if z and cur:
        pos = ("above" if cur > z["hi"] else "below" if cur < z["lo"] else "within")
        L += [f"_These moves happened while the stock traded **₹{z['lo']:,.0f}–{z['hi']:,.0f}** "
              f"(~₹{z['avg']:,.0f} avg) that quarter — so that's roughly where they added / trimmed. "
              f"Now **₹{cur:,.0f}** ({pos} that zone)._", ""]
    if not (ch["entered"] or ch["exited"] or ch["added"] or ch["trimmed"]):
        L += ["- _No material change in the disclosed holder base quarter-over-quarter._", ""]
        return L

    def _who(r):
        star = " ⭐" if r["notable"] else ""
        kind = (r["classification"] if r["classification"] == "LISTED company"
                else "promoter" if r["is_promoter"] else r["category"])
        return f"**{r['name']}** ({kind}){star}"

    for r in ch["entered"]:
        L.append(f"- 🆕 **New:** {_who(r)} — 0.00% → {r['pct']:.2f}% (+{r['pct']:.2f}pp)")
    for r in ch["added"]:
        L.append(f"- 🟢 **Added:** {_who(r)} {r['prev_pct']:.2f}% → {r['pct']:.2f}% "
                 f"(+{r['delta']:.2f}pp)")
    for r in ch["trimmed"]:
        L.append(f"- 🔴 **Trimmed:** {_who(r)} {r['prev_pct']:.2f}% → {r['pct']:.2f}% "
                 f"({r['delta']:.2f}pp)")
    for r in ch["exited"]:
        L.append(f"- ⚪ **Exited:** {_who(r)} — {r['prev_pct']:.2f}% → 0.00% "
                 f"(−{r['prev_pct']:.2f}pp)")
    L += ["", "_Diff of the two most recent SEBI Reg-31 shareholding filings. ⭐ = notable "
          "(promoter / mutual fund / FPI / insurer / listed-company holder) — real "
          "conviction or distribution, above retail churn._", ""]
    return L


def _zone_str(lo, hi, avg=None) -> str:
    if lo is None:
        return "n/a"
    s = f"₹{lo:,.0f}–{hi:,.0f}"
    return s + (f" (~₹{avg:,.0f})" if avg else "")


def _smart_money_cost_block(con: duckdb.DuckDBPyConnection, symbol: str) -> list[str]:
    """Price context on the institutional holders: their inferred **cost zone** vs the current
    price → a profit-booking-risk read (the missing half of 'who holds how much'). [] if <2 SHP
    quarters or no price."""
    ic = ownership.institutional_cost(con, symbol)
    if not ic:
        return []
    known = [h for h in ic["holders"] if h["gain_pct"] is not None]
    unknown = [h for h in ic["holders"] if h["gain_pct"] is None]
    if not known and not unknown:
        return []
    s = ic["summary"]
    L = ["### 💰 Smart-money cost & profit-booking risk", ""]
    if s["avg_gain_pct"] is not None:
        L.append(f"At **₹{ic['current_price']:,.0f}**, the institutions whose cost we can estimate "
                 f"(from the quarters they added, {ic['window']['from']:%b-%Y}→{ic['window']['to']:%b-%Y}) "
                 f"are **{s['emoji']} {s['read']}** on a stake-weighted basis.")
    else:
        L.append(f"At **₹{ic['current_price']:,.0f}** — {s['emoji']} {s['read']}.")
    L.append("")
    if known:
        known.sort(key=lambda h: -(h["gain_pct"]))       # highest booking-risk first
        rows = [[h["name"], h["category"], f"{h['pct']:.2f}%",
                 _zone_str(h["zone_lo"], h["zone_hi"], h["avg_cost"]),
                 f"{h['emoji']} {h['read']}"] for h in known[:8]]
        L.append(_table(["Holder", "Type", "Stake", "Est. cost zone", "Now vs cost"], rows))
        L.append("")
    if unknown:
        L.append(f"_Plus {len(unknown)} large holder(s) (e.g. promoters / long-term funds) already "
                 f"holding in our earliest snapshot — they **entered before our data, so their cost "
                 f"is unknown** and isn't guessed._")
    L += ["", "_Exact transaction prices aren't disclosed; a holder's cost is **inferred** from the "
          "price range of the quarter(s) they added in (SEBI Reg-31 filings are quarterly). Near cost "
          "= little selling pressure; large gains = watch for profit-booking unless the stock is still "
          "in a strong uptrend. Coverage deepens as more shareholding history is ingested._", ""]
    return L


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _names(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """symbol → readable company name (sector_map.company, then equity_master.company_name)."""
    m: dict[str, str] = {}
    for sql, col in (("SELECT symbol, company FROM sector_map WHERE company IS NOT NULL", 0),
                     ("SELECT symbol, company_name FROM equity_master "
                      "WHERE company_name IS NOT NULL", 1)):
        try:
            for s, c in con.execute(sql).fetchall():
                m.setdefault(s, c)
        except Exception:  # noqa: BLE001 — either table/column may be absent; name falls back to symbol
            pass
    return m


# market-cap tier bands (₹ crore) — labelled inline so the reader sees the cutoffs used
_CAP_TIERS = [("Large cap (≥ ₹50,000 cr)", 50_000, float("inf")),
              ("Mid cap (₹10,000–50,000 cr)", 10_000, 50_000),
              ("Small cap (< ₹10,000 cr)", 0, 10_000)]


def _cap_tier(mcap_cr: float | None) -> str | None:
    if mcap_cr is None or mcap_cr != mcap_cr:
        return None
    for label, lo, hi in _CAP_TIERS:
        if lo <= mcap_cr < hi:
            return label
    return None


def _peer_comparison(con: duckdb.DuckDBPyConnection, symbol: str, consolidated: bool) -> list[str]:
    """Peer table grouped into large / mid / small cap (≤5 per tier), companies shown by
    **name** not symbol, the target marked ◄. Peers share the NSE industry. [] if too thin."""
    names = _names(con)
    pcols = (["P/E", "P/B", "ROE%", "ROA%", "NetMargin%", "GNPA%"] if fundamentals.is_bank(con, symbol)
             else ["P/E", "P/B", "ROE%", "ROCE%", "NetMargin%", "D/E"])
    recs = []
    for ps in [symbol, *sector.peers(con, symbol)]:
        r = quant._ratios(con, ps, consolidated)
        if not r:
            continue
        mcap = valuation.snapshot(con, ps, consolidated).get("market_cap_cr")
        recs.append({"sym": ps, "name": names.get(ps, ps), "mcap": mcap, "r": r,
                     "target": ps == symbol})
    if len(recs) < 2:
        return []
    by_tier: dict[str, list[dict]] = {t[0]: [] for t in _CAP_TIERS}
    unclassified: list[dict] = []
    for rec in recs:
        t = _cap_tier(rec["mcap"])
        (by_tier[t] if t else unclassified).append(rec)

    hdr = ["Company", "M-cap (₹cr)", *pcols]
    lines = ["", "### Peer comparison — by market-cap tier", "",
             "_Peers share this company's NSE industry classification, grouped by size (bands "
             "shown), up to 5 peers per tier ranked by market cap. Company names are shown, not "
             "tickers; ◄ marks the company this report is about (always listed in its own tier)._",
             ""]
    any_table = False
    for label, _lo, _hi in _CAP_TIERS:
        members = sorted(by_tier[label], key=lambda r: -(r["mcap"] or 0))
        if not members:
            continue
        chosen = [r for r in members if r["target"]]                # the target, if in this tier
        chosen += [r for r in members if not r["target"]][:5]       # + up to 5 peers
        chosen.sort(key=lambda r: -(r["mcap"] or 0))
        rows = [[rec["name"] + (" ◄" if rec["target"] else ""), _f(rec["mcap"], 0)]
                + [_f(rec["r"].get(c), 1) for c in pcols] for rec in chosen]
        lines += [f"**{label}**", "", _table(hdr, rows), ""]
        any_table = True
    return lines if any_table else []


def _cover(ebit, fin) -> str:
    """Interest coverage, capped — a near-zero finance cost (debt-free) otherwise
    shows a meaningless huge multiple (e.g. 21,948x)."""
    if ebit is None or fin is None or pd.isna(ebit) or pd.isna(fin) or fin == 0:
        return "n/a"
    v = ebit / fin
    return ">500x" if v > 500 else f"{v:,.1f}x"


_VERDICT_WORDS = ("BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL", "AVOID", "SWITCH")


def verdict_from_text(text: str | None) -> str | None:
    """Pull the headline call (Buy/Accumulate/…/Avoid) out of an LLM analysis so the
    levels setup can defer to it. Looks near the word 'verdict' first, then falls back to
    the first standalone verdict keyword. None when nothing matches."""
    if not text:
        return None
    head = text[:1500]
    m = re.search(r"verdict[^A-Za-z]{0,12}(" + "|".join(_VERDICT_WORDS) + r")", head, re.I)
    if not m:
        m = re.search(r"\b(" + "|".join(_VERDICT_WORDS) + r")\b", head, re.I)
    return m.group(1).upper() if m else None


def _dots(score: float) -> str:
    """Confluence score → 1–5 filled dots, a compact at-a-glance strength read."""
    n = int(min(5, max(1, round(score))))
    return "●" * n + "○" * (5 - n)


# jargon → plain English for the "built from" column and the narrative
_SRC_PLAIN = {"20-DMA": "the 20-day average", "50-DMA": "the 50-day average",
              "200-DMA": "the 200-day average", "swing": "a prior turning point",
              "volume-node": "a heavy-volume price band", "52w": "the 52-week extreme",
              "round": "a round number"}


def _sources_phrase(sources: list[str]) -> str:
    """Confluence sources → a readable clause, e.g. 'the 20- and 50-day averages, a
    heavy-volume price band and a round number'."""
    seen: list[str] = []
    for s in sources:
        p = _SRC_PLAIN.get(s, s)
        if p not in seen:
            seen.append(p)
    if not seen:
        return ""
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen[:-1]) + " and " + seen[-1]


def _zone_rows(zones: list[dict]) -> list[list[str]]:
    return [[f"{z['lo']:,.0f}–{z['hi']:,.0f}" if z["hi"] - z["lo"] >= 0.5 else f"{z['mid']:,.0f}",
             _dots(z["score"]), _sources_phrase(z["sources"])] for z in zones]


def _levels_narrative(symbol: str, lv: dict) -> str:
    """A plain-English reading of the levels for a non-technical reader — what the trend is,
    where price sits vs its floor and ceiling, and what the setup means in rupee terms. Built
    entirely from the ``levels`` dict (deterministic)."""
    price = lv["close"]
    atr = lv.get("atr") or 0.0
    st = lv["structure"]
    sups, ress = lv.get("supports", []), lv.get("resistances", [])
    setup = lv.get("setup", {})
    out: list[str] = []

    out.append({
        "up": f"**{symbol} is in an up-trend** — a rising staircase of higher highs and higher "
              "lows, which is the healthiest backdrop for buying dips.",
        "down": f"**{symbol} is in a down-trend** — lower highs and lower lows, so rallies tend "
                "to get sold; treat bounces with caution.",
        "range": f"**{symbol} is range-bound** — drifting sideways rather than trending, so it "
                 "tends to swing between a floor (support) and a ceiling (resistance) until it "
                 "breaks decisively out of that band.",
    }.get(st.get("trend"), f"{symbol}'s trend is unclear."))

    if sups:
        ns = sups[0]
        strong = max(sups, key=lambda z: z["score"])
        in_zone = ns["lo"] - 0.5 * atr <= price <= ns["hi"] + 0.5 * atr
        verb = "sitting right on" if in_zone else "holding above"
        src = _sources_phrase(ns["sources"])
        out.append(f"At ₹{price:,.0f} it's {verb} its nearest floor, ₹{ns['lo']:,.0f}–₹{ns['hi']:,.0f}"
                   + (f" — a spot where {src} all coincide, and the more of those that line up the "
                      "more likely buyers defend it." if src else "."))
        if strong is not ns and strong["mid"] < ns["mid"]:
            out.append(f"The firmest support lower down is ₹{strong['lo']:,.0f}–₹{strong['hi']:,.0f} "
                       f"({_sources_phrase(strong['sources'])}) — the level to watch on a deeper pullback.")
    else:
        out.append(f"At ₹{price:,.0f} there's no clear support mapped just below — it's near the "
                   "lower end of its recent range, so a floor isn't well defined yet.")

    if ress:
        nr = ress[0]
        gap = 100 * (nr["mid"] - price) / price if price else 0
        near = ("almost immediately overhead" if gap < 3
                else f"about {gap:.0f}% above" if gap < 25 else f"far above (~{gap:.0f}%)")
        out.append(f"The first ceiling above is ₹{nr['lo']:,.0f}–₹{nr['hi']:,.0f}, {near}; a break "
                   "and a daily *close* above it is what would signal the next leg up.")
    else:
        out.append("There's no resistance mapped overhead — it's at or near its highs (blue-sky), so "
                   "momentum rather than a level is the guide here; trail a stop rather than aim at a target.")

    kind = setup.get("kind")
    if kind == "reference-only":
        out.append("Because the **fundamental verdict above is negative**, these levels are **for "
                   "reference only** — a way to gauge risk if you already hold, not a reason to buy.")
    else:
        rr, stop = setup.get("rr"), setup.get("stop")
        tgts, elo, ehi = setup.get("targets") or [], setup.get("entry_lo"), setup.get("entry_hi")
        if rr and stop and tgts and elo is not None:
            emid = (elo + ehi) / 2
            risk, reward = emid - stop, tgts[0] - emid
            if kind == "accumulate":
                out.append(f"**The risk/reward is attractive:** buying near ₹{elo:,.0f}–₹{ehi:,.0f} "
                           f"risks about ₹{risk:,.0f} a share (down to the ₹{stop:,.0f} stop) to aim "
                           f"at roughly ₹{reward:,.0f} of upside (the ₹{tgts[0]:,.0f} target) — a "
                           f"reward:risk of {rr:.1f}:1, comfortably past the ~1.5:1 bar worth taking.")
            else:
                out.append(f"**The risk/reward is poor right now:** from here you'd risk about "
                           f"₹{risk:,.0f} a share (to the ₹{stop:,.0f} stop) to chase only ~₹{reward:,.0f} "
                           f"of upside (the ₹{tgts[0]:,.0f} target) — just {rr:.1f}:1. Better to wait for "
                           "a cheaper entry deeper into support, or a confirmed breakout above the "
                           "ceiling, before committing.")
        elif setup.get("note"):
            out.append(setup["note"])
    return " ".join(out)


def render_levels(con: duckdb.DuckDBPyConnection, symbol: str, lv: dict | None) -> list[str]:
    """Markdown for the 'Trading levels & setup' section from a ``technical.levels`` dict.
    [] when the history is too thin (the report then simply omits the section)."""
    if not lv or not lv.get("history_ok"):
        if lv and lv.get("n_days"):
            return ["", "## Trading levels & setup",
                    f"_Only {lv['n_days']} sessions of price history on file — too thin for "
                    "reliable support/resistance levels. Skipped._"]
        return []
    st = lv["structure"]
    trend_lbl = {"up": "up-trend (higher highs & lows)", "down": "down-trend (lower highs & lows)",
                 "range": "range-bound (no clear trend)"}.get(st["trend"], st["trend"])
    L = ["", "## Trading levels & setup",
         "_Computed from the daily price history — support/resistance **zones** (confluence of "
         "swing pivots, moving averages, 52-week extremes, volume-by-price and round numbers), "
         "market structure, and a reward:risk-framed entry/stop/target. These are **timing levels, "
         "not a call** — they defer to the fundamental verdict above._", ""]
    if not lv.get("reliable"):
        L.append(f"⚠ Limited history ({lv['n_days']} sessions) — treat these zones as indicative.")
        L.append("")
    struct_line = f"**Structure:** {trend_lbl}"
    if st.get("last_swing_high"):
        struct_line += f" · last swing high ₹{st['last_swing_high']:,.0f}"
    if st.get("last_swing_low"):
        struct_line += f" · last swing low ₹{st['last_swing_low']:,.0f}"
    struct_line += f" · close ₹{lv['close']:,.2f} · ATR {lv['atr_pct']}%"
    L += [struct_line, ""]

    # plain-English reading first — the story, before the reference tables
    L += ["**In plain English.** " + _levels_narrative(symbol, lv), ""]

    hdr = ["Zone (₹)", "Confluence", "Built from"]
    if lv["supports"]:
        L += ["**Support zones (nearest first)**", "", _table(hdr, _zone_rows(lv["supports"])), ""]
    if lv["resistances"]:
        L += ["**Resistance zones (nearest first)**", "",
              _table(hdr, _zone_rows(lv["resistances"])), ""]
    if lv["patterns"]:
        pats = "; ".join(f"**{p['name']}** ({p['direction']}) — {p['note']}" for p in lv["patterns"])
        L += [f"**Patterns:** {pats}", ""]
    s = lv["setup"]
    L += [f"**Setup — {s.get('bias', s.get('kind'))}:** {s['note']}", ""]
    L += ["_How to read. Support is where buyers have repeatedly stepped in (a floor); resistance "
          "where sellers have capped it (a ceiling). A zone backed by more methods — higher "
          "confluence (more dots) — and by more volume is a stronger level than any single line. "
          "The **stop** is your invalidation: if price closes below it, the setup was wrong, so you "
          "exit small rather than hope. **Reward:risk** weighs the distance to the first target "
          "against the distance to the stop — below ~1.5:1 the trade isn't worth the risk. Levels "
          "are probabilities, not promises, and they never override the fundamental verdict above._"]
    return L


def _bank_frames(con: duckdb.DuckDBPyConnection, symbol: str, af: pd.DataFrame,
                 consolidated: bool) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """(annual metrics, quarterly metrics, borrowed) for a bank. On a consolidated report the
    regulatory ratios (NPA %, CET1) come from the standalone filing — ``borrowed`` says so."""
    am = lenders.annual_metrics(af)
    qm = lenders.quarterly_metrics(fundamentals.load_quarters(con, symbol, consolidated))
    borrowed = False
    if consolidated:
        am, b1 = lenders.with_regulatory_fallback(
            am, lenders.annual_metrics(load_annual(con, symbol, False)))
        qm, b2 = lenders.with_regulatory_fallback(
            qm, lenders.quarterly_metrics(fundamentals.load_quarters(con, symbol, False)))
        borrowed = b1 or b2
    return am, qm, borrowed


def _bank_sections(con: duckdb.DuckDBPyConnection, symbol: str, af: pd.DataFrame,
                   consolidated: bool) -> list[str]:
    """§1–§8 for a bank — its earnings engine, returns, balance sheet, asset quality, capital and
    quarterly trend — instead of the industrial statements (EBITDA, working capital, FCF)."""
    am, qm, borrowed = _bank_frames(con, symbol, af, consolidated)
    L = ["> 🏦 **This is a bank**, so it's read on a bank's numbers — its earnings engine (net "
         "interest income), asset quality (bad loans), capital and funding — rather than the "
         "industrial yardsticks (EBITDA, working-capital days, free cash flow) that don't fit a "
         "lender, whose raw material is deposits and whose product is loans.", ""]
    if am.empty:
        return L + ["_No annual bank statements on file._", ""]

    def row(label: str, col: str, years: list, nd: int = 0, pct: bool = False,
            lo: float | None = None, hi: float | None = None) -> list[str]:
        vals = am[col] if col in am else pd.Series(np.nan, index=am.index)
        return [label] + [_f(vals.get(y), nd, pct=pct, lo=lo, hi=hi) for y in years]

    def fy(years: list) -> list[str]:
        return [f"FY{y.year}" for y in years]

    years = [y for y in am.index if not pd.isna(am.at[y, "interest_earned_cr"])]
    if years:
        L += ["## 1. Income statement (bank)", _table(["₹ crore"] + fy(years), [
            row("Interest earned", "interest_earned_cr", years),
            row("Interest expended", "interest_expended_cr", years),
            row("**Net interest income (NII)**", "nii_cr", years),
            row("Other income (fees, treasury, recoveries)", "other_income_cr", years),
            row("Operating expenses", "opex_cr", years),
            row("  of which employee cost", "employee_cr", years),
            row("**Operating profit before provisions (PPOP)**", "ppop_cr", years),
            row("Provisions & contingencies", "provisions_cr", years),
            row("Profit before tax", "pbt_cr", years),
            row("Tax", "tax_cr", years),
            row("**Net profit (PAT)**", "pat_cr", years),
        ]), ""]
        L += ["## 2. Margins, returns & growth", _table(["Metric"] + fy(years), [
            row("NIM (NII ÷ avg total assets)", "nim_%", years, 2, True, -5, 20),
            row("Cost-to-income", "cost_to_income_%", years, 1, True, 0, 150),
            row("Credit cost (provisions ÷ avg loans)", "credit_cost_%", years, 2, True, -10, 20),
            row("ROA", "roa_%", years, 2, True, -10, 10),
            row("ROE", "roe_%", years, 1, True, -100, 100),
            row("Other income ÷ (NII + other income)", "fee_share_%", years, 1, True, -50, 100),
            row("NII YoY", "nii_yoy_%", years, 1, True, -100, 500),
            row("PPOP YoY", "ppop_yoy_%", years, 1, True, -100, 500),
            row("PAT YoY", "pat_yoy_%", years, 1, True, -100, 1000),
        ]), "",
            "**How to read this.** **NII** is the bank's gross margin in rupees — what it earns on "
            "loans and investments minus what it pays depositors. **NIM** is that as a % of assets "
            "(here on total assets; the bank's own figure, on interest-earning assets, runs a little "
            "higher). **Cost-to-income** is how much of each rupee of income goes on running the "
            "bank — under ~45% is efficient. **Credit cost** is the year's provisions for bad loans "
            "as a % of loans — the single biggest swing factor in bank profits. **ROA** above ~1.5% "
            "is top-tier for an Indian bank; **ROE** is what shareholders earn on their capital and "
            "is what the P/B in §10 has to be justified by.", ""]

    by = [y for y in am.index if not pd.isna(am.at[y, "assets_cr"])]
    if by:
        L += ["## 3. Balance sheet (bank)", _table(["₹ crore"] + fy(by), [
            row("Advances (loans)", "advances_cr", by),
            row("Investments", "investments_cr", by),
            row("**Total assets**", "assets_cr", by),
            row("Deposits", "deposits_cr", by),
            row("Borrowings", "borrowings_cr", by),
            row("**Net worth (capital + reserves)**", "networth_cr", by),
            row("Book value per share (₹)", "bvps", by, 1, lo=0, hi=100_000),
        ]), ""]
        shares = (af["EquityShareCapital"] / af["FaceValueOfEquityShareCapital"]
                  if {"EquityShareCapital", "FaceValueOfEquityShareCapital"} <= set(af.columns)
                  else pd.Series(dtype=float)).dropna()
        jumps = [(y, r) for y, r in (shares / shares.shift(1)).dropna().items() if r >= 1.8]
        for y, r in jumps:
            L += [f"_Share count rose ~{r:.0f}× in FY{y.year} (a bonus issue or split), so book value "
                  "per share isn't comparable across that year — net worth itself kept growing._", ""]

    qy = [y for y in am.index if not all(pd.isna(am.at[y, c]) for c in
                                         ("gnpa_%", "nnpa_%", "gnpa_cr", "cet1_%"))]
    if qy:
        L += ["## 4. Asset quality — bad loans", _table(["Metric"] + fy(qy), [
            row("Gross NPA (₹ cr)", "gnpa_cr", qy),
            row("Gross NPA %", "gnpa_%", qy, 2, True, 0, 60),
            row("Net NPA (₹ cr)", "nnpa_cr", qy),
            row("Net NPA %", "nnpa_%", qy, 2, True, 0, 40),
            row("Provision coverage (PCR)", "pcr_%", qy, 1, True, 0, 100),
        ]), "",
            "**How to read this.** A **non-performing asset (NPA)** is a loan that has stopped paying "
            "(90+ days overdue). **Gross NPA %** is the share of all loans gone bad; **net NPA %** is "
            "what's left after the bank has already set money aside (provisions) against them — the "
            "part that could still hurt profit. **Provision coverage** is how much of the bad loans is "
            "already provided for: above ~70% is a thick cushion. The direction matters more than "
            "the level — rising NPAs lead rising credit costs by a few quarters.", ""]
    fy_cap = [y for y in am.index if not all(pd.isna(am.at[y, c]) for c in
                                            ("cet1_%", "cd_ratio_%", "adv_yoy_%", "dep_yoy_%"))]
    if fy_cap:
        L += ["## 5. Capital & funding", _table(["Metric"] + fy(fy_cap), [
            row("CET1 ratio", "cet1_%", fy_cap, 1, True, 0, 60),
            row("Tier-1 ratio", "tier1_%", fy_cap, 1, True, 0, 60),
            row("Credit-deposit (CD) ratio", "cd_ratio_%", fy_cap, 1, True, 0, 300),
            row("Advances (loan) growth YoY", "adv_yoy_%", fy_cap, 1, True, -100, 500),
            row("Deposit growth YoY", "dep_yoy_%", fy_cap, 1, True, -100, 500),
        ]), "",
            "**How to read this.** **CET1** is the bank's core equity as a % of its risk-weighted "
            "loans — its loss-absorbing cushion. RBI's floor, including the conservation buffer, is "
            "8%; well above that means room to grow without raising fresh equity. The **CD ratio** is "
            "loans ÷ deposits: above ~90% the bank is lending faster than it gathers deposits and has "
            "to lean on costlier borrowings, which squeezes margins. Loan growth running far ahead "
            "of deposit growth is the same warning in motion.", ""]
    if borrowed:
        L += ["_NPA and capital ratios are regulatory figures reported for the **bank** itself; "
              "consolidated filings leave them blank, so they are taken from the standalone results._",
              ""]
    L += ["## 6. Cash flow — context only for a bank",
          "A bank's operating cash flow mostly records deposits coming in and loans going out, so it "
          "swings with balance-sheet growth rather than with how well profit turns into cash. "
          "CFO ÷ PAT, free cash flow and accruals therefore aren't earnings-quality signals for a "
          "bank and are left out.", "",
          "## 7. Why the industrial ratios are left out",
          "EBITDA, interest cover, debt ÷ equity, working-capital days and the Altman / Piotroski / "
          "Beneish scores all assume an industrial balance sheet — inventories, receivables, "
          "borrowings used to fund a plant. For a bank, interest paid is its cost of raw material and "
          "deposits are not 'debt' in that sense, so those numbers would mislead (every bank would "
          "look dangerously over-borrowed). The bank measures above, and the health checks in §9, "
          "replace them.", ""]

    if not qm.empty:
        q = qm.tail(8)

        def qrow(label: str, col: str, nd: int = 0, pct: bool = False,
                 lo: float | None = None, hi: float | None = None) -> list[str]:
            vals = q[col] if col in q else pd.Series(np.nan, index=q.index)
            return [label] + [_f(v, nd, pct=pct, lo=lo, hi=hi) for v in vals]
        L += [f"## 8. Quarterly trend (last {len(q)}q)", _table(
            ["Quarter"] + [str(i.date()) for i in q.index], [
                qrow("Interest earned (₹cr)", "interest_earned_cr"),
                qrow("NII (₹cr)", "nii_cr"),
                qrow("NII YoY", "nii_yoy_%", 1, True, -100, 500),
                qrow("Other income (₹cr)", "other_income_cr"),
                qrow("PPOP (₹cr)", "ppop_cr"),
                qrow("Provisions (₹cr)", "provisions_cr"),
                qrow("Net profit (₹cr)", "pat_cr"),
                qrow("PAT YoY", "pat_yoy_%", 1, True, -100, 1000),
                qrow("Gross NPA %", "gnpa_%", 2, True, 0, 60),
                qrow("Net NPA %", "nnpa_%", 2, True, 0, 40),
                qrow("Provision coverage", "pcr_%", 1, True, 0, 100),
                qrow("CET1", "cet1_%", 1, True, 0, 60),
            ]), ""]
    return L


def _bank_forensics(con: duckdb.DuckDBPyConnection, symbol: str, af: pd.DataFrame,
                    consolidated: bool) -> list[str]:
    """§9 for a bank: why the industrial scores don't apply, then the bank health checks."""
    am, qm, _ = _bank_frames(con, symbol, af, consolidated)
    icon = {"ok": "✅", "warn": "⚠️", "alarm": "🔴"}
    L = ["- **Altman Z · Piotroski F · Beneish M · Sloan accruals — not applicable to banks.** "
         "They are built from industrial balance-sheet items (inventories, receivables, current "
         "assets, PP&E) that a bank doesn't have, so they are replaced by the checks below.",
         "", "**🏦 Bank health checks** (from the figures in §1–§8):"]
    checks = lenders.health_checks(am, qm)
    L += [f"- {icon[s]} {t}" for s, t in checks] or ["- _Not enough bank data on file to run the checks._"]
    L.append("")
    return L


def _statement_sections(con: duckdb.DuckDBPyConnection, symbol: str, af: pd.DataFrame,
                        consolidated: bool) -> tuple[list[str], pd.Series, pd.Series]:
    """§1–§8 for an industrial / services company: income statement, margins, balance sheet,
    returns & leverage, working capital, cash flow, FCF & earnings quality, quarterly trend.
    Returns ``(lines, cfo, pat)`` — the forensic section cross-checks Beneish against CFO/PAT."""
    L: list[str] = []
    def s(el: str) -> pd.Series:
        return af[el] if el in af.columns else pd.Series(np.nan, index=af.index)

    yrs = list(af.index)

    def cells(series, nd=0, pct=False, x=False, div=CR):
        return [_f(None if pd.isna(series.get(y)) else series.get(y) / div, nd, pct, x)
                for y in years]

    # ---- raw series (₹) ----
    rev, oi, inc = s("RevenueFromOperations"), s("OtherIncome"), s("Income")
    cogs = (s("CostOfMaterialsConsumed").fillna(0) + s("PurchasesOfStockInTrade").fillna(0)
            + s("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade").fillna(0))
    cogs = cogs.where(s("CostOfMaterialsConsumed").notna())
    emp, fin, dep = s("EmployeeBenefitExpense"), s("FinanceCosts"), s("DepreciationDepletionAndAmortisationExpense")
    oexp, texp = s("OtherExpenses"), s("Expenses")
    pbeit, exc = s("ProfitBeforeExceptionalItemsAndTax"), s("ExceptionalItemsBeforeTax")
    pbt, ctax, dtax, tax = s("ProfitBeforeTax"), s("CurrentTax"), s("DeferredTax"), s("TaxExpense")
    pat, ci = s("ProfitLossForPeriod"), s("ComprehensiveIncomeForThePeriod")
    ebit, ebitda = pbt + fin, pbt + fin + dep
    gp = rev - cogs

    assets, ca, nca = s("Assets"), s("CurrentAssets"), s("NoncurrentAssets")
    ppe, inv, recv = s("PropertyPlantAndEquipment"), s("Inventories"), s("TradeReceivablesCurrent")
    cash = s("CashAndCashEquivalents")
    eq, shcap, oeq = s("Equity"), s("EquityShareCapital"), s("OtherEquity")
    liab, cl, ncl = s("Liabilities"), s("CurrentLiabilities"), s("NoncurrentLiabilities")
    debt_c, debt_nc = s("BorrowingsCurrent"), s("BorrowingsNoncurrent")
    payables = s("TradePayablesCurrent")
    debt = debt_c.add(debt_nc, fill_value=0).where(debt_c.notna() | debt_nc.notna())
    netdebt = debt - cash

    cfo, cfi, cff = (s("CashFlowsFromUsedInOperatingActivities"),
                     s("CashFlowsFromUsedInInvestingActivities"),
                     s("CashFlowsFromUsedInFinancingActivities"))
    capex = s("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities").abs()
    borrow_in = s("ProceedsFromBorrowingsClassifiedAsFinancingActivities")
    borrow_out = s("RepaymentsOfBorrowingsClassifiedAsFinancingActivities")
    net_borrow = borrow_in.fillna(0) - borrow_out.fillna(0)
    div_paid = s("DividendsPaidClassifiedAsFinancingActivities").abs()
    tax_rate = tax / pbt
    fcf = cfo - capex
    fcff = cfo - capex + fin * (1 - tax_rate)
    fcfe = cfo - capex + net_borrow

    # ===================== INCOME STATEMENT =====================
    years = [y for y in yrs if not pd.isna(rev.get(y))]
    hdr = ["Income statement"] + [f"FY{y.year}" for y in years]
    rows1 = [
        ["Revenue from operations"] + cells(rev),
        ["Other income"] + cells(oi),
        ["Total income"] + cells(inc),
        ["COGS (materials+purchases+Δinv)"] + cells(cogs),
        ["Employee benefit expense"] + cells(emp),
        ["Finance costs"] + cells(fin),
        ["Depreciation & amortisation"] + cells(dep),
        ["Other expenses"] + cells(oexp),
        ["Total expenses"] + cells(texp),
        ["EBITDA"] + cells(ebitda),
        ["EBIT"] + cells(ebit),
        ["Profit before excep. & tax"] + cells(pbeit),
        ["Exceptional items"] + cells(exc),
        ["Profit before tax"] + cells(pbt),
        ["  Current tax"] + cells(ctax),
        ["  Deferred tax"] + cells(dtax),
        ["Total tax"] + cells(tax),
        ["Net profit (PAT)"] + cells(pat),
        ["Comprehensive income"] + cells(ci),
    ]
    L += ["## 1. Income statement", _table(hdr, rows1), ""]

    # ---- margins & growth ----
    def yoy(series):
        return series / series.shift(1) - 1
    rows2 = [
        ["Gross margin"] + [_f(None if pd.isna(gp.get(y)) else 100 * gp.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["EBITDA margin"] + [_f(None if pd.isna(ebitda.get(y)) else 100 * ebitda.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["EBIT margin"] + [_f(None if pd.isna(ebit.get(y)) else 100 * ebit.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["PBT margin"] + [_f(None if pd.isna(pbt.get(y)) else 100 * pbt.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["Net margin"] + [_f(None if pd.isna(pat.get(y)) else 100 * pat.get(y) / rev.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
        ["Effective tax rate"] + [_f(None if pd.isna(tax_rate.get(y)) else 100 * tax_rate.get(y), 1, pct=True, lo=0, hi=80) for y in years],
        ["Revenue YoY"] + [_f(None if pd.isna(yoy(rev).get(y)) else 100 * yoy(rev).get(y), 1, pct=True, lo=-100, hi=500) for y in years],
        ["PAT YoY"] + [_f(None if pd.isna(yoy(pat).get(y)) else 100 * yoy(pat).get(y), 1, pct=True, lo=-100, hi=500) for y in years],
        ["Other income / PBT"] + [_f(None if pd.isna(oi.get(y)) or pd.isna(pbt.get(y)) else 100 * oi.get(y) / pbt.get(y), 1, pct=True, lo=-200, hi=300) for y in years],
    ]
    L += ["## 2. Profitability, margins & growth", _table(
        ["Metric"] + [f"FY{y.year}" for y in years], rows2), ""]

    # ===================== BALANCE SHEET =====================
    by = [y for y in yrs if not pd.isna(assets.get(y))]
    if by:
        years = by
        hdr = ["Balance sheet"] + [f"FY{y.year}" for y in years]
        L += ["## 3. Balance sheet", _table(hdr, [
            ["Property, plant & equipment"] + cells(ppe),
            ["Non-current assets (total)"] + cells(nca),
            ["Inventories"] + cells(inv),
            ["Trade receivables (current)"] + cells(recv),
            ["Cash & equivalents"] + cells(cash),
            ["Current assets (total)"] + cells(ca),
            ["**Total assets**"] + cells(assets),
            ["Equity share capital"] + cells(shcap),
            ["Other equity (reserves)"] + cells(oeq),
            ["**Total equity**"] + cells(eq),
            ["Borrowings — non-current"] + cells(debt_nc),
            ["Borrowings — current"] + cells(debt_c),
            ["Total debt"] + cells(debt),
            ["Trade payables (current)"] + cells(payables),
            ["Current liabilities (total)"] + cells(cl),
            ["Non-current liabilities (total)"] + cells(ncl),
            ["**Total liabilities**"] + cells(liab),
            ["Net debt (debt − cash)"] + cells(netdebt),
        ]), ""]

        # returns / leverage / liquidity (balance-sheet years)
        L += ["## 4. Returns, leverage & liquidity", _table(
            ["Metric"] + [f"FY{y.year}" for y in years], [
                ["ROE (PAT/equity)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(eq.get(y)) else 100 * pat.get(y) / eq.get(y), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROCE (EBIT/(eq+debt))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) / (eq.get(y) + (debt.get(y) or 0)), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROIC (EBIT(1−t)/(eq+debt−cash))"] + [_f(None if pd.isna(ebit.get(y)) or pd.isna(eq.get(y)) else 100 * ebit.get(y) * (1 - (tax_rate.get(y) if not pd.isna(tax_rate.get(y)) else 0)) / (eq.get(y) + (debt.get(y) or 0) - (cash.get(y) or 0)), 1, pct=True, lo=-100, hi=300) for y in years],
                ["ROA (PAT/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(assets.get(y)) else 100 * pat.get(y) / assets.get(y), 1, pct=True, lo=-100, hi=100) for y in years],
                ["Debt / equity"] + [_f(None if pd.isna(debt.get(y)) or pd.isna(eq.get(y)) else debt.get(y) / eq.get(y), 2, x=True, lo=0, hi=50) for y in years],
                ["Net debt / EBITDA"] + [_f(None if pd.isna(netdebt.get(y)) or pd.isna(ebitda.get(y)) else netdebt.get(y) / ebitda.get(y), 2, x=True, lo=-50, hi=50) for y in years],
                ["Interest coverage (EBIT/int)"] + [_cover(ebit.get(y), fin.get(y)) for y in years],
                ["Current ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else ca.get(y) / cl.get(y), 2, x=True, lo=0, hi=50) for y in years],
                ["Quick ratio"] + [_f(None if pd.isna(ca.get(y)) or pd.isna(cl.get(y)) else (ca.get(y) - (inv.get(y) or 0)) / cl.get(y), 2, x=True, lo=0, hi=50) for y in years],
            ]), ""]

        # working capital / cash conversion
        L += ["## 5. Working capital & cash conversion", _table(
            ["Metric (days)"] + [f"FY{y.year}" for y in years], [
                ["Receivable days"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(rev.get(y)) else 365 * recv.get(y) / rev.get(y), 0, lo=0, hi=2000) for y in years],
                ["Inventory days"] + [_f(None if pd.isna(inv.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) else 365 * inv.get(y) / cogs.get(y), 0, lo=0, hi=2000) for y in years],
                ["Payable days"] + [_f(None if pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) else 365 * payables.get(y) / cogs.get(y), 0, lo=0, hi=2000) for y in years],
                ["Cash conversion cycle"] + [_f(None if pd.isna(recv.get(y)) or pd.isna(inv.get(y)) or pd.isna(payables.get(y)) or pd.isna(cogs.get(y)) or not cogs.get(y) or not rev.get(y) else 365 * (recv.get(y) / rev.get(y) + inv.get(y) / cogs.get(y) - payables.get(y) / cogs.get(y)), 0, lo=-1000, hi=2000) for y in years],
                ["Asset turnover (Rev/assets)"] + [_f(None if pd.isna(rev.get(y)) or pd.isna(assets.get(y)) else rev.get(y) / assets.get(y), 2, x=True, lo=0, hi=20) for y in years],
            ]), ""]

    # ===================== CASH FLOW =====================
    cy = [y for y in yrs if not pd.isna(cfo.get(y))]
    if cy:
        years = cy
        hdr = ["Cash flow"] + [f"FY{y.year}" for y in years]
        L += ["## 6. Cash flow statement", _table(hdr, [
            ["CFO — operating"] + cells(cfo),
            ["CFI — investing"] + cells(cfi),
            ["CFF — financing"] + cells(cff),
            ["  Capex (PP&E purchase)"] + cells(capex),
            ["  Borrowings raised"] + cells(borrow_in),
            ["  Borrowings repaid"] + cells(borrow_out),
            ["  Dividends paid"] + cells(div_paid),
            ["Net change in cash"] + cells(s("IncreaseDecreaseInCashAndCashEquivalents")),
        ]), ""]

        # free cash flow & cash quality
        L += ["## 7. Free cash flow & earnings quality", _table(
            ["Metric"] + [f"FY{y.year}" for y in years], [
                ["FCF (CFO−Capex)"] + cells(fcf),
                ["FCFF (CFO−Capex+Int(1−t))"] + cells(fcff),
                ["FCFE (CFO−Capex+NetBorrow)"] + cells(fcfe),
                ["CFO / PAT"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(pat.get(y)) else cfo.get(y) / pat.get(y), 2, x=True, lo=-50, hi=50) for y in years],
                ["CFO / EBITDA"] + [_f(None if pd.isna(cfo.get(y)) or pd.isna(ebitda.get(y)) else 100 * cfo.get(y) / ebitda.get(y), 0, pct=True, lo=-100, hi=200) for y in years],
                ["Accruals ((PAT−CFO)/assets)"] + [_f(None if pd.isna(pat.get(y)) or pd.isna(cfo.get(y)) or pd.isna(assets.get(y)) else 100 * (pat.get(y) - cfo.get(y)) / assets.get(y), 1, pct=True, lo=-150, hi=150) for y in years],
            ]), ""]

        # rolling CFO quality
        v = pd.DataFrame({"cfo": cfo, "pat": pat, "ebitda": ebitda}).dropna(subset=["cfo"]).sort_index()
        def roll_ratio(num, den, n):
            if len(v) < n:
                return None
            t = v.tail(n)
            return t[num].sum() / t[den].sum() if t[den].sum() else None
        L += ["**Rolled cash quality (most recent window):**",
              f"- 3-yr CFO/PAT: {_f(roll_ratio('cfo','pat',3), 2, x=True, lo=-50, hi=50)} · "
              f"5-yr CFO/PAT: {_f(roll_ratio('cfo','pat',5), 2, x=True, lo=-50, hi=50)}",
              f"- 3-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',3) is None else 100*roll_ratio('cfo','ebitda',3), 0, pct=True, lo=-100, hi=200)} · "
              f"5-yr CFO/EBITDA: {_f(None if roll_ratio('cfo','ebitda',5) is None else 100*roll_ratio('cfo','ebitda',5), 0, pct=True, lo=-100, hi=200)}", ""]

    # ===================== QUARTERLY MOMENTUM =====================
    qm = fundamentals.quarterly_metrics(con, symbol, consolidated)
    if not qm.empty:
        q = qm.tail(8)
        hdr = ["Quarter"] + [str(i.date()) for i in q.index]
        L += [f"## 8. Quarterly P&L trend (last {len(q)}q)", _table(hdr, [
            ["Revenue (₹cr)"] + [_f(x, 0) for x in q["revenue_cr"]],
            ["Net profit (₹cr)"] + [_f(x, 0) for x in q["net_profit_cr"]],
            ["Net margin"] + [_f(x, 1, pct=True, lo=-100, hi=100) for x in q["net_margin_%"]],
            ["EBITDA margin"] + [_f(x, 1, pct=True, lo=-100, hi=100) for x in q["ebitda_margin_%"]],
            ["Rev YoY"] + [_f(x, 1, pct=True, lo=-100, hi=500) for x in q["rev_yoy_%"]],
            ["PAT YoY"] + [_f(x, 1, pct=True, lo=-100, hi=500) for x in q["net_yoy_%"]],
            ["Interest cover"] + [_f(x, 1, x=True, lo=-50, hi=500) for x in q["interest_cover_x"]],
        ]), ""]
    return L, cfo, pat


def build_deep_brief(con: duckdb.DuckDBPyConnection, symbol: str, *,
                     consolidated: bool = False, target_shares: float | None = None,
                     guidance: dict | None = None, overview: str | None = None,
                     share_action: dict | None = None) -> str:
    af = load_annual(con, symbol, consolidated)        # index=year-end, cols=elements (₹)
    label = "consolidated" if consolidated else "standalone"
    L = [f"# {symbol} — deep fundamental & forensic brief ({label})\n",
         f"_Report generated {date.today():%d-%b-%Y}. All figures ₹ crore unless "
         "noted. History depth is data-bound: P&L is multi-year; balance sheet & "
         "cash flow are present only for years where the result XBRL carried them "
         "(typically FY2023+)._\n"]
    if overview:                                        # business overview leads the report
        L += [overview, ""]
    if af.empty:
        # No structured statements (e.g. a REIT/InvIT, or a newly listed/renamed entity).
        # Still deliver the business overview above + whatever price/technical context exists.
        L += ["_No structured annual financials are published for this symbol on NSE's result "
              "XBRL feed (typical for REITs / InvITs, or a newly listed / recently renamed "
              "entity), so the statement tables are omitted. The business overview above and "
              "the price/technical snapshot below still apply._", ""]
        ts = technical.snapshot(con, symbol)
        if ts:
            L += ["## Technical snapshot",
                  f"- Close ₹{_f(ts['close'],2)} · SMA20/50/200 {_f(ts['sma20'],0)}/{_f(ts['sma50'],0)}/"
                  f"{_f(ts['sma200'],0)} · RSI {_f(ts['rsi14'],0)} · "
                  f"{_f(ts['pct_from_52w_high'],1,pct=True)} from 52w high",
                  f"- Signals: {', '.join(ts['signals'])}"]
        return "\n".join(L)

    bank = fundamentals.is_bank_frame(af)
    if bank:                                            # 🏦 a bank's own statements & ratios
        L += _bank_sections(con, symbol, af, consolidated)
        cfo = pat = None
    else:                                               # industrial / services statements
        body, cfo, pat = _statement_sections(con, symbol, af, consolidated)
        L += body

    # ===================== FORENSIC DEEP DIVE =====================
    L += ["## 9. Forensic deep-dive", ""]
    p = con.execute(
        "SELECT period_end, promoter_holding_pct, pledged_pct_of_promoter, pledged_pct_of_total "
        "FROM shareholding WHERE symbol = ? ORDER BY period_end DESC LIMIT 1", [symbol]).fetchone()
    if bank:
        L += _bank_forensics(con, symbol, af, consolidated)
    else:
        mcap = valuation.market_cap(con, symbol, consolidated, shares_override=target_shares)
        z = forensic.altman_z(con, symbol, consolidated=consolidated, market_cap=mcap)
        fsc = forensic.piotroski_f(con, symbol, consolidated=consolidated)
        m = forensic.beneish_m(con, symbol, consolidated=consolidated)
        acc = forensic.accruals(con, symbol, consolidated=consolidated)
        zband = ("n/a" if z.value is None else "safe" if z.value > 2.99
                 else "distress" if z.value < 1.81 else "grey zone")
        fband = ("n/a" if fsc.value is None else "strong" if fsc.value >= 8
                 else "weak" if fsc.value <= 2 else "middling")
        mflag = ("n/a" if m.value is None else
                 "⚠ above −1.78 (possible manipulation)" if m.value > -1.78 else "clean (≤ −1.78)")
        # corroborate a Beneish flag against the harder cash/accrual evidence — a sharp
        # margin recovery can trip the statistical screen without any real manipulation.
        cp = (cfo / pat).replace([np.inf, -np.inf], np.nan).dropna()
        cfo_pat_latest = float(cp.iloc[-1]) if len(cp) else None
        beneish_fp = (m.value is not None and m.value > -1.78
                      and acc.value is not None and acc.value <= 10
                      and cfo_pat_latest is not None and cfo_pat_latest >= 1.0)
        mcaveat = (" — but Sloan accruals and cash conversion look clean, so likely a statistical "
                   "false positive from a sharp margin recovery" if beneish_fp else "")
        L.append(f"- **Altman Z = {_f(z.value, 2)} — {zband}.** Bankruptcy-distance score "
                 "(>2.99 safe · 1.81–2.99 grey · <1.81 distress); calibrated for manufacturers, so "
                 "asset-heavy giants can read low." + (f" _{z.note}_" if z.note else ""))
        if fsc.value is not None and fsc.components:
            passed = [k for k, v in fsc.components.items() if v]
            failed = [k for k, v in fsc.components.items() if not v]
            L.append(f"- **Piotroski F = {_f(fsc.value, 0)}/9 — {fband}.** 9-point fundamental-strength "
                     f"checklist. Passed: {', '.join(passed) or 'none'}. Failed: {', '.join(failed) or 'none'}.")
        else:
            L.append(f"- **Piotroski F:** n/a (missing {fsc.missing}).")
        L.append(f"- **Beneish M = {_f(m.value, 2)} — {mflag}{mcaveat}.** Statistical earnings-manipulation "
                 "screen (a flag to dig, not proof — corroborate with accruals/receivables/cash).")
        if acc.value is not None:
            L.append(f"- **Sloan accruals = {_f(acc.value, 1, pct=True)} of avg assets — "
                     f"{glossary.label('Sloan accruals%', acc.value) or 'n/a'}.** Non-cash part of "
                     f"earnings (cash-flow accruals {_f(acc.components.get('cashflow_accruals_%'), 1, pct=True)}); "
                     "near-zero/negative = earnings cash-backed, high positive = aggressive.")
    if p and p[2] is not None:
        L.append(f"- **Promoter pledge (as of {p[0]:%d-%b-%Y}):** promoter holds "
                 f"{_f(p[1], 1, pct=True)}; **{_f(p[2], 1, pct=True)} of that is pledged** "
                 f"({glossary.label('Pledge%', p[2]) or 'n/a'}) — 0% ideal, >50% a serious red flag.")
    elif p:
        L.append(f"- **Promoter pledge (as of {p[0]:%d-%b-%Y}):** promoter holds "
                 f"{_f(p[1], 1, pct=True)} — no significant promoter; {_f(p[3], 1, pct=True)} of "
                 "total shares encumbered.")
    else:
        L.append("- **Promoter pledge:** n/a (no shareholding snapshot).")
    L.append("- **Contingent liabilities & related-party transactions:** these aren't in the "
             "structured XBRL that builds the tables above — they're disclosed only in the **notes "
             "to accounts**. They are **not skipped**: the **Analysis section below reads them "
             "directly from the filing PDFs** and calls out anything material (large or opaque "
             "related-party dealings, sizeable contingent claims). See its forensic assessment for "
             "this company's specifics — a blank there means nothing material was disclosed, not "
             "that it wasn't checked.")
    L.append("")
    L += _insider_block(con, symbol)                    # insider/promoter trades (PIT)
    L += _shp_block(con, symbol)                        # who owns it, classified (SHP)
    L += _ownership_changes_block(con, symbol)          # QoQ diff — who entered / exited / moved
    L += _smart_money_cost_block(con, symbol)           # their cost zone vs price → booking risk
    try:                                                # 🏢 inside view — employee/management sentiment
        from equity_research.analysis import employer_sentiment
        L += employer_sentiment.section_lines(con, symbol)
    except Exception:  # noqa: BLE001 — a best-effort signal must never break the report
        log.debug("employer sentiment section skipped for %s", symbol, exc_info=True)

    # ===================== VALUATION + TECHNICAL (summary) =====================
    snap = valuation.snapshot(con, symbol, consolidated, shares_override=target_shares)
    hist = valuation.valuation_history(con, symbol, consolidated)
    sec = sector.sector_valuation(con, symbol, consolidated, target_shares_override=target_shares)
    industry = sector.industry_of(con, symbol)
    lens = sector.valuation_lens(industry)
    evd = valuation.ev_ebitda(con, symbol, consolidated, shares_override=target_shares)
    L += ["## 10. Valuation"]
    if share_action:
        sa = share_action
        L += ["", f"> ⚠️ **Heads-up — the share count may be stale.** This company had a "
              f"**{sa['kind'].lower()}** (_{sa['subject']}_, ex-date {sa['ex_date']:%d-%b-%Y}) "
              f"**after** the FY-{sa['fy_end'].year} annual filing the share count is taken from. "
              "Until the next annual XBRL is filed, the **market cap, P/E, P/B and the DCF in §11 "
              "are computed on the pre-action share count** and therefore read slightly off. The "
              "numbers below are still shown as-is (unadjusted) — mentally apply the "
              "bonus/split ratio, or resend with an explicit share count to correct them.", ""]
    if snap:
        pe, pb, ey = snap.get("pe_ttm"), snap.get("pb"), snap.get("earnings_yield_%")
        ev_val = evd.get("ev_ebitda")
        is_cyclical = lens == "cyclical" and ev_val is not None and ev_val == ev_val and ev_val > 0
        L.append(f"- Market cap **₹{_f(snap.get('market_cap_cr'),0)} cr**"
                 + (f" · sector: {industry}" if industry else ""))
        if lens == "financial":
            primary = ("**P/B judged against ROE** — the right lens for a lender; its P/E and a "
                       "DCF are unreliable, so they are shown only for context")
        elif is_cyclical:
            primary = ("**EV/EBITDA on mid-cycle margins** — the right lens for an asset-heavy / "
                       "cyclical business, because trailing P/E is misleading at cycle peaks and troughs")
        else:
            primary = "**P/E** — the standard lens for a normal earnings-compounder"
        L += [f"- **Valuation lens for this business:** {primary}.", "",
              "Each multiple on its own line, with what it says here — and whether that reads "
              "cheap or dear:", ""]
        # P/E
        L.append(f"- **P/E (TTM) = {_f(pe,1,lo=0,hi=2000)}** — rupees paid today for ₹1 of the last "
                 "twelve months' profit. Lower looks cheaper, but a low P/E can equally signal a "
                 "cyclical earnings peak or a value trap, so it only means something *against the "
                 "stock's own history and its peers*, never on its own.")
        # P/B
        L.append(f"- **P/B = {_f(pb,2,lo=0,hi=200)}** — rupees paid for ₹1 of net worth (book "
                 "value). This is the primary lens for banks/NBFCs, where it must be read *together "
                 "with ROE* — a higher P/B is only justified by a durably higher ROE.")
        # earnings yield
        L.append(f"- **Earnings yield = {_f(ey,2,pct=True,lo=-50,hi=50)}** — the inverse of P/E: "
                 "annual profit as a percentage of the price you pay. Compare it to the ~7% 10-year "
                 "government-bond yield (your risk-free alternative) — well below 7% means you are "
                 "paying up for future growth; near or above it means the earnings are cheap today.")
        if is_cyclical:
            mid = evd.get("ev_ebitda_midcycle")
            midtxt = (f", or **{_f(mid,1,x=True,lo=0,hi=100)}x on mid-cycle margins**"
                      if mid and mid == mid else "")
            L.append(f"- **EV/EBITDA = {_f(ev_val,1,x=True,lo=0,hi=100)}x**{midtxt} — enterprise "
                     f"value (market cap **+** net debt ₹{_f(evd.get('net_debt_cr'),0)} cr) per ₹1 "
                     "of operating profit. It strips out leverage, so it is the fair lens for "
                     "capital-heavy / cyclical names — judge it on the mid-cycle figure, not a "
                     "single peak or trough year.")
        if lens == "financial":
            r0 = af.loc[af.index[-1]]
            roe = (100 * r0["ProfitLossForPeriod"] / r0["Equity"]
                   if r0.get("ProfitLossForPeriod") is not None and r0.get("Equity") else None)
            L.append(f"- **ROE = {_f(roe,1,pct=True,lo=-100,hi=100)}** — how hard the equity works "
                     "(profit ÷ net worth). For a lender this is the number the P/B has to be earned "
                     "against: a rich P/B on a mediocre ROE is a warning; a fair P/B on a high, "
                     "stable ROE is the sweet spot.")
        if snap.get("note") and not share_action:   # the §10 banner is the stronger, specific version
            L.append(f"- ⚠ _{snap['note']}_")
    # own-history percentile band on the lens's primary multiple (more intuitive than median)
    if not hist.empty:
        col = "pb" if lens == "financial" else "pe"
        cur = snap.get("pb") if col == "pb" else snap.get("pe_ttm")
        ser = hist[col].dropna() if col in hist else pd.Series(dtype=float)
        ser = ser[(ser > 0) & (ser < 100_000)]
        pctile = valuation.multiple_percentile(ser, cur)
        if pctile is not None:
            tag = ("cheap vs its own history" if pctile <= 35 else
                   "rich vs its own history" if pctile >= 65 else "mid-range vs its own history")
            L.append(f"- {'P/B' if col == 'pb' else 'P/E'} {_f(cur,1)} — **{pctile:.0f}th percentile** "
                     f"of its {len(ser)}-yr range ({tag})")
    fwd = _forward_line(guidance, snap, evd, lens)            # forward multiple from guidance
    if fwd:
        L.append(fwd)
    if sec.get("peers_with_data", 0) >= 3:
        L.append(f"- Sector ({sec['industry']}): P/E vs median {_f(sec.get('sector_median_pe'),1)} — "
                 f"cheaper than {_f(sec.get('pe_cheaper_than_%_of_peers'),0)}% of {sec['peers_with_data']} peers")
    elif sec.get("industry"):
        n_peers = sec.get("peers_with_data", 0)
        L.append(f"- Sector ({sec['industry']}): insufficient peer data "
                 f"({n_peers} peer{'s' if n_peers != 1 else ''} with comparable P/E) — "
                 "sector percentile omitted; see the peer table below")

    # peer comparison — grouped into large / mid / small cap, companies by name (target ◄)
    L += _peer_comparison(con, symbol, consolidated)
    L += [
        "",
        "**How to read the valuation numbers.** "
        "The three multiples above each answer a different question, and none of them means much "
        "in isolation — a number only becomes cheap or dear next to the company's own history and "
        "its peers. Beyond the multiples themselves, two supporting reads matter. The "
        "**own-history percentile** places today's multiple within the stock's own past range, "
        "which is more intuitive than a raw median: below the 35th percentile is cheap relative to "
        "itself, above the 65th is rich. The **forward multiple**, when it appears, is the same "
        "ratio computed on the profit management has *explicitly guided* for next year — it shows "
        "up only when there is real guidance to anchor it, so it is a genuine signpost rather than "
        "a projection we invented. Finally, the **valuation lens** named above is chosen "
        "automatically from the company's sector, because the fair yardstick differs by business: "
        "P/B-on-ROE for financials, EV/EBITDA on mid-cycle margins for asset-heavy cyclicals, and "
        "P/E for everything else.",
        "",
    ]

    # =============== VALUATION — WHAT THE PRICE IMPLIES (reverse-DCF first) ===============
    inp = quant.dcf_inputs(con, symbol, consolidated, shares_override=target_shares)
    L += ["## 11. Valuation — what the price implies (reverse-DCF)"]
    if share_action:
        L += ["", f"> ⚠️ _Per §10, a {share_action['kind'].lower()} after the filed share count "
              "means the per-share DCF figures below are on the pre-action count — read the "
              "intrinsic-value-per-share relative to the pre-action price, not today's._", ""]
    if inp.is_financial:
        L.append("- Reverse/forward-DCF is not meaningful for a lender/financial; rely on the "
                 "P/B-on-ROE and the peer comparison in §10." + (f" {inp.note}" if inp.note else ""))
    elif not inp.usable:
        L.append(f"- DCF inputs unavailable: {', '.join(inp.missing) or inp.note or 'n/a'}. "
                 "Rely on the relative valuation in §10.")
    else:
        rev = quant.reverse_dcf(inp)
        # LEAD with the reverse-DCF — the robust 'what's priced in' read.
        if rev.get("implied_growth") is not None:
            hg = rev.get("historical_growth")
            L.append(f"- **Reverse-DCF (the centrepiece):** at today's price the market is pricing in "
                     f"~{_f(100 * rev['implied_growth'], 1, pct=True)} perpetual revenue growth, vs "
                     f"~{_f(100 * (hg or 0), 1, pct=True)} delivered historically — "
                     f"**{'plausible' if rev.get('plausible') else 'demanding'}**. If the company can "
                     "clear that implied bar the stock is cheap; if not, it's rich.")
        elif rev.get("note"):
            L.append(f"- **Reverse-DCF:** {rev['note']}.")
        # Monte-Carlo FCFF-DCF: only a SECONDARY cross-check, and only where it's meaningful.
        sc = quant.scenario_dcf(inp)
        if sc.get("meaningful"):
            mc = quant.monte_carlo_dcf(inp)
            if mc.median and mc.price:
                if mc.price <= mc.median:
                    mos = ("a margin of safety of " + glossary.read(
                        "Margin of safety%", 100 * (mc.median - mc.price) / mc.median, nd=0, pct=True))
                else:
                    mos = f"price {_f(mc.price / mc.median, 1)}x the DCF median (no margin of safety)"
                L.append(f"- _Cross-check_ — Monte-Carlo FCFF-DCF: intrinsic ₹{_f(mc.median, 0)} "
                         f"(median; p10–p90 ₹{_f(mc.p10, 0)}–{_f(mc.p90, 0)}) vs price ₹{_f(mc.price, 0)} "
                         f"→ {mos}; scenario bear/base/bull ₹{_f(sc.get('bear'),0,lo=0,hi=1_000_000)} / "
                         f"{_f(sc.get('base'),0,lo=0,hi=1_000_000)} / {_f(sc.get('bull'),0,lo=0,hi=1_000_000)}.")
            L.append(f"  - DCF drivers: growth {_f(100*inp.growth,1,pct=True)} · EBIT margin "
                     f"{_f(100*inp.ebit_margin,1,pct=True)} · WACC {_f(100*inp.wacc,1,pct=True)} "
                     f"(β {_f(inp.beta,2)}) · terminal g {_f(100*inp.terminal_growth,1,pct=True)} · "
                     f"net debt ₹{_f(inp.net_debt/CR,0)} cr.")
        else:
            L.append("- _Monte-Carlo FCFF-DCF cross-check omitted_ — high-beta / cyclical / capex-heavy "
                     "inputs drive the modelled FCFF negative (a point-estimate DCF isn't reliable here); "
                     "lean on the reverse-DCF above and the sector-appropriate multiples in §10.")
        if inp.note:
            L.append(f"- _{inp.note.strip()}_")
    L += [
        "",
        "**How to read this section.** "
        "A discounted-cash-flow (DCF) values a business as the present value of all the cash it "
        "will hand its owners over time. Rather than *guess* that future cash, the reverse-DCF "
        "flips the question — it solves for the growth rate today's *price* already assumes, then "
        "asks whether that bar is realistic next to what the company has actually delivered. That "
        "is the more robust read, which is why it leads: if the company can clear the implied bar "
        "the stock is cheap, and if it cannot, the stock is rich. The Monte-Carlo cross-check does "
        "the opposite — it runs thousands of scenarios, varying growth, margin and the discount "
        "rate, to produce an intrinsic-value *range* rather than a single false-precision number; "
        "the margin of safety is simply how far the current price sits below that range (your "
        "cushion for being wrong), and a price above the range means you are paying a premium with "
        "no cushion at all. Two inputs drive it: WACC is the blended cost of the company's debt and "
        "equity (the rate future cash is discounted at), and terminal growth is the modest rate "
        "that cash is assumed to compound at forever after the explicit forecast. For lenders and "
        "deeply cyclical or capex-heavy names a point-estimate DCF is unreliable, so there the "
        "reverse-DCF and the §10 multiples carry the weight — and in every case a DCF is "
        "assumption-driven, so read the range, never a single point.",
    ]
    L.append("")

    # ===================== STATISTICAL FORENSICS =====================
    L += ["## 12. Statistical forensics"]
    bf = quant.benford(con, symbol)
    if bf.get("mad") is not None:
        L.append(f"- Benford first-digit conformity: MAD {_f(bf['mad'], 4)} → **{bf['verdict']}** "
                 f"(n={bf['n']})" + (" — ⚠ possible manipulation/rounding" if bf.get("flag") else "") + ".")
    else:
        L.append(f"- Benford: {bf.get('note', 'n/a')}.")
    zs = quant.sector_zscores(con, symbol, consolidated)
    if zs.get("ratios"):
        rows = [[k, _f(v["value"], 2), _f(v["peer_mean"], 2), _f(v["z"], 2)]
                for k, v in zs["ratios"].items()]
        # blank line BEFORE the table so markdown parses it as a table, not inline pipe-text
        L += [f"- Sector-relative z-scores ({zs.get('industry', '?')}, vs {len(rows)} ratios over peers):",
              "", _table(["Ratio", "Value", "Peer mean", "z"], rows), ""]
    else:
        L.append(f"- Sector z-scores: {zs.get('note', 'n/a')}.")
    L += [
        "",
        "**How to read this section.** "
        "Benford's Law observes that in large sets of naturally occurring financial figures the "
        "*leading* digit is not uniform: a 1 starts a number about 30% of the time and a 9 only "
        "about 5%. The MAD (mean absolute deviation) score measures how far the company's reported "
        "numbers stray from that expected shape — a low MAD means they conform and look natural, "
        "while a high MAD means the numbers look 'engineered'. It is only ever a soft flag to dig "
        "into, never proof of anything on its own. The sector z-scores then place each of the "
        "company's ratios in standard deviations from the peer average: within one standard "
        "deviation is in line with peers, while beyond two is a genuine outlier. Read the direction "
        "with the metric — a high z-score is good for ROE, ROCE and margins, reads as expensive for "
        "P/E and P/B, and means more leverage for debt-to-equity.",
    ]
    L.append("")

    ts = technical.snapshot(con, symbol)
    if ts:
        L += ["## 13. Technical snapshot",
              f"- Close ₹{_f(ts['close'],2)} · SMA20/50/200 {_f(ts['sma20'],0)}/{_f(ts['sma50'],0)}/{_f(ts['sma200'],0)} · "
              f"RSI {_f(ts['rsi14'],0)} · {_f(ts['pct_from_52w_high'],1,pct=True)} from 52w high",
              f"- Signals: {', '.join(ts['signals'])}",
              "",
              "**How to read this section.** "
              "The SMA 20, 50 and 200 are the average closing prices over the last 20, 50 and 200 "
              "trading days, smoothing out daily noise to reveal the underlying trend: a price "
              "above the 200-day average signals a long-term uptrend, and the 50-day crossing above "
              "the 200-day is a bullish 'golden cross' regime (the reverse being a bearish 'death "
              "cross'). RSI(14) measures momentum on a 0–100 scale, where above 70 is overbought, "
              "below 30 is oversold, and roughly 40–60 is neutral. The '% from 52-week high' shows "
              "how far the stock has pulled back from its recent peak. Taken together, technicals "
              "describe price behaviour and timing — they complement, never replace, the "
              "fundamentals and valuation above."]

    L += ["", "## 14. Notes"]
    if sector.is_order_driven(industry):     # only relevant to order-book-driven businesses
        L.append("- **Order book / backlog** is read from the filings and, when disclosed, appears "
                 "in the **Business overview** at the top — it is not part of the structured XBRL "
                 "statements.")
    L.append(f"- Statements are {label}; pass the consolidated flag for group-level figures.")
    if bank:
        L.append("- Bank ratios are computed from the results filing: NIM = NII ÷ average total "
                 "assets (a proxy — banks' own NIM divides by interest-earning assets only, so it "
                 "reads a little higher); ROA / ROE on average balances; credit cost = provisions ÷ "
                 "average advances; provision coverage = 1 − net NPA ÷ gross NPA. NPA % and CET1 are "
                 "as reported by the bank (a few filings state them 100× too small; those are "
                 "rescaled).")
    else:
        L.append("- COGS, EBITDA and FCFF/FCFE use documented approximations "
                 "(COGS=materials+purchases+Δinv; EBITDA=PBT+interest+depreciation; "
                 "FCFF adds back after-tax interest; FCFE adds net borrowing).")
    return "\n".join(L)
