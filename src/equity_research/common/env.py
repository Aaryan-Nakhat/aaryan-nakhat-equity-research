"""Load the repo's ``.env`` into the process environment — the Python twin of the loop in
``scripts/run_email_bot.ps1`` (``KEY=value`` lines; blanks and ``#`` comments skipped). No
third-party dependency. Existing environment variables win, so a value exported in the shell
(or set by the launcher) is never clobbered by the file."""

from __future__ import annotations

import os
from pathlib import Path

from equity_research.common.db import DEFAULT_DB_PATH

REPO_ROOT = DEFAULT_DB_PATH.parents[2]          # <repo>/data/processed/equity.duckdb → <repo>
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def load_env(path: str | Path | None = None, *, override: bool = False) -> int:
    """Read ``path`` (default ``<repo>/.env``) into ``os.environ``. Returns how many keys were set.
    A missing file is fine (returns 0) — every setting also has a code default."""
    p = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if not key or (not override and key in os.environ):
            continue
        os.environ[key] = val
        n += 1
    return n
