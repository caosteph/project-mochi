"""Phase 4A.1 — /ask rich rendering + reply-threaded follow-ups. Offline: the routed
model and the Telegram bot are faked; no network. Uses the TelegramChannel.__new__
bypass so we don't spin up build_agent().
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlmodel import Session, select

from app.agent import router
from app.channels import telegram, telegram_commands
from app.config import settings
from app.memory.models import HostedConsult


class FakeModel:
    def __init__(self, answer="a generic answer"):
        self.answer = answer
        self.received = None

    def invoke(self, messages):
        self.received = messages
        return SimpleNamespace(content=self.answer)


class Bot:
    def __init__(self, fail_md=False):
        self.calls = []  # (text, parse_mode)
        self.fail_md = fail_md
        self._n = 0

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **k):
        if parse_mode == "MarkdownV2" and self.fail_md:
            raise Exception("Bad Request: can't parse entities")
        self.calls.append((text, parse_mode))
        self._n += 1
        return SimpleNamespace(message_id=self._n)

    async def send_chat_action(self, **k):
        pass


def _chan():
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    c._ask_threads = {}
    return c


def _update(text, reply_to_id=None, reply_text=None):
    replied = None
    if reply_to_id is not None:
        replied = SimpleNamespace(message_id=reply_to_id, text=reply_text or "prior answer")
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=settings.telegram_chat_id),
        message=SimpleNamespace(text=text, reply_to_message=replied),
    )


# --- rendering --------------------------------------------------------------

def test_send_rich_formats_markdown_and_table_as_codeblock():
    chan = _chan()
    bot = Bot()
    md = "## Odds\n\n| Team | Odds |\n|------|------|\n| Brazil | 5.0 |\n\n**Why**\n* depth\n* form"
    msg = asyncio.run(chan._send_rich(bot, 1, md))
    text, parse_mode = bot.calls[0]
    assert parse_mode == "MarkdownV2"
    assert "```" in text  # the table became a monospace code block
    assert "|------|" not in text  # no raw markdown table separator left
    assert msg.message_id == 1


def test_send_rich_falls_back_to_plain_on_bad_markdown():
    chan = _chan()
    bot = Bot(fail_md=True)
    asyncio.run(chan._send_rich(bot, 1, "**hi**"))
    assert bot.calls[-1][1] is None  # delivered as plain text, not MarkdownV2


# --- follow-up routing ------------------------------------------------------

def test_reply_to_known_ask_answer_routes_to_followup(monkeypatch):
    chan = _chan()
    chan._ask_threads = {5: [SystemMessage(content="s"), HumanMessage(content="q"), AIMessage(content="a")]}
    seen = {"followup": 0, "graph": 0}

    async def fake_followup(chat_id, ctx, history, new_text):
        seen["followup"] += 1

    async def fake_graph(*a, **k):
        seen["graph"] += 1
        return (None, "x", None)

    async def fake_deliver(*a, **k):
        pass

    monkeypatch.setattr(chan, "_on_ask_followup", fake_followup)
    monkeypatch.setattr(chan, "_run_with_status", fake_graph)
    monkeypatch.setattr(chan, "_deliver", fake_deliver)

    ctx = SimpleNamespace(bot=Bot())
    asyncio.run(chan._on_message(_update("expand on that", reply_to_id=5), ctx))
    asyncio.run(chan._on_message(_update("hi", reply_to_id=999), ctx))  # unknown → graph
    asyncio.run(chan._on_message(_update("hello"), ctx))  # no reply → graph
    assert seen == {"followup": 1, "graph": 2}


# --- follow-up scrubs + audits + threads ------------------------------------

def test_followup_scrubs_audits_and_extends_thread(engine, monkeypatch):
    chan = _chan()
    monkeypatch.setattr(telegram_commands, "get_engine", lambda: engine)
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "Stephanie")
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    fake = FakeModel("follow-up answer")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    history = [SystemMessage(content="s"), HumanMessage(content="q"), AIMessage(content="a")]
    bot = Bot()
    ctx = SimpleNamespace(bot=bot)
    asyncio.run(chan._on_ask_followup(1, ctx, history, "does Stephanie's email s@x.com matter?"))

    # model saw the prior history + a scrubbed new turn
    assert fake.received[:3] == history
    new_turn = fake.received[-1].content
    assert "Stephanie" not in new_turn and "s@x.com" not in new_turn and "[redacted]" in new_turn
    # audited
    with Session(engine) as s:
        rows = list(s.exec(select(HostedConsult)))
    assert len(rows) == 1 and rows[0].n_redactions >= 2
    # the new answer is stored as a continuable thread
    assert len(chan._ask_threads) == 1
    stored = next(iter(chan._ask_threads.values()))
    assert stored[-1].content == "follow-up answer"


def test_followup_refuses_when_hosted_off(monkeypatch):
    chan = _chan()
    monkeypatch.setattr(router, "hosted_available", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("must not call the model when hosted is off")

    monkeypatch.setattr(router, "chat_model", _boom)
    bot = Bot()
    asyncio.run(chan._on_ask_followup(1, SimpleNamespace(bot=bot), [], "hi"))
    assert bot.calls and "off right now" in bot.calls[0][0].lower()


# --- /ask while replying includes scrubbed quoted context -------------------

def test_ask_while_replying_includes_scrubbed_context(engine, monkeypatch):
    chan = _chan()
    monkeypatch.setattr(telegram_commands, "get_engine", lambda: engine)
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "")
    fake = FakeModel("answer")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    update = _update("/ask what does this mean?", reply_to_id=7, reply_text="my email is x@y.com and I owe $50")
    asyncio.run(chan._on_ask(update, SimpleNamespace(bot=Bot())))

    sent = fake.received[-1].content
    assert "what does this mean?" in sent  # the question
    assert "Context" in sent and "x@y.com" not in sent  # quoted context included but scrubbed
