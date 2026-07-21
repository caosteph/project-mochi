"""Inline-keyboard callbacks — including the human-in-the-loop approval gate.

These paths were 21% covered: the split made that visible by separating them from the
streaming engine they used to share a file with. They deserve tests more than most of the
channel, because one of them is safety rule 3 — an external write only happens after
Stephanie presses Approve, and the button press is what resumes the paused graph.

Offline: no bot, no model, no graph. `_run_with_status` is faked so we can assert what the
callback *passed* it, which is the thing that actually encodes approve-vs-reject.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langgraph.types import Command
from sqlmodel import Session, select

from app.channels import telegram, telegram_buttons
from app.memory.models import EmailSignal, Reminder, ReminderStatus, SignalStatus


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


class Query:
    def __init__(self, data):
        self.data = data
        self.answered = False
        self.markup_cleared = False

    async def answer(self):
        self.answered = True

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = True


def _update(data: str, chat_id: int = 1):
    return SimpleNamespace(
        callback_query=Query(data),
        effective_chat=SimpleNamespace(id=chat_id),
    )


@pytest.fixture
def chan(monkeypatch, engine):
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)  # skip build_agent()
    monkeypatch.setattr(telegram_buttons, "get_engine", lambda: engine)
    monkeypatch.setattr(c, "_authorized", lambda _update: True)
    return c


def _reminder(engine, text="water the plants") -> int:
    with Session(engine) as s:
        r = Reminder(
            text=text,
            due_at=datetime.now(UTC) - timedelta(minutes=5),
            status=ReminderStatus.SENT.value,
        )
        s.add(r)
        s.commit()
        return r.id


# --- reminder buttons -------------------------------------------------------

def test_done_button_marks_the_reminder_done(chan, engine):
    rid = _reminder(engine)
    bot = Bot()
    asyncio.run(chan._on_callback(_update(f"rem:done:{rid}"), SimpleNamespace(bot=bot)))
    with Session(engine) as s:
        assert s.get(Reminder, rid).status == ReminderStatus.DONE.value
    assert "done" in bot.sent[0].lower()


def test_snooze_button_pushes_the_due_time_out(chan, engine):
    """Regression: this handler used to return the ORM object from a closed session and then
    format `reminder.due_at`, raising DetachedInstanceError. The snooze was written but the
    confirmation crashed — press Snooze, get silence. Untested until the channel split made
    telegram_buttons.py visible at 21% coverage."""
    rid = _reminder(engine)
    with Session(engine) as s:
        before = s.get(Reminder, rid).due_at
    asyncio.run(chan._on_callback(_update(f"rem:snooze:{rid}"), SimpleNamespace(bot=Bot())))
    with Session(engine) as s:  # read inside the session — the instance detaches on exit
        after = s.get(Reminder, rid)
        due_after, status_after = after.due_at, after.status
    assert due_after > before and status_after == ReminderStatus.PENDING.value


def test_button_for_a_deleted_reminder_says_so_instead_of_crashing(chan):
    bot = Bot()
    asyncio.run(chan._on_callback(_update("rem:done:999999"), SimpleNamespace(bot=bot)))
    assert "already gone" in bot.sent[0].lower()


# --- email-signal buttons ---------------------------------------------------

def _signal(engine) -> int:
    with Session(engine) as s:
        sig = EmailSignal(
            source="gmail:abc123",
            signal_type="return",
            title="Rain jacket from REI",
            due_date=datetime.now(UTC) + timedelta(days=3),
            status=SignalStatus.ASKED.value,
        )
        s.add(sig)
        s.commit()
        return sig.id


def test_approving_a_signal_creates_the_reminder(chan, engine):
    sid = _signal(engine)
    bot = Bot()
    asyncio.run(chan._on_callback(_update(f"sig:approve:{sid}"), SimpleNamespace(bot=bot)))
    with Session(engine) as s:
        assert list(s.exec(select(Reminder))), "approval should have created a reminder"
    assert "i'll remind you" in bot.sent[0].lower()


def test_rejecting_a_signal_creates_nothing_and_dismisses_it(chan, engine):
    sid = _signal(engine)
    bot = Bot()
    asyncio.run(chan._on_callback(_update(f"sig:reject:{sid}"), SimpleNamespace(bot=bot)))
    with Session(engine) as s:
        assert not list(s.exec(select(Reminder))), "reject must not create a reminder"
        assert s.get(EmailSignal, sid).status == SignalStatus.DISMISSED.value
    assert "skipped" in bot.sent[0].lower()


# --- the approval gate (safety rule 3) --------------------------------------

def _capture_resume(chan, monkeypatch):
    """Replace the graph run with a recorder, returning the Command it was resumed with."""
    seen = {}

    async def fake_run(chat_id, ctx, graph_input, announce_thinking):
        seen["input"] = graph_input
        seen["announce"] = announce_thinking
        return None, "ok", None

    async def fake_deliver(*a, **k):
        seen["delivered"] = True

    monkeypatch.setattr(chan, "_run_with_status", fake_run)
    monkeypatch.setattr(chan, "_deliver", fake_deliver)
    return seen


def test_approve_resumes_the_paused_graph_with_approved_true(chan, monkeypatch):
    seen = _capture_resume(chan, monkeypatch)
    asyncio.run(chan._on_callback(_update("approve"), SimpleNamespace(bot=Bot())))
    assert isinstance(seen["input"], Command)
    assert seen["input"].resume == {"approved": True}
    assert seen["delivered"]


def test_reject_resumes_with_approved_false(chan, monkeypatch):
    """The half that matters most: anything other than a literal 'approve' must not
    be treated as consent."""
    seen = _capture_resume(chan, monkeypatch)
    asyncio.run(chan._on_callback(_update("reject"), SimpleNamespace(bot=Bot())))
    assert seen["input"].resume == {"approved": False}


def test_unauthorized_chat_never_reaches_a_handler(monkeypatch, engine):
    """The whitelist is the first security control; a callback from anywhere else is inert."""
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    monkeypatch.setattr(telegram_buttons, "get_engine", lambda: engine)
    monkeypatch.setattr(c, "_authorized", lambda _update: False)
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(c, "_run_with_status", boom)
    monkeypatch.setattr(c, "_on_reminder_button", boom)
    for data in ("approve", "rem:done:1", "sig:approve:1"):
        asyncio.run(c._on_callback(_update(data), SimpleNamespace(bot=Bot())))
    assert called["n"] == 0


def test_the_keyboard_is_cleared_so_a_button_cannot_be_pressed_twice(chan, monkeypatch):
    _capture_resume(chan, monkeypatch)
    update = _update("approve")
    asyncio.run(chan._on_callback(update, SimpleNamespace(bot=Bot())))
    assert update.callback_query.markup_cleared and update.callback_query.answered
