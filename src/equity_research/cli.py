"""``eqr`` — the workbench from your terminal. Same commands, same flow as emailing the bot.

    eqr infosys                    # any company name, in plain words → full deep report
    eqr hdfc bank consolidated     # several matches → a numbered list; then:
    eqr pick 2                     # answer the numbered list (or a report's "deeper cut" menu)
    eqr screen: value              # every email command works: screen:, sector:, tailwind, fund: …
    eqr help                       # the full command menu
    eqr bot                        # run the always-on email bot (what the scheduled task runs)

Reports print to the terminal and are saved (Markdown + HTML + PDF) under ``data/outputs/<date>/``
(override with ``EQR_OUTPUT_DIR``). ``--open`` opens the saved HTML in your browser; ``--quiet``
prints only a preview + the saved paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from equity_research.common.env import REPO_ROOT, load_env

_STATE_FILE = REPO_ROOT / "data" / "processed" / "cli_state.json"
_SAVE_MIN_CHARS = 800            # replies shorter than this (acks, short notes) are printed, not saved
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
    def __call__(self, root: str, d) -> None:        # LocalSession on_delivery hook
        from equity_research.bot.local import localize

        body = localize(d.body)
        path = self._save(d, body) if (d.attachments or len(body) >= _SAVE_MIN_CHARS) else None
        with self._lock:
            self._clear()
            if path is None:
                print(f"› {body.strip()}\n", flush=True)
                return
            if self.quiet:
                lines = body.strip().splitlines()
                print("\n".join(lines[:_QUIET_PREVIEW_LINES]))
                if len(lines) > _QUIET_PREVIEW_LINES:
                    print(f"… ({len(lines) - _QUIET_PREVIEW_LINES} more lines in the saved report)")
            else:
                print(body.strip())
            print(f"\n📄 Saved: {path}", flush=True)
            for name, _ in d.attachments:
                print(f"   + {path.parent / (path.stem + '__' + name)}", flush=True)
            print(flush=True)
        if self.open_html:
            webbrowser.open(path.as_uri())

    def _save(self, d, body: str) -> Path:
        from equity_research.reports.pdf import render_html

        out_root = Path(os.environ.get("EQR_OUTPUT_DIR") or REPO_ROOT / "data" / "outputs")
        now = datetime.now()
        folder = out_root / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        title = re.sub(r"^\s*re:\s*", "", d.subject, flags=re.I).strip() or "report"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "report"
        stem = f"{now:%H%M%S}_{slug}"
        (folder / f"{stem}.md").write_text(body, encoding="utf-8")
        html = render_html(body, title)
        html_path = folder / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
        for name, data in d.attachments:
            (folder / f"{stem}__{name}").write_bytes(data)
        self.saved.append(html_path)
        return html_path


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
    process, so this fails while the email bot is running."""
    import duckdb

    from equity_research.common.db import connect

    try:
        connect().close()
        return None
    except duckdb.IOException as e:
        return ("The database is in use by another process — most likely the email bot is running.\n"
                "DuckDB allows one writer at a time. Stop the bot to use the CLI directly "
                "(a shared server mode is coming with the web UI).\n"
                f"  ({e})")


def _pop_flags(args: list[str], *names: str) -> tuple[list[str], set[str]]:
    hit = {a for a in args if a in names}
    return [a for a in args if a not in names], hit


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

    args, flags = _pop_flags(args, "--open", "--quiet", "-q")
    if not args:
        print(_usage())
        return 2

    _setup_logging()
    err = _db_error()
    if err:
        print(err, file=sys.stderr)
        return 1

    from equity_research.bot.local import LocalSession

    printer = _Printer(quiet=bool(flags & {"--quiet", "-q"}), open_html="--open" in flags)
    session = LocalSession(on_delivery=printer)
    printer.start()
    try:
        if args[0] == "pick":
            if len(args) != 2 or not args[1].isdigit():
                printer.stop()
                print("usage: eqr pick <number>", file=sys.stderr)
                return 2
            state = _load_state()
            if not state:
                printer.stop()
                print("Nothing to pick from yet — run a command first (e.g. `eqr hdfc`).",
                      file=sys.stderr)
                return 1
            result = session.pick(state["root"], state["subject"], int(args[1]))
        else:
            result = session.ask(" ".join(args))
            _save_state(result.root, result.subject)
    except KeyboardInterrupt:
        printer.stop()
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:                            # noqa: BLE001
        import duckdb

        if not isinstance(e, duckdb.IOException):
            raise
        printer.stop()
        print("\nThe database got busy mid-run (the email bot or a backfill needed it — DuckDB "
              "allows one writer at a time). Nothing was lost; run the command again in a minute.\n"
              f"  ({e})", file=sys.stderr)
        return 1
    finally:
        printer.stop()
        session.close()

    if result.menu:
        n = len(result.menu)
        rng = "1" if n == 1 else f"1–{n}"
        print(f"↳ Next: `eqr pick <n>` ({rng}) — " + "; ".join(
            f"{o.number}) {o.label}" for o in result.menu[:6]) + (" …" if n > 6 else ""))
    if argv is None:                                  # a real `eqr` process (not a test/embed call)
        _exit_watchdog(0)
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
