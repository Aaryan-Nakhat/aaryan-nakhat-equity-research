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


class EmailConfigError(RuntimeError):
    pass


def _cfg(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(key, default)
    if required and not val:
        raise EmailConfigError(f"missing required env var {key}")
    return val


def send_report(subject: str, body: str, *, to: str | None = None,
                html: str | None = None) -> None:
    """Send a plain-text (and optional HTML) email via SMTP STARTTLS."""
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
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
