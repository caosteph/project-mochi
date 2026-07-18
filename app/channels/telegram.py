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
import os
import threading
from datetime import time as dt_time

import telegramify_markdown
from tzlocal import get_localzone
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command
from sqlmodel import Session, select
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

from app.agent import router, sanitize
from app.agent.graph import build_agent
from app.agent.router import Sensitivity
from app.channels.base import Channel
from app.config import settings
from app.memory import extract, store
from app.memory.db import get_engine
from app.memory.models import EmailSignal, HostedConsult, SignalStatus
from app.proactive import briefing, jobs, reminders

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
    "add_reminder": "⏰ Setting that reminder…",
    "list_reminders": "📋 Checking your reminders…",
    "cancel_reminder": "🗑️ Cancelling that reminder…",
    "consult_expert": "🧭 Consulting a bigger model…",
    "build_web_app": "🛠️ Building that…",
    "make_document": "📄 Putting that document together…",
    "serve_project": "🌐 Serving that up…",
    "list_projects": "📁 Checking what I've built…",
}

# Lightweight system prompt for the /ask generic path — no persona tool/safety block,
# no memory, no history. Kept separate from the graph so /ask never touches sensitive data.
_ASK_SYSTEM = "You are Mochi, Stephanie's helpful assistant. Answer the question clearly and concisely."


def status_for_tool(name: str) -> str:
    return _TOOL_STATUS.get(name, "⏳ Working on it…")


_TG_LIMIT = 4096  # Telegram's max message length


