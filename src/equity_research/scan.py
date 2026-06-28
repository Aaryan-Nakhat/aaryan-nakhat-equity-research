"""Watchlist scan orchestrator (Phase 5).

Refreshes the latest EOD, runs every per-symbol detector (technical + fundamental
from the DB, announcements via one batched browser session), and returns the
fired alerts plus a market FII/DII note. The Telegram bot pushes the results and
generates a deep report for any 'results filed' alert.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb

from equity_research.analysis import alerts, fundamentals, positioning, valuation
from equity_research.common.db import connect
from equity_research.common.http import ScrapeError, fetch_bytes
from equity_research.ingest import ingest_eod, store_pledge
from equity_research.scrapers import fbil, mcx, nse_api
from equity_research import watchlist


_IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("equity_research.scan")

# Event types whose attached filing PDF is worth an inline Gemini read — the
# details (order value/client, deal terms, rating, etc.) live in the PDF, not the
# one-line NSE subject, so these get a point-wise read.
_ANALYZE_TITLES = {"Results filed", "Concall / investor meet", "Scheme / M&A",
                   "Open offer / SAST", "Rights issue", "QIP / fund raising",
                   "Order / contract win", "Acquisition / disposal",
                   "Credit rating update", "Preferential issue", "Buyback"}


@dataclass
class ScanResult:
    results: dict[str, list[alerts.Alert]] = field(default_factory=dict)
    movers: list[dict] = field(default_factory=list)
    upcoming: list[dict] = field(default_factory=list)
    market: str = ""
    insider: list[str] = field(default_factory=list)        # formatted insider/promoter alert lines
    # per-symbol dedup-state advances, persisted ONLY after the digest is delivered
    # (see commit_scan_state) so a crash before delivery can't silently eat events.
    pending_state: dict[str, dict] = field(default_factory=dict)
    # raw insider rows {symbol: [rows]} stored ONLY after delivery (the table is the
    # dedup ledger: a disclosure alerts once, then storing it marks it seen).
    pending_insider: dict[str, list[dict]] = field(default_factory=dict)


def commit_scan_state(sr: "ScanResult", con: duckdb.DuckDBPyConnection | None = None) -> None:
    """Advance the dedup 'last-seen' markers from a scan. Call this **only after** the
    digest has actually been delivered — that's the whole point: if the scan crashes or
    delivery fails, state is left untouched and the events resurface on the next run."""
    if not sr.pending_state:
        return
    own = con is None
    con = con or connect()
    try:
        for sym, updates in sr.pending_state.items():
            if updates:
                alerts.save_state(con, sym, updates)
        if sr.pending_insider:                              # mark surfaced disclosures seen
            from equity_research.ingest import store_insider_trades
            store_insider_trades(con, sr.pending_insider)
    finally:
        if own:
            con.close()


# Digest header: headline + sectoral Nifty indices (display name = strip "Nifty ",
# except where shortened below). India VIX is appended after. Easy to adjust.
_HEADER_INDICES = ["Nifty 50", "Nifty Bank", "Nifty Financial Services", "Nifty IT",
                   "Nifty Auto", "Nifty Pharma", "Nifty FMCG", "Nifty Metal",
                   "Nifty Energy", "Nifty Realty"]
_INDEX_EMOJI = {"Nifty 50": "🇮🇳", "Nifty Bank": "🏦", "Nifty Financial Services": "💹",
                "Nifty IT": "💻", "Nifty Auto": "🚗", "Nifty Pharma": "💊",
                "Nifty FMCG": "🛒", "Nifty Metal": "⚙️", "Nifty Energy": "⚡",
                "Nifty Realty": "🏠"}


def market_context(con: duckdb.DuckDBPyConnection) -> str:
    """Market header — Nifty 50 + the key sectoral indices + India VIX (latest close
    and day move), so a stock's move reads against the market and its sector."""
    wanted = _HEADER_INDICES + ["India VIX"]
    rows = con.execute(
        "SELECT index_name, close, pct_change FROM index_close WHERE index_name IN ({}) "
        "AND trade_date = (SELECT max(trade_date) FROM index_close)".format(
            ",".join("?" * len(wanted))), wanted).fetchall()
    by_name = {r[0]: (r[1], r[2]) for r in rows if r[1] is not None}

    def val(close, chg, nd=0):
        return f"{close:,.{nd}f}" + (f" ({chg:+.1f}%)" if chg is not None else "")

    lines = []
    idx = [n for n in _HEADER_INDICES if n in by_name]
    if idx:
        lines.append("- 📈 **Indices**")
        lines += [f"    - {_INDEX_EMOJI.get(n, '•')} {n} — {val(*by_name[n])}" for n in idx]
    if "India VIX" in by_name:
        lines.append("- 😨 **India VIX** — " + val(*by_name["India VIX"], nd=1))
    return "\n".join(lines)


