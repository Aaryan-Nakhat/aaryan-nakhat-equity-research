"""``eqr doctor`` — what works on this machine, and the one-line fix for what doesn't.

Checks the things a first-time user trips on: the ``.env``, an LLM, email (optional), the NSE
opt-in (needed to download financial statements), the PDF renderer, the data store and whether a
server is running. Read-only: it changes nothing.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    status: str          # ok | warn | fail | info
    name: str
    detail: str
    fix: str = ""


_ICON = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "info": "ℹ️ "}


def _env_file() -> Check:
    from equity_research.common.env import DEFAULT_ENV_PATH

    if DEFAULT_ENV_PATH.is_file():
        return Check("ok", ".env", f"found ({DEFAULT_ENV_PATH})")
    return Check("warn", ".env", "not found — running on defaults",
                  "copy .env.example to .env and fill in what you need")


def _llm() -> Check:
    model = (os.environ.get("LLM_MODEL") or "").strip()
    if model:
        return Check("ok", "LLM", f"LLM_MODEL={model} (the AI analysis, name matching, Tailwind…)")
    return Check("warn", "LLM", "not set — reports still build with every number, without the AI "
                 "write-up", "set LLM_MODEL (+ its API key) in .env — any provider LiteLLM supports")


def _email() -> Check:
    from equity_research.bot import app

    if app.email_configured():
        return Check("ok", "Email bot", f"configured for {', '.join(sorted(app.ALLOWED))}")
    return Check("info", "Email bot", "not configured — optional; the web UI and `eqr` work without it",
                 "set IMAP_/SMTP_ credentials + EMAIL_ALLOWED_SENDERS in .env to email commands")


def _nse() -> Check:
    on = os.environ.get("NSE_SCRAPING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if on:
        return Check("ok", "NSE data", "enabled — financial statements, filings, ownership can be fetched")
    return Check("warn", "NSE data", "off — prices work, but financial statements / filings / "
                 "ownership can't be downloaded, so deep reports stay thin",
                 "read NSE's terms, then set NSE_SCRAPING_ENABLED=true in .env (or run `eqr demo`, "
                 "which asks once)")


def _playwright_browsers_dir() -> Path:
    """Where Playwright keeps its browsers (``PLAYWRIGHT_BROWSERS_PATH`` or the per-OS default)."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _pdf() -> Check:
    base = _playwright_browsers_dir()
    if base.is_dir() and any(p.name.startswith("chromium") for p in base.iterdir()):
        return Check("ok", "PDF renderer", "Chromium installed")
    return Check("warn", "PDF renderer", "Chromium not installed — reports come without PDFs",
                 "uv run playwright install chromium")


def _data() -> Check:
    import duckdb

    from equity_research.common.db import DEFAULT_DB_PATH, connect

    if not DEFAULT_DB_PATH.exists():
        return Check("warn", "Data", "no database yet", "run `eqr demo` (≈12 min) to fetch a starter set")
    try:
        con = connect()
    except duckdb.IOException:
        return Check("info", "Data", "in use by the running server (that's expected)")
    try:
        names = con.execute("SELECT count(*) FROM equity_master").fetchone()[0]
        eod = con.execute("SELECT count(DISTINCT symbol), max(trade_date) FROM equity_eod").fetchone()
        fin = con.execute("SELECT count(DISTINCT symbol) FROM financials").fetchone()[0]
    finally:
        con.close()
    detail = (f"{names:,} listed names · prices for {eod[0]:,} (to {eod[1]}) · "
              f"financials for {fin:,}")
    if fin == 0:
        return Check("warn", "Data", detail, "run `eqr demo` to fetch a starter set of companies")
    return Check("ok", "Data", detail)


def _server() -> Check:
    from equity_research import cli, config

    if cli._server_base():
        return Check("ok", "Server", f"running — web UI at http://{cli._local_host()}:{config.WEB_PORT}")
    return Check("info", "Server", "not running", "`eqr serve` starts the web UI (and the email bot "
                 "if configured)")


def run() -> int:
    """Print every check; exit code 1 if anything failed outright."""
    checks = []
    for fn in (_env_file, _llm, _email, _nse, _pdf, _data, _server):
        try:
            checks.append(fn())
        except Exception as e:  # noqa: BLE001 — a broken check must not hide the others
            checks.append(Check("fail", fn.__name__.strip("_"), f"check crashed: {e}"))
    width = max(len(c.name) for c in checks)
    print("eqr doctor — what works on this machine\n")
    for c in checks:
        print(f"{_ICON[c.status]} {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != "ok":
            print(f"   {'':<{width}}  → {c.fix}")
    return 1 if any(c.status == "fail" for c in checks) else 0
