"""Scheduled pushes and background refreshes, each a `maybe_*` heartbeat hook gated by time, day and its own lock."""

from __future__ import annotations

import os
import threading
from datetime import datetime

from equity_research import config, mail_cleanup, scan, schedule, screen_digest
from equity_research.analysis import (
    call_radar,
    former_companies,
    results_radar,
)
from equity_research.bot.core import (
    ALLOWED,
    INTRADAY_CUTOFF_HOUR,
    INTRADAY_HOUR,
    INTRADAY_MIN,
    IST,
    MAIL_BIN_AFTER_MIN,
    MAIL_SWEEP_EVERY_MIN,
    PREMARKET_CUTOFF_HOUR,
    PREMARKET_HOUR,
    PREMARKET_MIN,
    SCAN_HOUR,
    URGENT_SLOTS,
    WEEKLY_PUSH_WEEKDAY,
    _track_push,
    log,
)
from equity_research.bot.screens import _pickaxe_lock, _pickaxe_worker
from equity_research.common import llm
from equity_research.common.db import connect
from equity_research.reports import (
    call_radar_brief,
    premarket,
    results_brief,
    sector_brief,
    tailwind_brief,
)
from equity_research.reports import email as emailer

_former_lock = threading.Lock()


def _former_worker() -> None:
    """Weekly: learn what can be held beyond today's NSE main board, so it can be found and entered —
    companies that stopped trading (names, merger dates), ETFs / SME / REITs / InvITs, BSE-only shares."""
    from equity_research.portfolio import instruments

    if not _former_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        for step in (former_companies.refresh, instruments.refresh_nse, instruments.refresh_bse):
            try:
                step(con)
            except Exception:  # noqa: BLE001 — one source down shouldn't stop the others
                log.exception("weekly instruments refresh: %s failed", step.__qualname__)
        scan._set_meta(con, "last_weekly_instruments", datetime.now(IST).date().isoformat())
    finally:
        con.close()
        _former_lock.release()


_bse_lock = threading.Lock()


def _bse_prices_worker() -> None:
    from equity_research.portfolio import instruments

    if not _bse_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        instruments.refresh_bse_prices(con)
        scan._set_meta(con, "last_bse_prices", datetime.now(IST).date().isoformat())
    except Exception:  # noqa: BLE001
        log.exception("BSE prices refresh failed")
    finally:
        con.close()
        _bse_lock.release()


def maybe_bse_prices() -> None:
    """Heartbeat hook: BSE-only closes once a day after the evening scan hour — and at once the first time, or
    when a newly added BSE-only holding has no price yet."""
    now = datetime.now(IST)
    if _bse_lock.locked():
        return
    con = connect()
    try:
        done = scan._meta(con, "last_bse_prices") == now.date().isoformat()
        listed = con.execute("SELECT count(*) FROM instruments WHERE kind IN ('bse', 'bse_suspended')").fetchone()[0]
        never = con.execute("SELECT count(*) FROM bse_prices").fetchone()[0] == 0
        unpriced = con.execute("""SELECT count(*) FROM holding_lots WHERE symbol LIKE 'BSE:%'
                                  AND symbol NOT IN (SELECT symbol FROM bse_prices)""").fetchone()[0]
    finally:
        con.close()
    # one small file a day (a holding can be BSE-only under an old NSE symbol, so don't gate on how it's stored)
    if listed and (never or unpriced or (not done and now.hour >= SCAN_HOUR)):
        threading.Thread(target=_bse_prices_worker, name="bse-prices", daemon=True).start()


def maybe_former_refresh() -> None:
    """Heartbeat hook: once a week (and on first start), in a background thread."""
    if _former_lock.locked():
        return
    con = connect()
    try:
        last = scan._meta(con, "last_weekly_instruments")
        no_codes = con.execute("SELECT count(*) FROM bse_codes").fetchone()[0] == 0 or con.execute(
            "SELECT count(*) FROM instruments WHERE kind = 'bse_suspended'").fetchone()[0] == 0   # first run of a new list
    finally:
        con.close()
    if last and not no_codes and (datetime.now(IST).date() - datetime.fromisoformat(last).date()).days < 7:
        return
    threading.Thread(target=_former_worker, name="former-companies", daemon=True).start()


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
    emailer.send_report(f"📡 Screener movements — {today}{schedule.catch_up_note(schedule.open_slot())}", md_text, to=to,
                        html=emailer.body_html(md_text, "Screener movements"))
    log.info("screen digest sent to %s", to)
    return True


