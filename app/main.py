"""Entrypoint. Run with: `uv run python -m app.main` (or `python -m app.main`).

Normally started by launchd (`launchd/com.mochi.agent.plist`), which restarts it on exit.
A single-instance lock makes it safe for that to overlap with a manual start.
"""

import fcntl
import logging
import os
import sys
from pathlib import Path

from app.channels.telegram import TelegramChannel
from app.warmup import start_keep_warm

log = logging.getLogger(__name__)

LOCK_PATH = Path(__file__).resolve().parent.parent / "data" / "mochi.lock"

# Held open for the life of the process — closing the handle releases the lock, so this
# module-level reference is load-bearing, not a leak.
_lock_handle = None


def acquire_single_instance_lock(path: Path | str | None = None) -> bool:
    """Take an exclusive, non-blocking lock. False if another instance already holds it.

    WHY: two processes polling the same Telegram bot token both answer every message, so
    Stephanie sees each reply twice ("you also just duplicated the message that you just sent
    me"). That happened when a manual run overlapped the launchd agent. The OS lock makes a
    second instance impossible rather than merely discouraged, and it's released automatically
    if the process dies, so launchd's KeepAlive restart still works.
    """
    global _lock_handle
    lock_path = Path(path) if path is not None else LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not acquire_single_instance_lock():
        log.error(
            "Another Mochi instance is already running (lock: %s). Exiting — two pollers on one "
            "bot token answer every message twice.",
            LOCK_PATH,
        )
        sys.exit(1)
    start_keep_warm()  # keep the local model resident so idle-gap replies aren't slow
    TelegramChannel().run()


if __name__ == "__main__":
    main()
