"""Email bot for the equity-research workbench — the always-on delivery channel.

resolve -> deep report -> PDF, plus the self-healing watchlist scan and all the
discovery engines, delivered over email:

  PULL  you email a stock name (Subject) from an allowlisted address ->
        IMAP IDLE wakes the bot -> it resolves, builds the deep report, and
        replies in-thread with formatted HTML + the PDF attached. Ambiguous
        names get a numbered "which one?" reply; you reply with the number.
  PUSH  once per trading day at/after 18:00 IST it runs the watchlist scan and
        emails a digest (with deep-report PDFs for any 'results filed' event).

``handle_request`` is channel-agnostic: every reply goes through ``emailer.send_report`` to
``req.sender``, so the CLI / web UI drive the same commands by registering a local delivery
sink for their own sender address (see ``reports.email.register_local_sink``).

Run the bot via run_email_bot.ps1 (or ``eqr bot``).
"""

from __future__ import annotations

import os
import re
import sys
import time

import duckdb

from equity_research import config
from equity_research.analysis import (
    sector_analysis,
)
from equity_research.bot.core import (
    ALLOWED,
    IDLE_TIMEOUT,
    SCAN_HOUR,
    _basis,
    _clean_query,
    _clear_consumed,
    _find_pending,
    _inr,
    _MenuItem,
    _reply_text,
    _screen_run,
    _selection,
    _set_pending,
    _track_basket,
    log,
    setup_logging,
)
from equity_research.bot.deliver import (
    _handle_fund,
    _handle_ipo,
    _send_choices,
    _send_fund_report,
    _send_ipo_report,
    _send_levels,
    _send_report,
    _send_upside_drivers,
)
from equity_research.bot.help import _HELP_SECTIONS, _send_help
from equity_research.bot.personal import (
    _handle_alert,
    _send_raise_plan,
    _send_reality_check,
    _send_scorecard,
    _send_sell_advisor,
    _send_thesis,
    maybe_alert_scan,
    maybe_scorecard,
    maybe_thesis_sweep,
)
from equity_research.bot.pushes import (
    maybe_bse_prices,
    maybe_call_radar,
    maybe_concall_ingest,
    maybe_former_refresh,
    maybe_intraday,
    maybe_mail_housekeeping,
    maybe_pickaxe,
    maybe_premarket,
    maybe_results,
    maybe_results_ingest,
    maybe_scan,
    maybe_screen_digest,
    maybe_sector_rotation,
    maybe_tailwind,
    maybe_tailwind_urgent,
)
from equity_research.bot.queries import (
    _alert_query,
    _booking_query,
    _calls_query,
    _fund_query,
    _help_query,
    _hotlist_query,
    _investor_query,
    _ipo_query,
    _levels_query,
    _pickaxe_query,
    _policy_query,
    _raise_amount,
    _reality_query,
    _results_query,
    _scorecard_query,
    _screen_query,
    _sector_query,
    _sell_query,
    _suppliers_query,
    _tailwind_query,
    _thesis_query,
)
from equity_research.bot.screens import (
    _send_beaters_screen,
    _send_booking_risk,
    _send_call_radar,
    _send_deleverage_screen,
    _send_fundamental_screen,
    _send_holdco_screen,
    _send_hotlist,
    _send_institutions_screen,
    _send_investor,
    _send_investor_screen,
    _send_margins_screen,
    _send_pickaxe,
    _send_policy_screen,
    _send_quality_screen,
    _send_results,
    _send_sector_analysis,
    _send_sector_list,
    _send_sector_rotation,
    _send_smallcap_screen,
    _send_suppliers,
    _send_tailwind,
    _send_technical_screen,
    _send_volume_screen,
)
from equity_research.common.db import connect
from equity_research.reports.inbox import EmailRequest, Inbox
from equity_research.reports.resolve import resolve


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
        if str(symbol).startswith("UD:"):       # deeper-cut menu: upside drivers (listed)
            _send_upside_drivers(symbol[3:], req, name)   # keep the menu armed for other cuts
            return
        if str(symbol).startswith("IUD:"):      # deeper-cut menu: upside drivers (IPO)
            _send_upside_drivers(symbol[4:], req, name, ipo_mode=True)
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
        screens = {"holdco": _send_holdco_screen, "investors": _send_investor_screen,
                   "smallcap": _send_smallcap_screen, "policy": _send_policy_screen,
                   "technical": _send_technical_screen, "volume": _send_volume_screen,
                   "beaters": _send_beaters_screen, "institutions": _send_institutions_screen,
                   "hotlist": _send_hotlist, "margins": _send_margins_screen,
                   "deleverage": _send_deleverage_screen, "quality": _send_quality_screen,
                   "value": _send_fundamental_screen}
        if sq in screens:
            screens[sq](req)
        else:
            _reply_text(req, "🔎 There's no screen by that name. Try one of: "
                             + ", ".join(f"`screen: {n}`" for n in screens) + ".")
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

    # 1e-sept) ⛏️ Pickaxe — surging Indian demand → indirect beneficiary ('pickaxe', 'demand')
    if _pickaxe_query(req.subject):
        _send_pickaxe(req)
        return

    # 1e-oct) 🔥 Hotlist — multi-signal confluence across the discovery engines ('hotlist')
    if _hotlist_query(req.subject):
        _send_hotlist(req)
        return

    # 1e-nov-t) 🛡️ Thesis Guard — why you own it, re-checked ('thesis: X — reasons', 'theses', 'unthesis: X')
    tq = _thesis_query(req.subject)
    if tq:
        _send_thesis(req, *tq)
        return

    # 1e-nov-a) 🔍 Reality Check — is that post / article true ('reality check: <link or text>')
    raw = _reality_query(req)
    if raw:
        _send_reality_check(req, raw)
        return

    # 1e-nov) 📊 Scorecard — the track record of every call ('scorecard', 'track record')
    if _scorecard_query(req.subject):
        _send_scorecard(req)
        return

    # 1e-non) 🎙️ Concalls — notable earnings calls (tone vs delivery) ('calls', 'concall')
    if _calls_query(req.subject):
        _send_call_radar(req)
        return

    # 1e-dec) 📈 Results Radar — strongest just-reported quarters ('results', 'movers')
    if _results_query(req.subject):
        _send_results(req)
        return

    # 1e-undec) 🔔 Announcements — manage standing keyword alerts ('alert: X', 'alerts', 'unalert: X')
    alertq = _alert_query(req.subject)
    if alertq:
        _handle_alert(req, alertq[0], alertq[1])
        return

    # 1f) explicit technical levels ('levels: <name>' / 'technical: <name>' / 'setup:' / 'chart:')
    lq = _levels_query(req.subject)
    if lq:
        _send_levels(lq, req)
        return

    # 1g) holdings sell-priority ranking ('sell' / 'raise' / 'trim') — which to sell first if
    #     you need cash. Bare word, so it must sit before the free-text stock-name fallback.
    amount = _raise_amount(req.subject)
    if amount:
        _send_raise_plan(req, amount)
        return
    if _sell_query(req.subject):
        _send_sell_advisor(req)
        return

    # 2) fresh query from the subject. Ack IMMEDIATELY at pickup — symbol resolution can
    #    take minutes, and a silent gap reads as "the bot is dead" and provokes resends.
    query = _clean_query(req.subject)
    if not query:
        _reply_text(req, "Send a company name in the Subject line, e.g. 'Infosys'.")
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


