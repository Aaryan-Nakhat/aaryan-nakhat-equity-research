"""Delivering reports: the deep report + PDF, the Upside Drivers one-pager, IPO notes, levels and fund reports."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime

from equity_research import config
from equity_research.analysis import (
    technical,
    track_record,
)
from equity_research.bot.core import (
    IST,
    PENDING_TTL_H,
    _MenuItem,
    _needs_llm,
    _re_subject,
    _reply_text,
    _set_pending,
    log,
)
from equity_research.bot.queries import _FundCand
from equity_research.common.db import connect
from equity_research.reports import (
    charts,
    deep_brief,
    fund_brief,
    glossary,
)
from equity_research.reports import email as emailer
from equity_research.reports.inbox import EmailRequest
from equity_research.reports.pdf import report_to_pdf
from equity_research.reports.pipeline import (
    generate_ipo_report,
    generate_report,
    generate_upside_drivers,
)
from equity_research.reports.resolve import resolve
from equity_research.reports.synthesize import fund_thesis
from equity_research.scrapers import ipo


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


def _pdf_with_charts(symbol: str, report_md: str) -> tuple[bytes | None, list]:
    """Full report PDF with the fundamental charts embedded — best-effort with a
    HARD timeout. The PDF (Playwright Chromium) can hang on a busy box; the full
    report is already in the email body, so on timeout/failure we return None and
    deliver body-only rather than blocking the whole send forever. Returns (pdf, chart images)."""
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
        pdf = ex.submit(report_to_pdf, report_md, symbol, images).result(timeout=config.PDF_RENDER_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — timeout or render failure
        log.exception("report PDF generation failed/timed out for %s — sending body-only", symbol)
        pdf = None
    finally:
        ex.shutdown(wait=False)            # don't block on a hung render thread
    return pdf, images


def _set_followup(req: EmailRequest, symbol: str, name: str | None, *, ipo_mode: bool = False) -> None:
    """Arm the numbered follow-up menu in THIS thread so a bare-number reply maps back
    to a deeper cut for ``symbol`` (24h TTL, via the thread-scoped pending state). ``ipo_mode``
    tags the upside-drivers item as IPO (``IUD:`` — grounded in the offer docs)."""
    tag = "IUD" if ipo_mode else "UD"
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
          "  **1) Upside Drivers 1-pager** — forward-looking catalysts, each quantified, "
          f"timeline-tagged and rated Secured / In Progress / Aspirational certainty, grounded in {grounded}.\n\n"
          "_(More deeper cuts coming soon.)_")
    emailer.send_report(
        _re_subject(req.subject),
        md, to=req.sender, html=emailer.body_html(md, symbol),
        in_reply_to=req.message_id, references=req.references or req.message_id,
    )
    _set_followup(req, symbol, name, ipo_mode=ipo_mode)
    log.info("sent deeper-cut menu for %s to %s", symbol, req.sender)


def _report_memory(symbol: str) -> str | None:
    """'Last time we said…' for the top of a deep report (REPORT_MEMORY_ENABLED), or None."""
    con = connect()
    try:
        return track_record.memory_line(con, symbol)
    except Exception:  # noqa: BLE001
        log.exception("track record: memory line failed for %s", symbol)
        return None
    finally:
        con.close()


def _track_verdict(symbol: str, report_md: str, subject: str) -> None:
    """Log a deep report's closing verdict (an unreadable one is logged as REVIEW, never scored)."""
    if not track_record.enabled():
        return
    con = connect()
    try:
        label, stance = track_record.parse_verdict(report_md)
        track_record.log_call(con, "deep_report", symbol, stance, label, ref=subject)
    except Exception:  # noqa: BLE001
        log.exception("track record: couldn't log the verdict for %s", symbol)
    finally:
        con.close()


def _send_report(symbol: str, req: EmailRequest, resolved_name: str | None = None,
                 consolidated: bool | None = None, *, ack: bool = True) -> None:
    log.info("generating report for %s (req from %s, basis=%s)", symbol, req.sender,
             {True: "consolidated", False: "standalone"}.get(consolidated, "auto"))
    if ack:                                     # fresh queries pre-ack at pickup instead
        _ack(symbol, req, resolved_name)
    report_md = generate_report(symbol, deep=True, consolidated=consolidated)  # full report — body + PDF
    pdf, images = _pdf_with_charts(symbol, report_md)
    today = datetime.now(IST).date().isoformat()
    head = f"Report for **{symbol}**" + (f" — {resolved_name}" if resolved_name else "")
    memory = _report_memory(symbol)               # rendered for the reader; the LLM never saw it
    body = f"{head}\n\n" + (f"{memory}\n\n" if memory else "") + report_md
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
        images=images,
    )
    log.info("sent report for %s to %s", symbol, req.sender)
    _track_verdict(symbol, report_md, req.subject)
    _send_followup_menu(symbol, req, resolved_name)      # separate "want a deeper cut?" prompt


