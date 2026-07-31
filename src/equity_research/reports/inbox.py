"""IMAP inbox reader for the email channel (Phase 5b).

Uses IMAP IDLE so the bot *waits* for mail instead of polling: it holds one
connection open and Gmail signals the moment a request arrives. Returns only
UNSEEN messages whose sender is on the allowlist, skipping the bot's own
replies (tagged with ``email.BOT_HEADER``) so there's no request/reply loop.
"""

from __future__ import annotations

import email
import os
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

from imapclient import IMAPClient, SocketTimeout

from equity_research.reports.email import BOT_HEADER

# Bound every blocking socket op so a half-open / silently-dropped Gmail connection
# raises instead of hanging the IDLE loop forever (the main loop catches it and
# reconnects). This does NOT cut the IDLE wait short — during idle_check imapclient
# sets the socket to non-blocking and uses select() with its own timeout, then
# restores this read timeout afterwards; the read timeout only governs the quick
# round-trips (idle handshake, idle_done, search, fetch, flag).
_SOCKET_TIMEOUT = SocketTimeout(connect=30, read=120)


@dataclass(frozen=True)
class EmailRequest:
    uid: int
    sender: str          # bare address, lowercased
    subject: str
    body: str            # first meaningful text line (the query, or a "2" reply)
    message_id: str
    references: str      # full References chain (thread history) — for thread-scoped menus
    in_reply_to: str = ""  # immediate parent Message-ID (for in-thread replies)


def _unfold(raw) -> str:
    """Unfold a (possibly multi-line) header value into one CR/LF-free line."""
    return " ".join(str(raw).split()) if raw else ""


def _decode(raw) -> str:
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # noqa: BLE001
        return str(raw).strip()


def _first_line(msg: email.message.Message) -> str:
    """First non-empty, non-quoted line of the plain-text body."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                    break
                except Exception:  # noqa: BLE001
                    continue
    else:
        try:
            text = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(">") and not s.lower().startswith("on "):
            return s
    return ""


class Inbox:
    """A logged-in IMAP session over Gmail's INBOX with IDLE wait."""

    def __init__(self) -> None:
        self.host = os.environ.get("IMAP_HOST", "imap.gmail.com")
        self.port = int(os.environ.get("IMAP_PORT", "993"))
        self.user = os.environ["IMAP_USER"]
        self.password = os.environ["IMAP_PASS"]
        self._c: IMAPClient | None = None

    def connect(self) -> None:
        self._c = IMAPClient(self.host, port=self.port, ssl=True, timeout=_SOCKET_TIMEOUT)
        self._c.login(self.user, self.password)
        self._c.select_folder("INBOX")

    def logout(self) -> None:
        try:
            if self._c:
                self._c.logout()
        except Exception:  # noqa: BLE001
            pass
        self._c = None

    def wait(self, timeout: int = 300) -> bool:
        """Block until new mail arrives or ``timeout`` s elapse (IDLE).

        Returns True on inbox activity, False on timeout. The timeout doubles
        as the daily-scan heartbeat for the caller. IDLE is re-armed each call
        (Gmail drops it after ~29 min), so keep timeout well under that.
        """
        assert self._c is not None
        self._c.idle()
        try:
            responses = self._c.idle_check(timeout=timeout)
        finally:
            self._c.idle_done()
        return bool(responses)

    def fetch_requests(self, allowed: set[str], since_days: int = 1) -> list[EmailRequest]:
        """UNSEEN messages from allowlisted senders (excludes the bot's own).

        Searches per allowed sender (``UNSEEN FROM <addr> SINCE <recent>``) rather
        than all UNSEEN — the bot's mailbox is a real inbox that may hold
        thousands of unread messages; we only ever want recent mail from you.
        """
        assert self._c is not None
        out: list[EmailRequest] = []
        since = date.today() - timedelta(days=since_days)
        uids: set[int] = set()
        for addr in allowed:
            uids.update(self._c.search(["UNSEEN", "FROM", addr, "SINCE", since]))
        if not uids:
            return out
        # BODY.PEEK[] so reading doesn't auto-mark Seen — we mark only on success.
        fetched = self._c.fetch(list(uids), ["BODY.PEEK[]"])
        for uid, data in fetched.items():
            raw = data.get(b"BODY[]") or data.get(b"RFC822")
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            if msg.get(BOT_HEADER):          # our own reply — never act on it
                continue
            sender = parseaddr(msg.get("From", ""))[1].lower().strip()
            if sender not in allowed:        # auth: only allowlisted senders
                continue
            out.append(EmailRequest(
                uid=int(uid),
                sender=sender,
                subject=_decode(msg.get("Subject")),
                body=_first_line(msg),
                message_id=_unfold(msg.get("Message-ID")),
                # full References chain so we can identify the thread root; In-Reply-To
                # is only the immediate parent (kept separately for in-thread replies).
                # A long References header arrives FOLDED (CRLF + indent) — unfold it,
                # or echoing it into an outgoing reply's headers raises ValueError.
                references=_unfold(msg.get("References") or msg.get("In-Reply-To")),
                in_reply_to=_unfold(msg.get("In-Reply-To")),
            ))
        return out

    def mark_seen(self, uids: list[int]) -> None:
        assert self._c is not None
        if uids:
            self._c.add_flags(uids, [b"\\Seen"])