def _fii_dii_line(data) -> str:
    """One-line FII/DII net cash flows from the fiidiiTradeReact feed (best-effort)."""
    out: dict[str, float] = {}
    for r in data if isinstance(data, list) else []:
        cat = (r.get("category") or "").upper()
        try:
            net = float(r["netValue"]) if r.get("netValue") is not None else None
        except (TypeError, ValueError, KeyError):
            net = None
        if net is None:
            continue
        if "FII" in cat or "FPI" in cat:
            out["FII"] = net
        elif "DII" in cat:
            out["DII"] = net
    parts = [f"{k} {'+' if v >= 0 else '−'}₹{abs(v):,.0f} cr"
             for k in ("FII", "DII") if (v := out.get(k)) is not None]
    return "- 💸 **FII / DII (cash)** — " + " · ".join(parts) if parts else ""


def _fii_futures_line(d: dict) -> str:
    """FII index-futures positioning bullet (sentiment) + retail contrast. Best-effort."""
    nl = (d or {}).get("net_long_pct")
    if nl is None:
        return ""
    label = ("bullish" if nl >= 55 else "neutral" if nl >= 45
             else "cautious" if nl >= 35 else "bearish")
    prev = d.get("prev_net_long_pct")
    trend = f"; was {prev:.0f}% last wk" if prev is not None else ""
    retail = d.get("retail_net_long_pct")
    rtxt = f" · retail {retail:.0f}% long" if retail is not None else ""
    return f"- 🌍 **FII index futures** — {nl:.0f}% net-long ({label}{trend}){rtxt}"


def _money_lines(usd: float | None, comm: dict) -> str:
    """USD/INR (FBIL) + near-month MCX commodity futures, one point-wise bullet each."""
    lines = []
    if usd is not None:
        lines.append(f"- 💵 **USD/INR** — {usd:,.2f}")
    for sym, label, emoji in (("CRUDEOIL", "Crude oil", "🛢️"),
                              ("GOLD", "Gold", "🥇"), ("SILVER", "Silver", "🥈")):
        d = (comm or {}).get(sym)
        if d and d.get("ltp") is not None:
            pct = d.get("pct")
            v = f"₹{d['ltp']:,.0f}" + (f" ({pct:+.1f}%)" if pct is not None else "")
            lines.append(f"- {emoji} **{label}** — {v}")
    return "\n".join(lines)


def _meta(con, key):
    r = con.execute("SELECT value FROM alert_state WHERE symbol='__meta__' AND key=?", [key]).fetchone()
    return r[0] if r else None


def _set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                "VALUES ('__meta__', ?, ?, now())", [key, value])


def _holidays(con: duckdb.DuckDBPyConnection) -> set[date]:
    """NSE trading holidays, cached in alert_state; refetched if >30 days stale."""
    raw, fetched = _meta(con, "holidays"), _meta(con, "holidays_fetched")
    fresh = False
    if fetched:
        try:
            fresh = (date.today() - date.fromisoformat(fetched)).days <= 30
        except ValueError:
            fresh = False
    if raw and fresh:
        return {date.fromisoformat(x) for x in raw.split(",") if x}
    hs = nse_api.trading_holidays()
    if hs:
        _set_meta(con, "holidays", ",".join(d.isoformat() for d in sorted(hs)))
        _set_meta(con, "holidays_fetched", date.today().isoformat())
        return hs
    return {date.fromisoformat(x) for x in raw.split(",") if x} if raw else set()  # stale fallback


