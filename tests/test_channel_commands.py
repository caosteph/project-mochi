"""Slash commands: the transparency, kill-switch, and briefing paths.

`telegram_commands.py` was the biggest remaining gap in `app/channels/` after the split (59%).
The `/ask` paths already have coverage in test_ask_ux.py; this covers the rest, with a bias toward
the ones where being wrong is quietly harmful:

  /sent      — the transparency half of the hosted-model design. If it under-reports, Stephanie
               believes less left the machine than actually did.
  /pause     — the proactivity kill switch. It has to work while everything else is broken.
  /briefing  — must survive a failing calendar rather than dying silently.

Offline: no real bot, model, or network. Shared scaffolding — see tests/support.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.channels import telegram_commands
from app.memory.models import HostedConsult, WebSearch
from app.proactive import jobs
from tests.support import FakeMessage, make_update


def _cmd(text=""):
    return make_update(message=FakeMessage(text))


# --- /sent : the transparency surface ---------------------------------------


def test_sent_says_so_plainly_when_nothing_has_left_the_machine(channel, ctx):
    update = _cmd("/sent")
    asyncio.run(channel._on_sent(update, ctx))
    assert "stayed local" in update.message.replies[0].lower()


def test_sent_reports_both_hosted_consults_and_web_searches(channel, ctx, engine):
    """Two different audit tables feed one list; a regression that drops either would make
    /sent under-report what actually left."""
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="what is a mortgage point", answer="...", n_redactions=2))
        s.add(WebSearch(query="weather in paris", n_redactions=0, n_results=3))
        s.commit()
    update = _cmd("/sent")
    asyncio.run(channel._on_sent(update, ctx))
    out = update.message.replies[0]
    assert "mortgage point" in out and "weather in paris" in out
    assert "2 redacted" in out  # the redaction count is the point, not decoration


def test_sent_truncates_long_entries_rather_than_flooding_the_chat(channel, ctx, engine):
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="x" * 500, answer="...", n_redactions=0))
        s.commit()
    update = _cmd("/sent")
    asyncio.run(channel._on_sent(update, ctx))
    assert "…" in update.message.replies[0]
    assert "x" * 200 not in update.message.replies[0]


def test_sent_is_ordered_newest_first(channel, ctx, engine):
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="older question", answer="a",
                            created_at=datetime.now(UTC) - timedelta(days=1)))
        s.add(HostedConsult(sent_text="newer question", answer="a",
                            created_at=datetime.now(UTC)))
        s.commit()
    update = _cmd("/sent")
    asyncio.run(channel._on_sent(update, ctx))
    out = update.message.replies[0]
    assert out.index("newer question") < out.index("older question")


# --- /pause and /resume : the kill switch -----------------------------------


def test_pause_and_resume_toggle_proactivity(channel, ctx):
    original = jobs._enabled
    try:
        asyncio.run(channel._on_pause(_cmd("/pause"), ctx))
        assert jobs._enabled is False
        asyncio.run(channel._on_resume(_cmd("/resume"), ctx))
        assert jobs._enabled is True
    finally:
        jobs.set_enabled(original)


def test_pause_confirms_in_words_she_can_act_on(channel, ctx):
    original = jobs._enabled
    try:
        update = _cmd("/pause")
        asyncio.run(channel._on_pause(update, ctx))
        assert "/resume" in update.message.replies[0]  # tells her how to undo it
    finally:
        jobs.set_enabled(original)


# --- /briefing --------------------------------------------------------------


def test_briefing_sends_the_digest(channel, ctx, monkeypatch):
    monkeypatch.setattr(telegram_commands.briefing, "build_briefing", lambda _s: "☀️ Today: nothing on")
    update = _cmd("/briefing")
    asyncio.run(channel._on_briefing(update, ctx))
    assert update.message.replies == ["☀️ Today: nothing on"]


def test_briefing_reports_an_error_instead_of_dying_silently(channel, ctx, monkeypatch):
    """Calendar I/O can fail; the failure has to reach her, not just the log."""
    def boom(_s):
        raise RuntimeError("calendar unreachable")

    monkeypatch.setattr(telegram_commands.briefing, "build_briefing", boom)
    reported = {}

    async def fake_report(chat_id, ctx_, error):
        reported["error"] = error

    monkeypatch.setattr(channel, "_report_error", fake_report)
    asyncio.run(channel._on_briefing(_cmd("/briefing"), ctx))
    assert isinstance(reported.get("error"), RuntimeError)


# --- argument-less command usage -------------------------------------------


def test_build_without_a_description_explains_itself(channel, ctx):
    update = _cmd("/build")
    asyncio.run(channel._on_build(update, ctx))
    assert "/build" in update.message.replies[0]


def test_doc_without_a_description_explains_itself(channel, ctx):
    update = _cmd("/doc")
    asyncio.run(channel._on_doc(update, ctx))
    assert "/doc" in update.message.replies[0]


def test_ask_without_a_question_explains_itself(channel, ctx):
    update = _cmd("/ask")
    asyncio.run(channel._on_ask(update, ctx))
    assert "/ask" in update.message.replies[0]


# --- the whitelist ----------------------------------------------------------


def test_unauthorized_chat_gets_nothing_from_any_command(channel, ctx, fake_bot, monkeypatch):
    """The chat_id whitelist is the first security control — every command re-checks it, and this
    asserts none of them forgot."""
    monkeypatch.setattr(channel, "_authorized", lambda _u: False)
    for handler in (channel._on_start, channel._on_ask, channel._on_sent, channel._on_build,
                    channel._on_doc, channel._on_pause, channel._on_resume, channel._on_briefing):
        update = _cmd("/whatever")
        asyncio.run(handler(update, ctx))
        assert update.message.replies == [], f"{handler.__name__} replied to an unauthorized chat"
    assert fake_bot.messages == []
