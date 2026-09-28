"""``eqr`` — the workbench from your terminal. Same commands, same flow as emailing the bot.

    eqr infosys                    # any company name, in plain words → full deep report
    eqr hdfc bank consolidated     # several matches → a numbered list; then:
    eqr pick 2                     # answer the numbered list (or a report's "deeper cut" menu)
    eqr screen: value              # every email command works: screen:, sector:, tailwind, fund: …
    eqr help                       # the full command menu
    eqr serve                      # the web UI at http://localhost:8765 (+ the email bot if configured)
    eqr doctor                     # what works on this machine, and how to fix what doesn't
    eqr bot                        # run the always-on email bot (it also serves the web UI)

Reports print to the terminal and are saved (Markdown + HTML + PDF) under ``data/outputs/<date>/``
(override with ``EQR_OUTPUT_DIR``). ``--open`` opens the saved HTML in your browser; ``--quiet``
prints only a preview + the saved paths. If a server is running (`eqr serve`, or the email bot),
commands run through it — one process owns the database — and `--direct` forces a local run.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from equity_research.common.env import REPO_ROOT, load_env

_STATE_FILE = REPO_ROOT / "data" / "processed" / "cli_state.json"
_QUIET_PREVIEW_LINES = 12


def _usage() -> str:
    return (__doc__ or "").split("\n\n", 1)[1].rstrip()


# ----------------------------- output -----------------------------
class _Printer:
    """Prints each reply as it arrives (and saves the substantial ones), with a live 'working…'
    ticker in between on an interactive terminal. Thread-safe: Pickaxe delivers from a worker."""

    def __init__(self, *, quiet: bool, open_html: bool) -> None:
        self.quiet = quiet
        self.open_html = open_html
        self.saved: list[Path] = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # live ticker ---------------------------------------------------
    def start(self) -> None:
        if sys.stderr.isatty():
            self._ticker = threading.Thread(target=self._tick, name="eqr-ticker", daemon=True)
            self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=2)
        self._clear()

    def _tick(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        while not self._stop.wait(0.2):
            secs = int(time.monotonic() - self._t0)
            with self._lock:
                sys.stderr.write(f"\r{frames[i % len(frames)]} working… {secs // 60}m{secs % 60:02d}s ")
                sys.stderr.flush()
            i += 1

    def _clear(self) -> None:
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 40 + "\r")
            sys.stderr.flush()

    # deliveries ----------------------------------------------------
    def __call__(self, root: str, d) -> None:        # LocalSession on_delivery hook (direct mode)
        from equity_research.bot.local import is_substantial, localize, save_delivery

        body = localize(d.body)
        saved = save_delivery(d, body) if is_substantial(d, body) else None
        self.show(body, saved.html if saved else None, saved.attachments if saved else [])

    def show(self, body: str, html: Path | None, attachments: list[Path]) -> None:
        """Print one reply: a short note as-is, a report (preview when --quiet) + its saved paths.
        Also used for replies forwarded from a running `eqr serve` (already saved there)."""
        with self._lock:
            self._clear()
            if html is None:
                print(f"› {body.strip()}\n", flush=True)
                return
            if self.quiet:
                lines = body.strip().splitlines()
                print("\n".join(lines[:_QUIET_PREVIEW_LINES]))
                if len(lines) > _QUIET_PREVIEW_LINES:
                    print(f"… ({len(lines) - _QUIET_PREVIEW_LINES} more lines in the saved report)")
            else:
                print(body.strip())
            print(f"\n📄 Saved: {html}", flush=True)
            for a in attachments:
                print(f"   + {a}", flush=True)
            print(flush=True)
        self.saved.append(html)
        if self.open_html:
            webbrowser.open(Path(html).as_uri())


# ----------------------------- state -----------------------------
def _save_state(root: str, subject: str) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"root": root, "subject": subject,
                                           "ts": datetime.now().isoformat()}), encoding="utf-8")
    except OSError:
        pass                                   # picks just won't carry over; not fatal


def _load_state() -> dict | None:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ----------------------------- plumbing -----------------------------
def _setup_logging() -> None:
    """Full INFO log to data/processed/cli.log. The terminal shows only our own warnings (and any
    library error) — fetch logs and third-party warnings stay in the file."""
    logdir = REPO_ROOT / "data" / "processed"
    logdir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(logdir / "cli.log", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    sh.addFilter(lambda r: r.name.startswith("equity") or r.levelno >= logging.ERROR)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]
    # Scrapling attaches its own console handler unless its logger already has one — give it the
    # file handler instead (and don't double-log via root).
    sl = logging.getLogger("scrapling")
    sl.handlers[:] = [fh]
    sl.propagate = False


def _db_error() -> str | None:
    """None if the database opens; otherwise a friendly explanation. DuckDB allows one writer
    process, so this fails while another process (a backfill, an old bot without the web server)
    holds it."""
    import duckdb

    from equity_research.common.db import connect

    try:
        connect().close()
        return None
    except duckdb.IOException as e:
        return ("The database is in use by another process. DuckDB allows one writer at a time — "
                "start the bot / `eqr serve` (the CLI then runs through it) or stop the other "
                f"process.\n  ({e})")


def _pop_flags(args: list[str], *names: str) -> tuple[list[str], set[str]]:
    hit = {a for a in args if a in names}
    return [a for a in args if a not in names], hit


# ----------------------------- via a running server -----------------------------
def _local_host() -> str:
    from equity_research import config

    return "127.0.0.1" if config.WEB_HOST in ("", "0.0.0.0", "::") else config.WEB_HOST


def _server_base() -> str | None:
    """Base URL of a running `eqr serve` / email-bot web server on this machine, or None."""
    import urllib.request

    from equity_research import config

    base = f"http://{_local_host()}:{config.WEB_PORT}"
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=0.7) as r:
            return base if json.load(r).get("service") == "eqr" else None
    except (OSError, ValueError):
        return None


def _via_server(base: str, payload: dict,
                printer: _Printer) -> tuple[str, str, list[tuple[int, str]]]:
    """Submit to the server and print its replies as they stream in (the server saves the files).
    Returns (root, subject, menu). Raises RuntimeError with the server's message on failure."""
    import urllib.error
    import urllib.request

    from equity_research import config

    auth = {"Authorization": f"Bearer {config.WEB_PASSWORD}"} if config.WEB_PASSWORD else {}
    req = urllib.request.Request(base + "/api/jobs", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **auth}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            job = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("The server needs a password — set the same WEB_PASSWORD in this "
                               "machine's .env.") from e
        raise RuntimeError(f"The server refused the command (HTTP {e.code}).") from e
    menu: list[tuple[int, str]] = []
    events = urllib.request.Request(f"{base}/api/jobs/{job['id']}/events", headers=auth)
    with urllib.request.urlopen(events, timeout=120) as stream:
        for raw in stream:
            line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
            if line.startswith("event: end"):
                break
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["kind"] == "note":
                printer.show(ev["text"], None, [])
            elif ev["kind"] == "report":
                printer.show(ev["markdown"], Path(ev["html_path"]),
                             [Path(a["path"]) for a in ev.get("attachments", [])])
            elif ev["kind"] == "menu":
                menu = [(o["n"], o["label"]) for o in ev["options"]]
            elif ev["kind"] == "error":
                raise RuntimeError(ev["message"])
    return job["root"], job["subject"], menu


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):              # ₹ / emoji on a Windows console
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")
    # LiteLLM logs every call to stdout via its own handler; keep the terminal to the reports.
    os.environ["LITELLM_LOG"] = "ERROR"
    load_env()

    if not args or args[0] in ("-h", "--help"):
        print(_usage())
        return 0
    if args[0] == "bot":
        from equity_research.bot import app
        app.main()
        return 0
    if args[0] == "serve":
        return _serve()
    if args[0] == "doctor":
        from equity_research import doctor
        return doctor.run()

    args, flags = _pop_flags(args, "--open", "--quiet", "-q", "--direct")
    if not args:
        print(_usage())
        return 2
    state = None
    if args[0] == "pick":
        if len(args) != 2 or not args[1].isdigit():
            print("usage: eqr pick <number>", file=sys.stderr)
            return 2
        state = _load_state()
        if not state:
            print("Nothing to pick from yet — run a command first (e.g. `eqr hdfc`).", file=sys.stderr)
            return 1

    printer = _Printer(quiet=bool(flags & {"--quiet", "-q"}), open_html="--open" in flags)
    base = None if "--direct" in flags else _server_base()
    if base:
        payload = ({"pick": int(args[1]), "root": state["root"], "subject": state["subject"]}
                   if state else {"text": " ".join(args)})
        printer.start()
        try:
            root, subject, menu = _via_server(base, payload, printer)
        except KeyboardInterrupt:
            printer.stop()
            print(f"\nStopped watching — the job keeps running on the server ({base}).",
                  file=sys.stderr)
            return 130
        except RuntimeError as e:
            printer.stop()
            print(f"\n{e}", file=sys.stderr)
            return 1
        finally:
            printer.stop()
    else:
        rc = _run_direct(args, state, printer)
        if isinstance(rc, int):
            return rc
        root, subject, menu = rc

    if not state:
        _save_state(root, subject)
    if menu:
        n = len(menu)
        rng = "1" if n == 1 else f"1–{n}"
        print(f"↳ Next: `eqr pick <n>` ({rng}) — " + "; ".join(
            f"{num}) {label}" for num, label in menu[:6]) + (" …" if n > 6 else ""))
    if argv is None:                                  # a real `eqr` process (not a test/embed call)
        _exit_watchdog(0)
    return 0


