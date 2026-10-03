"""Shared pieces of the bot: schedule constants, logging, the "which one?" reply state, and small reply helpers."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from equity_research import config
from equity_research.analysis import (
    track_record,
)
from equity_research.common import llm
from equity_research.common.db import DEFAULT_DB_PATH, connect
from equity_research.reports import email as emailer
from equity_research.reports import (
    md,
)
from equity_research.reports.inbox import EmailRequest

# Delivery schedule, toggles and cadence all come from equity_research.config (env-driven, defaults =
# original behaviour). These module-level aliases keep the rest of the file terse. See config.py.
IST = config.TZ
SCAN_HOUR = config.EOD_HOUR                             # full daily digest + all weekly pushes
INTRADAY_HOUR, INTRADAY_MIN = config.MIDDAY             # midday same-day digest
INTRADAY_CUTOFF_HOUR = config.MIDDAY_CUTOFF_HOUR        # don't fire a stale "midday" digest after this
PREMARKET_HOUR, PREMARKET_MIN = config.PREMARKET        # pre-open GIFT Nifty digest
PREMARKET_CUTOFF_HOUR = config.PREMARKET_CUTOFF_HOUR    # latest a catch-up pre-market digest may fire
# Urgent Tailwind break-in slots — the supply-shock alert fires once per slot on a trading day
# (deduped so no shock repeats). Empty (TAILWIND_URGENT_SLOTS=) disables urgent alerts entirely.
URGENT_SLOTS = config.URGENT_SLOTS
WEEKLY_PUSH_WEEKDAY = config.WEEKLY_PUSH_WEEKDAY        # weekday (Mon=0…Sun=6) the weekly pushes fire
IDLE_TIMEOUT = config.HEARTBEAT_SECONDS     # IDLE wait + heartbeat (< Gmail's ~29 min cap)
PENDING_TTL_H = config.MENU_TTL_HOURS       # how long a "which one?" choice stays answerable
MAIL_BIN_AFTER_MIN = config.MAIL_BIN_AFTER_MIN     # server-account: bin workbench mail this old
MAIL_SWEEP_EVERY_MIN = config.MAIL_SWEEP_EVERY_MIN # how often the housekeeping pass runs

ALLOWED = {a.strip().lower() for a in os.environ.get("EMAIL_ALLOWED_SENDERS", "").split(",") if a.strip()}

_LOGDIR = DEFAULT_DB_PATH.parent               # data/processed — next to the DuckDB file
log = logging.getLogger("equity-email")


def setup_logging() -> None:
    """The bot's logging: INFO to data/processed/email_bot.log + stdout. Called by ``main`` (not at
    import), so the CLI / web UI can import this module without writing to the bot's log."""
    _LOGDIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s equity-email | %(message)s",
        handlers=[logging.FileHandler(_LOGDIR / "email_bot.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


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
        _track_basket(con, query, [c.symbol for c in cands], ref=req.subject)
    finally:
        con.close()


def _track_basket(con, source: str, symbols: list[str], *, ref: str = "") -> None:
    """Log an idea engine's list to the track record (a no-op for menus that aren't ideas —
    name matches, your own holdings, IPO / fund pickers — and when the track record is off)."""
    if source not in track_record.IDEA_SOURCES or not track_record.enabled():
        return
    try:
        track_record.log_basket(con, source, symbols, ref=ref)
    except Exception:  # noqa: BLE001 — the record must never cost the reply
        log.exception("track record: couldn't log the %s list", source)


def _track_push(source: str, rep: dict | None, subject: str) -> None:
    """Log the picks of a scheduled push (no reply thread, so no _set_pending) to the track record."""
    if not rep or not rep.get("picks") or not track_record.enabled():
        return
    con = connect()
    try:
        _track_basket(con, source, [p["symbol"] for p in rep["picks"]], ref=subject)
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
    # thread's menu — that's how a fund reply once triggered a stock's upside drivers.
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
    subject ('… — upside drivers') forks a brand-new thread despite correct
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


class _MenuItem:
    """Pending-state shim for a follow-up menu choice (reuses the numbered-reply UX).
    ``symbol`` is prefixed by action, e.g. ``UD:RELIANCE`` → upside drivers."""
    def __init__(self, symbol: str, name: str | None) -> None:
        self.symbol = symbol
        self.name = name or ""


def _reply_text(req: EmailRequest, text: str) -> None:
    emailer.send_report(_re_subject(req.subject), text, to=req.sender,
                        html=emailer.body_html(text),
                        in_reply_to=req.message_id, references=req.references or req.message_id)


# ----------------- screeners (idea generation) -----------------
def _needs_llm(req: EmailRequest, what: str) -> bool:
    """True (after replying why) when an AI-only command runs without an LLM configured."""
    if llm.configured():
        return False
    _reply_text(req, f"🤖 {what} needs an LLM — {llm.NOT_CONFIGURED_HELP}. The number-driven "
                     "commands (a company's report figures, screens, sectors, results) work without one.")
    return True


def _screen_run(fn, *, timeout: int = config.SCREEN_TIMEOUT_S):
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


def _inr(v: float) -> str:
    """₹ in Indian grouping (12,34,567), a minus sign for losses."""
    n = int(round(abs(v)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return ("−₹" if v < 0 else "₹") + s
