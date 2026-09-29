"""Backfill the track record from calls that had already gone out before logging began.

    uv run python scripts/backfill_track_record.py [--dry-run] [--no-mail]

Only calls that were **actually sent** are recovered — nothing is regenerated for a past date (an LLM
asked today "what would you have said in August" knows how August went). Every recovered row is
marked ``provenance='recovered'`` so the scorecard can show it apart from live calls. Sources:

1. **Numbered lists the bot sent** — every idea-engine list (Tailwind, Pickaxe, Hotlist, screens,
   Concalls, Results) is stored with its timestamp as the thread's reply menu (``alert_state``).
2. **Deep reports still in the bot's mailbox** — Sent and Trash (Gmail empties Trash after ~30 days),
   matched by the ``X-EquityBot`` header; the verdict is read from the report text and the time from
   the email's date.
3. **Reports saved on disk** by the web UI / ``eqr`` (``data/outputs/<date>/<HHMMSS>_*.md``).

A deep report whose text can't be found (older than the mailbox keeps) has no recoverable verdict and
is left out rather than guessed. Run it with the bot stopped (DuckDB has one writer). Idempotent —
re-running adds nothing already logged.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_IST = timezone(timedelta(hours=5, minutes=30))
_HEAD = re.compile(r"Report for \*\*([A-Z0-9&\-]+)\*\*")


def _menus(con, tr, dry: bool) -> int:
    """Idea-engine lists from the stored reply menus."""
    n = 0
    for (value,) in con.execute("SELECT value FROM alert_state WHERE symbol = '__email__'").fetchall():
        try:
            d = json.loads(value)
        except (TypeError, ValueError):
            continue
        src, ts = d.get("query", ""), d.get("ts")
        if src not in tr.IDEA_SOURCES or not ts:
            continue
        syms = [c[0] for c in d.get("cands", []) if c and c[0]]
        if dry:
            n += len(syms)
            continue
        n += tr.log_basket(con, src, syms, provenance="recovered", made_at=datetime.fromisoformat(ts),
                           ref=f"menu: {src}")
    return n


def _deep(con, tr, symbol: str, text: str, made_at: datetime, ref: str, dry: bool) -> int:
    label, stance = tr.parse_verdict(text)
    if stance == "review":
        return 0
    if dry:
        return 1
    return int(tr.log_call(con, "deep_report", symbol, stance, label, provenance="recovered",
                           made_at=made_at, ref=ref))


def _body(msg) -> str:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _mailbox(con, tr, dry: bool) -> int:
    """Deep reports the bot sent that are still in its Sent / Trash folders."""
    from imapclient import IMAPClient

    from equity_research.mail_cleanup import _find_folder
    from equity_research.reports.email import BOT_HEADER

    host, user, pw = os.environ.get("IMAP_HOST", "imap.gmail.com"), os.environ.get("IMAP_USER"), \
        os.environ.get("IMAP_PASS")
    if not (user and pw):
        print("  (no IMAP credentials in .env — skipping the mailbox)")
        return 0
    n = 0
    c = IMAPClient(host, port=int(os.environ.get("IMAP_PORT", "993")), ssl=True, timeout=90)
    try:
        c.login(user, pw)
        for folder in (_find_folder(c, "\\Sent", "[Gmail]/Sent Mail"), _find_folder(c, "\\Trash", "[Gmail]/Trash")):
            try:
                c.select_folder(folder, readonly=True)
                uids = c.search(["HEADER", BOT_HEADER, "1"])
            except Exception as e:  # noqa: BLE001
                print(f"  (can't read {folder}: {e})")
                continue
            print(f"  {folder}: {len(uids)} bot messages")
            for i in range(0, len(uids), 100):
                for data in c.fetch(uids[i:i + 100], ["RFC822"]).values():
                    msg = email.message_from_bytes(data[b"RFC822"])
                    text = _body(msg)
                    m = _HEAD.search(text[:400])
                    if not m:
                        continue
                    try:
                        when = parsedate_to_datetime(msg["Date"])
                    except (TypeError, ValueError):
                        continue
                    n += _deep(con, tr, m.group(1), text, when, f"email: {msg['Subject']}", dry)
    finally:
        try:
            c.logout()
        except Exception:  # noqa: BLE001
            pass
    return n


def _saved(con, tr, dry: bool) -> int:
    """Deep reports the web UI / eqr saved to disk (folder date + HHMMSS in the file name, IST)."""
    from equity_research.bot.local import output_root

    n = 0
    for f in sorted(output_root().glob("*/*.md")):
        m = re.match(r"(\d{6})_", f.name)
        if not m:
            continue
        try:
            when = datetime.strptime(f"{f.parent.name} {m.group(1)}", "%Y-%m-%d %H%M%S").replace(tzinfo=_IST)
        except ValueError:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        h = _HEAD.search(text[:400])
        if h:
            n += _deep(con, tr, h.group(1), text, when, f"saved: {f.name}", dry)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="count what would be recovered; write nothing")
    ap.add_argument("--no-mail", action="store_true", help="skip the mailbox (offline)")
    args = ap.parse_args()
    from equity_research.common.env import load_env
    load_env()
    from equity_research.analysis import track_record as tr
    from equity_research.common.db import connect

    con = connect()
    try:
        before = con.execute("SELECT count(*) FROM calls").fetchone()[0]
        print("Recovering calls that had already been sent" + (" (dry run)" if args.dry_run else ""))
        print(f"  idea-engine lists: {_menus(con, tr, args.dry_run)} picks")
        if not args.no_mail:
            print(f"  deep reports in the mailbox: {_mailbox(con, tr, args.dry_run)}")
        print(f"  deep reports saved on disk: {_saved(con, tr, args.dry_run)}")
        after = con.execute("SELECT count(*) FROM calls").fetchone()[0]
        print(f"calls table: {before} → {after}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