def _send_upside_drivers(symbol: str, req: EmailRequest, name: str | None = None,
                         *, ipo_mode: bool = False) -> None:
    """Upside-drivers 1-pager (opt-in deeper cut) — email body + PDF, in-thread. ``ipo_mode``
    grounds it in the IPO offer documents instead of listed filings."""
    if _needs_llm(req, "The Upside Drivers 1-pager"):
        return
    log.info("generating upside drivers for %s (req from %s, ipo=%s)", symbol, req.sender, ipo_mode)
    _reply_text(req, f"🚀 Building the upside-drivers 1-pager for **{symbol}**"
                     + (f" ({name})" if name else "") + " — ~1–2 min; it'll land in this thread.")
    md = generate_upside_drivers(symbol, ipo_mode=ipo_mode)
    if not md:
        src = "offer documents" if ipo_mode else "filings (concalls / presentations)"
        _reply_text(req, f"Couldn't build upside drivers for {symbol} — no {src} "
                         "were available to ground it.")
        return
    pdf = _text_pdf(md, f"{symbol} — Upside Drivers")
    today = datetime.now(IST).date().isoformat()
    head = f"Upside Drivers — **{symbol}**" + (f" — {name}" if name else "")
    body = f"{head}\n\n{md}"
    attachments = []
    if pdf:
        attachments.append((f"{symbol}_upside_drivers_{today}.pdf", pdf))
    else:
        body += "\n\n_(The PDF couldn't be generated this time — the full 1-pager is above.)_"
    emailer.send_report(
        _re_subject(req.subject),
        body,
        to=req.sender,
        html=emailer.body_html(body, f"{symbol} — upside drivers"),
        attachments=attachments,
        in_reply_to=req.message_id,
        references=req.references or req.message_id,
    )
    log.info("sent upside drivers for %s to %s", symbol, req.sender)


def _text_pdf(report_md: str, title: str) -> bytes | None:
    """Text-only PDF (no charts) for the deeper-cut / IPO notes — best-effort with a HARD
    timeout so a hung Chromium render never blocks; the note is already in the email body."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, title, []).result(timeout=config.PDF_RENDER_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — timeout or render failure
        log.exception("PDF failed/timed out for %r — sending body-only", title)
        return None
    finally:
        ex.shutdown(wait=False)


# ----------------- IPO (pre-listing) -----------------
def _ipo_list_safe(fn, *, timeout: int = config.PDF_RENDER_TIMEOUT_S):
    """Run a browser-tier IPO list fetch under a HARD timeout so a wedged stealth Chromium session
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
    """Pre-listing IPO note — email body + PDF, in-thread. No deeper-cut/upside-drivers
    follow-up for IPOs (the note already carries the forward view)."""
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
    # (No upside-drivers follow-up for IPOs — the IPO note already covers the forward
    # view; per the user's ask, don't prompt for a deeper cut here.)


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


def _levels_pdf(report_md: str, symbol: str, images: list) -> bytes | None:
    """Small PDF for a levels reply — the section tables + the annotated chart. Best-effort
    under a hard timeout (the body already carries the text)."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(report_to_pdf, report_md, f"{symbol} — Trading levels",
                         images).result(timeout=config.LEVELS_PDF_TIMEOUT_S)
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
        in_reply_to=req.message_id, references=req.references or req.message_id, images=images,
    )
    log.info("sent levels for %s to %s", symbol, req.sender)


def _fund_pdf(con, scheme_code: int, report_md: str, name: str) -> tuple[bytes | None, list]:
    """Charted fund-report PDF (NAV growth + rolling-returns), best-effort with a
    HARD timeout — the report is already in the body, so never block the send. Returns (pdf, charts)."""
    try:
        images = charts.fund_charts(con, scheme_code)
    except Exception:  # noqa: BLE001
        log.exception("fund charts failed for scheme %s", scheme_code)
        images = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        pdf = ex.submit(report_to_pdf, report_md, name, images).result(timeout=config.PDF_RENDER_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        log.exception("fund PDF failed/timed out for scheme %s — body-only", scheme_code)
        pdf = None
    finally:
        ex.shutdown(wait=False)
    return pdf, images


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
        pdf, images = _fund_pdf(con, scheme_code, md, name)  # PDF carries the thesis too
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
                        html=emailer.body_html(body), attachments=attachments, images=images,
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
