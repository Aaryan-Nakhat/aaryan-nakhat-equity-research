"""Pre-market digest — a before-the-open read of what the overnight tape implies for Nifty.

Fires ~8:30 AM IST (see ``email_bot.maybe_premarket``). Assembles four best-effort
inputs — GIFT Nifty (the Nifty future that trades overnight on NSE IX), the overnight
US/Asia indices, FII index-futures positioning + India VIX, and the latest market
headlines — computes the **implied gap** (GIFT Nifty vs Nifty-50's previous close), and
asks the LLM for a tight "overnight drivers + how Nifty may open + what to watch" read.

Every input degrades independently: if GIFT Nifty is unreachable the note still ships the
global tape and news; if the LLM is down the numbers still go out. Returns ``None`` only
when we have essentially nothing worth sending.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as _time
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import positioning
from equity_research.reports import md, synthesize
from equity_research.scrapers import markets_global, nseix

_IST = ZoneInfo("Asia/Kolkata")


# ----------------- implied-gap read -----------------
def _gap_read(gift: dict | None, ref: dict | None) -> dict | None:
    """GIFT Nifty vs Nifty-50 previous close → {pts, pct, bias, emoji}."""
    if not gift or not ref:
        return None
    last, prev = gift.get("last"), ref.get("nifty_prev_close")
    if last is None or not prev:
        return None
    pts = last - prev
    pct = pts / prev * 100
    if pct >= 0.5:
        bias, emoji = "strong gap-up", "🟢"
    elif pct >= 0.15:
        bias, emoji = "mild gap-up", "🟢"
    elif pct > -0.15:
        bias, emoji = "flat / muted open", "⚪"
    elif pct > -0.5:
        bias, emoji = "mild gap-down", "🔴"
    else:
        bias, emoji = "strong gap-down", "🔴"
    return {"pts": pts, "pct": pct, "bias": bias, "emoji": emoji}


def _vix_read(vix: float | None) -> str:
    if vix is None:
        return ""
    if vix < 13:
        return "low — calm / complacent"
    if vix < 16:
        return "normal"
    if vix < 20:
        return "elevated — some nervousness"
    return "high — fear / stress"


# ----------------- number formatting -----------------
def _f(v, dec: int = 0) -> str:
    return "n/a" if v is None else f"{v:,.{dec}f}"


def _signed(v, dec: int = 0) -> str:
    return "n/a" if v is None else f"{v:+,.{dec}f}"


# ----------------- LLM context -----------------
def _llm_context(gift, ref, gap, glob, fii, news) -> str:
    lines = ["=== OVERNIGHT GLOBAL INDICES ==="]
    if glob:
        for g in glob:
            lines.append(f"{g['name']} ({g['region']}): {_f(g['last'], 2)} "
                         f"({_signed(g['pct'], 2)}%)")
    else:
        lines.append("(unavailable)")

    lines.append("\n=== GIFT NIFTY — implied Indian open ===")
    if gift:
        lines.append(f"GIFT Nifty (near-month, expiry {gift.get('expiry')}): {_f(gift.get('last'), 1)}, "
                     f"own change {_signed(gift.get('pct_change'), 2)}% vs its prior close.")
    if gap and ref:
        lines.append(f"Nifty-50 previous close: {_f(ref.get('nifty_prev_close'), 1)}. "
                     f"Implied gap = {_signed(gap['pts'], 0)} pts ({_signed(gap['pct'], 2)}%) "
                     f"→ {gap['bias']}.")
    if ref and ref.get("vix") is not None:
        lines.append(f"India VIX: {_f(ref.get('vix'), 2)} ({_signed(ref.get('vix_pct'), 1)}%) "
                     f"— {_vix_read(ref.get('vix'))}.")
    if fii:
        lines.append(f"FII index-futures net-long: {_f(fii.get('net_long_pct'), 0)}% "
                     f"(prev ~{_f(fii.get('prev_net_long_pct'), 0)}%); "
                     f"retail net-long {_f(fii.get('retail_net_long_pct'), 0)}%.")

    lines.append("\n=== LATEST INDIAN-MARKET HEADLINES ===")
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. {n['title']}")
    else:
        lines.append("(no headlines fetched)")
    return "\n".join(lines)


# ----------------- legend -----------------
_LEGEND = (
    "\n\n---\n"
    "**📖 How to read this (plain English)**\n\n"
    "- **GIFT Nifty** — the Nifty-50 index *future* that trades on NSE's GIFT City exchange for ~21 "
    "hours a day, including all night. Because it keeps trading after Indian markets close and reacts "
    "to overnight US/Asia moves, its level early in the morning is the market's best guess of **where "
    "Nifty will open**. It's the single most-watched pre-open cue.\n"
    "- **Implied gap** — GIFT Nifty's current level minus *yesterday's* Nifty-50 closing level. Positive "
    "= pointing to a higher open (**gap-up**); negative = lower open (**gap-down**); near zero = a flat "
    "open. GIFT Nifty is a future, so it usually sits a few points *above* spot (cost of carry) — the "
    "**direction and size** matter more than the exact number.\n"
    "- **India VIX** — the 'fear gauge': expected Nifty volatility over the next month. Low (**<13**) = "
    "calm; **13–16** = normal; **16–20** = nervous; **>20** = fearful. A jump in VIX often means a "
    "choppier, gap-prone session.\n"
    "- **FII index-futures net-long %** — of the index-future bets big foreign investors hold, the share "
    "that is *long* (betting up). High = FIIs positioned bullish; low = defensive / short. A big gap "
    "vs **retail** positioning often marks stretched sentiment.\n"
    "- **Overnight global** — US indices (S&P 500 / Nasdaq / Dow) closed a few hours ago; Asia (Nikkei / "
    "Hang Seng) is trading into our morning. GIFT Nifty is largely reacting to these.\n"
    "- _This is a **read of the setup**, not a trade call — a gap-up open can still fade, and a gap-down "
    "can be bought. It tells you the mood you're walking into, not where the day ends._"
)


# ----------------- build -----------------
def build_premarket(con: duckdb.DuckDBPyConnection) -> str | None:
    """Assemble the pre-market digest markdown. ``None`` if we have essentially no data."""
    gift = nseix.gift_nifty()
    ref = markets_global.nifty_reference()
    glob = markets_global.overnight_indices()
    try:
        fii = positioning.fii_index_futures(con)
    except Exception:  # noqa: BLE001
        fii = {}
    news = markets_global.market_headlines(15)

    if not gift and not glob and not news:
        return None  # nothing worth sending

    gap = _gap_read(gift, ref)
    context = _llm_context(gift, ref, gap, glob, fii, news)
    brief = synthesize.premarket_brief(context)

    now = datetime.now(_IST)
    today = now.date()
    pre_open = now.time() < _time(9, 15)   # laptop-asleep catch-up may fire this after the open
    stamp = "" if pre_open else f"  ·  _as of {now:%H:%M} — market already open_"
    if pre_open:
        subtitle = ("_Before-the-open read: what the overnight tape and GIFT Nifty imply for how Nifty "
                    "may start the day. Not a trade call — see the legend at the bottom._")
    else:
        subtitle = ("_Morning snapshot (sent when this machine woke). Nifty is already trading, so the "
                    "'gap' below is **vs yesterday's close**, and the overnight-tape read still frames "
                    "the day. Not a trade call — see the legend at the bottom._")
    parts = [f"# 🌅 Pre-market — {today:%a %d-%b-%Y}{stamp}", subtitle]

    # headline implied-open line
    if gap and gift:
        label = "Implied open" if pre_open else "Gap so far"
        parts.append(
            f"## {gap['emoji']} {label}: **{gap['bias']}**  ·  "
            f"GIFT Nifty {_f(gift.get('last'), 0)}  ·  gap {_signed(gap['pts'], 0)} pts "
            f"({_signed(gap['pct'], 2)}%)")
    elif gift:
        parts.append(f"## GIFT Nifty {_f(gift.get('last'), 0)} "
                     f"({_signed(gift.get('pct_change'), 2)}% vs its prior close)")

    # LLM narrative
    if brief:
        parts.append("### 🧭 Overnight read\n\n" + brief)

    # GIFT Nifty + domestic gauges table
    if gift or ref:
        rows = []
        if gift:
            rows.append(["GIFT Nifty (near-month)", _f(gift.get("last"), 1),
                         _signed(gift.get("pct_change"), 2) + "%",
                         f"expiry {gift.get('expiry') or 'n/a'}"])
        if ref:
            rows.append(["Nifty-50 prev close", _f(ref.get("nifty_prev_close"), 1), "—",
                         "yesterday's close (gap baseline)"])
            if ref.get("vix") is not None:
                rows.append(["India VIX", _f(ref.get("vix"), 2),
                             _signed(ref.get("vix_pct"), 1) + "%", _vix_read(ref.get("vix"))])
        if fii and fii.get("net_long_pct") is not None:
            rows.append(["FII index-fut net-long", _f(fii.get("net_long_pct"), 0) + "%",
                         "prev " + _f(fii.get("prev_net_long_pct"), 0) + "%",
                         "retail " + _f(fii.get("retail_net_long_pct"), 0) + "% long"])
        parts += ["## 📊 GIFT Nifty & domestic gauges",
                  md.table(["Gauge", "Level", "Change", "Note"], rows, "lrrl")]

    # overnight global table
    if glob:
        rows = [[g["name"], g["region"], _f(g["last"], 2), _signed(g["pct"], 2) + "%"]
                for g in glob]
        parts += ["## 🌍 Overnight global markets",
                  md.table(["Index", "Region", "Last", "Change"], rows, "llrr")]

    # headlines
    if news:
        top = news[:8]
        parts += ["## 📰 Overnight & morning headlines",
                  "\n".join(f"- {n['title']}" for n in top)]

    return "\n\n".join(parts) + _LEGEND
