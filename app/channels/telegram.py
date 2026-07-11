"""Telegram long-polling adapter.

Long-polling means no public URL / webhook is needed — ideal for prototyping on a
laptop behind NAT. The chat_id whitelist is the first security control: the agent
only ever responds to Stephanie's own chat.
"""

import asyncio
import logging

from langchain_core.messages import HumanMessage
from sqlmodel import Session
from telegram import Update
from telegram.ext import (
    Application,
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


class TelegramChannel(Channel):
    def __init__(self) -> None:
        self.agent = build_agent()

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == settings.telegram_chat_id

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
        # thread_id keys the durable conversation in the checkpointer.
        config = {"configurable": {"thread_id": str(update.effective_chat.id)}}

        # The graph runs synchronously (sync Postgres checkpointer); offload it to a
        # worker thread so the bot's event loop stays responsive.
        result = await asyncio.to_thread(
            self.agent.invoke,
            {"messages": [HumanMessage(text)]},
            config,
        )
        reply = result["messages"][-1].content
        await update.message.reply_text(reply)

        chat_id = update.effective_chat.id
        await asyncio.to_thread(self._log_turn, chat_id, text, reply)

    def _log_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        with Session(get_engine()) as session:
            store.log_message(session, chat_id=chat_id, role="user", text=user_text)
            store.log_message(session, chat_id=chat_id, role="assistant", text=assistant_text)

    def run(self) -> None:
        app = Application.builder().token(settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("start", self._on_start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        log.info("Telegram channel started (long-polling). Whitelisted chat: %s", settings.telegram_chat_id)
        app.run_polling()
