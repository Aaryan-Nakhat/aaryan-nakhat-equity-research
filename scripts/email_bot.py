"""Email channel for the equity-research workbench (Phase 5b).

A drop-in alternative to the Telegram bot for when Telegram is blocked. Same
brains (resolve -> deep report -> PDF, and the self-healing watchlist scan),
delivered over email instead:

  PULL  you email a stock name (Subject) from an allowlisted address ->
        IMAP IDLE wakes the bot -> it resolves, builds the deep report, and
        replies in-thread with formatted HTML + the PDF attached. Ambiguous
        names get a numbered "which one?" reply; you reply with the number.
  PUSH  once per trading day at/after 18:00 IST it runs the watchlist scan and
        emails a digest (with deep-report PDFs for any 'results filed' event).

Gated by the CHANNELS env flag (must contain 'email'); Telegram code is left
fully intact and revives by setting CHANNELS=telegram. Run via run_email_bot.ps1.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# make src/ importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from equity_research import scan  # noqa: E402
from equity_research import screen_digest  # noqa: E402
from equity_research.analysis import (booking_risk, holdco, investors, policy,  # noqa: E402
                                      screener, sector_analysis, sell_advisor, smallcap,
                                      supply_chain, technical, technical_screen)
from equity_research.common.db import connect  # noqa: E402
from equity_research.reports import charts  # noqa: E402
from equity_research.reports import deep_brief  # noqa: E402
from equity_research.reports import glossary  # noqa: E402
from equity_research.reports import md  # noqa: E402
from equity_research.reports import premarket  # noqa: E402
from equity_research.reports import sector_brief  # noqa: E402
from equity_research.reports import tailwind_brief  # noqa: E402
from equity_research.reports import email as emailer  # noqa: E402
from equity_research.reports.inbox import EmailRequest, Inbox  # noqa: E402
from equity_research.reports.pdf import report_to_pdf  # noqa: E402
from equity_research.reports.pipeline import (generate_report, generate_growth_triggers,  # noqa: E402
                                              generate_ipo_report)
from equity_research.reports.synthesize import fund_thesis  # noqa: E402
from equity_research.scrapers import ipo  # noqa: E402
from equity_research.reports.resolve import resolve  # noqa: E402
from equity_research.reports import fund_brief  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SCAN_HOUR = 18
INTRADAY_HOUR, INTRADAY_MIN = 12, 30    # midday same-day digest (12:30 IST)
INTRADAY_CUTOFF_HOUR = 14               # don't fire a stale "midday" digest after 2pm
PREMARKET_HOUR, PREMARKET_MIN = 8, 30   # pre-open GIFT Nifty digest (08:30 IST)
PREMARKET_CUTOFF_HOUR = 12             # catch-up: laptop is asleep at 08:30, so fire on the FIRST
                                       # heartbeat after 08:30 (typically your ~09:25 login), up to
                                       # noon (the 12:30 midday digest takes over past that). Past the
                                       # 09:15 open the digest self-relabels to a "gap so far" snapshot.
IDLE_TIMEOUT = 300          # IDLE wait + daily-scan heartbeat (< Gmail's ~29 min cap)
PENDING_TTL_H = 24          # how long a "which one?" choice stays answerable

ALLOWED = {a.strip().lower() for a in os.environ.get("EMAIL_ALLOWED_SENDERS", "").split(",") if a.strip()}

_LOGDIR = Path(__file__).resolve().parent.parent / "data" / "processed"
_LOGDIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s equity-email | %(message)s",
    handlers=[logging.FileHandler(_LOGDIR / "email_bot.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("equity-email")


# ----------------- disambiguation state (alert_state, '__email__' namespace) -----------------
# Pending menus are keyed by (sender, email-thread) — NOT sender alone — so a person can have
# several menus open at once (e.g. an `ipo: ongoing` list AND a stock's "want a deeper cut?"
# prompt) and a numbered reply resolves against the thread it was sent in, never a stale one.
def _thread_id(req: EmailRequest) -> str:
    """Stable short id for the email thread: hash of the thread-root Message-ID (first entry
    in References), falling back to the immediate parent, then this message's own id. A reply
    carries the same root, so its menu resolves in-thread."""
    refs = (req.references or "").split()
    root = refs[0] if refs else (req.in_reply_to or req.message_id or "")
    return hashlib.sha1(root.strip().encode("utf-8", "replace")).hexdigest()[:16]


def _pending_key(req: EmailRequest) -> str:
    return f"pending:{req.sender}:{_thread_id(req)}"


def _set_pending(req: EmailRequest, query: str, cands: list) -> None:
    con = connect()
    try:
        payload = json.dumps({"query": query, "ts": datetime.now(timezone.utc).isoformat(),
                              "cands": [[c.symbol, c.name] for c in cands]})
        con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                    "VALUES ('__email__', ?, ?, now())", [_pending_key(req), payload])
    finally:
        con.close()


def _find_pending(req: EmailRequest) -> tuple[str, list] | None:
    """The pending menu a reply answers → (storage_key, cands), or None. Matches the reply's
    thread first; if none matches but the sender has exactly one live menu, uses that (rescues
    replies whose client dropped the threading headers). Expired menus (>TTL) are ignored."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT key, value FROM alert_state WHERE symbol='__email__' AND key LIKE ?",
            [f"pending:{req.sender}:%"]).fetchall()
    finally:
        con.close()
    fresh: list[tuple[str, list]] = []
    for key, val in rows:
        data = json.loads(val)
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(data["ts"])).total_seconds() / 3600
        if age_h <= PENDING_TTL_H:
            fresh.append((key, data["cands"]))
    if not fresh:
        return None
    want = _pending_key(req)
    for key, cands in fresh:
        if key == want:
            return key, cands
    # Fallback ONLY for a bare new email (no threading headers at all) with a lone live
    # menu. A reply that IS threaded but matches nothing must never borrow another
    # thread's menu — that's how a fund reply once triggered a stock's growth triggers.
    if not req.references and not req.in_reply_to and len(fresh) == 1:
        return fresh[0]
    return None


def _clear_consumed(key: str, cands: list) -> None:
    """Delete the pending menu at ``key`` ONLY if it still holds the menu we just
    answered. The report handlers arm a NEW menu (the deeper-cut follow-up) under the
    same thread key during handling — an unconditional post-send delete would wipe
    that fresh menu (the bug that broke 'reply 1' after an IPO-list choice)."""
    con = connect()
    try:
        row = con.execute("SELECT value FROM alert_state WHERE symbol='__email__' AND key=?",
                          [key]).fetchone()
        if row and json.loads(row[0]).get("cands") == cands:
            con.execute("DELETE FROM alert_state WHERE symbol='__email__' AND key=?", [key])
    finally:
        con.close()


# ----------------- helpers -----------------
def _re_subject(subject: str) -> str:
    """'Re: <original subject>' — NEVER append suffixes: Gmail only groups messages
    into one conversation when the subject matches (ignoring Re:), so a decorated
    subject ('… — growth triggers') forks a brand-new thread despite correct
    In-Reply-To/References. One request flow = one subject = one thread."""
    s = subject.strip()
    if not s.lower().startswith("re:"):
        s = f"Re: {s}"
    return s


def _clean_query(subject: str) -> str:
    """Strip a leading 'Re:' and any consolidated/standalone keyword from the query."""
    q = re.sub(r"^\s*re:\s*", "", subject, flags=re.I)
    q = re.sub(r"\b(consolidated|standalone|cons)\b", "", q, flags=re.I)
    return q.strip()


def _basis(subject: str) -> bool | None:
    """Reporting basis from the subject: True=consolidated, False=standalone,
    None=auto (let the pipeline decide)."""
    s = subject.lower()
    if "consolidated" in s or re.search(r"\bcons\b", s):
        return True
    if "standalone" in s:
        return False
    return None


def _selection(body: str) -> int | None:
    m = re.search(r"\d+", body or "")
    return int(m.group()) if m else None


# ----------------- delivery -----------------
def _ack(symbol: str, req: EmailRequest, resolved_name: str | None = None) -> None:
    """Instant 'got it, working on it' reply so you know it's processing."""
    name = f" ({resolved_name})" if resolved_name else ""
    md = (f"📩 Got it — building the deep report for **{symbol}**{name}.\n\n"
          "This takes ~2–3 minutes; the full analysis + PDF will land in this thread shortly.")
    try:
        emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                            html=emailer.body_html(md),
                            in_reply_to=req.message_id, references=req.references or req.message_id)
    except Exception:  # noqa: BLE001 — an ack failure shouldn't block the real report
        log.exception("ack send failed for %s", symbol)


def _pdf_with_charts(symbol: str, report_md: str) -> bytes | None:
    """Full report PDF with the fundamental charts embedded — best-effort with a
    HARD timeout. The PDF (Playwright Chromium) can hang on a busy box; the full
    report is already in the email body, so on timeout/failure we return None and
    deliver body-only rather than blocking the whole send forever."""
    con = connect()
    try:
        images = charts.report_charts(con, symbol)
    except Exception:  # noqa: BLE001 — a chart should never block the report
        log.exception("charts failed for %s", symbol)
        images = []
    finally:
        con.close()
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, symbol, images).result(timeout=150)
    except Exception:  # noqa: BLE001 — timeout or render failure
        log.exception("report PDF generation failed/timed out for %s — sending body-only", symbol)
        return None
    finally:
        ex.shutdown(wait=False)            # don't block on a hung render thread


class _MenuItem:
    """Pending-state shim for a follow-up menu choice (reuses the numbered-reply UX).
    ``symbol`` is prefixed by action, e.g. ``GT:RELIANCE`` → growth triggers."""
    def __init__(self, symbol: str, name: str | None) -> None:
        self.symbol = symbol
        self.name = name or ""


def _set_followup(req: EmailRequest, symbol: str, name: str | None, *, ipo_mode: bool = False) -> None:
    """Arm the numbered follow-up menu in THIS thread so a bare-number reply maps back
    to a deeper cut for ``symbol`` (24h TTL, via the thread-scoped pending state). ``ipo_mode``
    tags the growth-triggers item as IPO (``IGT:`` — grounded in the offer docs)."""
    tag = "IGT" if ipo_mode else "GT"
    _set_pending(req, f"__followup__:{symbol}", [_MenuItem(f"{tag}:{symbol}", name)])


def _send_followup_menu(symbol: str, req: EmailRequest, name: str | None = None,
                        *, ipo_mode: bool = False) -> None:
    """A short, separate in-thread email sent right AFTER the report — asks whether you
    want a deeper cut, and arms the numbered reply. Extensible: add a menu row + a matching
    prefix branch in handle_request for the next cut."""
    grounded = ("the RHP & offer documents" if ipo_mode
                else "the company's concalls & investor presentations")
    md = (f"✅ Full {'IPO note' if ipo_mode else 'report'} for **{symbol}**"
          + (f" — {name}" if name else "") + " is in the previous email (body + PDF).\n\n"
          "**Want a deeper cut?** Just reply to this email with the number:\n\n"
          "  **1) Growth-triggers 1-pager** — forward-looking catalysts, each quantified, "
          f"timeline-tagged and rated HIGH / MEDIUM / OPTIONALITY conviction, grounded in {grounded}.\n\n"
          "_(More deeper cuts coming soon.)_")
    emailer.send_report(
        _re_subject(req.subject),
        md, to=req.sender, html=emailer.body_html(md, symbol),
        in_reply_to=req.message_id, references=req.references or req.message_id,
    )
    _set_followup(req, symbol, name, ipo_mode=ipo_mode)
    log.info("sent deeper-cut menu for %s to %s", symbol, req.sender)


