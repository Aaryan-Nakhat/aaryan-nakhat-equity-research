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

from equity_research.analysis import alerts, fundamentals, positioning, technical, valuation
from equity_research.common.db import connect
from equity_research.common.http import ScrapeError, fetch_bytes
from equity_research.ingest import (
    ingest_eod,
    ingest_mf_holdings_all,
    ingest_mf_navall,
    store_pledge,
)
from equity_research.scrapers import fbil, mcx, nse_api, nse_shp
from equity_research import watchlist


_IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("equity_research.scan")

# Event types whose attached filing PDF is worth an inline LLM read — the
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
    level_alerts: list[dict] = field(default_factory=list)  # technical level events (tracking/holdings)
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


@dataclass
class IntradayResult:
    """Midday snapshot — the full digest's sections with LIVE data (live indices + movers +
    commodities; FII/DII & positioning are prior-session until published after close)."""
    movers: list[dict] = field(default_factory=list)
    filings: list[dict] = field(default_factory=list)
    insider: list[str] = field(default_factory=list)
    market: str = ""
    upcoming: list[dict] = field(default_factory=list)
    level_alerts: list[dict] = field(default_factory=list)
    asof: "datetime | None" = None


# Digest header: headline + sectoral Nifty indices (display name = strip "Nifty ",
# except where shortened below). India VIX is appended after. Easy to adjust.
_HEADER_INDICES = ["Nifty 50", "Nifty Bank", "Nifty Financial Services", "Nifty IT",
                   "Nifty Auto", "Nifty Pharma", "Nifty FMCG", "Nifty Metal",
                   "Nifty Energy", "Nifty Realty"]
_INDEX_EMOJI = {"Nifty 50": "🇮🇳", "Nifty Bank": "🏦", "Nifty Financial Services": "💹",
                "Nifty IT": "💻", "Nifty Auto": "🚗", "Nifty Pharma": "💊",
                "Nifty FMCG": "🛒", "Nifty Metal": "⚙️", "Nifty Energy": "⚡",
                "Nifty Realty": "🏠"}


def _index_header_bullets(by_name: dict) -> str:
    """Point-wise indices + India VIX header from {display-name: (value, pct)}."""
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


def market_context(con: duckdb.DuckDBPyConnection) -> str:
    """Market header — Nifty 50 + the key sectoral indices + India VIX from the latest
    EOD close (for the 18:00 digest)."""
    wanted = _HEADER_INDICES + ["India VIX"]
    rows = con.execute(
        "SELECT index_name, close, pct_change FROM index_close WHERE index_name IN ({}) "
        "AND trade_date = (SELECT max(trade_date) FROM index_close)".format(
            ",".join("?" * len(wanted))), wanted).fetchall()
    return _index_header_bullets({r[0]: (r[1], r[2]) for r in rows if r[1] is not None})


def live_market_context() -> str:
    """Market header from **live** index values (NSE /api/allIndices) for the midday digest."""
    quotes = nse_api.live_indices()
    by_name = {n: quotes[n.upper()] for n in (_HEADER_INDICES + ["India VIX"])
               if n.upper() in quotes}
    return _index_header_bullets(by_name)


def _fii_dii_line(data) -> str:
    """One-line FII/DII net cash flows from the fiidiiTradeReact feed (best-effort).
    Tagged with the feed's trade date (it's published after close, so prior-session intraday)."""
    out: dict[str, float] = {}
    asof = None
    for r in data if isinstance(data, list) else []:
        cat = (r.get("category") or "").upper()
        asof = asof or r.get("date")
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
    if not parts:
        return ""
    d = _parse_dt(asof)
    tag = f" (as of {d:%d-%b})" if d else (f" (as of {asof})" if asof else "")
    return "- 💸 **FII / DII (cash)** — " + " · ".join(parts) + tag


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
    dt = d.get("date")                          # participant-OI is EOD (prior-session intraday)
    asof = f" (as of {dt:%d-%b})" if dt else ""
    return f"- 🌍 **FII index futures** — {nl:.0f}% net-long ({label}{trend}){rtxt}{asof}"


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


def already_intraday_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_intraday_date") == datetime.now(_IST).date().isoformat()
    finally:
        if own:
            con.close()


