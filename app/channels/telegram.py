"""Telegram long-polling adapter.

Long-polling means no public URL / webhook is needed — ideal for prototyping on a
laptop behind NAT. The chat_id whitelist is the first security control: the agent
only ever responds to Stephanie's own chat.

Phase 2 adds the human-in-the-loop approval flow: when a tool (e.g. create_draft)
calls interrupt(), the graph pauses and streaming surfaces an `__interrupt__` update.
We surface the proposal with Approve/Reject buttons; the button press resumes the
graph via Command(resume=...). thread_id is constant per chat (whitelist), so the
resume always targets the right paused conversation.

This module is the core: authorization, the plain-message turn, the turn log, and
handler registration. The rest of the channel lives beside it, split out because one
670-line class made the genuinely intricate part (streaming) hard to find and harder
to test:

  telegram_stream.py    live token streaming, status breadcrumbs, delivery
  telegram_commands.py  the nine slash commands
  telegram_buttons.py   inline-keyboard callbacks (reminders, signals, approvals)

They are mixins rather than collaborators because they are all *handlers* — they share
one object's identity (`self.agent`, the whitelist, the turn log) and are wired to
python-telegram-bot as bound methods. `ChannelContract` in `channels/base.py` states
what they may assume about each other.
"""

import asyncio
import logging
from datetime import time as dt_time

from langchain_core.messages import HumanMessage
from sqlmodel import Session
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from tzlocal import get_localzone

from app.agent.graph import build_agent
from app.channels.base import Channel
from app.channels.telegram_buttons import ButtonsMixin
from app.channels.telegram_commands import CommandsMixin
from app.channels.telegram_stream import StreamingMixin
from app.config import settings
from app.memory import extract, store
from app.memory.db import get_engine
from app.proactive import jobs

log = logging.getLogger(__name__)

# Every slash command Mochi answers, paired with the handler attribute that serves it.
# Declared as data so `run()` can't drift from what's actually implemented, and so a
# test can assert the wiring without starting a bot.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "_on_start"),
    ("pause", "_on_pause"),
    ("resume", "_on_resume"),
    ("ask", "_on_ask"),
    ("sent", "_on_sent"),
    ("build", "_on_build"),
    ("doc", "_on_doc"),
    ("briefing", "_on_briefing"),
)


class TelegramChannel(StreamingMixin, CommandsMixin, ButtonsMixin, Channel):
    def __init__(self) -> None:
        self.agent = build_agent()
        # message_id of each hosted /ask answer → the (de-identified) message history that
        # produced it, so a swipe-reply to that answer can continue the expert thread.
        # In-memory (resets on restart); capped so it can't grow unbounded.
        self._ask_threads: dict[int, list] = {}

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == settings.telegram_chat_id

    def _config(self, chat_id: int) -> dict:
        # One durable conversation per chat; constant thread_id also keys the
        # paused state that Command(resume=...) resolves back to.
        return {"configurable": {"thread_id": str(chat_id)}}

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            log.warning("Ignored message from non-whitelisted chat %s", update.effective_chat.id)
            return

        text = update.message.text
        chat_id = update.effective_chat.id

        # Swipe-reply to a prior /ask answer → continue that expert thread (hosted),
        # instead of the normal local-agent path. Any other reply falls through to the
        # graph, so replying to a normal (possibly personal) message never routes hosted.
        replied_to = update.message.reply_to_message
        if replied_to is not None and replied_to.message_id in self._ask_threads:
            await self._on_ask_followup(chat_id, ctx, self._ask_threads[replied_to.message_id], text)
            return

        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, {"messages": [HumanMessage(text)]}, announce_thinking=True
        )
        if error is not None:
            await self._report_error(chat_id, ctx, error)
            return
        await self._deliver(chat_id, ctx, interrupt_payload, reply, user_text=text)
        if settings.fact_sweep_enabled:
            await self._fact_sweep(text)

    async def _fact_sweep(self, text: str) -> None:
        """Background fact capture: after the reply is delivered, extract any durable facts
        the user stated and store the new ones. Runs off the event loop (no user-facing
        latency) and is fully error-isolated — a sweep failure never affects the turn. This
        is the reliable backstop to the flaky remember_fact tool (see app/memory/extract.py)."""
        def run():
            with Session(get_engine()) as session:
                return extract.sweep_and_store(session, text)

        try:
            stored = await asyncio.to_thread(run)
            if stored:
                log.info("fact sweep stored %d new fact(s)", len(stored))
        except Exception:
            log.exception("fact sweep failed; ignoring")

    def _log_one(self, chat_id: int, role: str, text: str) -> None:
        with Session(get_engine()) as session:
            store.log_message(session, chat_id=chat_id, role=role, text=text)

    async def _log_turn(
        self, chat_id: int, user_text: str | None, assistant_text: str | None
    ) -> None:
        """Record either side of a turn, off the event loop. Both halves are optional
        because they don't always arrive together: `_deliver` logs the user's message
        before the graph may pause for approval, and logs the reply only once it exists."""
        for role, text in (("user", user_text), ("assistant", assistant_text)):
            if text is not None:
                await asyncio.to_thread(self._log_one, chat_id, role, text)

    def run(self) -> None:
        app = Application.builder().token(settings.telegram_bot_token).build()
        for command, handler_name in COMMANDS:
            app.add_handler(CommandHandler(command, getattr(self, handler_name)))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        # Proactive reminder-tick (JobQueue = APScheduler on the bot's own loop).
        app.job_queue.run_repeating(
            jobs.reminder_tick_job, interval=settings.reminder_tick_interval_seconds, first=10
        )
        # Email-signal ingestion (~6h): the quarantined reader scans recent mail and
        # pushes approval asks for anything actionable it finds.
        app.job_queue.run_repeating(
            jobs.signal_ingest_job, interval=settings.signal_scan_interval_seconds, first=30
        )
        # Daily morning briefing — one deterministic digest at the configured local hour.
        app.job_queue.run_daily(
            jobs.daily_briefing_job,
            time=dt_time(hour=settings.briefing_hour, tzinfo=get_localzone()),
        )
        log.info("Telegram channel started (long-polling). Whitelisted chat: %s", settings.telegram_chat_id)
        app.run_polling()