def _send_report(symbol: str, req: EmailRequest, resolved_name: str | None = None,
                 consolidated: bool | None = None, *, ack: bool = True) -> None:
    log.info("generating report for %s (req from %s, basis=%s)", symbol, req.sender,
             {True: "consolidated", False: "standalone"}.get(consolidated, "auto"))
    if ack:                                     # fresh queries pre-ack at pickup instead
        _ack(symbol, req, resolved_name)
    report_md = generate_report(symbol, deep=True, consolidated=consolidated)  # full report — body + PDF
    pdf = _pdf_with_charts(symbol, report_md)
    today = datetime.now(IST).date().isoformat()
    head = f"Report for **{symbol}**" + (f" — {resolved_name}" if resolved_name else "")
    body = f"{head}\n\n{report_md}"
    attachments = [("Metrics_and_ratings_guide.pdf", glossary.guide_pdf())]
    if pdf:
        attachments.insert(0, (f"{symbol}_{today}.pdf", pdf))
    else:
        body += "\n\n_(The charted PDF couldn't be generated this time — the full report is above.)_"
    emailer.send_report(
        _re_subject(req.subject),
        body,
        to=req.sender,
        html=emailer.body_html(body, symbol),
        attachments=attachments,
        in_reply_to=req.message_id,
        references=req.references or req.message_id,
    )
    log.info("sent report for %s to %s", symbol, req.sender)
    _send_followup_menu(symbol, req, resolved_name)      # separate "want a deeper cut?" prompt


def _send_growth_triggers(symbol: str, req: EmailRequest, name: str | None = None,
                          *, ipo_mode: bool = False) -> None:
    """Growth-triggers 1-pager (opt-in deeper cut) — email body + PDF, in-thread. ``ipo_mode``
    grounds it in the IPO offer documents instead of listed filings."""
    log.info("generating growth triggers for %s (req from %s, ipo=%s)", symbol, req.sender, ipo_mode)
    _reply_text(req, f"🚀 Building the growth-triggers 1-pager for **{symbol}**"
                     + (f" ({name})" if name else "") + " — ~1–2 min; it'll land in this thread.")
    md = generate_growth_triggers(symbol, ipo_mode=ipo_mode)
    if not md:
        src = "offer documents" if ipo_mode else "filings (concalls / presentations)"
        _reply_text(req, f"Couldn't build growth triggers for {symbol} — no {src} "
                         "were available to ground it.")
        return
    pdf = _text_pdf(md, f"{symbol} — Growth Triggers")
    today = datetime.now(IST).date().isoformat()
    head = f"Growth triggers — **{symbol}**" + (f" — {name}" if name else "")
    body = f"{head}\n\n{md}"
    attachments = []
    if pdf:
        attachments.append((f"{symbol}_growth_triggers_{today}.pdf", pdf))
    else:
        body += "\n\n_(The PDF couldn't be generated this time — the full 1-pager is above.)_"
    emailer.send_report(
        _re_subject(req.subject),
        body,
        to=req.sender,
        html=emailer.body_html(body, f"{symbol} — growth triggers"),
        attachments=attachments,
        in_reply_to=req.message_id,
        references=req.references or req.message_id,
    )
    log.info("sent growth triggers for %s to %s", symbol, req.sender)


def _text_pdf(report_md: str, title: str) -> bytes | None:
    """Text-only PDF (no charts) for the deeper-cut / IPO notes — best-effort with a HARD
    timeout so a hung Chromium render never blocks; the note is already in the email body."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, title, []).result(timeout=150)
    except Exception:  # noqa: BLE001 — timeout or render failure
        log.exception("PDF failed/timed out for %r — sending body-only", title)
        return None
    finally:
        ex.shutdown(wait=False)


# ----------------- IPO (pre-listing) -----------------
def _ipo_list_safe(fn, *, timeout: int = 150):
    """Run a browser-tier IPO list fetch under a HARD timeout so a wedged Camoufox session
    can never freeze the request loop. Returns ``None`` (== fetch failure, so the caller
    replies 'try again') on timeout or error; the healthy fetch takes ~60-90s."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=timeout)
    except Exception:  # noqa: BLE001 — timeout or fetch error → signal failure
        log.exception("IPO list fetch timed out / failed")
        return None
    finally:
        ex.shutdown(wait=False)                # don't block on a hung browser thread


def _ipo_query(subject: str) -> tuple[str, str] | None:
    """Parse an 'ipo: ...' subject → ('list','ongoing'|'upcoming') or ('name', <query>);
    None if it isn't an IPO request."""
    m = re.match(r"^\s*(?:re:\s*)?ipo\s*[:\-]\s*(.+)$", subject, flags=re.I)
    if not m:
        return None
    val = m.group(1).strip()
    low = val.lower()
    if low in ("ongoing", "live", "current", "open", "active"):
        return ("list", "ongoing")
    if low in ("upcoming", "forthcoming", "coming", "new"):
        return ("list", "upcoming")
    return ("name", val)


def _ipo_line(i: int, x: dict) -> str:
    sub = f" · {x['subscription_x']:.1f}x sub" if x.get("subscription_x") else ""
    dates = f"{x['start']}–{x['end']}" if x.get("start") else ""
    return f"  {i}) {x['symbol']:<10} — {x['company']} · {x['price_band']} · {dates}{sub}"


def _send_ipo_list(kind: str, req: EmailRequest) -> None:
    """List live / upcoming IPOs as a numbered menu; a numeric reply → that IPO's note.
    'upcoming' is filtered to issues whose RHP is already published (so it's analysable)."""
    if kind == "ongoing":
        ipos = _ipo_list_safe(ipo.list_current)
        title = "🟢 Live IPOs (open now)"
    else:
        ipos = _ipo_list_safe(ipo.list_upcoming)
        if ipos is not None:
            ipos = [x for x in ipos if ipo.has_prospectus(x["symbol"])]
        title = "🔜 Upcoming IPOs (RHP available)"
    if ipos is None:                       # NSE fetch failed (not the same as 'none open')
        _reply_text(req, "Couldn't reach NSE for the IPO list just now — its bot-protected "
                         "endpoint is timing out. Please resend `ipo: " + kind + "` in a minute.")
        return
    if not ipos:
        _reply_text(req, f"No {kind} IPOs "
                    + ("open right now." if kind == "ongoing"
                       else "with an RHP published yet. Check back closer to the open date."))
        return
    cands = [_MenuItem(f"IPO:{x['symbol']}", x["company"]) for x in ipos]
    _set_pending(req, f"ipo:{kind}", cands)
    lines = "\n".join(_ipo_line(i, x) for i, x in enumerate(ipos, 1))
    md = (f"**{title}** — reply to this email with just the number for a full pre-listing "
          f"analysis (financials, fresh/OFS, valuation vs peers, risks, apply-or-not):\n\n"
          f"```\n{lines}\n```\n\n(Reply within {PENDING_TTL_H}h.)")
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "IPOs"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("listed %d %s IPOs to %s", len(ipos), kind, req.sender)


def _send_ipo_report(symbol: str, req: EmailRequest, name: str | None = None) -> None:
    """Pre-listing IPO note — email body + PDF, in-thread — then the deeper-cut menu."""
    log.info("generating IPO note for %s (req from %s)", symbol, req.sender)
    _reply_text(req, f"🧾 Building the pre-listing IPO analysis for **{symbol}**"
                     + (f" ({name})" if name else "")
                     + " — reading the RHP; ~2–3 min, it'll land in this thread.")
    md = generate_ipo_report(symbol)
    if not md:
        _reply_text(req, f"Couldn't build the IPO note for {symbol} — the offer documents "
                         "(RHP) aren't published on NSE yet.")
        return
    pdf = _text_pdf(md, f"{symbol} — IPO analysis")
    today = datetime.now(IST).date().isoformat()
    head = f"IPO analysis — **{symbol}**" + (f" — {name}" if name else "")
    body = (f"{head}\n\n{md}\n\n---\n\n_An **IPO metrics & terminology guide** is attached — "
            "plain-English on fresh-issue vs OFS, QIB/NII/RII subscription, anchor investors, "
            "RoNW, contingent liabilities and the APPLY/NEUTRAL/AVOID scale._")
    attachments = [("IPO_metrics_and_terminology_guide.pdf", glossary.ipo_guide_pdf())]
    if pdf:
        attachments.insert(0, (f"{symbol}_IPO_{today}.pdf", pdf))
    else:
        body += "\n\n_(The PDF couldn't be generated this time — the full note is above.)_"
    emailer.send_report(
        _re_subject(req.subject), body, to=req.sender,
        html=emailer.body_html(body, f"{symbol} — IPO"), attachments=attachments,
        in_reply_to=req.message_id, references=req.references or req.message_id,
    )
    log.info("sent IPO note for %s to %s", symbol, req.sender)
    _send_followup_menu(symbol, req, name, ipo_mode=True)   # IPO growth-triggers follow-up


def _handle_ipo(kind: str, val: str, req: EmailRequest) -> None:
    """Route an 'ipo:' request → a live/upcoming list, or a named-IPO note. Ack first —
    the NSE list fetch is browser-tier (~1-2 min) and silence provokes resends."""
    if kind == "list":
        _reply_text(req, f"📩 Got it — fetching the {val} IPO list from NSE "
                         "(its bot-protected API takes ~1–2 min). The list will land in this thread.")
        _send_ipo_list(val, req)
        return
    _reply_text(req, f"📩 Got it — looking up the IPO '{val}' on NSE (~1–2 min).")
    # a named IPO — match against live then upcoming by symbol / company substring
    q = val.lower()
    pool = (_ipo_list_safe(ipo.list_current) or []) + (_ipo_list_safe(ipo.list_upcoming) or [])
    hits = [x for x in pool if q in x["symbol"].lower() or q in x["company"].lower()]
    if not hits:
        _reply_text(req, f"Couldn't find a live or upcoming IPO matching '{val}'. "
                         "Try `ipo: ongoing` or `ipo: upcoming` to see the current list.")
    elif len(hits) == 1:
        _send_ipo_report(hits[0]["symbol"], req, hits[0]["company"])
    else:
        cands = [_MenuItem(f"IPO:{x['symbol']}", x["company"]) for x in hits]
        _set_pending(req, f"ipo:{val}", cands)
        _send_choices(val, cands, req)


def _send_choices(query: str, cands: list, req: EmailRequest) -> None:
    lines = [f"  {i}) {c.symbol:<12} — {c.name}" for i, c in enumerate(cands, 1)]
    md = (f'"{query}" matched several NSE listings. **Reply to this email with just '
          f'the number:**\n\n```\n' + "\n".join(lines) + "\n```\n\n"
          f"(Reply within {PENDING_TTL_H}h; otherwise just send a fresh email.)")
    emailer.send_report(
        _re_subject(req.subject),
        md,
        to=req.sender,
        html=emailer.body_html(md),
        in_reply_to=req.message_id,
        references=req.references or req.message_id,
    )
    log.info("asked %s to disambiguate %r (%d candidates)", req.sender, query, len(cands))


def _reply_text(req: EmailRequest, text: str) -> None:
    emailer.send_report(_re_subject(req.subject), text, to=req.sender,
                        html=emailer.body_html(text),
                        in_reply_to=req.message_id, references=req.references or req.message_id)