def maybe_screen_digest() -> None:
    """Fire the weekly trigger-based screener digest once per week (Saturday ≥18:00 IST, or caught up within WEEKLY_CATCHUP_HOURS if missed):
    holdco / fundamental / investor deltas vs the last run. No email if nothing crossed a
    threshold. Fingerprints advance ONLY after a successful send, so a delivery failure
    re-surfaces the same deltas next time rather than eating them."""
    if not screen_digest.due_this_week():
        return
    log.info("weekly screen digest firing%s", schedule.catch_up_note(schedule.open_slot()))
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
    """Fire the weekly sector-rotation push once per week (Saturday ≥18:00 IST, or caught up within WEEKLY_CATCHUP_HOURS if missed): all sectors
    ranked by relative strength vs Nifty + valuation vs their own history — leaders, laggards, and
    value-turning candidates. Reads the latest EOD, so a weekend fire is fine. The week-marker
    advances only after a successful send."""
    if not scan.sector_rotation_due():
        return
    log.info("weekly sector-rotation push firing%s", schedule.catch_up_note(schedule.open_slot()))
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
    emailer.send_report(f"🔄 Sector rotation — {today}{schedule.catch_up_note(schedule.open_slot())}", body, to=to,
                        html=emailer.body_html(body, "Sector rotation"))
    scan.mark_sector_rotation()                             # advance week-marker ONLY after send
    log.info("weekly sector-rotation push sent to %s", to)


def maybe_tailwind() -> None:
    """Fire the weekly 💨 Tailwind push once per week (Saturday ≥18:00 IST, or caught up within WEEKLY_CATCHUP_HOURS if missed): scout global
    policy/supply shocks → verified Indian beneficiaries. Runs the full 4-tier agent pipeline
    (news scout → analyst → grounded mapper → auditor), so it's slow (~2–4 min) but weekly. No
    email if nothing genuine surfaced; the week-marker advances only after a successful send (or a
    clean empty result), so a delivery failure re-surfaces it next heartbeat."""
    if not llm.configured():                              # AI push — needs an LLM
        return
    if not scan.tailwind_due():
        return
    log.info("weekly Tailwind push firing%s", schedule.catch_up_note(schedule.open_slot()))
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
    emailer.send_report(f"💨 Tailwind — {today}{schedule.catch_up_note(schedule.open_slot())}", rep["markdown"], to=to,
                        html=emailer.body_html(rep["markdown"], "Tailwind"))
    scan.mark_tailwind()                                    # advance week-marker ONLY after send
    scan.add_tailwind_seen(rep.get("keys", []))            # so the mid-week urgent alert won't repeat these
    _track_push("tailwind", rep, "weekly Tailwind")
    log.info("weekly Tailwind push sent to %s (%d catalysts)", to, rep["n_catalysts"])


def _current_urgent_slot(now: datetime) -> str | None:
    """The urgent-Tailwind slot currently 'due' — the latest ``URGENT_SLOTS`` entry whose start time
    has passed today, or ``None`` before the first slot. Slots run in order, so this also gives free
    catch-up: if the laptop was asleep through pre-market and wakes mid-morning, 'premarket' is still
    the due slot and fires on the first heartbeat (until midday takes over)."""
    slot = None
    for h, m, name in URGENT_SLOTS:
        if (now.hour, now.minute) >= (h, m):
            slot = name
    return slot


def maybe_tailwind_urgent() -> None:
    """Urgent break-in: on a trading day (not the weekly-push day), at each configured slot
    (`config.URGENT_SLOTS`, default pre-market / midday / evening), run the lighter Tailwind pass and —
    only if a FRESH shock with a verified Indian beneficiary just landed (high-severity, or carrying a
    small/mid-cap name) — send a compact '💨 Fresh supply shock' email. The weekly-push day is skipped
    (the full push covers it). Marks each slot done whether or not it sent, so the ~1–2 min pipeline
    runs at most once per slot; the seen-set stops it repeating a shock across slots and days."""
    if not llm.configured():                              # AI push — needs an LLM
        return
    now = datetime.now(IST)
    if now.weekday() == WEEKLY_PUSH_WEEKDAY:                # weekly-push day → the full push handles it
        return
    slot = _current_urgent_slot(now)
    if slot is None:                                        # before pre-market — nothing due yet
        return
    if scan.tailwind_urgent_slot_done(slot) or not scan.market_open_today():
        return
    log.info("urgent Tailwind check firing (%s slot)", slot)
    con = connect()
    try:
        seen = scan.tailwind_seen_keys(con)
        rep = tailwind_brief.build_tailwind_urgent(con, seen)
    except Exception:  # noqa: BLE001
        log.exception("urgent tailwind build failed")       # no mark → retried next heartbeat
        return
    finally:
        con.close()
    scan.mark_tailwind_urgent_slot(slot)                    # this slot ran — don't re-run it today
    if not rep:
        return                                              # quiet slot — the common, correct outcome
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send urgent Tailwind")
        return
    today = datetime.now(IST).date().isoformat()
    emailer.send_report(f"💨 Fresh supply shock — {today} ({slot})", rep["markdown"], to=to,
                        html=emailer.body_html(rep["markdown"], "Fresh supply shock"))
    scan.add_tailwind_seen(rep.get("keys", []))            # don't re-alert this shock the rest of the week
    _track_push("tailwind", rep, f"urgent Tailwind ({slot})")
    log.info("urgent Tailwind break-in sent to %s (%s slot, %d catalysts)", to, slot, rep["n_catalysts"])


