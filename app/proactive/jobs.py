"""The reminder-tick — the proactive push. Deterministic, no LLM.

Runs on the bot's JobQueue every ~60s. It re-derives "what's due" from Postgres
each run (stateless → survives restarts; a reminder due during downtime fires on
the next tick). Each reminder is sent in its own try/except so one bad row can't
wedge all future proactivity. Nudge is sent THEN marked (bias to never-lost over
never-duplicated).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import health
from app.config import settings
from app.memory import consolidate, store
from app.memory.db import get_engine
from app.memory.models import ConsolidationRun, EmailSignal, MessageLog, SignalStatus
from app.proactive import briefing, email_signals, reminder_time, reminders

log = logging.getLogger(__name__)

# Runtime kill-switch, initialized from config, toggled live by /pause /resume.
_enabled = settings.proactivity_enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    """For /status, which reports whether proactivity is paused."""
    return _enabled


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
    now = now or datetime.now(UTC)
    if not _enabled:
        return 0
    if reminder_time.in_quiet_hours(now.astimezone()):
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
        health.record_tick()  # only a tick that SUCCEEDED counts — /status must not look alive when it isn't
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
    now = now or datetime.now(UTC)
    if not _enabled:
        return 0
    if reminder_time.in_quiet_hours(now.astimezone()):
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
    now = now or datetime.now(UTC)
    if not _enabled or settings.signal_mode == "off":
        return 0
    shadow = settings.signal_mode == "shadow"
    email_signals.ingest_signals(session, service=service, extractor=extractor, now=now, shadow=shadow)
    if shadow:
        return 0  # shadow observes only — never asks her
    return await send_pending_asks(bot, session, chat_id, now=now)


def _ingest_blocking(shadow: bool) -> None:
    with Session(get_engine()) as session:
        email_signals.ingest_signals(session, shadow=shadow)


async def signal_ingest_job(context) -> None:
    """PTB JobQueue callback (~6h). The heavy part — reading email bodies and running
    the quarantined reader — is offloaded to a worker thread so it never blocks the
    bot loop; the approval asks are then sent from the loop (live mode only). Top-level
    try/except so a failure never stops the scheduler."""
    try:
        if not _enabled or settings.signal_mode == "off":
            return
        shadow = settings.signal_mode == "shadow"
        await asyncio.to_thread(_ingest_blocking, shadow)
        if shadow:
            return  # shadow scans + logs, but never asks her
        with Session(get_engine()) as session:
            await send_pending_asks(context.bot, session, settings.telegram_chat_id)
    except Exception:
        log.exception("signal_ingest_job crashed; will retry next interval")


# --- Phase 6: the daily morning briefing ------------------------------------

async def run_daily_briefing(bot, session: Session, chat_id: int, *, now=None, service=None) -> bool:
    """Send the morning digest — ONE deterministic message. Gated by the /pause
    kill-switch and `briefing_enabled`. Returns True if a briefing was sent. Testable
    core: pass a mock bot + scratch session + mock calendar `service`."""
    now = now or datetime.now(UTC)
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


# --- Memory consolidation (docs/14 #1(c)) — daily, only while she's idle ---------------------


def consolidation_due(session: Session, now: datetime) -> str | None:
    """None if a pass should run now, else why not. A laptop sleeps at night, so this is "once a day
    whenever she's been idle", not a fixed time: an hourly job asks, and only a completed ("done")
    run counts — a yielded or failed one retries at the next idle hour."""
    if settings.consolidation_mode == "off":
        return "off"
    last_user = session.exec(
        select(MessageLog.created_at).where(MessageLog.role == "user")
        .order_by(MessageLog.created_at.desc()).limit(1)
    ).first()
    if last_user is not None and now - last_user < timedelta(minutes=settings.consolidation_idle_minutes):
        return "not idle"
    last_done = session.exec(
        select(ConsolidationRun.started_at).where(ConsolidationRun.outcome == "done")
        .order_by(ConsolidationRun.started_at.desc()).limit(1)
    ).first()
    if last_done is not None and now - last_done < timedelta(hours=settings.consolidation_min_interval_hours):
        return "ran recently"
    return None


def run_consolidation_pass(session: Session, *, judge, now: datetime | None = None) -> dict | None:
    """Run one pass if due; record it; return a plain summary (not the ORM row — it's expired after
    commit). Shadow mode: proposals are logged, memory is untouched. Never raises."""
    now = now or datetime.now(UTC)
    if consolidation_due(session, now) is not None:
        return None
    run = ConsolidationRun(run_id=uuid.uuid4().hex[:8], mode=settings.consolidation_mode, started_at=now)
    session.add(run)
    session.commit()
    run_id, started = run.run_id, run.started_at

    def she_is_back() -> bool:  # she messaged since the run began → stop before the next model call
        return session.exec(
            select(MessageLog.id).where(MessageLog.role == "user", MessageLog.created_at > started).limit(1)
        ).first() is not None

    summary = {"run_id": run_id, "outcome": "", "candidates": 0, "judged": 0, "proposals": 0}
    try:
        result = consolidate.consolidate(
            session, judge=judge, threshold=settings.consolidation_similarity_threshold,
            max_pairs=settings.consolidation_max_pairs, should_yield=she_is_back,
        )
        consolidate.log_shadow(run_id, result.proposals)
        summary.update(outcome="yielded" if result.yielded else "done", candidates=result.candidates,
                       judged=result.judged, proposals=len(result.proposals))
    except Exception as exc:
        log.exception("consolidation run %s failed", run_id)
        summary["outcome"] = f"error: {type(exc).__name__}"
    run = session.get(ConsolidationRun, run.id)
    run.finished_at = datetime.now(UTC)
    run.pairs_judged, run.proposals, run.outcome = summary["judged"], summary["proposals"], summary["outcome"]
    session.add(run)
    session.commit()
    return summary


def _consolidation_blocking() -> dict | None:
    from app.agent.graph import consolidation_judge  # lazy: the agent layer owns model selection

    with Session(get_engine()) as session:
        return run_consolidation_pass(session, judge=consolidation_judge())


async def consolidation_job(_context) -> None:
    """PTB JobQueue callback (hourly). The pass makes many local-model calls, so it runs off the loop.
    Top-level try/except so a failure never stops the scheduler."""
    try:
        if settings.consolidation_mode == "off":
            return
        summary = await asyncio.to_thread(_consolidation_blocking)
        if summary:
            log.info("consolidation run %(run_id)s %(outcome)s: %(candidates)d candidates, %(judged)d judged, "
                     "%(proposals)d proposals", summary)
    except Exception:
        log.exception("consolidation_job crashed; will retry next interval")
