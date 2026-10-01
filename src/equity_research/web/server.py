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
    GET  /api/holdings               → your portfolio valued (re-reads holdings.csv if it changed)
    GET  /api/stocks?q=bharat ele    → company-name search for the add box
    POST /api/holdings               ← {"stock", "qty", "price", "date"?}  → the new lot
    PUT  /api/holdings/{id}          ← {"qty", "price", "date"?}          (UI lots only)
    DELETE /api/holdings/{id}                                             (UI lots only)
    POST /api/sells                  ← {"stock", "qty", "price", "date", "kind"?: "sell" | "buyback"}
    DELETE /api/sells/{id}                                                (UI sells only)

Binds to localhost by default (``WEB_HOST`` / ``WEB_PORT``). Runs standalone (``serve``) or in a
background thread of the email bot (``start_background``), so one process owns the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import urllib.parse
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
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


def _static_version() -> str:
    """Changes whenever a static file changes, so browsers fetch the new CSS / JS after an update
    instead of a cached copy (``/static/app.js?v=…``)."""
    files = (_WEB_DIR / "static").glob("*")
    return str(max((f.stat().st_mtime_ns for f in files if f.is_file()), default=0))


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


class LotRequest(BaseModel):
    stock: str | None = None
    qty: float | str
    price: float | str
    date: str | None = None
    received: str | None = None      # got these through a merger / demerger on this date


class SellRequest(BaseModel):
    stock: str
    qty: float | str
    price: float | str
    date: str
    kind: str = "sell"


class JobRequest(BaseModel):
    text: str | None = None
    pick: int | None = None
    root: str | None = None
    subject: str | None = None


_COOKIE = "eqr_session"
_OPEN_PATHS = ("/login", "/logout", "/api/health")     # reachable without signing in


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def session_token(password: str) -> str:
    """The session cookie value: an HMAC of the password (changing the password signs everyone out)."""
    return hmac.new(password.encode(), b"eqr-session-v1", hashlib.sha256).hexdigest()


def _authorised(request: Request, password: str) -> bool:
    cookie = request.cookies.get(_COOKIE, "")
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    return (hmac.compare_digest(cookie, session_token(password))
            or (bool(bearer) and hmac.compare_digest(bearer, password)))


