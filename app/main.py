"""Entrypoint. Run with: `uv run python -m app.main` (or `python -m app.main`).

Phase 0: just the Telegram channel + agent. The APScheduler for proactive jobs
gets wired in here in Phase 3.
"""

import logging

from app.channels.telegram import TelegramChannel
from app.warmup import start_keep_warm


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start_keep_warm()  # keep the local model resident so idle-gap replies aren't slow
    TelegramChannel().run()


if __name__ == "__main__":
    main()
