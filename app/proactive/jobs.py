"""The reminder-tick — the proactive push. Deterministic, no LLM.

Runs on the bot's JobQueue every ~60s. It re-derives "what's due" from Postgres
each run (stateless → survives restarts; a reminder due during downtime fires on
the next tick). Each reminder is sent in its own try/except so one bad row can't
wedge all future proactivity. Nudge is sent THEN marked (bias to never-lost over
never-duplicated).
"""

import logging
from datetime import datetime, timezone

from sqlmodel import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.memory import store
from app.memory.db import get_engine
from app.proactive import reminders

log = logging.getLogger(__name__)

# Runtime kill-switch, initialized from config, toggled live by /pause /resume.
_enabled = settings.proactivity_enabled


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def _keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"rem:done:{reminder_id}"),
                InlineKeyboardButton("⏰ Snooze", callback_data=f"rem:snooze:{reminder_id}"),
            ]
        ]
    )


async def run_reminder_tick(bot, session: Session, chat_id: int, now: datetime | None = None) -> int:
    """Send nudges for due reminders. Testable core: pass a mock bot + scratch
    session. Returns the number of nudges sent."""
    now = now or datetime.now(timezone.utc)
    if not _enabled:
        return 0
    if reminders.in_quiet_hours(now.astimezone()):
        return 0

    sent = 0
    for reminder in reminders.due_reminders(session, now):
        try:
            await bot.send_message(
                chat_id=chat_id, text=f"🔔 {reminder.text}", reply_markup=_keyboard(reminder.id)
            )
            reminders.mark_fired(session, reminder, now)  # send THEN mark → bias to not-lost
            store.log_message(session, chat_id=chat_id, role="assistant", text=f"[reminder] {reminder.text}")
            sent += 1
        except Exception:  # one failure must not abort the tick / stop future proactivity
            log.exception("reminder %s failed to send; continuing", reminder.id)
    return sent


async def reminder_tick_job(context) -> None:
    """PTB JobQueue callback — wraps the core with a real session + bot. Top-level
    try/except so a failure never stops the scheduler from running again."""
    try:
        with Session(get_engine()) as session:
            await run_reminder_tick(context.bot, session, settings.telegram_chat_id)
    except Exception:
        log.exception("reminder_tick_job crashed; will retry next interval")