# ----------------- screeners (idea generation) -----------------
def _screen_query(subject: str) -> str | None:
    """Parse a screener request → 'holdco' | 'investors' | 'smallcap' | 'policy' | 'technical' |
    'value' (default), or None if not a screen. Accepts 'screen: holdco', 'screen: investors',
    'screen: smallcap', 'screen: policy', 'screen: technical', 'screen: value', bare 'screen'."""
    m = re.match(r"^\s*(?:re:\s*)?screen\s*[:\-]?\s*(.*)$", subject, flags=re.I)
    if not m:
        return None
    val = m.group(1).strip().lower()
    if val in ("holdco", "holdcos", "holding", "discount", "discounts"):
        return "holdco"
    if val in ("investors", "investor", "hni", "hnis", "marquee", "bigbull", "big bull"):
        return "investors"
    if val in ("smallcap", "smallcaps", "small cap", "small-cap", "capex", "smallcap capex"):
        return "smallcap"
    if val in ("policy", "policies", "scheme", "schemes", "govt", "government", "gov",
               "policy radar", "scheme radar", "budget"):
        return "policy"
    if val in ("technical", "technicals", "ta", "setup", "setups", "momentum", "chart", "charts",
               "buy", "buys"):
        return "technical"
    return "value"


def _sector_query(subject: str) -> str | None:
    """Parse a sector request → the raw sector text (e.g. 'defence', 'pharma', 'list'), or None if
    not a `sector:` command. 'sector: list' / 'sector: help' returns the literal 'list'."""
    m = re.match(r"^\s*(?:re:\s*)?sector\s*[:\-]\s*(.+)$", subject, flags=re.I)
    if not m:
        return None
    return m.group(1).strip()


def _suppliers_query(subject: str) -> str | None:
    """Parse a supply-chain request ('suppliers: BEL', 'supply chain: HAL', 'ancillaries: <co>')
    → the company text, or None if not a suppliers command."""
    m = re.match(r"^\s*(?:re:\s*)?(?:suppliers?|supply\s*chain|ancillaries|ancillary|vendors?)"
                 r"\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m else None


def _booking_query(subject: str) -> bool:
    """True for a portfolio profit-booking-risk request ('booking', 'booking risk', 'profit
    booking', 'sell risk', 'booking:')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:booking(?:\s*risk)?|profit[\s-]*booking|sell[\s-]*risk)"
                         r"\s*[:\-]?\s*$", subject, flags=re.I))


def _policy_query(subject: str) -> bool:
    """True for a bare policy-radar request ('policy:', 'schemes', 'policy radar', 'govt schemes')
    outside the `screen:` prefix."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:policy|policies|schemes?|policy radar|scheme radar|"
                         r"govt schemes?|government schemes?)\s*[:\-]?\s*$", subject, flags=re.I))


def _tailwind_query(subject: str) -> bool:
    """True for a global supply-shock / Tailwind request ('tailwind', 'tailwind:', 'catalysts',
    'global catalysts', 'supply shock'), optionally with a trailing '--latest' / 'fresh' flag."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:tailwind|tailwinds|global\s*catalyst(?:s)?|"
                         r"catalyst(?:s)?|supply\s*shock(?:s)?)\s*[:\-]?\s*"
                         r"(?:(?:--?\s*)?(?:latest|fresh|refresh|new|now))?\s*$", subject, flags=re.I))


def _wants_latest(subject: str) -> bool:
    """True if a Tailwind request asks to bypass the 24h cache ('--latest' / 'latest' / 'fresh')."""
    return bool(re.search(r"(?:--?\s*)?\b(?:latest|fresh|refresh)\b", subject or "", flags=re.I))


def _investor_query(subject: str) -> str | None:
    """Parse 'investor: <name>' / 'hni: <name>' → the free-text name, or None."""
    m = re.match(r"^\s*(?:re:\s*)?(?:investor|hni)\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _help_query(subject: str) -> bool:
    """True for a bare help / command-menu request ('help', 'commands', 'menu', '?',
    'what can you do')."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:help|help\s*me|commands?|command\s*list|menu|"
                         r"what\s*can\s*(?:you|i)\s*(?:do|ask)|\?)\s*[:\-]?\s*$",
                         subject, flags=re.I))


def _sell_query(subject: str) -> bool:
    """True for a holdings sell-priority request — bare 'sell' / 'raise' / 'trim' (optionally
    with trailing text, e.g. 'sell: need cash'). Ranks YOUR holdings weakest-hand first."""
    return bool(re.match(r"^\s*(?:re:\s*)?(?:sell|raise|trim)(?:\s*[:\-]\s*.*)?$",
                         subject, flags=re.I))


def _levels_query(subject: str) -> str | None:
    """Parse 'levels: <name>' / 'technical: <name>' / 'setup: <name>' / 'chart: <name>' →
    the free-text company name, or None. A quick, no-LLM technical read (support/resistance
    zones, structure, patterns, entry/stop/target) with an annotated chart."""
    m = re.match(r"^\s*(?:re:\s*)?(?:levels?|technicals?|setup|chart)\s*[:\-]\s*(.+)$",
                 subject, flags=re.I)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _levels_pdf(report_md: str, symbol: str, images: list) -> bytes | None:
    """Small PDF for a levels reply — the section tables + the annotated chart. Best-effort
    under a hard timeout (the body already carries the text)."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, f"{symbol} — Trading levels",
                         images).result(timeout=90)
    except Exception:  # noqa: BLE001
        log.exception("levels PDF failed for %s", symbol)
        return None
    finally:
        ex.shutdown(wait=False)


def _send_levels(query: str, req: EmailRequest) -> None:
    """On-demand technical levels for a named stock — computed, no LLM. Resolves the name,
    maps support/resistance zones + structure + a reward:risk setup, and sends the text plus
    an annotated candlestick chart (zones + entry/stop/target). Auto-uses the best resolve
    match (this is a quick look-up); the reply says which symbol it used."""
    _reply_text(req, f"📈 Reading the price structure for **{query}** — support/resistance "
                     "levels, patterns & setup (~30s, no wait for the LLM).")
    try:
        cands = resolve(query)
    except Exception:  # noqa: BLE001
        log.exception("resolve failed for levels %r", query)
        _reply_text(req, f"Couldn't look up '{query}' right now — please try again.")
        return
    if not cands:
        _reply_text(req, f"Couldn't resolve '{query}' to an NSE symbol. Try the exact name.")
        return
    symbol, name = cands[0].symbol, cands[0].name
    con = connect()
    try:
        lv = technical.levels(con, symbol)
        lines = deep_brief.render_levels(con, symbol, lv)
        chart = (charts.levels_chart(con, symbol, lv, draw_setup=True)
                 if lv.get("history_ok") else None)
    finally:
        con.close()
    if not lines:
        _reply_text(req, f"No price history on file for {symbol} yet — can't map levels.")
        return
    head = f"📈 Trading levels — **{symbol}**" + (f" — {name}" if name else "")
    if len(cands) > 1:
        head += (f"\n\n_Resolved '{query}' → {symbol}. Reply with the exact name if you "
                 "meant a different company._")
    body = head + "\n\n" + "\n".join(lines)
    images = [chart] if chart else []
    pdf = _levels_pdf(body, symbol, images) if images else None
    attachments = [(f"{symbol}_levels.pdf", pdf)] if pdf else []
    emailer.send_report(
        _re_subject(req.subject), body, to=req.sender,
        html=emailer.body_html(body, symbol), attachments=attachments,
        in_reply_to=req.message_id, references=req.references or req.message_id,
    )
    log.info("sent levels for %s to %s", symbol, req.sender)


def _screen_run(fn, *, timeout: int = 300):
    """Run a screener under a HARD timeout (they loop the universe with per-symbol analysis).
    Returns None on timeout/error so the caller replies honestly instead of hanging."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=timeout)
    except Exception:  # noqa: BLE001
        log.exception("screener run timed out / failed")
        return None
    finally:
        ex.shutdown(wait=False)


def _crore(v) -> str:
    if v is None:
        return "n/a"
    return f"₹{v/1e5:,.2f} L cr" if v >= 1e5 else f"₹{v:,.0f} cr"


_md_table = md.table          # shared markdown pipe-table helper (renders as styled <table>)


# The complete command menu, section by section. Kept in one place so `help` never drifts from
# what handle_request actually dispatches. (cmd, what-you-get) rows; commands in `backticks`.
_HELP_SECTIONS: list[tuple[str, str, list[list[str]]]] = [
    ("📊 Stock deep report", "Just put a company name or NSE symbol in the Subject — the core report.", [
        ["`Adani Power` (any company name or symbol)",
         "Full deep report: fundamentals, forensics (Altman / Beneish / Piotroski), sector-lens "
         "valuation, technicals, shareholding + smart-money cost & profit-booking risk — with a PDF."],
        ["`Reliance consolidated` / `Reliance standalone`",
         "Same report, forced to that financials basis (default auto-picks)."],
        ["reply `1` after a report",
         "Growth-triggers 1-pager — forward catalysts, each with an estimated ₹cr / % business impact."],
    ]),
    ("💰 Your portfolio & holdings", "Reads your tagged holdings in the watchlist.", [
        ["`booking`", "Where the tracked institutions on YOUR holdings sit on big gains → "
                      "profit-booking (selling) risk, ranked."],
        ["`sell` (or `raise` / `trim`)",
         "Ranks your holdings weakest-hand-first — which to sell first if you need cash."],
    ]),
    ("🔎 Idea screeners — find new names", "Each returns a numbered list; reply a number → deep report.", [
        ["`screen: value` (or just `screen`)",
         "Quality + forensic + cheap-vs-own-history, ranked across the Nifty-500."],
        ["`screen: holdco`", "Holding companies trading below their listed-stake NAV (the Elcid trade)."],
        ["`screen: investors`", "Where marquee HNIs just entered / added / trimmed."],
        ["`screen: smallcap`", "Capex-led small-caps (spending now for future growth)."],
        ["`screen: technical`", "Strongest chart setups to buy — with entry / stop / target."],
        ["`screen: policy`", "Recent govt schemes/policies → likely listed beneficiaries."],
    ]),
    ("🧭 Sector analysis", "Top-down, one sectoral index at a time.", [
        ["`sector: <name>` e.g. `sector: defence`, `sector: pharma`",
         "Trend + valuation vs its own history + who's accumulating + best & cheapest names + "
         "supply chain. Reply a number → deep report."],
        ["`sector: list`", "The ~20 sectors covered."],
        ["`sector: rotation`", "All sectors ranked — leaders / laggards / turning-up-from-cheap."],
    ]),
    ("👤 Marquee investors (HNIs)", "", [
        ["`investor: <name>` e.g. `investor: Mukul Agrawal`",
         "That investor's disclosed holdings + latest-quarter moves + their cost vs the current price."],
    ]),
    ("🔗 Supply chain", "", [
        ["`suppliers: <company>` e.g. `suppliers: BEL`",
         "The smaller LISTED suppliers / ancillaries feeding that name. Reply a number → deep report."],
    ]),
    ("💨 Global supply shocks (Tailwind)", "Cached for 24h — add `--latest` to force a fresh scan.", [
        ["`tailwind` (or `catalysts`)",
         "Global commodity shocks — export bans / quotas / tariffs (metals, agri, pharma, chemicals, "
         "energy…) → verified Indian beneficiaries, with the supplier's world-share and each firm's "
         "revenue-share & market share. Reply with a symbol → deep report."],
        ["`tailwind --latest` (or `tailwind fresh`)",
         "Same, but forces a brand-new live scan instead of the 24h-cached result."],
    ]),
    ("🏛️ Government policy radar", "", [
        ["`policy` (or `schemes`)",
         "Recent official (PIB) schemes / policies → likely listed beneficiaries (watchlist flagged). "
         "Same as `screen: policy`."],
    ]),
    ("📈 Quick technical levels", "Computed, no LLM wait (~30s).", [
        ["`levels: <name>` (or `chart:` / `setup:`)",
         "Support / resistance zones, structure, patterns, entry / stop / target + an annotated chart."],
    ]),
    ("🟢 IPOs", "", [
        ["`ipo: ongoing`", "IPOs open right now → reply a number → full analysis."],
        ["`ipo: upcoming`", "Upcoming IPOs whose RHP is out → reply a number → analysis."],
        ["`ipo: <name>`", "That IPO's note — price band, financials, fresh vs OFS, risks, anchors."],
    ]),
    ("💵 Mutual funds", "", [
        ["`fund: <name>` (or `mf: <name>`) e.g. `fund: Parag Parikh Flexi Cap`",
         "Fund report — returns, rolling performance, risk, top holdings — with a PDF. "
         "Several matches → a numbered menu."],
    ]),
    ("📬 Arrives automatically (no command needed)", "Scheduled pushes to your inbox.", [
        ["🌅 Pre-market (morning)", "GIFT Nifty implied Nifty open + overnight US/Asia + VIX / FII + news."],
        ["🔔 Midday (12:30)", "Live movers, filings today, insider trades."],
        ["📊 Full digest (18:00)", "Market header + watchlist alerts + events (with filing analysis) + insider."],
        ["📡 Screener movements (Sat 18:00)", "What newly crossed the screens this week."],
        ["🔄 Sector rotation (Sat 18:00)", "Sector leaders / laggards / value-turning."],
        ["💨 Tailwind (Sat + mid-week urgent)", "Global supply-shock → Indian beneficiaries."],
    ]),
]

