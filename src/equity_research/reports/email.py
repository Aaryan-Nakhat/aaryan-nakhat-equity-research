"""Email delivery for research reports (SMTP).

Config from environment (or a .env you load yourself):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
  REPORT_FROM (default SMTP_USER), REPORT_TO.

For Gmail, use an App Password (not your account password). See ``.env.example``.

Local delivery: the CLI and web UI reuse the bot's command handlers, which all reply through
``send_report(..., to=<requester>)``. They register a sink for their own local sender address
(``register_local_sink``); a send to that address is handed to the sink instead of SMTP. Routing by
recipient (not a global switch) means replies sent later from background threads (Pickaxe) still
reach the right place, and scheduled pushes to the real inbox are unaffected.
"""

from __future__ import annotations

import os
import smtplib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage

from equity_research.reports.pdf import disclaimer_text, render_html

# Marks every email the bot sends, so the inbound poller can ignore its own
# replies (no request/reply loop) when send + receive share one mailbox.
BOT_HEADER = "X-EquityBot"


class EmailConfigError(RuntimeError):
    pass


@dataclass
class Delivery:
    """One reply, as a local sink receives it — the same fields an email would carry."""
    subject: str
    body: str                                   # markdown
    to: str
    html: str | None = None
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    in_reply_to: str | None = None
    references: str | None = None


_local_sinks: dict[str, Callable[[Delivery], None]] = {}
_sinks_lock = threading.Lock()


def register_local_sink(address: str, sink: Callable[[Delivery], None]) -> None:
    """Route every ``send_report`` addressed to ``address`` to ``sink`` instead of SMTP."""
    with _sinks_lock:
        _local_sinks[address.strip().lower()] = sink


def unregister_local_sink(address: str) -> None:
    with _sinks_lock:
        _local_sinks.pop(address.strip().lower(), None)


def _local_sink_for(address: str | None) -> Callable[[Delivery], None] | None:
    if not address:
        return None
    with _sinks_lock:
        return _local_sinks.get(address.strip().lower())


def _cfg(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(key, default)
    if required and not val:
        raise EmailConfigError(f"missing required env var {key}")
    return val


def body_html(markdown_text: str, title: str = "") -> str:
    """Markdown report -> styled HTML for the email body (same look as the PDF)."""
    return render_html(markdown_text, title)


def send_report(subject: str, body: str, *, to: str | None = None,
                html: str | None = None,
                attachments: list[tuple[str, bytes]] | None = None,
                in_reply_to: str | None = None,
                references: str | None = None) -> None:
    """Send a plain-text (+ optional HTML) email via SMTP STARTTLS.

    ``attachments`` is a list of ``(filename, data)`` PDF blobs. ``in_reply_to`` /
    ``references`` (a Message-ID) thread the reply under the original request. A recipient with
    a registered local sink (CLI / web UI) gets the delivery handed over instead — no SMTP.
    """
    sink = _local_sink_for(to)
    if sink is not None:
        sink(Delivery(subject=subject, body=body, to=to or "", html=html,
                      attachments=list(attachments or []),
                      in_reply_to=in_reply_to, references=references))
        return
    host = _cfg("SMTP_HOST", required=True)
    port = int(_cfg("SMTP_PORT", "587"))
    user = _cfg("SMTP_USER", required=True)
    password = _cfg("SMTP_PASS", required=True)
    sender = _cfg("REPORT_FROM", user)
    recipient = to or _cfg("REPORT_TO", required=True)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg[BOT_HEADER] = "1"
    if in_reply_to:
        # Unfold: a References chain read from a raw message can carry folded
        # CRLF+indent whitespace, which EmailMessage rejects in a header value.
        msg["In-Reply-To"] = " ".join(in_reply_to.split())
        msg["References"] = " ".join((references or in_reply_to).split())
    # Compliance footer on the plain-text part. The HTML alternative already
    # carries it (render_html appends the same disclaimer), so a client shows it
    # whichever part it renders. Empty REPORT_DISCLAIMER disables both.
    disc = disclaimer_text()
    text_body = f"{body}\n\n—\n{disc}" if disc else body
    msg.set_content(text_body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data in attachments or []:
        msg.add_attachment(data, maintype="application", subtype="pdf",
                           filename=filename)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
