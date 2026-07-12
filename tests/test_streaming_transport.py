"""The Telegram streaming transport, tested end-to-end without a phone or the model:
drive the real handler with a mocked `agent.stream` (canned events) + a recording
mock bot, and assert exactly what the user would see — a 💭 status breadcrumb, the
reply streamed into its own message, and (for a gated draft) the Approve/Reject
proposal instead of a reply. This is the "can you test it independently?" guard.
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langgraph.types import Interrupt

from app.channels.telegram import TelegramChannel


def _chunk(text):
    return SimpleNamespace(content=text)


class _RecordingBot:
    def __init__(self):
        self.ops = []  # ("send"|"edit", text, has_buttons)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.ops.append(("send", text, reply_markup is not None))
        return SimpleNamespace(message_id=sum(1 for o in self.ops if o[0] == "send"))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.ops.append(("edit", text, False))

    async def send_chat_action(self, chat_id, action):
        pass


def _channel(stream_items):
    ch = TelegramChannel.__new__(TelegramChannel)
    ch.agent = SimpleNamespace(stream=lambda *a, **k: iter(stream_items))
    ch._log_one = lambda *a, **k: None  # skip DB — we're testing the transport
    return ch


def _ctx():
    bot = _RecordingBot()
    return SimpleNamespace(bot=bot), bot


def test_reply_streams_into_its_own_message():
    async def run():
        items = [
            ("messages", (_chunk("Hello"), {"langgraph_node": "agent"})),
            ("messages", (_chunk(" there!"), {"langgraph_node": "agent"})),
            ("updates", {"agent": {"messages": [AIMessage("Hello there!")]}}),
        ]
        ch = _channel(items)
        ctx, bot = _ctx()
        interrupt, reply, error = await ch._run_with_status(
            1, ctx, {"messages": []}, announce_thinking=True
        )
        assert error is None and interrupt is None
        assert reply == "Hello there!"
        # First op is the 💭 breadcrumb; the reply ends up displayed verbatim.
        assert bot.ops[0] == ("send", "💭 Thinking…", False)
        assert any("Hello there!" in text for _, text, _ in bot.ops[1:])

    asyncio.run(run())


def test_tool_call_shows_breadcrumb():
    async def run():
        items = [
            ("updates", {"agent": {"messages": [
                AIMessage("", tool_calls=[
                    {"name": "calendar_list_events", "id": "c1", "type": "tool_call", "args": {}}
                ])
            ]}}),
            ("updates", {"tools": {"messages": []}}),
            ("messages", (_chunk("You have 2 events."), {"langgraph_node": "agent"})),
            ("updates", {"agent": {"messages": [AIMessage("You have 2 events.")]}}),
        ]
        ch = _channel(items)
        ctx, bot = _ctx()
        _, reply, _ = await ch._run_with_status(1, ctx, {"messages": []}, announce_thinking=True)

        assert reply == "You have 2 events."
        texts = [text for _, text, _ in bot.ops]
        assert "📅 Checking your calendar…" in texts  # the tool breadcrumb rendered
        assert any("You have 2 events." in t for t in texts)

    asyncio.run(run())


def test_draft_interrupt_shows_approve_reject_not_a_reply():
    async def run():
        items = [
            ("updates", {"agent": {"messages": [
                AIMessage("", tool_calls=[
                    {"name": "create_draft", "id": "c1", "type": "tool_call",
                     "args": {"to": "me@x.com", "subject": "Hi", "body": "hello"}}
                ])
            ]}}),
            ("updates", {"__interrupt__": (Interrupt(
                value={"type": "approval_request", "action": "create_draft",
                       "details": {"to": "me@x.com", "subject": "Hi", "body": "hello"}}
            ),)}),
        ]
        ch = _channel(items)
        ctx, bot = _ctx()
        interrupt, reply, error = await ch._run_with_status(
            1, ctx, {"messages": []}, announce_thinking=True
        )
        assert error is None and reply is None
        assert interrupt["details"]["to"] == "me@x.com"

        await ch._deliver(1, ctx, interrupt, reply)
        # A proposal with buttons was sent; no plain reply.
        assert any(has_buttons for _, _, has_buttons in bot.ops), "expected Approve/Reject buttons"
        assert any("Subject: Hi" in text for _, text, _ in bot.ops)

    asyncio.run(run())
