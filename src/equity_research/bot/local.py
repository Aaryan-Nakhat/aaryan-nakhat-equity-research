"""The local channel — run workbench commands without email (shared by the ``eqr`` CLI and web UI).

A command becomes the same ``EmailRequest`` the inbox would produce and goes through the bot's own
``handle_request``, so name resolution ("hdfc bank", "parag parikh flexi cap"), the "one match → go
ahead / several → numbered list" flow, follow-up menus and every screener behave exactly as they do
over email. Replies are captured by a local delivery sink (``reports.email.register_local_sink``)
instead of SMTP, grouped per conversation (the email thread root).

A numbered pick is a reply in the same conversation: ``pick(root, subject, n)`` builds the in-thread
reply the bot expects, so thread-scoped menus resolve exactly as they do in Gmail.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from equity_research.reports import email as emailer
from equity_research.reports.inbox import EmailRequest

LOCAL_SENDER = "you@eqr.local"

# Email-specific wording → channel-neutral wording, applied ONLY to what a local channel displays
# (the email text itself is untouched). Order matters: specific phrases before general ones.
_WORDING: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), r) for p, r in [
    (r"reply to this email with (?:just )?the number", "pick a number"),
    (r"reply with a number", "pick a number"),
    (r"reply with a stock's symbol or name", "pick a number (or ask for any stock by name)"),
    (r"reply with a company name", "ask for any company by name"),
    (r"reply with a name", "ask for a name"),
    (r"reply with the exact name", "ask again with the exact name"),
    (r"reply within (\d+)h", r"menu stays open \1h"),
    (r"lands in this thread", "will show up here"),
    (r"land in this thread", "show up here"),
    (r"in this thread", "here"),
    (r"in the previous email", "above"),
    (r"send a fresh email", "run a new command"),
    (r"send a company name in the subject line", "give a company name"),
    (r"\b(?:email|reply) `", "run `"),
    (r"everything you can email me — put the command in the \*\*subject\*\* line",
     "everything you can ask — type a command"),
    (r"put this in the subject", "Type this"),
    (r"put a company name or NSE symbol in the subject", "type a company name or NSE symbol"),
    (r"scheduled pushes to your inbox", "scheduled pushes (email bot only)"),
]]


def localize(text: str) -> str:
    """Rewrite email phrasing for a local channel, keeping the sentence's leading capital."""
    def fix(pat: re.Pattern[str], repl: str, s: str) -> str:
        def one(m: re.Match[str]) -> str:
            out = m.expand(repl)
            return out[:1].upper() + out[1:] if m.group(0)[:1].isupper() else out
        return pat.sub(one, s)
    for pat, repl in _WORDING:
        text = fix(pat, repl, text)
    return text


def _new_id() -> str:
    return f"<{uuid.uuid4().hex}@eqr.local>"


def thread_root(references: str | None, in_reply_to: str | None, message_id: str | None = None) -> str:
    """The conversation id — the same root the bot's ``_thread_id`` hashes (first References entry,
    else the parent, else the message itself)."""
    refs = (references or "").split()
    return (refs[0] if refs else (in_reply_to or message_id or "")).strip()


@dataclass
class MenuOption:
    number: int
    symbol: str                  # the bot's raw menu token, e.g. 'INFY', 'UD:INFY', 'MF:122639'
    label: str                   # human label, e.g. 'INFY — Infosys Limited'


@dataclass
class Result:
    root: str                    # conversation id (pass to ``pick``)
    subject: str                 # the original command
    deliveries: list[emailer.Delivery] = field(default_factory=list)
    menu: list[MenuOption] = field(default_factory=list)   # numbered choices now open, if any


def _label(symbol: str, name: str) -> str:
    if symbol.startswith("UD:"):
        return f"Upside Drivers 1-pager — {symbol[3:]}"
    if symbol.startswith("IUD:"):
        return f"Upside Drivers 1-pager (IPO) — {symbol[4:]}"
    if symbol.startswith("MF:"):
        return name or symbol[3:]
    if symbol.startswith("IPO:"):
        return f"{name or symbol[4:]} (IPO)"
    return f"{symbol} — {name}" if name else symbol


class LocalSession:
    """Runs commands through the bot's ``handle_request`` and collects the replies per conversation.
    ``on_delivery(root, delivery)`` fires as each reply arrives (the "got it" ack first, the report
    minutes later) so a UI can stream them. Call ``close()`` when done."""

    def __init__(self, sender: str = LOCAL_SENDER,
                 on_delivery: Callable[[str, emailer.Delivery], None] | None = None) -> None:
        self.sender = sender
        self.on_delivery = on_delivery
        self._by_root: dict[str, list[emailer.Delivery]] = {}
        self._lock = threading.Lock()
        emailer.register_local_sink(sender, self._sink)

    def close(self) -> None:
        emailer.unregister_local_sink(self.sender)

    def _sink(self, d: emailer.Delivery) -> None:
        root = thread_root(d.references, d.in_reply_to)
        with self._lock:
            self._by_root.setdefault(root, []).append(d)
        if self.on_delivery is not None:
            self.on_delivery(root, d)

    def ask(self, text: str, *, wait_background: bool = True) -> Result:
        """A fresh command — exactly what you'd put in an email's Subject line."""
        subject = " ".join(text.split())
        mid = _new_id()
        req = EmailRequest(uid=0, sender=self.sender, subject=subject, body="",
                           message_id=mid, references="", in_reply_to="")
        return self._run(req, root=mid, subject=subject, wait_background=wait_background)

    def pick(self, root: str, subject: str, n: int, *, wait_background: bool = True) -> Result:
        """Answer the numbered menu open in conversation ``root`` — a bare-number in-thread reply."""
        s = subject.strip()
        re_subject = s if s.lower().startswith("re:") else f"Re: {s}"
        req = EmailRequest(uid=0, sender=self.sender, subject=re_subject, body=str(n),
                           message_id=_new_id(), references=root, in_reply_to=root)
        return self._run(req, root=root, subject=subject, wait_background=wait_background)

    def _run(self, req: EmailRequest, *, root: str, subject: str, wait_background: bool) -> Result:
        from equity_research.bot import app          # heavy import — only when a command runs

        before = set(threading.enumerate())
        with self._lock:
            start = len(self._by_root.get(root, []))
        app.handle_request(req)
        if wait_background:
            # Some builds (Pickaxe) ack, then deliver from a background thread minutes later. Wait
            # only for those — the bot names them '*-ondemand'. Other new threads are thread-pool
            # workers (name resolution, timeouts) that idle forever; joining them would hang.
            for t in set(threading.enumerate()) - before:
                if t.name.endswith("-ondemand"):
                    t.join()
        with self._lock:
            deliveries = list(self._by_root.get(root, [])[start:])
        return Result(root=root, subject=subject, deliveries=deliveries, menu=self.menu(root))

    def menu(self, root: str) -> list[MenuOption]:
        """The numbered choices currently open in conversation ``root`` (empty if none / expired)."""
        from equity_research.bot import app

        probe = EmailRequest(uid=0, sender=self.sender, subject="", body="",
                             message_id="", references=root, in_reply_to=root)
        found = app._find_pending(probe)
        if not found:
            return []
        return [MenuOption(i, str(sym), _label(str(sym), name or ""))
                for i, (sym, name) in enumerate(found[1], 1)]
