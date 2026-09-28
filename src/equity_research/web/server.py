"""The HTTP server: the job API (used by the web UI and by the ``eqr`` CLI when a server is running)
and the saved-report files.

    GET  /api/health                 → {"ok": true, "service": "eqr", "active": n}
    POST /api/jobs                   ← {"text": "infosys"} or {"pick": 2, "root": …, "subject": …}
                                     → the job summary (id, root, subject, status…)
    GET  /api/jobs                   → recent jobs, newest first
    GET  /api/jobs/{id}              → a job with its full event log
    GET  /api/jobs/{id}/events       → Server-Sent Events: each event as it happens, then "end"
    GET  /api/history                → saved reports, newest first (survives restarts)
    GET  /files/{path}               → a saved report / PDF from the outputs folder

Binds to localhost by default (``WEB_HOST`` / ``WEB_PORT``). Runs standalone (``serve``) or in a
background thread of the email bot (``start_background``), so one process owns the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import re
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from equity_research import config
from equity_research.bot import local
from equity_research.web.jobs import JobManager, read_history

log = logging.getLogger("equity-research.web")

SERVICE = "eqr"
_WEB_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _templates():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(loader=FileSystemLoader(_WEB_DIR / "templates"),
                       autoescape=select_autoescape(["html"]))


@lru_cache(maxsize=1)
def command_catalog() -> list[dict]:
    """Every command, from the same help table the bot emails (so the UI can't drift from the real
    commands): ``[{"cmd", "also", "desc", "section"}]``. ``cmd`` is the first backticked form."""
    from equity_research.bot.app import _HELP_SECTIONS

    out = []
    for title, _intro, rows in _HELP_SECTIONS:
        for first, desc in rows:
            if first.strip().lower().startswith("reply"):
                continue                                   # a reply-a-number, not a command
            forms = re.findall(r"`([^`]+)`", first)
            if not forms:
                continue
            out.append({"cmd": forms[0], "also": forms[1:],
                        "desc": re.sub(r"\*\*|__", "", desc).strip(), "section": title})
    return out


class JobRequest(BaseModel):
    text: str | None = None
    pick: int | None = None
    root: str | None = None
    subject: str | None = None


def create_app(manager: JobManager | None = None) -> FastAPI:
    """The FastAPI app. ``manager`` is injectable for tests; by default one is made on first use."""
    app = FastAPI(title="Equity Research Workbench", docs_url=None, redoc_url=None)
    state: dict[str, JobManager | None] = {"m": manager}
    lock = threading.Lock()

    def jobs() -> JobManager:
        with lock:
            if state["m"] is None:
                state["m"] = JobManager()
            return state["m"]

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": SERVICE, "active": jobs().active()}

    @app.post("/api/jobs")
    def create(req: JobRequest) -> dict:
        if req.pick is not None:
            if not req.root or not req.subject:
                raise HTTPException(400, "a pick needs the conversation's root and subject")
            return jobs().submit_pick(req.root, req.subject, req.pick).summary()
        if not req.text or not req.text.strip():
            raise HTTPException(400, "type a command — a company name, 'screen: value', 'help' …")
        return jobs().submit(req.text).summary()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        return [j.summary() for j in sorted(jobs().jobs.values(), key=lambda j: -j.created)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs().jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return {**job.summary(), "events": job.events}

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, after: int = 0) -> StreamingResponse:
        job = jobs().jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")

        async def stream():
            seen = after
            while True:
                batch = await asyncio.to_thread(job.wait_events, seen, 15.0)
                for ev in batch:
                    seen = ev["seq"]
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if job.finished and seen >= len(job.events):
                    yield "event: end\ndata: {}\n\n"
                    return
                if not batch:
                    yield ": keep-alive\n\n"           # long builds: keep proxies from timing out

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/history")
    def history(limit: int = 200) -> list[dict]:
        return read_history(limit)

    @app.get("/api/commands")
    def commands() -> list[dict]:
        return command_catalog()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        cmds = json.dumps(command_catalog(), ensure_ascii=False).replace("</", "<\\/")  # stay inside <script>
        return HTMLResponse(_templates().get_template("index.html").render(commands_json=cmds))

    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")

    @app.get("/files/{path:path}")
    def files(path: str) -> FileResponse:
        root = local.output_root().resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():   # no escaping the outputs folder
            raise HTTPException(404, "not found")
        return FileResponse(target)

    return app


def _server(host: str, port: int):
    import uvicorn

    return uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port, log_level="warning",
                                         access_log=False))


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the server in the foreground (``eqr serve`` without email configured)."""
    host, port = host or config.WEB_HOST, port or config.WEB_PORT
    log.info("web UI on http://%s:%d", host, port)
    _server(host, port).run()


def start_background(host: str | None = None, port: int | None = None) -> threading.Thread | None:
    """Run the server in a daemon thread (hosted by the email bot). Returns None — and the bot
    carries on without a UI — if it can't start (e.g. the port is taken by another instance)."""
    host, port = host or config.WEB_HOST, port or config.WEB_PORT
    server = _server(host, port)

    def run() -> None:
        try:
            server.run()
        except (SystemExit, OSError):
            log.error("web UI couldn't start on %s:%d (port in use?) — continuing without it",
                      host, port)

    t = threading.Thread(target=run, name="eqr-web", daemon=True)
    t.start()
    for _ in range(50):                     # up to ~5 s for the socket to bind
        if server.started or not t.is_alive():
            break
        threading.Event().wait(0.1)
    if server.started:
        log.info("web UI on http://%s:%d", host, port)
        return t
    return None