def maybe_pickaxe() -> None:
    """Fire the ⛏️ Pickaxe push once per calendar MONTH (first Saturday ≥18:00 IST): scout India's
    rising demand → the indirect 'sell the pickaxes' beneficiaries, deep-enriched with charts. The
    full build is ~10-15 min, so it runs in a BACKGROUND thread (never blocks the heartbeat); the
    month-marker advances only inside the worker after a successful send (or a clean empty result),
    so a delivery failure re-surfaces it next heartbeat. On-demand `pickaxe` runs any time."""
    if not llm.configured():                              # AI push — needs an LLM
        return
    if not scan.pickaxe_due() or _pickaxe_lock.locked():
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send Pickaxe")
        return
    today = datetime.now(IST).date().isoformat()
    log.info("monthly Pickaxe push firing (background)")
    threading.Thread(
        target=_pickaxe_worker,
        kwargs={"req": None, "to": to, "subject": f"⛏️ Pickaxe — {today}{schedule.catch_up_note(schedule.open_slot())}",
                "use_cache": False, "weekly": True},
        name="pickaxe-monthly", daemon=True).start()


_concall_lock = threading.Lock()


def _concall_ingest_worker() -> None:
    """Background pass: score a bounded batch of newly-filed transcripts into `concall_signals`.
    LLM-heavy (reads a PDF per call), so it runs off the heartbeat thread and only a bounded batch
    per pass — a results-season backlog drains over successive hourly runs. Holds `_concall_lock`
    so passes never overlap."""
    if not _concall_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        n = call_radar.ingest_new(con, max_new=config.CALL_RADAR_MAX_NEW)
        scan.mark_concall_ingest(con)
        if n:
            log.info("Concalls: scored %d new transcript(s) this pass", n)
    except Exception:  # noqa: BLE001 — never let the ingest crash the bot
        log.exception("Concalls ingest pass failed")
    finally:
        con.close()
        _concall_lock.release()


def maybe_concall_ingest() -> None:
    """Heartbeat hook: kick off the incremental concall-scoring pass at most ~hourly, in a
    background thread so it never blocks the IMAP loop. On-demand `calls` reads whatever's scored."""
    if not llm.configured():                              # AI push — needs an LLM
        return
    if _concall_lock.locked() or not scan.concall_ingest_due():
        return
    threading.Thread(target=_concall_ingest_worker, name="concall-ingest", daemon=True).start()


def maybe_call_radar() -> None:
    """Fire the 🎙️ Concalls push once per week (Saturday ≥18:00 IST, or caught up within WEEKLY_CATCHUP_HOURS if missed): the week's most notable
    earnings calls (forward tone vs delivered numbers). Reads the pre-scored table, so it's cheap.
    On-demand `calls` runs any time."""
    if not scan.call_radar_due():
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send Concalls")
        return
    con = connect()
    try:
        rep = call_radar_brief.build_call_radar(con)
    except Exception:  # noqa: BLE001
        log.exception("Concalls build failed")                # no mark → retried next heartbeat
        return
    finally:
        con.close()
    if not rep or not rep.get("picks"):
        scan.mark_call_radar()                                  # nothing scored — don't retry all evening
        log.info("weekly Concalls: nothing scored this week — no email")
        return
    today = datetime.now(IST).date().isoformat()
    note = ("\n\n_(Weekly digest — email **`calls`** any time for the interactive radar where you can "
            "reply a number for a name's deep report.)_")
    emailer.send_report(f"🎙️ Concalls — {today}{schedule.catch_up_note(schedule.open_slot())}", rep["markdown"] + note, to=to,
                        html=emailer.body_html(rep["markdown"] + note, "Concalls"))
    scan.mark_call_radar()                                       # advance week-marker ONLY after send
    _track_push("calls", rep, "weekly Concalls")
    log.info("weekly Concalls push sent (%d calls) to %s", len(rep["picks"]), to)


