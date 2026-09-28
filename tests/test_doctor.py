"""`eqr doctor` on a brand-new machine: nothing configured, no data — every problem gets a fix."""

from __future__ import annotations

from equity_research.common import db


def test_fresh_machine(tmp_path, monkeypatch, capsys):
    from equity_research import cli, doctor
    from equity_research.bot import app
    from equity_research.common import env

    for k in ("LLM_MODEL", "NSE_SCRAPING_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "none.duckdb")
    monkeypatch.setattr(env, "DEFAULT_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app, "ALLOWED", set())
    monkeypatch.setattr(cli, "_server_base", lambda: None)
    assert doctor.run() == 0                                  # warnings, not failures
    out = capsys.readouterr().out
    for fix in ("copy .env.example", "set LLM_MODEL", "NSE_SCRAPING_ENABLED=true", "eqr demo",
                "eqr serve"):
        assert fix in out, fix
