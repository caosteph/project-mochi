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
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlmodel import Session, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.agent import router, sanitize
from app.agent.router import Sensitivity
from app.channels.render import render_proposal
from app.memory import store
from app.memory.db import get_engine
from app.memory.models import HostedConsult, WebSearch
from app.proactive import briefing, jobs

log = logging.getLogger(__name__)

_FACTS_LIMIT = 40  # how many facts /facts shows (pinned first); keeps the message phone-readable
_FACT_SNIP = 90    # truncate each fact's text in the /facts list

# Lightweight system prompt for the /ask generic path — no persona tool/safety block,
# no memory, no history. Kept separate from the graph so /ask never touches sensitive data.
_ASK_SYSTEM = "You are Mochi, Stephanie's helpful assistant. Answer the question clearly and concisely."

_ASK_THREAD_CAP = 50  # how many /ask answers stay swipe-replyable (bounds in-memory growth)
_PENDING_ASK_CAP = 20  # how many un-tapped /ask Send/Cancel previews to retain (bounds growth)


class CommandsMixin:
    """The slash commands. Mixed into `TelegramChannel`; see `ChannelContract`."""

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
        went_hosted = router.hosted_available()
        raw = f"[Context — a message I'm replying to]\n{quoted}\n\n{question}" if quoted else question
        payload, hits = sanitize.redact(raw) if went_hosted else (raw, 0)
        # Egress gate: refuse before anything leaves if the payload is too personal to send
        # hosted (a single high-risk identifier, or a dense count). Only applies when hosted;
        # the local path never leaves the machine. Mirrors _on_ask_followup / consult_expert.
        if went_hosted and sanitize.is_too_personal(raw, hits):
            await update.message.reply_text(
                "That's too personal to send to the external model — ask me directly and I'll answer it locally."
            )
            return
        # Egress preview: if hosting scrubbed ANY identifier (but stayed under the refuse bar), show her
        # exactly what will leave and wait for a Send/Cancel tap. A 0-hit generic query auto-sends (nothing
        # identifiable leaves). This is the /ask analogue of web_search's require_approval — /ask runs
        # outside the graph, so it can't use interrupt(); the tap resolves via _on_ask_confirm.
        if went_hosted and hits > 0:
            await self._confirm_ask(update, payload, hits)
            return
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            await self._run_ask(chat_id, ctx, payload, hits, update.message.text, went_hosted=went_hosted)
        except Exception as exc:
            await self._report_error(chat_id, ctx, exc)

    async def _run_ask(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, payload: str, hits: int,
                       user_text: str, *, went_hosted: bool) -> None:
        """Invoke the /ask generic path on an already-decided (scrubbed) payload, audit it if it left
        the machine, render the answer, and remember it as a continuable thread. Shared by the direct
        (auto-allowed) path and the Send-tap path (_on_ask_confirm)."""
        def run():
            messages = [SystemMessage(_ASK_SYSTEM), HumanMessage(payload)]
            answer = router.chat_model(Sensitivity.NON_SENSITIVE, temperature=0.5).invoke(messages).content
            if went_hosted:  # only audit when something actually left the machine
                with Session(get_engine()) as session:
                    session.add(HostedConsult(sent_text=payload, answer=answer, n_redactions=hits))
                    session.commit()
            return answer, messages

        answer, messages = await asyncio.to_thread(run)
        sent = await self._send_rich(ctx.bot, chat_id, answer)
        self._remember_ask(sent, [*messages, AIMessage(content=answer or "")])
        await self._log_turn(chat_id, user_text, answer or "")

    async def _confirm_ask(self, update: Update, payload: str, hits: int) -> None:
        """Stash a scrubbed /ask payload under a token and show a Send/Cancel preview of exactly what
        would leave the machine. Nothing is sent until she taps Send (resolved by _on_ask_confirm)."""
        tok = uuid.uuid4().hex[:8]
        self._pending_ask[tok] = {"payload": payload, "hits": hits, "user_text": update.message.text}
        if len(self._pending_ask) > _PENDING_ASK_CAP:
            for stale in list(self._pending_ask)[:-_PENDING_ASK_CAP]:
                del self._pending_ask[stale]
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Send", callback_data=f"ask:send:{tok}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"ask:cancel:{tok}"),
            ]]
        )
        await update.message.reply_text(
            render_proposal("consult_expert", {"question": payload}), reply_markup=keyboard
        )

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
        if sanitize.is_too_personal(new_text, hits):
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

    async def _on_facts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """`/facts` — show what Mochi knows about you, pinned facts (the always-on profile card)
        first and marked, each with an id you can pass to /pin or /unpin. Read-only."""
        if not self._authorized(update):
            return

        def build() -> str:
            with Session(get_engine()) as session:
                facts = store.list_facts(session, limit=_FACTS_LIMIT)
                total = store.count_facts(session)
                if not facts:
                    return "I haven't stored any facts about you yet."
                lines = []
                for f in facts:
                    text = f.text if len(f.text) <= _FACT_SNIP else f.text[:_FACT_SNIP - 1] + "…"
                    lines.append(f"{'*' if f.pinned else ' '} {f.id}. {text}")
                header = f"Memory — {len(facts)} of {total} facts (* = always-on, in every reply):"
                footer = "Change what's always-on: /pin <number> or /unpin <number>."
                return "\n".join([header, "", *lines, "", footer])

        try:
            text = await asyncio.to_thread(build)
        except Exception as exc:
            await self._report_error(update.effective_chat.id, ctx, exc)
            return
        await update.message.reply_text(text)

    async def _on_pin(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_pin(update, ctx, pinned=True)

    async def _on_unpin(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_pin(update, ctx, pinned=False)

    async def _set_pin(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, pinned: bool) -> None:
        """Shared body of /pin and /unpin: pin/unpin the fact with the given id, then invalidate the
        profile-card cache so the change lands on the very next reply (no restart)."""
        if not self._authorized(update):
            return
        verb = "pin" if pinned else "unpin"
        arg = (update.message.text or "").partition(" ")[2].strip()
        if not arg.isdigit():
            await update.message.reply_text(f"Usage: /{verb} <number> — the number shown by /facts.")
            return
        fact_id = int(arg)

        def run() -> str | None:
            with Session(get_engine()) as session:
                fact = store.set_pinned(session, fact_id=fact_id, pinned=pinned)
                return None if fact is None else fact.text

        try:
            text = await asyncio.to_thread(run)
        except Exception as exc:
            await self._report_error(update.effective_chat.id, ctx, exc)
            return
        if text is None:
            await update.message.reply_text(f"No fact #{fact_id}. Run /facts to see the current list.")
            return
        from app.agent import graph

        graph.invalidate_profile_card()
        snip = text if len(text) <= _FACT_SNIP else text[:_FACT_SNIP - 1] + "…"
        done = "Pinned — it'll be in every reply now" if pinned else "Unpinned — no longer always-on"
        await update.message.reply_text(f"{done}: {snip}")

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