_results_lock = threading.Lock()


def _results_ingest_worker() -> None:
    """Background pass: refresh the fresh quarter's financials for a bounded batch of names that just
    filed results (XBRL per name — off the heartbeat thread). Holds `_results_lock` so passes never
    overlap; the radar reads whatever's landed."""
    if not _results_lock.acquire(blocking=False):
        return
    con = connect()
    try:
        n = results_radar.refresh_new(con, max_new=config.RESULTS_MAX_NEW)
        scan.mark_results_ingest(con)
        if n:
            log.info("Results Radar: refreshed %d just-reported name(s) this pass", n)
    except Exception:  # noqa: BLE001 — never let the refresh crash the bot
        log.exception("Results Radar refresh pass failed")
    finally:
        con.close()
        _results_lock.release()


def maybe_results_ingest() -> None:
    """Heartbeat hook: refresh just-reported names' financials at most ~hourly, in a background thread
    so it never blocks the IMAP loop. On-demand `results` reads whatever's landed."""
    if _results_lock.locked() or not scan.results_ingest_due():
        return
    threading.Thread(target=_results_ingest_worker, name="results-ingest", daemon=True).start()


def maybe_results() -> None:
    """Fire the 📈 Results Radar push once per week (Saturday ≥18:00 IST, or caught up within WEEKLY_CATCHUP_HOURS if missed): the strongest
    just-reported quarters. Reads the numbers (no LLM), so it's cheap. On-demand `results` any time."""
    if not scan.results_radar_due():
        return
    to = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not to:
        log.error("no REPORT_TO / allowlist — cannot send Results Radar")
        return
    con = connect()
    try:
        rep = results_brief.build_results(con)
    except Exception:  # noqa: BLE001
        log.exception("Results Radar build failed")            # no mark → retried next heartbeat
        return
    finally:
        con.close()
    if not rep or not rep.get("picks"):
        scan.mark_results_radar()                              # nothing reported — don't retry all evening
        log.info("weekly Results Radar: nothing in the window — no email")
        return
    today = datetime.now(IST).date().isoformat()
    note = ("\n\n_(Weekly digest — email **`results`** any time for the live radar where you can reply a "
            "number for a name's deep report.)_")
    emailer.send_report(f"📈 Results Radar — {today}{schedule.catch_up_note(schedule.open_slot())}", rep["markdown"] + note, to=to,
                        html=emailer.body_html(rep["markdown"] + note, "Results Radar"))
    scan.mark_results_radar()                                  # advance week-marker ONLY after send
    _track_push("results", rep, "weekly Results Radar")
    log.info("weekly Results Radar push sent (%d names) to %s", len(rep["picks"]), to)


_last_mail_sweep: datetime | None = None


def maybe_mail_housekeeping() -> None:
    """Move processed workbench mail on the SERVER account (this bot's Gmail) to Trash once it's
    older than 30 min — requests in Inbox (SEEN, from the client) and reports in Sent (to the
    client). Scoped strictly to correspondence with the client address, so the account's personal
    mail is never touched. Runs at most every ~15 min. Best-effort; never blocks the loop."""
    global _last_mail_sweep
    now = datetime.now(IST)
    if _last_mail_sweep and (now - _last_mail_sweep).total_seconds() < MAIL_SWEEP_EVERY_MIN * 60:
        return
    correspondent = os.environ.get("REPORT_TO") or (min(ALLOWED) if ALLOWED else None)
    if not correspondent:
        return
    _last_mail_sweep = now                                  # set before, so a failure doesn't hot-loop
    try:
        n = mail_cleanup.sweep_server_mailbox(
            host=os.environ.get("IMAP_HOST", "imap.gmail.com"),
            port=int(os.environ.get("IMAP_PORT", "993")),
            user=os.environ["IMAP_USER"], password=os.environ["IMAP_PASS"],
            correspondent=correspondent, older_than_minutes=MAIL_BIN_AFTER_MIN)
        if n:
            log.info("mail housekeeping: binned %d server workbench mail(s)", n)
    except Exception:  # noqa: BLE001
        log.exception("mail housekeeping failed")
