"""The reminder-tick — the proactive push. Deterministic, no LLM.

Runs on the bot's JobQueue every ~60s. It re-derives "what's due" from Postgres
each run (stateless → survives restarts; a reminder due during downtime fires on
the next tick). Each reminder is sent in its own try/except so one bad row can't
wedge all future proactivity. Nudge is sent THEN marked (bias to never-lost over
never-duplicated).
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.memory import store
from app.memory.db import get_engine
from app.memory.models import EmailSignal, SignalStatus
from app.proactive import briefing, email_signals, reminders

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


# --- Phase 3B: email-signal ingestion + proactive approval asks -------------

def _signal_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"sig:approve:{signal_id}"),
                InlineKeyboardButton("❌ No", callback_data=f"sig:reject:{signal_id}"),
            ]
        ]
    )


async def send_pending_asks(bot, session: Session, chat_id: int, now: datetime | None = None) -> int:
    """Push the approval ask for each detected-but-not-yet-asked signal, flipping it to
    ASKED so it's never re-asked. Respects the /pause kill-switch and quiet hours (a
    deferred ask just waits as DETECTED for the next non-quiet tick). Capped per run.
    Each send in its own try/except. Returns the number of asks sent."""
    now = now or datetime.now(timezone.utc)
    if not _enabled:
        return 0
    if reminders.in_quiet_hours(now.astimezone()):
        return 0

    pending = session.exec(
        select(EmailSignal)
        .where(EmailSignal.status == SignalStatus.DETECTED.value)
        .order_by(EmailSignal.created_at)
    ).all()

    asked = 0
    for signal in pending:
        if asked >= settings.signal_max_per_scan:
            break
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=email_signals.suggest_text(signal),
                reply_markup=_signal_keyboard(signal.id),
            )
            signal.status = SignalStatus.ASKED.value
            session.add(signal)
            session.commit()
            store.log_message(session, chat_id=chat_id, role="assistant", text=f"[signal] {signal.title}")
            asked += 1
        except Exception:  # one failed ask must not abort the rest
            log.exception("failed to send signal ask for %s; continuing", signal.id)
    return asked


async def run_signal_ingest_tick(
    bot, session: Session, chat_id: int, *, service=None, extractor=None, now: datetime | None = None
) -> int:
    """Testable core: scan for new signals, then push any pending approval asks. In
    tests, pass a mock bot + a mock Gmail `service` + a fake `extractor` for a fully
    offline run. Returns the number of asks sent."""
    now = now or datetime.now(timezone.utc)
    if not _enabled or not settings.signal_scanning_enabled:
        return 0
    email_signals.ingest_signals(session, service=service, extractor=extractor, now=now)
    return await send_pending_asks(bot, session, chat_id, now=now)


def _ingest_blocking() -> None:
    with Session(get_engine()) as session:
        email_signals.ingest_signals(session)


async def signal_ingest_job(context) -> None:
    """PTB JobQueue callback (~6h). The heavy part — reading email bodies and running
    the quarantined reader — is offloaded to a worker thread so it never blocks the
    bot loop; the approval asks are then sent from the loop. Top-level try/except so a
    failure never stops the scheduler."""
    try:
        if not _enabled or not settings.signal_scanning_enabled:
            return
        await asyncio.to_thread(_ingest_blocking)
        with Session(get_engine()) as session:
            await send_pending_asks(context.bot, session, settings.telegram_chat_id)
    except Exception:
        log.exception("signal_ingest_job crashed; will retry next interval")


# --- Phase 6: the daily morning briefing ------------------------------------

async def run_daily_briefing(bot, session: Session, chat_id: int, *, now=None, service=None) -> bool:
    """Send the morning digest — ONE deterministic message. Gated by the /pause
    kill-switch and `briefing_enabled`. Returns True if a briefing was sent. Testable
    core: pass a mock bot + scratch session + mock calendar `service`."""
    now = now or datetime.now(timezone.utc)
    if not _enabled or not settings.briefing_enabled:
        return False
    text = briefing.build_briefing(session, now=now, service=service)
    await bot.send_message(chat_id=chat_id, text=text)
    store.log_message(session, chat_id=chat_id, role="assistant", text="[briefing] daily digest")
    return True


async def daily_briefing_job(context) -> None:
    """PTB JobQueue callback — fires once a day (run_daily) at the configured hour.
    Top-level try/except so a failure never stops tomorrow's run."""
    try:
        with Session(get_engine()) as session:
            await run_daily_briefing(context.bot, session, settings.telegram_chat_id)
    except Exception:
        log.exception("daily_briefing_job crashed; will retry tomorrow")
