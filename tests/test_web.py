"""The web server + job runner, and the CLI forwarding to it — against a throwaway DuckDB and output
folder (no network, no SMTP). Commands used are ones that need no data or LLM (`help`, an
out-of-range pick)."""

from __future__ import annotations

import json
import smtplib
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from equity_research.common import db


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setenv("EQR_OUTPUT_DIR", str(tmp_path / "out"))

    def no_smtp(*a, **k):
        raise AssertionError("SMTP must not be used")
    monkeypatch.setattr(smtplib, "SMTP", no_smtp)
    return tmp_path


@pytest.fixture
def client(env):
    from equity_research.web.jobs import JobManager
    from equity_research.web.server import create_app

    manager = JobManager(max_workers=2)
    with TestClient(create_app(manager)) as c:
        yield c
    manager.close()


def wait_done(client, job_id: str, timeout: float = 60) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job didn't finish")


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "service": "eqr", "active": 0}


def test_a_command_runs_and_its_report_is_saved_and_served(client, env):
    job = client.post("/api/jobs", json={"text": "help"}).json()
    assert job["status"] in ("queued", "running", "done")
    done = wait_done(client, job["id"])
    assert done["status"] == "done"
    kinds = [e["kind"] for e in done["events"]]
    assert kinds[:2] == ["status", "status"] and kinds[-1] == "done" and "report" in kinds
    rep = next(e for e in done["events"] if e["kind"] == "report")
    assert "screen:" in rep["markdown"]
    page = client.get(rep["html_url"])
    assert page.status_code == 200 and "<html" in page.text.lower()
    hist = client.get("/api/history").json()
    assert hist and hist[0]["job"] == job["id"] and hist[0]["html"] == rep["html_url"]


def test_event_stream_replays_then_ends(client):
    job = client.post("/api/jobs", json={"text": "help"}).json()
    wait_done(client, job["id"])
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as r:
        lines = [ln for ln in r.iter_lines() if ln]
    body = lines[:lines.index("event: end")]                  # the events, before the end marker
    data = [json.loads(ln[6:]) for ln in body if ln.startswith("data: ")]
    assert [d["seq"] for d in data] == list(range(1, len(data) + 1))
    assert lines[-2:] == ["event: end", "data: {}"]


def test_pick_answers_the_conversations_menu(client):
    from equity_research.bot import app
    from equity_research.bot.local import LOCAL_SENDER

    first = client.post("/api/jobs", json={"text": "help"}).json()
    wait_done(client, first["id"])
    probe = app.EmailRequest(uid=0, sender=LOCAL_SENDER, subject="hdfc", body="",
                             message_id=first["root"], references="", in_reply_to="")
    app._set_pending(probe, "hdfc", [app._MenuItem("HDFCBANK", "HDFC Bank"),
                                     app._MenuItem("HDFCLIFE", "HDFC Life")])
    pick = client.post("/api/jobs", json={"pick": 9, "root": first["root"],
                                          "subject": first["subject"]}).json()
    assert pick["root"] == first["root"]
    done = wait_done(client, pick["id"])
    notes = [e["text"] for e in done["events"] if e["kind"] == "note"]
    assert any("2 option(s)" in n for n in notes)            # answered in the same conversation


def test_the_page_and_its_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and 'id="q"' in page.text and "EQR_COMMANDS" in page.text
    assert "</script>" not in page.text.split("EQR_COMMANDS", 1)[1].split("</script>", 1)[0]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_command_catalogue_comes_from_the_bots_help(client):
    cmds = client.get("/api/commands").json()
    names = {c["cmd"] for c in cmds}
    assert {"screen: value", "hotlist", "tailwind", "booking"} <= names
    assert not any(c["cmd"].startswith("1") for c in cmds)          # "reply 1" isn't a command
    assert all(c["desc"] and c["section"] for c in cmds)


def test_bad_requests(client):
    assert client.post("/api/jobs", json={"text": "  "}).status_code == 400
    assert client.post("/api/jobs", json={"pick": 1}).status_code == 400
    assert client.get("/api/jobs/nope").status_code == 404


def test_files_cannot_escape_the_outputs_folder(client, env):
    (env / "secret.txt").write_text("nope", encoding="utf-8")
    assert client.get("/files/../secret.txt").status_code == 404
    assert client.get("/files/..%2Fsecret.txt").status_code == 404


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cli_forwards_to_a_running_server(env, monkeypatch, capsys):
    """The real thing: a server in this process on a free port, and `eqr help` sent to it."""
    from equity_research import cli, config
    from equity_research.web import server as web

    port = _free_port()
    monkeypatch.setattr(config, "WEB_PORT", port)
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(cli, "load_env", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_STATE_FILE", env / "cli_state.json")
    monkeypatch.setattr(cli, "_run_direct", lambda *a: pytest.fail("must go through the server"))
    srv = web._server("127.0.0.1", port)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        assert cli._server_base() == f"http://127.0.0.1:{port}"
        assert cli.main(["help", "--quiet"]) == 0
        out = capsys.readouterr().out
        assert "Saved:" in out and str(env / "out") in out           # the server saved it
        assert json.loads((env / "cli_state.json").read_text())["root"]  # picks will work
    finally:
        srv.should_exit = True
        t.join(timeout=5)
