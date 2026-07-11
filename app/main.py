"""Entrypoint. Run with: `uv run python -m app.main` (or `python -m app.main`).

Phase 0: just the Telegram channel + agent. The APScheduler for proactive jobs
gets wired in here in Phase 3.
"""

import logging

from app.channels.telegram import TelegramChannel


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    TelegramChannel().run()


if __name__ == "__main__":
    main()
