"""Inline-keyboard callbacks — including the human-in-the-loop approval gate.

These paths were 21% covered: the channel split made that visible by separating them from the
streaming engine they used to share a file with. They deserve tests more than most of the channel,
because one of them is safety rule 3 — an external write only happens after Stephanie presses
Approve, and the button press is what resumes the paused graph.

Offline: no real bot, model, or graph. `_run_with_status` is faked so we can assert what the
callback *passed* it, which is the thing that actually encodes approve-vs-reject. DB writes go to
the scratch engine (the default under conftest). Scaffolding is shared — see tests/support.
"""

import asyncio

from langgraph.types import Command
from sqlmodel import Session, select

from app.memory.models import EmailSignal, Reminder, ReminderStatus, SignalStatus
from tests.support import FakeQuery, make_update


def _tap(channel, ctx, data, chat_id=1):
    """Run one callback (a button tap) to completion."""
    asyncio.run(channel._on_callback(make_update(callback_query=FakeQuery(data), chat_id=chat_id), ctx))


# --- reminder buttons -------------------------------------------------------


def test_done_button_marks_the_reminder_done(channel, ctx, fake_bot, seed, engine):
    rid = seed.reminder("water the plants", status=ReminderStatus.SENT.value).id
    _tap(channel, ctx, f"rem:done:{rid}")
    with Session(engine) as s:
        assert s.get(Reminder, rid).status == ReminderStatus.DONE.value
    assert "done" in fake_bot.texts[0].lower()


def test_snooze_button_pushes_the_due_time_out(channel, ctx, seed, engine):
    """Regression: this handler used to return the ORM object from a closed session and then
    format `reminder.due_at`, raising DetachedInstanceError. The snooze was written but the
    confirmation crashed — press Snooze, get silence. Untested until the channel split made
    telegram_buttons.py visible at 21% coverage."""
    r = seed.reminder("water the plants", status=ReminderStatus.SENT.value, days=-1)  # already fired
    rid, before = r.id, r.due_at
    _tap(channel, ctx, f"rem:snooze:{rid}")
    with Session(engine) as s:  # read inside the session — the instance detaches on exit
        after = s.get(Reminder, rid)
        due_after, status_after = after.due_at, after.status
    assert due_after > before and status_after == ReminderStatus.PENDING.value


def test_button_for_a_deleted_reminder_says_so_instead_of_crashing(channel, ctx, fake_bot):
    _tap(channel, ctx, "rem:done:999999")
    assert "already gone" in fake_bot.texts[0].lower()


# --- email-signal buttons ---------------------------------------------------


def test_approving_a_signal_creates_the_reminder(channel, ctx, fake_bot, seed, engine):
    sid = seed.signal().id
    _tap(channel, ctx, f"sig:approve:{sid}")
    with Session(engine) as s:
        assert list(s.exec(select(Reminder))), "approval should have created a reminder"
    assert "i'll remind you" in fake_bot.texts[0].lower()


def test_rejecting_a_signal_creates_nothing_and_dismisses_it(channel, ctx, fake_bot, seed, engine):
    sid = seed.signal().id
    _tap(channel, ctx, f"sig:reject:{sid}")
    with Session(engine) as s:
        assert not list(s.exec(select(Reminder))), "reject must not create a reminder"
        assert s.get(EmailSignal, sid).status == SignalStatus.DISMISSED.value
    assert "skipped" in fake_bot.texts[0].lower()


# --- the approval gate (safety rule 3) --------------------------------------


def _capture_resume(channel, monkeypatch):
    """Replace the graph run with a recorder, returning the Command it was resumed with."""
    seen = {}

    async def fake_run(chat_id, ctx, graph_input, announce_thinking):
        seen["input"] = graph_input
        seen["announce"] = announce_thinking
        return None, "ok", None

    async def fake_deliver(*a, **k):
        seen["delivered"] = True

    monkeypatch.setattr(channel, "_run_with_status", fake_run)
    monkeypatch.setattr(channel, "_deliver", fake_deliver)
    return seen


def test_approve_resumes_the_paused_graph_with_approved_true(channel, ctx, monkeypatch):
    seen = _capture_resume(channel, monkeypatch)
    _tap(channel, ctx, "approve")
    assert isinstance(seen["input"], Command)
    assert seen["input"].resume == {"approved": True}
    assert seen["delivered"]


def test_reject_resumes_with_approved_false(channel, ctx, monkeypatch):
    """The half that matters most: anything other than a literal 'approve' must not
    be treated as consent."""
    seen = _capture_resume(channel, monkeypatch)
    _tap(channel, ctx, "reject")
    assert seen["input"].resume == {"approved": False}


def test_unauthorized_chat_never_reaches_a_handler(channel, ctx, monkeypatch):
    """The whitelist is the first security control; a callback from anywhere else is inert."""
    monkeypatch.setattr(channel, "_authorized", lambda _update: False)
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(channel, "_run_with_status", boom)
    monkeypatch.setattr(channel, "_on_reminder_button", boom)
    for data in ("approve", "rem:done:1", "sig:approve:1"):
        _tap(channel, ctx, data, chat_id=999)
    assert called["n"] == 0


def test_the_keyboard_is_cleared_so_a_button_cannot_be_pressed_twice(channel, ctx, monkeypatch):
    _capture_resume(channel, monkeypatch)
    query = FakeQuery("approve")
    asyncio.run(channel._on_callback(make_update(callback_query=query), ctx))
    assert query.markup_cleared and query.answered