_HELP_FOOTER = (
    "**Tips**\n\n"
    "- **Reply a number** to any list (screeners, sectors, suppliers, IPOs, funds) to drill into it.\n"
    "- Add **consolidated** or **standalone** after a stock name to force the financials basis.\n"
    "- A list's numbered menu stays answerable for **24h** in that email thread.\n"
    "- Not sure of a name? Just send your best guess — I resolve it (and ask if there are several)."
)


def _send_help(req: EmailRequest) -> None:
    """The full command menu — every option, section by section, each as a table."""
    log.info("sending help/command menu to %s", req.sender)
    parts = ["# 📖 Your command menu",
             "_Everything you can email me — put the command in the **Subject** line. "
             "Section by section below._"]
    for title, intro, rows in _HELP_SECTIONS:
        parts.append(f"## {title}")
        if intro:
            parts.append(f"_{intro}_")
        parts.append(_md_table(["Put this in the Subject", "What you get back"], rows, "ll"))
    parts.append("---")
    parts.append(_HELP_FOOTER)
    body = "\n\n".join(parts)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Commands"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)


def _send_fundamental_screen(req: EmailRequest) -> None:
    """Ranked value+quality+forensic screen → a numbered list; reply a number → deep report."""
    log.info("running fundamental screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — running the quality + forensic + cheapness screen across "
                     "the Nifty-500 (~1–2 min). The ranked list will land in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: screener.fundamental_screen(con, limit=20))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The screen timed out this time — please resend `screen: value` in a moment.")
        return
    if not rows:
        _reply_text(req, "No names scored — the universe's financials may not be ingested yet. "
                         "Run the one-time `backfill_universe.py` to seed the Nifty-500.")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Score", "Why"],
        [[i, r["symbol"], r["name"][:34], f"{r['composite']:.1f}", r["why"]]
         for i, r in enumerate(rows, 1)],
        align="rllrl")
    md = ("**🔎 Value + quality + forensic screen — Nifty-500**\n\n"
          "Composite 0-100: 40% quality · 35% forensic · 25% cheap-vs-own-history. "
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n"
          f"_The screen finds; your reply diligences. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]      # plain SYM → numbered reply → deep report
    _set_pending(req, "screen:value", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — value"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent fundamental screen (%d names) to %s", len(rows), req.sender)


def _send_sector_list(req: EmailRequest) -> None:
    """List the sectors the `sector:` command understands."""
    cat = sector_analysis.catalog()
    lines = [f"- {m['emoji']} **{k}** — {m['index']}" for k, m in cat.items()]
    md = ("**🧭 Sector analysis — available sectors**\n\n"
          "Send `sector: <name>` (e.g. `sector: defence`, `sector: pharma`, `sector: realty`) for a "
          "top-down read: index trend + valuation vs its own history, who's accumulating it, and the "
          "best / cheapest names inside it (reply a number for a stock's deep report).\n\n"
          + "\n".join(lines))
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Sectors"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)


def _send_sector_rotation(req: EmailRequest) -> None:
    """On-demand sector-rotation digest (leaders / laggards / value-turning across all sectors)."""
    log.info("running sector rotation (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — ranking all sectors by relative strength vs Nifty + valuation "
                     "vs their own history. Lands in this thread shortly.")
    con = connect()
    try:
        body = _screen_run(lambda: sector_brief.build_sector_rotation(con))
    finally:
        con.close()
    if not body:
        _reply_text(req, "Couldn't rank the sectors this time — please resend `sector: rotation`.")
        return
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Sector rotation"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sector rotation to %s", req.sender)


def _send_sector_analysis(req: EmailRequest, canonical: str) -> None:
    """Top-down sector report → numbered top/undervalued list; reply a number → deep report."""
    meta = sector_analysis.catalog()[canonical]
    log.info("running sector analysis '%s' (req from %s)", canonical, req.sender)
    _reply_text(req, f"📩 Got it — pulling the top-down read on {meta['emoji']} "
                     f"{meta['index'].replace('Nifty ', '')}: index trend + valuation vs its own "
                     f"history, who's accumulating it, and the best / cheapest names inside it "
                     f"(~1–2 min). The report will land in this thread.")
    con = connect()
    try:
        report = _screen_run(lambda: sector_brief.build_sector_report(con, canonical))
    finally:
        con.close()
    if not report:
        _reply_text(req, f"Couldn't build that sector this time (timed out or no index data) — "
                         f"please resend `sector: {canonical}` in a moment.")
        return
    body = report["markdown"]
    cands = [_MenuItem(p["symbol"], p["name"]) for p in report["picks"]]  # plain SYM → reply → deep report
    _set_pending(req, f"sector:{canonical}", cands)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, f"Sector — {report['sector_name']}"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sector analysis '%s' (%d picks) to %s", canonical, len(cands), req.sender)


def _send_tailwind(req: EmailRequest) -> None:
    """💨 Tailwind — global supply/policy shocks → verified Indian beneficiaries; reply → deep report.
    Serves the 24h cache by default; `tailwind --latest` (or `fresh`) forces a live re-scan."""
    latest = _wants_latest(req.subject)
    log.info("running Tailwind (req from %s, latest=%s)", req.sender, latest)
    _reply_text(req, "📩 Got it — pulling the latest global supply-shock scan and the Indian names "
                     "that benefit. Forcing a fresh live scan (~2–4 min); it lands in this thread."
                     if latest else
                     "📩 Got it — fetching the global supply-shock scan (cached for 24h, so this is "
                     "quick unless it's stale; add `--latest` to force a fresh live scan). Lands in "
                     "this thread shortly.")
    con = connect()
    try:
        rep = _screen_run(lambda: tailwind_brief.build_tailwind_report(con, use_cache=not latest),
                          timeout=420)
    finally:
        con.close()
    if not rep:
        _reply_text(req, "No clean global supply-shock → Indian-beneficiary setup surfaced right now "
                         "(nothing fresh crossed the bar, or no verifiable listed name). That's a "
                         "valid answer — I don't force one. Try again in a day or two.")
        return
    body = rep["markdown"]
    if rep.get("from_cache"):                              # note that this is a reused (not live) scan
        stamp = ""
        try:
            ca = datetime.fromisoformat(rep["cached_at"]).astimezone(IST)
            stamp = f" from {ca:%d-%b %H:%M}"
        except Exception:  # noqa: BLE001
            pass
        body = (f"> ♻️ _Cached scan{stamp} (within 24h — same data as the last run). Reply "
                f"`tailwind --latest` for a fresh live scan._\n\n" + body)
    _set_pending(req, "tailwind", [_MenuItem(p["symbol"], p["name"]) for p in rep["picks"]])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Tailwind"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent Tailwind (%d catalysts, %d picks, cache=%s) to %s",
             rep["n_catalysts"], len(rep["picks"]), rep.get("from_cache", False), req.sender)