def _install_auth(app: FastAPI, password: str) -> None:
    """With ``WEB_PASSWORD`` set: the browser signs in once (an HttpOnly, SameSite=Lax cookie — which
    also blocks cross-site form/fetch posts), the CLI sends ``Authorization: Bearer <password>``."""

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static/") or _authorised(request, password):
            return await call_next(request)
        if path.startswith(("/api/", "/files/")):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(_templates().get_template("login.html").render(error=None))

    @app.post("/login")
    async def login(request: Request):
        form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
        given = (form.get("password") or [""])[0]
        if not hmac.compare_digest(given, password):
            await asyncio.sleep(0.8)                      # slow down guessing
            return HTMLResponse(_templates().get_template("login.html").render(
                error="That password isn't right."), status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(_COOKIE, session_token(password), httponly=True, samesite="lax",
                        max_age=30 * 24 * 3600)
        return resp

    @app.get("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_COOKIE)
        return resp


def create_app(manager: JobManager | None = None, *, password: str | None = None) -> FastAPI:
    """The FastAPI app. ``manager`` is injectable for tests; by default one is made on first use.
    ``password`` (default ``WEB_PASSWORD``) turns on sign-in."""
    app = FastAPI(title="Equity Research Workbench", docs_url=None, redoc_url=None)
    password = config.WEB_PASSWORD if password is None else password
    if password:
        _install_auth(app, password)
    state: dict[str, JobManager | None] = {"m": manager}
    lock = threading.Lock()

    def jobs() -> JobManager:
        with lock:
            if state["m"] is None:
                state["m"] = JobManager()
            return state["m"]

    @app.get("/api/health")
    def health() -> dict:
        # open without sign-in (the CLI probes it) — so it reveals nothing beyond "a server is here"
        return {"ok": True, "service": SERVICE, "auth": bool(password)}

    @app.get("/api/status")
    def status() -> dict:
        return {"active": jobs().active()}

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
        return HTMLResponse(_templates().get_template("index.html").render(
            commands_json=cmds, auth=bool(password), v=_static_version()))

    def _holdings_call(fn):
        from equity_research import portfolio
        from equity_research.common.db import connect

        con = connect()
        try:
            return fn(portfolio, con)
        except KeyError:
            raise HTTPException(404, "no such entry") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            con.close()

    @app.get("/api/holdings")
    def holdings_view() -> JSONResponse:
        def run(h, con):
            sync = h.sync_csv(con)
            return {**h.portfolio(con), "csv": sync, "how_to": h.HOW_TO_ENTER}
        out = _holdings_call(run)
        _start_lookups(out)
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    def _start_lookups(out: dict) -> None:
        """Background reads the page is waiting on: demerger cost notices, merger terms, dividend histories,
        the 31-Jan-2018 prices. Each runs once at a time; the page re-polls while any is pending."""
        from equity_research.analysis import demerger_costs, former_companies
        from equity_research.portfolio import income, tax

        if out.get("lookups"):
            demerger_costs.lookup_missing_async(out["lookups"])
        if out.get("merger_lookups"):
            former_companies.lookup_missing_async(out["merger_lookups"])
        if out.get("dividend_refresh"):
            income.refresh_async(out["dividend_refresh"])
        if out.get("fmv_pending"):
            tax.load_fmv_async()

    @app.get("/api/stocks")
    def stocks_search(q: str = "") -> list[dict]:
        return _holdings_call(lambda h, con: h.search(con, q))

    @app.post("/api/holdings")
    def holdings_add(req: LotRequest) -> JSONResponse:
        lot = _holdings_call(lambda h, con: h.add_lot(con, req.stock or "", req.qty, req.price, req.date or None,
                                                          received=req.received or None))
        return JSONResponse(json.loads(json.dumps(lot, default=str)))

    @app.put("/api/holdings/{lot_id}")
    def holdings_edit(lot_id: str, req: LotRequest) -> dict:
        _holdings_call(lambda h, con: h.update_lot(con, lot_id, req.qty, req.price, req.date or None,
                                                             req.received or None))
        return {"ok": True}

    @app.delete("/api/holdings/{lot_id}")
    def holdings_delete(lot_id: str) -> dict:
        _holdings_call(lambda h, con: h.delete_lot(con, lot_id))
        return {"ok": True}

    @app.post("/api/sells")
    def sells_add(req: SellRequest) -> JSONResponse:
        sell = _holdings_call(lambda h, con: h.add_sell(con, req.stock, req.qty, req.price, req.date, kind=req.kind))
        return JSONResponse(json.loads(json.dumps(sell, default=str)))

    @app.delete("/api/sells/{sell_id}")
    def sells_delete(sell_id: str) -> dict:
        _holdings_call(lambda h, con: h.delete_sell(con, sell_id))
        return {"ok": True}

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


def refuse_reason(host: str) -> str | None:
    """Why the server won't start on ``host`` — anything beyond localhost needs ``WEB_PASSWORD`` (an
    open UI on a VPS would let anyone run reports on your LLM key). None when it's fine."""
    if is_loopback(host) or config.WEB_PASSWORD:
        return None
    if os.environ.get("EQR_IN_CONTAINER"):
        # Inside Docker the server has to listen on all interfaces for port publishing to work;
        # exposure is decided by the host's port mapping (the shipped compose file publishes to
        # 127.0.0.1 only). Allowed — but said loudly, because publishing it wider needs a password.
        log.warning("web UI listening on %s inside a container with no WEB_PASSWORD — keep the port "
                    "published to 127.0.0.1 only, or set WEB_PASSWORD before exposing it", host)
        return None
    return (f"refusing to serve on {host} without WEB_PASSWORD — set a password in .env, or keep "
            "WEB_HOST=127.0.0.1")


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the server in the foreground (``eqr serve`` without email configured)."""
    host, port = host or config.WEB_HOST, port or config.WEB_PORT
    if (why := refuse_reason(host)):
        raise SystemExit(why)
    log.info("web UI on http://%s:%d", host, port)
    _server(host, port).run()


def start_background(host: str | None = None, port: int | None = None) -> threading.Thread | None:
    """Run the server in a daemon thread (hosted by the email bot). Returns None — and the bot
    carries on without a UI — if it can't start (e.g. the port is taken by another instance)."""
    host, port = host or config.WEB_HOST, port or config.WEB_PORT
    if (why := refuse_reason(host)):
        log.error("web UI: %s — continuing without it", why)
        return None
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