# ----------------- main loop -----------------
_EMAIL_KEYS = ("IMAP_USER", "IMAP_PASS", "SMTP_USER", "SMTP_PASS")


def email_configured() -> bool:
    """Is the email channel set up (allowlist + IMAP/SMTP credentials)?"""
    return bool(ALLOWED) and all(os.environ.get(k) for k in _EMAIL_KEYS)


def main(web_ui: bool | None = None) -> None:
    """The always-on bot. It also hosts the web UI (``WEB_UI_ENABLED``, or ``web_ui=True`` from
    ``eqr serve``) in a background thread, so the bot, the UI and CLI jobs share one process —
    and one DuckDB writer."""
    setup_logging()
    if not ALLOWED:
        log.error("EMAIL_ALLOWED_SENDERS is empty — refusing to start (no auth allowlist)")
        sys.exit(1)
    for key in _EMAIL_KEYS:
        if not os.environ.get(key):
            log.error("missing required env var %s — refusing to start", key)
            sys.exit(1)

    if config.WEB_UI_ENABLED if web_ui is None else web_ui:
        from equity_research.web import server as web_server
        web_server.start_background()
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
                # Each scheduled push is gated by its config.ENABLE_* flag (all default on); the
                # on-demand email commands stay available regardless. Disable a push in .env to skip it.
                _drain(inbox)
                if config.ENABLE_PREMARKET:
                    maybe_premarket()    # pre-open GIFT Nifty digest
                if config.ENABLE_MIDDAY:
                    maybe_intraday()     # midday same-day digest
                if config.ENABLE_EOD_DIGEST:
                    maybe_scan()         # full daily digest (once/day ≥ EOD_HOUR)
                if config.ENABLE_SCREEN_DIGEST:
                    maybe_screen_digest()  # weekly screener-movements digest
                if config.ENABLE_SECTOR_ROTATION:
                    maybe_sector_rotation()  # weekly sector-rotation push
                if config.ENABLE_TAILWIND:
                    maybe_tailwind()         # weekly global supply-shock → beneficiaries
                    maybe_tailwind_urgent()  # urgent break-in on a fresh shock (per URGENT_SLOTS)
                if config.ENABLE_PICKAXE:
                    maybe_pickaxe()      # monthly surging-demand → indirect beneficiaries
                if config.ENABLE_CONCALLS:
                    maybe_concall_ingest()   # incremental earnings-call scoring (background)
                    maybe_call_radar()       # weekly 🎙️ Concalls push
                if config.ENABLE_RESULTS_RADAR:
                    maybe_results_ingest()   # refresh just-reported names' financials (background)
                    maybe_results()          # weekly results-radar push
                if config.ENABLE_KEYWORD_ALERTS:
                    maybe_alert_scan()   # keyword filing-alert sweep (background)
                if config.ENABLE_SCORECARD_PUSH:
                    maybe_scorecard()        # weekly track-record email
                if config.ENABLE_THESIS_GUARD:
                    maybe_thesis_sweep()     # evening re-check of your theses (background)
                maybe_former_refresh()       # weekly: merged companies, ETFs / SME / REITs / InvITs, BSE-only
                maybe_bse_prices()           # daily: closes of BSE-only shares you hold (background)
                if config.ENABLE_MAIL_HOUSEKEEPING:
                    maybe_mail_housekeeping()  # bin processed workbench mail on this server account
                inbox.wait(timeout=IDLE_TIMEOUT)   # then sleep in IDLE until a nudge / timeout
        except Exception:  # noqa: BLE001 — connection dropped / IDLE expired
            log.exception("inbox session error — reconnecting in %ds", config.RECONNECT_BACKOFF_S)
        finally:
            inbox.logout()
        time.sleep(config.RECONNECT_BACKOFF_S)


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
        except duckdb.IOException:
            # The DB is held by another process (the `eqr` CLI mid-report, a backfill). Leave the
            # email UNSEEN so the next cycle retries it, instead of silently dropping the request.
            n = _db_busy_retries[req.uid] = _db_busy_retries.get(req.uid, 0) + 1
            if n < _DB_BUSY_MAX_RETRIES:
                log.warning("database busy — will retry request %r (attempt %d/%d)",
                            req.subject, n, _DB_BUSY_MAX_RETRIES)
                continue
            log.error("database still busy after %d attempts — giving up on %r", n, req.subject)
            _db_busy_retries.pop(req.uid, None)
            try:
                _reply_text(req, "⚠️ The database was busy (another job was using it) and I couldn't "
                                 "process this after several tries. Please send it again in a few minutes.")
            except Exception:  # noqa: BLE001
                log.exception("busy-notice send failed for %s", req.sender)
        except Exception:  # noqa: BLE001 — one bad request shouldn't kill the loop
            log.exception("failed handling request from %s", req.sender)
        _db_busy_retries.pop(req.uid, None)
        inbox.mark_seen([req.uid])


# Requests that hit a busy DB (another process holds the single-writer lock) are retried on later
# drain cycles; after this many attempts the sender is told to resend. Keyed by IMAP uid.
_DB_BUSY_MAX_RETRIES = 5
_db_busy_retries: dict[int, int] = {}


# Names other modules / tests reach for — kept importable from here.
__all__ = [
    "ALLOWED",
    "EmailRequest",
    "_HELP_SECTIONS",
    "_MenuItem",
    "_find_pending",
    "_inr",
    "_raise_amount",
    "_reality_query",
    "_reply_text",
    "_screen_run",
    "_set_pending",
    "_thesis_query",
    "_track_basket",
    "connect",
    "email_configured",
    "handle_request",
    "main",
    "maybe_premarket",
]

if __name__ == "__main__":
    main()