def _send_suppliers(req: EmailRequest, query: str) -> None:
    """Map ONE company's smaller listed suppliers/ancillaries → numbered list; reply → deep report."""
    cand = resolve(query)
    if not cand:
        _reply_text(req, f"Couldn't resolve '{query}' to an NSE company — try the exact name or symbol.")
        return
    target = cand[0]
    log.info("running supplier map for %s (req from %s)", target.symbol, req.sender)
    _reply_text(req, f"📩 Got it — mapping the smaller **listed** suppliers / ancillaries feeding "
                     f"{target.name} ({target.symbol}). ~1 min; the list lands in this thread.")
    con = connect()
    try:
        sup = _screen_run(lambda: supply_chain.suppliers_for_company(con, target.symbol, target.name))
    finally:
        con.close()
    if not sup:
        _reply_text(req, f"No **listed** suppliers/ancillaries surfaced for {target.name} that I could "
                         f"verify on NSE. (This maps listed vendors only — many suppliers are private.)")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Supplies", "Source"],
        [[i, r["symbol"], r["name"][:28], (r.get("role") or "")[:40],
          "🖐️ curated" if r["source"] == "curated" else "🤖 AI"] for i, r in enumerate(sup, 1)],
        align="rllll")
    body = (f"**🔗 Supply chain — {target.name} ({target.symbol})**\n\n"
            "Smaller **listed** companies that supply / make components for it — the *indirect* "
            "beneficiaries beyond the marquee name. 🖐️ hand-curated · 🤖 **AI-suggested, verify** the "
            "link before acting. **Reply a number for that stock's full deep report.**\n\n" + table +
            f"\n\n_Listed vendors only — a discovery aid, not a confirmed supplier ledger. "
            f"(Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, f"suppliers:{target.symbol}", [_MenuItem(r["symbol"], r["name"]) for r in sup])
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, f"Suppliers — {target.symbol}"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent supplier map for %s (%d names) to %s", target.symbol, len(sup), req.sender)


def _send_booking_risk(req: EmailRequest) -> None:
    """Heads-up on YOUR holdings where institutions sit on big gains (elevated selling risk)."""
    log.info("running portfolio booking-risk (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — checking your holdings for where the tracked institutions are "
                     "sitting on big gains (profit-booking risk). Lands in this thread shortly.")
    con = connect()
    try:
        res = _screen_run(lambda: booking_risk.portfolio_booking_risk(con, "holding"))
    finally:
        con.close()
    if res is None:
        _reply_text(req, "That scan timed out this time — please resend `booking` shortly.")
        return
    scored, nodata = res["scored"], res["nodata"]
    if not scored:
        _reply_text(req, "None of your holdings have enough shareholding history yet to estimate "
                         "an institutional cost. (Deepens as SHP history is ingested.)")
        return

    def _risk(r):
        return f"{r['emoji']} {r['avg_gain']:+.0f}%"

    def _who(r):
        t = r.get("top_holder")
        return (f"{t['name']} {t['gain_pct']:+.0f}%" if t else "—")
    rows = [[i, r["symbol"], r["name"], _risk(r), str(r["n_high"]), _who(r)]
            for i, r in enumerate(scored, 1)]
    table = _md_table(["#", "Symbol", "Company", "Instns' avg gain", "≥50% holders", "Deepest holder"],
                      rows, align="rlllrl")
    high = [r for r in scored if r["avg_gain"] >= 50]
    lead = (f"**{len(high)} of your {len(scored)} scored holdings** have institutions sitting on "
            f"**≥50% gains** — watch those for profit-booking pressure." if high
            else "None of your holdings show institutions on big (≥50%) gains right now.")
    body = ("**⚠️ Profit-booking risk — your holdings**\n\n" + lead + " Ranked by the stake-weighted "
            "gain the **tracked institutions** are sitting on (inferred from the price zones of the "
            "quarters they added in — exact prices aren't disclosed). Highest = deepest in profit = "
            "most likely to book. **Reply a number for that stock's deep report.**\n\n" + table)
    if nodata:
        body += ("\n\n_No institutional cost estimate yet for: "
                 + ", ".join(r["symbol"] for r in nodata[:20])
                 + " (held before our data / thin shareholding history — deepens as more is ingested)._")
    body += ("\n\n_A heads-up, not a sell signal — a strong stock can keep running past institutional "
             f"cost. Decision support; the call is yours. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in scored] + \
            [_MenuItem(r["symbol"], r["name"]) for r in nodata]
    _set_pending(req, "booking", cands)
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body, "Booking risk — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent booking-risk (%d scored, %d nodata) to %s", len(scored), len(nodata), req.sender)


def _send_smallcap_screen(req: EmailRequest) -> None:
    """Capex-led small-cap discovery screen → a numbered list; reply a number → deep report."""
    log.info("running small-cap screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — hunting strong small-caps (₹1,000–10,000 cr) led by the "
                     "**capex cycle**, with traps gated out. ~1–2 min; the ranked list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: smallcap.smallcap_screen(con, limit=20))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The small-cap screen timed out this time — please resend `screen: smallcap`.")
        return
    if not rows:
        _reply_text(req, "No small-caps scored yet — the small-cap universe's financials may not be "
                         "ingested. Run `backfill_universe.py --seed-smallcaps --only-missing` first.")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Score", "M-cap", "Capex", "Why"],
        [[i, r["symbol"], r["name"][:26], f"{r['composite']:.1f}", f"{r['mcap']:,.0f}",
          (f"{r['capex_growth']:.1f}×" if r.get("capex_growth") else "—"), r["why"]]
         for i, r in enumerate(rows, 1)],
        align="rllrrll")
    md = ("**🚀 Small-cap capex-cycle screen — ₹1,000–10,000 cr**\n\n"
          "Composite 0-100: **30% capex cycle** (capex vs its 3y base · capex÷depr · self-funded) · "
          "25% capital efficiency (ROCE & trend) · 20% cash/balance-sheet · 15% forensic · "
          "10% smart-money — with near-distress / manipulation / heavy-pledge / shrinking-revenue "
          "names **gated out**. Valuation shown for context, not scored. "
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n"
          f"_The screen finds; your reply diligences. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "screen:smallcap", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — small-cap capex"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent small-cap screen (%d names) to %s", len(rows), req.sender)


_TA_LEGEND = (
    "\n\n---\n\n**📖 What the terms mean** (plain English — this screen is chart-based, so here's the jargon)\n\n"
    "- **Uptrend (>200-DMA):** price is above its average of the last 200 days — the long-term trend is up. "
    "**50>200:** the 50-day average sits above the 200-day one (a 'golden cross') — momentum backs the trend.\n"
    "- **RS vs Nifty (relative strength):** how much the stock beat or lagged the Nifty index over ~3 months. "
    "'+40%' = it outran the market by 40%.\n"
    "- **MACD+:** a popular momentum indicator that's turned positive — recent momentum is bullish.\n"
    "- **RSI:** a 0–100 'how stretched' gauge. ~40–60 is healthy; **>70 = overbought** (stretched — risky to chase).\n"
    "- **% below 52w high:** how far under its 1-year peak it is. Near 0% = knocking on a breakout.\n"
    "- **Delivery spike:** an unusually high share of volume was actually *delivered* (bought to hold, not day-traded) "
    "— a sign of real conviction.\n"
    "- **Buy zone (support):** the price range to buy **on a small dip** — the nearest *support* (a level buyers have "
    "defended before). Don't chase above it; wait for the pullback.\n"
    "- **Stop:** where you sell if you're wrong (just below support) — it caps the loss. "
    "**Target (resistance):** the next *resistance* (a level sellers have defended) — where to consider booking profit.\n"
    "- **Reward:risk (R:R):** potential profit ÷ potential loss. '2.4:1' = aiming to make ₹2.40 for every ₹1 at risk. "
    "Higher is better — we only label it **accumulate** at ≥1.5:1.\n"
    "- **Setup type:** *accumulate* = clean buy-the-dip (R:R ≥ 1.5) · *breakout* = at new highs with no ceiling above, "
    "so trail a stop instead of a fixed target · *watch* = the shape is there but reward:risk is thin, so wait for a "
    "better entry.\n\n"
    "_A **candidate finder with defined risk**, not a promise — it hands you an entry, a stop and a target so your "
    "downside is always capped. Reply a number to get the full company report before you buy._"
)


def _send_technical_screen(req: EmailRequest) -> None:
    """Technical-setup discovery screen → the strongest chart setups to buy, market-wide, each
    with an entry zone / stop / target / reward:risk. Reply a number → that name's deep report."""
    log.info("running technical screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning the liquid universe for the strongest **technical setups** "
                     "(trend · relative strength · momentum), trap-gated, with entry/stop/target. "
                     "~1 min; the ranked list lands in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: technical_screen.technical_screen(con, limit=15))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The technical screen timed out this time — please resend `screen: technical`.")
        return
    if not rows:
        _reply_text(req, "No clean setups cleared the liquidity + safety gate today — the universe's "
                         "financials may not be ingested yet (run `backfill_universe.py`), or the "
                         "tape simply has no strong, un-extended setups right now.")
        return

    def _rng(lo, hi):
        return f"₹{lo:,.0f}–₹{hi:,.0f}" if lo is not None else "—"
    tbl = []
    for i, r in enumerate(rows, 1):
        tgt = f"₹{r['target']:,.0f}" if r["target"] else ("trail" if r["kind"] == "breakout" else "—")
        rr = f"{r['rr']:.1f}:1" if r["rr"] else "—"
        tbl.append([i, r["symbol"], r["name"][:18], r["kind"], f"₹{r['price']:,.0f}",
                    _rng(r["entry_lo"], r["entry_hi"]), f"₹{r['stop']:,.0f}", tgt, rr, r["why"]])
    table = _md_table(
        ["#", "Symbol", "Company", "Setup", "Price", "Buy zone", "Stop", "Target", "R:R", "Why"],
        tbl, align="rlllrrrrll")
    md = ("**📈 Technical setups — strongest charts to buy (market-wide)**\n\n"
          "Ranked on **price action**: 30% trend (>200-DMA · 50>200) · 25% relative strength vs "
          "Nifty · 15% MACD · 10% RSI-health · 10% breakout proximity · 10% delivery. Only liquid "
          "names (≥₹2 cr/day) that **clear a trap gate** (no Altman-distress / Beneish-manipulator / "
          "heavy pledge) and sit near a **buyable** support surface. **Buy zone** = a pullback to the "
          "nearest support; **stop** below it; **target** = next resistance (`trail` = breakout, no "
          "overhead). `accumulate` R:R≥1.5 · `breakout` blue-sky · `watch` = thin R:R. "
          "**Reply a number for that name's full deep report before you act.**\n\n"
          + table + "\n\n"
          "_Candidate finder with **defined risk**, not a back-tested edge — short-term timing is the "
          "least-proven part of the tool. Bounded to symbols with financials ingested (so the safety "
          f"gate is real); coverage grows with the backfill. (Reply within {PENDING_TTL_H}h.)_"
          + _TA_LEGEND)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "screen:technical", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — technical setups"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent technical screen (%d names) to %s", len(rows), req.sender)


def _send_policy_screen(req: EmailRequest) -> None:
    """Government policy / scheme radar — schemes in the latest PIB (primary) releases, with the
    sector(s) they hit and likely listed beneficiaries (watchlist names flagged). Standalone
    screen; no effect on reports/watchlist/digests."""
    log.info("running policy radar (req from %s)", req.sender)
    _reply_text(req, "🏛️ Got it — scanning the latest **government press releases (PIB, primary "
                     "source)** for new schemes/policies and mapping each to the sectors and "
                     "listed companies it affects. ~1 min; the list lands here.")
    con = connect()
    try:
        rows = _screen_run(lambda: policy.policy_scan(con, limit_releases=120), timeout=300)
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The policy radar timed out this time — please resend `screen: policy`.")
        return
    if not rows:
        _reply_text(req, "No market-relevant government schemes in the latest PIB releases right "
                         "now — try again later (the feed refreshes through the day).")
        return
    parts = ["## 🏛️ Government policy radar",
             "_Schemes & policies from **primary government press releases (PIB)** that move a "
             "**listed sector** — often at the **announced / cabinet-approved / draft / "
             "consultation** stage, before formal launch. Most **watchlist-relevant first**; "
             "⭐ = a name you hold/track. A discovery screen — it defers to each stock's own "
             "fundamentals, so reply with any symbol for its full deep report._"]
    for i, s in enumerate(rows, 1):
        parts.append("---")
        tag_bits = []
        if s.get("ministry"):
            tag_bits.append(f"🏛️ **{s['ministry']}**")
        if s.get("stage"):
            tag_bits.append(f"📅 _{s['stage']}_")
        if s.get("confidence"):
            tag_bits.append(f"🎯 _{s['confidence']} confidence_")
        parts.append(f"### {i}. {s['scheme']}")
        if tag_bits:
            parts.append(" · ".join(tag_bits))
        if s.get("sectors"):
            parts.append("🧭 **Sectors:** " + ", ".join(f"**{x}**" for x in s["sectors"])
                         + (f"  ·  ⚙️ **Mechanism:** {s['mechanism']}" if s.get("mechanism") else ""))
        if s.get("what_it_is"):
            parts.append(f"📄 **What it is:** {s['what_it_is']}")
        if s.get("benefit"):
            parts.append(f"💡 **Why it matters:** {s['benefit']}")
        bens = s.get("beneficiaries") or []
        listed = [b for b in bens if b["symbol"]]
        others = [b for b in bens if not b["symbol"]]
        if listed:
            parts.append("🎯 **Likely beneficiaries (NSE-listed):**")
            lines = []
            for b in listed[:12]:
                star = " ⭐" if b["on_watchlist"] else ""
                why = f" — {b['why']}" if b.get("why") else ""
                lines.append(f"- **{b['name']}** ({b['symbol']}){star}{why}")
            parts.append("\n".join(lines))
        if others:
            parts.append("_Also flagged (not matched to an NSE symbol): _"
                         + ", ".join(b["name"] for b in others[:8]))
        parts.append(f"🔗 _Source: PIB — {s['link']}_")
    md = "\n\n".join(parts)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Policy radar"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent policy radar (%d schemes) to %s", len(rows), req.sender)


