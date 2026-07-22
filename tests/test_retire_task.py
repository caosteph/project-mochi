"""Let a task be retired — the staleness root-fix.

Her transcript's loudest unaddressed pain: Mochi fired/asked about reminders she'd already
handled ("THIS IS SO STALE WHY WOULD YOU ASK", "I ALREADY GOT REJECTED FROM PERPLEXITY NO NEED TO
KEEP REMINDING"). Cancelling one instance never stopped the next recreation, because obsolescence
was only ever recorded per-row. A `RetiredTopic` tombstone is the topic-level mute the creation
paths now consult.

Real database throughout (a tombstone matched fuzzily, reminders cancelled, signals dismissed,
tasks marked done — none of which a mocked session can exercise). The interrupt boundary isn't
touched here; retire_task takes no user choice.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.agent.tools.reminder_tools import add_reminder, retire_task
from app.memory.models import (
    EmailSignal,
    Reminder,
    ReminderStatus,
    RetiredTopic,
    SignalStatus,
    Task,
    TaskStatus,
)
from app.proactive import reminders

# --- the engine: is_retired ------------------------------------------------


def test_is_retired_fuzzy_matches_her_phrasings_but_not_unrelated_topics(engine):
    with Session(engine) as s:
        s.add(RetiredTopic(text="submit health insurance claims"))
        s.commit()
    with Session(engine) as s:
        assert reminders.is_retired(s, "the health insurance thing")   # her later phrasing
        assert reminders.is_retired(s, "health insurance claims")
        assert not reminders.is_retired(s, "dentist appointment")      # unrelated stays live
        assert not reminders.is_retired(s, "call mom")


# --- the engine: retire_topic clears everything outstanding -----------------


def test_retire_topic_tombstones_cancels_reminders_dismisses_signals_marks_tasks(engine):
    with Session(engine) as s:
        s.add(Reminder(text="submit health insurance claims",
                       due_at=datetime.now(UTC) + timedelta(days=1),
                       status=ReminderStatus.PENDING.value))
        s.add(Reminder(text="call mom", due_at=datetime.now(UTC) + timedelta(days=1),
                       status=ReminderStatus.PENDING.value))  # unrelated — must survive
        s.add(EmailSignal(source="gmail:x", signal_type="bill", title="health insurance claim due",
                          due_date=datetime.now(UTC) + timedelta(days=2),
                          status=SignalStatus.ASKED.value))
        s.add(Task(text="finish health insurance claims", status=TaskStatus.OPEN.value))
        s.commit()

    with Session(engine) as s:
        topic, cancelled = reminders.retire_topic(s, "health insurance claims")

    assert topic == "health insurance claims" and cancelled == 1
    with Session(engine) as s:
        by_text = {r.text: r.status for r in s.exec(select(Reminder))}
        assert by_text["submit health insurance claims"] == ReminderStatus.CANCELLED.value
        assert by_text["call mom"] == ReminderStatus.PENDING.value  # untouched
        assert s.exec(select(EmailSignal)).one().status == SignalStatus.DISMISSED.value
        assert s.exec(select(Task)).one().status == TaskStatus.DONE.value
        assert s.exec(select(RetiredTopic)).one().text == "health insurance claims"


def test_retire_topic_returns_plain_values_not_orm_objects(engine):
    """Session-scope safety: the return must survive the session closing (the DetachedInstanceError
    class that bit cancel/snooze). `topic, count` are a str and an int, safe to use afterwards."""
    with Session(engine) as s:
        topic, cancelled = reminders.retire_topic(s, "anything")
    assert topic == "anything" and cancelled == 0  # readable after the session closed


# --- the seams: creation is refused for a retired topic ---------------------


def test_create_or_get_reminder_refuses_a_retired_topic(engine):
    with Session(engine) as s:
        reminders.retire_topic(s, "perplexity prep")
    with Session(engine) as s, pytest.raises(reminders.RetiredTopicError):
        reminders.create_or_get_reminder(s, text="perplexity prep", when="tomorrow at 9am")


def test_add_reminder_tool_says_its_done_instead_of_recreating(engine):
    with Session(engine) as s:
        reminders.retire_topic(s, "perplexity prep")
    out = add_reminder.invoke({"text": "prep for perplexity", "when": "tomorrow at 9am"})
    assert "done" in out.lower() and "not setting" in out.lower()
    with Session(engine) as s:  # nothing was created
        assert not [r for r in s.exec(select(Reminder)) if "perplexity" in r.text.lower()]


# (The signal detector skipping a retired topic lives in test_email_signals.py, which has the
#  Gmail-stub scaffolding — see test_retired_topic_is_never_offered_as_a_signal.)


# --- the tool ---------------------------------------------------------------


def test_retire_task_tool_returns_a_clean_confirmation(engine):
    with Session(engine) as s:
        s.add(Reminder(text="perplexity prep", due_at=datetime.now(UTC) + timedelta(days=1),
                       status=ReminderStatus.PENDING.value))
        s.commit()
    out = retire_task.invoke({"topic": "perplexity prep"})
    assert "won't bring up" in out.lower() and "perplexity prep" in out.lower()
    assert "1 reminder" in out  # named what it cleared
