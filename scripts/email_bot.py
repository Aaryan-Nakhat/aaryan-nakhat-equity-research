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
from equity_research.reports.pipeline import generate_report  # noqa: E402
from equity_research.reports.resolve import resolve  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SCAN_HOUR = 18
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
def _set_pending(sender: str, query: str, cands: list) -> None:
    con = connect()
    try:
        payload = json.dumps({"query": query, "ts": datetime.now(timezone.utc).isoformat(),
                              "cands": [[c.symbol, c.name] for c in cands]})
        con.execute("INSERT OR REPLACE INTO alert_state(symbol, key, value, updated_at) "
                    "VALUES ('__email__', ?, ?, now())", [f"pending:{sender}", payload])
    finally:
        con.close()


def _get_pending(sender: str) -> list | None:
    con = connect()
    try:
        r = con.execute("SELECT value FROM alert_state WHERE symbol='__email__' AND key=?",
                        [f"pending:{sender}"]).fetchone()
    finally:
        con.close()
    if not r:
        return None
    data = json.loads(r[0])
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(data["ts"])).total_seconds() / 3600
    return data["cands"] if age_h <= PENDING_TTL_H else None


def _clear_pending(sender: str) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM alert_state WHERE symbol='__email__' AND key=?",
                    [f"pending:{sender}"])
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


def _send_report(symbol: str, req: EmailRequest, resolved_name: str | None = None,
                 consolidated: bool | None = None) -> None:
    log.info("generating report for %s (req from %s, basis=%s)", symbol, req.sender,
             {True: "consolidated", False: "standalone"}.get(consolidated, "auto"))
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


# ----------------- request handling -----------------
def handle_request(req: EmailRequest) -> None:
    basis = _basis(req.subject)                 # consolidated / standalone / auto (from the subject)
    # 1) is this a numbered reply to a pending "which one?" question?
    pending = _get_pending(req.sender)
    sel = _selection(req.body) if req.body and len(req.body.strip()) <= 4 else None
    if pending and sel is not None and 1 <= sel <= len(pending):
        symbol, name = pending[sel - 1]
        _clear_pending(req.sender)
        _send_report(symbol, req, resolved_name=name, consolidated=basis)
        return

    # 2) fresh query from the subject
    query = _clean_query(req.subject)
    if not query:
        _reply_text(req, "Send a company name in the Subject line, e.g. 'Adani Power'.")
        return
    try:
        cands = resolve(query)
    except Exception:  # noqa: BLE001
        log.exception("resolve failed for %r", query)
        _reply_text(req, f"Couldn't look up '{query}' right now — please try again.")
        return
    if not cands:
        _reply_text(req, f"Couldn't resolve '{query}' to an NSE symbol. Try the exact name.")
    elif len(cands) == 1:
        _send_report(cands[0].symbol, req, resolved_name=cands[0].name, consolidated=basis)
    else:
        _set_pending(req.sender, query, cands)
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
            _drain(inbox)            # catch anything that arrived while we were down
            while True:
                activity = inbox.wait(timeout=IDLE_TIMEOUT)
                if activity:
                    _drain(inbox)
                maybe_scan()         # heartbeat: fires at most once/day
        except Exception:  # noqa: BLE001 — connection dropped / IDLE expired
            log.exception("inbox session error — reconnecting in 15s")
        finally:
            inbox.logout()
        time.sleep(15)


def _drain(inbox: Inbox) -> None:
    """Handle every pending request from allowlisted senders, then mark them seen."""
    reqs = inbox.fetch_requests(ALLOWED)
    if reqs:
        log.info("got %d request(s): %s", len(reqs), [r.subject for r in reqs])
    for req in reqs:
        try:
            handle_request(req)
        except Exception:  # noqa: BLE001 — one bad request shouldn't kill the loop
            log.exception("failed handling request from %s", req.sender)
        inbox.mark_seen([req.uid])


if __name__ == "__main__":
    main()