def _run_direct(args: list[str], state: dict | None,
                printer: _Printer) -> int | tuple[str, str, list[tuple[int, str]]]:
    """No server running: open the database in this process and run the command here."""
    _setup_logging()
    err = _db_error()
    if err:
        print(err, file=sys.stderr)
        return 1

    from equity_research.bot.local import LocalSession

    session = LocalSession(on_delivery=printer)
    printer.start()
    try:
        if state:
            result = session.pick(state["root"], state["subject"], int(args[1]))
        else:
            result = session.ask(" ".join(args))
    except KeyboardInterrupt:
        printer.stop()
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:                            # noqa: BLE001
        import duckdb

        if not isinstance(e, duckdb.IOException):
            raise
        printer.stop()
        print("\nThe database got busy mid-run (another process needed it — DuckDB allows one "
              "writer at a time). Nothing was lost; run the command again in a minute.\n"
              f"  ({e})", file=sys.stderr)
        return 1
    finally:
        printer.stop()
        session.close()
    return result.root, result.subject, [(o.number, o.label) for o in result.menu]


def _serve() -> int:
    """`eqr serve` — the web UI, plus the email bot in the same process when email is configured."""
    from equity_research import config
    from equity_research.bot import app

    url = f"http://{_local_host()}:{config.WEB_PORT}"
    if _server_base():
        print(f"A server is already running at {url}", file=sys.stderr)
        return 1
    if app.email_configured():
        print(f"Web UI → {url}  (the email bot runs in the same process)")
        app.main(web_ui=True)
        return 0
    from equity_research.web import server

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    print(f"Web UI → {url}  (email isn't configured — web only)")
    server.serve()
    return 0


def _exit_watchdog(code: int, grace_s: float = 15.0) -> None:
    """Everything is printed and saved. Interpreter exit joins leftover worker threads; if one is
    wedged (e.g. a PDF render that timed out), force the exit after ``grace_s`` rather than leave
    the terminal hanging. A normal exit finishes long before the timer fires."""
    sys.stdout.flush()
    sys.stderr.flush()
    t = threading.Timer(grace_s, os._exit, args=(code,))
    t.daemon = True
    t.start()


if __name__ == "__main__":
    raise SystemExit(main())
