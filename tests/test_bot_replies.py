"""Command replies must tell "nothing found" apart from "timed out" — a quiet week between results
seasons once told the user Results Radar had timed out. Throwaway DuckDB, no network, no SMTP."""

from __future__ import annotations

import smtplib

import pytest

from equity_research.common import db


@pytest.fixture
def session(tmp_path, monkeypatch):
    from equity_research.bot.local import LocalSession

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("no SMTP"))
    s = LocalSession(sender="replies@eqr.local")
    yield s
    s.close()


def text_of(result) -> str:
    return " | ".join(d.body for d in result.deliveries)


def test_results_nothing_reported_is_not_a_timeout(session, monkeypatch):
    from equity_research.reports import results_brief

    monkeypatch.setattr(results_brief, "build_results", lambda con: None)   # an empty window
    out = text_of(session.ask("results"))
    assert "No companies have reported" in out and "timed out" not in out


def test_results_timeout_says_so(session, monkeypatch):
    from equity_research.bot import app

    monkeypatch.setattr(app, "_screen_run", lambda fn, **k: None)          # the wrapper's timeout
    assert "timed out" in text_of(session.ask("results"))


def test_tailwind_nothing_vs_timeout(session, monkeypatch):
    from equity_research.bot import app
    from equity_research.reports import tailwind_brief

    monkeypatch.setattr(tailwind_brief, "build_tailwind_report", lambda con, **k: None)
    out = text_of(session.ask("tailwind"))
    assert "surfaced right now" in out and "timed out" not in out
    monkeypatch.setattr(app, "_screen_run", lambda fn, **k: None)
    assert "timed out" in text_of(session.ask("tailwind"))
