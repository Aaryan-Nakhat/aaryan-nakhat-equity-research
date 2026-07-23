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
from equity_research.common.db import connect  # noqa: E402
from equity_research.reports import charts  # noqa: E402
from equity_research.reports import glossary  # noqa: E402
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


def _clear_pending(key: str) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM alert_state WHERE symbol='__email__' AND key=?", [key])
    finally:
        con.close()


# ----------------- helpers -----------------
def _re_subject(subject: str, suffix: str = "") -> str:
    s = subject.strip()
    if not s.lower().startswith("re:"):
        s = f"Re: {s}"
    return f"{s}{suffix}"


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
        _re_subject(req.subject, " — want a deeper cut?"),
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
        _re_subject(req.subject, " — growth triggers"),
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
    emailer.send_report(_re_subject(req.subject, f" — {kind} IPOs"), md, to=req.sender,
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
        _re_subject(req.subject, " — IPO analysis"), body, to=req.sender,
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
        _re_subject(req.subject, " — which one?"),
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
        _clear_pending(key)     # only after a successful send — a crash must not eat the reply
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
                maybe_intraday()     # heartbeat: midday same-day digest (12:30–14:00 IST)
                maybe_scan()         # heartbeat: full digest, fires at most once/day ≥18:00
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
