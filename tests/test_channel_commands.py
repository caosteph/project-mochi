"""Slash commands: the transparency, kill-switch, and briefing paths.

`telegram_commands.py` was the biggest remaining gap in `app/channels/` after the split (59%).
The `/ask` paths already have coverage in test_ask_ux.py; this covers the rest, with a bias
toward the ones where being wrong is quietly harmful:

  /sent      — the transparency half of the hosted-model design. If it under-reports, Stephanie
               believes less left the machine than actually did.
  /pause     — the proactivity kill switch. It has to work while everything else is broken.
  /briefing  — must survive a failing calendar rather than dying silently.

Offline: no bot, no model, no network.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app.channels import telegram, telegram_commands
from app.memory.models import HostedConsult, WebSearch
from app.proactive import jobs


class Message:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.reply_to_message = None

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, **kw):
        pass


def _update(text="", chat_id=1):
    msg = Message(text)
    return SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=chat_id))


@pytest.fixture
def chan(monkeypatch, engine):
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    c._ask_threads = {}
    monkeypatch.setattr(telegram_commands, "get_engine", lambda: engine)
    monkeypatch.setattr(c, "_authorized", lambda _u: True)

    async def _no_log(*a, **k):  # the turn log needs its own engine; not what's under test
        pass

    monkeypatch.setattr(c, "_log_turn", _no_log)
    return c


# --- /sent : the transparency surface ---------------------------------------

def test_sent_says_so_plainly_when_nothing_has_left_the_machine(chan):
    update = _update("/sent")
    asyncio.run(chan._on_sent(update, SimpleNamespace(bot=Bot())))
    assert "stayed local" in update.message.replies[0].lower()


def test_sent_reports_both_hosted_consults_and_web_searches(chan, engine):
    """Two different audit tables feed one list; a regression that drops either would make
    /sent under-report what actually left."""
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="what is a mortgage point", answer="...", n_redactions=2))
        s.add(WebSearch(query="weather in paris", n_redactions=0, n_results=3))
        s.commit()
    update = _update("/sent")
    asyncio.run(chan._on_sent(update, SimpleNamespace(bot=Bot())))
    out = update.message.replies[0]
    assert "mortgage point" in out and "weather in paris" in out
    assert "2 redacted" in out  # the redaction count is the point, not decoration


def test_sent_truncates_long_entries_rather_than_flooding_the_chat(chan, engine):
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="x" * 500, answer="...", n_redactions=0))
        s.commit()
    update = _update("/sent")
    asyncio.run(chan._on_sent(update, SimpleNamespace(bot=Bot())))
    assert "…" in update.message.replies[0]
    assert "x" * 200 not in update.message.replies[0]


def test_sent_is_ordered_newest_first(chan, engine):
    with Session(engine) as s:
        s.add(HostedConsult(sent_text="older question", answer="a",
                            created_at=datetime.now(UTC) - timedelta(days=1)))
        s.add(HostedConsult(sent_text="newer question", answer="a",
                            created_at=datetime.now(UTC)))
        s.commit()
    update = _update("/sent")
    asyncio.run(chan._on_sent(update, SimpleNamespace(bot=Bot())))
    out = update.message.replies[0]
    assert out.index("newer question") < out.index("older question")


# --- /pause and /resume : the kill switch -----------------------------------

def test_pause_and_resume_toggle_proactivity(chan):
    original = jobs._enabled
    try:
        asyncio.run(chan._on_pause(_update("/pause"), None))
        assert jobs._enabled is False
        asyncio.run(chan._on_resume(_update("/resume"), None))
        assert jobs._enabled is True
    finally:
        jobs.set_enabled(original)


def test_pause_confirms_in_words_she_can_act_on(chan):
    original = jobs._enabled
    try:
        update = _update("/pause")
        asyncio.run(chan._on_pause(update, None))
        assert "/resume" in update.message.replies[0]  # tells her how to undo it
    finally:
        jobs.set_enabled(original)


# --- /briefing --------------------------------------------------------------

def test_briefing_sends_the_digest(chan, monkeypatch):
    monkeypatch.setattr(telegram_commands.briefing, "build_briefing", lambda _s: "☀️ Today: nothing on")
    update = _update("/briefing")
    asyncio.run(chan._on_briefing(update, SimpleNamespace(bot=Bot())))
    assert update.message.replies == ["☀️ Today: nothing on"]


def test_briefing_reports_an_error_instead_of_dying_silently(chan, monkeypatch):
    """Calendar I/O can fail; the failure has to reach her, not just the log."""
    def boom(_s):
        raise RuntimeError("calendar unreachable")

    monkeypatch.setattr(telegram_commands.briefing, "build_briefing", boom)
    reported = {}

    async def fake_report(chat_id, ctx, error):
        reported["error"] = error

    monkeypatch.setattr(chan, "_report_error", fake_report)
    asyncio.run(chan._on_briefing(_update("/briefing"), SimpleNamespace(bot=Bot())))
    assert isinstance(reported.get("error"), RuntimeError)


# --- argument-less command usage -------------------------------------------

def test_build_without_a_description_explains_itself(chan):
    update = _update("/build")
    asyncio.run(chan._on_build(update, SimpleNamespace(bot=Bot())))
    assert "/build" in update.message.replies[0]


def test_doc_without_a_description_explains_itself(chan):
    update = _update("/doc")
    asyncio.run(chan._on_doc(update, SimpleNamespace(bot=Bot())))
    assert "/doc" in update.message.replies[0]


def test_ask_without_a_question_explains_itself(chan):
    update = _update("/ask")
    asyncio.run(chan._on_ask(update, SimpleNamespace(bot=Bot())))
    assert "/ask" in update.message.replies[0]


# --- the whitelist ----------------------------------------------------------

def test_unauthorized_chat_gets_nothing_from_any_command(monkeypatch, engine):
    """The chat_id whitelist is the first security control — every command re-checks it,
    and this asserts none of them forgot."""
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    c._ask_threads = {}
    monkeypatch.setattr(telegram_commands, "get_engine", lambda: engine)
    monkeypatch.setattr(c, "_authorized", lambda _u: False)
    bot = Bot()
    for handler in (c._on_start, c._on_ask, c._on_sent, c._on_build, c._on_doc,
                    c._on_pause, c._on_resume, c._on_briefing):
        update = _update("/whatever")
        asyncio.run(handler(update, SimpleNamespace(bot=bot)))
        assert update.message.replies == [], f"{handler.__name__} replied to an unauthorized chat"
    assert bot.sent == []
