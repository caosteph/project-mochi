"""Phase 4A — the consult_expert tool + the /ask path. All offline (the routed model is
faked). Proves the safety gates: fail-closed when hosted is off, deterministic scrub +
audit before anything is sent, refusal of too-personal questions, and that /ask builds a
generic-only prompt with no memory.
"""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage
from sqlmodel import Session, select

from app.agent import router
from app.agent.tools import expert_tools
from app.agent.tools.expert_tools import consult_expert
from app.config import settings
from app.memory.models import HostedConsult


class FakeModel:
    def __init__(self, answer="a generic answer"):
        self.answer = answer
        self.received = None

    def invoke(self, messages):
        self.received = messages
        return SimpleNamespace(content=self.answer)


@pytest.fixture(autouse=True)
def _use_test_engine(engine, monkeypatch):
    monkeypatch.setattr(expert_tools, "get_engine", lambda: engine)


def _rows(engine):
    with Session(engine) as s:
        return list(s.exec(select(HostedConsult)))


def test_unavailable_when_hosted_off(engine, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("chat_model must not be called when hosted is off")

    monkeypatch.setattr(router, "chat_model", _boom)
    out = consult_expert.invoke({"question": "anything at all"})
    assert "your own knowledge" in out.lower()
    assert _rows(engine) == []  # nothing sent, nothing audited


def test_scrubs_and_audits_when_hosted_on(engine, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "Stephanie")
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    fake = FakeModel("Here is the expert view.")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    out = consult_expert.invoke(
        {"question": "Stephanie wants advice; reach her at steph@x.com about a diet plan"}
    )
    assert "expert view" in out.lower()
    # what the hosted model actually received was scrubbed — no name, no email
    sent = fake.received[-1].content
    assert "Stephanie" not in sent and "steph@x.com" not in sent and "[redacted]" in sent
    # and it's in the audit log
    rows = _rows(engine)
    assert len(rows) == 1 and rows[0].n_redactions >= 2 and "[redacted]" in rows[0].sent_text


def test_refuses_too_personal(engine, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr(settings, "redact_terms", "")
    monkeypatch.setattr(settings, "redact_max_hits", 1)
    calls = {"n": 0}

    def _cm(*a, **k):
        calls["n"] += 1
        return FakeModel()

    monkeypatch.setattr(router, "chat_model", _cm)
    out = consult_expert.invoke({"question": "email a@b.com and c@d.com and e@f.com please"})
    assert "too personal" in out.lower()
    assert calls["n"] == 0 and _rows(engine) == []  # never sent, never audited


def test_ask_path_builds_generic_only_prompt(engine, monkeypatch):
    from app.channels import telegram, telegram_commands

    monkeypatch.setattr(router, "hosted_available", lambda: False)  # local path, no audit
    monkeypatch.setattr(telegram_commands, "get_engine", lambda: engine)
    fake = FakeModel("4")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)  # skip build_agent()
    chan._ask_threads = {}

    async def _noop(*a, **k):
        pass

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **k):
            sent.append(text)

        async def send_chat_action(self, **k):
            pass

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=settings.telegram_chat_id),
        message=SimpleNamespace(text="/ask what is 2+2", reply_to_message=None, reply_text=_noop),
    )
    asyncio.run(chan._on_ask(update, SimpleNamespace(bot=Bot())))

    # the model saw ONLY [system prompt, the question] — no recalled memory, no history
    assert len(fake.received) == 2
    assert isinstance(fake.received[0], SystemMessage)
    assert fake.received[1].content == "what is 2+2"
    assert sent and "4" in sent[0]
