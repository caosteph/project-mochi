"""Reminder tools must return usable strings, not raise on their own success.

Three separate instances of one mistake shipped to production: a helper commits (which expires
the SQLAlchemy instance), the model object escapes the `with Session(...)` block, and formatting
its attributes afterwards raises DetachedInstanceError. Every time, the *write succeeded* and the
*confirmation crashed* — the worst shape of failure, because from Stephanie's phone it looks like
nothing happened, so she asks again:

  - the Snooze button (app/channels/telegram_buttons.py)
  - `cancel_reminder` (here) — she asked eight times in one conversation
  - `reminders.snooze`'s caller pattern generally

These tests call the real tools against a real database, which is the only way this class of bug
shows up: it cannot reproduce with a mocked session, because mocks never expire.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.agent.tools.reminder_tools import cancel_reminder, list_reminders
from app.memory.db import get_engine
from app.memory.models import Reminder, ReminderStatus


@pytest.fixture
def a_reminder(engine):
    with Session(engine) as s:
        r = Reminder(
            text="submit health insurance claims",
            due_at=datetime.now(UTC) + timedelta(days=1),
            status=ReminderStatus.PENDING.value,
        )
        s.add(r)
        s.commit()
        return r.id


def test_cancel_reminder_returns_confirmation_instead_of_raising(a_reminder):
    """The regression: this raised DetachedInstanceError after successfully cancelling."""
    out = cancel_reminder.invoke({"query": "health insurance"})
    assert "Cancelled" in out and "health insurance" in out


def test_cancel_reminder_actually_cancels(a_reminder):
    cancel_reminder.invoke({"query": "health insurance"})
    with Session(get_engine()) as s:
        assert s.get(Reminder, a_reminder).status == ReminderStatus.CANCELLED.value


def test_cancel_reminder_says_so_when_nothing_matches(engine):
    out = cancel_reminder.invoke({"query": "a reminder that does not exist"})
    assert "couldn't find" in out


def test_list_reminders_formats_without_touching_a_closed_session(a_reminder):
    out = list_reminders.invoke({})
    assert "submit health insurance claims" in out


def test_list_reminders_is_graceful_when_empty(engine):
    assert "no upcoming reminders" in list_reminders.invoke({}).lower()


def test_cancel_then_list_no_longer_shows_it(a_reminder):
    """The full loop she was trying to complete: cancel it, then confirm it's gone."""
    cancel_reminder.invoke({"query": "health insurance"})
    assert "health insurance" not in list_reminders.invoke({})


@pytest.fixture
def three_reminders(engine):
    with Session(engine) as s:
        for i, text in enumerate(
            ["dentist appointment", "call mom every sunday", "submit health insurance claims"]
        ):
            s.add(Reminder(
                text=text,
                due_at=datetime.now(UTC) + timedelta(days=i + 1),
                status=ReminderStatus.PENDING.value,
            ))
        s.commit()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("dentist", "dentist appointment"),
        ("the dentist reminder", "dentist appointment"),          # the tool's own docstring example
        ("my reminder about the dentist", "dentist appointment"),  # how she actually phrases it
        ("the mom reminder", "call mom every sunday"),
        ("the health insurance one", "submit health insurance claims"),
    ],
)
def test_cancel_matches_the_phrasings_the_model_passes_through(three_reminders, query, expected):
    """Matching used to be a bare substring test, so anything with filler words ("the …
    reminder") silently found nothing while the reminder stayed live."""
    assert expected in cancel_reminder.invoke({"query": query})


def test_fuzzy_matching_does_not_cancel_an_unrelated_reminder(three_reminders):
    """The risk of loosening the match: cancelling the wrong thing is worse than not cancelling."""
    cancel_reminder.invoke({"query": "the dentist reminder"})
    with Session(get_engine()) as s:
        by_text = {r.text: r.status for r in s.exec(select(Reminder))}
    assert by_text["dentist appointment"] == ReminderStatus.CANCELLED.value
    assert by_text["call mom every sunday"] == ReminderStatus.PENDING.value
    assert by_text["submit health insurance claims"] == ReminderStatus.PENDING.value


def test_ambiguous_match_cancels_exactly_one_and_names_it(engine):
    with Session(engine) as s:
        for i, text in enumerate(["dentist appointment", "dentist cleaning follow-up"]):
            s.add(Reminder(text=text, due_at=datetime.now(UTC) + timedelta(days=i + 1),
                           status=ReminderStatus.PENDING.value))
        s.commit()
    out = cancel_reminder.invoke({"query": "the dentist reminder"})
    with Session(get_engine()) as s:
        cancelled = [r.text for r in s.exec(select(Reminder))
                     if r.status == ReminderStatus.CANCELLED.value]
    assert len(cancelled) == 1, "must not cancel both on an ambiguous query"
    assert cancelled[0] in out, "the reply must name what it cancelled, so a wrong pick is visible"


def test_recurring_reminder_renders_its_cadence(engine):
    with Session(engine) as s:
        s.add(Reminder(
            text="call mom",
            due_at=datetime.now(UTC) + timedelta(days=2),
            status=ReminderStatus.PENDING.value,
            recurrence="weekly",
        ))
        s.commit()
    assert "every weekly" in list_reminders.invoke({})