def _send_holdco_screen(req: EmailRequest) -> None:
    """Holdco-discount screen → listed holders whose stake NAV exceeds their own market cap."""
    log.info("running holdco screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — scanning for holding companies trading below the value of their "
                     "listed stakes (the Elcid pattern). ~1 min; the ranked list will land here.")
    con = connect()
    try:
        rows = _screen_run(lambda: holdco.holdco_discounts(con, limit=20))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The holdco scan timed out — please resend `screen: holdco` in a moment.")
        return
    if not rows:
        _reply_text(req, "No holdcos surfaced yet — this needs SHP ingested across the universe. "
                         "Run the one-time `backfill_universe.py` (Nifty-500 + known holdcos) to seed it.")
        return
    tbl_rows = []
    for i, r in enumerate(rows, 1):
        disc = f"{r['discount_pct']:+.0f}%" if r["discount_pct"] is not None else "n/a"
        top = ", ".join(f"{inv} {pct:.1f}%" for inv, _nm, pct, _v in r["top_stakes"][:3])
        tbl_rows.append([i, r["holder"], disc, _crore(r["own_mcap_cr"]),
                         _crore(r["stake_nav_cr"]), top])
    table = _md_table(
        ["#", "Holdco", "Discount", "Own mcap", "Stake NAV", "Top listed stakes"],
        tbl_rows, align="rlrrrl")
    md = ("**🏦 Holdco discounts — listed stake NAV vs own market cap** (the Elcid trade, generalised)\n\n"
          "**Reply with a number for that holding company's full deep report.**\n\n"
          + table + "\n\n"
          "_Discount = 1 − own market cap ÷ stake NAV. Counts only **disclosed listed** stakes "
          "(SHP promoter + public >1% tables); unlisted subsidiaries aren't valued. Coverage grows "
          f"with SHP ingested. (Reply within {PENDING_TTL_H}h.)_")
    cands = [_MenuItem(r["holder"], r["holder_name"]) for r in rows]
    _set_pending(req, "screen:holdco", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — holdco"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent holdco screen (%d names) to %s", len(rows), req.sender)


_SELL_LEGEND = (
    "\n\n---\n\n**📖 What the terms mean** (plain English — skip if you know them)\n\n"
    "- **Keep score (0-100):** how strong a *hold* each stock is — higher = keep, lower = sell first. "
    "It blends the five things below, each judged **only against your own holdings** (so it ranks *your* book).\n"
    "- **DCF upside / overvalued:** our estimate of what the business is worth vs its current price. "
    "'~20% upside' = looks about 20% cheap; '~90% overvalued' = the price is far above what it seems worth.\n"
    "- **Cheaper than X% of own history:** where today's valuation sits vs how expensive/cheap *this* stock "
    "usually is. 'Cheaper than 75% of its own history' = unusually cheap for it.\n"
    "- **Piotroski (quality):** a 0–9 financial-health score (profit, debt, efficiency). 8–9 strong, 0–2 weak.\n"
    "- **Forensic (0–4):** accounting-safety score — higher = cleaner books (no distress / manipulation / pledge flags).\n"
    "- **Momentum vs Nifty:** how the price did against the market over ~3 months. 'lags −15%' = 15% worse than the index.\n"
    "- **Smart-money flow:** whether big institutions (mutual funds, insurers, foreign funds) added or trimmed last "
    "quarter. 'trimming' = they're reducing their stake.\n"
    "- **Verdict:** 🔴 sell candidate (weakest of your book) · 🟡 trim if needed · 🟢 keep (strongest) · "
    "⚪ no data (not analysed yet — email the symbol once)."
)


