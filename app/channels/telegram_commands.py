"""Slash commands.

These are deliberately *commands* rather than agent tools where the work needs no tool
selection (`/build`, `/doc`) or must bypass the agent entirely (`/ask`, which never touches
memory or Google so no sensitive-origin data can reach a hosted model).

Privacy note for anything here that talks to a hosted model: only `sanitize.redact`-scrubbed
text leaves, every hosted call is written to `HostedConsult` (surfaced by `/sent`), and a
follow-up that scrubs too much is refused outright. See rule 1 in CLAUDE.md.
"""

import asyncio
import logging
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlmodel import Session, select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.agent import router, sanitize
from app.agent.router import Sensitivity
from app.memory.db import get_engine
from app.memory.models import HostedConsult, WebSearch
from app.proactive import briefing, jobs

log = logging.getLogger(__name__)

# Lightweight system prompt for the /ask generic path — no persona tool/safety block,
# no memory, no history. Kept separate from the graph so /ask never touches sensitive data.
_ASK_SYSTEM = "You are Mochi, Stephanie's helpful assistant. Answer the question clearly and concisely."

_ASK_THREAD_CAP = 50  # how many /ask answers stay swipe-replyable (bounds in-memory growth)


class CommandsMixin:
    """The nine slash commands. Mixed into `TelegramChannel`; see `ChannelContract`."""

    def _remember_ask(self, message, history: list) -> None:
        """Record a hosted answer's message_id → its conversation history, so replying to it
        continues the thread. Cap the store (drop oldest) to bound memory."""
        if message is None:
            return
        self._ask_threads[message.message_id] = history
        if len(self._ask_threads) > _ASK_THREAD_CAP:
            for stale in list(self._ask_threads)[:-_ASK_THREAD_CAP]:
                del self._ask_threads[stale]

    async def _on_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "Hi Stephanie — I'm running locally on your Mac. Say anything."
        )

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
        self._remember_ask(sent, [*messages, AIMessage(content=answer or "")])
        await self._log_turn(chat_id, update.message.text, answer or "")

    async def _on_ask_followup(
        self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, history: list, new_text: str
    ) -> None:
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
        messages = [*history, HumanMessage(content=clean)]

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
        self._remember_ask(sent, [*messages, AIMessage(content=answer or "")])
        await self._log_turn(chat_id, new_text, answer or "")

    async def _on_sent(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show what has actually been sent to the external model (the audit log) — the
        transparency half of the de-identified hybrid."""
        if not self._authorized(update):
            return

        def fetch():
            with Session(get_engine()) as session:
                consults = list(
                    session.exec(select(HostedConsult).order_by(HostedConsult.created_at.desc()).limit(10))
                )
                searches = list(
                    session.exec(select(WebSearch).order_by(WebSearch.created_at.desc()).limit(10))
                )
                return consults, searches

        consults, searches = await asyncio.to_thread(fetch)
        if not consults and not searches:
            await update.message.reply_text(
                "Nothing's been sent externally — everything has stayed local. 🔒"
            )
            return

        def _snip(s: str) -> str:
            return s[:120] + ("…" if len(s) > 120 else "")

        items: list[tuple] = []
        for r in consults:
            extra = f" ({r.n_redactions} redacted)" if r.n_redactions else ""
            items.append((r.created_at, f"💬 ask — {_snip(r.sent_text)}{extra}"))
        for r in searches:
            extra = f" ({r.n_redactions} redacted)" if r.n_redactions else ""
            items.append((r.created_at, f"🔎 search — {_snip(r.query)}{extra}"))
        items.sort(key=lambda x: x[0], reverse=True)

        lines = ["🌐 Recent things sent externally (scrubbed before leaving):"]
        lines += [f"• {created.astimezone():%b %-d %-I:%M %p} — {label}" for created, label in items[:12]]
        await update.message.reply_text("\n".join(lines))

    async def _on_build(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """`/build <description>` — generate + serve a web page/app, reply with the link.
        A command (not an agent tool) because this needs no tool selection, and it predates
        dynamic tool binding. Runs off the loop (codegen + serve are slow)."""
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
        await self._log_turn(chat_id, update.message.text, result)

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
        await self._log_turn(chat_id, update.message.text, None)

    async def _on_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        jobs.set_enabled(False)
        await update.message.reply_text("🔕 Proactive reminders paused. Say /resume to turn them back on.")

    async def _on_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
