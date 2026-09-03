"""Server-mailbox housekeeping — move processed workbench mail to Gmail Trash.

The bot's account (``IMAP_USER``, e.g. aaryan.nakhat@gmail.com) is ALSO a personal Gmail, so this
ONLY ever touches mail **corresponding with the workbench client** (``correspondent``, e.g.
aaryan.nakhat.invest@gmail.com): processed requests in the Inbox (matched ``SEEN FROM correspondent``)
and the reports the bot sent (matched ``TO correspondent`` in Sent). Personal mail from any other
sender is never matched.

A message is only binned once it's older than a grace window (30 min by default), so a report still
in flight — or an unprocessed request (Inbox match requires ``SEEN``) — is never disturbed. "Bin" =
Gmail **Trash** (auto-purges after ~30 days, so recoverable). Best-effort: logs and returns a count,
never raises fatally. ``dry_run=True`` reports what it WOULD move without touching anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from imapclient import IMAPClient

from equity_research.reports.email import BOT_HEADER   # 'X-EquityBot' — stamped on every bot send

log = logging.getLogger("equity-research.mailclean")

_BATCH = 200          # move in chunks so the first backlog-clearing run doesn't hit IMAP limits


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _find_folder(client: IMAPClient, want_flag: str, fallback: str) -> str:
    """Locate a special-use folder (e.g. '\\Trash', '\\Sent') by its flag, robust to bytes/str."""
    for flags, _delim, name in client.list_folders():
        norm = {f.decode() if isinstance(f, bytes) else f for f in flags}
        if want_flag in norm:
            return name
    return fallback


def sweep_server_mailbox(*, host: str, port: int, user: str, password: str,
                         correspondent: str, older_than_minutes: int = 30,
                         dry_run: bool = False) -> int:
    """Move workbench mail (to/from ``correspondent``) older than ``older_than_minutes`` to Trash.
    Returns the number moved (or that WOULD move, if ``dry_run``). Never raises fatally."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    moved = 0
    c = IMAPClient(host, port=port, ssl=True, timeout=60)
    c.normalise_times = False          # keep INTERNALDATE tz-AWARE (default naive-local misreads as UTC)
    try:
        c.login(user, password)
        trash = _find_folder(c, "\\Trash", "[Gmail]/Trash")
        sent = _find_folder(c, "\\Sent", "[Gmail]/Sent Mail")
        # (folder, search criteria). Inbox: only SEEN (processed) requests FROM the client — nothing
        # in-flight/unprocessed. Sent: EVERYTHING the bot sent (the X-EquityBot header), so ALL report
        # types are cleaned — stock reports, screeners, sector, Tailwind, the digests, and the acks —
        # not just deep reports. Both stay off the account's personal mail.
        targets = [("INBOX", ["SEEN", "FROM", correspondent]),
                   (sent, ["HEADER", BOT_HEADER, "1"])]
        for folder, crit in targets:
            try:
                c.select_folder(folder)
            except Exception:  # noqa: BLE001
                log.warning("mailclean: cannot select folder %s — skipping", folder)
                continue
            try:
                uids = c.search(crit)
            except Exception:  # noqa: BLE001
                log.exception("mailclean: search failed in %s", folder)
                continue
            if not uids:
                continue
            info = c.fetch(uids, ["INTERNALDATE"])
            old = [u for u, d in info.items()
                   if (_to_utc(d.get(b"INTERNALDATE")) or datetime.now(timezone.utc)) < cutoff]
            if not old:
                continue
            if dry_run:
                moved += len(old)
                log.info("mailclean[dry-run]: %d msg(s) in %s would move to Trash", len(old), folder)
                continue
            for i in range(0, len(old), _BATCH):            # batch — the first run clears a backlog
                chunk = old[i:i + _BATCH]
                try:
                    c.move(chunk, trash)
                    moved += len(chunk)
                except Exception:  # noqa: BLE001
                    log.exception("mailclean: move failed in %s (batch %d)", folder, i // _BATCH)
            log.info("mailclean: moved %d msg(s) from %s to Trash", len(old), folder)
    finally:
        try:
            c.logout()
        except Exception:  # noqa: BLE001
            pass
    return moved
