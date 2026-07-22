"""cancel_reminder's two tiers: one match cancels straight away, several show a picker.

This is the Tier-1 deterministic consumer of the choice mechanism — it proves the button flow
end-to-end without depending on the 7B choosing a tool. It's also the exact scenario from her
transcript: she asked to "remove the outdated reminder" with more than one candidate and got prose
loops instead of a picker.

Real database (mocked sessions can't reproduce the session-scope bugs this area keeps hitting);
the interrupt boundary is faked to simulate her tap.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.agent.tools import reminder_tools
from app.agent.tools.reminder_tools import cancel_reminder
from app.memory.models import Reminder, ReminderStatus
from app.proactive import reminders


def _add(engine, text, days=1):
    with Session(engine) as s:
        r = Reminder(text=text, due_at=datetime.now(UTC) + timedelta(days=days),
                     status=ReminderStatus.PENDING.value)
        s.add(r)
        s.commit()
        return r.id


def _status(engine, rid):
    with Session(engine) as s:
        return s.get(Reminder, rid).status


# --- single match: no button, just cancel (her chosen behaviour) ------------

def test_one_match_cancels_directly(engine, monkeypatch):
    rid = _add(engine, "dentist appointment")
    # If it tried to ask, that's a bug — one match must not prompt.
    monkeypatch.setattr(reminder_tools, "ask_choice",
                        lambda *a: pytest.fail("should not ask when there's a single match"))
    out = cancel_reminder.invoke({"query": "the dentist reminder"})
    assert "Cancelled" in out and "dentist" in out
    assert _status(engine, rid) == ReminderStatus.CANCELLED.value


def test_no_match_says_so(engine):
    assert "couldn't find" in cancel_reminder.invoke({"query": "nonexistent thing"})


# --- several matches: a picker, and only the tapped one is cancelled --------

def test_ambiguous_match_asks_which_and_cancels_the_tapped_one(engine, monkeypatch):
    a = _add(engine, "dentist appointment", days=1)
    b = _add(engine, "dentist cleaning follow-up", days=2)

    asked = {}

    def fake_ask(question, options):
        asked["question"] = question
        asked["options"] = list(options)
        return options.index("dentist cleaning follow-up")  # she taps the second one

    monkeypatch.setattr(reminder_tools, "ask_choice", fake_ask)

    out = cancel_reminder.invoke({"query": "the dentist reminder"})

    assert "cancel" in asked["question"].lower()  # a clear prompt
    assert set(asked["options"]) == {"dentist appointment", "dentist cleaning follow-up"}
    assert "Cancelled" in out and "cleaning follow-up" in out
    # exactly the tapped one is cancelled; the other stays pending
    assert _status(engine, b) == ReminderStatus.CANCELLED.value
    assert _status(engine, a) == ReminderStatus.PENDING.value


def test_ambiguous_then_no_choice_cancels_nothing(engine, monkeypatch):
    a = _add(engine, "dentist appointment")
    b = _add(engine, "dentist cleaning follow-up")
    monkeypatch.setattr(reminder_tools, "ask_choice", lambda *a: -1)  # she dismissed it
    out = cancel_reminder.invoke({"query": "dentist"})
    assert "didn't cancel" in out.lower()
    assert _status(engine, a) == ReminderStatus.PENDING.value
    assert _status(engine, b) == ReminderStatus.PENDING.value


# --- the read-only finder the tool relies on --------------------------------

def test_find_pending_matches_is_readonly_and_ranked(engine):
    _add(engine, "dentist appointment", days=2)
    _add(engine, "dentist cleaning follow-up", days=1)
    with Session(engine) as s:
        matches = reminders.find_pending_matches(s, "dentist")
        # both match, nothing cancelled by looking
        assert {m.text for m in matches} == {"dentist appointment", "dentist cleaning follow-up"}
        assert all(m.status == ReminderStatus.PENDING.value for m in matches)


def test_find_pending_matches_ignores_already_cancelled(engine):
    rid = _add(engine, "dentist appointment")
    with Session(engine) as s:
        reminders.cancel_reminder_by_id(s, rid)
    with Session(engine) as s:
        assert reminders.find_pending_matches(s, "dentist") == []