def mark_intraday(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        _set_meta(con, "last_intraday_date", datetime.now(_IST).date().isoformat())
    finally:
        if own:
            con.close()


def already_premarket_today(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_premarket_date") == datetime.now(_IST).date().isoformat()
    finally:
        if own:
            con.close()


def mark_premarket(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        _set_meta(con, "last_premarket_date", datetime.now(_IST).date().isoformat())
    finally:
        if own:
            con.close()


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def sector_rotation_due(con: duckdb.DuckDBPyConnection | None = None) -> bool:
    """True once per ISO week — the weekly sector-rotation push hasn't fired yet this week."""
    own = con is None
    con = con or connect()
    try:
        return _meta(con, "last_sector_rotation_week") != _iso_week(datetime.now(_IST))
    finally:
        if own:
            con.close()


def mark_sector_rotation(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        _set_meta(con, "last_sector_rotation_week", _iso_week(datetime.now(_IST)))
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


_FOREIGN_RE = re.compile(
    r"\b(fpi|fii|foreign|offshore|mauritius|cayman|luxembourg|singapore|cyprus|vcc|"
    r"ireland|netherlands|global|overseas)\b", re.I)


def _listed_name_map(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """``norm_name(company) → NSE symbol`` for the listed master — lets a deal counterparty
    that is itself a listed company be flagged (the Elcid pattern). {} if the master is empty."""
    try:
        return {nse_shp.norm_name(n): s for s, n in con.execute(
            "SELECT symbol, company_name FROM equity_master "
            "WHERE company_name IS NOT NULL").fetchall()}
    except Exception:  # noqa: BLE001
        return {}


def _classify_client(name: str, listed: dict[str, str] | None) -> str | None:
    """Best-effort **what-is-this-counterparty** from the deal client name alone: a listed
    company (with its symbol), mutual fund, insurer, FPI/foreign, other fund, LLP, trust,
    HUF, unlisted pvt / unlisted company, or an individual. Name-only heuristic (deals carry
    no structured category), so it's a hint, not a guarantee."""
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    sym = (listed or {}).get(nse_shp.norm_name(n))
    if sym:
        return f"listed co · {sym}"
    if re.search(r"mutual fund|asset management|\bamc\b|\bmf\b", low):
        return "mutual fund"
    if re.search(r"\blic\b|insurance|assurance", low):
        return "insurer"
    if _FOREIGN_RE.search(low):
        return "FPI / foreign"
    if re.search(r"\bfund\b|investment trust|\breit\b|\binvit\b", low):
        return "fund / investment vehicle"
    if "llp" in low:
        return "LLP"
    if re.search(r"\btrust\b", low):
        return "trust"
    if re.search(r"\bhuf\b", low):
        return "HUF"
    if re.search(r"private limited|\bpvt\b", low):
        return "unlisted pvt company"
    if re.search(r"\b(limited|ltd|plc|corp|corporation|industries|holdings?|ventures?|"
                 r"enterprises?|capital|securities|broking|trading|infra)\b", low):
        return "unlisted company"
    return "individual"


def _deal_alert(dl: dict, listed: dict[str, str] | None = None) -> alerts.Alert:
    """A bulk/block-deal Alert (green BUY / red SELL) for a watchlist stock. ``deal_type`` may
    be 'bulk & block' when the same trade was reported in both feeds. The counterparty is
    classified (listed co / MF / FPI / individual …) in brackets after its name."""
    sev = "green" if dl.get("buy_sell") == "BUY" else "red"
    price = f"₹{dl['price']:,.0f}" if dl.get("price") else "?"
    title = f"{dl.get('deal_type', '').title()} deal — {dl.get('buy_sell', '').title()}"
    client = dl.get("client", "?")
    cls = _classify_client(client, listed)
    who = f"{client} ({cls})" if cls else client
    body = f"{who} {dl.get('buy_sell', '').lower()} {_fmt_qty(dl.get('qty'))} sh @ {price}"
    return alerts.Alert(dl["symbol"], sev, title, body)


def _facilitation_alert(dl: dict, listed: dict[str, str] | None = None) -> alerts.Alert:
    """A collapsed **offsetting** buy+sell (same counterparty, same qty, same price) — a crossed
    / facilitation block that leaves **no net position change**. Shown as one neutral line so it
    doesn't read as a contradictory buy-and-sell pair."""
    price = f"₹{dl['price']:,.0f}" if dl.get("price") else "?"
    title = f"{dl.get('deal_type', '').title()} deal — Facilitation (net-zero)"
    client = dl.get("client", "?")
    cls = _classify_client(client, listed)
    who = f"{client} ({cls})" if cls else client
    body = (f"{who} **bought & sold** {_fmt_qty(dl.get('qty'))} sh @ {price} — "
            f"crossed block, no net position change")
    return alerts.Alert(dl["symbol"], "neutral", title, body)


def watchlist_deals(syms: list[str], deals: dict,
                    listed: dict[str, str] | None = None) -> dict[str, list[alerts.Alert]]:
    """Bulk/block deals (pre-fetched, market-wide) filtered to ``syms``, **deduped across the
    bulk and block feeds** — NSE reports one large trade in *both*, which otherwise showed the
    same buy/sell four times. A trade seen in both is labelled 'bulk & block'. A counterparty
    that appears on **both sides at the same qty and price** is a crossed / facilitation block
    (no net position change) and is collapsed into one neutral line; genuinely one-sided buys
    and sells stay separate. ``listed`` classifies each counterparty."""
    symset = set(syms)
    merged: dict[tuple, dict] = {}
    for src in ("bulk", "block"):
        for dl in deals.get(src) or []:
            sym = dl.get("symbol")
            if sym not in symset or not dl.get("client"):
                continue
            key = (sym, dl.get("buy_sell"), (dl.get("client") or "").strip().lower(),
                   dl.get("qty"), dl.get("price"))
            if key in merged:
                merged[key]["_sources"].add(src)
            else:
                d = dict(dl)
                d["_sources"] = {src}
                merged[key] = d
    # group the deduped legs by (symbol, client, qty, price): if the same counterparty is on
    # BOTH buy and sell of an identical qty/price, it's an offsetting cross → one neutral line.
    by_trade: dict[tuple, dict] = {}
    for (sym, buy_sell, client, qty, price), d in merged.items():
        by_trade.setdefault((sym, client, qty, price), {})[buy_sell] = d
    out: dict[str, list[alerts.Alert]] = {}
    for (sym, _client, _qty, _price), sides in by_trade.items():
        buy, sell = sides.get("BUY"), sides.get("SELL")
        if buy and sell:                                    # offsetting cross → collapse
            srcs = buy["_sources"] | sell["_sources"]
            d = {**buy, "_sources": srcs,
                 "deal_type": "bulk & block" if len(srcs) == 2 else next(iter(srcs))}
            out.setdefault(sym, []).append(_facilitation_alert(d, listed))
            continue
        for d in sides.values():
            d["deal_type"] = "bulk & block" if len(d["_sources"]) == 2 else next(iter(d["_sources"]))
            out.setdefault(sym, []).append(_deal_alert(d, listed))
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

    bm_dates: set = set()

    def _bm(i, r):
        sym, d = r.get("bm_symbol"), _parse_dt(r.get("bm_date"))
        purpose = llm.get(i) or _bm_purpose(r.get("bm_desc") or "", r.get("bm_purpose") or "")
        add(sym, d, f"Board meeting — {purpose}")          # no length cap — full purpose
        if sym and d:
            bm_dates.add((sym, d))

    _each(bms, _bm)

    def _cal(i, r):           # skip the calendar entry a board meeting already covers (same sym+date)
        sym, d = r.get("symbol"), _parse_dt(r.get("date"))
        if (sym, d) not in bm_dates:
            add(sym, d, r.get("purpose") or "Event")

    _each(feeds.get("event_calendar"), _cal)
    _each(feeds.get("corp_actions"), lambda i, r: add(
        r.get("symbol"), _parse_dt(r.get("exDate")),
        f"{(r.get('subject') or 'Corporate action')} (ex-date)"))
    out.sort(key=lambda u: u["date"])
    return out


def _enrich_event_docs(results: dict[str, list[alerts.Alert]], cap: int = 25) -> None:
    """Download + LLM-analyse the attached filing for EVERY notable doc-bearing
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


def _annotate_types(items: list[dict], tmap: dict[str, str]) -> list[dict]:
    """Tag each mover/upcoming dict with its watchlist bucket ('holding'|'tracking')."""
    for it in items or []:
        it["list_type"] = tmap.get(it.get("symbol"), "holding")
    return items or []


def _grouped_by_type(items: list[dict]) -> list[tuple[str | None, list[dict]]]:
    """Split into (label, sublist) groups — *Your Holdings* then *Your Tracking List* —
    preserving each item's order. If nothing is tracked, one unlabelled group (a flat list),
    so a pure-holdings watchlist reads exactly as before."""
    hold = [x for x in items if x.get("list_type", "holding") != "tracking"]
    track = [x for x in items if x.get("list_type", "holding") == "tracking"]
    if not track:
        return [(None, items)]
    groups: list[tuple[str | None, list[dict]]] = []
    if hold:
        groups.append(("📁 Your Holdings", hold))
    groups.append(("👀 Your Tracking List", track))
    return groups


def _levels_map(con: duckdb.DuckDBPyConnection, syms: list[str]) -> dict[str, dict]:
    """Compute ``technical.levels`` once per watchlist symbol so the movers annotation and
    the level-alerts share it (no double compute). Best-effort per symbol."""
    out: dict[str, dict] = {}
    for s in syms or []:
        try:
            out[s] = technical.levels(con, s)
        except Exception:  # noqa: BLE001
            log.exception("levels failed for %s", s)
    return out


def _annotate_mover_levels(movers: list[dict], lmap: dict[str, dict]) -> list[dict]:
    """Tag each mover with its nearest support (``sup``) and resistance (``res``) mid-price so
    every watchlist line can show where it sits vs its levels — not only when an alert fires."""
    for m in movers or []:
        lv = (lmap or {}).get(m.get("symbol"))
        if lv and lv.get("history_ok"):
            m["sup"] = lv["supports"][0]["mid"] if lv.get("supports") else None
            m["res"] = lv["resistances"][0]["mid"] if lv.get("resistances") else None
    return movers or []


def _levels_bracket(m: dict) -> str:
    """`· (S ₹1,250 / R ₹1,333)` for a mover line — nearest support / resistance. '' if neither.
    Uses ``_fmt_price`` so low-priced names keep decimals (₹0.24, not a degenerate ₹0)."""
    if m.get("sup") is None and m.get("res") is None:
        return ""
    s = f"S ₹{_fmt_price(m['sup'])}" if m.get("sup") is not None else "S —"
    r = f"R ₹{_fmt_price(m['res'])}" if m.get("res") is not None else "R —"
    return f" · ({s} / {r})"


def _level_alerts(con: duckdb.DuckDBPyConnection, syms: list[str], names: dict, tmap: dict,
                  *, live_prices: dict | None = None, lmap: dict | None = None) -> list[dict]:
    """Technical **level events** for the watchlist — transition-based so each fires once, not
    every day a condition holds. Compares a previous reference price with the current one:
    for the 6 PM digest, yesterday's close → today's close; for the midday digest, the prior
    close → the live price. Bucketed by list_type:

    - **Tracking** (watching for an entry): 🚀 a fresh break above the nearest resistance, or
      🎯 a fresh pullback into the strongest support zone.
    - **Holdings** (watching the exit): ⚠️ a fresh loss of the 50/200-DMA, or 🔻 a break below
      the strongest support (invalidation).

    Returns dicts carrying ``symbol``/``company``/``list_type``/``text`` (list_type lets the
    digest group them into Holdings vs Tracking). Best-effort per symbol."""
    out: list[dict] = []
    for sym in syms or []:
        try:
            lv = (lmap or {}).get(sym) or technical.levels(con, sym)
            if not lv.get("history_ok"):
                continue
            ind = technical.indicators(con, sym)
            if ind.empty or len(ind) < 2:
                continue
            last, prevrow = ind.iloc[-1], ind.iloc[-2]
            atr = lv.get("atr") or 0.0
            if live_prices is not None:
                cur = live_prices.get(sym)
                prev = float(last["close"])
                if cur is None:
                    continue
                cur = float(cur)
            else:
                cur, prev = float(last["close"]), float(prevrow["close"])
            ltype = tmap.get(sym, "holding")
            sups, ress = lv.get("supports", []), lv.get("resistances", [])
            txt = None
            if ltype == "tracking":
                if ress:                                      # fresh breakout over nearest resistance
                    r = ress[0]
                    if prev <= r["hi"] and cur > r["hi"]:
                        txt = (f"🚀 broke resistance ₹{r['mid']:,.0f} — potential breakout "
                               f"(reply `levels: {sym}` for the setup)")
                if txt is None and sups:                      # fresh pullback into strong support
                    s = max(sups, key=lambda z: z["score"])
                    if prev > s["hi"] and s["lo"] - 0.5 * atr <= cur <= s["hi"] + 0.5 * atr:
                        txt = (f"🎯 pulled into support ₹{s['lo']:,.0f}–₹{s['hi']:,.0f} — watch "
                               f"for an entry (reply `levels: {sym}`)")
            else:                                             # holding — stop / trim watch
                for dma, label in (("sma50", "50-DMA"), ("sma200", "200-DMA")):
                    v = last.get(dma)
                    vp = prevrow.get(dma) if live_prices is None else v
                    if v == v and vp == vp and v and prev >= vp and cur < v:
                        txt = f"⚠️ lost the {label} (₹{v:,.0f}) — stop / trim watch"
                        break
                if txt is None and sups:
                    s = max(sups, key=lambda z: z["score"])
                    if prev >= s["lo"] and cur < s["lo"]:
                        txt = f"🔻 broke support ₹{s['lo']:,.0f} — invalidation, review the position"
            if txt:
                out.append({"symbol": sym, "company": names.get(sym, sym),
                            "list_type": ltype, "text": txt})
        except Exception:  # noqa: BLE001 — a level read must never break the scan
            log.exception("level alert failed for %s", sym)
    return out


def _level_alert_section(alerts_list: list[dict], names: dict) -> str | None:
    """Render the '🎯 Level alerts' section grouped into Holdings / Tracking, or None if empty."""
    if not alerts_list:
        return None
    rows = ["## 🎯 Level alerts"]
    for label, grp in _grouped_by_type(alerts_list):
        if label:
            rows += ["", f"### {label}", ""]
        for a in grp:
            nm = names.get(a["symbol"]) or a.get("company") or a["symbol"]
            rows.append(f"- **{nm}** ({a['symbol']}) — {a['text']}")
    return "\n".join(rows)


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
        for label, grp in _grouped_by_type(upcoming):
            if label:
                rows += ["", f"### {label}", ""]
            for u in grp:
                nm = names.get(u["symbol"]) or u["symbol"]
                rows.append(f"- **{nm}** ({u['symbol']}) — {u['date']:%d-%b}: {u['what']}")
        parts.append("\n".join(rows))

    if movers:
        def _mline(m):
            pc = m["chg_pct"]
            emo = "🟢" if pc and pc > 0 else "🔴" if pc and pc < 0 else "⚪"
            chg = f"{pc:+.1f}%" if pc is not None else "n/a"
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
            return (f"- {emo} **{m['company']}** ({m['symbol']}) — ₹{_fmt_price(m['close'])} · "
                    f"{chg} · {deliv}{tail}{val}{_levels_bracket(m)}")
        rows = ["## Movers (today)"]
        for label, grp in _grouped_by_type(movers):
            if label:
                rows += ["", f"### {label}", ""]
            rows += [_mline(m) for m in grp]
        parts.append("\n".join(rows))

    lvl = _level_alert_section(sr.level_alerts, names)
    if lvl:
        parts.append(lvl)

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


def _intraday_movers(syms: list[str], quotes: dict) -> list[dict]:
    rows = []
    for s in syms:
        d = quotes.get(s) or {}
        if d.get("last") is None or d.get("pchange") is None:
            continue
        rows.append({"symbol": s, "company": d.get("company") or s, **d})
    rows.sort(key=lambda r: abs(r.get("pchange") or 0), reverse=True)
    return rows


def _intraday_filings(anns_by_sym: dict, names: dict) -> list[dict]:
    """Today's (IST) non-routine corporate filings across the watchlist."""
    from equity_research.analysis.alerts import _categorise, _clip
    today = datetime.now(_IST).date()
    out, seen = [], set()
    for sym, anns in (anns_by_sym or {}).items():
        for a in anns or []:
            try:
                adt = datetime.strptime(a.get("an_dt", "")[:20].strip(), "%d-%b-%Y %H:%M:%S")
            except (ValueError, TypeError):
                continue
            if adt.date() != today:
                continue
            title, sev, _ = _categorise(a.get("desc", ""), a.get("attchmntText", ""),
                                        str(a.get("hasXbrl", "")).lower() == "true")
            if title is None or (sym, title) in seen:        # routine noise / repeat
                continue
            seen.add((sym, title))
            out.append({"symbol": sym, "company": names.get(sym, sym), "sev": sev, "title": title,
                        "body": _clip(a.get("attchmntText") or a.get("desc") or ""), "time": adt})
    out.sort(key=lambda r: r["time"], reverse=True)
    return out


def _intraday_insider(insider_by_sym: dict) -> list[str]:
    """Today's (IST) material insider/promoter disclosures, formatted."""
    today = datetime.now(_IST).date()
    out = []
    for sym, rows in (insider_by_sym or {}).items():
        for r in rows or []:
            if not _is_material_insider(r):
                continue
            try:
                ddt = datetime.strptime((r.get("disclosure_dt") or "").strip(), "%d-%b-%Y %H:%M")
            except (ValueError, TypeError):
                continue
            if ddt.date() == today:
                out.append(_fmt_insider(sym, r))
    return out


def run_intraday_scan(con: duckdb.DuckDBPyConnection | None = None) -> IntradayResult:
    """Midday snapshot — the full digest's sections with LIVE data: live market header
    (live indices · VIX · FII/DII · FII-futures · USD/INR · commodities), Upcoming, live
    Movers, today's filings + insider. No EOD ingest (today's bhavcopy doesn't exist yet);
    FII/DII & positioning are prior-session until published after close. All best-effort."""
    own = con is None
    con = con or connect()
    try:
        syms = watchlist.symbols(con)
        names = {s: (c or s) for s, c in watchlist.entries(con)}

        def _safe(fn, default):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                log.exception("intraday section failed: %s", getattr(fn, "__name__", fn))
                return default

        def _labeler(descs):
            from equity_research.reports import synthesize
            return synthesize.label_events(descs)

        quotes = _safe(lambda: nse_api.live_quotes_batch(syms) if syms else {}, {})
        anns = _safe(lambda: nse_api.corporate_announcements_batch(syms) if syms else {}, {})
        insider = _safe(lambda: nse_api.insider_trades_batch(syms) if syms else {}, {})
        feeds = _safe(lambda: nse_api.market_feeds() if syms else {}, {})
        usd = _safe(lambda: fbil.usd_inr(), None)
        comm = _safe(lambda: mcx.commodities(), {})
        for s in syms:                                  # fill missing names from the live quote
            if names.get(s) in (None, s) and (quotes.get(s) or {}).get("company"):
                names[s] = quotes[s]["company"]
        market = "\n".join(x for x in (
            _safe(lambda: live_market_context(), ""),    # LIVE indices + VIX
            _safe(lambda: _fii_dii_line(feeds.get("fiidii") or []), ""),
            _safe(lambda: _fii_futures_line(positioning.fii_index_futures(con)), ""),
            _safe(lambda: _money_lines(usd, comm), ""),   # USD/INR + live commodities
        ) if x)
        tmap = watchlist.type_map(con)
        live_px = {s: (quotes.get(s) or {}).get("last") for s in syms
                   if (quotes.get(s) or {}).get("last") is not None}
        lmap = _safe(lambda: _levels_map(con, syms), {})
        movers = _annotate_mover_levels(
            _annotate_types(_safe(lambda: _intraday_movers(syms, quotes), []), tmap), lmap)
        return IntradayResult(
            movers=movers,
            filings=_safe(lambda: _intraday_filings(anns, names), []),
            insider=_safe(lambda: _intraday_insider(insider), []),
            market=market,
            upcoming=_annotate_types(
                _safe(lambda: watchlist_upcoming(syms, feeds, labeler=_labeler), []), tmap),
            level_alerts=_safe(
                lambda: _level_alerts(con, syms, names, tmap, live_prices=live_px, lmap=lmap), []),
            asof=datetime.now(_IST),
        )
    finally:
        if own:
            con.close()


def format_intraday_digest(sr: IntradayResult) -> str:
    """Midday digest — live market header, Upcoming, live Movers, today's Events + Insider."""
    asof = sr.asof or datetime.now(_IST)
    names = {m["symbol"]: m["company"] for m in sr.movers}
    parts = [f"# 🔔 Watchlist — same-day ({asof:%d-%b %H:%M} IST)"]
    if sr.market:
        parts.append(sr.market)
    if sr.upcoming:
        rows = ["## 📅 Upcoming"]
        for label, grp in _grouped_by_type(sr.upcoming):
            if label:
                rows += ["", f"### {label}", ""]
            for u in grp:
                nm = names.get(u["symbol"]) or u["symbol"]
                rows.append(f"- **{nm}** ({u['symbol']}) — {u['date']:%d-%b}: {u['what']}")
        parts.append("\n".join(rows))
    if sr.movers:
        def _mline(m):
            pc = m.get("pchange")
            emo = "🟢" if pc and pc > 0 else "🔴" if pc and pc < 0 else "⚪"
            rng = (f" · day {_fmt_price(m['low'])}–{_fmt_price(m['high'])}"
                   if m.get("low") and m.get("high") else "")
            deliv = f" · deliv {m['deliv_pct']:.0f}%" if m.get("deliv_pct") is not None else ""
            pct = f"{pc:+.1f}%" if pc is not None else "n/a"
            return (f"- {emo} **{m['company']}** ({m['symbol']}) — "
                    f"₹{_fmt_price(m['last'])} · {pct}{rng}{deliv}{_levels_bracket(m)}")
        rows = ["## Movers (live)"]
        for label, grp in _grouped_by_type(sr.movers):
            if label:
                rows += ["", f"### {label}", ""]
            rows += [_mline(m) for m in grp]
        parts.append("\n".join(rows))
    lvl = _level_alert_section(sr.level_alerts, names)
    if lvl:
        parts.append(lvl)
    if sr.filings:
        rows = ["## Events (filed today)"]
        for f in sr.filings:
            emo = alerts.EMOJI.get(f["sev"], "🔔")
            rows.append(f"- {emo} **{f['company']}** ({f['symbol']}) — {f['title']}"
                        + (f": {f['body']}" if f["body"] else ""))
        parts.append("\n".join(rows))
    if sr.insider:
        rows = ["## 🔬 Insider & promoter (today)"]
        rows += [f"- {ln}" for ln in sr.insider]
        parts.append("\n".join(rows))
    parts.append("_Same-day snapshot — FII/DII & positioning are prior-session; the deep "
                 "filing analysis + EOD delivery/valuation come in the 6 PM digest. "
                 "Reply with a company name for its report._")
    return "\n\n".join(parts)


def run_watchlist_scan(con: duckdb.DuckDBPyConnection | None = None) -> ScanResult:
    """Scan the watchlist → ScanResult(results, movers, upcoming). Ingests EOD first."""
    own = con is None
    con = con or connect()
    try:
        refresh_eod(con)
        try:                                 # accumulate the day's MF NAV universe
            ingest_mf_navall(con)
            ingest_mf_holdings_all(con)      # refresh covered AMCs' month-end holdings
        except Exception:  # noqa: BLE001 — MF data is a bonus, never break the scan
            pass
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
        # per-stock bulk/block deals (institutional buy/sell) — deduped across the bulk &
        # block feeds, counterparty classified (listed co / MF / FPI / individual …)
        listed = _listed_name_map(con)
        for sym, deal_alerts in watchlist_deals(syms, feeds.get("deals") or {}, listed).items():
            results.setdefault(sym, []).extend(deal_alerts)
        _enrich_event_docs(results)                         # inline LLM read of filings

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
        tmap = watchlist.type_map(con)
        names_all = {s: (c or s) for s, c in watchlist.entries(con)}
        lmap = _safe(lambda: _levels_map(con, syms), {})
        movers = _annotate_mover_levels(
            _annotate_types(_safe(lambda: watchlist_movers(con), []), tmap), lmap)
        return ScanResult(
            results,
            movers,
            _annotate_types(_safe(lambda: watchlist_upcoming(syms, feeds, labeler=_labeler), []), tmap),
            market,
            insider=_safe(lambda: _insider_alerts(con, insider_by_sym), []),
            level_alerts=_safe(lambda: _level_alerts(con, syms, names_all, tmap, lmap=lmap), []),
            pending_state=pending,
            pending_insider=insider_by_sym,
        )
    finally:
        if own:
            con.close()
