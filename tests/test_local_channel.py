"""Tests for the local (non-email) channel: delivery routing, wording, thread-scoped menus, and the
``eqr`` CLI end to end. Every test runs against a throwaway DuckDB in a temp dir — no network, no
SMTP, and never the real data store."""

from __future__ import annotations

import smtplib

import pytest

from equity_research.common import db
from equity_research.reports import email as emailer


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point ``connect()`` at a fresh schema-only database for the duration of a test."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.duckdb")
    return tmp_path


@pytest.fixture
def no_smtp(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("SMTP must not be used for a local delivery")
    monkeypatch.setattr(smtplib, "SMTP", boom)


# ----------------------------- delivery routing -----------------------------
def test_local_sink_receives_delivery_instead_of_smtp(no_smtp):
    got = []
    emailer.register_local_sink("Me@Eqr.Local", got.append)
    try:
        emailer.send_report("Re: infy", "hello", to="me@eqr.local", html="<p>hello</p>",
                            attachments=[("a.pdf", b"%PDF")], in_reply_to="<r>", references="<r>")
    finally:
        emailer.unregister_local_sink("me@eqr.local")
    assert len(got) == 1
    d = got[0]
    assert (d.subject, d.body, d.references, d.attachments) == ("Re: infy", "hello", "<r>",
                                                                 [("a.pdf", b"%PDF")])


def test_unregistered_address_still_goes_over_smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, pw):
            pass

        def send_message(self, msg):
            sent.append(msg["To"])

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASS": "p"}.items():
        monkeypatch.setenv(k, v)
    emailer.send_report("s", "b", to="someone@example.com")
    assert sent == ["someone@example.com"]


# ----------------------------- wording -----------------------------
@pytest.mark.parametrize("email_text, local_text", [
    ("Just reply to this email with the number:", "Just pick a number:"),
    ("**Reply with a number** for its report.", "**Pick a number** for its report."),
    ("(Reply within 24h; otherwise just send a fresh email.)",
     "(Menu stays open 24h; otherwise just run a new command.)"),
    ("Lands in this thread shortly.", "Will show up here shortly."),
    ("everything will land in this thread.", "everything will show up here."),
    ("email `pickaxe` again", "run `pickaxe` again"),
])
def test_localize(email_text, local_text):
    from equity_research.bot.local import localize
    assert localize(email_text) == local_text


# ----------------------------- sessions & menus -----------------------------
def test_help_runs_through_the_bot_handler(temp_db, no_smtp):
    from equity_research.bot.local import LocalSession

    s = LocalSession(sender="t1@eqr.local")
    try:
        r = s.ask("help")
    finally:
        s.close()
    assert r.deliveries, "help should reply"
    assert "screen:" in r.deliveries[0].body          # the same command menu the email bot sends
    assert r.menu == []


def test_menu_is_scoped_to_its_conversation_and_pick_answers_it(temp_db, no_smtp):
    from equity_research.bot import app
    from equity_research.bot.local import LocalSession

    s = LocalSession(sender="t2@eqr.local")
    try:
        first = s.ask("help")                        # any conversation to hang a menu on
        other = s.ask("help")
        # arm a two-option "which one?" menu in the FIRST conversation only, exactly as the bot does
        probe = app.EmailRequest(uid=0, sender=s.sender, subject="hdfc", body="",
                                 message_id=first.root, references="", in_reply_to="")
        app._set_pending(probe, "hdfc", [app._MenuItem("HDFCBANK", "HDFC Bank Limited"),
                                         app._MenuItem("HDFCLIFE", "HDFC Life Insurance")])
        assert [o.label for o in s.menu(first.root)] == ["HDFCBANK — HDFC Bank Limited",
                                                          "HDFCLIFE — HDFC Life Insurance"]
        assert s.menu(other.root) == []              # never leaks into another conversation
        # an out-of-range pick is answered in-thread, with the menu's size
        r = s.pick(first.root, "hdfc", 9)
        assert "2 option(s)" in r.deliveries[-1].body
    finally:
        s.close()


# ----------------------------- busy database (email bot side) -----------------------------
def test_email_request_is_retried_not_dropped_when_db_busy(monkeypatch):
    """Another process (the CLI mid-report) holds the DuckDB lock → the email stays unseen and is
    retried next cycle; after the retry cap the sender is told to resend."""
    import duckdb

    from equity_research.bot import app

    req = app.EmailRequest(uid=42, sender="me@x.com", subject="infosys", body="",
                           message_id="<m>", references="")

    class FakeInbox:
        seen: list[int] = []

        def fetch_requests(self, allowed):
            return [req]

        def mark_seen(self, uids):
            self.seen.extend(uids)

    def busy(r):
        raise duckdb.IOException("Cannot open file: being used by another process")

    replies = []
    monkeypatch.setattr(app, "handle_request", busy)
    monkeypatch.setattr(app, "_reply_text", lambda r, t: replies.append(t))
    monkeypatch.setattr(app, "_db_busy_retries", {})
    inbox = FakeInbox()
    for _ in range(app._DB_BUSY_MAX_RETRIES - 1):
        app._drain(inbox)
    assert inbox.seen == [] and replies == []           # still unseen → will be retried
    app._drain(inbox)                                     # final attempt → give up politely
    assert inbox.seen == [42] and len(replies) == 1 and "send it again" in replies[0]


# ----------------------------- CLI end to end -----------------------------
def test_cli_help_prints_and_saves(temp_db, no_smtp, monkeypatch, capsys):
    from equity_research import cli

    monkeypatch.setattr(cli, "load_env", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "_STATE_FILE", temp_db / "cli_state.json")
    monkeypatch.setenv("EQR_OUTPUT_DIR", str(temp_db / "out"))

    assert cli.main(["help", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "Saved:" in out
    saved = list((temp_db / "out").rglob("*.html"))
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8").strip()
    assert (temp_db / "cli_state.json").exists()     # so a later `eqr pick` knows the conversation


def test_cli_pick_without_history_explains(temp_db, monkeypatch, capsys):
    from equity_research import cli

    monkeypatch.setattr(cli, "load_env", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "_STATE_FILE", temp_db / "missing.json")
    assert cli.main(["pick", "1"]) == 1
    assert "run a command first" in capsys.readouterr().err
