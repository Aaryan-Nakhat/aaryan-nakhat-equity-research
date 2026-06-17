"""Email delivery for research reports (SMTP).

Config from environment (or a .env you load yourself):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
  REPORT_FROM (default SMTP_USER), REPORT_TO.

For Gmail, use an App Password (not your account password). See ``.env.example``.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from equity_research.reports.pdf import render_html

# Marks every email the bot sends, so the inbound poller can ignore its own
# replies (no request/reply loop) when send + receive share one mailbox.
BOT_HEADER = "X-EquityBot"


class EmailConfigError(RuntimeError):
    pass


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
    ``references`` (a Message-ID) thread the reply under the original request.
    """
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
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data in attachments or []:
        msg.add_attachment(data, maintype="application", subtype="pdf",
                           filename=filename)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