def _send_sell_advisor(req: EmailRequest) -> None:
    """Sell-priority ranking of the user's holdings (Version A — merit only, no cost/tax yet):
    weakest hand first, so if you need cash you sell from the top down. Reply a number → that
    holding's full deep report before acting."""
    log.info("running sell advisor (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — ranking your holdings by **which to sell first** on merit "
                     "(valuation headroom, quality, forensic, momentum, smart-money). "
                     "~1–2 min; the ranked list lands in this thread.")
    con = connect()
    try:
        rows = _screen_run(lambda: sell_advisor.sell_ranking(con))
    finally:
        con.close()
    if rows is None:
        _reply_text(req, "The sell ranking timed out this time — please resend `sell` in a moment.")
        return
    if not rows:
        _reply_text(req, "No holdings tagged yet — add stocks to your watchlist as 'holding' first, "
                         "then resend `sell`.")
        return
    table = _md_table(
        ["#", "Symbol", "Company", "Keep", "Verdict", "Why"],
        [[i, r["symbol"], r["name"][:24],
          (f"{r['keep_score']:.0f}" if r["keep_score"] is not None else "—"),
          r["verdict"], r["why"]]
         for i, r in enumerate(rows, 1)],
        align="rlllll")
    md = ("**💰 Which to sell first — your holdings, ranked**\n\n"
          "If you need cash, sell from the **top** (weakest hand) down. **Keep score 0-100** "
          "(higher = stronger hold): 35% valuation headroom (DCF upside + cheap-vs-own-history) · "
          "25% quality (Piotroski) · 20% forensic · 10% momentum vs Nifty · 10% smart-money flow — "
          "each ranked **within your own book**. "
          "**Reply with a number for that holding's full deep report before you act.**\n\n"
          + table + "\n\n"
          "_Merit only — this doesn't yet know your cost, P&L or tax (that's the next version). "
          f"Decision support; the call is yours. (Reply within {PENDING_TTL_H}h.)_"
          + _SELL_LEGEND)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in rows]
    _set_pending(req, "sell", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Sell-priority — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent sell advisor (%d holdings) to %s", len(rows), req.sender)


_INVESTOR_CAVEAT = ("_Tracks the SHP public/promoter tables — only holders **disclosed by "
                    "name** are visible (public stakes below ~1% aren't filed at all), and "
                    "coverage is bounded by the SHP universe ingested so far. A drop out of the "
                    "list can mean a full exit **or** trimming below the ~1% disclosure floor._")


def _send_investor_screen(req: EmailRequest) -> None:
    """What every tracked marquee investor did last quarter (entered/exited/added/trimmed),
    across the SHP data → numbered list of the stocks they moved; reply → deep report."""
    log.info("running investor-moves screen (req from %s)", req.sender)
    _reply_text(req, "📩 Got it — checking what the tracked marquee investors did last quarter "
                     "across the shareholding data. Lands in this thread shortly.")
    con = connect()
    try:
        moves = _screen_run(lambda: investors.all_moves(con))
    finally:
        con.close()
    if moves is None:
        _reply_text(req, "The investor scan timed out — please resend `screen: investors` shortly.")
        return
    if not moves:
        _reply_text(req, "No tracked investor showed a disclosed move ≥0.5pp last quarter across "
                         "the ingested SHP universe.\n\n" + _INVESTOR_CAVEAT)
        return
    sym_arrow = {"entered": "🟢 new", "exited": "🔴 exit", "added": "➕ add", "trimmed": "➖ trim"}
    tbl_rows, cands, n = [], [], 0
    for canon in investors.roster():
        m = moves.get(canon)
        if not m:
            continue
        for kind in ("entered", "added", "trimmed", "exited"):
            for r in m[kind]:
                n += 1
                delta = (f"{r['delta']:+.2f}pp" if "delta" in r
                         else (f"{r['pct']:.2f}%" if kind == "entered" else f"was {r['prev_pct']:.2f}%"))
                tbl_rows.append([n, canon, sym_arrow[kind], r["symbol"], delta, r["name"][:28]])
                cands.append(_MenuItem(r["symbol"], r["name"]))
    table = _md_table(["#", "Investor", "Move", "Symbol", "Δ / stake", "Company"],
                      tbl_rows, align="rllrrl")
    md = (f"**👤 Marquee-investor moves — last disclosed quarter** ({len(moves)} of "
          f"{len(investors.roster())} tracked names moved)\n\n"
          "**Reply with a number for that stock's full deep report.**\n\n"
          + table + "\n\n" + _INVESTOR_CAVEAT + f"\n\n_(Reply within {PENDING_TTL_H}h.)_")
    _set_pending(req, "screen:investors", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, "Screen — investors"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent investor-moves screen (%d moves, %d investors) to %s", n, len(moves), req.sender)


def _send_investor(name: str, req: EmailRequest) -> None:
    """One marquee investor's current book + last-quarter moves → numbered holdings;
    reply → deep report on any of them."""
    canon = investors.resolve(name)
    if not canon:
        roster = ", ".join(investors.roster()[:8])
        _reply_text(req, f"'{name}' isn't a tracked investor. Try one of: {roster}… "
                         "or `screen: investors` for everyone's latest moves.")
        return
    log.info("running investor book for %s (req from %s)", canon, req.sender)
    _reply_text(req, f"📩 Got it — pulling **{canon}**'s disclosed holdings and last-quarter "
                     "moves from the shareholding data.")
    con = connect()
    try:
        book = _screen_run(lambda: investors.holdings(con, canon))
        mv = _screen_run(lambda: investors.moves(con, canon))
    finally:
        con.close()
    if not book:
        _reply_text(req, f"No disclosed (≥~1%) holdings found for **{canon}** in the ingested "
                         "SHP universe yet.\n\n" + _INVESTOR_CAVEAT)
        return
    def _cost_cell(r):
        if r.get("gain_pct") is not None:
            avg = r.get("cost_avg")
            return f"{r['cost_emoji']} {r['gain_pct']:+.0f}%" + (f" (~₹{avg:,.0f})" if avg else "")
        if r.get("zone_lo") is not None:              # held since before our data
            return "⚪ pre-data"
        return "—"
    table = _md_table(["#", "Symbol", "Company", "Stake", "Now vs their cost", "As of"],
                      [[i, r["symbol"], r["name"], f"{r['pct']:.2f}%", _cost_cell(r),
                        f"{r['as_of']:%b-%Y}"] for i, r in enumerate(book, 1)], align="rlllrr")
    parts = [f"**👤 {canon} — disclosed holdings** ({len(book)} names ≥~1%)\n\n"
             "**Reply with a number for that stock's full deep report.**\n\n" + table
             + "\n\n_**Now vs their cost** = current price vs the price zone of the quarters "
             "**this investor** added in (inferred — exact prices aren't disclosed). 🟢 near cost · "
             "🟡 in profit · 🟠🔴 large gains → watch for profit-booking; ⚪ pre-data = held since "
             "before our data (cost unknown). Deepens as more shareholding history is ingested._"]
    if mv and (mv["entered"] or mv["exited"] or mv["added"] or mv["trimmed"]):
        def _fmt(items, kind):
            return ", ".join(
                (f"{r['symbol']} ({r['delta']:+.2f}pp)" if "delta" in r
                 else (f"{r['symbol']} ({r['pct']:.2f}%)" if kind == "entered"
                       else f"{r['symbol']} (was {r['prev_pct']:.2f}%)")) for r in items)
        mv_lines = ["", "**Last-quarter moves:**"]
        if mv["entered"]:
            mv_lines.append(f"- 🟢 **New:** {_fmt(mv['entered'], 'entered')}")
        if mv["added"]:
            mv_lines.append(f"- ➕ **Added:** {_fmt(mv['added'], 'added')}")
        if mv["trimmed"]:
            mv_lines.append(f"- ➖ **Trimmed:** {_fmt(mv['trimmed'], 'trimmed')}")
        if mv["exited"]:
            mv_lines.append(f"- 🔴 **Exited / below disclosure:** {_fmt(mv['exited'], 'exited')}")
        parts.append("\n".join(mv_lines))
    parts.append("\n" + _INVESTOR_CAVEAT + f"\n\n_(Reply within {PENDING_TTL_H}h.)_")
    md = "\n\n".join(parts)
    cands = [_MenuItem(r["symbol"], r["name"]) for r in book]
    _set_pending(req, f"investor:{canon}", cands)
    emailer.send_report(_re_subject(req.subject), md, to=req.sender,
                        html=emailer.body_html(md, f"{canon} — holdings"),
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent investor book for %s (%d holdings) to %s", canon, len(book), req.sender)


# ----------------- fund (mutual-fund) reports -----------------
class _FundCand:
    """Minimal Candidate shim so fund matches reuse the pending/disambiguation UX."""
    def __init__(self, scheme_code: int, name: str) -> None:
        self.symbol = f"MF:{scheme_code}"
        self.name = name


def _fund_query(subject: str) -> str | None:
    """Return the fund name if the subject is a fund request ('fund: X' / 'mf: X'), else None."""
    m = re.match(r"^\s*(?:re:\s*)?(?:fund|mf)\s*[:\-]\s*(.+)$", subject, flags=re.I)
    return m.group(1).strip() if m else None


def _fund_pdf(con, scheme_code: int, report_md: str, name: str) -> bytes | None:
    """Charted fund-report PDF (NAV growth + rolling-returns), best-effort with a
    HARD timeout — the report is already in the body, so never block the send."""
    try:
        images = charts.fund_charts(con, scheme_code)
    except Exception:  # noqa: BLE001
        log.exception("fund charts failed for scheme %s", scheme_code)
        images = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, name, images).result(timeout=150)
    except Exception:  # noqa: BLE001
        log.exception("fund PDF failed/timed out for scheme %s — body-only", scheme_code)
        return None
    finally:
        ex.shutdown(wait=False)


def _send_fund_report(scheme_code: int, req: EmailRequest, name: str) -> None:
    log.info("generating fund report for scheme %s (req from %s)", scheme_code, req.sender)
    _reply_text(req, f"Got it — pulling the fund report for **{name}** (fetching NAV history). "
                     "One moment…")
    con = connect()
    try:
        md = fund_brief.build_fund_brief(con, scheme_code)
        if not md:
            _reply_text(req, f"Couldn't build a report for '{name}' — no NAV history found.")
            return
        thesis = fund_thesis(md, name)              # qualitative read + verdict (best-effort)
        if thesis:
            md = f"{md}\n\n{'=' * 60}\n## Analysis\n\n{thesis}"
        pdf = _fund_pdf(con, scheme_code, md, name)  # PDF carries the thesis too
    finally:
        con.close()
    body = md
    attachments = [("Mutual_fund_metrics_guide.pdf", glossary.fund_guide_pdf())]
    if pdf:
        today = datetime.now(IST).date().isoformat()
        attachments.insert(0, (f"{name[:40].strip()}_{today}.pdf", pdf))
    else:
        body += "\n\n_(The charted PDF couldn't be generated this time — the full report is above.)_"
    emailer.send_report(_re_subject(req.subject), body, to=req.sender,
                        html=emailer.body_html(body), attachments=attachments,
                        in_reply_to=req.message_id, references=req.references or req.message_id)
    log.info("sent fund report (scheme %s) to %s", scheme_code, req.sender)


def _handle_fund(query: str, req: EmailRequest) -> None:
    con = connect()
    try:
        cands = fund_brief.resolve_fund(con, query)
    finally:
        con.close()
    if not cands:
        _reply_text(req, f"Couldn't find a fund matching '{query}'. Try the fuller name, "
                         "e.g. 'fund: Parag Parikh Flexi Cap'.")
    elif len(cands) == 1:
        _send_fund_report(cands[0][0], req, cands[0][1])
    else:
        _set_pending(req, query, [_FundCand(c, n) for c, n in cands])
        _send_choices(query, [_FundCand(c, n) for c, n in cands], req)


# ----------------- request handling -----------------
def handle_request(req: EmailRequest) -> None:
    basis = _basis(req.subject)                 # consolidated / standalone / auto (from the subject)
    # 1) is this a numbered reply to a pending "which one?" / deeper-cut menu? Resolve it
    #    against the menu armed IN THIS THREAD (thread-scoped), never a stale one from another.
    found = _find_pending(req)
    sel = _selection(req.body) if req.body and len(req.body.strip()) <= 4 else None
    if found and sel is not None and 1 <= sel <= len(found[1]):
        key, cands = found
        symbol, name = cands[sel - 1]
        if str(symbol).startswith("GT:"):       # deeper-cut menu: growth triggers (listed)
            _send_growth_triggers(symbol[3:], req, name)   # keep the menu armed for other cuts
            return
        if str(symbol).startswith("IGT:"):      # deeper-cut menu: growth triggers (IPO)
            _send_growth_triggers(symbol[4:], req, name, ipo_mode=True)
            return
        if str(symbol).startswith("MF:"):       # a fund choice
            _send_fund_report(int(symbol[3:]), req, name)
        elif str(symbol).startswith("IPO:"):    # an IPO choice (from the ipo list)
            _send_ipo_report(symbol[4:], req, name)
        else:
            _send_report(symbol, req, resolved_name=name, consolidated=basis)
        # after a successful send (a crash must not eat the reply) — and surgical, so the
        # deeper-cut menu the handler just armed under this thread key survives
        _clear_consumed(key, cands)
        return

    # a numbered reply that matched no live menu must NEVER fall through to the subject
    # parsers — a thread subject that inherited 'ipo:'/'fund:' via 'Re:' would turn the
    # bare number into a garbage lookup. Say what happened instead.
    if sel is not None and re.match(r"^\s*re:", req.subject or "", flags=re.I):
        if found:
            _reply_text(req, f"That menu has {len(found[1])} option(s) — reply with a "
                             f"number between 1 and {len(found[1])}.")
        else:
            _reply_text(req, "There's no active menu in this thread any more (menus expire "
                             "after 24h or after being used). Send a fresh request — a company "
                             "name, `fund: <name>`, or `ipo: ongoing`.")
        return

    # 1a) help / command menu ('help', 'commands', 'menu', '?')
    if _help_query(req.subject):
        _send_help(req)
        return

    # 1b) explicit fund request ('fund: <name>' / 'mf: <name>')
    fq = _fund_query(req.subject)
    if fq:
        _handle_fund(fq, req)
        return

    # 1c) explicit IPO request ('ipo: ongoing' / 'ipo: upcoming' / 'ipo: <name>')
    iq = _ipo_query(req.subject)
    if iq:
        _handle_ipo(iq[0], iq[1], req)
        return

    # 1d) explicit marquee-investor book ('investor: <name>' / 'hni: <name>')
    nq = _investor_query(req.subject)
    if nq:
        _send_investor(nq, req)
        return

    # 1e) explicit screener ('screen: value' / 'holdco' / 'investors' / 'smallcap' / bare)
    sq = _screen_query(req.subject)
    if sq:
        if sq == "holdco":
            _send_holdco_screen(req)
        elif sq == "investors":
            _send_investor_screen(req)
        elif sq == "smallcap":
            _send_smallcap_screen(req)
        elif sq == "policy":
            _send_policy_screen(req)
        elif sq == "technical":
            _send_technical_screen(req)
        else:
            _send_fundamental_screen(req)
        return

    # 1e-tri) explicit sector analysis ('sector: defence' / 'sector: pharma' / 'sector: list')
    secq = _sector_query(req.subject)
    if secq:
        if secq.lower() in ("list", "help", "?", "options", "sectors"):
            _send_sector_list(req)
            return
        if secq.lower() in ("rotation", "rotate", "rotations", "overview", "all", "compare"):
            _send_sector_rotation(req)
            return
        canon = sector_analysis.resolve_sector(secq)
        if canon:
            _send_sector_analysis(req, canon)
        else:
            names = ", ".join(sector_analysis.catalog().keys())
            _reply_text(req, f"I don't recognise the sector '{secq}'. Try `sector: list` to see the "
                             f"options, or one of: {names}.")
        return

    # 1e-quater) supply-chain map ('suppliers: BEL' / 'supply chain: HAL' / 'ancillaries: <co>')
    supq = _suppliers_query(req.subject)
    if supq:
        _send_suppliers(req, supq)
        return

    # 1e-quinque) portfolio profit-booking risk ('booking' / 'booking risk' / 'profit booking')
    if _booking_query(req.subject):
        _send_booking_risk(req)
        return

    # 1e-bis) bare government policy / scheme radar ('policy:', 'schemes', 'policy radar')
    if _policy_query(req.subject):
        _send_policy_screen(req)
        return

    # 1e-sext) 💨 Tailwind — global supply-shock → Indian beneficiaries ('tailwind', 'catalysts')
    if _tailwind_query(req.subject):
        _send_tailwind(req)
        return

    # 1f) explicit technical levels ('levels: <name>' / 'technical: <name>' / 'setup:' / 'chart:')
    lq = _levels_query(req.subject)
    if lq:
        _send_levels(lq, req)
        return

    # 1g) holdings sell-priority ranking ('sell' / 'raise' / 'trim') — which to sell first if
    #     you need cash. Bare word, so it must sit before the free-text stock-name fallback.
    if _sell_query(req.subject):
        _send_sell_advisor(req)
        return

    # 2) fresh query from the subject. Ack IMMEDIATELY at pickup — symbol resolution can
    #    take minutes, and a silent gap reads as "the bot is dead" and provokes resends.
    query = _clean_query(req.subject)
    if not query:
        _reply_text(req, "Send a company name in the Subject line, e.g. 'Adani Power'.")
        return
    _reply_text(req, f"📩 Got it — resolving **{query}** and building the deep report. "
                     "This takes a few minutes; everything will land in this thread.")
    try:
        cands = resolve(query)
    except Exception:  # noqa: BLE001
        log.exception("resolve failed for %r", query)
        _reply_text(req, f"Couldn't look up '{query}' right now — please try again.")
        return
    if not cands:
        _reply_text(req, f"Couldn't resolve '{query}' to an NSE symbol. Try the exact name.")
    elif len(cands) == 1:
        _send_report(cands[0].symbol, req, resolved_name=cands[0].name, consolidated=basis,
                     ack=False)                      # already acked at pickup
    else:
        _set_pending(req, query, cands)
        _send_choices(query, cands, req)


# ----------------- watchlist push (self-healing daily) -----------------
def _push_digest(sr: "scan.ScanResult") -> bool:
    """Daily digest: upcoming events + per-stock movers + events (deals / corporate
    events / forensic changes, with inline filing analysis), by company name. No
    PDFs — reply with a name for the full report. Returns True if a digest was sent."""
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send digest")
        return False
    if not sr.results and not sr.movers and not sr.upcoming:
        log.info("nothing to report — no digest email sent")
        return False
    today = datetime.now(IST).date().isoformat()
    md = scan.format_digest(today, sr)
    emailer.send_report(f"📊 Watchlist — {today}", md, to=to,
                        html=emailer.body_html(md, "Watchlist"))
    log.info("digest sent to %s (%d movers, %d event-symbols, %d upcoming)",
             to, len(sr.movers), len(sr.results), len(sr.upcoming))
    return True


def _push_intraday(sr: "scan.IntradayResult") -> bool:
    """Midday same-day digest: live movers + today's filings/insider. Returns True if sent."""
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send intraday digest")
        return False
    if not sr.movers and not sr.filings and not sr.insider:
        log.info("intraday: nothing to report — no email sent")
        return False
    md = scan.format_intraday_digest(sr)
    hhmm = (sr.asof or datetime.now(IST)).strftime("%H:%M")
    emailer.send_report(f"🔔 Watchlist — same-day ({hhmm})", md, to=to,
                        html=emailer.body_html(md, "Watchlist — same-day"))
    log.info("intraday digest sent to %s (%d movers, %d filings, %d insider)",
             to, len(sr.movers), len(sr.filings), len(sr.insider))
    return True


def maybe_intraday() -> None:
    """Fire the midday same-day digest once per trading day, in the 12:30–14:00 IST window."""
    now = datetime.now(IST)
    if not (INTRADAY_HOUR, INTRADAY_MIN) <= (now.hour, now.minute) or now.hour >= INTRADAY_CUTOFF_HOUR:
        return
    if scan.already_intraday_today() or not scan.market_open_today():
        return
    log.info("midday intraday digest firing")
    try:
        sr = scan.run_intraday_scan()
    except Exception:  # noqa: BLE001
        log.exception("intraday scan failed")
        return                                  # no mark → retried next heartbeat (still in window)
    _push_intraday(sr)
    scan.mark_intraday()


def _push_premarket(md_text: str) -> bool:
    """Deliver the pre-market digest. Returns True if sent."""
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send pre-market digest")
        return False
    today = datetime.now(IST).date()
    emailer.send_report(f"🌅 Pre-market — {today:%d-%b-%Y}", md_text, to=to,
                        html=emailer.body_html(md_text, "Pre-market"))
    log.info("pre-market digest sent to %s", to)
    return True


def maybe_premarket() -> None:
    """Fire the pre-open GIFT Nifty digest once per trading day, in the 08:30–09:00 IST window."""
    now = datetime.now(IST)
    if not (PREMARKET_HOUR, PREMARKET_MIN) <= (now.hour, now.minute) or now.hour >= PREMARKET_CUTOFF_HOUR:
        return
    if scan.already_premarket_today() or not scan.market_open_today():
        return
    log.info("pre-market digest firing")
    con = connect()
    try:
        md_text = premarket.build_premarket(con)
    except Exception:  # noqa: BLE001
        log.exception("pre-market build failed")
        return                                  # no mark → retried next heartbeat (still in window)
    finally:
        con.close()
    if md_text is None:
        log.info("pre-market: no data to report — no email sent")
        scan.mark_premarket()                   # nothing to fetch today; don't retry all morning
        return
    _push_premarket(md_text)
    scan.mark_premarket()


def maybe_scan() -> None:
    """Fire the watchlist scan once per trading day, first heartbeat at/after 18:00 IST."""
    now = datetime.now(IST)
    if now.hour < SCAN_HOUR:
        return
    if scan.already_scanned_today():
        return
    if not scan.market_open_today():
        scan.mark_scanned()
        log.info("market closed today — skipping scan (marked done)")
        return
    log.info("self-healing daily scan firing")
    try:
        sr = scan.run_watchlist_scan()
    except Exception:  # noqa: BLE001
        log.exception("scan failed")
        return                                  # no mark, no commit → retried next heartbeat
    if _push_digest(sr):
        scan.commit_scan_state(sr)              # advance dedup markers ONLY after delivery
    scan.mark_scanned()


def _push_screen_digest(md_text: str) -> bool:
    """Deliver the weekly screener-movements digest. Returns True if sent."""
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send screen digest")
        return False
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"📡 Screener movements — {today}", md_text, to=to,
                        html=emailer.body_html(md_text, "Screener movements"))
    log.info("screen digest sent to %s", to)
    return True


def maybe_screen_digest() -> None:
    """Fire the weekly trigger-based screener digest once per ISO week (Saturday ≥18:00 IST):
    holdco / fundamental / investor deltas vs the last run. No email if nothing crossed a
    threshold. Fingerprints advance ONLY after a successful send, so a delivery failure
    re-surfaces the same deltas next time rather than eating them."""
    now = datetime.now(IST)
    if now.weekday() != 5 or now.hour < SCAN_HOUR:      # Saturday evening, weekly
        return
    if not screen_digest.due_this_week():
        return
    log.info("weekly screen digest firing")
    con = connect()
    try:
        delta = screen_digest.build_screen_delta(con)
        md_text = screen_digest.format_screen_digest(delta)
        if md_text is None:
            screen_digest.commit_screen_state(con, delta)   # nothing triggered — mark week done
            log.info("screen digest: no triggers this week")
            return
        if _push_screen_digest(md_text):
            screen_digest.commit_screen_state(con, delta)   # advance fingerprints ONLY after send
    except Exception:  # noqa: BLE001
        log.exception("screen digest failed")               # no commit → retried next heartbeat
    finally:
        con.close()


def maybe_sector_rotation() -> None:
    """Fire the weekly sector-rotation push once per ISO week (Saturday ≥18:00 IST): all sectors
    ranked by relative strength vs Nifty + valuation vs their own history — leaders, laggards, and
    value-turning candidates. Reads the latest EOD, so a weekend fire is fine. The week-marker
    advances only after a successful send."""
    now = datetime.now(IST)
    if now.weekday() != 5 or now.hour < SCAN_HOUR:      # Saturday evening, weekly
        return
    if not scan.sector_rotation_due():
        return
    log.info("weekly sector-rotation push firing")
    con = connect()
    try:
        body = sector_brief.build_sector_rotation(con)
    except Exception:  # noqa: BLE001
        log.exception("sector rotation failed")             # no mark → retried next heartbeat
        return
    finally:
        con.close()
    if not body:
        scan.mark_sector_rotation()                         # nothing ranked — don't retry all evening
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send sector rotation")
        return
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"🔄 Sector rotation — {today}", body, to=to,
                        html=emailer.body_html(body, "Sector rotation"))
    scan.mark_sector_rotation()                             # advance week-marker ONLY after send
    log.info("weekly sector-rotation push sent to %s", to)


