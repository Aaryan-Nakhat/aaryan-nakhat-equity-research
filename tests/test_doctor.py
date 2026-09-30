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


def test_nse_enabled_without_its_browser_fails_loudly(monkeypatch):
    from equity_research import doctor

    monkeypatch.setenv("NSE_SCRAPING_ENABLED", "true")
    monkeypatch.setattr(doctor, "_stealth_chromium_ready", lambda: False)
    c = doctor._nse()
    assert c.status == "fail" and "playwright install chromium" in c.fix
    monkeypatch.setattr(doctor, "_stealth_chromium_ready", lambda: True)
    assert doctor._nse().status == "ok"
