"""Phase 4A.1 — /ask rich rendering + reply-threaded follow-ups. Offline: the routed model and the
Telegram bot are faked; no network. Uses the shared `channel` double so we don't spin up
build_agent(). Scaffolding lives in tests/support.
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlmodel import Session, select

from app.agent import router
from app.config import settings
from app.memory.models import HostedConsult
from tests.support import FakeBot, FakeMessage, FakeModel, make_update


def _update(text, reply_to_id=None, reply_text=None):
    replied = None
    if reply_to_id is not None:
        replied = SimpleNamespace(message_id=reply_to_id, text=reply_text or "prior answer")
    return make_update(message=FakeMessage(text, reply_to_message=replied),
                       chat_id=settings.telegram_chat_id)


# --- rendering --------------------------------------------------------------


def test_send_rich_formats_markdown_and_table_as_codeblock(channel):
    bot = FakeBot()
    md = "## Odds\n\n| Team | Odds |\n|------|------|\n| Brazil | 5.0 |\n\n**Why**\n* depth\n* form"
    msg = asyncio.run(channel._send_rich(bot, 1, md))
    sent = bot.messages[0]
    assert sent.parse_mode == "MarkdownV2"
    assert "```" in sent.text  # the table became a monospace code block
    assert "|------|" not in sent.text  # no raw markdown table separator left
    assert msg.message_id == 1


def test_send_rich_falls_back_to_plain_on_bad_markdown(channel):
    bot = FakeBot(fail_markdown=True)
    asyncio.run(channel._send_rich(bot, 1, "**hi**"))
    assert bot.messages[-1].parse_mode is None  # delivered as plain text, not MarkdownV2


# --- follow-up routing ------------------------------------------------------


def test_reply_to_known_ask_answer_routes_to_followup(channel, ctx, monkeypatch):
    channel._ask_threads = {5: [SystemMessage(content="s"), HumanMessage(content="q"), AIMessage(content="a")]}
    seen = {"followup": 0, "graph": 0}

    async def fake_followup(chat_id, ctx_, history, new_text):
        seen["followup"] += 1

    async def fake_graph(*a, **k):
        seen["graph"] += 1
        return (None, "x", None)

    async def fake_deliver(*a, **k):
        pass

    monkeypatch.setattr(channel, "_on_ask_followup", fake_followup)
    monkeypatch.setattr(channel, "_run_with_status", fake_graph)
    monkeypatch.setattr(channel, "_deliver", fake_deliver)

    asyncio.run(channel._on_message(_update("expand on that", reply_to_id=5), ctx))
    asyncio.run(channel._on_message(_update("hi", reply_to_id=999), ctx))  # unknown → graph
    asyncio.run(channel._on_message(_update("hello"), ctx))  # no reply → graph
    assert seen == {"followup": 1, "graph": 2}


# --- follow-up scrubs + audits + threads ------------------------------------


def test_followup_scrubs_audits_and_extends_thread(channel, engine, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "Stephanie")
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    fake = FakeModel("follow-up answer")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    history = [SystemMessage(content="s"), HumanMessage(content="q"), AIMessage(content="a")]
    bot = FakeBot()
    asyncio.run(channel._on_ask_followup(1, SimpleNamespace(bot=bot), history,
                                         "does Stephanie's email s@x.com matter?"))

    # model saw the prior history + a scrubbed new turn
    assert fake.received[:3] == history
    new_turn = fake.received[-1].content
    assert "Stephanie" not in new_turn and "s@x.com" not in new_turn and "[redacted]" in new_turn
    # audited
    with Session(engine) as s:
        rows = list(s.exec(select(HostedConsult)))
    assert len(rows) == 1 and rows[0].n_redactions >= 2
    # the new answer is stored as a continuable thread
    assert len(channel._ask_threads) == 1
    stored = next(iter(channel._ask_threads.values()))
    assert stored[-1].content == "follow-up answer"


def test_followup_refuses_when_hosted_off(channel, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("must not call the model when hosted is off")

    monkeypatch.setattr(router, "chat_model", _boom)
    bot = FakeBot()
    asyncio.run(channel._on_ask_followup(1, SimpleNamespace(bot=bot), [], "hi"))
    assert bot.texts and "off right now" in bot.texts[0].lower()


# --- /ask while replying includes scrubbed quoted context -------------------


def test_ask_while_replying_includes_scrubbed_context(channel, ctx, engine, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "")
    fake = FakeModel("answer")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    update = _update("/ask what does this mean?", reply_to_id=7, reply_text="my email is x@y.com and I owe $50")
    asyncio.run(channel._on_ask(update, ctx))

    sent = fake.received[-1].content
    assert "what does this mean?" in sent  # the question
    assert "Context" in sent and "x@y.com" not in sent  # quoted context included but scrubbed