def maybe_tailwind() -> None:
    """Fire the weekly 💨 Tailwind push once per ISO week (Saturday ≥18:00 IST): scout global
    policy/supply shocks → verified Indian beneficiaries. Runs the full 4-tier agent pipeline
    (news scout → analyst → grounded mapper → auditor), so it's slow (~2–4 min) but weekly. No
    email if nothing genuine surfaced; the week-marker advances only after a successful send (or a
    clean empty result), so a delivery failure re-surfaces it next heartbeat."""
    now = datetime.now(IST)
    if now.weekday() != 5 or now.hour < SCAN_HOUR:          # Saturday evening, weekly
        return
    if not scan.tailwind_due():
        return
    log.info("weekly Tailwind push firing")
    con = connect()
    try:
        rep = tailwind_brief.build_tailwind_report(con)
    except Exception:  # noqa: BLE001
        log.exception("tailwind build failed")              # no mark → retried next heartbeat
        return
    finally:
        con.close()
    if not rep:
        scan.mark_tailwind()                                # nothing genuine this week — don't retry
        log.info("tailwind: nothing surfaced this week — no email")
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send Tailwind")
        return
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"💨 Tailwind — {today}", rep["markdown"], to=to,
                        html=emailer.body_html(rep["markdown"], "Tailwind"))
    scan.mark_tailwind()                                    # advance week-marker ONLY after send
    scan.add_tailwind_seen(rep.get("keys", []))            # so the mid-week urgent alert won't repeat these
    log.info("weekly Tailwind push sent to %s (%d catalysts)", to, rep["n_catalysts"])


def maybe_tailwind_urgent() -> None:
    """Mid-week urgent break-in: on a trading day (Mon–Fri) ≥18:00 IST, once/day, run the lighter
    Tailwind pass and — only if a FRESH, high-severity shock with a verified Indian beneficiary just
    landed — send a compact '💨 Fresh supply shock' email alongside the daily digest. Saturday is
    skipped (the full weekly push covers it). Marks done each evening whether or not it sent, so the
    ~1–2 min pipeline runs at most once per trading day; the seen-set stops it repeating a shock."""
    now = datetime.now(IST)
    if now.weekday() == 5 or now.hour < SCAN_HOUR:          # Sat → weekly handles it; else evening only
        return
    if scan.already_tailwind_urgent_today() or not scan.market_open_today():
        return
    log.info("mid-week urgent Tailwind check firing")
    con = connect()
    try:
        seen = scan.tailwind_seen_keys(con)
        rep = tailwind_brief.build_tailwind_urgent(con, seen)
    except Exception:  # noqa: BLE001
        log.exception("urgent tailwind build failed")       # no mark → retried next heartbeat
        return
    finally:
        con.close()
    scan.mark_tailwind_urgent()                             # ran today — don't re-run all evening
    if not rep:
        return                                              # quiet day — the common, correct outcome
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send urgent Tailwind")
        return
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"💨 Fresh supply shock — {today}", rep["markdown"], to=to,
                        html=emailer.body_html(rep["markdown"], "Fresh supply shock"))
    scan.add_tailwind_seen(rep.get("keys", []))            # don't re-alert this shock the rest of the week
    log.info("urgent Tailwind break-in sent to %s (%d catalysts)", to, rep["n_catalysts"])


# ----------------- main loop -----------------
def main() -> None:
    channels = os.environ.get("CHANNELS", "email").lower()
    if "email" not in channels:
        log.info("email channel disabled (CHANNELS=%s) — exiting", channels)
        return
    if not ALLOWED:
        log.error("EMAIL_ALLOWED_SENDERS is empty — refusing to start (no auth allowlist)")
        sys.exit(1)
    for key in ("IMAP_USER", "IMAP_PASS", "SMTP_USER", "SMTP_PASS"):
        if not os.environ.get(key):
            log.error("missing required env var %s — refusing to start", key)
            sys.exit(1)

    log.info("email bot starting — allowlist=%s, scan>=%02d:00 IST", sorted(ALLOWED), SCAN_HOUR)
    while True:  # reconnect loop
        inbox = Inbox()
        try:
            inbox.connect()
            log.info("IMAP connected (%s) — waiting for mail via IDLE", inbox.user)
            while True:
                # Drain FIRST, every cycle — IDLE only reduces latency, it is NOT the source
                # of truth. While a report is generating (minutes) the bot isn't in IDLE, and
                # Gmail's IDLE only reports mail that arrives *during* its wait window; so any
                # request sent while busy (or one IDLE simply misses) would otherwise wait for
                # a *later* email to nudge it. An unconditional drain each loop guarantees every
                # UNSEEN request is picked up within one cycle — no more "send it 3-4 times".
                _drain(inbox)
                maybe_premarket()    # heartbeat: pre-open GIFT Nifty digest (08:30–09:00 IST)
                maybe_intraday()     # heartbeat: midday same-day digest (12:30–14:00 IST)
                maybe_scan()         # heartbeat: full digest, fires at most once/day ≥18:00
                maybe_screen_digest()  # heartbeat: weekly screener-movements digest (Sat ≥18:00)
                maybe_sector_rotation()  # heartbeat: weekly sector-rotation push (Sat ≥18:00)
                maybe_tailwind()     # heartbeat: weekly global supply-shock → beneficiaries (Sat ≥18:00)
                maybe_tailwind_urgent()  # heartbeat: mid-week urgent break-in on a fresh big shock (Mon–Fri ≥18:00)
                inbox.wait(timeout=IDLE_TIMEOUT)   # then sleep in IDLE until a nudge / timeout
        except Exception:  # noqa: BLE001 — connection dropped / IDLE expired
            log.exception("inbox session error — reconnecting in 15s")
        finally:
            inbox.logout()
        time.sleep(15)


def _dedupe(reqs: list) -> tuple[list, list]:
    """Collapse identical requests (same sender + subject + body) to one — IMAP/Gmail
    occasionally serves a message twice, or the user double-sends. Returns
    (unique_requests, duplicate_uids); the dupes are marked seen but not processed."""
    unique, dupe_uids, keys = [], [], set()
    for r in reqs:
        key = ((r.sender or "").strip().lower(), (r.subject or "").strip().lower(),
               " ".join((r.body or "").split())[:300])
        if key in keys:
            dupe_uids.append(r.uid)
            continue
        keys.add(key)
        unique.append(r)
    return unique, dupe_uids


def _drain(inbox: Inbox) -> None:
    """Handle every pending request from allowlisted senders, then mark them seen."""
    reqs = inbox.fetch_requests(ALLOWED)
    if not reqs:
        return
    reqs, dupe_uids = _dedupe(reqs)
    log.info("got %d request(s)%s: %s", len(reqs),
             f" ({len(dupe_uids)} duplicate(s) skipped)" if dupe_uids else "",
             [r.subject for r in reqs])
    if dupe_uids:
        inbox.mark_seen(dupe_uids)              # drop the dupes without re-processing
    for req in reqs:
        try:
            handle_request(req)
        except Exception:  # noqa: BLE001 — one bad request shouldn't kill the loop
            log.exception("failed handling request from %s", req.sender)
        inbox.mark_seen([req.uid])


if __name__ == "__main__":
    main()