class TelegramChannel(Channel):
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

    async def _send_rich(self, bot, chat_id: int, text: str):
        """Send model text rendered as Telegram MarkdownV2 (bold/bullets/code, and tables
        as aligned monospace code blocks). Falls back to plain text on any parse error, and
        chunks anything over Telegram's length limit — so a message never fails to deliver.
        Returns the (last) sent Message so its id can anchor a reply-thread."""
        text = text or "…"
        try:
            formatted = telegramify_markdown.markdownify(text, latex_escape=False)
        except Exception:  # converter hiccup → treat as unformatted
            formatted = None
        if formatted and len(formatted) <= _TG_LIMIT:
            try:
                return await bot.send_message(chat_id=chat_id, text=formatted, parse_mode="MarkdownV2")
            except Exception:  # malformed MarkdownV2 (BadRequest) → plain fallback
                log.warning("MarkdownV2 send failed; falling back to plain text", exc_info=True)
        last = None
        for i in range(0, len(text), 4000):
            last = await bot.send_message(chat_id=chat_id, text=text[i : i + 4000])
        return last

    def _remember_ask(self, message, history: list) -> None:
        """Record a hosted answer's message_id → its conversation history, so replying to it
        continues the thread. Cap the store (drop oldest) to bound memory."""
        if message is None:
            return
        self._ask_threads[message.message_id] = history
        if len(self._ask_threads) > 50:
            for stale in list(self._ask_threads)[:-50]:
                del self._ask_threads[stale]

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

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            return
        chat_id = update.effective_chat.id
        await query.edit_message_reply_markup(reply_markup=None)  # no double-taps

        data = query.data or ""
        if data.startswith("rem:"):
            await self._on_reminder_button(chat_id, ctx, data)
            return
        if data.startswith("sig:"):
            await self._on_signal_button(chat_id, ctx, data)
            return
        # Otherwise it's a draft approve/reject: resume the paused graph.
        interrupt_payload, reply, error = await self._run_with_status(
            chat_id, ctx, Command(resume={"approved": data == "approve"}), announce_thinking=False
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
            with Session(get_engine()) as session:
                if action == "done":
                    return reminders.mark_done(session, reminder_id), "done"
                return reminders.snooze(session, reminder_id), "snooze"

        reminder, kind = await asyncio.to_thread(apply)
        if reminder is None:
            await ctx.bot.send_message(chat_id=chat_id, text="That reminder's already gone.")
        elif kind == "done":
            await ctx.bot.send_message(chat_id=chat_id, text="✅ Marked done.")
        else:
            await ctx.bot.send_message(
                chat_id=chat_id, text=f"⏰ Snoozed — I'll remind you again {reminder.due_at.astimezone():%a %-I:%M %p}."
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

    async def _on_ask(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The generic-knowledge path: a stateless question routed to the stronger model
        when hosted is available (else local). It never touches memory or Google — so no
        sensitive-origin data can enter — and only a scrubbed payload is ever sent hosted.
        If sent as a reply to a message, that quoted text is added (scrubbed) as context.
        The answer is stored so a swipe-reply to it continues the thread (see _on_message)."""
        if not self._authorized(update):
            return
        question = (update.message.text or "").partition(" ")[2].strip()
        if not question:
            await update.message.reply_text(
                "Ask me a general question and I'll use the stronger model when it's available: "
                "/ask <question>"
            )
            return
        chat_id = update.effective_chat.id
        reply = update.message.reply_to_message
        quoted = (reply.text or "") if reply is not None else ""
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        def run():
            went_hosted = router.hosted_available()
            raw = f"[Context — a message I'm replying to]\n{quoted}\n\n{question}" if quoted else question
            payload, hits = sanitize.redact(raw) if went_hosted else (raw, 0)
            messages = [SystemMessage(_ASK_SYSTEM), HumanMessage(payload)]
            answer = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.5).invoke(messages).content
            if went_hosted:  # only audit when something actually left the machine
                with Session(get_engine()) as session:
                    session.add(HostedConsult(sent_text=payload, answer=answer, n_redactions=hits))
                    session.commit()
            return answer, messages

        try:
            answer, messages = await asyncio.to_thread(run)
        except Exception as exc:
            await self._report_error(chat_id, ctx, exc)
            return
        sent = await self._send_rich(ctx.bot, chat_id, answer)
        self._remember_ask(sent, messages + [AIMessage(content=answer or "")])
        await asyncio.to_thread(self._log_one, chat_id, "user", update.message.text)
        await asyncio.to_thread(self._log_one, chat_id, "assistant", answer or "")

    async def _on_ask_followup(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, history: list, new_text: str) -> None:
        """Continue an /ask thread when Stephanie swipe-replies to a prior hosted answer.
        The new turn is scrubbed and appended to the (already de-identified) history, kept on
        the same NON_SENSITIVE/hosted path with the same fail-closed + audit guarantees."""
        if not router.hosted_available():
            await ctx.bot.send_message(
                chat_id=chat_id, text="The expert model's off right now — ask me normally and I'll answer locally."
            )
            return
        clean, hits = sanitize.redact(new_text)
        if sanitize.is_too_personal(hits):
            await ctx.bot.send_message(chat_id=chat_id, text="That follow-up's too personal to send externally — ask me directly.")
            return
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        messages = history + [HumanMessage(content=clean)]

        def run():
            answer = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.5).invoke(messages).content
            with Session(get_engine()) as session:
                session.add(HostedConsult(sent_text=clean, answer=answer, n_redactions=hits))
                session.commit()
            return answer

        try:
            answer = await asyncio.to_thread(run)
        except Exception as exc:
            await self._report_error(chat_id, ctx, exc)
            return
        sent = await self._send_rich(ctx.bot, chat_id, answer)
        self._remember_ask(sent, messages + [AIMessage(content=answer or "")])
        await asyncio.to_thread(self._log_one, chat_id, "user", new_text)
        await asyncio.to_thread(self._log_one, chat_id, "assistant", answer or "")

    async def _on_sent(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show what has actually been sent to the external model (the audit log) — the
        transparency half of the de-identified hybrid."""
        if not self._authorized(update):
            return

        def fetch():
            with Session(get_engine()) as session:
                return list(
                    session.exec(
                        select(HostedConsult).order_by(HostedConsult.created_at.desc()).limit(10)
                    )
                )

        rows = await asyncio.to_thread(fetch)
        if not rows:
            await update.message.reply_text(
                "Nothing's been sent to the external model — everything has stayed local. 🔒"
            )
            return
        lines = ["🌐 Recent de-identified questions sent externally:"]
        for r in rows:
            snippet = r.sent_text[:120] + ("…" if len(r.sent_text) > 120 else "")
            extra = f"  ({r.n_redactions} redacted)" if r.n_redactions else ""
            lines.append(f"• {r.created_at.astimezone():%b %-d %-I:%M %p} — {snippet}{extra}")
        await update.message.reply_text("\n".join(lines))

    async def _on_build(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """`/build <description>` — generate + serve a web page/app, reply with the link.
        A command (not an agent tool) because the local 7B can't reliably select among 15
        tools; this needs no tool-selection. Runs off the loop (codegen + serve are slow)."""
        if not self._authorized(update):
            return
        description = (update.message.text or "").partition(" ")[2].strip()
        if not description:
            await update.message.reply_text("Tell me what to build: /build a landing page for my bakery")
            return
        from app.agent.tools.builder_tools import build_web_app

        chat_id = update.effective_chat.id
        await ctx.bot.send_message(chat_id=chat_id, text="🛠️ Building that — one moment…")
        try:
            result = await asyncio.to_thread(lambda: build_web_app.invoke({"description": description}))
        except Exception as exc:
            await self._report_error(chat_id, ctx, exc)
            return
        await ctx.bot.send_message(chat_id=chat_id, text=result)
        await asyncio.to_thread(self._log_one, chat_id, "user", update.message.text)
        await asyncio.to_thread(self._log_one, chat_id, "assistant", result)

    async def _on_doc(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """`/doc <description>` — write a document (local model, so personal content stays
        local) and send it as a PDF (or .docx if 'word'/'docx' is mentioned)."""
        if not self._authorized(update):
            return
        description = (update.message.text or "").partition(" ")[2].strip()
        if not description:
            await update.message.reply_text("Tell me what to write: /doc a one-page plan for my week")
            return
        from app.agent.tools import builder_tools
        from app.agent.tools.builder_tools import make_document

        chat_id = update.effective_chat.id
        fmt = "docx" if any(w in description.lower() for w in ("word", "docx", ".doc")) else "pdf"
        await ctx.bot.send_message(chat_id=chat_id, text="📄 Writing that up…")

        def run() -> list[str]:
            make_document.invoke({"description": description, "format": fmt})  # generates content internally
            return builder_tools.drain_artifacts()

        try:
            paths = await asyncio.to_thread(run)
        except Exception as exc:
            await self._report_error(chat_id, ctx, exc)
            return
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    await ctx.bot.send_document(chat_id=chat_id, document=fh, filename=os.path.basename(path))
            except Exception:
                log.exception("failed to send document %s", path)
        await asyncio.to_thread(self._log_one, chat_id, "user", update.message.text)

    async def _on_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        jobs.set_enabled(False)
        await update.message.reply_text("🔕 Proactive reminders paused. Say /resume to turn them back on.")

    async def _on_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        jobs.set_enabled(True)
        await update.message.reply_text("🔔 Proactive reminders back on.")

    async def _on_briefing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """`/briefing` — the morning digest on demand (today's calendar + reminders due
        today + goals/tasks). Deterministic (no model), and works even when proactivity
        is paused, since she explicitly asked for it. Built off the loop (calendar I/O)."""
        if not self._authorized(update):
            return

        def build() -> str:
            with Session(get_engine()) as session:
                return briefing.build_briefing(session)

        try:
            text = await asyncio.to_thread(build)
        except Exception as exc:
            await self._report_error(update.effective_chat.id, ctx, exc)
            return
        await update.message.reply_text(text)

    async def _run_with_status(
        self,
        chat_id: int,
        ctx: ContextTypes.DEFAULT_TYPE,
        graph_input,
        announce_thinking: bool,
    ):
        """Stream the graph with two live surfaces:
        - a status breadcrumb (💭 Thinking → 📅/✉️ a tool) edited in place, left in
          the chat as a record of what Mochi did;
        - the reply itself, streamed token-by-token into a separate message that types
          out live (so the wait feels shorter — the ultimate progress indicator).

        stream_mode=["updates","messages"] gives both node updates (for the status
        breadcrumb + the approval interrupt) and LLM token chunks (for the live reply).
        The stream is a sync generator, so it runs on a worker thread that hands events
        to this async consumer via a queue. Returns (interrupt_payload, reply, error);
        on a plain reply the text is already displayed here, so _deliver only logs it."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for item in self.agent.stream(
                    graph_input, self._config(chat_id), stream_mode=["updates", "messages"]
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("stream", item))
            except Exception as exc:  # surfaced to the user instead of a silent no-reply
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        status_msg_id: list[int | None] = [None]  # breadcrumb message
        reply_msg_id: list[int | None] = [None]  # streaming-reply message

        async def set_status(text: str) -> None:
            try:
                if status_msg_id[0] is None:
                    msg = await ctx.bot.send_message(chat_id=chat_id, text=text)
                    status_msg_id[0] = msg.message_id
                else:
                    await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg_id[0])
            except Exception:  # status is best-effort, never fatal
                pass

        shown_reply = [""]  # last text put in the reply message (avoids no-op edits)

        async def show_reply(text: str) -> None:
            text = text.strip()
            if not text or text == shown_reply[0]:
                return
            text = text[:4000]  # Telegram message cap; the final reply is short in practice
            shown_reply[0] = text
            try:
                if reply_msg_id[0] is None:
                    msg = await ctx.bot.send_message(chat_id=chat_id, text=text)
                    reply_msg_id[0] = msg.message_id
                else:
                    await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=reply_msg_id[0])
            except Exception:
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
        reply_buf = ""
        last_edit = 0.0
        try:
            while True:
                kind, data = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    error = data
                    continue  # keep draining until 'done'

                mode, payload = data
                if mode == "updates":
                    update = payload
                    if "__interrupt__" in update:
                        interrupt_payload = update["__interrupt__"][0].value
                        continue
                    if "tools" in update:
                        # A tool just ran; discard any pre-tool stray tokens so only
                        # the post-tool reply streams.
                        reply_buf = ""
                        continue
                    agent_payload = update.get("agent")
                    if agent_payload and agent_payload.get("messages"):
                        msg = agent_payload["messages"][-1]
                        tool_names = [tc["name"] for tc in (getattr(msg, "tool_calls", None) or [])]
                        if tool_names:
                            await set_status(status_for_tool(tool_names[-1]))
                        elif msg.content:
                            reply = msg.content  # authoritative final text
                    continue

                # mode == "messages": (message_chunk, metadata) — live tokens.
                chunk, meta = payload
                if meta.get("langgraph_node") == "agent" and getattr(chunk, "content", None):
                    reply_buf += chunk.content
                    now = loop.time()
                    if now - last_edit >= 1.0:  # throttle Telegram edits
                        last_edit = now
                        await show_reply(reply_buf)
        finally:
            stop.set()
            await typing

        # Make sure the full, authoritative reply is displayed, then upgrade it in place to
        # rendered Markdown (bold/bullets/code, tables→monospace). Streaming stays plain
        # (partial Markdown is malformed); only this final edit is formatted, and it falls
        # back silently to the plain text already shown if MarkdownV2 won't parse.
        if interrupt_payload is None and error is None:
            final_text = (reply or reply_buf or "Done.").strip()
            await show_reply(final_text)
            if reply_msg_id[0] is not None and final_text:
                try:
                    formatted = telegramify_markdown.markdownify(final_text, latex_escape=False)
                    if 0 < len(formatted) <= _TG_LIMIT and formatted.strip() != final_text:
                        await ctx.bot.edit_message_text(
                            formatted, chat_id=chat_id, message_id=reply_msg_id[0], parse_mode="MarkdownV2"
                        )
                except Exception:
                    pass  # plain reply already shown

        return interrupt_payload, reply, error

    async def _deliver(
        self,
        chat_id: int,
        ctx: ContextTypes.DEFAULT_TYPE,
        interrupt_payload: dict | None,
        reply: str | None,
        user_text: str | None = None,
    ) -> None:
        """Finish the turn. On approval, show the proposal with Approve/Reject (the
        reply comes after approval). Otherwise the reply text was already streamed
        live by _run_with_status, so here we only log it. Logs the turn."""
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

        # The reply was already streamed into the chat by _run_with_status; just log it.
        await asyncio.to_thread(self._log_one, chat_id, "assistant", reply or "Done.")

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
        app.add_handler(CommandHandler("pause", self._on_pause))
        app.add_handler(CommandHandler("resume", self._on_resume))
        app.add_handler(CommandHandler("ask", self._on_ask))
        app.add_handler(CommandHandler("sent", self._on_sent))
        app.add_handler(CommandHandler("build", self._on_build))
        app.add_handler(CommandHandler("doc", self._on_doc))
        app.add_handler(CommandHandler("briefing", self._on_briefing))
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
