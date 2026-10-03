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
    monkeypatch.setenv("LLM_MODEL", "test/model")     # an LLM "is configured" (never called)
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
    from equity_research.bot import screens

    monkeypatch.setattr(screens, "_screen_run", lambda fn, **k: None)          # the wrapper's timeout
    assert "timed out" in text_of(session.ask("results"))


def test_tailwind_nothing_vs_timeout(session, monkeypatch):
    from equity_research.bot import screens
    from equity_research.reports import tailwind_brief

    monkeypatch.setattr(tailwind_brief, "build_tailwind_report", lambda con, **k: None)
    out = text_of(session.ask("tailwind"))
    assert "surfaced right now" in out and "timed out" not in out
    monkeypatch.setattr(screens, "_screen_run", lambda fn, **k: None)
    assert "timed out" in text_of(session.ask("tailwind"))


def test_ai_only_commands_say_they_need_an_llm(session, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    for cmd in ("tailwind", "pickaxe", "policy"):
        out = text_of(session.ask(cmd))
        assert "needs an LLM" in out and "LLM_MODEL" in out, cmd


def test_generate_fails_fast_without_a_model(monkeypatch):
    from equity_research.common import llm

    monkeypatch.setenv("LLM_MODEL", "")
    with pytest.raises(llm.LLMNotConfigured):
        llm.generate("sys", "hi")
