"""The streaming engine: how a graph run becomes live text in a Telegram chat.

Split out of `telegram.py` because it is the one genuinely intricate part of the channel —
a sync generator consumed by an async loop, two message surfaces edited in place, and a
throttle — and it was previously buried in a 670-line class alongside nine slash commands.

Everything here is transport. No agent logic, no persistence beyond the turn log.
"""

import asyncio
import contextlib
import logging
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.channels.render import (
    balance_markdown,
    chunk,
    render_proposal,
    status_for_tool,
    to_markdown_v2,
)
from app.config import settings

log = logging.getLogger(__name__)

_TELEGRAM_TEXT_CAP = 4000  # a little under Telegram's 4096 so an edit never 400s on length
_EDIT_THROTTLE_SECONDS = 1.0  # how often the streaming reply may be re-edited


class StreamingMixin:
    """Live-reply streaming, rich sending, and turn delivery.

    Mixed into `TelegramChannel`; see `ChannelContract` in `channels/base.py` for the
    members it assumes exist (`self.agent`, `_config`, `_log_turn`).
    """

    async def _send_rich(self, bot, chat_id: int, text: str):
        """Send model text rendered as Telegram MarkdownV2 (bold/bullets/code, and tables
        as aligned monospace code blocks). Falls back to plain text on any parse error, and
        chunks anything over Telegram's length limit — so a message never fails to deliver.
        Returns the (last) sent Message so its id can anchor a reply-thread."""
        text = text or "…"
        formatted = to_markdown_v2(text)
        if formatted:
            try:
                return await bot.send_message(chat_id=chat_id, text=formatted, parse_mode="MarkdownV2")
            except Exception:  # malformed MarkdownV2 (BadRequest) → plain fallback
                log.warning("MarkdownV2 send failed; falling back to plain text", exc_info=True)
        last = None
        for piece in chunk(text):
            last = await bot.send_message(chat_id=chat_id, text=piece)
        return last

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

        status_msg_id: int | None = None  # breadcrumb message
        reply_msg_id: int | None = None  # streaming-reply message
        shown_reply = ""  # last text put in the reply message (avoids no-op edits)

        async def set_status(text: str) -> None:
            nonlocal status_msg_id
            try:
                if status_msg_id is None:
                    msg = await ctx.bot.send_message(chat_id=chat_id, text=text)
                    status_msg_id = msg.message_id
                else:
                    await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg_id)
            except Exception:  # status is best-effort, never fatal
                pass

        async def show_reply(text: str) -> None:
            nonlocal reply_msg_id, shown_reply
            text = text.strip()
            if not text or text == shown_reply:
                return
            text = text[:_TELEGRAM_TEXT_CAP]  # the final reply is short in practice
            shown_reply = text
            # Format DURING the stream: balance the half-written markers, then try MarkdownV2 and
            # fall back to plain. Previously streaming was always plain and formatting only
            # appeared on the final edit, so the reply visibly "popped" into shape at the end.
            formatted = to_markdown_v2(balance_markdown(text))
            for body, mode in ((formatted, "MarkdownV2"), (text, None)):
                if body is None:
                    continue
                try:
                    if reply_msg_id is None:
                        msg = await ctx.bot.send_message(chat_id=chat_id, text=body, parse_mode=mode)
                        reply_msg_id = msg.message_id
                    else:
                        await ctx.bot.edit_message_text(
                            body, chat_id=chat_id, message_id=reply_msg_id, parse_mode=mode
                        )
                    return
                except Exception:
                    continue  # malformed MarkdownV2 → retry the same content as plain text

        stop = asyncio.Event()

        async def keep_typing():
            while not stop.is_set():
                # The indicator is cosmetic: a failed send must never break the turn, and the
                # 4s wait timing out is the normal path (it means the turn is still running).
                with contextlib.suppress(Exception):
                    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=4.0)

        t_start = loop.time()
        ttft = None  # time-to-first-token
        n_tool_steps = 0
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
                            n_tool_steps += 1
                            await set_status(status_for_tool(tool_names[-1]))
                        elif msg.content:
                            reply = msg.content  # authoritative final text
                    continue

                # mode == "messages": (message_chunk, metadata) — live tokens.
                chunk_msg, meta = payload
                if meta.get("langgraph_node") == "agent" and getattr(chunk_msg, "content", None):
                    if ttft is None:
                        ttft = loop.time() - t_start
                    reply_buf += chunk_msg.content
                    now = loop.time()
                    if now - last_edit >= _EDIT_THROTTLE_SECONDS:  # throttle Telegram edits
                        last_edit = now
                        await show_reply(reply_buf)
        finally:
            stop.set()
            await typing
        if settings.latency_log:
            ttft_s = f"{ttft:.1f}s" if ttft is not None else "n/a"
            log.info("latency: turn total=%.1fs ttft=%s tool_steps=%d",
                     loop.time() - t_start, ttft_s, n_tool_steps)

        # Show the full authoritative reply. Streaming is already formatted (show_reply balances
        # the half-written markers), but it is throttled, so the last tokens may not have been
        # displayed — this final edit guarantees the complete text is what's on screen, and
        # re-renders it without the balancing hack now that the markers are genuinely closed.
        if interrupt_payload is None and error is None:
            final_text = (reply or reply_buf or "Done.").strip()
            await show_reply(final_text)
            if reply_msg_id is not None and final_text:
                try:
                    formatted = to_markdown_v2(final_text)
                    if formatted and formatted.strip() != final_text:
                        await ctx.bot.edit_message_text(
                            formatted, chat_id=chat_id, message_id=reply_msg_id, parse_mode="MarkdownV2"
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
            await self._log_turn(chat_id, user_text, None)

        if interrupt_payload is not None:
            proposal = render_proposal(
                interrupt_payload.get("action", ""), interrupt_payload.get("details", {})
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
        await self._log_turn(chat_id, None, reply or "Done.")

    async def _report_error(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
        log.error("Graph run failed", exc_info=error)
        await ctx.bot.send_message(
            chat_id=chat_id, text="⚠️ Something went wrong on my end — mind trying again?"
        )