def is_trading_day(con: duckdb.DuckDBPyConnection, d: date) -> bool:
    """Weekday and not an NSE holiday."""
    if d.weekday() >= 5:
        return False
    return d not in _holidays(con)


def market_open_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    """Is today (IST) a trading session? Used to skip weekend/holiday scans."""
    own = con is None
    con = con or connect()
    try:
        return is_trading_day(con, datetime.now(_IST).date())
    finally:
        if own:
            con.close()


def already_scanned_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_scan_date") == datetime.now(_IST).date().isoformat()
    finally:
        if own:
            con.close()


def mark_scanned(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        _set_meta(con, "last_scan_date", datetime.now(_IST).date().isoformat())
    finally:
        if own:
            con.close()


def refresh_eod(con: duckdb.DuckDBPyConnection, lookback: int = 7) -> date | None:
    """Ingest the latest available trading day's full EOD set (idempotent)."""
    today = date.today()
    for i in range(lookback + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            ingest_eod(d, con)
            return d
        except ScrapeError:
            continue
    return None


def fii_dii_note() -> str | None:
    """One-line market note from the latest FII/DII cash activity (event 15)."""
    try:
        rows = nse_api.fii_dii_activity()
    except Exception:  # noqa: BLE001
        return None
    parts = []
    for r in rows if isinstance(rows, list) else []:
        cat = r.get("category", "")
        net = (r.get("netValue") or r.get("buyValue"))
        try:
            net = float(r.get("netValue")) if r.get("netValue") is not None else None
        except (TypeError, ValueError):
            net = None
        if net is not None:
            parts.append(f"{cat} net ₹{net:,.0f} cr")
    return "📊 FII/DII (cash): " + " · ".join(parts) if parts else None


def _fmt_qty(q: float | None) -> str:
    if q is None:
        return "?"
    if q >= 1e7:
        return f"{q / 1e7:.2f} Cr"
    if q >= 1e5:
        return f"{q / 1e5:.1f} L"
    return f"{q:,.0f}"


def _deal_alert(dl: dict) -> alerts.Alert:
    """A bulk/block-deal Alert (green BUY / red SELL) for a watchlist stock."""
    sev = "green" if dl.get("buy_sell") == "BUY" else "red"
    price = f"₹{dl['price']:,.0f}" if dl.get("price") else "?"
    title = f"{dl['deal_type'].title()} deal — {dl.get('buy_sell', '').title()}"
    body = f"{dl.get('client', '?')} {dl.get('buy_sell', '').lower()} {_fmt_qty(dl.get('qty'))} sh @ {price}"
    return alerts.Alert(dl["symbol"], sev, title, body)


def watchlist_deals(syms: list[str], deals: dict) -> dict[str, list[alerts.Alert]]:
    """Bulk/block deals (pre-fetched, market-wide) filtered to ``syms``."""
    symset = set(syms)
    out: dict[str, list[alerts.Alert]] = {}
    for dl in (deals.get("bulk") or []) + (deals.get("block") or []):
        sym = dl.get("symbol")
        if sym in symset and dl.get("client"):
            out.setdefault(sym, []).append(_deal_alert(dl))
    return out


def _parse_dt(s) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except (TypeError, ValueError, AttributeError):
        return None


def _bm_purpose(desc: str, fallback: str) -> str:
    """Heuristic board-meeting purpose: the text after 'consider', matched
    **case-insensitively** (NSE writes 'Consider'/'consider'/'CONSIDER'). Falls
    back when the phrasing differs. Never raises."""
    low = (desc or "").lower()
    i = low.find("consider")
    if i != -1:
        tail = desc[i + len("consider"):].strip().rstrip(".")
        if tail:
            return tail
    return (fallback or "meeting").strip() or "meeting"


def watchlist_upcoming(syms: list[str], feeds: dict, days: int = 30, labeler=None) -> list[dict]:
    """Upcoming events for the watchlist (next ``days``): board meetings (with
    purpose), results / fund-raising / AGM (event calendar), and ex-dividend /
    split / bonus dates (corporate actions). Returns [{symbol, date, what}].

    ``labeler(list[str]) -> list[str]`` (optional) turns the raw board-meeting
    descriptions into clean plain-English purposes via the LLM, in one batched
    call; whenever it returns nothing for an item we fall back to the keyword
    heuristic. Each record is processed best-effort — a single malformed entry is
    skipped, never allowed to abort the whole scan (a missing 'consider' once took
    the entire digest down)."""
    symset = set(syms)
    today = datetime.now(_IST).date()
    horizon = today + timedelta(days=days)
    seen: set = set()
    out: list[dict] = []

    def add(sym, d, what):
        if not sym or sym not in symset or d is None or d < today or d > horizon:
            return
        key = (sym, d, what.lower()[:24])
        if key in seen:
            return
        seen.add(key)
        out.append({"symbol": sym, "date": d, "what": what})

    def _each(rows, fn):
        for i, r in enumerate(rows or []):
            try:
                fn(i, r)
            except Exception:  # noqa: BLE001 — one bad record must not sink the digest
                log.exception("skipping malformed upcoming-event record: %r", r)

    # board meetings — LLM-label the purposes in one batch (best-effort), else heuristic
    bms = feeds.get("board_meetings") or []
    llm: dict[int, str] = {}
    if labeler and bms:
        try:
            labels = labeler([(r.get("bm_desc") or "") for r in bms])
            llm = {i: labels[i] for i in range(min(len(labels), len(bms))) if labels[i]}
        except Exception:  # noqa: BLE001 — labeling is best-effort
            log.exception("LLM event labeling failed — using heuristic purposes")

    def _bm(i, r):
        purpose = llm.get(i) or _bm_purpose(r.get("bm_desc") or "", r.get("bm_purpose") or "")
        add(r.get("bm_symbol"), _parse_dt(r.get("bm_date")), f"Board meeting — {purpose}"[:70])

    _each(bms, _bm)
    _each(feeds.get("event_calendar"), lambda i, r: add(
        r.get("symbol"), _parse_dt(r.get("date")), r.get("purpose") or "Event"))
    _each(feeds.get("corp_actions"), lambda i, r: add(
        r.get("symbol"), _parse_dt(r.get("exDate")),
        f"{(r.get('subject') or 'Corporate action')} (ex-date)"))
    out.sort(key=lambda u: u["date"])
    return out


def _enrich_event_docs(results: dict[str, list[alerts.Alert]], cap: int = 25) -> None:
    """Download + Gemini-analyse the attached filing for EVERY notable doc-bearing
    event (results / concall / scheme / order win / acquisition / rating / etc.),
    point-wise and inline — multiple per stock. Deduped by PDF URL so the same
    document isn't read twice; ``cap`` is a generous safety bound for runaway days."""
    # analyse EVERY fired event that carries a PDF (clarifications, press releases,
    # AGM proceedings, orders … all have detail in the attachment, not the subject);
    # _ANALYZE_TITLES only sets priority so the richest filings win under the cap.
    candidates = [(sym, al) for sym, fired in results.items() for al in fired if al.attachment]
    prio = {t: i for i, t in enumerate(
        ("Results filed", "Concall / investor meet", "Scheme / M&A", "Order / contract win",
         "Acquisition / disposal", "Open offer / SAST", "QIP / fund raising", "Rights issue",
         "Credit rating update", "Preferential issue", "Buyback"))}
    candidates.sort(key=lambda x: prio.get(x[1].title, 99))
    if not candidates:
        return
    from equity_research.reports import synthesize  # lazy: keeps genai off the hot path
    done, seen = 0, set()
    for sym, al in candidates:
        if done >= cap:
            break
        if al.attachment in seen:          # don't re-analyse the same PDF
            continue
        try:
            al.analysis = synthesize.analyze_filing(fetch_bytes(al.attachment), sym, al.title)
            seen.add(al.attachment)
            done += 1
        except Exception:  # noqa: BLE001 — a bad doc shouldn't break the scan
            log.exception("filing analysis failed for %s (%s)", sym, al.title)


def watchlist_movers(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Per-stock daily snapshot: close, day %change, delivery%, 52-week position.

    The always-populated skeleton of the digest (price/volume are the only things
    that change every day). Sorted biggest-move first. Carries the company name.
    """
    names = dict(watchlist.entries(con))
    out: list[dict] = []
    for sym in watchlist.symbols(con):
        row = con.execute(
            "SELECT trade_date, close, prev_close, deliv_per FROM equity_eod "
            "WHERE symbol = ? AND series = 'EQ' ORDER BY trade_date DESC LIMIT 1", [sym]).fetchone()
        if not row or row[1] is None:
            continue
        d, close, prev, deliv = row
        hl = con.execute(
            "SELECT max(high), min(low) FROM equity_eod WHERE symbol = ? AND series = 'EQ' "
            "AND trade_date >= ?", [sym, d - timedelta(days=365)]).fetchone()
        hi, lo = (hl or (None, None))
        chg = (close / prev - 1) * 100 if prev else None
        pos = (close - lo) / (hi - lo) * 100 if hi and lo and hi > lo else None
        # valuation lens: current P/E vs the stock's own positive-P/E history median.
        # Suppress (with a reason) when the P/E is meaningless rather than show a bogus
        # number: a loss-maker, profit > sales (a demerger/exceptional artifact, e.g.
        # TMPV), or negative net worth (accumulated losses > equity, e.g. Vodafone Idea).
        snap = valuation.snapshot(con, sym)
        pe = snap.get("pe_ttm")
        pe = float(pe) if (pe is not None and pe == pe and 0 < pe < 1000) else None
        pb = snap.get("pb")
        t = fundamentals.ttm(con, sym)
        net, rev = t.get("ttm_net_profit_cr"), t.get("ttm_revenue_cr")
        pe_note = None
        if net is not None and net == net and net <= 0:
            pe, pe_note = None, "loss-making"
        elif net and rev and net == net and rev == rev and net > rev:
            pe, pe_note = None, "earnings distorted, profit > sales"
        elif pb is not None and pb == pb and pb < 0:
            pe, pe_note = None, "negative net worth"
        pe_med = None
        if pe is not None:
            h = valuation.valuation_history(con, sym)
            if not h.empty and "pe" in h:
                pos_pe = h["pe"].dropna()
                pos_pe = pos_pe[pos_pe > 0]
                pe_med = float(pos_pe.median()) if len(pos_pe) else None
        out.append({"symbol": sym, "company": names.get(sym) or sym, "close": close,
                    "chg_pct": chg, "deliv": deliv, "pos_52w": pos, "pe": pe,
                    "pe_median": pe_med, "pe_note": pe_note})
    out.sort(key=lambda m: abs(m["chg_pct"]) if m["chg_pct"] is not None else 0, reverse=True)
    return out


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{p:,.2f}" if p < 100 else f"{p:,.0f}"   # decimals for low-priced/penny stocks


def _pos_label(pos: float | None) -> str:
    if pos is None:
        return ""
    if pos >= 90:
        return "near 52w-high"
    if pos <= 10:
        return "near 52w-low"
    return f"{pos:.0f}% of 52w range"


def format_digest(date_str: str, sr: ScanResult) -> str:
    """Build the digest markdown — Upcoming events, a per-stock Movers snapshot,
    and Events (with any inline filing analysis), all by company name (ticker in
    parens). Shared by the email and Telegram channels."""
    results, movers, upcoming = sr.results, sr.movers, sr.upcoming
    names = {m["symbol"]: m["company"] for m in movers}
    parts = [f"# Watchlist — {date_str}"]
    if sr.market:
        parts.append(sr.market)

    if upcoming:
        rows = ["## 📅 Upcoming"]
        for u in upcoming:
            nm = names.get(u["symbol"]) or u["symbol"]
            rows.append(f"- **{nm}** ({u['symbol']}) — {u['date']:%d-%b}: {u['what']}")
        parts.append("\n".join(rows))

    if movers:
        rows = ["## Movers (today)"]
        for m in movers:
            chg = f"{m['chg_pct']:+.1f}%" if m["chg_pct"] is not None else "n/a"
            deliv = f"deliv {m['deliv']:.0f}%" if m["deliv"] is not None else "deliv n/a"
            tail = f" · {_pos_label(m['pos_52w'])}" if _pos_label(m["pos_52w"]) else ""
            val = ""
            if m.get("pe"):
                val = f" · P/E {m['pe']:.0f}"
                if m.get("pe_median"):
                    med = m["pe_median"]
                    rel = "below" if m["pe"] < med * 0.9 else "above" if m["pe"] > med * 1.1 else "~"
                    val += f" ({rel} 5y-med {med:.0f})"
            elif m.get("pe_note"):                      # explain why there's no P/E
                val = f" · P/E n/a ({m['pe_note']})"
            rows.append(f"- **{m['company']}** ({m['symbol']}) — ₹{_fmt_price(m['close'])} · "
                        f"{chg} · {deliv}{tail}{val}")
        parts.append("\n".join(rows))

    if results:
        ev = ["## Events (today)"]
        for sym in sorted(results, key=lambda s: names.get(s, s)):
            lines = [f"**{names.get(sym) or sym}** ({sym})", ""]   # blank line → bullets form a list
            for al in results[sym]:
                emo = alerts.EMOJI.get(al.severity, "🔔")
                lines.append(f"- {emo} {al.title}" + (f" — {al.body}" if al.body else ""))
                if al.analysis:                       # inline point-wise filing read (full, never capped)
                    for ln in al.analysis.splitlines():
                        t = re.sub(r"^\s*[-*•·–]+\s*", "", ln).strip()
                        if t:
                            lines.append(f"    - {t}")  # nested sub-bullets under the event
            ev.append("\n".join(lines))
        parts.append("\n\n".join(ev))
    else:
        parts.append("_No corporate events, institutional deals, or forensic changes today._")

    if sr.insider:
        rows = ["## 🔬 Insider & promoter trades"]
        rows += [f"- {ln}" for ln in sr.insider]
        parts.append("\n".join(rows))

    parts.append("_Reply with a company name to get its full report._")
    return "\n\n".join(parts)


_INSIDER_ALERT_DAYS = 5     # only alert on disclosures filed within N days (cold-start guard)


def _is_material_insider(r: dict) -> bool:
    """Promoter/director trades, or any open-market (not off-market) trade — the signal;
    routine off-market designated-person/relative ESOP transfers are noise."""
    cat = (r.get("category") or "").lower()
    mode = (r.get("mode") or "").lower()
    return ("promoter" in cat or "director" in cat
            or ("market" in mode and "off" not in mode))


def _fmt_insider(sym: str, r: dict) -> str:
    txn = (r.get("txn_type") or "").lower()
    emoji = "🟢" if "buy" in txn else "🔴" if "sell" in txn else "🔹"
    who = (r.get("acq_name") or "Insider").title()
    val, qty = r.get("value_cr"), r.get("qty")
    size = (f"₹{val:,.1f} cr" if val and val >= 0.05 else f"{qty:,.0f} sh" if qty else "—")
    hb, ha = r.get("hold_before_pct"), r.get("hold_after_pct")
    hold = (f"; holding {hb:.2f}%→{ha:.2f}%"
            if hb is not None and ha is not None and (hb or ha) else "")
    filed = (r.get("disclosure_dt") or "").split()[0]
    return (f"{emoji} **{sym}** — {r.get('category') or 'Insider'} {who} "
            f"{(r.get('txn_type') or 'traded').lower()} {size} "
            f"({r.get('mode') or 'n/a'}){hold} · filed {filed}")


def _insider_alerts(con: duckdb.DuckDBPyConnection, insider_by_sym: dict) -> list[str]:
    """New (not yet stored) + material + recent insider/promoter disclosures, formatted."""
    if not insider_by_sym:
        return []
    from datetime import datetime, timedelta
    syms = list(insider_by_sym)
    seen = {(s, d) for s, d in con.execute(
        f"SELECT symbol, did FROM insider_trades WHERE symbol IN ({','.join('?' * len(syms))})",
        syms).fetchall()}
    cutoff = datetime.now() - timedelta(days=_INSIDER_ALERT_DAYS)

    def recent(r):
        try:
            return datetime.strptime((r.get("disclosure_dt") or "").strip(),
                                     "%d-%b-%Y %H:%M") >= cutoff
        except (ValueError, TypeError):
            return False

    lines = []
    for sym in syms:
        for r in insider_by_sym.get(sym) or []:
            did = r.get("did")
            if did and (sym, did) not in seen and _is_material_insider(r) and recent(r):
                lines.append(_fmt_insider(sym, r))
    return lines


def run_watchlist_scan(con: duckdb.DuckDBPyConnection | None = None) -> ScanResult:
    """Scan the watchlist → ScanResult(results, movers, upcoming). Ingests EOD first."""
    own = con is None
    con = con or connect()
    try:
        refresh_eod(con)
        syms = watchlist.symbols(con)
        # one batched browser session for all symbols' announcements
        try:
            anns_by_sym = nse_api.corporate_announcements_batch(syms) if syms else {}
        except Exception:  # noqa: BLE001
            anns_by_sym = {}
        # one more batched session for promoter-pledge snapshots (persist + alert)
        try:
            pledge_by_sym = nse_api.promoter_pledge_batch(syms) if syms else {}
            store_pledge(con, pledge_by_sym)
        except Exception:  # noqa: BLE001
            pledge_by_sym = {}
        # one session for all market-wide feeds: deals + upcoming events
        try:
            feeds = nse_api.market_feeds() if syms else {}
        except Exception:  # noqa: BLE001
            feeds = {}
        # one session for insider/promoter (SEBI PIT) disclosures across the watchlist
        try:
            insider_by_sym = nse_api.insider_trades_batch(syms) if syms else {}
        except Exception:  # noqa: BLE001
            insider_by_sym = {}
        results: dict[str, list[alerts.Alert]] = {}
        pending: dict[str, dict] = {}
        for sym in syms:
            try:
                # commit=False: hold the dedup-state advance until the digest is
                # delivered (commit_scan_state), so a crash can't eat today's events.
                fired, updates = alerts.scan_symbol(con, sym, anns_by_sym.get(sym, []),
                                                    pledge_by_sym.get(sym), commit=False)
            except Exception:  # noqa: BLE001 — one bad symbol shouldn't kill the scan
                fired, updates = [], {}
            if updates:
                pending[sym] = updates
            if fired:
                results[sym] = fired
        # per-stock bulk/block deals (institutional buy/sell) — merge in
        for sym, deal_alerts in watchlist_deals(syms, feeds.get("deals") or {}).items():
            results.setdefault(sym, []).extend(deal_alerts)
        _enrich_event_docs(results)                         # inline Gemini read of filings

        # build each digest section best-effort — one failing section must never
        # abort the whole scan (it's the difference between a partial digest and none).
        def _safe(fn, default):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                log.exception("digest section failed: %s", getattr(fn, "__name__", fn))
                return default

        def _labeler(descs):
            from equity_research.reports import synthesize  # lazy: keeps genai off the hot path
            return synthesize.label_events(descs)

        # market block: indices+VIX (DB) · FII/DII (feeds) · USD/INR (FBIL) + commodities
        # (MCX) — each best-effort, a failing source is simply left out.
        usd = _safe(lambda: fbil.usd_inr(), None)
        comm = _safe(lambda: mcx.commodities(), {})
        market = "\n".join(x for x in (
            _safe(lambda: market_context(con), ""),
            _safe(lambda: _fii_dii_line(feeds.get("fiidii") or []), ""),
            _safe(lambda: _fii_futures_line(positioning.fii_index_futures(con)), ""),
            _safe(lambda: _money_lines(usd, comm), ""),
        ) if x)
        return ScanResult(
            results,
            _safe(lambda: watchlist_movers(con), []),
            _safe(lambda: watchlist_upcoming(syms, feeds, labeler=_labeler), []),
            market,
            insider=_safe(lambda: _insider_alerts(con, insider_by_sym), []),
            pending_state=pending,
            pending_insider=insider_by_sym,
        )
    finally:
        if own:
            con.close()
