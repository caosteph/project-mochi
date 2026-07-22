"""Inline-keyboard callbacks: the "press a button" half of the human-in-the-loop design.

Three kinds of button reach `_on_callback`, distinguished by their callback_data prefix:
  rem:  — Done / Snooze on a fired reminder
  sig:  — Approve / Reject a proactively-detected email signal
  (none) — Approve / Reject a paused graph interrupt (e.g. an email draft)

The last one is the safety-critical path: it resumes a LangGraph `interrupt()`, which is
what stands between "Mochi proposes" and "Mochi acts". See rule 3 in CLAUDE.md.
"""

import asyncio
import contextlib
import logging

from langgraph.types import Command
from sqlmodel import Session
from telegram import Update
from telegram.ext import ContextTypes

from app.channels.render import render_resolved_choice
from app.memory.db import get_engine
from app.memory.models import EmailSignal, SignalStatus
from app.proactive import reminders

log = logging.getLogger(__name__)


class ButtonsMixin:
    """Callback-query handling. Mixed into `TelegramChannel`; see `ChannelContract`."""

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(update):
            await query.answer()
            return
        chat_id = update.effective_chat.id

        data = query.data or ""
        if data.startswith("rem:"):
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)  # no double-taps
            await self._on_reminder_button(chat_id, ctx, data)
            return
        if data.startswith("sig:"):
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
            await self._on_signal_button(chat_id, ctx, data)
            return
        if data.startswith("ans:"):
            await self._resume_choice(chat_id, ctx, query, int(data.split(":")[1]))
            return
        # Otherwise it's a draft approve/reject: resume the paused graph.
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, Command(resume={"approved": data == "approve"}), announce_thinking=False
        )
        if error is not None:
            await self._report_error(chat_id, ctx, error)
            return
        await self._deliver(chat_id, ctx, interrupt_payload, reply)

    async def _resume_choice(self, chat_id, ctx, query, choice: int) -> None:
        """She tapped one of a choice question's option buttons. Toast for instant feedback,
        rewrite the question to show what she picked (no dangling dead buttons), then resume the
        paused graph with her index so the waiting tool returns the chosen option to the model."""
        chosen = ""
        if query.message and query.message.reply_markup:
            buttons = [b for row in query.message.reply_markup.inline_keyboard for b in row]
            if 0 <= choice < len(buttons):
                chosen = buttons[choice].text
        await query.answer(text=f"Got it — {chosen}" if chosen else "Got it")
        if query.message:
            with contextlib.suppress(Exception):  # cosmetic; a failed edit must not drop the turn
                await query.edit_message_text(
                    render_resolved_choice(query.message.text or "", chosen)
                )

        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, Command(resume={"choice": choice}), announce_thinking=False
        )
        if error is not None:
            await self._report_error(chat_id, ctx, error)
            return
        await self._deliver(chat_id, ctx, interrupt_payload, reply)

    async def _on_reminder_button(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
        # data is "rem:done:<id>" or "rem:snooze:<id>"
        _, action, rid = data.split(":")
        reminder_id = int(rid)

        def apply():
            # Read due_at INSIDE the session and return a plain value. session.commit()
            # expires the instance, so touching reminder.due_at after the `with` block
            # raises DetachedInstanceError — which is exactly what used to happen here:
            # the snooze was written but the confirmation message blew up, so Stephanie
            # pressed Snooze and got silence. (Matches _on_signal_button, already correct.)
            with Session(get_engine()) as session:
                if action == "done":
                    return ("done", None) if reminders.mark_done(session, reminder_id) else (None, None)
                reminder = reminders.snooze(session, reminder_id)
                return ("snooze", reminder.due_at) if reminder else (None, None)

        kind, due_at = await asyncio.to_thread(apply)
        if kind is None:
            await ctx.bot.send_message(chat_id=chat_id, text="That reminder's already gone.")
        elif kind == "done":
            await ctx.bot.send_message(chat_id=chat_id, text="✅ Marked done.")
        else:
            await ctx.bot.send_message(
                chat_id=chat_id, text=f"⏰ Snoozed — I'll remind you again {due_at.astimezone():%a %-I:%M %p}."
            )

    async def _on_signal_button(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
        # data is "sig:approve:<id>" or "sig:reject:<id>" — Stephanie's yes/no to a
        # proactively-detected email signal. Approve → create the reminder; reject →
        # dismiss. DB work off the event loop, matching the reminder-button handler.
        _, action, sid = data.split(":")
        signal_id = int(sid)

        def apply():
            with Session(get_engine()) as session:
                signal = session.get(EmailSignal, signal_id)
                if signal is None:
                    return None
                if action == "approve":
                    reminder = reminders.create_from_signal(session, signal)
                    return ("approve", reminder.text, reminder.due_at)
                signal.status = SignalStatus.DISMISSED.value
                session.add(signal)
                session.commit()
                return ("reject", None, None)

        result = await asyncio.to_thread(apply)
        if result is None:
            await ctx.bot.send_message(chat_id=chat_id, text="That one's already gone.")
            return
        kind, text, due_at = result
        if kind == "approve":
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"✅ I'll remind you — {text} ({due_at.astimezone():%a %b %-d, %-I:%M %p}).",
            )
        else:
            await ctx.bot.send_message(chat_id=chat_id, text="👍 Skipped — I won't set that one.")
