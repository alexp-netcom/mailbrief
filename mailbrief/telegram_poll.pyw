"""Hidden launcher for the Telegram poll - run with pythonw.exe, no console.

The scheduled task calls: <pythonw.exe> <this file>. pythonw has no console
window and no stdout, so this file redirects stdout/stderr to the poll log
itself before running the normal `mailbrief telegram` command. Without the
redirect, any print() under pythonw crashes. cwd does not matter - the repo
root is derived from this file's location.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mailbrief.config import data_dir  # noqa: E402
from mailbrief.__main__ import main  # noqa: E402

log_dir = data_dir() / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
sys.stdout = sys.stderr = open(  # noqa: SIM115 - closed by process exit
    log_dir / "telegram-poll.out.txt", "a", encoding="utf-8", buffering=1
)
sys.exit(main(["telegram"]))
