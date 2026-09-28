"""The job runner behind the web UI and CLI forwarding.

A job is one command (``"infosys"``, ``"screen: value"``) or one numbered pick in an existing
conversation. It runs the bot's own ``handle_request`` through a ``LocalSession`` on a small thread
pool, and records an **event log** a client can follow live: short notes (the "got it" acks),
reports (saved to disk like the CLI's, with their file paths and URLs), the numbered menu that
opened, and done / error. Events are appended under a condition variable, so a streaming reader can
block until the next one arrives.

Replies are routed to a job by conversation root: a pick reuses its conversation's root, so the
**latest** job for a root receives its replies (including ones a background build — Pickaxe —
delivers minutes later, which the job waits for before it finishes).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from equity_research import config
from equity_research.bot import local
from equity_research.reports import email as emailer

log = logging.getLogger("equity-research.web")

WEB_SENDER = local.LOCAL_SENDER      # same as the CLI: a menu opened in one can be picked in the other
_KEEP_JOBS = 200                      # finished jobs kept in memory (history persists on disk)


@dataclass
class Job:
    id: str
    text: str                         # what the user typed (or "pick 2")
    subject: str                      # the conversation's original command
    root: str                         # conversation id
    created: float = field(default_factory=time.time)
    status: str = "queued"            # queued → running → done | error
    events: list[dict] = field(default_factory=list)
    menu: list[dict] = field(default_factory=list)
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def add(self, kind: str, **data) -> None:
        with self._cond:
            self.events.append({"seq": len(self.events) + 1, "kind": kind, "ts": time.time(), **data})
            self._cond.notify_all()

    def wait_events(self, after: int, timeout: float) -> list[dict]:
        """Events with seq > ``after``; blocks up to ``timeout`` for the next one (or completion)."""
        with self._cond:
            if len(self.events) <= after and self.status not in ("done", "error"):
                self._cond.wait(timeout)
            return self.events[after:]

    @property
    def finished(self) -> bool:
        return self.status in ("done", "error")

    def summary(self) -> dict:
        return {"id": self.id, "text": self.text, "subject": self.subject, "root": self.root,
                "created": self.created, "status": self.status, "menu": self.menu,
                "reports": sum(1 for e in self.events if e["kind"] == "report")}


def file_url(path: Path) -> str:
    """URL the server serves a saved output file at (``/files/<relative path>``)."""
    rel = Path(path).resolve().relative_to(local.output_root().resolve())
    return "/files/" + rel.as_posix()


class JobManager:
    """Runs commands for the web UI / CLI and keeps their live event logs."""

    def __init__(self, max_workers: int | None = None) -> None:
        self.session = local.LocalSession(sender=WEB_SENDER, on_delivery=self._on_delivery)
        self.pool = ThreadPoolExecutor(max_workers or max(config.WEB_MAX_JOBS, 1),
                                       thread_name_prefix="eqr-job")
        self.jobs: dict[str, Job] = {}
        self._by_root: dict[str, Job] = {}
        self._lock = threading.Lock()

    # submission ------------------------------------------------------
    def submit(self, text: str) -> Job:
        """A fresh command, as you'd put in an email Subject."""
        subject = " ".join(text.split())
        job = Job(id=uuid.uuid4().hex[:12], text=subject, subject=subject, root=local.new_root())
        return self._start(job, lambda: self.session.ask(subject, root=job.root))

    def submit_pick(self, root: str, subject: str, n: int) -> Job:
        """Answer the numbered menu open in conversation ``root``."""
        job = Job(id=uuid.uuid4().hex[:12], text=f"pick {n}", subject=subject, root=root)
        return self._start(job, lambda: self.session.pick(root, subject, n))

    def _start(self, job: Job, run) -> Job:
        with self._lock:
            self.jobs[job.id] = job
            self._by_root[job.root] = job
            self._trim()
        job.add("status", status="queued")
        self.pool.submit(self._run, job, run)
        return job

    def _run(self, job: Job, run) -> None:
        job.status = "running"
        job.add("status", status="running")
        try:
            result = run()
            job.menu = [{"n": o.number, "label": o.label} for o in result.menu]
            if job.menu:
                job.add("menu", options=job.menu)
            job.status = "done"
            job.add("done")
        except Exception as e:  # noqa: BLE001 — a failed job reports itself, never kills the pool
            import duckdb

            busy = isinstance(e, duckdb.IOException)
            log.exception("job %s (%r) failed", job.id, job.text)
            job.status = "error"
            job.add("error", message=("The database was busy — please retry in a minute." if busy
                                      else f"Something went wrong: {e}"))

    # deliveries ------------------------------------------------------
    def _on_delivery(self, root: str, d: emailer.Delivery) -> None:
        with self._lock:
            job = self._by_root.get(root)
        if job is None:
            log.warning("delivery for unknown conversation %s — dropped", root)
            return
        body = local.localize(d.body)
        if not local.is_substantial(d, body):
            job.add("note", text=body.strip())
            return
        saved = local.save_delivery(d, body)
        title = (d.subject or "").removeprefix("Re: ").strip() or job.subject
        job.add("report", title=title, markdown=body,
                html_path=str(saved.html), html_url=file_url(saved.html),
                attachments=[{"name": a.name.split("__", 1)[-1], "path": str(a), "url": file_url(a)}
                             for a in saved.attachments])
        _append_history({"ts": datetime.now().isoformat(timespec="seconds"), "job": job.id,
                         "root": job.root, "subject": job.subject, "title": title,
                         "html": file_url(saved.html),
                         "attachments": [file_url(a) for a in saved.attachments]})

    # housekeeping ------------------------------------------------------
    def _trim(self) -> None:
        done = [j for j in self.jobs.values() if j.finished]
        for j in sorted(done, key=lambda j: j.created)[:max(0, len(done) - _KEEP_JOBS)]:
            self.jobs.pop(j.id, None)
            if self._by_root.get(j.root) is j:
                self._by_root.pop(j.root, None)

    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if not j.finished)

    def close(self) -> None:
        self.session.close()
        self.pool.shutdown(wait=False, cancel_futures=True)


_history_lock = threading.Lock()


def history_path() -> Path:
    return local.output_root() / "history.jsonl"


def _append_history(rec: dict) -> None:
    """One line per saved report — the web UI's history survives restarts."""
    try:
        with _history_lock:
            p = history_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("couldn't record report history", exc_info=True)


def read_history(limit: int = 200) -> list[dict]:
    """Saved reports, newest first."""
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines[-limit:]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
