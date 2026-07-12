"""Telegram long-polling adapter.

Long-polling means no public URL / webhook is needed — ideal for prototyping on a
laptop behind NAT. The chat_id whitelist is the first security control: the agent
only ever responds to Stephanie's own chat.

Phase 2 adds the human-in-the-loop approval flow: when a tool (e.g. create_draft)
calls interrupt(), the graph pauses and streaming surfaces an `__interrupt__` update.
We surface the proposal with Approve/Reject buttons; the button press resumes the
graph via Command(resume=...). thread_id is constant per chat (whitelist), so the
resume always targets the right paused conversation.

Because the local model is slow, we stream the graph (stream_mode="updates") instead
of a blocking invoke: the moment Mochi decides to use a tool, we post a small status
breadcrumb ("📅 Checking your calendar…") so she's never staring at silence, plus a
"typing…" indicator between steps. Telegram's native status line only allows fixed
built-in actions (no custom text), so named statuses are ordinary chat messages, left
in place as a breadcrumb trail.
"""

import asyncio
import logging
import threading

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlmodel import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.agent.graph import build_agent
from app.channels.base import Channel
from app.config import settings
from app.memory import store
from app.memory.db import get_engine

log = logging.getLogger(__name__)

# Friendly, present-tense status shown when Mochi starts using a tool. Kept short —
# these are breadcrumbs, not sentences.
_TOOL_STATUS = {
    "calendar_list_events": "📅 Checking your calendar…",
    "gmail_list_recent": "📬 Looking through your inbox…",
    "create_draft": "✉️ Drafting that email…",
    "recall": "🧠 Checking what I remember…",
    "remember_fact": "🧠 Noting that down…",
    "add_goal": "🎯 Adding that goal…",
    "add_task": "✅ Adding that task…",
}


def status_for_tool(name: str) -> str:
    return _TOOL_STATUS.get(name, "⏳ Working on it…")


class TelegramChannel(Channel):
    def __init__(self) -> None:
        self.agent = build_agent()

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == settings.telegram_chat_id

    def _config(self, chat_id: int) -> dict:
        # One durable conversation per chat; constant thread_id also keys the
        # paused state that Command(resume=...) resolves back to.
        return {"configurable": {"thread_id": str(chat_id)}}

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Hi Stephanie — I'm running locally on your Mac. Say anything."
        )

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            log.warning("Ignored message from non-whitelisted chat %s", update.effective_chat.id)
            return

        text = update.message.text
        chat_id = update.effective_chat.id
        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, {"messages": [HumanMessage(text)]}, announce_thinking=True
        )
        if error is not None:
            await self._report_error(chat_id, ctx, error)
            return
        await self._deliver(chat_id, ctx, interrupt_payload, reply, user_text=text)

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            return

        approved = query.data == "approve"
        chat_id = update.effective_chat.id
        # Strip the buttons so the decision can't be double-tapped.
        await query.edit_message_reply_markup(reply_markup=None)

        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, Command(resume={"approved": approved}), announce_thinking=False
        )
        if error is not None:
            await self._report_error(chat_id, ctx, error)
            return
        await self._deliver(chat_id, ctx, interrupt_payload, reply)

    async def _run_with_status(
        self,
        chat_id: int,
        ctx: ContextTypes.DEFAULT_TYPE,
        graph_input,
        announce_thinking: bool,
    ):
        """Stream the graph and narrate what Mochi is doing through a single status
        message that's edited in place across phases — 💭 Thinking → 📅 (a tool) →
        ✍️ Composing — so even a no-tool turn shows activity. The status message is
        left in the chat (a breadcrumb of the last phase), not deleted. The stream is
        a sync generator, so it runs on a worker thread and hands events to this async
        consumer via a queue. Returns (interrupt_payload, reply, error)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for update in self.agent.stream(
                    graph_input, self._config(chat_id), stream_mode="updates"
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("update", update))
            except Exception as exc:  # surfaced to the user instead of a silent no-reply
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        # One status message, edited in place as phases progress.
        status_msg_id: list[int | None] = [None]

        async def set_status(text: str) -> None:
            try:
                if status_msg_id[0] is None:
                    msg = await ctx.bot.send_message(chat_id=chat_id, text=text)
                    status_msg_id[0] = msg.message_id
                else:
                    await ctx.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=status_msg_id[0]
                    )
            except Exception:  # editing/sending status is best-effort, never fatal
                pass

        stop = asyncio.Event()

        async def keep_typing():
            while not stop.is_set():
                try:
                    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    pass

        typing = asyncio.create_task(keep_typing())
        if announce_thinking:
            await set_status("💭 Thinking…")
        threading.Thread(target=worker, daemon=True).start()

        interrupt_payload = None
        reply = None
        error = None
        try:
            while True:
                kind, data = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    error = data
                    continue  # keep draining until 'done'
                update = data
                if "__interrupt__" in update:
                    interrupt_payload = update["__interrupt__"][0].value
                    continue
                if "tools" in update:
                    # A read tool just finished; the model is about to compose.
                    await set_status("✍️ Composing your reply…")
                    continue
                agent_payload = update.get("agent")
                if agent_payload and agent_payload.get("messages"):
                    msg = agent_payload["messages"][-1]
                    tool_names = [tc["name"] for tc in (getattr(msg, "tool_calls", None) or [])]
                    if tool_names:
                        await set_status(status_for_tool(tool_names[-1]))
                    elif msg.content:
                        reply = msg.content
        finally:
            stop.set()
            await typing

        return interrupt_payload, reply, error

    async def _deliver(
        self,
        chat_id: int,
        ctx: ContextTypes.DEFAULT_TYPE,
        interrupt_payload: dict | None,
        reply: str | None,
        user_text: str | None = None,
    ) -> None:
        """Send the graph's output. If it paused for approval, show the proposal
        with Approve/Reject (the assistant reply comes later, after approval);
        otherwise send the reply. Logs the turn."""
        if user_text is not None:
            await asyncio.to_thread(self._log_one, chat_id, "user", user_text)

        if interrupt_payload is not None:
            details = interrupt_payload.get("details", {})
            proposal = (
                "📝 Draft to approve (it will not be sent):\n\n"
                f"To: {details.get('to')}\n"
                f"Subject: {details.get('subject')}\n\n"
                f"{details.get('body')}"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data="approve"),
                        InlineKeyboardButton("❌ Reject", callback_data="reject"),
                    ]
                ]
            )
            await ctx.bot.send_message(chat_id=chat_id, text=proposal, reply_markup=keyboard)
            return

        reply = reply or "Done."
        await ctx.bot.send_message(chat_id=chat_id, text=reply)
        await asyncio.to_thread(self._log_one, chat_id, "assistant", reply)

    async def _report_error(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
        log.error("Graph run failed", exc_info=error)
        await ctx.bot.send_message(
            chat_id=chat_id, text="⚠️ Something went wrong on my end — mind trying again?"
        )

    def _log_one(self, chat_id: int, role: str, text: str) -> None:
        with Session(get_engine()) as session:
            store.log_message(session, chat_id=chat_id, role=role, text=text)

    def run(self) -> None:
        app = Application.builder().token(settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("start", self._on_start))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        log.info("Telegram channel started (long-polling). Whitelisted chat: %s", settings.telegram_chat_id)
        app.run_polling()
