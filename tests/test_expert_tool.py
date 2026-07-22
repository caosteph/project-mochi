"""Phase 4A — the consult_expert tool + the /ask path. All offline (the routed model is faked).
Proves the safety gates: fail-closed when hosted is off, deterministic scrub + audit before
anything is sent, refusal of too-personal questions, and that /ask builds a generic-only prompt
with no memory. Scaffolding is shared — see tests/support.
"""

import asyncio

from langchain_core.messages import SystemMessage
from sqlmodel import Session, select

from app.agent import router
from app.agent.tools.expert_tools import consult_expert
from app.config import settings
from app.memory.models import HostedConsult
from tests.support import FakeMessage, FakeModel, make_update


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


def test_ask_path_builds_generic_only_prompt(channel, ctx, fake_bot, monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: False)  # local path, no audit
    fake = FakeModel("4")
    monkeypatch.setattr(router, "chat_model", lambda *a, **k: fake)

    asyncio.run(channel._on_ask(make_update(message=FakeMessage("/ask what is 2+2")), ctx))

    # the model saw ONLY [system prompt, the question] — no recalled memory, no history
    assert len(fake.received) == 2
    assert isinstance(fake.received[0], SystemMessage)
    assert fake.received[1].content == "what is 2+2"
    assert fake_bot.texts and "4" in fake_bot.texts[0]
