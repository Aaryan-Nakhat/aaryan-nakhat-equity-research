"""Launcher for the always-on email bot — the logic lives in ``equity_research.bot.app``.

Kept at this path because run_email_bot.ps1 (and the scheduled task) start ``scripts/email_bot.py``
and the maintenance scripts find the running bot by that name. Equivalent: ``eqr bot``.
"""

import sys
from pathlib import Path

# make src/ importable when run as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from equity_research.bot.app import main  # noqa: E402

if __name__ == "__main__":
    main()
